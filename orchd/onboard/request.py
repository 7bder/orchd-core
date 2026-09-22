"""Orchd 任务生命周期管理 - request 域。

迁移自 orchd/onboard.py（task-split-onboard-bootstrap-request）：
  - _find_review_priority_tasks: 查找可审查的 in_review 任务
  - _build_candidates: 构建候选池
  - _filter_conflicts: 文件冲突过滤
  - _route_by_role: 按角色路由
  - request: 只读查询候选任务
"""

from __future__ import annotations
import subprocess
from functools import partial
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops import GUARD_FAIL_CLOSED, GUARD_WARN, check_workspace_state, run_guard
from orchd.guide import (
    NEXT_ACTION_EXIT,
    NEXT_ACTION_REVIEW_FIRST,
    NEXT_ACTION_SUBMIT_REVIEW,
    NEXT_ACTION_WAIT,
)
from orchd.ledger import Store, TaskDerived, TaskState, is_fingerprint_agent_id as _is_fingerprint_agent_id
from orchd.pool import (
    Candidate,
    INFLIGHT_MARKER,
    _build_claimed_files,
    build_dependency_index,
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
from orchd.gitops.repo import GitBackend


# conflict_policy（task-inflight-conflict-visibility）：在途任务重叠的处置策略。
# 缺省 warn —— 不新增任何跨任务硬阻断（硬阻断会造成互等死锁，且在途集合本身
# 不稳定）；冲突风险由对账/真源修复在事后吸收。
CONFLICT_POLICY_DEFAULT = "warn"
CONFLICT_POLICIES = ("warn", "serialize", "block")

# retract 认领冷却期（秒）：与 claim._RETRACT_COOLDOWN_S 保持一致（task-retract-bind-cooloff）。
_RETRACT_COOLDOWN_S = 300


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


# ------------------------------------------------------------------
# 在途冲突真源与策略（task-inflight-conflict-visibility）
# ------------------------------------------------------------------


def _git_lines(project_root: Path, *args: str) -> list[str] | None:
    """执行 git 子命令并返回非空输出行（best-effort）。

    异常 / 非零退出 / 超时返回 None（「测不到」），调用方据此降级，
    不把测不到伪装成空集。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _default_branch(project_root: Path) -> str:
    """默认分支名（best-effort；取不到回退 "main"，与 worktree._git_diff_names 一致）。"""
    try:
        from orchd.gitops import get_default_branch

        return get_default_branch(project_root) or "main"
    except Exception:
        return "main"


def _inflight_files(
    project_root: Path | None, tasks: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """以 git 事实取「在途」任务 → 实际改动文件（task-inflight-conflict-visibility）。

    「在途」= 分支 ``task/<id>`` 存在且有**未落默认分支的提交**（即不在
    ``git for-each-ref --merged=<default>`` 结果内）。覆盖 done / in_review /
    force-status 悬空态 —— 判定只看「改动是否已落 main」，与任务状态字段无关，
    这正是修掉 _build_claimed_files 只看 claimed 之状态盲区的方式。

    性能：批量 2 次 git 调用（列全部 task/* 分支 + 列已并入默认分支的分支），
    与候选数 N 无关；随后仅对在途任务逐个取实际改动文件（O(M)，M = 在途任务数），
    避免 O(N×M)。任一步失败即返回已收集部分（best-effort，与
    ``worktree.actual_changes_conflict`` 的降级语义一致）。

    Args:
        project_root: 主工作树根；None / 非 git → 空结果。
        tasks: 仅登记 _master.json 已知任务的分支，避免野生分支误报。

    Returns:
        {task_id: [实际改动文件]}；仅含「有未落 main 提交且改动非空」的任务。
    """
    if project_root is None:
        return {}
    known = {t.get("id", "") for t in tasks}
    prefix = "task/"
    refspec = f"refs/heads/{prefix}"
    branches = _git_lines(
        project_root, "for-each-ref", "--format=%(refname:short)", refspec
    )
    if not branches:
        return {}
    merged = set(
        _git_lines(
            project_root,
            "for-each-ref",
            f"--merged={_default_branch(project_root)}",
            "--format=%(refname:short)",
            refspec,
        )
        or []
    )
    inflight: dict[str, list[str]] = {}
    for branch in branches:
        if branch in merged or not branch.startswith(prefix):
            continue
        tid = branch[len(prefix):]
        if tid not in known:
            continue
        # 在途=分支级事实（仅 git 有分支概念）：经端口 GitBackend 直连（与原
        # _git_diff_names 同函数；无 git 时分支循环本就为空，此处不做快照语义外溢）。
        files = GitBackend(project_root).changed_paths(tid) or []
        if files:
            inflight[tid] = files
    return inflight


def _resolve_conflict_policy(raw: Any, degraded_guards: list[dict[str, Any]]) -> str:
    """解析 config.conflict_policy：缺省 warn；非法值回退 warn 并留痕（不得静默）。"""
    if raw is None:
        return CONFLICT_POLICY_DEFAULT
    if isinstance(raw, str) and raw in CONFLICT_POLICIES:
        return raw
    degraded_guards.append({
        "guard": "conflict_policy",
        "severity": "warning",
        "status": "fallback",
        "reason": "conflict_policy_invalid",
        "value": raw,
        "fallback": CONFLICT_POLICY_DEFAULT,
        "hint": (
            f"config.conflict_policy 取值非法（{raw!r}），已回退 "
            f"{CONFLICT_POLICY_DEFAULT}；合法值：{' / '.join(CONFLICT_POLICIES)}"
        ),
    })
    return CONFLICT_POLICY_DEFAULT


def _filter_conflicts(
    candidates: list[Candidate],
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    project_root: Path | None,
    conflict_policy: str | None = None,
) -> tuple[
    list[Candidate],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    int,
]:
    excluded_conflicts = []
    candidate_conflicts = {}
    kept = []
    degraded_guards: list[dict[str, Any]] = []
    guard_unavailable_count = 0
    policy = _resolve_conflict_policy(conflict_policy, degraded_guards)
    # 在途集合一次计算（与候选数 N 无关）：批量 2 次 git 调用 + O(在途数) 次改动查询。
    inflight_files = _inflight_files(project_root, tasks)
    claimed_files = _build_claimed_files(
        state, tasks, include_pending=True, inflight_files=inflight_files
    )
    # 依赖索引一次构建（与候选数 N 无关）：闭包原实现单次 O(T²)（子孙遍历每步全表扫描），
    # 且在每个候选循环里被反复调用 ⇒ 整体 O(候选数 × T²)（task-pool-build-sort 实测：
    # 任务量增长时 request 的主要热点）。此处提到循环外，单次闭包降为 O(T + E)。
    dep_index = build_dependency_index(tasks)
    # 工作区可用性一次探测（task-pool-build-sort）：原实现把它放在**每个候选**的守卫里
    # ⇒ 每候选一次 git 子进程（实测 n=200 时 173 次 spawn、占本路径绝大部分耗时）。
    # 探测是循环不变量，故提到循环外；失败/不适用时按原语义逐候选记 guard_unavailable
    # 并跳过（决策语义不变，降级留痕由「每候选一条」收敛为「一轮一条」）。
    actual_probe: dict[str, Any] | None = None
    actual_probe_failed = False
    if project_root is not None:
        def _probe_workspace() -> Any:
            st = check_workspace_state(project_root)
            if st.get("state") == "error":
                raise RuntimeError(st.get("error") or st.get("reason"))
            if not st.get("available"):
                raise NotApplicableError(st.get("reason") or "unavailable")
            return st

        try:
            actual_probe = run_guard(
                _probe_workspace, guard_name="actual_changes_conflict",
                on_error=GUARD_FAIL_CLOSED, fallback=None,
                context={"command": "request"}, hint="conflict precheck",
                degraded=degraded_guards)
        except OrchdError as e:
            if e.code is not ErrorCode.E030:
                raise
            actual_probe_failed = True

    for cand in candidates:
        conflicts = detect_file_conflict(state, tasks, cand.task, include_pending=True, claimed_files=claimed_files)
        # actual_changes_conflict 返回 list[dict]（与上方 conflicts 的 list[Conflict] dataclass
        # 不同源）；显式注解避免 mypy 沿控制流把本变量误并为 dataclass 列表（纯类型层修正）。
        actual_conflicts: list[dict[str, Any]] = []
        if project_root is not None:
            if actual_probe_failed:
                guard_unavailable_count += 1
                excluded_conflicts.append({"task_id": cand.task.get("id", ""), "conflicts": [{"task_id": "*", "files": sorted(cand.task.get("files_to_edit", [])), "claimed_by": "guard_unavailable", "source": "actual"}], "reason": "guard_unavailable", "guard": "actual_changes_conflict"})
                continue
            if actual_probe is None:
                # 环境不适用（非 git / 无 git）：与原先一致——不取实际改动，不阻断该候选
                actual_conflicts = []
            else:
                from orchd.worktree import actual_changes_conflict

                try:
                    actual_conflicts = run_guard(
                        partial(actual_changes_conflict,
                                project_root, state, tasks, cand.task),
                        guard_name="actual_changes_conflict",
                        on_error=GUARD_FAIL_CLOSED, fallback=[],
                        context={"task_id": cand.task.get("id", ""),
                                 "command": "request"},
                        hint="conflict precheck",
                        degraded=degraded_guards) or []
                except OrchdError as e:
                    # 预检故障 ⇒ 该候选按 guard_unavailable 排除（fail-closed，不做
                    # "默认无冲突"假设）；原实现同样逐候选处置，此处保持语义不变。
                    if e.code is not ErrorCode.E030:
                        raise
                    guard_unavailable_count += 1
                    excluded_conflicts.append({"task_id": cand.task.get("id", ""), "conflicts": [{"task_id": "*", "files": sorted(cand.task.get("files_to_edit", [])), "claimed_by": "guard_unavailable", "source": "actual"}], "reason": "guard_unavailable", "guard": "actual_changes_conflict"})
                    continue
        dep_closure = get_dependency_closure(
            cand.task.get("id", ""), tasks, index=dep_index
        )
        # 显式注解：本列表由多来源 dict 字面量拼装（declared / inflight / actual），
        # 无注解时 mypy 会按首个 append 收窄元素类型。
        excluded: list[dict[str, Any]] = []
        pending_soft = []
        for c in conflicts:
            if c.claimed_by == "pending":
                pending_soft.append({"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by})
            elif c.claimed_by == INFLIGHT_MARKER:
                # 在途重叠按 conflict_policy 分流：warn（缺省）= 仅提示 + 降权排序，
                # 不新增任何阻断；serialize / block = 硬排除并给出可解释字段。
                other = state.get(c.task_id)
                entry = {
                    "task_id": c.task_id,
                    "files": c.files,
                    "claimed_by": c.claimed_by,
                    "source": "inflight",
                    "blocked_by": c.task_id,
                    "other_status": (other.status if other else "pending"),
                    "policy": policy,
                }
                if policy in ("serialize", "block"):
                    excluded.append(entry)
                else:
                    pending_soft.append(entry)
            else:
                excluded.append({"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by, "source": "declared"})
        for ac in actual_conflicts:
            if ac.get("task_id") in dep_closure:
                continue
            excluded.append({"task_id": ac["task_id"], "files": ac.get("files", []), "claimed_by": ac.get("claimed_by", "actual"), "source": "actual"})
        # 去重（task-request-response-fidelity AC2）：同一（对方任务, 文件集合）组合只保留
        # 一条，actual 优先于 declared（实际在途比声明更精确）。循环变量取 ac 以与上方
        # Conflict dataclass 循环（c）区分，避免同作用域内两种类型混用。
        _seen: dict[tuple[str, frozenset[str]], dict[str, Any]] = {}
        for _e in excluded:
            _key = (_e["task_id"], frozenset(_e.get("files", [])))
            if _key not in _seen or _e.get("source") == "actual":
                _seen[_key] = _e
        excluded = list(_seen.values())
        if excluded:
            excluded_conflicts.append({"task_id": cand.task.get("id", ""), "conflicts": excluded})
            continue
        kept.append(cand)
        if pending_soft:
            candidate_conflicts[cand.task.get("id", "")] = pending_soft
    kept.sort(key=lambda c: c.task.get("id", "") in candidate_conflicts)
    return kept, excluded_conflicts, candidate_conflicts, degraded_guards, guard_unavailable_count


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
            # 显式注解（首处定义即声明）：本函数各分支 result 的 value 类型集合不同，
            # 无注解时 mypy 按首处字面量收窄并在后续分支报 dict-item。
            result: dict[str, Any] = {"candidate": None, "message": message, "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
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
            result = {"candidate": None, "message": message, "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
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
                result = {"candidate": None, "message": message, "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
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
                result = {"candidate": None, "message": _none_ready_message(cooldown_excluded), "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": blocked_count, "reason": reason, "mismatched": mismatched, "excluded_conflicts": excluded_conflicts}
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
    for optional in ("difficulty", "estimated_hours"):
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
        if etype == "RETRACT" and not ev.get("retracted"):
            task_last_retract[tid] = ev
        elif etype == "CLAIMED" and not ev.get("retracted"):
            task_last_claimed[tid] = ev.get("timestamp")

    kept = []
    cooldown_excluded = []
    now = None
    for cand in candidates:
        tid = cand.task.get("id", "")
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
        if ts.status == "in_review" and ts.review_claimed_by == agent_id:
            return tid, (ts.review_phase or "unified")
    return None


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
    if not kept and guard_unavailable_count and guard_unavailable_count == len(excluded_conflicts):
        guard_unavailable_result: dict[str, Any] = {"candidate": None, "message": "guard_unavailable", "next_action": NEXT_ACTION_EXIT, "pool_size": 0, "blocked_count": 0, "reason": "guard_unavailable", "mismatched": [], "excluded_conflicts": excluded_conflicts, "degraded_guards": degraded_guards}
        if cooldown_excluded:
            guard_unavailable_result["cooldown_excluded"] = cooldown_excluded
        return guard_unavailable_result
    if excluded_conflicts and not kept:
        # will be handled by _route
        pass
    return _route_by_role(store, tasks, state, derived, kept, candidate_conflicts, excluded_conflicts, degraded_guards, guard_unavailable_count, excluded_self_review, capabilities, exclude, cooldown_excluded=cooldown_excluded)