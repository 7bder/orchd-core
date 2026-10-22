"""review 提交记录段（_review_submit_record）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.errors import ErrorCode
from orchd.errors import OrchdError
from orchd.ledger import Store
from orchd.gitops_ops import make_event


def _review_submit_record(
    *,
    store: Store,
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None,
    project_root: Path | None,
    rework_scope: str | None,
    integrity_warnings: list[dict[str, Any]],
    baseline_sha: str | None,
    current_sha: str | None,
    baseline_drift: bool,
    is_self_review: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """review 事件与分支派发段（原主 try 体 L775-869，10 条语句）。

    构造 REVIEW_SUBMITTED 事件（含 rework_scope / review_type / comments 条件字段）、
    组装响应 result（自审 / 完整性 / 基线漂移三块可选字段），再按 verdict×review_type
    四支派发：spec APPROVED 推进 code 就绪；code/unified APPROVED 透出 pending 事件；
    CHANGES_REQUESTED 终态收敛后回 pending；其余显式 E007 invalid_verdict。
    """
    event = make_event(
        task_id, agent_id, "REVIEW_SUBMITTED",
        verdict=verdict,
        is_self_review=is_self_review,
    )
    if rework_scope is not None:
        event["rework_scope"] = rework_scope
    # review-unify-r2：unified 单阶段（review_type 为 None）不写 review_type
    # 字段（R2-b：新事件无 review_type）；two_phase 保留 spec/code 供 replay
    # 按两阶段语义解释，与老事件兼容。
    if review_type is not None:
        event["review_type"] = review_type
    if comments:
        event["comments"] = comments

    result: dict[str, Any] = {
        "submitted": True,
        "task_id": task_id,
        "review_type": review_type,
        "verdict": verdict,
    }
    if is_self_review:
        # 与 claim 的 self_review_notice 呼应：非阻断提示，便于调用方即时知情
        result["is_self_review"] = True
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings

    if baseline_drift:
        assert baseline_sha is not None and current_sha is not None
        # AC1（task-review-diagnostics-hardening）：baseline_warning 结构化，
        # 以稳定契约对象承载漂移信息（code / baseline_sha / current_sha /
        # severity / message），字段级可断言，避免 container 布局下告警长期
        # 被当作裸字符串噪音忽略。无漂移时不产出该字段。
        result["baseline_warning"] = {
            "code": "baseline_drift",
            "baseline_sha": baseline_sha,
            "current_sha": current_sha,
            "severity": "warning",
            "message": (
                f"task branch HEAD changed during review "
                f"(claimed at {baseline_sha[:7]}, now {current_sha[:7]}). "
                f"Review may be based on outdated code."
            ),
        }

    pending_code_event: dict[str, Any] | None = None

    if verdict == "APPROVED" and review_type == "spec":
        store.append_event(event)
        code_ready = make_event(task_id, agent_id, "REVIEW_READY", review_type="code")
        store.append_event(code_ready)
        result["task_status"] = "in_review"
        result["next_review"] = "code"
        new_state = store.replay()
        store.update_checkpoint(new_state)

    elif verdict == "APPROVED" and (review_type == "code" or review_type is None):
        # review-unify-r2：unified 单阶段（review_type 为 None）与 code 终审
        # 一样走 merge → completed；two_phase 的 spec APPROVED 走上一分支。
        result["task_status"] = "in_review"
        pending_code_event = event

    elif verdict == "CHANGES_REQUESTED":
        # 打回前经统一终态收敛恢复默认分支（task-pass9-done-review-finalizer-unify）：
        # strict 模式切换失败抛 E018/E017 → 不写事件、任务仍 in_review、审查 claim
        # 保留，reviewer 处理后重试即可（与 done 强约束一致）。
        if project_root:
            from orchd.gitops.guard import (
                compat_checked_out_main,
                release_task_lifecycle,
            )

            _rel = release_task_lifecycle(
                store, project_root, task_id=task_id, agent_id=agent_id,
                command="review", mode="strict",
            )
            _compat = compat_checked_out_main(_rel)
            if _compat is not None:
                result["checked_out_main"] = _compat
        store.append_event(event)
        result["task_status"] = "pending"
        result["back_to_pool"] = True
        new_state = store.replay()
        store.update_checkpoint(new_state)

    else:
        # P2-5：未知 verdict / 不支持 review_type 组合不得静默无操作，
        # 显式报错，避免调用方拿到无 task_status 的「成功」结果。
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_verdict: unsupported verdict={verdict!r} review_type={review_type!r}",
            [{"reason": "invalid_verdict", "task_id": task_id, "verdict": verdict,
              "review_type": review_type,
              "hint": "verdict 仅支持 APPROVED / CHANGES_REQUESTED"}],
        )

    return result, pending_code_event
