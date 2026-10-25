"""候选池构造与按角色路由（_find_review_priority_tasks/_build_candidates/_none_ready_message/_route_by_role/_filter_cooldown_tasks/_held_review_claim）。"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_RETRACT,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
from orchd.contracts.task_schema import (
    BRIEF_FIELD,
    DEPENDS_ON_FIELD,
    DIFFICULTY_FIELD,
    ESTIMATED_HOURS_FIELD,
    ID_FIELD,
    IMPORTANCE_FIELD,
    MAX_ATTEMPTS_FIELD,
    MODULE_FIELD,
    NAME_FIELD,
    REQUIRE_INDEPENDENT_REVIEW_FIELD,
    REQUIRES_FIELD,
    REVIEWERS_FIELD,
    SOURCE_FIELD,
)
from orchd.guide import NEXT_ACTION_EXIT
from orchd.ledger import Store, TaskDerived, TaskState
from orchd.ledger import is_fingerprint_agent_id as _is_fingerprint_agent_id
from orchd.pool import Candidate, INFLIGHT_MARKER, build_pool, effective_importance, sort_candidates
from orchd.review import extract_last_done as _extract_last_done
from orchd.review import extract_review_comments as _extract_review_comments
from .constants import CONFLICT_POLICY_DEFAULT, _RETRACT_COOLDOWN_S


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
       is_self_review（照常分配）；enforce_self_review_block=True 或任务
       require_independent_review=true 时归入 excluded_self_review（不分配，AC1）。

    H2（2026-08-13）：``derived`` 为 request 单次扫描的派生缓存，
    循环内查询实现者改为 O(1)（原实现对每个候选任务全扫一次 ledger）。
    """
    task_map = {t.get(ID_FIELD, ""): t for t in tasks}
    review_tasks: list[dict[str, Any]] = []
    excluded_self_review: list[dict[str, Any]] = []
    for tid, ts in state.items():
        if ts.status != STATUS_IN_REVIEW or ts.review_claimed_by is not None:
            continue
        task_def = task_map.get(tid, {})
        # 向后兼容：reviewers 字段存在且非空时仍按其门禁（旧契约/测试）。
        # 字段缺失或为空（指纹身份模型）则跳过名单门禁，仅按实现指纹去重。
        # 指纹豁免（task-fp-review-priority-exempt，对齐 claim 侧 E007）：指纹形态
        # agent_id（12 位 hex）无法预写静态 reviewers 名单，不在名单内也不排除；
        # 具名 agent（名单外）仍被排除（向后兼容）。
        designated = task_def.get(REVIEWERS_FIELD)
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
                NAME_FIELD: task_def.get(NAME_FIELD, ""),
                "done_author": done_author,
                "is_self_review": True,
            })
        # task-review-independence-enforce：任务级独立审查与全局开关 OR。
        if is_self and (enforce_self_review_block
                        or task_def.get(REQUIRE_INDEPENDENT_REVIEW_FIELD, False)):
            continue
        entry = {
            "task_id": tid,
            "review_phase": ts.review_phase or "spec",
            NAME_FIELD: task_def.get(NAME_FIELD, ""),
        }
        if is_self:
            entry["is_self_review"] = True
        review_tasks.append(entry)
    # spec 阶段优先
    review_tasks.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    return review_tasks, excluded_self_review


def _build_candidates(
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    capabilities: list[str] | None,
    exclude: list[str] | None,
    sort_key: str | None,
    importance_thresholds: dict[str, Any] | None,
) -> list[Candidate]:
    candidates = build_pool(tasks, state, capabilities=capabilities, exclude=exclude)
    candidates = sort_candidates(candidates, sort_key=sort_key, importance_thresholds=importance_thresholds)
    return candidates


