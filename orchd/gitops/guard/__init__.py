"""gitops guard — 守卫面聚合入口（W6-C1：退化为纯重导出）。

来源：``orchd/gitops/guard.py``（1652 行，W6-C1 纯搬移）。

本入口**不再持有任何实现**：仅重导出 ``core`` / ``write`` / ``checkout`` /
``lifecycle`` / ``restore`` 五域的 40 个顶层符号（40 名）。
外部 ``from orchd.gitops.guard import <名>`` 取到的仍是子模块同一对象
（补丁面/打点键随落点迁移，见 tests/test_gitops_guard.py 的行为级机判）。
"""

# ------------------------------------------------------------------
# 门禁执行器（task-audit-git-guard-fail-closed：语义三分类 + 降级可审计）
# ------------------------------------------------------------------
# 背景：全项目多处安全门禁以 ``except OrchdError: raise`` + ``except Exception: pass``
# 实现，把「环境不适用」与「校验故障」两种语义混为一谈——门禁自身出故障时引擎
# 认为「没发现问题」并静默放行（fail-open），且降级完全不可审计。
#
# 本执行器把错误分为三类，处置各不相同：
#   1. ``OrchdError``          → 业务拒绝（门禁判定不通过）→ 原样抛出；
#   2. ``NotApplicableError``  → 环境不适用（非 git 仓库 / 无独立主工作树 /
#                                任务分支不存在）→ 合法降级，但必须留痕；
#   3. 其他 ``Exception``      → 校验故障（git 超时 / IO / 导入失败）→ 按
#                                ``on_error`` 阻断（fail_closed）或升级 E030 告警
#                                （warn），绝不静默放行。
#
# gitops 与 onboard 共用本实现，杜绝每处手写 try/except。依赖方向保持叶子化：
# 本段只依赖 orchd.errors，不导入 onboard / review / worktree。
#
# NotApplicableError 定义于 orchd.errors（全引擎通用的门禁语义类型，非 git 领域
# 类型），此处 re-export 仅为兼容既有 ``from orchd.gitops import NotApplicableError``
# 导入路径，不新增依赖边（``gitops → errors`` 本就存在）。

from .checkout import (
    managed_checkout_branch,
    _reviewer_auto_checkout,
    checkout_default_strict,
    _checkout_default_with_retry,
)
from .core import (
    GUARD_FAIL_CLOSED,
    GUARD_WARN,
    GUARD_STATUS_NOT_APPLICABLE,
    GUARD_STATUS_FAILED,
    record_degraded_guard,
    _guard_failed,
    run_guard,
    _probe_guard_workspace_state,
    _container_task_wt_exists,
    commit_hint_for_branch,
    _sync_lag_exempted,
    _is_nogit_orchd_dir,
    _nogit_maindir_violation,
    _record_git_unavailable_guard,
    _SESSION_LOCK_DEGRADE_REASONS,
    _session_lock_degrade_reason,
    _clear_timeout_index_lock,
)
from .lifecycle import (
    release_task_lifecycle,
    compat_checked_out_main,
    _audit_lifecycle_release,
    _implementer_ensure_trunk,
    _audit_trunk_restore,
)
from .restore import (
    _count_git_processes,
    restore_worktree_paths,
    _restore_refusal_reason,
)
from .write import (
    _build_wrong_branch_hint,
    _enforce_branch_allowed,
    _enforce_workspace_clean,
    guard_write_command,
    _resolve_claim_check_root,
    _same_git_repo,
    guard_claim,
    _layout_is_not_container,
    guard_done_branch,
    guard_clean_workspace,
    guard_review_write,
)

# 校验/兼容面：``NotApplicableError`` 原由本模块 re-export 供
# ``from orchd.gitops import NotApplicableError`` 使用，拆分后继续从本入口导出。
from .core import NotApplicableError  # noqa: F401  (re-export)

__all__ = [
    "GUARD_FAIL_CLOSED",
    "GUARD_WARN",
    "GUARD_STATUS_NOT_APPLICABLE",
    "GUARD_STATUS_FAILED",
    "record_degraded_guard",
    "_guard_failed",
    "run_guard",
    "_probe_guard_workspace_state",
    "_container_task_wt_exists",
    "managed_checkout_branch",
    "release_task_lifecycle",
    "compat_checked_out_main",
    "_audit_lifecycle_release",
    "_implementer_ensure_trunk",
    "_audit_trunk_restore",
    "_reviewer_auto_checkout",
    "_build_wrong_branch_hint",
    "_enforce_branch_allowed",
    "commit_hint_for_branch",
    "_sync_lag_exempted",
    "_enforce_workspace_clean",
    "_is_nogit_orchd_dir",
    "_nogit_maindir_violation",
    "_record_git_unavailable_guard",
    "_SESSION_LOCK_DEGRADE_REASONS",
    "_session_lock_degrade_reason",
    "guard_write_command",
    "_resolve_claim_check_root",
    "_same_git_repo",
    "guard_claim",
    "_layout_is_not_container",
    "guard_done_branch",
    "guard_clean_workspace",
    "guard_review_write",
    "checkout_default_strict",
    "_count_git_processes",
    "_clear_timeout_index_lock",
    "restore_worktree_paths",
    "_restore_refusal_reason",
    "_checkout_default_with_retry",
    "NotApplicableError",
]
