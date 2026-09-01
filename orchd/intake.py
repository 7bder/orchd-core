"""Orchd 摄入产物提交引擎（叶子模块，零 orchd 内部依赖）。

intake-commit-enforcement（2026-08-14）：
把「摄入产物（IDEAS.md / ROADMAP.md）的提交」从约定层下沉为引擎命令
``orchd intake``——前置守卫（main + 非摄入产物干净）+ IDEAS 条目状态校验
+ 强制提交。与 amend 互补：amend 负责「注册 + 提交」，intake 负责「只改
IDEAS/ROADMAP、暂不注册任务」场景的提交。

安全约束：
- 只提交 IDEAS.md / ROADMAP.md（经 ``resolve_workspace_root`` 定位，兼容
  根布局与发布态 .orchd 布局），不触碰 _master.json（注册归 amend）；
- 不 push、不新增 ledger 事件、不改事件格式（与 gitops best-effort 语义一致，
  不触碰 §9.1 状态机——不做历史规划的 proposal/confirm 状态机）。

依赖方向：intake.py → 标准库（pathlib）+ 惰性导入 gitops / ledger（避免循环）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


def _atomic_write_text(path: Path, text: str) -> None:
    """P2-12：原子写（tmp + os.replace），避免读-改-写中途崩溃产生半截 IDEAS.md。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))

# IDEAS.md 条目 status 合法值白名单（与 IDEAS.md 模板状态流转一致）
# study（论证中，2026-08-15 idea-write-gate）：idea propose 写入的初始态，待用户 confirm/drop 裁决。
_VALID_IDEA_STATUSES = frozenset({"pending", "taskified", "questioning", "dropped", "study"})

# 摄入产物文件白名单（与 split._INTAKE_PRODUCT_FILES 对齐，两种布局）：
# 摄入 → amend / intake 的正当链路中允许未提交态；其余已跟踪改动视为非摄入脏。
_INTAKE_PRODUCT_FILES = frozenset({
    ".orchd/_master.json",
    "IDEAS.md",
    ".orchd/IDEAS.md",
    "ROADMAP.md",
    ".orchd/ROADMAP.md",
})

# 条目标题中的显式 id 约定写法：`<标题>（id: <slug>）`
# slug 仅允许字母、数字、连字符、下划线，且以字母或数字开头（与 source: idea:<id>
# 的引用形态一致；中文标题无法可靠派生 ASCII slug，故 id 必须显式声明）。
_TITLE_ID_RE = re.compile(r"id:\s*([A-Za-z0-9][A-Za-z0-9_-]*)")


def _parse_title_id(title: str) -> str | None:
    """从灵感标题中解析显式 id（约定写法 ``...（id: <slug>）``）。

    ``- id:`` 是 E025 溯源校验（``spec._check_idea_reference``）与自动归档
    （``ideas._entry_is_resolved``）的权威锚点：缺 id 的条目既无法通过 source
    引用校验，也永不自动归档。写入侧必须产出该字段，否则与读取侧契约断裂。

    Returns:
        解析到的 slug；标题未声明 id 时返回 None（调用方应 fail-closed 拒绝）。
    """
    m = _TITLE_ID_RE.search(title or "")
    return m.group(1) if m else None


def _parse_idea_statuses(text: str) -> list[tuple[int, str, str]]:
    """解析 IDEAS.md 各条目的 status 字段，返回 [(行号, 标题, status)]。

    条目识别：``## `` 开头的行为标题；其后、下一个 ``## `` 之前行中匹配
    ``- status: <值>`` 或 ``status: <值>``（对齐 spec._check_idea_reference 解析）。
    """
    entries: list[tuple[int, str, str]] = []
    current_title = ""
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_title = stripped[3:].strip()
        elif current_title:
            for marker in ("- status:", "status:"):
                if stripped.startswith(marker):
                    entries.append((i, current_title, stripped[len(marker):].strip()))
                    break
    return entries


