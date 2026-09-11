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

import re
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
    task_branch_head,
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


def _branch_degraded_guard(task_id: str, branch_state: dict[str, Any]) -> dict[str, Any]:
    """把 try_git_branch 的 failed 状态转成一条 degraded_guard（task-git-branch-fail-report）。

    与 bind_task_wt 失败先例一致：claim 结果 JSON 通过 degraded_guards 显式暴露
    git 任务分支 checkout / 创建失败的原因（step + stderr 摘要），使 agent 可见可查，
    而非静默吞错后在下游 checkout 崩溃。
    """
    return {
        "guard": "try_git_branch",
        "task_id": task_id,
        "state": branch_state.get("state"),
        "step": branch_state.get("step"),
        "error": branch_state.get("error"),
        "hint": "git 任务分支 checkout/创建失败，claim 已降级继续；请据 error 排查仓库状态"
        "（分支冲突 / 脏工作区 / 仓库损坏），必要时先 git 修复再重试",
    }


# brief/acceptance_criteria 中常见文件路径模式（task-amend-scope-add 连带文件预警）。
_SCOPE_FILE_RE = re.compile(
    r"(?:orchd|tests|scripts|docs|rules|templates|design)/[\w./-]+\.(?:py|md|json|yaml|yml|txt)"
)


def build_scope_warning(task_def: dict[str, Any], project_root: Path | None = None) -> dict[str, Any] | None:
    """扫描 brief/acceptance_criteria 提及但未在 files_to_edit 声明的文件（task-amend-scope-add）。

    单一来源（task-fix-claim-scope-warning）：实认领路径 ``claim`` 与 CLI 预览路径
    ``claim_preview`` 共用本函数，消除原先两份重复实现。best-effort——任何异常一律
    返回 ``None``，不阻断认领/预览。

    Args:
        task_def: ``_master.json`` tasks[] 中的单个任务定义。
        project_root: 可选项目根路径，传入时定位主工作树并生成带绝对路径的
            amend 可执行命令（task-amend-guidance-mainwt）；为 None 时回退
            到无绝对路径的简短命令（向后兼容测试直调场景）。

    Returns:
        存在遗漏文件时返回含 ``type`` / ``message`` / ``missing_files`` / ``hint``
        的 scope_warning 字典；无遗漏或扫描失败时返回 ``None``。
    """
    try:
        declared_files = set(task_def.get("files_to_edit", []))
        text_to_scan = (task_def.get("brief", "") or "") + " " + " ".join(
            task_def.get("acceptance_criteria", []) or []
        )
        mentioned = set(_SCOPE_FILE_RE.findall(text_to_scan))
        missing = sorted(mentioned - declared_files)
        if not missing:
            return None
        task_id = task_def.get("id", "<id>")
        # task-amend-guidance-mainwt：定位主工作树，生成带绝对路径的可执行命令
        main_wt = None
        try:
            if project_root is not None:
                from orchd.gitops import main_worktree_root
                main_wt = str(main_worktree_root(project_root))
        except Exception:
            main_wt = None
        try:
            from orchd.guide import amend_mainwt_command, amend_patch_cmd
            if main_wt:
                patch_cmd = amend_mainwt_command(task_id, main_wt, files=missing[:3])
            else:
                patch_cmd = amend_patch_cmd(task_id, files=missing[:3], entry="orchd")
        except Exception:
            patch_cmd = "orchd amend --task <id> --files-to-edit <file>"
        verify_note = (
            "verify_command 变更走同一通道（--verify-command 覆写，白名单内不阻断）；"
            if main_wt else ""
        )
        return {
            "type": "files_to_edit_missing",
            "message": f"brief/acceptance 中提到 {len(missing)} 个文件未在 files_to_edit 中声明，实现时可能触发 E010",
            "missing_files": missing,
            "main_worktree": main_wt,
            "hint": ("若确需修改这些文件，认领后、动手前请回到主工作树补 "
                     "files_to_edit 声明并执行 amend（任务 worktree 不保留 "
                     "_master.json，唯一权威 = 主工作树）；"
                     f"{verify_note}可执行命令：{patch_cmd}"),
        }
    except Exception:
        return None  # best-effort，预警失败不阻断认领/预览


# ------------------------------------------------------------------
# claim（写操作，锁内）
# ------------------------------------------------------------------


