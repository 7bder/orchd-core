"""claim —— 聚合入口（task-split2-w8-1-claim-pkg 自 ``orchd/onboard/claim.py`` 拆出，纯重导出）。

原模块 docstring 如下（信息不丢）：

  Orchd 任务生命周期管理 - claim 域。

  迁移自 orchd/onboard.py（task-split-onboard-claim）：
    - _is_high_risk: 高风险领域判定
    - _extract_previous_changes: 从 ledger 提取最近一次 DONE 的 changes
    - _claim_precheck: claim 预校验（无锁快速失败）
    - _claim_setup_worktree: 创建/绑定任务 worktree
    - _claim_write_event: 锁内写 CLAIMED/REVIEW_CLAIMED 事件
    - _claim_review_branch: reviewer claim 时的分支诊断
    - claim: 认领任务主入口（锁内 check-then-act）

实现按域落在 5 个子模块：``scope.py`` / ``precheck.py`` / ``execute.py`` /
``review_face.py`` / ``run.py``。
本入口只重导出，不含任何实现。``from orchd.onboard.claim import X`` 与
``orchd.onboard.claim.X`` 的既有消费面（含 onboard/__init__ 的可调用模块代理）零改动。
"""

from __future__ import annotations

from .precheck import (
    _RETRACT_COOLDOWN_S,
    _branch_degraded_guard,
    _check_master_uncommitted,
    _check_retract_cooldown,
    _claim_precheck,
)
from .review_face import (
    _attach_verify_head_drift,
    _build_self_review_notice,
    _claim_review_branch,
    _reconcile_mergeability,
    _resolve_review_worktree,
)
from .run import claim
from .execute import (
    _claim_setup_worktree,
    _claim_write_event,
)
from .scope import (
    _SCOPE_FILE_RE,
    _extract_previous_changes,
    _is_high_risk,
    build_scope_warning,
)

__all__ = [
    "_RETRACT_COOLDOWN_S",
    "_SCOPE_FILE_RE",
    "_attach_verify_head_drift",
    "_branch_degraded_guard",
    "_build_self_review_notice",
    "_check_master_uncommitted",
    "_check_retract_cooldown",
    "_claim_precheck",
    "_claim_review_branch",
    "_claim_setup_worktree",
    "_claim_write_event",
    "_extract_previous_changes",
    "_is_high_risk",
    "_reconcile_mergeability",
    "_resolve_review_worktree",
    "build_scope_warning",
    "claim",
]
