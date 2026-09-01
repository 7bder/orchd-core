"""Orchd 任务生命周期管理 - lifecycle 子包。

包含 done 主流程的完整性门禁与核心实现：
  - guards: 4 个 fail-closed 门禁（cross_worktree_dirty / declared_diff /
    zero_residual / out_of_scope）
  - core: done 主流程实现（_done_impl / _commit_and_verify_integrity 等）
  - regression: 全量回归门禁
"""

from __future__ import annotations

from orchd.onboard.lifecycle.core import (
    done,
    _done_impl,
    _done_precheck,
    _run_verify,
    _verify_fail_error,
    _verify_timeout_error,
    _verify_failure_early_done,
    _commit_and_verify_integrity,
    _done_auto_commit,
    _write_done_event,
    _done_lessons_hook,
    _assemble_done_result,
)
from orchd.onboard.lifecycle.guards import (
    _guard_cross_worktree_dirty,
    _guard_declared_diff,
    _guard_out_of_scope,
    _guard_zero_residual,
)
from orchd.onboard.lifecycle.regression import (
    _full_regression_enabled,
    _maybe_full_regression,
    _has_engine_files,
)

__all__ = [
    "done",
    "_done_impl",
    "_done_precheck",
    "_run_verify",
    "_verify_fail_error",
    "_verify_timeout_error",
    "_verify_failure_early_done",
    "_commit_and_verify_integrity",
    "_done_auto_commit",
    "_write_done_event",
    "_done_lessons_hook",
    "_assemble_done_result",
    "_guard_cross_worktree_dirty",
    "_guard_declared_diff",
    "_guard_out_of_scope",
    "_guard_zero_residual",
    "_full_regression_enabled",
    "_maybe_full_regression",
    "_has_engine_files",
]