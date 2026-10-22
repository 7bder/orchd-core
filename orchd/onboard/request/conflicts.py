"""冲突策略解析与文件冲突过滤（_resolve_conflict_policy/_filter_conflicts）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
from functools import partial

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops import GUARD_FAIL_CLOSED, check_workspace_state, run_guard
from orchd.ledger import TaskState
from orchd.pool import (
    Candidate, INFLIGHT_MARKER, _build_claimed_files, build_dependency_index, detect_file_conflict,
    get_dependency_closure,
)
from .constants import CONFLICT_POLICIES, CONFLICT_POLICY_DEFAULT
from .gitinfo import _inflight_files


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