def _none_ready_message(cooldown_excluded: list[dict[str, Any]] | None) -> str:
    """``none_ready`` 分支文案（task-request-cooldown-surface）。

    无冷却剔除时沿用既有文案（零回归）；存在冷却剔除时点名「数量 + 任务 id +
    可重试语义」，避免把「候选都因 retract 冷却被剔除」误报成
    「所有任务已完成或被阻塞」，与 conflict_excluded / excluded_self_review
    等剔除类字段的留痕口径一致。
    """
    if not cooldown_excluded:
        return "所有任务已完成或被阻塞"
    ids = ", ".join(str(entry.get("task_id", "")) for entry in cooldown_excluded)
    return (
        f"当前无就绪候选：{len(cooldown_excluded)} 个任务处于 retract 认领冷却期"
        f"（{ids}），冷却结束（自 retract 起 {_RETRACT_COOLDOWN_S}s）后可重试"
    )


def _route_by_role(
    store: Store,
    tasks: list[dict[str, Any]],
    state: dict[str, TaskState],
    derived: TaskDerived,
    candidates: list[Candidate],
    candidate_conflicts: dict[str, Any],
    excluded_conflicts: list[dict[str, Any]],
    degraded_guards: list[dict[str, Any]],
    guard_unavailable_count: int,
    excluded_self_review: list[dict[str, Any]],
    capabilities: list[str] | None = None,
    exclude: list[str] | None = None,
    cooldown_excluded: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not candidates:
        # 四分支语义恢复（43e2f72~1 之前行为）：按 guard_unavailable > conflict_excluded > capability_mismatch > none_ready 优先级
        if guard_unavailable_count and guard_unavailable_count == len(excluded_conflicts) and excluded_conflicts:
            reason = "guard_unavailable"
            mismatched: list = []
            message = "guard_unavailable: 冲突预检门禁跑不起来（git 探测失败），请检查 git 环境后重试"
            blocked_count = 0
            for task in tasks:
                tid = task.get(ID_FIELD, "")
                ts = state.get(tid)
                s = ts.status if ts else STATUS_PENDING
                if s == STATUS_PENDING:
                    for dep_id in task.get(DEPENDS_ON_FIELD, []):
                        dep_ts = state.get(dep_id)
                        dep_s = dep_ts.status if dep_ts else STATUS_PENDING
                        if dep_s not in TERMINAL_STATUSES:
                            blocked_count += 1
                            break
            # 显式注解（首处定义即声明）：本函数各分支 result 的 value 类型集合不同，
            # 无注解时 mypy 按首处字面量收窄并在后续分支报 dict-item。
            result: dict[str, Any] = {"candidate": None, "message": message, "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        elif excluded_conflicts:
            reason = "conflict_excluded"
            mismatched = []
            message = "全部就绪候选因文件冲突被依赖感知强制过滤，请等待冲突任务完成后重试"
            blocked_count = 0
            for task in tasks:
                tid = task.get(ID_FIELD, "")
                ts = state.get(tid)
                s = ts.status if ts else STATUS_PENDING
                if s == STATUS_PENDING:
                    for dep_id in task.get(DEPENDS_ON_FIELD, []):
                        dep_ts = state.get(dep_id)
                        dep_s = dep_ts.status if dep_ts else STATUS_PENDING
                        if dep_s not in TERMINAL_STATUSES:
                            blocked_count += 1
                            break
            result = {"candidate": None, "message": message, "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        elif capabilities:
            unfiltered = build_pool(tasks, state, capabilities=None, exclude=exclude)
            mismatched = [{"task_id": c.task.get(ID_FIELD, ""), REQUIRES_FIELD: list(c.task.get(REQUIRES_FIELD, []))} for c in unfiltered if not set(c.task.get(REQUIRES_FIELD, [])).issubset(set(capabilities))]
            if mismatched:
                reason = "capability_mismatch"
                message = "能力不匹配：存在就绪候选但 requires 不满足，请检查 --capabilities"
                blocked_count = 0
                for task in tasks:
                    tid = task.get(ID_FIELD, "")
                    ts = state.get(tid)
                    s = ts.status if ts else STATUS_PENDING
                    if s == STATUS_PENDING:
                        for dep_id in task.get(DEPENDS_ON_FIELD, []):
                            dep_ts = state.get(dep_id)
                            dep_s = dep_ts.status if dep_ts else STATUS_PENDING
                            if dep_s not in TERMINAL_STATUSES:
                                blocked_count += 1
                                break
                result = {"candidate": None, "message": message, "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
            else:
                reason = "none_ready"
                mismatched = []
                blocked_count = 0
                for task in tasks:
                    tid = task.get(ID_FIELD, "")
                    ts = state.get(tid)
                    s = ts.status if ts else STATUS_PENDING
                    if s == STATUS_PENDING:
                        for dep_id in task.get(DEPENDS_ON_FIELD, []):
                            dep_ts = state.get(dep_id)
                            dep_s = dep_ts.status if dep_ts else STATUS_PENDING
                            if dep_s not in TERMINAL_STATUSES:
                                blocked_count += 1
                                break
                result = {"candidate": None, "message": _none_ready_message(cooldown_excluded), "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        else:
            reason = "none_ready"
            mismatched = []
            blocked_count = 0
            for task in tasks:
                tid = task.get(ID_FIELD, "")
                ts = state.get(tid)
                s = ts.status if ts else STATUS_PENDING
                if s == STATUS_PENDING:
                    for dep_id in task.get(DEPENDS_ON_FIELD, []):
                        dep_ts = state.get(dep_id)
                        dep_s = dep_ts.status if dep_ts else STATUS_PENDING
                        if dep_s not in TERMINAL_STATUSES:
                            blocked_count += 1
                            break
            result = {"candidate": None, "message": _none_ready_message(cooldown_excluded), "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
        if excluded_self_review:
            result["excluded_self_review"] = excluded_self_review
        if degraded_guards:
            result["degraded_guards"] = degraded_guards
        # 剔除须留痕（task-request-cooldown-surface）：有冷却剔除才补字段，空列表维持既有字段集合
        if cooldown_excluded:
            result["cooldown_excluded"] = cooldown_excluded
        return result
    best = candidates[0]
    task_id = best.task.get(ID_FIELD, "")
    downstream_blocked = []
    for task in tasks:
        ts = state.get(task.get(ID_FIELD, ""))
        s = ts.status if ts else STATUS_PENDING
        if s == STATUS_PENDING and task_id in task.get(DEPENDS_ON_FIELD, []):
            downstream_blocked.append(task.get(ID_FIELD, ""))
    review_comments = _extract_review_comments(store, task_id, derived)
    warnings = []
    ts = state.get(task_id)
    if ts and ts.attempt_count > 0:
        warnings.append(f"rework_task: 第 {ts.attempt_count} 轮返工")
        if ts.attempt_count >= best.task.get(MAX_ATTEMPTS_FIELD, 3):
            warnings.append("exceeded_max_attempts")
    candidate = {"task_id": task_id, NAME_FIELD: best.task.get(NAME_FIELD, ""), BRIEF_FIELD: best.task.get(BRIEF_FIELD, ""), MODULE_FIELD: best.task.get(MODULE_FIELD, ""), IMPORTANCE_FIELD: effective_importance(best.task, best.blocked_downstream_count), DEPENDS_ON_FIELD: list(best.task.get(DEPENDS_ON_FIELD, [])), "downstream_blocked": downstream_blocked, "review_comments": review_comments, SOURCE_FIELD: best.task.get(SOURCE_FIELD)}
    if task_id in candidate_conflicts:
        candidate["conflict_with"] = candidate_conflicts[task_id]
        warnings.append(f"file_conflict_pending: 与池内 {len(candidate_conflicts[task_id])} 个任务共享声明文件")
        _inflight_n = sum(
            1 for x in candidate_conflicts[task_id]
            if x.get("claimed_by") == INFLIGHT_MARKER
        )
        if _inflight_n:
            _pol = next(
                (x.get("policy") for x in candidate_conflicts[task_id] if x.get("policy")),
                CONFLICT_POLICY_DEFAULT,
            )
            warnings.append(
                f"file_conflict_inflight: 其中 {_inflight_n} 个为在途任务（改动未落 main，"
                f"conflict_policy={_pol}）；warn 下仅提示不阻断"
            )
    if ts and ts.attempt_count > 0:
        candidate["rework"] = True
        candidate["attempt_count"] = ts.attempt_count
    for optional in (DIFFICULTY_FIELD, ESTIMATED_HOURS_FIELD):
        if optional in best.task:
            candidate[optional] = best.task[optional]
    result = {"candidate": candidate, "pool_size": len(candidates), "prompt": f"确认将此任务分配给 {task_id}?", "warnings": warnings, "excluded_conflicts": excluded_conflicts}
    if excluded_self_review:
        result["excluded_self_review"] = excluded_self_review
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    # 剔除须留痕（task-request-cooldown-surface）：命中有候选 + 另有冷却剔除时同样透出
    if cooldown_excluded:
        result["cooldown_excluded"] = cooldown_excluded
    return result


def _filter_cooldown_tasks(
    candidates: list[Candidate], store: Store
) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """剔除在 retract 认领冷却期内的任务（task-retract-bind-cooloff）。

    扫描 ledger 找每个候选任务最近一条 RETRACT 事件（disposition=abandon），
    若在 _RETRACT_COOLDOWN_S 内且无后续 CLAIMED → 剔除。
    """
    from datetime import datetime
    if not candidates:
        return candidates, []
    all_events = store._read_ledger_lines(from_line=1)
    # 按 task_id 收集最近 RETRACT 和 CLAIMED 时间
    task_last_retract = {}
    task_last_claimed = {}
    for ev in all_events:
        tid = ev.get("task_id")
        etype = ev.get("type")
        if etype == EVENT_RETRACT and not ev.get("retracted"):
            task_last_retract[tid] = ev
        elif etype == EVENT_CLAIMED and not ev.get("retracted"):
            task_last_claimed[tid] = ev.get("timestamp")

    kept = []
    cooldown_excluded = []
    now = None
    for cand in candidates:
        tid = cand.task.get(ID_FIELD, "")
        retract_ev = task_last_retract.get(tid)
        if retract_ev is None:
            kept.append(cand)
            continue
        # disposition 非 abandon 不冷却
        if retract_ev.get("disposition", "abandon") != "abandon":
            kept.append(cand)
            continue
        # 有后续 CLAIMED 不冷却
        claimed_ts = task_last_claimed.get(tid)
        if claimed_ts:
            try:
                rt = datetime.fromisoformat(retract_ev["timestamp"])
                ct = datetime.fromisoformat(claimed_ts)
                if ct > rt:
                    kept.append(cand)
                    continue
            except (ValueError, KeyError):
                pass
        # 检查时间
        try:
            rt = datetime.fromisoformat(retract_ev["timestamp"])
            if now is None:
                now = datetime.now(rt.tzinfo) if rt.tzinfo else datetime.now()
            seconds_ago = (now - rt).total_seconds()
        except (ValueError, KeyError):
            kept.append(cand)
            continue
        if seconds_ago < _RETRACT_COOLDOWN_S:
            cooldown_excluded.append({"task_id": tid, "retracted_at": retract_ev["timestamp"], "seconds_ago": seconds_ago})
        else:
            kept.append(cand)
    return kept, cooldown_excluded


def _held_review_claim(
    state: dict[str, TaskState], agent_id: str
) -> tuple[str, str] | None:
    """本 agent 已领未提交的审查认领 ``(tid, review_phase)``；无则 None。

    判据与 :func:`orchd.guide._summarize` 的 ``my_review_tid`` 同源（in_review 且
    ``review_claimed_by`` 为当前 agent），保证 request 出口与 guidance step 不漂移
    （task-guidance-review-priority-fix）。
    """
    for tid, ts in state.items():
        if ts.status == STATUS_IN_REVIEW and ts.review_claimed_by == agent_id:
            return tid, (ts.review_phase or "unified")
    return None
