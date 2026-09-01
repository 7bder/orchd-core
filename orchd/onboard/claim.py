"""Orchd 任务生命周期管理 - claim 域。

迁移自 orchd/onboard.py（task-split-onboard-claim）：
  - _is_high_risk: 高风险领域判定
  - _extract_previous_changes: 从 ledger 提取最近一次 DONE 的 changes
  - _claim_precheck: claim 预校验（无锁快速失败）
  - _claim_setup_worktree: 创建/绑定任务 worktree
  - _claim_write_event: 锁内写 CLAIMED/REVIEW_CLAIMED 事件
  - _claim_review_branch: reviewer claim 时的分支诊断
  - claim: 认领任务主入口（锁内 check-then-act）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops import (
    GUARD_FAIL_CLOSED,
    GUARD_WARN,
    branch_exists,
    get_head_commit,
    guard_claim as _guard_claim,
    hook_install,
    is_task_worktree,
    release_session_lock_if_owned,
    run_guard,
)
from orchd.gitops_ops import make_event as _make_event, try_git_branch as _try_git_branch
from orchd.ledger import (
    Store,
    TaskDerived,
    is_fingerprint_agent_id as _is_fingerprint_agent_id,
    resolve_session_identity,
    resolve_store_dir,
)
from orchd.pool import detect_file_conflict
from orchd.review import (
    extract_last_done as _extract_last_done,
    extract_review_comments as _extract_review_comments,
    find_last_done_event as _find_last_done_event,
)
from orchd.worktree import (
    _task_wt_name,
    actual_changes_conflict,
    bind_task_wt,
    detect_layout,
    diagnose_missing_branch_files,
    ensure_task_wt,
    task_branch_files,
)

# 同包辅助（bootstrap 域迁移后 _is_high_risk 应在 claim 域，因为仅 claim 调用）
# 注：_is_high_risk 和 _extract_previous_changes 随 claim 域整体迁移


def _is_high_risk(task_def: dict[str, Any]) -> bool:
    """高风险领域判定：任务触碰引擎核心（状态机分支 / CLI 契约 / 锁协议）。

    用于「共享上下文按需」：这类任务的实现默认自动附 conventions.md
    （编码规范 + 自检约定），降低越界/违规概率；architecture.md 不自动附，
    仅任务自身 files_to_read 显式引用或 --with-context 显式开启时提供。

    双态兼容（task-12-engine-path-abstraction，AC4）：files_to_edit 命中
    ``orchd/``（开发态根布局）或 ``.orchd/orchd/``（发布态自包含 .orchd 布局）
    均判高风险；``.orchd/_master.json`` 固定资产同样高风险。
    """
    module = task_def.get("module", "")
    if module == "mod-core":
        return True
    for f in task_def.get("files_to_edit", []):
        if (
            f.startswith("orchd/")
            or f.startswith(".orchd/orchd/")
            or f == ".orchd/_master.json"
        ):
            return True
    return False


def _extract_previous_changes(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> str | None:
    """从 ledger 中提取该任务最近一次 DONE 的 changes_description。"""
    return _extract_last_done(store, task_id, derived)[1]


# ------------------------------------------------------------------
# claim（写操作，锁内）
# ------------------------------------------------------------------


def _claim_precheck(store, tasks, agent_id, task_id, role, project_root, review_type, enforce_self_review_block):
    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E005, f"task '{task_id}' not found in master", [{"task_id": task_id, "hint": f"任务 {task_id} 在 _master.json 中不存在，检查 id 是否拼写正确或是否已注册"}])
    from orchd.ledger import resolve_session_identity
    session_id = resolve_session_identity(store.orchd_dir)["session_id"]
    if role is None:
        pre_state = store.replay()
        pre_ts = pre_state.get(task_id)
        role = "reviewer" if (pre_ts and pre_ts.status == "in_review") else "implementer"
    if role == "reviewer" and project_root:
        from orchd.worktree import _task_wt_name, detect_layout
        _layout = detect_layout(project_root)
        if _layout.get("layout") == "container":
            _wt_dir = _layout["task_wt_root"] / _task_wt_name(task_id)
            if not (_wt_dir / ".git").exists():
                _try_git_branch(project_root, task_id)
    degraded_guards = []
    _guard_claim(project_root, role=role, task_id=task_id, orchd_dir=store.orchd_dir, agent_id=agent_id, degraded=degraded_guards)
    return task_def, role, session_id, degraded_guards


def _claim_setup_worktree(project_root, task_id, task_def, store, degraded_guards):
    worktree_path = None
    degraded_warning = None
    if project_root:
        from orchd.worktree import bind_task_wt, ensure_task_wt
        wt_info = ensure_task_wt(project_root, task_id)
        if wt_info.get("worktree") is not None:
            worktree_path = str(wt_info["worktree"])
        if not wt_info.get("separate"):
            _try_git_branch(project_root, task_id)
        if wt_info.get("degraded"):
            degraded_warning = f"worktree degraded: {wt_info.get('reason')}"
        files_to_edit = task_def.get("files_to_edit", [])
        if files_to_edit:
            hook_install(project_root, task_id, files_to_edit, exempt_files=task_def.get("exempt_files"))
        if worktree_path is not None:
            try:
                bind_task_wt(resolve_store_dir(store.orchd_dir), task_id, worktree_path)
            except Exception as exc:
                degraded_guards.append({"guard": "bind_task_wt", "task_id": task_id, "error": str(exc), "hint": "binding failed, degraded"})
    return worktree_path, degraded_warning


def _claim_write_event(store, tasks, agent_id, task_id, task_def, role, session_id, review_type, enforce_self_review_block, project_root):
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        derived = store.scan_task_derived()
        ts = state.get(task_id)
        status = ts.status if ts else "pending"
        is_self_review = False
        if role == "reviewer":
            if ts and ts.review_claimed_by: raise OrchdError(ErrorCode.E009, f"already_claimed by {ts.review_claimed_by}", [{"task_id": task_id, "claimed_by": ts.review_claimed_by, "review_claimed_by": ts.review_claimed_by, "hint": f"任务已被 {ts.review_claimed_by} 认领为 reviewer，等待其完成或由其 retract 后重试，禁止重复 claim"}])
            if status != "in_review": raise OrchdError(ErrorCode.E008, f"task_not_in_review: '{task_id}' status={status} review_phase={ts.review_phase if ts else None}", [{"task_id": task_id, "current_status": status, "review_phase": ts.review_phase if ts else None, "hint": f"任务未进入审查（当前 {status}），需 in_review 且 review_phase={ts.review_phase if ts else 'spec'} 再 claim"}])
            cur_phase = (ts.review_phase if ts else None) or "spec"
            if review_type and review_type != cur_phase: raise OrchdError(ErrorCode.E007, f"phase_mismatch {cur_phase}", [{"task_id": task_id}])
            designated = task_def.get("reviewers", [])
            if agent_id not in designated and not _is_fingerprint_agent_id(agent_id): raise OrchdError(ErrorCode.E007, f"not_designated_reviewer: '{agent_id}' 不在任务 '{task_id}' 的 reviewers 名单中", [{"task_id": task_id, "agent": agent_id, "reviewers": designated, "hint": "请使用名单内的 agent ID"}])
            if ts and ts.claimed_by == agent_id and ts.review_claimed_by and ts.review_claimed_by != agent_id: raise OrchdError(ErrorCode.E011, "review_hijack", [{"task_id": task_id}])
            done_author,_ = _extract_last_done(store, task_id, derived)
            done_event = _find_last_done_event(store, task_id, derived)
            done_session = done_event.get("session_id") if done_event else None
            is_self = bool(done_author and ((done_session==session_id and done_author==agent_id) if done_session and session_id else done_author==agent_id))
            if is_self and enforce_self_review_block: raise OrchdError(ErrorCode.E016, "self_review", [{"task_id": task_id, "done_by": done_author}])
            if is_self: is_self_review=True
        else:
            if ts and ts.claimed_by:
                other = not (ts.claimed_session==session_id and ts.claimed_by==agent_id) if ts.claimed_session and session_id else ts.claimed_by!=agent_id
                if other: raise OrchdError(ErrorCode.E009, f"already_claimed by {ts.claimed_by}", [{"task_id": task_id, "claimed_by": ts.claimed_by, "claimed_session": ts.claimed_session, "hint": f"任务已被 {ts.claimed_by} 认领，等待其完成或由其 retract 后重试，禁止重复 claim"}])
            if status != "pending": raise OrchdError(ErrorCode.E008, f"task_not_pending: '{task_id}' status={status}", [{"task_id": task_id, "current_status": status, "hint": f"任务未就绪（当前 {status}），需 pending 再 claim；若被他人 claimed 已在上一步 E009 中提示"}])
            for dep_id in task_def.get("depends_on", []):
                dep_ts = state.get(dep_id)
                if (dep_ts.status if dep_ts else "pending") not in ("completed","cancelled"): raise OrchdError(ErrorCode.E008, f"dependency_not_met: '{task_id}' blocked_by {dep_id} status={dep_ts.status if dep_ts else 'pending'}", [{"task_id": task_id, "blocked_by": dep_id, "blocked_status": dep_ts.status if dep_ts else "pending", "hint": f"依赖 {dep_id} 未完成（当前 {dep_ts.status if dep_ts else 'pending'}），需等待其 completed 后重试"}])
            conflicts = detect_file_conflict(state, tasks, task_def)
            if conflicts: raise OrchdError(ErrorCode.E010, "conflict", [{"task_id": c.task_id} for c in conflicts])
            if project_root:
                from orchd.worktree import actual_changes_conflict
                ac = actual_changes_conflict(project_root, state, tasks, task_def)
                if ac: raise OrchdError(ErrorCode.E010, "actual changes", ac)
        for tid,t_state in state.items():
            def _owns(h, hs): return (hs==session_id and h==agent_id) if hs and session_id else bool(h and h==agent_id)
            if t_state.status in ("claimed","done","in_review") and _owns(t_state.claimed_by, t_state.claimed_session) and (tid!=task_id or enforce_self_review_block):
                raise OrchdError(ErrorCode.E011, f"agent_busy {tid}", [{"agent_id": agent_id, "blocking_task": tid, "blocking_status": t_state.status, "hint": "任务完成审查（completed/cancelled）或被打回（pending）后才可领取新任务"}])
            if t_state.status=="in_review" and _owns(t_state.review_claimed_by, t_state.review_claimed_session):
                rid=""
                for ev in store._read_ledger_lines(from_line=1):
                    if ev.get("task_id")==tid and ev.get("type")=="REVIEW_CLAIMED" and ev.get("agent_id")==agent_id: rid=ev.get("event_id","")
                raise OrchdError(ErrorCode.E011, f"review busy {tid}", [{"agent_id": agent_id, "blocking_task": tid, "review_claim_event_id": rid, "hint": "retract" if rid else "no event"}])
        files_claimed = task_def.get("files_to_edit", [])
        if role=="reviewer":
            rp = ts.review_phase if ts else None
            bs = get_head_commit(project_root) if project_root else None
            event=_make_event(task_id, agent_id, "REVIEW_CLAIMED", review_type=rp, baseline_sha=bs) if rp else _make_event(task_id, agent_id, "REVIEW_CLAIMED", baseline_sha=bs)
        else:
            event=_make_event(task_id, agent_id, "CLAIMED", role=role, files_claimed=files_claimed)
        store.append_event(event)
        new_state=store.replay()
        store.update_checkpoint(new_state)
        return event, state, derived, integrity_warnings, is_self_review
    finally:
        store.release_lock()

def _claim_review_branch(store, task_id, task_def, project_root, role, derived, review_phase, is_self_review, event, degraded_guards, shared=None):
    if role != "reviewer":
        return None
    files_to_review = [{"path": p, "priority": "must_read"} for p in task_def.get("files_to_edit", [])]
    if shared:
        if review_phase == "spec":
            arch = shared.get("architecture")
            if arch: files_to_review.append({"path": arch, "priority": "reference", "hint": "arch"})
        elif review_phase == "code":
            conv = shared.get("conventions")
            if conv: files_to_review.append({"path": conv, "priority": "must_read", "hint": "conv"})
        else:
            arch = shared.get("architecture")
            if arch: files_to_review.append({"path": arch, "priority": "reference", "hint": "arch"})
            conv = shared.get("conventions")
            if conv: files_to_review.append({"path": conv, "priority": "must_read", "hint": "conv"})
    done_event = _find_last_done_event(store, task_id, derived)
    changes_description = done_event.get("changes_description") if done_event else None
    result = {"claimed": True, "task_id": task_id, "review_type": review_phase, "files_to_review": files_to_review, "acceptance_criteria": task_def.get("acceptance_criteria", []), "changes_description": changes_description, "review_comments": _extract_review_comments(store, task_id, derived), "event_id": event["event_id"]}
    if is_self_review:
        _done_by = done_event.get("agent_id") if done_event else None
        result["self_review_notice"] = {"message": "self_review", "hint": "enforce flag", "done_by": _done_by, "enforce_self_review_block": False}
    if done_event and done_event.get("verify"):
        result["verify"] = done_event["verify"]
    review_degraded=[]
    def _diag():
        if project_root is None: raise NotApplicableError("no root")
        exists=branch_exists(project_root, f"task/{task_id}")
        if exists is None: raise RuntimeError("git fail")
        if not exists: raise NotApplicableError("no branch")
        if not is_task_worktree(project_root): raise NotApplicableError("not worktree")
        from orchd.worktree import diagnose_missing_branch_files, task_branch_files
        return {"branch_files": task_branch_files(project_root, task_id), "missing_declared_files": diagnose_missing_branch_files(project_root, task_id, task_def.get("files_to_edit", []))}
    diag=run_guard(_diag, guard_name="review_branch_diff_diagnosis", on_error=GUARD_WARN, fallback=None, context={"task_id": task_id}, hint="diag fail", degraded=review_degraded)
    if diag is None:
        result["branch_files"]=None
        result["missing_declared_files"]=None
    else:
        result["branch_files"]=diag["branch_files"]
        result["missing_declared_files"]=diag["missing_declared_files"]
    if review_degraded or degraded_guards:
        result["degraded_guards"]=degraded_guards+review_degraded
    return result
def claim(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str | None = None,
    project_root: Path | None = None,
    shared: dict[str, Any] | None = None,
    review_type: str | None = None,
    with_context: bool = False,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    task_def, role, session_id, degraded_guards = _claim_precheck(store, tasks, agent_id, task_id, role, project_root, review_type, enforce_self_review_block)
    event, state, derived, integrity_warnings, is_self_review = _claim_write_event(store, tasks, agent_id, task_id, task_def, role, session_id, review_type, enforce_self_review_block, project_root)
    worktree_path = None
    degraded_warning = None
    if role == "implementer" and project_root:
        worktree_path, degraded_warning = _claim_setup_worktree(project_root, task_id, task_def, store, degraded_guards)
    review_phase = (store.replay().get(task_id).review_phase if store.replay().get(task_id) else None)
    if role == "reviewer":
        rb = _claim_review_branch(store, task_id, task_def, project_root, role, derived, review_phase, is_self_review, event, degraded_guards, shared)
        if rb is not None:
            return rb
    files_to_read = list(task_def.get("files_to_read", []))
    if shared:
        if with_context:
            for key in ("architecture", "conventions"):
                path = shared.get(key)
                if path:
                    files_to_read.append({"path": path, "priority": "reference", "hint": "shared"})
        elif _is_high_risk(task_def):
            conv = shared.get("conventions")
            if conv:
                files_to_read.append({"path": conv, "priority": "reference", "hint": "high risk"})
    previous_changes = _extract_previous_changes(store, task_id, derived)
    pending_conflicts = [{"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by} for c in detect_file_conflict(state, tasks, task_def, include_pending=True) if c.claimed_by == "pending"]
    result = {"claimed": True, "task": task_def, "files_to_read": files_to_read, "files_to_edit": task_def.get("files_to_edit", []), "review_comments": _extract_review_comments(store, task_id, derived), "previous_changes": previous_changes, "branch": f"task/{task_id}", "pending_conflicts": pending_conflicts, "event_id": event["event_id"]}
    if role == "implementer" and worktree_path is not None:
        result["worktree_path"] = worktree_path
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if degraded_warning:
        result["degraded_warning"] = degraded_warning
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    if role == "implementer" and project_root:
        from orchd.worktree import detect_layout
        layout = detect_layout(project_root)
        if layout.get("layout") == "container":
            result["session_lock_released"] = release_session_lock_if_owned(project_root / ".orchd", agent_id).get("released", False)
    return result