def _claim_precheck(store, tasks, agent_id, task_id, role, project_root, review_type, enforce_self_review_block):
    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E005, f"task '{task_id}' not found in master", [{"task_id": task_id, "hint": f"任务 {task_id} 在 _master.json 中不存在，检查 id 是否拼写正确或是否已注册"}])
    session_id = resolve_session_identity(store.orchd_dir)["session_id"]
    if role is None:
        pre_state = store.replay()
        pre_ts = pre_state.get(task_id)
        role = "reviewer" if (pre_ts and pre_ts.status == "in_review") else "implementer"
    degraded_guards = []
    if role == "reviewer" and project_root:
        from orchd.worktree import _task_wt_name, detect_layout
        _layout = detect_layout(project_root)
        if _layout.get("layout") == "container":
            _wt_dir = _layout["task_wt_root"] / _task_wt_name(task_id)
            if not (_wt_dir / ".git").exists():
                branch_state = _try_git_branch(project_root, task_id)
                if branch_state and branch_state.get("state") == "failed":
                    degraded_guards.append(_branch_degraded_guard(task_id, branch_state))
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
            branch_state = _try_git_branch(project_root, task_id)
            if branch_state and branch_state.get("state") == "failed":
                degraded_guards.append(_branch_degraded_guard(task_id, branch_state))
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
            # task-master-single-copy：master 副本抑制现在是唯一权威机制（worktree
            # 不再保留 .orchd/_master.json），删除此前的双副本同步止血补丁——它方向
            # 与 canonical 权威矛盾（写本地、读 canonical），且已被 sparse-checkout
            # 抑制取代。副本抑制失败会经 ensure_task_wt 的 master_suppression 段
            # 随 wt_info 透出，此处转成 degraded_guard 供 agent 可见（禁止静默）。
            supp = wt_info.get("master_suppression") or {}
            if supp.get("ok") is False:
                degraded_guards.append({
                    "guard": "master_suppression",
                    "task_id": task_id,
                    "method": supp.get("method"),
                    "error": supp.get("reason"),
                    "hint": ".orchd/_master.json 副本未抑制：请在主工作树手工清理残留副本后再 claim",
                })
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
            if ts and ts.review_claimed_by:
                raise OrchdError(ErrorCode.E009, f"already_claimed by {ts.review_claimed_by}", [{"task_id": task_id, "claimed_by": ts.review_claimed_by, "review_claimed_by": ts.review_claimed_by, "hint": f"任务已被 {ts.review_claimed_by} 认领为 reviewer，等待其完成或由其 retract 后重试，禁止重复 claim"}])
            if status != "in_review":
                raise OrchdError(ErrorCode.E008, f"task_not_in_review: '{task_id}' status={status} review_phase={ts.review_phase if ts else None}", [{"task_id": task_id, "current_status": status, "review_phase": ts.review_phase if ts else None, "hint": f"任务未进入审查（当前 {status}），需 in_review 且 review_phase={ts.review_phase if ts else 'spec'} 再 claim"}])
            cur_phase = (ts.review_phase if ts else None) or "spec"
            if review_type and review_type != cur_phase:
                raise OrchdError(ErrorCode.E007, f"phase_mismatch {cur_phase}", [{"task_id": task_id}])
            designated = task_def.get("reviewers", [])
            if agent_id not in designated and not _is_fingerprint_agent_id(agent_id):
                raise OrchdError(ErrorCode.E007, f"not_designated_reviewer: '{agent_id}' 不在任务 '{task_id}' 的 reviewers 名单中", [{"task_id": task_id, "agent": agent_id, "reviewers": designated, "hint": "请使用名单内的 agent ID"}])
            if ts and ts.claimed_by == agent_id and ts.review_claimed_by and ts.review_claimed_by != agent_id:
                raise OrchdError(ErrorCode.E011, "review_hijack", [{"task_id": task_id}])
            done_author, _ = _extract_last_done(store, task_id, derived)
            done_event = _find_last_done_event(store, task_id, derived)
            done_session = done_event.get("session_id") if done_event else None
            is_self = bool(done_author and ((done_session == session_id and done_author == agent_id) if done_session and session_id else done_author == agent_id))
            if is_self and enforce_self_review_block:
                raise OrchdError(ErrorCode.E016, "self_review", [{"task_id": task_id, "done_by": done_author}])
            if is_self:
                is_self_review = True
        else:
            if ts and ts.claimed_by:
                other = not (ts.claimed_session == session_id and ts.claimed_by == agent_id) if ts.claimed_session and session_id else ts.claimed_by != agent_id
                if other:
                    raise OrchdError(ErrorCode.E009, f"already_claimed by {ts.claimed_by}", [{"task_id": task_id, "claimed_by": ts.claimed_by, "claimed_session": ts.claimed_session, "hint": f"任务已被 {ts.claimed_by} 认领，等待其完成或由其 retract 后重试，禁止重复 claim"}])
            if status != "pending":
                raise OrchdError(ErrorCode.E008, f"task_not_pending: '{task_id}' status={status}", [{"task_id": task_id, "current_status": status, "hint": f"任务未就绪（当前 {status}），需 pending 再 claim；若被他人 claimed 已在上一步 E009 中提示"}])
            for dep_id in task_def.get("depends_on", []):
                dep_ts = state.get(dep_id)
                if (dep_ts.status if dep_ts else "pending") not in ("completed", "cancelled"):
                    raise OrchdError(ErrorCode.E008, f"dependency_not_met: '{task_id}' blocked_by {dep_id} status={dep_ts.status if dep_ts else 'pending'}", [{"task_id": task_id, "blocked_by": dep_id, "blocked_status": dep_ts.status if dep_ts else "pending", "hint": f"依赖 {dep_id} 未完成（当前 {dep_ts.status if dep_ts else 'pending'}），需等待其 completed 后重试"}])
            conflicts = detect_file_conflict(state, tasks, task_def)
            if conflicts:
                try:
                    from orchd.guide import amend_patch_cmd as _amend_cmd2
                    _patch = _amend_cmd2(task_id, files=["<file>"], entry="orchd")
                except Exception:
                    _patch = "orchd amend --task <id> --files-to-edit <file>"
                raise OrchdError(ErrorCode.E010, "conflict", [
                    {"task_id": c.task_id} for c in conflicts
                ] + [{
                    "hint": ("与在途任务声明文件冲突：等其完成、或用 depends_on 串行化；"
                             "若本任务确需新增文件，先在主工作树补声明："
                             f"{_patch}"),
                }])
            if project_root:
                from orchd.worktree import actual_changes_conflict
                ac = actual_changes_conflict(project_root, state, tasks, task_def)
                if ac:
                    try:
                        from orchd.guide import amend_patch_cmd as _amend_cmd3
                        _patch_ac = _amend_cmd3(task_id, files=["<file>"], entry="orchd")
                    except Exception:
                        _patch_ac = "orchd amend --task <id> --files-to-edit <file>"
                    raise OrchdError(ErrorCode.E010, "actual changes", ac + [{
                        "hint": ("分支实际改动与在途任务冲突；若改动确属本任务，"
                                 "先在主工作树补声明后重试："
                                 f"{_patch_ac}"),
                    }])
        for tid, t_state in state.items():
            def _owns(h, hs):
                return (hs == session_id and h == agent_id) if hs and session_id else bool(h and h == agent_id)

            if t_state.status in ("claimed", "done", "in_review") and _owns(t_state.claimed_by, t_state.claimed_session) and (tid != task_id or enforce_self_review_block):
                raise OrchdError(ErrorCode.E011, f"agent_busy {tid}", [{"agent_id": agent_id, "blocking_task": tid, "blocking_status": t_state.status, "hint": "任务完成审查（completed/cancelled）或被打回（pending）后才可领取新任务"}])
            if t_state.status == "in_review" and _owns(t_state.review_claimed_by, t_state.review_claimed_session):
                rid = ""
                for ev in store._read_ledger_lines(from_line=1):
                    if ev.get("task_id") == tid and ev.get("type") == "REVIEW_CLAIMED" and ev.get("agent_id") == agent_id:
                        rid = ev.get("event_id", "")
                raise OrchdError(ErrorCode.E011, f"review busy {tid}", [{"agent_id": agent_id, "blocking_task": tid, "review_claim_event_id": rid, "hint": "retract" if rid else "no event"}])
        files_claimed = task_def.get("files_to_edit", [])
        if role == "reviewer":
            rp = ts.review_phase if ts else None
            # AC1（task-review-baseline-and-worktree-recycle-fix）：审查基线取**任务
            # 分支 tip**。原实现取 get_head_commit(project_root)，container 布局下
            # project_root 解析为主工作树 → 基线恒为 main HEAD，而提交期 current_sha
            # 取任务 worktree HEAD，二者必然不等 → 漂移检测恒误报（基线校验失效）。
            bs = None
            if project_root:
                bs = task_branch_head(project_root, task_id) or get_head_commit(project_root)
            event = _make_event(task_id, agent_id, "REVIEW_CLAIMED", review_type=rp, baseline_sha=bs) if rp else _make_event(task_id, agent_id, "REVIEW_CLAIMED", baseline_sha=bs)
        else:
            event = _make_event(task_id, agent_id, "CLAIMED", role=role, files_claimed=files_claimed)
        store.append_event(event)
        new_state = store.replay()
        store.update_checkpoint(new_state)
        return event, state, derived, integrity_warnings, is_self_review
    finally:
        store.release_lock()