def check_idea_statuses(workspace_root: Path) -> list[dict[str, Any]]:
    """校验 IDEAS.md 条目 status 合法性，返回违规清单（空 = 合法）。

    warning 级（不阻断提交）：非法 status 说明整理/状态流转有误，须人工核对。
    """
    ideas_path = workspace_root / "IDEAS.md"
    if not ideas_path.exists():
        return []
    try:
        text = ideas_path.read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return []
    violations: list[dict[str, Any]] = []
    for lineno, title, status in _parse_idea_statuses(text):
        if status not in _VALID_IDEA_STATUSES:
            violations.append({
                "line": lineno + 1,
                "title": title,
                "status": status,
                "allowed": sorted(_VALID_IDEA_STATUSES),
            })
    return violations


def _intake_guard(project_root: Path) -> dict[str, Any] | None:
    """摄入前置守卫：main 分支 + 非摄入产物干净（对齐 amend 的 E017 语义）。

    供 intake_commit / idea_propose / idea_confirm / idea_drop 复用（idea-write-gate）。

    Returns:
        - 通过: ``None``。
        - 失败: 结构化错误 dict（not_on_main / dirty_workspace），调用方直接返回。
    """
    from orchd.gitops import get_current_branch, get_default_branch, list_tracked_changes

    project_root = Path(project_root)
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        return {
            "committed": False,
            "reason": "not_on_main",
            "branch": current_branch,
            "hint": f"intake/idea 仅在 default（{default_branch}）分支执行，请先切回",
        }
    dirty_files = list_tracked_changes(project_root)
    if dirty_files is not None:
        non_intake = [f for f in dirty_files if f not in _INTAKE_PRODUCT_FILES]
        if non_intake:
            return {
                "committed": False,
                "reason": "dirty_workspace",
                "dirty_files": non_intake,
                "hint": (
                    "请先提交或还原摄入产物（IDEAS.md / ROADMAP.md / _master.json）"
                    "之外的文件改动（untracked 工具/配置文件不阻塞）"
                ),
            }
    return None


