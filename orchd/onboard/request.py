"""Orchd 任务生命周期管理 - request 域。

迁移自 orchd/onboard.py（task-split-onboard-bootstrap-request）：
  - _find_review_priority_tasks: 查找可审查的 in_review 任务
  - _build_candidates: 构建候选池
  - _filter_conflicts: 文件冲突过滤
  - _route_by_role: 按角色路由
  - request: 只读查询候选任务
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops import GUARD_FAIL_CLOSED, GUARD_WARN, check_workspace_state, run_guard
from orchd.ledger import Store, TaskDerived, TaskState, is_fingerprint_agent_id as _is_fingerprint_agent_id
from orchd.pool import (
    _build_claimed_files,
    build_pool,
    detect_file_conflict,
    effective_importance,
    get_dependency_closure,
    sort_candidates,
)
from orchd.review import (
    extract_last_done as _extract_last_done,
    extract_review_comments as _extract_review_comments,
    request_reviewer as _request_reviewer,
)


# ------------------------------------------------------------------
# request（只读，无锁）
# ------------------------------------------------------------------


def _find_review_priority_tasks(
    store: Store, state: dict[str, TaskState], tasks: list[dict[str, Any]],
    agent_id: str, derived: TaskDerived | None = None,
    enforce_self_review_block: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """查找该 agent 可审查的 in_review 任务（用于 implementer request 时的优先调度提示）。

    返回 ``(review_tasks, excluded_self_review)`` 二元组：
    - ``review_tasks``：可分配的审查任务（按 spec 阶段优先排序）。
    - ``excluded_self_review``：自审任务列表。默认（enforce=False）仅标注、照常
      进入 review_tasks；线上版 enforce=True 时改为排除（仅标注、不分配）。

    过滤条件（task-fp-request-filter，指纹身份模型）：
    1. 任务状态 in_review 且审查未被他人认领（review_claimed_by is None）。
    2. reviewers 名单门禁仅作**向后兼容**：该字段存在且非空时，不在名单内
       直接排除（不计入自审）；字段缺失/为空（生产 _master.json 已无
       reviewers）则跳过名单门禁，仅按实现指纹去重。
    3. self-review：DONE 实现指纹 == 当前 request 指纹时，默认仅标注
       is_self_review（照常分配）；enforce_self_review_block=True 时归入
       excluded_self_review（不分配，AC1）。

    H2（2026-08-13）：``derived`` 为 request 单次扫描的派生缓存，
    循环内查询实现者改为 O(1)（原实现对每个候选任务全扫一次 ledger）。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    review_tasks: list[dict[str, Any]] = []
    excluded_self_review: list[dict[str, Any]] = []
    for tid, ts in state.items():
        if ts.status != "in_review" or ts.review_claimed_by is not None:
            continue
        task_def = task_map.get(tid, {})
        # 向后兼容：reviewers 字段存在且非空时仍按其门禁（旧契约/测试）。
        # 字段缺失或为空（指纹身份模型）则跳过名单门禁，仅按实现指纹去重。
        # 指纹豁免（task-fp-review-priority-exempt，对齐 claim 侧 E007）：指纹形态
        # agent_id（12 位 hex）无法预写静态 reviewers 名单，不在名单内也不排除；
        # 具名 agent（名单外）仍被排除（向后兼容）。
        designated = task_def.get("reviewers")
        if designated and agent_id not in designated \
                and not _is_fingerprint_agent_id(agent_id):
            continue
        # self-review：DONE 实现指纹 == 当前 request 指纹。
        # 默认仅标注 is_self_review 并照常分配；enforce=True 时归入
        # excluded_self_review（不分配，保 AC1 可见性）。
        done_author, _ = _extract_last_done(store, tid, derived)
        is_self = bool(done_author and done_author == agent_id)
        if is_self:
            excluded_self_review.append({
                "task_id": tid,
                "review_phase": ts.review_phase or "spec",
                "name": task_def.get("name", ""),
                "done_author": done_author,
                "is_self_review": True,
            })
        if is_self and enforce_self_review_block:
            continue
        entry = {
            "task_id": tid,
            "review_phase": ts.review_phase or "spec",
            "name": task_def.get("name", ""),
        }
        if is_self:
            entry["is_self_review"] = True
        review_tasks.append(entry)
    # spec 阶段优先
    review_tasks.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    return review_tasks, excluded_self_review


def _build_candidates(state, tasks, capabilities, exclude, sort_key, importance_thresholds):
    candidates = build_pool(tasks, state, capabilities=capabilities, exclude=exclude)
    candidates = sort_candidates(candidates, sort_key=sort_key, importance_thresholds=importance_thresholds)
    return candidates


