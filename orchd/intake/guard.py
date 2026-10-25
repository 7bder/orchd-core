"""intake.guard —— 自 ``orchd/intake.py`` 拆出。

摄入前置守卫与锁目录解析（_intake_guard / _resolve_lock_orchd_dir）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 摄入产物文件白名单（task-inventory-honesty/F9 起以本集合为单一真源；
# split._INTAKE_PRODUCT_FILES 复用此处，禁双写漂移。两种布局）：
# 摄入 → amend / intake 的正当链路中允许未提交态；其余已跟踪改动视为非摄入脏。
# IDEAS-archive.md 在列：released-pending 条目的归档改动可随 intake 提交落盘。
_INTAKE_PRODUCT_FILES = frozenset({
    ".orchd/_master.json",
    "IDEAS.md",
    ".orchd/IDEAS.md",
    "IDEAS-archive.md",
    ".orchd/IDEAS-archive.md",
    "ROADMAP.md",
    ".orchd/ROADMAP.md",
})


# init 后必脏提示后缀（task-intake-hint-phase）：init 必然产生未提交文件
# （mod-*/spec.json、_ledger.jsonl 等），无该句时新项目按 init → intake
# 自然顺序必撞 dirty_workspace 死循环。两处 dirty hint 共用，禁手写漂移。
_DIRTY_AFTER_INIT_HINT = (
    "；若刚执行 init（mod-*/spec.json、_ledger.jsonl 等必然未提交），"
    "请先提交落盘后再继续"
)


def _intake_guard(project_root: Path) -> dict[str, Any] | None:
    """摄入前置守卫：main 分支 + 非摄入产物干净（对齐 amend 的 E017 语义）。

    供 intake_commit / idea_propose / idea_confirm 复用（idea-write-gate）。
    出口动词统一后不再有 idea_drop：close 的后端住在 ideas.decision（它自行取同一把
    ``.intake.lock`` 并复用本守卫）。

    Returns:
        - 通过: ``None``。
        - 失败: 结构化错误 dict（not_on_main / dirty_workspace），调用方直接返回。
    """
    from orchd.gitops import get_current_branch, list_tracked_changes
    from orchd.line_ctx import resolve_trunk_for

    project_root = Path(project_root)
    current_branch = get_current_branch(project_root)
    # task-line-guard-intake-wiring：主干按当前线解析（单线恒 main）
    default_branch = resolve_trunk_for(project_root)
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
                    "请先提交或还原摄入产物（IDEAS.md / IDEAS-archive.md / ROADMAP.md / "
                    "_master.json）之外的文件改动（untracked 工具/配置文件不阻塞）"
                    + _DIRTY_AFTER_INIT_HINT
                ),
            }
    return None


def _resolve_lock_orchd_dir(project_root: Path) -> Path:
    """准入锁用 .orchd 目录（防双嵌套，task-intake-lock-path-fix）。

    ``resolve_workspace_root`` 在自包含布局（IDEAS.md 位于 .orchd/ 内）下已返回
    ``<project>/.orchd``，再拼 ``/ ".orchd"`` 会双重嵌套为 ``.orchd/.orchd``，
    使准入锁落错路径并分裂（amend 经 split.py 传真实 orchd_dir 则落在共享账本根）。
    此处统一经 ``resolve_canonical_project_root`` 定位 canonical 主工作树根，
    再拼一次 ``.orchd``——container 与 flat、自包含与根布局均收敛到同一真实目录。
    """
    try:
        from orchd.worktree import resolve_canonical_project_root

        canonical = resolve_canonical_project_root(Path(project_root))
    except Exception:
        canonical = Path(project_root)
    return Path(canonical) / ".orchd"