def intake_commit(
    project_root: Path,
    message: str | None = None,
) -> dict[str, Any]:
    """摄入产物校验 + 强制提交（引擎命令 ``orchd intake`` 的后端）。

    Args:
        project_root: 仓库根目录。
        message: 提交消息（缺省用默认 chore(intake) 前缀）。

    Returns:
        结构化结果，永不抛异常：
        - 前置失败（非 main）: ``{"committed": False, "reason": "not_on_main",
          "branch": ...}``
        - 前置失败（非摄入脏）: ``{"committed": False, "reason": "dirty_workspace",
          "dirty_files": [...], "hint": ...}``
        - 提交结果: ``{"committed": bool, "commit": {...}, 可选
          "status_warnings": [...], 可选 "commit_warning": {...}}``
    """
    from orchd.gitops import ensure_committed
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    project_root = Path(project_root)
    # canonical 共享读（task-canonical-workspace-docs，2026-08-25）：container 布局
    # 下 resolve_workspace_root 解析到 canonical 主工作树根，摄入产物（IDEAS.md /
    # ROADMAP.md）统一在 main 工作树定位与提交，任务 worktree 本地副本不参与。

    # 1) 前置守卫：main 分支 + 非摄入产物干净（对齐 amend 的 E017 语义）
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        return guard_err

    # 2) IDEAS 条目状态校验（warning 不阻断）
    # canonical 工作区根（task-canonical-workspace-docs，2026-08-25）：
    # resolve_workspace_root 先解析到 canonical 主工作树根（container 布局返回
    # main/，flat 返回本地），IDEAS.md / ROADMAP.md 以主工作树副本为权威，
    # 避免任务 worktree 本地 .orchd/ 拷贝过期导致摄入不一致。
    workspace_root = resolve_workspace_root(project_root)
    status_warnings = check_idea_statuses(workspace_root)

    # 3) 强制提交摄入产物（IDEAS.md + ROADMAP.md）—— 受准入写锁串行
    # （task-admission-lock-engine：D 项，与 amend 共用同一把 .intake.lock）
    orchd_dir = workspace_root / ".orchd"
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        paths = [str(workspace_root / "IDEAS.md"), str(workspace_root / "ROADMAP.md")]
        commit_message = message or "chore(intake): orchd intake — commit intake products"
        commit = ensure_committed(project_root, paths, commit_message)
        result: dict[str, Any] = {
            "committed": commit.get("performed") is True,
            "commit": commit,
        }
        if status_warnings:
            result["status_warnings"] = status_warnings
        # 提交未执行（非 no_changes）→ commit_warning（降级可审计化）
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"摄入产物 commit 未执行（{commit.get('reason')}）：改动可能未入库，"
                    "请人工核对（可运行 orchd status --audit-intake 巡检）"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def _roadmap_land_entry(header: str, version: str, roadmap_rel: str, section_id: str) -> str:
    """构造 ROADMAP 落地 IDEAS pending 条目（date 用运行日，标题取章节头）。

    ``section_id`` 来自 ROADMAP 章节的 ``id:`` 声明（``roadmap_land`` 已校验其
    存在，缺失时以 ``no_section_id`` 拒绝）——**必须写入条目** ``- id:`` 字段：
    它是 E025 溯源校验与自动归档的权威锚点，此前因漏传该参数导致落地条目缺 id，
    既无法被 source 引用（amend 报 E025），也永不自动归档。
    """
    import datetime

    date = datetime.date.today().isoformat()
    return (
        f"## {date} {header}\n"
        f"- status: pending\n"
        f"- id: {section_id}\n"
        f"- goal: 把 ROADMAP §{version} 规划内容落地拆解为具体任务并注册到 _master.json。\n"
        f"- idea: ROADMAP §{version}（{header}）落地。\n"
        f"- detail: {roadmap_rel} §{version}\n"
        f"- notes: 由 orchd roadmap-land 生成（intake-dual-path），待摄入拆解为任务。\n"
    )


