"""intake.roadmap —— 自 ``orchd/intake.py`` 拆出。

ROADMAP 规划章节落地（roadmap_land）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .guard import _DIRTY_AFTER_INIT_HINT, _INTAKE_PRODUCT_FILES, _resolve_lock_orchd_dir
from .parse import _atomic_write_text

def _roadmap_land_entry(header: str, version: str, roadmap_rel: str, section_id: str) -> str:
    """构造 ROADMAP 落地 IDEAS study 条目（date 用运行日，标题取章节头）。

    ``section_id`` 来自 ROADMAP 章节的 ``id:`` 声明（``roadmap_land`` 已校验其
    存在，缺失时以 ``no_section_id`` 拒绝）——**必须写入条目** ``- id:`` 字段：
    它是 E025 溯源校验与自动归档的权威锚点，此前因漏传该参数导致落地条目缺 id，
    既无法被 source 引用（amend 报 E025），也永不自动归档。
    """
    import datetime

    date = datetime.date.today().isoformat()
    return (
        f"## {date} {header}\n"
        f"- status: study\n"
        f"- id: {section_id}\n"
        f"- goal: 把 ROADMAP §{version} 规划内容落地拆解为具体任务并注册到 _master.json。\n"
        f"- idea: ROADMAP §{version}（{header}）落地。\n"
        f"- detail: {roadmap_rel} §{version}\n"
        f"- notes: 由 orchd roadmap-land 生成（intake-dual-path，study 待用户 confirm 升 pending 后再摄入拆解）。\n"
    )


def _entry_id_exists(text: str, entry_id: str) -> bool:
    """IDEAS 文本中是否存在 ``- id:`` 精确命中的条目（幂等判据共用）。

    行级精确值比对（``- id:`` 与 ``id:`` 两种写法）；与
    ``cli.commands.ideas`` 追记定位的同构扫描保持一致。注释示例内的
    ``- id: <slug>`` 字面值永不等于真实 slug，无需预剔注释。
    """
    for line in text.splitlines():
        s = line.strip()
        if (s.startswith("- id:") and s[len("- id:"):].strip() == entry_id) or (
            s.startswith("id:") and s[len("id:"):].strip() == entry_id
        ):
            return True
    return False


def roadmap_land(
    project_root: Path,
    version: str,
) -> dict[str, Any]:
    """roadmap-land 落地：为 ROADMAP 规划章节生成 IDEAS study 条目（intake-dual-path）。

    双路径「有规划」入口：ROADMAP（意图层）→ 落地进 IDEAS（执行层）→ 摄入拆解 → 任务池。

    Args:
        project_root: 仓库根目录。
        version: 规划章节版本（如 ``1.3``）。

    Returns:
        结构化结果，永不抛异常：
        - 前置失败: ``{"landed": False, "reason": "not_on_main" | "dirty_workspace", ...}``
        - 读盘失败: ``{"landed": False, "reason": "read_error", ...}``（ROADMAP.md /
          IDEAS.md / IDEAS-archive.md 任一不可按 UTF-8 读出即拒——判重面缺一角就不落地，
          task-idea-archive-diagnostics 起走 :func:`orchd.ledger.read_text_or_error`）
        - 定位失败: ``{"landed": False, "reason": "roadmap_missing" | "section_not_found"
          | "historical_section" | "no_section_id", ...}``
        - 幂等跳过: ``{"landed": False, "reason": "already_landed", ...}``
        - 成功: ``{"landed": True, "version", "section_id", "commit": {...}, 可选
          "commit_warning": {...}}``
    """
    from orchd.gitops import (
        ensure_committed,
        get_current_branch,
        list_tracked_changes,
    )
    from orchd.ledger import (
        read_text_or_error,
        resolve_roadmap_path,
        resolve_workspace_root,
    )
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )
    from orchd.line_ctx import resolve_trunk_for
    from orchd.spec import _parse_roadmap_sections

    project_root = Path(project_root)

    # 1) 前置守卫：主干 + 非摄入产物干净（对齐 intake_commit 语义）
    current_branch = get_current_branch(project_root)
    # task-line-guard-intake-wiring：主干按当前线解析（单线恒 main）
    default_branch = resolve_trunk_for(project_root)
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
                    "请先提交或还原摄入产物（IDEAS.md / IDEAS-archive.md / ROADMAP.md / "
                    "_master.json）之外的文件改动（untracked 工具/配置文件不阻塞）"
                    + _DIRTY_AFTER_INIT_HINT
                ),
            }

    # 2) 定位 ROADMAP 规划章节（宿主根优先、.orchd/ 回退）
    # task-roadmap-root-resolution：ROADMAP 走独立定位 helper，不再由工作区文档根
    # 拼出——.orchd/ 下存在 IDEAS / SKILL 会把工作区根锁在 .orchd/，宿主根 ROADMAP
    # 会被漏判为 roadmap_missing（与 validate 系定位分裂）。IDEAS 仍走工作区文档根
    # （捆绑判定语义零回归）。
    ws = resolve_workspace_root(project_root)
    roadmap = resolve_roadmap_path(project_root)
    if not roadmap.exists():
        return {"landed": False, "reason": "roadmap_missing", "hint": f"缺 {roadmap}"}
    roadmap_text, roadmap_err = read_text_or_error(roadmap)
    if roadmap_err is not None:
        return {
            "landed": False,
            "reason": "read_error",
            "version": version,
            "hint": f"ROADMAP 不可按 UTF-8 读出：{roadmap}（权限 / 目录形 / 编码）",
        }
    sections = _parse_roadmap_sections(roadmap_text)
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

    # 3) 幂等：同一章节已落地 → 跳过。双判据（unified-intake-eng-roadmapland）：
    # ① 章节 id 精确命中（- id: 主判据：重定基改版本号后 §字面失效时仍能认出）；
    # ② §版本 字面命中（兼容存量无 id 条目；且与 E031「§版本文本即已落地」
    # 判据对齐——E031 放行处 land 必跳过，两边不打架）。
    # 判重面 = IDEAS.md ∪ IDEAS-archive.md（task-roadmap-land-dedup-across-archive）：
    # 条目被裁决归档后正文与 `- id:` 原样留在归档面，只查 IDEAS.md 会让同章节
    # 二次 land 双落同 id，E025 溯源当场分叉。面仅此两文件，不含任何其它 md。
    # 判重面两个文件都走唯一读盘兜底原语（task-idea-archive-hardening）：此前是裸
    # read_text，非 UTF-8 / 目录形输入会让本函数把异常穿到 CLI（实测落 E999 exit 1，
    # 不建锁不动文件）。语义由"意外出界"改成"结构化拒绝"，与 idea_propose 的
    # archive_unreadable 同族。
    ideas = ws / "IDEAS.md"
    ideas_text, ideas_err = (
        read_text_or_error(ideas) if ideas.exists() else ("", None)
    )
    archive = ws / "IDEAS-archive.md"
    archive_text, archive_err = (
        read_text_or_error(archive) if archive.exists() else ("", None)
    )
    if ideas_err is not None or archive_err is not None:
        return {
            "landed": False,
            "reason": "read_error",
            "version": version,
            "hint": (
                "IDEAS.md / IDEAS-archive.md 不可按 UTF-8 读出（判重面缺一角即不落地）："
                "先确认两文件能按 UTF-8 读出（权限 / 是否被换成目录 / 编码）后重试"
            ),
        }
    id_hit = _entry_id_exists(ideas_text, sec["id"]) or _entry_id_exists(
        archive_text, sec["id"])
    literal_hit = f"§{version}" in ideas_text or f"§{version}" in archive_text
    if id_hit or literal_hit:
        landed_resp: dict[str, Any] = {
            "landed": False,
            "reason": "already_landed",
            "version": version,
            "hint": (
                f"IDEAS.md / IDEAS-archive.md 已有引用 ROADMAP §{version} 的落地条目"
            ),
        }
        # detail 陈旧提示（task-caliber-residue-and-surface-gate AC6）：条目按 **id** 命中
        # 即算已落地，但正文里的 § 版本号与当前章节版本不一致（章节被重定基 / 改名）。
        # E031 侧已对齐同一真源、不再为这种形态恒告警，故这里给一条**只读提示**指明该
        # 改哪一行即可——不新增错误码、不动失败路径（additive，键仅在陈旧时出现）。
        if id_hit and not literal_hit:
            landed_resp["detail_stale"] = {
                "section_id": sec["id"],
                "expected": f"§{version}",
                "hint": (
                    f"该章节落地条目未引用 §{version}：land 因 id 命中而跳过、E031 亦按 id "
                    f"放行；若版本号确已变更，请把该条目 detail 的 § 版本号改为 §{version}"
                ),
            }
        return landed_resp

    # 4) 生成 IDEAS study 条目（追加到 IDEAS.md 末尾）—— 受准入写锁串行
    # （task-admission-lock-engine：D 项，与 amend 共用同一把 .intake.lock）
    orchd_dir = _resolve_lock_orchd_dir(project_root)
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        roadmap_rel = roadmap.relative_to(project_root).as_posix()
        entry = _roadmap_land_entry(sec["header"], version, roadmap_rel, sec["id"])
        if ideas.exists():
            existing, existing_err = read_text_or_error(ideas)
            if existing_err is not None:
                return {
                    "landed": False,
                    "reason": "read_error",
                    "version": version,
                    "hint": "IDEAS.md 不可按 UTF-8 读出：先修好该文件再 roadmap-land",
                }
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