def _resolve_review_worktree(project_root, task_id):
    """审查分支 diff 诊断的作用域根（AC2）：按布局解析到任务 worktree。

    container 布局下审查认领由主工作树发起，``project_root`` 即主工作树 →
    ``is_task_worktree`` 恒 False → 诊断恒降级（``not worktree``）。本函数把
    作用域解析到 ``<task_wt_root>/task-<id>``；本身就是任务 worktree 则原样
    返回；解析不到（flat / 无独立 worktree）回退 ``project_root``，维持既有
    降级语义（best-effort，绝不抛异常）。
    """
    if project_root is None:
        return project_root
    try:
        if is_task_worktree(project_root):
            return project_root
        from orchd.worktree import _task_wt_name, detect_layout

        layout = detect_layout(Path(project_root))
        if layout.get("layout") == "container":
            cand = Path(layout["task_wt_root"]) / _task_wt_name(task_id)
            if (cand / ".git").exists():
                return cand
    except Exception:
        return project_root
    return project_root


def _claim_review_branch(store, task_id, task_def, project_root, role, derived, review_phase, is_self_review, event, degraded_guards, shared=None):
    if role != "reviewer":
        return None
    files_to_review = [{"path": p, "priority": "must_read"} for p in task_def.get("files_to_edit", [])]
    if shared:
        if review_phase == "spec":
            arch = shared.get("architecture")
            if arch:
                files_to_review.append({"path": arch, "priority": "reference", "hint": "arch"})
        elif review_phase == "code":
            conv = shared.get("conventions")
            if conv:
                files_to_review.append({"path": conv, "priority": "must_read", "hint": "conv"})
        else:
            arch = shared.get("architecture")
            if arch:
                files_to_review.append({"path": arch, "priority": "reference", "hint": "arch"})
            conv = shared.get("conventions")
            if conv:
                files_to_review.append({"path": conv, "priority": "must_read", "hint": "conv"})
    done_event = _find_last_done_event(store, task_id, derived)
    changes_description = done_event.get("changes_description") if done_event else None
    result = {"claimed": True, "task_id": task_id, "review_type": review_phase, "files_to_review": files_to_review, "acceptance_criteria": task_def.get("acceptance_criteria", []), "changes_description": changes_description, "review_comments": _extract_review_comments(store, task_id, derived), "event_id": event["event_id"]}
    if is_self_review:
        _done_by = done_event.get("agent_id") if done_event else None
        result["self_review_notice"] = {"message": "self_review", "hint": "enforce flag", "done_by": _done_by, "enforce_self_review_block": False}
    if done_event and done_event.get("verify"):
        result["verify"] = done_event["verify"]
    review_degraded = []

    def _diag():
        if project_root is None:
            raise NotApplicableError("no root")
        exists = branch_exists(project_root, f"task/{task_id}")
        if exists is None:
            raise RuntimeError("git fail")
        if not exists:
            raise NotApplicableError("no branch")
        # AC2（task-review-baseline-and-worktree-recycle-fix）：container 布局下
        # 审查认领由主工作树发起，project_root 即主工作树 → is_task_worktree 恒
        # False → 诊断恒降级 not worktree（branch_files / missing_declared_files
        # 恒为 null）。先按布局把诊断作用域解析到任务 worktree 再判定。
        diag_root = _resolve_review_worktree(project_root, task_id)
        if not is_task_worktree(diag_root):
            raise NotApplicableError("not worktree")
        return {"branch_files": task_branch_files(diag_root, task_id), "missing_declared_files": diagnose_missing_branch_files(diag_root, task_id, task_def.get("files_to_edit", []))}

    diag = run_guard(_diag, guard_name="review_branch_diff_diagnosis", on_error=GUARD_WARN, fallback=None, context={"task_id": task_id}, hint="diag fail", degraded=review_degraded)
    if diag is None:
        result["branch_files"] = None
        result["missing_declared_files"] = None
    else:
        result["branch_files"] = diag["branch_files"]
        result["missing_declared_files"] = diag["missing_declared_files"]
    if review_degraded or degraded_guards:
        result["degraded_guards"] = degraded_guards + review_degraded
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
    # task-amend-scope-add：claim 连带文件预警（与 claim_preview 共用 build_scope_warning 单一来源）
    scope_warning = build_scope_warning(task_def, project_root=project_root)
    result = {"claimed": True, "task": task_def, "files_to_read": files_to_read, "files_to_edit": task_def.get("files_to_edit", []), "review_comments": _extract_review_comments(store, task_id, derived), "previous_changes": previous_changes, "branch": f"task/{task_id}", "pending_conflicts": pending_conflicts, "event_id": event["event_id"]}
    if scope_warning:
        result["scope_warning"] = scope_warning
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
