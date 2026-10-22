"""orchd/worktree/recycle/ — 聚合入口（W6-C5：退化为纯重导出）。

来源：``orchd/worktree/recycle.py``（1023 行，W6-C5 纯搬移）。

仅重导出 23 个顶层符号（外部导入取到的仍是子模块同一对象）。
"""

from .journal import (
    _recycle_actor,
    _QUIET_TRUTHY,
    _recycle_quiet,
    _log_recycle,
    _TX_STEPS,
    _RECYCLE_JOURNAL_DIRNAME,
    _recycle_journal_path,
    _tx_journal_read,
    _tx_journal_write,
    _tx_journal_clear,
    _tx_mark_step_done,
    _tx_fact_wt_gone,
    _tx_fact_branch_gone,
    _tx_fact_unbound,
    _tx_prune_registry,
    _task_status_for_recycle,
)
from .cleanup import (
    _cleanup_stale_session_locks,
    _rmtree_force,
)
from .steps import (
    _classify_remove_failure,
    _tx_step_remove_worktree,
    _tx_step_delete_branch,
    _tx_step_unbind,
)
from .remove import (
    remove_task_wt,
)

__all__ = [
    "_recycle_actor",
    "_QUIET_TRUTHY",
    "_recycle_quiet",
    "_log_recycle",
    "_TX_STEPS",
    "_RECYCLE_JOURNAL_DIRNAME",
    "_recycle_journal_path",
    "_tx_journal_read",
    "_tx_journal_write",
    "_tx_journal_clear",
    "_tx_mark_step_done",
    "_tx_fact_wt_gone",
    "_tx_fact_branch_gone",
    "_tx_fact_unbound",
    "_tx_prune_registry",
    "_task_status_for_recycle",
    "_classify_remove_failure",
    "_tx_step_remove_worktree",
    "_tx_step_delete_branch",
    "_tx_step_unbind",
    "remove_task_wt",
    "_cleanup_stale_session_locks",
    "_rmtree_force",
]