def roadmap_land(
    project_root: Path,
    version: str,
) -> dict[str, Any]:
    """roadmap-land 落地：为 ROADMAP 规划章节生成 IDEAS pending 条目（intake-dual-path）。

    双路径「有规划」入口：ROADMAP（意图层）→ 落地进 IDEAS（执行层）→ 摄入拆解 → 任务池。

    Args:
        project_root: 仓库根目录。
        version: 规划章节版本（如 ``1.3``）。

    Returns:
        结构化结果，永不抛异常：
        - 前置失败: ``{"landed": False, "reason": "not_on_main" | "dirty_workspace", ...}``
        - 定位失败: ``{"landed": False, "reason": "roadmap_missing" | "section_not_found"
          | "historical_section" | "no_section_id", ...}``
        - 幂等跳过: ``{"landed": False, "reason": "already_landed", ...}``
        - 成功: ``{"landed": True, "version", "section_id", "commit": {...}, 可选
          "commit_warning": {...}}``
    """
    from orchd.gitops import (
        ensure_committed,
        get_current_branch,
        get_default_branch,
        list_tracked_changes,
    )
    from orchd.ledger import resolve_workspace_root
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )
    from orchd.spec import _parse_roadmap_sections

    project_root = Path(project_root)

    # 1) 前置守卫：main + 非摄入产物干净（对齐 intake_commit 语义）
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        return {
            "landed": False,
            "reason": "not_on_main",
            "branch": current_branch,
            "hint": f"roadmap-land 仅在 default（{default_branch}）分支执行，请先切回",
        }
    dirty_files = list_tracked_changes(project_root)
    if dirty_files is not None:
        non_intake = [f for f in dirty_files if f not in _INTAKE_PRODUCT_FILES]
        if non_intake:
            return {
                "landed": False,
                "reason": "dirty_workspace",
                "dirty_files": non_intake,
                "hint": (
                    "请先提交或还原摄入产物（IDEAS.md / ROADMAP.md / _master.json）"
                    "之外的文件改动（untracked 工具/配置文件不阻塞）"
                ),
            }

    # 2) 定位 ROADMAP 规划章节（.orchd 布局 / 根布局）
    ws = resolve_workspace_root(project_root)
    roadmap = ws / "ROADMAP.md"
    if not roadmap.exists():
        return {"landed": False, "reason": "roadmap_missing", "hint": f"缺 {roadmap}"}
    sections = _parse_roadmap_sections(roadmap.read_text(encoding="utf-8"))
    sec = next((s for s in sections if s["version"] == version), None)
    available = [s["version"] for s in sections if not s["historical"] and s["id"]]
    if sec is None:
        return {
            "landed": False,
            "reason": "section_not_found",
            "version": version,
            "available": available,
        }
    if sec["historical"]:
        return {"landed": False, "reason": "historical_section", "version": version}
    if not sec["id"]:
        return {"landed": False, "reason": "no_section_id", "version": version}

    # 3) 幂等：IDEAS 已有引用该章节的落地条目 → 跳过
    ideas = ws / "IDEAS.md"
    ideas_text = ideas.read_text(encoding="utf-8") if ideas.exists() else ""
    if f"§{version}" in ideas_text:
        return {
            "landed": False,
            "reason": "already_landed",
            "version": version,
            "hint": f"IDEAS.md 已有引用 ROADMAP §{version} 的落地条目",
        }

    # 4) 生成 IDEAS pending 条目（追加到 IDEAS.md 末尾）—— 受准入写锁串行
    # （task-admission-lock-engine：D 项，与 amend 共用同一把 .intake.lock）
    orchd_dir = ws / ".orchd"
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        roadmap_rel = roadmap.relative_to(project_root).as_posix()
        entry = _roadmap_land_entry(sec["header"], version, roadmap_rel, sec["id"])
        if ideas.exists():
            existing = ideas.read_text(encoding="utf-8")
            if not existing.endswith("\n"):
                existing += "\n"
            _atomic_write_text(ideas, existing + "\n" + entry)
        else:
            _atomic_write_text(ideas, "# IDEAS\n" + "\n" + entry)

        # 5) 强制提交摄入产物（IDEAS.md + ROADMAP.md）
        commit = ensure_committed(
            project_root,
            [str(ws / "IDEAS.md"), str(ws / "ROADMAP.md")],
            f"chore(intake): orchd roadmap-land — §{version}",
        )
        result: dict[str, Any] = {
            "landed": True,
            "version": version,
            "section_id": sec["id"],
            "commit": commit,
        }
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"roadmap-land 落地 commit 未执行（{commit.get('reason')}）：IDEAS.md 改动"
                    "可能未入库，请人工核对"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def _find_idea_entry(text: str, title: str) -> dict[str, Any] | None:
    """定位 IDEAS.md 中标题匹配 ``title`` 的条目（idea-write-gate）。

    条目识别：``## `` 开头行为标题（约定格式 ``## YYYY-MM-DD <标题>``，日期为
    条目内自带的自然日期前缀）；``title`` 匹配标题**去除日期前缀后的部分**
    （整标题 == title，或以 ``" " + title`` 结尾），与 propose 写入的
    ``## {date} {title}`` 格式对齐。其后、下一个 ``## `` 之前的行收集字段
    （status / 其余 ``- key: value`` 或 ``key: value``）。

    Returns:
        ``{"header_line": int, "title": str, "status": str, "lines": [str], "end": int}``：
        header_line 为标题行号（0 基），lines 为条目全部行（含标题），end 为条目结束行号
        （不含，下一标题行号或文件末尾）。无匹配返回 None。
    """
    lines = text.splitlines()
    target: dict[str, Any] | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        header = stripped[3:].strip()
        # 匹配标题本体：整标题相等，或标题以 " {title}" 结尾（去掉日期前缀）
        if header == title or header.endswith(" " + title):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("## "):
                j += 1
            status = ""
            for k in range(i + 1, j):
                s = lines[k].strip()
                for marker in ("- status:", "status:"):
                    if s.startswith(marker):
                        status = s[len(marker):].strip()
                        break
            target = {
                "header_line": i,
                "title": header,
                "status": status,
                "lines": lines[i:j],
                "end": j,
            }
            break
    return target


