"""request 只读查询入口（编排器）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.guide import (
    NEXT_ACTION_DONE,
    NEXT_ACTION_EXIT,
    NEXT_ACTION_REVIEW_FIRST,
    NEXT_ACTION_SUBMIT_REVIEW,
    NEXT_ACTION_WAIT,
)
from orchd.ledger import Store
from orchd.review import request_reviewer as _request_reviewer
from .conflicts import _filter_conflicts, _filter_serial_source
from .phase import _apply_phase_gate
from .routing import (
    _build_candidates, _filter_cooldown_tasks, _find_review_priority_tasks, _held_review_claim,
    _route_by_role,
)


def _gate_impl_candidates(
    store: Store,
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    capabilities: list[str] | None,
    exclude: list[str] | None,
    sort_key: str | None,
    importance_thresholds: dict[str, Any] | None,
    project_root: Path | None,
    conflict_policy: str | None,
) -> dict[str, Any]:
    """实现候选的统一前置门（冷却 → 文件冲突 → 同 source 串行）。

    主路径与「审查优先」分支的 pool_size 口径必须同源（AC1
    task-request-response-fidelity），故三条管线只在此实现一次：
    分支各自复制会让 serial_blocked / cooldown_excluded 的漏出与计数漂移。

    串行剔除放在冲突过滤之后：冲突判定含实际工作区探测（一轮一次 git），先过滤
    省不掉该成本，而放在其后可保证 serial_blocked 只记录「冲突之外仍不可领」的卡，
    不与其他剔除原因重复归因。
    """
    candidates = _build_candidates(
        state, tasks, capabilities, exclude, sort_key, importance_thresholds)
    candidates, cooldown_excluded = _filter_cooldown_tasks(candidates, store)
    kept, excluded_conflicts, candidate_conflicts, degraded_guards, guard_unavailable_count = _filter_conflicts(
        candidates, state, tasks, project_root, conflict_policy, sort_key, importance_thresholds)
    kept, serial_blocked = _filter_serial_source(kept, state, tasks)
    return {
        "kept": kept,
        "excluded_conflicts": excluded_conflicts,
        "candidate_conflicts": candidate_conflicts,
        "degraded_guards": degraded_guards,
        "guard_unavailable_count": guard_unavailable_count,
        "cooldown_excluded": cooldown_excluded,
        "serial_blocked": serial_blocked,
    }


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
        return _request_reviewer(store, state, tasks, agent_id, derived)
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
    review_priority, excluded_self_review = _find_review_priority_tasks(store, state, tasks, agent_id, derived)
    # task-feedback-request-review-priority-align：本会话已持有实现认领时先给 done 出口，
    # 与 _classify 的 my_claimed 优先顺序对齐——否则「他人在审 + 我持有 claimed」会产出
    # review_first，agent 去 claim 审查被 E011 拒，手上的实现任务悬空。
    held_impl = next(
        (t.get("id") for t in tasks
         if (ts := state.get(t.get("id") or "")) is not None
         and ts.status == "claimed" and ts.claimed_by == agent_id),
        None,
    )
    if held_impl is not None:
        _impl_gated = _gate_impl_candidates(
            store, state, tasks, capabilities, exclude, sort_key, importance_thresholds,
            project_root, conflict_policy)
        held_resp: dict[str, Any] = {
            "candidate": None,
            "message": (f"本会话已持有实现任务：{held_impl}，请先 done 收尾再领取新任务"),
            "next_action": NEXT_ACTION_DONE,
            "impl_held": {"task_id": held_impl},
            "pool_size": len(_impl_gated["kept"]),
            "blocked_by": "held_impl",
        }
        if _impl_gated["cooldown_excluded"]:
            held_resp["cooldown_excluded"] = _impl_gated["cooldown_excluded"]
        if _impl_gated["serial_blocked"]:
            held_resp["serial_blocked"] = _impl_gated["serial_blocked"]
        return held_resp
    if review_priority:
        best_review = review_priority[0]
        rp_entry = {"task_id": best_review["task_id"], "review_phase": best_review["review_phase"], "name": best_review["name"], "total_available": len(review_priority)}
        # AC1（task-request-response-fidelity）：审查优先分支不得硬编码 pool_size: 0——
        # 复用主路径同一口径（_gate_impl_candidates：冷却 + 冲突 + 串行）算出真实可领取
        # 实现候选数，并给 blocked_by 归因，避免 candidate=null + pool_size=0 被读成「没活」。
        # task-request-serial-source-pregate：pool_size 不含串行忙态同源候选（与主路径同源）。
        _impl_gated = _gate_impl_candidates(
            store, state, tasks, capabilities, exclude, sort_key, importance_thresholds,
            project_root, conflict_policy)
        resp: dict[str, Any] = {"candidate": None, "review_priority": rp_entry, "message": f"有 {len(review_priority)} 个待审查任务可领取", "next_action": NEXT_ACTION_REVIEW_FIRST, "pool_size": len(_impl_gated["kept"]), "blocked_by": "review_priority"}
        if excluded_self_review:
            resp["excluded_self_review"] = excluded_self_review
        # 剔除须留痕：本分支同样算过冷却剔除（此前丢弃），非空时一并透出
        if _impl_gated["cooldown_excluded"]:
            resp["cooldown_excluded"] = _impl_gated["cooldown_excluded"]
        if _impl_gated["serial_blocked"]:
            resp["serial_blocked"] = _impl_gated["serial_blocked"]
        return resp
    if max_active is not None:
        active = sum(1 for ts in state.values() if ts.status == "claimed")
        if active >= max_active:
            return {"candidate": None, "message": f"max_active {active}/{max_active}", "next_action": NEXT_ACTION_WAIT, "reason": "max_active_reached", "pool_size": 0, "active_count": active, "max_active": max_active}
    # task-retract-bind-cooloff：冷却期任务剔除在 _gate_impl_candidates 内；
    # task-request-serial-source-pregate：同 source 串行忙态剔除同在此管线。
    gated = _gate_impl_candidates(
        store, state, tasks, capabilities, exclude, sort_key, importance_thresholds,
        project_root, conflict_policy)
    kept = gated["kept"]
    excluded_conflicts = gated["excluded_conflicts"]
    candidate_conflicts = gated["candidate_conflicts"]
    degraded_guards = gated["degraded_guards"]
    guard_unavailable_count = gated["guard_unavailable_count"]
    cooldown_excluded = gated["cooldown_excluded"]
    serial_blocked = gated["serial_blocked"]
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
    if not kept and serial_blocked:
        # task-request-serial-source-pregate：全部实现候选因同 source 忙态被剔除时，
        # 不得落回 none_ready（文案是「所有任务已完成或被阻塞」，会被读成池空）。
        # next_action=wait 复用既有词表（与 max_active 同档），不新增 step / 词表项。
        busy_ids = sorted({sid for entry in serial_blocked for sid in entry["busy_siblings"]})
        serial_result: dict[str, Any] = {
            "candidate": None,
            "message": (
                f"同 source 串行包：兄弟任务在手（{', '.join(busy_ids)}），"
                "等待其 completed 或 retract 后再领取"
            ),
            "next_action": NEXT_ACTION_WAIT,
            "pool_size": 0,
            "reason": "serial_source_busy",
            "serial_blocked": serial_blocked,
            "blocked_by": "serial_source_busy",
        }
        if excluded_conflicts:
            serial_result["excluded_conflicts"] = excluded_conflicts
        if cooldown_excluded:
            serial_result["cooldown_excluded"] = cooldown_excluded
        if excluded_self_review:
            serial_result["excluded_self_review"] = excluded_self_review
        if degraded_guards:
            serial_result["degraded_guards"] = degraded_guards
        serial_result["active_task_phase"] = active_phase
        serial_result["phase_gated"] = phase_gated
        return serial_result
    if excluded_conflicts and not kept:
        # will be handled by _route
        pass
    routed = _route_by_role(store, tasks, state, derived, kept, candidate_conflicts, excluded_conflicts, degraded_guards, guard_unavailable_count, excluded_self_review, capabilities, exclude, cooldown_excluded=cooldown_excluded)
    routed["active_task_phase"] = active_phase
    routed["phase_gated"] = phase_gated
    if serial_blocked:
        routed["serial_blocked"] = serial_blocked
    return routed
