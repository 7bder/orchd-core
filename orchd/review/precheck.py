"""review 提交前置段（_review_submit_precheck），依赖 history+branch+identity。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.errors import ErrorCode
from orchd.errors import OrchdError
from orchd.ledger import Store
from orchd.gitops import get_head_commit
from .branch import _task_branch_tip
from .history import _recent_transitions, extract_review_baseline
from .identity import is_self_review_author


def _review_submit_precheck(
    *,
    store: Store,
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None,
    project_root: Path | None,
    rework_scope: str | None,
) -> dict[str, Any]:
    """review 提交前置门（原 _review_submit_impl 主 try 体 L670-774，17 条语句）。

    lock 内依次：in_review / 审查阶段 / 审查认领三重门（E007 reason 三码）→
    基线与当前分支 tip 漂移检测 → CHANGES_REQUESTED 空意见门 → 自审事实判定 →
    rework_scope 仅 code 打回可携带的形态门。返回 5 名供事件段使用；
    段内全部 E007 站点带机读 reason（登记表见 tests/test_e007_reasons.py）。
    """
    integrity_warnings = store.check_integrity()
    state = store.replay()
    derived = store.scan_task_derived()
    ts = state.get(task_id)

    # P0-9：预计算最近状态变迁（仅 E007 路径使用，happy path 不触发开销）
    def _err_transitions() -> list[dict[str, Any]]:
        return _recent_transitions(store, task_id, derived=derived)

    if not ts or ts.status != "in_review":
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not in_review",
            [{"reason": "review_not_in_review",
              "task_id": task_id, "actual": ts.status if ts else "pending",
              "recent_transitions": _err_transitions(),
              "hint": f"任务当前状态为 {ts.status if ts else 'pending'}（非 in_review），"
                      f"可能尚未 done 或已被审查打回 pending"}],
        )
    if ts.review_phase != review_type:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: review phase mismatch "
            f"(expected '{ts.review_phase}', got '{review_type}')",
            [{"reason": "phase_mismatch",
              "task_id": task_id, "expected_phase": ts.review_phase,
              "got_phase": review_type,
              "recent_transitions": _err_transitions(),
              "hint": f"审查阶段不匹配：任务处于 {ts.review_phase} 阶段，"
                      f"你提交的是 {review_type}，请确认审查类型"}],
        )
    from orchd.ledger import resolve_session_identity
    current_session = resolve_session_identity(store.orchd_dir)["session_id"]
    if ts.review_claimed_session and current_session:
        claimed_other = not (
            ts.review_claimed_session == current_session
            and ts.review_claimed_by == agent_id
        )
    else:
        claimed_other = ts.review_claimed_by != agent_id
    if claimed_other:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: review not claimed by this session "
            f"(claimed_by='{ts.review_claimed_by}', claimed_session='{ts.review_claimed_session}')",
            [{"reason": "review_not_claimed",
              "task_id": task_id, "claimed_by": ts.review_claimed_by,
              "claimed_session": ts.review_claimed_session, "agent": agent_id,
              "current_session": current_session,
              "recent_transitions": _err_transitions(),
              "hint": "审查已被其他 session 认领或当前 session 不匹配；"
                      "可用 orchd retract --task <id> --type REVIEW_CLAIMED 释放后重新认领"}],
        )

    baseline_sha = extract_review_baseline(store, task_id, agent_id, derived)
    # AC1（task-review-baseline-and-worktree-recycle-fix）：current_sha 与
    # REVIEW_CLAIMED 的 baseline_sha 必须同源。原取 get_head_commit(project_root)
    # （主工作树 HEAD），与基线（任务分支 tip）比对必然不等 → 恒误报漂移。
    current_sha = (
        _task_branch_tip(project_root, task_id) or get_head_commit(project_root)
    ) if project_root else None
    baseline_drift = bool(baseline_sha and current_sha and baseline_sha != current_sha)

    # task-review-comments-gate-and-stale-timeout（A）：CHANGES_REQUESTED
    # 必须附意见，否则返工任务取到 review_comments=[]，实现者看不到打回原因。
    # APPROVED / REVIEW_READY 不受影响（通过无需意见）。空白字符串视同空。
    if verdict == "CHANGES_REQUESTED" and not (comments or "").strip():
        raise OrchdError(
            ErrorCode.E007,
            "invalid_state: CHANGES_REQUESTED requires non-empty review comments",
            [{"reason": "changes_requested_empty",
              "task_id": task_id, "verdict": verdict,
              "hint": "打回审查必须附上具体修改意见（comments 不能为空或仅空白），"
                      "否则实现者返工后无法获知需要修复什么"}],
        )

    # v4（2026-09-15 停服升级）：自审事实落账——提交审查时把「实现者 == 审查者」
    # 写入事件（与 REVIEW_CLAIMED 同口径、同一判定函数），TaskState.review_self_review
    # 随之派生；事后审计直接读账本，不必再用 DONE.agent_id == REVIEW_CLAIMED.agent_id
    # 的启发式推导。
    is_self_review = is_self_review_author(
        derived.last_done.get(task_id), agent_id, current_session
    )
    # task-review-rework-scope：打回范围分类。rework_scope 仅伴随
    # CHANGES_REQUESTED，且仅 code 阶段打回可声明 code（spec 打回重走
    # spec 是唯一语义）；APPROVED / 非 code 阶段携带即 E007，防误用。
    if rework_scope is not None:
        if verdict != "CHANGES_REQUESTED" or review_type != "code":
            raise OrchdError(
                ErrorCode.E007,
                "invalid_rework_scope: rework_scope 仅用于 code 阶段的 CHANGES_REQUESTED",
                [{"reason": "invalid_rework_scope",
                  "task_id": task_id, "verdict": verdict,
                  "review_type": review_type,
                  "hint": "仅 code 审查打回实现问题时可附 --rework-scope code"
                          "（返工后直达 code）；其余情形不得携带"}],
            )
        if rework_scope not in ("spec", "code"):
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_rework_scope: {rework_scope!r} 非法",
                [{"reason": "invalid_rework_scope",
                  "task_id": task_id,
                  "hint": "rework_scope 仅支持 spec（默认，全退重走）/ code"
                          "（仅实现问题，返工直达 code）"}],
            )

    return {
        "integrity_warnings": integrity_warnings,
        "baseline_sha": baseline_sha,
        "current_sha": current_sha,
        "baseline_drift": baseline_drift,
        "is_self_review": is_self_review,
    }
