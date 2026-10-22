"""request 只读查询入口（编排器）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.guide import (
    NEXT_ACTION_EXIT, NEXT_ACTION_REVIEW_FIRST, NEXT_ACTION_SUBMIT_REVIEW, NEXT_ACTION_WAIT,
)
from orchd.ledger import Store
from orchd.review import request_reviewer as _request_reviewer
from .conflicts import _filter_conflicts
from .phase import _apply_phase_gate
from .routing import (
    _build_candidates, _filter_cooldown_tasks, _find_review_priority_tasks, _held_review_claim,
    _route_by_role,
)


def request(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    capabilities: list[str] | None = None,
    exclude: list[str] | None = None,
    role: str = "implementer",
    sort_key: str | None = None,
    max_active: int | None = None,
    importance_thresholds: dict[str, Any] | None = None,
    enforce_self_review_block: bool = False,
    project_root: Path | None = None,
    conflict_policy: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    state = store.replay()
    derived = store.scan_task_derived()
    if project_root is None and store.orchd_dir is not None:
        try:
            project_root = Path(store.orchd_dir).parent
        except Exception:
            project_root = None
    if role == "reviewer":
        return _request_reviewer(store, state, tasks, agent_id, derived, enforce_self_review_block=enforce_self_review_block)
    # task-guidance-review-priority-fix：本会话已持有未提交的审查认领时先给「提交」出口，
    # 不再产 review_first 指向他任务的审查——否则 agent 去 claim 会被 E011 review busy
    # 拒绝（orchd/onboard/claim.py 的 review busy 分支），手上认领悬空未提交。
    # 与 _classify 的 submit_review 优先级保持一致（引导给出的命令不得被门禁拒绝）。
    held_review = _held_review_claim(state, agent_id)
    if held_review is not None:
        held_tid, held_phase = held_review
        return {
            "candidate": None,
            "message": (
                f"本会话已持有未提交的审查认领：{held_tid}（{held_phase} 阶段），"
                "请先提交审查结论再领取新任务"
            ),
            "next_action": NEXT_ACTION_SUBMIT_REVIEW,
            "review_held": {"task_id": held_tid, "review_phase": held_phase},
            "pool_size": 0,
        }
    review_priority, excluded_self_review = _find_review_priority_tasks(store, state, tasks, agent_id, derived, enforce_self_review_block=enforce_self_review_block)
    if review_priority:
        best_review = review_priority[0]
        rp_entry = {"task_id": best_review["task_id"], "review_phase": best_review["review_phase"], "name": best_review["name"], "total_available": len(review_priority)}
        if best_review.get("is_self_review"):
            rp_entry["is_self_review"] = True
        # AC1（task-request-response-fidelity）：审查优先分支不得硬编码 pool_size: 0——
        # 复用主路径同一口径（_build_candidates + 冷却剔除 + _filter_conflicts）算出真实
        # 可领取实现候选数，并给 blocked_by 归因，避免 candidate=null + pool_size=0 被读成
        # 「没活」。
        _impl_candidates = _build_candidates(state, tasks, capabilities, exclude, sort_key, importance_thresholds)
        _impl_candidates, _impl_cooldown_excluded = _filter_cooldown_tasks(_impl_candidates, store)
        _impl_kept, _, _, _, _ = _filter_conflicts(_impl_candidates, state, tasks, project_root, conflict_policy)
        resp: dict[str, Any] = {"candidate": None, "review_priority": rp_entry, "message": f"有 {len(review_priority)} 个待审查任务可领取", "next_action": NEXT_ACTION_REVIEW_FIRST, "pool_size": len(_impl_kept), "blocked_by": "review_priority"}
        if excluded_self_review:
            resp["excluded_self_review"] = excluded_self_review
        # 剔除须留痕：本分支同样算过冷却剔除（此前丢弃），非空时一并透出
        if _impl_cooldown_excluded:
            resp["cooldown_excluded"] = _impl_cooldown_excluded
        return resp
    if max_active is not None:
        active = sum(1 for ts in state.values() if ts.status == "claimed")
        if active >= max_active:
            return {"candidate": None, "message": f"max_active {active}/{max_active}", "next_action": NEXT_ACTION_WAIT, "reason": "max_active_reached", "pool_size": 0, "active_count": active, "max_active": max_active}
    candidates = _build_candidates(state, tasks, capabilities, exclude, sort_key, importance_thresholds)
    # task-retract-bind-cooloff：剔除冷却期任务（已放弃任务不再被推荐）
    candidates, cooldown_excluded = _filter_cooldown_tasks(candidates, store)
    kept, excluded_conflicts, candidate_conflicts, degraded_guards, guard_unavailable_count = _filter_conflicts(candidates, state, tasks, project_root, conflict_policy)
    # task-phase-request-gate：阶段闸门（仅 implementer 主路径生效）
    phase_values = {t.get("phase") for t in tasks if t.get("phase")}
    if phase is not None and phase not in phase_values:
        return {
            "candidate": None,
            "message": f"指定的阶段 {phase!r} 不在 tasks[] 的 phase 取值集合内",
            "next_action": NEXT_ACTION_EXIT,
            "pool_size": 0,
            "valid_phases": sorted(phase_values),
            "reason": "unknown_phase",
        }
    active_phase, gated_kept, phase_gated = _apply_phase_gate(
        kept, tasks, state, explicit_phase=phase
    )
    kept = gated_kept
    if not kept and guard_unavailable_count and guard_unavailable_count == len(excluded_conflicts):
        guard_unavailable_result: dict[str, Any] = {"candidate": None, "message": "guard_unavailable", "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": 0, "reason": "guard_unavailable", "mismatched": [], "excluded_conflicts": excluded_conflicts, "degraded_guards": degraded_guards}
        guard_unavailable_result["active_task_phase"] = active_phase
        guard_unavailable_result["phase_gated"] = phase_gated
        if cooldown_excluded:
            guard_unavailable_result["cooldown_excluded"] = cooldown_excluded
        return guard_unavailable_result
    if excluded_conflicts and not kept:
        # will be handled by _route
        pass
    routed = _route_by_role(store, tasks, state, derived, kept, candidate_conflicts, excluded_conflicts, degraded_guards, guard_unavailable_count, excluded_self_review, capabilities, exclude, cooldown_excluded=cooldown_excluded)
    routed["active_task_phase"] = active_phase
    routed["phase_gated"] = phase_gated
    return routed
