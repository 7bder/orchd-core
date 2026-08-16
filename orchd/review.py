"""Orchd review 子域：审查申请、审查意见提取与审查提交。

将与审查（review）相关的辅助函数与主流程从 onboard.py 外置，
保持 onboard.py 只保留生命周期主干（bootstrap / request / claim /
done / retract / force_status）。

依赖方向：本模块不导入 onboard.py，避免循环依赖。共享辅助
（make_event / guard_write_command）自 orchd.gitops_ops 导入，
低级钩子（get_head_commit / session_lock_*）自 orchd.gitops 导入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    get_head_commit,
    session_lock_check,
    session_lock_release,
)
from orchd.gitops_ops import (
    guard_write_command,
    make_event,
    try_auto_resolve_conflict,
    try_delete_task_branch,
    try_git_merge,
)
from orchd.ledger import Store, TaskDerived, TaskState


def request_reviewer(
    store: Store,
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    agent_id: str,
    derived: TaskDerived | None = None,
) -> dict[str, Any]:
    """查找处于 in_review 且未被审查者 claim 的任务。

    排序规则：spec 阶段的审查优先于 code 阶段。同等阶段内按 ledger 遍历顺序排列。
    调用方（onboard.request）在 reviewer 角色下转发到此函数。
    """
    review_candidates: list[dict[str, Any]] = []
    not_in_list: list[dict[str, Any]] = []
    task_map = {t.get("id", ""): t for t in tasks}
    for tid, ts in state.items():
        if ts.status == "in_review" and ts.review_claimed_by is None:
            task_def = task_map.get(tid, {})
            if agent_id not in task_def.get("reviewers", []):
                not_in_list.append({
                    "task_id": tid,
                    "review_phase": ts.review_phase or "spec",
                    "reviewers": task_def.get("reviewers", []),
                })
                continue
            done_author, _ = extract_last_done(store, tid, derived)
            if done_author and done_author == agent_id:
                continue
            review_candidates.append({
                "task_id": tid,
                "task": task_def,
                "review_phase": ts.review_phase,
            })
    review_candidates.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    if not review_candidates:
        if not_in_list:
            return {
                "candidate": None,
                "message": f"有 {len(not_in_list)} 个待审查任务但你不在名单内",
                "next_action": "exit",
                "pool_size": 0,
                "reason": "not_in_reviewer_list",
                "tasks": not_in_list,
            }
        return {
            "candidate": None,
            "message": "当前无待审查任务",
            "next_action": "exit",
            "pool_size": 0,
        }
    best = review_candidates[0]
    task_def = best["task"]
    task_id = best["task_id"]
    author, changes_description = extract_last_done(store, task_id, derived)

    candidate: dict[str, Any] = {
        "task_id": task_id,
        "review_type": best["review_phase"],
        "module": task_def.get("module", ""),
        "brief": task_def.get("brief", ""),
        "importance": task_def.get("importance", "normal"),
        "files_to_review": [
            {"path": p, "priority": "must_read"}
            for p in task_def.get("files_to_edit", [])
        ],
        "acceptance_criteria": task_def.get("acceptance_criteria", []),
        "changes_description": changes_description,
    }
    if author:
        candidate["author"] = author

    return {
        "candidate": candidate,
        "pool_size": len(review_candidates),
        "prompt": f"确认将此审查分配给 {agent_id}？(执行 / 跳过 / 重新声明能力)",
        "warnings": [],
    }


def extract_review_comments(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> list[str]:
    """从 ledger 中提取该任务的所有审查意见（REVIEW_SUBMITTED 的 comments）。

    传入 ``derived`` 时直接从缓存读取（O(1)），否则全扫描 ledger。
    """
    if derived is not None:
        return derived.review_comments.get(task_id, [])
    comments: list[str] = []
    if not store.ledger_exists():
        return comments
    events = store._read_ledger_lines(from_line=1)
    for ev in events:
        if (
            ev.get("task_id") == task_id
            and ev.get("type") == "REVIEW_SUBMITTED"
            and ev.get("comments")
        ):
            comments.append(ev["comments"])
    return comments


def extract_last_done(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> tuple[str | None, str | None]:
    """从 ledger 中提取该任务最近一次 DONE 事件的 (agent_id, changes_description)。

    多轮返工时返回最近一轮的作者与变更描述。
    """
    if derived is not None:
        ev = derived.last_done.get(task_id)
        if ev:
            return ev.get("agent_id"), ev.get("changes_description")
        return None, None
    if not store.ledger_exists():
        return None, None
    events = store._read_ledger_lines(from_line=1)
    author: str | None = None
    changes: str | None = None
    for ev in events:
        if ev.get("task_id") == task_id and ev.get("type") == "DONE":
            author = ev.get("agent_id")
            changes = ev.get("changes_description")
    return author, changes


def find_last_done_event(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> dict[str, Any] | None:
    """从 ledger 中查找该任务最近一次 DONE 事件（含 timestamp/agent_id）。

    用于 done 的"假失败消除"：verify 失败但 ledger 已写 DONE 时，
    说明 DONE 已实际落地，应返回成功语义而非 E014。
    """
    if derived is not None:
        return derived.last_done.get(task_id)
    if not store.ledger_exists():
        return None
    events = store._read_ledger_lines(from_line=1)
    last: dict[str, Any] | None = None
    for ev in events:
        if ev.get("task_id") == task_id and ev.get("type") == "DONE":
            last = ev
    return last


def extract_review_baseline(
    store: Store, task_id: str, agent_id: str, derived: TaskDerived | None = None
) -> str | None:
    """从最近的 REVIEW_CLAIMED 事件提取 baseline_sha（用于漂移检测）。"""
    if derived is not None:
        return derived.review_baselines.get((task_id, agent_id))
    try:
        events = store._read_ledger_lines(from_line=1)
    except Exception:
        return None
    for event in reversed(events):
        if (
            event.get("type") == "REVIEW_CLAIMED"
            and event.get("task_id") == task_id
            and event.get("agent_id") == agent_id
        ):
            return event.get("baseline_sha")
    return None


def review_submit(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    review_type: str,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """提交审查结果（APPROVED 或 CHANGES_REQUESTED）。

    锁内校验：任务必须处于 in_review 状态，审查阶段（spec/code）须匹配，
    且审查者须为当前 agent。

    spec APPROVED 自动推进到 code review；code APPROVED 先锁外 git merge，
    成功才写完成事件（任务 completed），merge 冲突则停留 in_review。
    CHANGES_REQUESTED 时任务回退 pending。
    """
    guard_write_command(
        project_root,
        allowed_branches=None,
        require_clean=False,
        command="review",
        orchd_dir=store.orchd_dir,
        agent_id=agent_id,
    )

    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        derived = store.scan_task_derived()
        ts = state.get(task_id)

        if not ts or ts.status != "in_review":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: task '{task_id}' not in_review",
                [{"task_id": task_id, "actual": ts.status if ts else "pending"}],
            )
        if ts.review_phase != review_type:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review phase mismatch (expected '{ts.review_phase}', got '{review_type}')",
                [{"task_id": task_id, "expected_phase": ts.review_phase}],
            )
        if ts.review_claimed_by != agent_id:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review not claimed by '{agent_id}' (claimed by '{ts.review_claimed_by}')",
                [{"task_id": task_id, "claimed_by": ts.review_claimed_by, "agent": agent_id}],
            )

        baseline_sha = extract_review_baseline(store, task_id, agent_id, derived)
        current_sha = get_head_commit(project_root) if project_root else None
        baseline_drift = bool(baseline_sha and current_sha and baseline_sha != current_sha)

        event = make_event(
            task_id, agent_id, "REVIEW_SUBMITTED",
            review_type=review_type,
            verdict=verdict,
        )
        if comments:
            event["comments"] = comments

        result: dict[str, Any] = {
            "submitted": True,
            "task_id": task_id,
            "review_type": review_type,
            "verdict": verdict,
        }
        if integrity_warnings:
            result["integrity_warnings"] = integrity_warnings

        if baseline_drift:
            result["baseline_warning"] = (
                f"baseline drift detected: task branch HEAD changed during review "
                f"(claimed at {baseline_sha[:7]}, now {current_sha[:7]}). "
                f"Review may be based on outdated code."
            )

        pending_code_event: dict[str, Any] | None = None

        if verdict == "APPROVED" and review_type == "spec":
            store.append_event(event)
            code_ready = make_event(task_id, agent_id, "REVIEW_READY", review_type="code")
            store.append_event(code_ready)
            result["task_status"] = "in_review"
            result["next_review"] = "code"
            new_state = store.replay()
            store.update_checkpoint(new_state)

        elif verdict == "APPROVED" and review_type == "code":
            result["task_status"] = "in_review"
            pending_code_event = event

        elif verdict == "CHANGES_REQUESTED":
            store.append_event(event)
            result["task_status"] = "pending"
            result["back_to_pool"] = True
            new_state = store.replay()
            store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    if pending_code_event is not None:
        merge_result = None
        if project_root:
            merge_result = try_git_merge(project_root, task_id)

        auto_resolved = False
        conflict_files: list[str] = []
        if merge_result is not None and merge_result.get("conflict"):
            auto = try_auto_resolve_conflict(project_root, task_id) if project_root else None
            if auto and auto.get("resolved"):
                auto_resolved = True
            else:
                conflict_files = (auto or {}).get("conflict_files") or merge_result.get("files", [])
                result["merged"] = False
                result["reason"] = "merge_conflict"
                result["conflict_files"] = conflict_files
                result["task_status"] = "in_review"
                result["action"] = (auto or {}).get("action") or (
                    f"merge 冲突已发生（main 已恢复）：请在 task 分支 task/{task_id} "
                    f"上执行 git merge main 解决冲突并提交，然后由同一 reviewer "
                    f"重试 code APPROVED"
                )

        if not (merge_result is not None and merge_result.get("conflict")) or auto_resolved:
            store.acquire_lock()
            try:
                state = store.replay()
                ts = state.get(task_id)
                if (
                    not ts
                    or ts.status != "in_review"
                    or ts.review_claimed_by != agent_id
                    or ts.review_phase != review_type
                ):
                    result["task_status"] = ts.status if ts else "unknown"
                    result["merged"] = False
                    result["reason"] = "state_changed_during_merge"
                    result["action"] = (
                        f"git merge 已执行，但任务状态在 merge 期间被改变"
                        f"（当前 {result['task_status']}），完成事件未写入，"
                        f"请人工核对状态与 main 分支后处理"
                    )
                else:
                    if merge_result is None:
                        pending_code_event["merge_warning"] = (
                            "git merge 未执行（非 git 仓库或 git 不可用），"
                            "按 best-effort 语义标记完成"
                        )
                    store.append_event(pending_code_event)
                    new_state = store.replay()
                    store.update_checkpoint(new_state)
                    result["task_status"] = "completed"
            finally:
                store.release_lock()
            if result.get("reason") != "state_changed_during_merge":
                if merge_result is None:
                    result["merged"] = None
                    result["merge_warning"] = pending_code_event.get("merge_warning", "")
                else:
                    result["merged"] = True
                    if auto_resolved:
                        result["auto_resolved"] = True
                        result["action"] = (
                            f"merge 冲突已自动化解（abort + 分支预演合并），"
                            f"任务已 completed"
                        )
                    result["branch_deleted"] = try_delete_task_branch(
                        project_root, task_id
                    )

    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    return result
