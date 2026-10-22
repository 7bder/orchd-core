"""lifecycle —— 聚合入口（W6-C7 自 ``orchd/worktree/lifecycle.py`` 拆出，纯重导出）。

原模块 docstring 如下（信息不丢）：

  任务 worktree 生命周期（task-14-worktree-lifecycle）：vendored 引擎 +
  建（ensure）+ 头守卫 + 旧版清理。依赖方向：同包 layout/bindings 原语。

实现按域落在 5 个子模块：``engine.py`` / ``suppress.py`` / ``create.py`` /
``mainhead.py`` / ``probe.py``。
本入口只重导出，不含任何实现。
"""

from __future__ import annotations

from .engine import (
    _engine_manifest,
    _propagate_vendored_engine,
    _propagate_container_marker,
    _git_minor_version,
    _merge_vendored_engine_entry,
)
from .suppress import _MASTER_IGNORE_PATTERNS, _suppress_task_master_copy, _master_suppression_entry
from .create import (
    _task_branch_divergence,
    _recreate_task_wt_from_base,
    _check_stale_baseline,
    ensure_task_wt,
)
from .mainhead import _read_main_worktree_head, _check_main_head_unchanged, _restore_main_head
from .probe import (
    _worktree_registered_paths,
    _is_effective_worktree,
    _cleanup_stale_task_wt,
    _verify_task_wt,
)

__all__ = [
    "_engine_manifest",
    "_propagate_vendored_engine",
    "_propagate_container_marker",
    "_git_minor_version",
    "_merge_vendored_engine_entry",
    "_MASTER_IGNORE_PATTERNS",
    "_suppress_task_master_copy",
    "_master_suppression_entry",
    "_task_branch_divergence",
    "_recreate_task_wt_from_base",
    "_check_stale_baseline",
    "ensure_task_wt",
    "_read_main_worktree_head",
    "_check_main_head_unchanged",
    "_restore_main_head",
    "_worktree_registered_paths",
    "_is_effective_worktree",
    "_cleanup_stale_task_wt",
    "_verify_task_wt",
]
