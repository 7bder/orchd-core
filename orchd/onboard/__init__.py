"""Orchd 任务生命周期管理模块。

覆盖任务从创建到终结的完整流程：
  - bootstrap: 输出分解套件（schema + prompt + guide），供 LLM 生成任务清单。
  - request: 只读查询候选任务（implementer / reviewer），无锁。
  - claim: 认领任务（锁内 check-then-act），并 best-effort 创建 git 分支。
  - done: 报告完成（锁外执行 verify_command → 锁内二次校验 + 写事件）。
  - review_submit: 提交审查结论（APPROVED / CHANGES_REQUESTED），锁内写事件。
  - retract: 撤回事件及其级联影响，锁内操作。
  - force_status: 强制设置任务状态（受"允许从"矩阵约束），锁内操作。

Git 辅助操作（_try_git_branch / _try_git_merge）均为 best-effort：
成功时反映到返回字段，失败时静默降级、不影响任务状态机。

依赖方向：onboard.py → ledger.py / pool.py / spec.py（不导入 cli / report）。
"""

from __future__ import annotations

# 子域外置（task-refactor-onboard-domain-split）：
# - 共享辅助（guard/event/decoder/session-lock）→ orchd.gitops_ops
# - review 子域 → orchd.review
# - git 写子域 → orchd.gitops_ops
# 此处 re-export 保持「from orchd.onboard import X」与 monkeypatch 旧路径兼容。
from orchd.gitops import get_head_commit  # noqa: E402  re-export for monkeypatch
from orchd.gitops_ops import (  # noqa: E402  re-exports (backward compat)
    decode_subprocess_output as _decode_subprocess_output,
    make_event as _make_event,
    now_iso as _now_iso,
    sync_master_with_main as _sync_master_with_main,
    try_auto_resolve_conflict as _try_auto_resolve_conflict,
    try_delete_task_branch as _try_delete_task_branch,
    try_git_branch as _try_git_branch,
    try_git_merge as _try_git_merge,
    verify_output_summary as _verify_output_summary,
)
from orchd.review import (  # noqa: E402  re-exports (backward compat)
    extract_last_done as _extract_last_done,
    extract_review_baseline as _extract_review_baseline,
    extract_review_comments as _extract_review_comments,
    find_last_done_event as _find_last_done_event,
    request_reviewer as _request_reviewer,
    review_submit as review_submit,
)

# 子模块 re-export
from orchd.onboard.bootstrap import (
    _FALLBACK_GUIDE,
    _resolve_resource_root,
    _find_project_root,
    bootstrap,
)
from orchd.onboard.request import (
    _find_review_priority_tasks,
    _build_candidates,
    _filter_conflicts,
    _route_by_role,
    request,
)
# claim 域：导入子模块并包装为可调用模块，使「orchd.onboard.claim」既是模块
# （供 monkeypatch.setattr("orchd.onboard.claim.get_head_commit", ...) 打点），
# 又可像函数一样直接调用（供 cli.py 的 from orchd.onboard import claim; claim(...)）。
import orchd.onboard.claim as _claim_mod


class _ClaimModule(_claim_mod.__class__):
    def __call__(self, *args, **kwargs):
        return self.claim(*args, **kwargs)


_claim_mod.__class__ = _ClaimModule
claim = _claim_mod
_is_high_risk = _claim_mod._is_high_risk
_extract_previous_changes = _claim_mod._extract_previous_changes
_claim_precheck = _claim_mod._claim_precheck
_claim_setup_worktree = _claim_mod._claim_setup_worktree
_claim_write_event = _claim_mod._claim_write_event
_claim_review_branch = _claim_mod._claim_review_branch

# lifecycle 子包：done 主流程 + 门禁 + 回归门禁
from orchd.onboard.lifecycle import (
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
    _full_regression_enabled,
    _maybe_full_regression,
    _has_engine_files,
    _guard_cross_worktree_dirty,
    _guard_declared_diff,
    _guard_out_of_scope,
    _guard_zero_residual,
)

# _config 域：配置加载 + 文档单阶段判定
from orchd.onboard._config import (
    _DOC_SINGLE_STAGE_SUFFIXES,
    _is_doc_single_stage,
    _load_config_blocked,
)

# control 域：retract / force_status + 常量
from orchd.onboard.control import (
    _FORCE_ESCAPE_HATCHES,
    _FORCE_TARGETS,
    _FULL_REGRESSION_TIMEOUT,
    _VERIFY_TIMEOUT,
    _task_completed_epoch,
    _validate_revive_evidence,
    force_status,
    retract,
)

# 兼容层：供 lifecycle/core 动态导入（from orchd.onboard import X）与 monkeypatch 透传
import subprocess
from orchd.gitops import (
    branch_exists,
    checkout_default_strict as _checkout_default_strict,
    get_default_branch as _get_default_branch,
    guard_claim as _guard_claim,
    is_task_worktree,
    list_tracked_changes,
)
from orchd.subproc import run_shell