def idea_propose(project_root: Path, title: str, feasibility: str) -> dict[str, Any]:
    """灵感写入 IDEAS 写入门禁：追加 status: study 条目（idea-write-gate，propose）。

    执行权：agent 可执行（记录论证中的灵感，待用户 confirm/drop 裁决）。

    Args:
        project_root: 仓库根目录。
        title: 灵感标题。
        feasibility: 可行性论证（写入 ``- 论证:`` 字段）。

    Returns:
        结构化结果，永不抛异常：
        - 前置失败: ``{"proposed": False, "reason": "not_on_main" | "dirty_workspace", ...}``
        - 重复标题: ``{"proposed": False, "reason": "duplicate_title", ...}``
        - 成功: ``{"proposed": True, "title", "commit": {...}, 可选 "commit_warning"}``
    """
    from orchd.gitops import ensure_committed
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    project_root = Path(project_root)
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        # guard 返回 committed 键，转换为该动作的键（proposed），保留 reason 与明细
        return {"proposed": False, **{k: v for k, v in guard_err.items() if k != "committed"}}

    ws = resolve_workspace_root(project_root)
    ideas = ws / "IDEAS.md"
    text = ideas.read_text(encoding="utf-8") if ideas.exists() else ""
    if _find_idea_entry(text, title) is not None:
        return {
            "proposed": False,
            "reason": "duplicate_title",
            "title": title,
            "hint": f"IDEAS.md 已存在标题为 '{title}' 的条目",
        }

    # 显式 id 强校验（fail-closed）：- id: 是 E025 溯源校验与自动归档的权威锚点。
    # 缺 id 的条目后续必然失败（amend 报 E025 source 引用不存在）且永不自动归档，
    # 故在此早失败，而非让问题推迟到注册阶段才暴露（对齐引擎硬约束取向）。
    idea_id = _parse_title_id(title)
    if not idea_id:
        return {
            "proposed": False,
            "reason": "missing_idea_id",
            "title": title,
            "hint": (
                "标题须含显式 id，写法：`<标题>（id: <slug>）`；"
                "slug 仅允许字母 / 数字 / 连字符 / 下划线，且以字母或数字开头。"
                "例：`引擎硬化整改（id: audit-engine-hardening）`。"
                "原因：- id: 是 E025 溯源校验与自动归档的权威锚点，缺 id 的条目"
                "既无法被 source 引用（amend 必报 E025），也永不自动归档。"
            ),
        }

    import datetime

    # 写入 IDEAS.md —— 受准入写锁串行（task-admission-lock-engine：D 项）
    orchd_dir = ws / ".orchd"
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        date = datetime.date.today().isoformat()
        entry = (
            f"## {date} {title}\n"
            f"- status: study\n"
            f"- id: {idea_id}\n"
            f"- 论证: {feasibility}\n"
            f"- notes: 由 orchd idea propose 写入（idea-write-gate），待用户 confirm 升 pending 或 drop 丢弃。\n"
        )
        if ideas.exists():
            existing = text
            if not existing.endswith("\n"):
                existing += "\n"
            _atomic_write_text(ideas, existing + "\n" + entry)
        else:
            _atomic_write_text(ideas, "# IDEAS\n" + "\n" + entry)

        commit = ensure_committed(
            project_root,
            [str(ideas)],
            f"chore(idea): orchd idea propose — {title}",
        )
        result: dict[str, Any] = {
            "proposed": True,
            "title": title,
            "id": idea_id,
            "commit": commit,
        }
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"idea propose commit 未执行（{commit.get('reason')}）：IDEAS.md 改动"
                    "可能未入库，请人工核对"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def _idea_transition(
    project_root: Path,
    title: str,
    target_status: str,
    action: str,
) -> dict[str, Any]:
    """idea confirm / drop 公共后端：把 status: study 条目改写为 target_status。

    执行权：仅用户执行（confirm/drop 是裁决动作，agent 不得代行）。

    Returns:
        结构化结果，永不抛异常（结果键用动作过去式：confirm→confirmed / drop→dropped）：
        - 前置失败: ``{"{key}": False, "reason": "not_on_main" | "dirty_workspace"}``
        - 不存在: ``{"{key}": False, "reason": "not_found"}``
        - 非 study: ``{"{key}": False, "reason": "not_study", "current_status": ...}``
        - 成功: ``{"{key}": True, "title", "new_status", "commit": {...}}``
    """
    from orchd.gitops import ensure_committed
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    # 动作 → 结果键（过去式）：confirm→confirmed，drop→dropped
    key = "dropped" if action == "drop" else "confirmed"

    project_root = Path(project_root)
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        # guard 返回 committed 键，转换为该动作的过去式键，保留 reason 与明细
        return {key: False, **{k: v for k, v in guard_err.items() if k != "committed"}}

    ws = resolve_workspace_root(project_root)
    ideas = ws / "IDEAS.md"
    if not ideas.exists():
        return {key: False, "reason": "not_found", "title": title}
    text = ideas.read_text(encoding="utf-8")
    entry = _find_idea_entry(text, title)
    if entry is None:
        return {key: False, "reason": "not_found", "title": title}
    if entry["status"] != "study":
        return {
            key: False,
            "reason": "not_study",
            "title": title,
            "current_status": entry["status"],
            "hint": f"仅 status: study 条目可 {action}（当前 '{entry['status']}'）",
        }

    lines = entry["lines"]
    # 改写状态 + 提交 —— 受准入写锁串行（task-admission-lock-engine：D 项）
    orchd_dir = ws / ".orchd"
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        new_lines: list[str] = []
        replaced = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("- status:") or s.startswith("status:"):
                indent = ln[: len(ln) - len(ln.lstrip())]
                new_lines.append(f"{indent}- status: {target_status}")
                replaced = True
            else:
                new_lines.append(ln)
        if not replaced:
            # 理论上不会走到（_find_idea_entry 已确认含 status），防御性兜底
            new_lines.append(f"- status: {target_status}")

        all_lines = text.splitlines()
        new_content_lines = all_lines[: entry["header_line"]] + new_lines + all_lines[entry["end"]:]
        _atomic_write_text(ideas, "\n".join(new_content_lines) + "\n")

        commit = ensure_committed(
            project_root,
            [str(ideas)],
            f"chore(idea): orchd idea {action} — {title}",
        )
        result: dict[str, Any] = {
            key: True,
            "title": title,
            "new_status": target_status,
            "commit": commit,
        }
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"idea {action} commit 未执行（{commit.get('reason')}）：IDEAS.md 改动"
                    "可能未入库，请人工核对"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def idea_confirm(project_root: Path, title: str) -> dict[str, Any]:
    """把 status: study 条目升为 pending（idea-write-gate，confirm，仅用户执行）。"""
    return _idea_transition(project_root, title, "pending", "confirm")


def idea_drop(project_root: Path, title: str) -> dict[str, Any]:
    """把 status: study 条目降为 dropped（idea-write-gate，drop，仅用户执行）。"""
    return _idea_transition(project_root, title, "dropped", "drop")