def _filter_conflicts(candidates, state, tasks, project_root):
    excluded_conflicts = []
    candidate_conflicts = {}
    kept = []
    degraded_guards = []
    guard_unavailable_count = 0
    claimed_files = _build_claimed_files(state, tasks, include_pending=True)
    for cand in candidates:
        conflicts = detect_file_conflict(state, tasks, cand.task, include_pending=True, claimed_files=claimed_files)
        actual_conflicts = []
        if project_root is not None:
            def _guard():
                st = check_workspace_state(project_root)
                if st.get("state") == "error":
                    raise RuntimeError(st.get("error") or st.get("reason"))
                if not st.get("available"):
                    raise NotApplicableError(st.get("reason") or "unavailable")
                from orchd.worktree import actual_changes_conflict
                return actual_changes_conflict(project_root, state, tasks, cand.task)
            try:
                actual_conflicts = run_guard(_guard, guard_name="actual_changes_conflict", on_error=GUARD_FAIL_CLOSED, fallback=[], context={"task_id": cand.task.get("id", ""), "command": "request"}, hint="conflict precheck", degraded=degraded_guards) or []
            except OrchdError as e:
                if e.code is not ErrorCode.E030:
                    raise
                guard_unavailable_count += 1
                excluded_conflicts.append({"task_id": cand.task.get("id", ""), "conflicts": [{"task_id": "*", "files": sorted(cand.task.get("files_to_edit", [])), "claimed_by": "guard_unavailable", "source": "actual"}], "reason": "guard_unavailable", "guard": "actual_changes_conflict"})
                continue
        dep_closure = get_dependency_closure(cand.task.get("id", ""), tasks)
        excluded = []
        pending_soft = []
        for c in conflicts:
            if c.claimed_by == "pending":
                pending_soft.append({"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by})
            else:
                excluded.append({"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by})
        for c in actual_conflicts:
            if c.get("task_id") in dep_closure:
                continue
            excluded.append({"task_id": c["task_id"], "files": c.get("files", []), "claimed_by": c.get("claimed_by", "actual"), "source": "actual"})
        if excluded:
            excluded_conflicts.append({"task_id": cand.task.get("id", ""), "conflicts": excluded})
            continue
        kept.append(cand)
        if pending_soft:
            candidate_conflicts[cand.task.get("id", "")] = pending_soft
    kept.sort(key=lambda c: c.task.get("id", "") in candidate_conflicts)
    return kept, excluded_conflicts, candidate_conflicts, degraded_guards, guard_unavailable_count


def _route_by_role(store, tasks, state, derived, candidates, candidate_conflicts, excluded_conflicts, degraded_guards, guard_unavailable_count, excluded_self_review, capabilities=None, exclude=None):
    if not candidates:
        # 四分支语义恢复（43e2f72~1 之前行为）：按 guard_unavailable > conflict_excluded > capability_mismatch > none_ready 优先级
        if guard_unavailable_count and guard_unavailable_count == len(excluded_conflicts) and excluded_conflicts:
            reason = "guard_unavailable"
            mismatched: list = []
            message = "guard_unavailable: 冲突预检门禁跑不起来（git 探测失败），请检查 git 环境后重试"
            blocked_count = 0
            for task in tasks:
                tid = task.get("id", "")
                ts = state.get(tid)
                s = ts.status if ts else "pending"
                if s == "pending":
                    for dep_id in task.get("depends_on", []):
                        dep_ts = state.get(dep_id)
                        dep_s = dep_ts.status if dep_ts else "pending"
                        if dep_s not in ("completed", "cancelled"):
                            blocked_count += 1
                            break
            result = {"candidate": None, "message": message, "next_action": "exit", "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        elif excluded_conflicts:
            reason = "conflict_excluded"
            mismatched = []
            message = "全部就绪候选因文件冲突被依赖感知强制过滤，请等待冲突任务完成后重试"
            blocked_count = 0
            for task in tasks:
                tid = task.get("id", "")
                ts = state.get(tid)
                s = ts.status if ts else "pending"
                if s == "pending":
                    for dep_id in task.get("depends_on", []):
                        dep_ts = state.get(dep_id)
                        dep_s = dep_ts.status if dep_ts else "pending"
                        if dep_s not in ("completed", "cancelled"):
                            blocked_count += 1
                            break
            result = {"candidate": None, "message": message, "next_action": "exit", "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        elif capabilities:
            unfiltered = build_pool(tasks, state, capabilities=None, exclude=exclude)
            mismatched = [{"task_id": c.task.get("id", ""), "requires": list(c.task.get("requires", []))} for c in unfiltered if not set(c.task.get("requires", [])).issubset(set(capabilities))]
            if mismatched:
                reason = "capability_mismatch"
                message = "能力不匹配：存在就绪候选但 requires 不满足，请检查 --capabilities"
                blocked_count = 0
                for task in tasks:
                    tid = task.get("id", "")
                    ts = state.get(tid)
                    s = ts.status if ts else "pending"
                    if s == "pending":
                        for dep_id in task.get("depends_on", []):
                            dep_ts = state.get(dep_id)
                            dep_s = dep_ts.status if dep_ts else "pending"
                            if dep_s not in ("completed", "cancelled"):
                                blocked_count += 1
                                break
                result = {"candidate": None, "message": message, "next_action": "exit", "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
            else:
                reason = "none_ready"
                mismatched = []
                blocked_count = 0
                for task in tasks:
                    tid = task.get("id", "")
                    ts = state.get(tid)
                    s = ts.status if ts else "pending"
                    if s == "pending":
                        for dep_id in task.get("depends_on", []):
                            dep_ts = state.get(dep_id)
                            dep_s = dep_ts.status if dep_ts else "pending"
                            if dep_s not in ("completed", "cancelled"):
                                blocked_count += 1
                                break
                result = {"candidate": None, "message": "所有任务已完成或被阻塞", "next_action": "exit", "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        else:
            reason = "none_ready"
            mismatched = []
            blocked_count = 0
            for task in tasks:
                tid = task.get("id", "")
                ts = state.get(tid)
                s = ts.status if ts else "pending"
                if s == "pending":
                    for dep_id in task.get("depends_on", []):
                        dep_ts = state.get(dep_id)
                        dep_s = dep_ts.status if dep_ts else "pending"
                        if dep_s not in ("completed", "cancelled"):
                            blocked_count += 1
                            break
            result = {"candidate": None, "message": "所有任务已完成或被阻塞", "next_action": "exit", "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        if excluded_self_review:
            result["excluded_self_review"] = excluded_self_review
        if degraded_guards:
            result["degraded_guards"] = degraded_guards
        return result
    best = candidates[0]
    task_id = best.task.get("id", "")
    downstream_blocked = []
    for task in tasks:
        ts = state.get(task.get("id", ""))
        s = ts.status if ts else "pending"
        if s == "pending" and task_id in task.get("depends_on", []):
            downstream_blocked.append(task.get("id", ""))
    review_comments = _extract_review_comments(store, task_id, derived)
    warnings = []
    ts = state.get(task_id)
    if ts and ts.attempt_count > 0:
        warnings.append(f"rework_task: 第 {ts.attempt_count} 轮返工")
        if ts.attempt_count >= best.task.get("max_attempts", 3):
            warnings.append("exceeded_max_attempts")
    candidate = {"task_id": task_id, "name": best.task.get("name", ""), "brief": best.task.get("brief", ""), "module": best.task.get("module", ""), "importance": effective_importance(best.task, best.blocked_downstream_count), "depends_on": list(best.task.get("depends_on", [])), "downstream_blocked": downstream_blocked, "review_comments": review_comments, "source": best.task.get("source")}
    if task_id in candidate_conflicts:
        candidate["conflict_with"] = candidate_conflicts[task_id]
        warnings.append(f"file_conflict_pending: 与池内 {len(candidate_conflicts[task_id])} 个任务共享声明文件")
    if ts and ts.attempt_count > 0:
        candidate["rework"] = True
        candidate["attempt_count"] = ts.attempt_count
    for optional in ("difficulty", "estimated_hours"):
        if optional in best.task:
            candidate[optional] = best.task[optional]
    result = {"candidate": candidate, "pool_size": len(candidates), "prompt": f"确认将此任务分配给 {task_id}?", "warnings": warnings, "excluded_conflicts": excluded_conflicts}
    if excluded_self_review:
        result["excluded_self_review"] = excluded_self_review
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    return result


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
    review_priority, excluded_self_review = _find_review_priority_tasks(store, state, tasks, agent_id, derived, enforce_self_review_block=enforce_self_review_block)
    if review_priority:
        best_review = review_priority[0]
        rp_entry = {"task_id": best_review["task_id"], "review_phase": best_review["review_phase"], "name": best_review["name"], "total_available": len(review_priority)}
        if best_review.get("is_self_review"):
            rp_entry["is_self_review"] = True
        resp: dict[str, Any] = {"candidate": None, "review_priority": rp_entry, "message": f"有 {len(review_priority)} 个待审查任务可领取", "next_action": "review_first", "pool_size": 0}
        if excluded_self_review:
            resp["excluded_self_review"] = excluded_self_review
        return resp
    if max_active is not None:
        active = sum(1 for ts in state.values() if ts.status == "claimed")
        if active >= max_active:
            return {"candidate": None, "message": f"max_active {active}/{max_active}", "next_action": "wait", "reason": "max_active_reached", "pool_size": 0, "active_count": active, "max_active": max_active}
    candidates = _build_candidates(state, tasks, capabilities, exclude, sort_key, importance_thresholds)
    kept, excluded_conflicts, candidate_conflicts, degraded_guards, guard_unavailable_count = _filter_conflicts(candidates, state, tasks, project_root)
    if not kept and guard_unavailable_count and guard_unavailable_count == len(excluded_conflicts):
        return {"candidate": None, "message": "guard_unavailable", "next_action": "exit", "pool_size": 0, "blocked_count": 0, "reason": "guard_unavailable", "mismatched": [], "excluded_conflicts": excluded_conflicts, "degraded_guards": degraded_guards}
    if excluded_conflicts and not kept:
        # will be handled by _route
        pass
    return _route_by_role(store, tasks, state, derived, kept, candidate_conflicts, excluded_conflicts, degraded_guards, guard_unavailable_count, excluded_self_review, capabilities, exclude)