"""git 分支状态辅助（_task_branch_tip/_wt_dir_name），叶域。"""

from __future__ import annotations

from pathlib import Path

from orchd.line_ctx import resolve_task_branch_for


def _task_branch_tip(project_root: Path, task_id: str) -> str | None:
    """best-effort 取 ``task/{task_id}`` 分支 tip SHA（task-merge-warning-resolve-sha）。

    供 merge_warning 事件附加 ``resolve_sha``：audit-merge 以此判定 main 是否
    已含实现（手工补 merge 落地后自动销账）。git 不可用 / 分支不存在 / 异常
    → None（省略字段，行为向后兼容）。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", resolve_task_branch_for(project_root, task_id)],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def _wt_dir_name(task_id: str) -> str:
    """任务 worktree 目录名（单一来源：``orchd.worktree.worktree_hint``）。

    AC4（task-review-diagnostics-hardening）：与 ``orchd/gitops/guard.py`` 的
    E018 hint 同源，均经 ``worktree_hint(task_id)``（内部使用 ``_task_wt_name``）
    输出真实目录名，杜绝各处自行拼 ``task-{task_id}`` 产生 ``task-task-<id>``
    双前缀。引擎不可用时回退等价实现（best-effort）。
    """
    try:
        from orchd.worktree import worktree_hint

        return worktree_hint(task_id)
    except Exception:
        short = task_id[5:] if task_id.startswith("task-") else task_id
        return f"task-{short}"
