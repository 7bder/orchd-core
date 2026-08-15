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

from pathlib import Path
from typing import Any

# IDEAS.md 条目 status 合法值白名单（与 IDEAS.md 模板状态流转一致）
_VALID_IDEA_STATUSES = frozenset({"pending", "taskified", "questioning", "dropped"})

# 摄入产物文件白名单（与 split._INTAKE_PRODUCT_FILES 对齐，两种布局）：
# 摄入 → amend / intake 的正当链路中允许未提交态；其余已跟踪改动视为非摄入脏。
_INTAKE_PRODUCT_FILES = frozenset({
    ".orchd/_master.json",
    "IDEAS.md",
    ".orchd/IDEAS.md",
    "ROADMAP.md",
    ".orchd/ROADMAP.md",
})


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
    from orchd.gitops import (
        ensure_committed,
        get_current_branch,
        get_default_branch,
        list_tracked_changes,
    )
    from orchd.ledger import resolve_workspace_root

    project_root = Path(project_root)

    # 1) 前置守卫：main 分支 + 非摄入产物干净（对齐 amend 的 E017 语义）
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        return {
            "committed": False,
            "reason": "not_on_main",
            "branch": current_branch,
            "hint": f"intake 仅在 default（{default_branch}）分支执行，请先切回",
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

    # 2) IDEAS 条目状态校验（warning 不阻断）
    workspace_root = resolve_workspace_root(project_root)
    status_warnings = check_idea_statuses(workspace_root)

    # 3) 强制提交摄入产物（IDEAS.md + ROADMAP.md）
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
