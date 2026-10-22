"""claim 域 —— 认领执行（锁内写事件 + worktree 就绪）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`_claim_write_event`：锁内 check-then-act——状态机校验（E008/E009/E010/E011/
  E016/E007 冷却）后写 CLAIMED / REVIEW_CLAIMED 事件并推进 checkpoint。
- :func:`_claim_setup_worktree`：创建/绑定任务 worktree（nogit 降级 / container /
  master 副本抑制 / lessons 迁移，失败一律 degraded_guards 留痕）。

依赖：precheck（冷却检查与 branch 降级守卫）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_head_commit, hook_install
from orchd.gitops_ops import make_event as _make_event, try_git_branch as _try_git_branch
from orchd.ledger import (
    Store,
    TaskDerived,
    TaskState,
    is_fingerprint_agent_id as _is_fingerprint_agent_id,
    resolve_store_dir,
)
from orchd.pool import detect_file_conflict
from orchd.review import (
    extract_last_done as _extract_last_done,
    find_last_done_event as _find_last_done_event,
    is_self_review_author as _is_self_review_author,
)
from orchd.worktree import (
    bind_task_wt,
    task_branch_head,
)

from .precheck import (
    _RETRACT_COOLDOWN_S,
    _branch_degraded_guard,
    _check_retract_cooldown,
)


def _claim_setup_worktree(
    project_root: Path | None,
    task_id: str,
    task_def: dict[str, Any],
    store: Store,
    degraded_guards: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    worktree_path = None
    degraded_warning = None
    if project_root:
        from orchd.nogit import git_available, take_snapshot

        if not git_available(project_root):
            # 单目录无 git（task-nogit-single-dir-pivot）：不建 sibling 拷贝，
            # 在项目主目录建立任务基线快照，并绑定任务到主目录（flat-git 同口径）。
            # 基线建立失败仅降级留痕（越界门禁按无基线处理，不阻断认领）。
            from orchd.worktree import bind_task_wt

            try:
                take_snapshot(Path(project_root), task_id, "base")
            except Exception as exc:
                degraded_guards.append({
                    "guard": "nogit_base_snapshot", "task_id": task_id,
                    "error": str(exc), "hint": "基线快照建立失败，已降级留痕",
                })
            worktree_path = str(Path(project_root).resolve())
            try:
                bind_task_wt(resolve_store_dir(store.orchd_dir), task_id, Path(worktree_path))
            except Exception as exc:  # pragma: no cover - 绑定失败降级留痕
                degraded_guards.append({
                    "guard": "bind_task_wt", "task_id": task_id,
                    "error": str(exc), "hint": "binding failed, degraded",
                })
            return worktree_path, degraded_warning
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
            hook_install(
                project_root, task_id, files_to_edit,
                exempt_files=task_def.get("exempt_files"))
        # pass13 §11-9 甲案：旧布局经验库迁移接进 claim 的「工作树就绪」段。此前
        # ``migrate_lessons_to_runtime`` **零生产调用者**（全仓只命中 doctor.py 的提示
        # 文本与 tests），container 重定向后旧布局 lessons*.jsonl 成孤儿 ⇒ lookup 恒 0、
        # guidance 注入空转。函数本体已具备安全前提：幂等（目标已存在不覆盖、保留行序）、
        # 只在账本根重定向生效时扫描（flat 返回 reason=flat_no_redirect 零操作）、持
        # lessons.lock 且拿不到锁 reason=lock_busy 绝不清阻塞——故可放热路径。
        # fail-open：迁移异常一律经 degraded_guards 留痕（与同段 bind_task_wt /
        # master_suppression 同形），绝不因观测性动作阻塞认领，也不得静默吞掉。
        try:
            from orchd.migrate import migrate_lessons_to_runtime

            migrate_lessons_to_runtime(store.orchd_dir)
        except Exception as exc:
            degraded_guards.append({
                "guard": "lessons_migrate",
                "task_id": task_id,
                "error": str(exc),
                "hint": "旧布局经验库迁移失败，已降级留痕（不阻塞 claim；可手工运行 "
                        "python -c \"from orchd.migrate import migrate_lessons_to_runtime; "
                        "migrate_lessons_to_runtime('.orchd')\"）",
            })
        if worktree_path is not None:
            try:
                bind_task_wt(resolve_store_dir(store.orchd_dir), task_id, Path(worktree_path))
            except Exception as exc:
                degraded_guards.append({
                    "guard": "bind_task_wt", "task_id": task_id,
                    "error": str(exc), "hint": "binding failed, degraded",
                })
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
                    "hint": (".orchd/_master.json 副本未抑制："
                             "请在主工作树手工清理残留副本后再 claim"),
                })
    return worktree_path, degraded_warning


def _claim_write_event(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    task_def: dict[str, Any],
    role: str,
    session_id: str | None,
    review_type: str | None,
    enforce_self_review_block: bool,
    project_root: Path | None,
    force: bool = False,
) -> tuple[dict[str, Any], dict[str, TaskState], TaskDerived, list[dict[str, Any]], bool]:
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
                raise OrchdError(
                    ErrorCode.E009, f"already_claimed by {ts.review_claimed_by}",
                    [{"task_id": task_id, "claimed_by": ts.review_claimed_by,
                      "review_claimed_by": ts.review_claimed_by,
                      "hint": f"任务已被 {ts.review_claimed_by} 认领为 reviewer，"
                              "等待其完成或由其 retract 后重试，禁止重复 claim"}])
            if status != "in_review":
                raise OrchdError(
                    ErrorCode.E008,
                    f"task_not_in_review: '{task_id}' status={status} "
                    f"review_phase={ts.review_phase if ts else None}",
                    [{"task_id": task_id, "current_status": status,
                      "review_phase": ts.review_phase if ts else None,
                      "hint": f"任务未进入审查（当前 {status}），需 in_review 且 "
                              f"review_phase={ts.review_phase if ts else 'spec'} "
                              "再 claim"}])
            cur_phase = (ts.review_phase if ts else None) or "spec"
            if review_type and review_type != cur_phase:
                raise OrchdError(
                    ErrorCode.E007, f"phase_mismatch {cur_phase}",
                    [{"task_id": task_id, "expected_phase": cur_phase,
                      "got_phase": review_type, "current_status": status,
                      "hint": f"审查阶段不匹配：任务处于 {cur_phase} 阶段，"
                              f"你指定的是 {review_type}（unified 单阶段须省略 "
                              "--type；文档类单阶段直接进 code）。当前状态 "
                              f"{status}，需 in_review 再 claim"}])
            designated = task_def.get("reviewers", [])
            if agent_id not in designated and not _is_fingerprint_agent_id(agent_id):
                raise OrchdError(
                    ErrorCode.E007,
                    f"not_designated_reviewer: '{agent_id}' 不在任务 "
                    f"'{task_id}' 的 reviewers 名单中",
                    [{"task_id": task_id, "agent": agent_id,
                      "reviewers": designated,
                      "hint": "请使用名单内的 agent ID"}])
            if (ts and ts.claimed_by == agent_id and ts.review_claimed_by
                    and ts.review_claimed_by != agent_id):
                raise OrchdError(ErrorCode.E011, "review_hijack", [{"task_id": task_id}])
            done_author, _ = _extract_last_done(store, task_id, derived)
            # v4（2026-09-15 停服升级）：判定收敛到 is_self_review_author（单一事实源），
            # 与原内联三元表达式严格等价——行为不变，仅消除三处私有副本的漂移风险。
            is_self = _is_self_review_author(
                _find_last_done_event(store, task_id, derived), agent_id, session_id
            )
            # task-review-independence-enforce：任务级独立审查与全局开关 OR——
            # 任一为 true 即 E016 硬阻断；缺省两者皆 false 保持仅标注行为不变。
            _require_independent = bool(task_def.get("require_independent_review", False))
            if is_self and (enforce_self_review_block or _require_independent):
                raise OrchdError(
                    ErrorCode.E016, "self_review",
                    [{"task_id": task_id, "done_by": done_author}])
            if is_self:
                is_self_review = True
        else:
            if ts and ts.claimed_by:
                other = (not (ts.claimed_session == session_id
                              and ts.claimed_by == agent_id)
                         if ts.claimed_session and session_id
                         else ts.claimed_by != agent_id)
                if other:
                    raise OrchdError(
                        ErrorCode.E009, f"already_claimed by {ts.claimed_by}",
                        [{"task_id": task_id, "claimed_by": ts.claimed_by,
                          "claimed_session": ts.claimed_session,
                          "hint": f"任务已被 {ts.claimed_by} 认领，等待其完成或由其 "
                                  "retract 后重试，禁止重复 claim"}])
            if status != "pending":
                raise OrchdError(
                    ErrorCode.E008, f"task_not_pending: '{task_id}' status={status}",
                    [{"task_id": task_id, "current_status": status,
                      "hint": f"任务未就绪（当前 {status}），需 pending 再 claim；"
                              "若被他人 claimed 已在上一步 E009 中提示"}])
            # task-retract-bind-cooloff：认领冷却保护
            if not force:
                _cooldown = _check_retract_cooldown(store, task_id)
                if _cooldown is not None:
                    raise OrchdError(
                        ErrorCode.E007,
                        f"retract_cooldown: task '{task_id}' was retracted "
                        f"{_cooldown['seconds_ago']:.0f}s ago "
                        f"(disposition={_cooldown['disposition']}), "
                        f"cooldown {_RETRACT_COOLDOWN_S}s not elapsed",
                        [{"task_id": task_id, "retracted_at": _cooldown["retracted_at"],
                          "seconds_ago": _cooldown["seconds_ago"],
                          "disposition": _cooldown["disposition"],
                          "hint": "任务刚被撤回，疑似主动放弃；等待冷却或使用 --force 显式绕过"}],
                    )
            for dep_id in task_def.get("depends_on", []):
                dep_ts = state.get(dep_id)
                if (dep_ts.status if dep_ts else "pending") not in TERMINAL_STATUSES:
                    raise OrchdError(
                    ErrorCode.E008,
                    f"dependency_not_met: '{task_id}' blocked_by {dep_id} "
                    f"status={dep_ts.status if dep_ts else 'pending'}",
                    [{"task_id": task_id, "blocked_by": dep_id,
                      "blocked_status": dep_ts.status if dep_ts else "pending",
                      "hint": f"依赖 {dep_id} 未完成（当前 "
                              f"{dep_ts.status if dep_ts else 'pending'}），"
                              "需等待其 completed 后重试"}])
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
            def _owns(h: str | None, hs: str | None) -> bool:
                return ((hs == session_id and h == agent_id)
                        if hs and session_id else bool(h and h == agent_id))

            if (t_state.status in ("claimed", "done", "in_review")
                    and _owns(t_state.claimed_by, t_state.claimed_session)
                    and (tid != task_id or enforce_self_review_block)):
                raise OrchdError(
                    ErrorCode.E011, f"agent_busy {tid}",
                    [{"agent_id": agent_id, "blocking_task": tid,
                      "blocking_status": t_state.status,
                      "hint": "任务完成审查（completed/cancelled）或被打回"
                              "（pending）后才可领取新任务"}])
            if (t_state.status == "in_review" and _owns(
                    t_state.review_claimed_by, t_state.review_claimed_session)):
                rid = ""
                for ev in store._read_ledger_lines(from_line=1):
                    if (ev.get("task_id") == tid
                            and ev.get("type") == "REVIEW_CLAIMED"
                            and ev.get("agent_id") == agent_id):
                        rid = ev.get("event_id", "")
                raise OrchdError(
                    ErrorCode.E011, f"review busy {tid}",
                    [{"agent_id": agent_id, "blocking_task": tid,
                      "review_claim_event_id": rid,
                      "hint": "retract" if rid else "no event"}])
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
            # v4（2026-09-15 停服升级）：自审事实随 REVIEW_CLAIMED 落账；派生状态
            # TaskState.review_self_review 由 _apply_event 写入，事后可回查。
            extra: dict[str, Any] = {
                "baseline_sha": bs,
                "is_self_review": is_self_review,
            }
            if rp:
                extra["review_type"] = rp
            event = _make_event(task_id, agent_id, "REVIEW_CLAIMED", **extra)
        else:
            event = _make_event(
                task_id, agent_id, "CLAIMED", role=role,
                files_claimed=files_claimed)
        store.append_event(event)
        new_state = store.replay()
        store.update_checkpoint(new_state)
        return event, state, derived, integrity_warnings, is_self_review
    finally:
        store.release_lock()
