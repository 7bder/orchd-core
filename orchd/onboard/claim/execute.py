"""claim 域 —— 认领执行（锁内写事件 + worktree 就绪）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`_claim_write_event`：锁内 check-then-act——状态机校验（E008/E009/E010/E011/
  E016/E007 冷却）后写 CLAIMED / REVIEW_CLAIMED 事件并推进 checkpoint。
- :func:`_claim_setup_worktree`：创建/绑定任务 worktree（nogit 降级 / container /
  master 副本抑制 / lessons 迁移，失败一律 degraded_guards 留痕）。

依赖：precheck（冷却 / branch 降级 / 归属谓词 / 事件查询 / hook 与 nogit 就绪分支）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_REVIEW_CLAIMED,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
from orchd.contracts.task_schema import (
    DEPENDS_ON_FIELD,
    EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
    REVIEWERS_FIELD,
    SOURCE_FIELD,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_current_branch, get_head_commit, hook_install
from orchd.gitops_ops import make_event as _make_event, try_git_branch as _try_git_branch
from orchd.ledger import (
    Store,
    TaskDerived,
    TaskState,
    is_fingerprint_agent_id as _is_fingerprint_agent_id,
    resolve_store_dir,
)
from orchd.ledger.session import require_review_lineage as _require_review_lineage
from orchd.onboard.serial_source import serial_busy_siblings
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
    _find_last_review_claim_event,
    _install_claim_hook,
    _owns_review,
    _owns_task,
    _setup_nogit_worktree,
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
            # 单目录无 git（task-nogit-single-dir-pivot）：基线快照 + 绑定主目录，
            # 分支逻辑落在 precheck._setup_nogit_worktree（依赖注入保持补丁面）。
            from orchd.worktree import bind_task_wt

            worktree_path = _setup_nogit_worktree(
                project_root, task_id, store, degraded_guards,
                take_snapshot=take_snapshot, bind_task_wt=bind_task_wt)
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
        files_to_edit = task_def.get(FILES_TO_EDIT_FIELD, [])
        if files_to_edit:
            # 经已登记的 ``execute -> gitops`` 边取 install 子模块（同 precheck）。
            import orchd.gitops as _gitops

            _install_claim_hook(
                project_root, task_id, files_to_edit,
                task_def.get(EXEMPT_FILES_FIELD), degraded_guards,
                hook_install=hook_install, hook_mod=_gitops.hook.install)
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



def _validate_claim_state(
    store: Store,
    tasks: list[dict[str, Any]],
    task_def: dict[str, Any],
    task_id: str,
    agent_id: str,
    session_id: str | None,
    role: str,
    review_type: str | None,
    project_root: Path | None,
    *,
    force: bool = False,
    state: dict[str, TaskState] | None = None,
    derived: TaskDerived | None = None,
    check_serial: bool = True,
) -> bool:
    """认领状态机校验（task-engine-defect-fix-unified-u3 单一实现）。

    **同一份**判定被两处消费：

    - precheck 的只读状态门（锁外快速失败，先于任何写 hook）；
    - :func:`_claim_write_event` 的锁内复核（并发权威）。

    错误码与语义只在此处实现一次——precheck / execute 不再各判一遍（AC3）。
    ``state`` / ``derived`` 可由调用方传入以复用 replay / scan；缺省自行计算。
    ``check_serial=False`` 由 **precheck 只读门**传入：同 source 串行门只保留在
    锁内权威复核一次（task-v163-claim-budget-and-errors：claim 与 request 共用
    :func:`serial_busy_siblings` 单一谓词，且每次 claim 只调用一次，杜绝重复候选
    逻辑；锁内复核仍兜住 request→claim 的 TOCTOU）。
    返回实现者本人在审查认领时是否构成自审（reviewer 路径；其余恒 False）。
    """
    if state is None:
        state = store.replay()
    ts = state.get(task_id)
    status = ts.status if ts else STATUS_PENDING
    is_self_review = False
    if role == "reviewer":
        # task-v163-review-segments-e009：reviewer 双重认领必须阻断（E009）。
        # 幂等复用（c601970d）只在**能证明同一会话**时成立——review_claimed_session
        # 与当前 session_id 均非空且逐字相等，且 claimed_by 为同一 agent。此前用
        # ``_owns_review`` 的「agent 回退」判定，在无会话身份（session_id 为空）时
        # 把两次匿名 claim 误判成同会话，重复认领被静默放行。
        same_review_session = bool(
            session_id
            and ts
            and ts.review_claimed_by == agent_id
            and ts.review_claimed_session == session_id
        )
        if ts and ts.review_claimed_by and not same_review_session:
            _claimed_phase = ts.review_phase
            raise OrchdError(
                ErrorCode.E009, f"already_claimed by {ts.review_claimed_by}",
                [{"task_id": task_id, "claimed_by": ts.review_claimed_by,
                  "review_claimed_by": ts.review_claimed_by,
                  "review_phase": _claimed_phase,
                  "task_status": status,
                  "hint": f"任务已被审查者 {ts.review_claimed_by} 认领"
                          f"（review_phase={_claimed_phase}），等待其完成或由其 "
                          "retract 后重试，禁止重复 claim；本判定先于分支/工作区守卫"}])
        if status != STATUS_IN_REVIEW:
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
        designated = task_def.get(REVIEWERS_FIELD, [])
        if agent_id not in designated and not _is_fingerprint_agent_id(agent_id):
            raise OrchdError(
                ErrorCode.E007,
                f"not_designated_reviewer: '{agent_id}' 不在任务 "
                f"'{task_id}' 的 reviewers 名单中",
                [{"task_id": task_id, "agent": agent_id,
                  REVIEWERS_FIELD: designated,
                  "hint": "请使用名单内的 agent ID"}])
        if (ts and ts.claimed_by == agent_id and ts.review_claimed_by
                and ts.review_claimed_by != agent_id):
            raise OrchdError(ErrorCode.E011, "review_hijack", [{"task_id": task_id}])
        if derived is None:
            derived = store.scan_task_derived()
        done_author, _ = _extract_last_done(store, task_id, derived)
        # v4（2026-09-15 停服升级）：判定收敛到 is_self_review_author（单一事实源），
        # 与原内联三元表达式严格等价——行为不变，仅消除三处私有副本的漂移风险。
        _done_event = _find_last_done_event(store, task_id, derived)
        is_self = _is_self_review_author(_done_event, agent_id, session_id)
        # P0-1（task-review-hardblock-e1-claim-gate）：自审无条件硬阻断——
        # 不再读取全局 config 开关与任务级 require_independent_review 字段。
        # 审查必须由实现会话 spawn 的独立 subagent 承担（独立 session 即非自审），
        # 因此「实现者本人 = 审查者」在任何配置下都不存在合法形态：无降级项、
        # 无 self_review_notice、无逃生口，一律 E016。
        if is_self:
            raise OrchdError(
                ErrorCode.E016, "self_review",
                [{"task_id": task_id, "done_by": done_author}])
        # P0-3（task-review-hardblock-e3-lineage-gate）：lineage 硬门——「指纹不同」
        # 只证明换了会话，证明不了「是实现会话 spawn 的 subagent」。这里要求当前
        # 会话 runtime 声明 kind=reviewer 且 parent_session_id 逐字等于实现会话
        # （该任务最后一次 DONE 事件的 session_id）；不符一律 E016。判定实现单一
        # 真源 = orchd.ledger.session.require_review_lineage（提交侧共用同一函数）。
        _require_review_lineage(
            store.orchd_dir,
            session_id=session_id,
            expected_parent_session_id=(
                _done_event.get("session_id") if _done_event else None
            ),
            task_id=task_id,
            stage="claim",
        )

    else:
        # AC1（u3）：in_review 且审查已被他人认领——先于任何写 hook 报 E009，
        # details/hint 带审查认领者指纹与 review_phase；此前该场景会被
        # guard_claim 错报成 E018（或写事件阶段的 E008）。
        if ts and ts.status == STATUS_IN_REVIEW and ts.review_claimed_by:
            raise OrchdError(
                ErrorCode.E009,
                f"already_claimed_by_reviewer: task '{task_id}' 审查中由 "
                f"{ts.review_claimed_by} 认领（review_phase={ts.review_phase}）",
                [{"task_id": task_id,
                  "claimed_by": ts.review_claimed_by,
                  "review_claimed_by": ts.review_claimed_by,
                  "review_phase": ts.review_phase,
                  "task_status": ts.status,
                  "implementer_claimed_by": ts.claimed_by,
                  "hint": (f"任务已进入审查且被 {ts.review_claimed_by} 认领"
                           f"（review_phase={ts.review_phase}）：实现认领须等"
                           "审查完成（completed）或被打回（pending）后重试")}],
            )
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
        if status != STATUS_PENDING:
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
        for dep_id in task_def.get(DEPENDS_ON_FIELD, []):
            dep_ts = state.get(dep_id)
            if (dep_ts.status if dep_ts else STATUS_PENDING) not in TERMINAL_STATUSES:
                raise OrchdError(
                ErrorCode.E008,
                f"dependency_not_met: '{task_id}' blocked_by {dep_id} "
                f"status={dep_ts.status if dep_ts else STATUS_PENDING}",
                [{"task_id": task_id, "blocked_by": dep_id,
                  "blocked_status": dep_ts.status if dep_ts else STATUS_PENDING,
                  "hint": f"依赖 {dep_id} 未完成（当前 "
                          f"{dep_ts.status if dep_ts else STATUS_PENDING}），"
                          "需等待其 completed 后重试"}])
        # engine-discipline-hardening（2026-10-23 停服施工）：同 source 串行门——
        # 同 source 其他任务存在在手（claimed/done/in_review）→ 拒绝本卡 claim（E041）。
        # 只拦实现侧（审查认领不破坏现场）；pending 兄弟不拦（批量注册骨架常态，
        # 伞收口卡 pending 亦不拦）；跨组冲突仍由 E010 / request 依赖感知过滤兜底。
        # 判定单一真源 = orchd/onboard/serial_source.py::serial_busy_siblings；
        # request 前置过滤共用同一谓词（task-request-serial-source-pregate）。
        # **谓词每次 claim 只调用一次**：precheck 只读门以 check_serial=False 跳过，
        # 由本处锁内权威复核统一判定（task-v163-claim-budget-and-errors）。
        if check_serial:
            _src = task_def.get(SOURCE_FIELD)
            if _src:
                _busy_siblings = serial_busy_siblings(task_def, tasks, state)
                if _busy_siblings:
                    raise OrchdError(
                        ErrorCode.E041,
                        f"serial_source_conflict: '{task_id}' source={_src} "
                        f"busy_siblings={','.join(sorted(_busy_siblings))}",
                        [{"task_id": task_id, SOURCE_FIELD: _src,
                          "busy_siblings": _busy_siblings,
                          "hint": "同 source 串行包：兄弟任务在手（claimed/done/in_review），"
                                  "等其 completed 或 retract 后再 claim；收口卡须待全部终态"}])

        conflicts = detect_file_conflict(state, tasks, task_def)
        if conflicts:
            try:
                from orchd.guide import amend_patch_cmd as _amend_cmd2
                _patch = _amend_cmd2(task_id, files=["<file>"], entry="orchd")
            except Exception:
                _patch = "orchd amend --task <id> --files-to-edit <file>"
            raise OrchdError(ErrorCode.E010, "conflict", [
                # task-debug-claim-register-ux：重叠文件已在 Conflict.files 里算好，
                # 此前只回 task_id ⇒ agent 必须手读 _master.json 才能比对。
                {"task_id": c.task_id, "files": list(c.files),
                 "claimed_by": getattr(c, "claimed_by", None)}
                for c in conflicts
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
        if (t_state.status in (STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW)
                and _owns_task(t_state, agent_id, session_id)
                and tid != task_id):
            # task-v163-claim-budget-and-errors：活跃认领滞留任务分支时，分支/工作区
            # 守卫（E018）优先于 agent_busy（E011）——否则 agent 会误以为只需等待，
            # 实际现场是「主工作树停在他人活跃任务的 task 分支」。此判定先于
            # implementer 滞留自愈，保证不自愈、不切换分支（主工作树原地不动）。
            _cur_branch = get_current_branch(project_root) if project_root else None
            if _cur_branch and _cur_branch.startswith("task/"):
                raise OrchdError(
                    ErrorCode.E018,
                    f"wrong_branch: claim 须在默认分支执行，当前滞留 {_cur_branch}",
                    [{"agent_id": agent_id, "task_id": task_id,
                      "blocking_task": tid,
                      "blocking_status": t_state.status,
                      "current_branch": _cur_branch,
                      "hint": "主工作树停在活跃任务的 task 分支：先完成/retract 该任务"
                              "回到默认分支后再领取；引擎不得在此状态自愈或切换分支"}])
            raise OrchdError(
                ErrorCode.E011, f"agent_busy {tid}",
                [{"agent_id": agent_id, "blocking_task": tid,
                  "blocking_status": t_state.status,
                  "hint": "任务完成审查（completed/cancelled）或被打回"
                          "（pending）后才可领取新任务"}])
        if (t_state.status == STATUS_IN_REVIEW
                and tid != task_id
                and _owns_review(t_state, agent_id, session_id)):
            rid = ""
            for ev in store._read_ledger_lines(from_line=1):
                if (ev.get("task_id") == tid
                        and ev.get("type") == EVENT_REVIEW_CLAIMED
                        and ev.get("agent_id") == agent_id):
                    rid = ev.get("event_id", "")
            raise OrchdError(
                ErrorCode.E011, f"review busy {tid}",
                [{"agent_id": agent_id, "blocking_task": tid,
                  "review_claim_event_id": rid,
                  "hint": "retract" if rid else "no event"}])
    return is_self_review


def _claim_write_event(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    task_def: dict[str, Any],
    role: str,
    session_id: str | None,
    review_type: str | None,
    project_root: Path | None,
    force: bool = False,
) -> tuple[dict[str, Any], dict[str, TaskState], TaskDerived, list[dict[str, Any]], bool]:
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        derived = store.scan_task_derived()
        is_self_review = _validate_claim_state(
            store, tasks, task_def, task_id, agent_id, session_id, role,
            review_type, project_root,
            force=force, state=state, derived=derived,
        )
        ts = state.get(task_id)
        files_claimed = task_def.get(FILES_TO_EDIT_FIELD, [])
        if role == "reviewer":
            active_claim = _find_last_review_claim_event(store, task_id)
            if (active_claim is not None
                    and ts is not None
                    and session_id
                    and ts.review_claimed_by == agent_id
                    and ts.review_claimed_session == session_id):
                # 幂等：同一会话重复认领同一阶段 → 复用既有 REVIEW_CLAIMED，
                # 不追加重复事件、不覆盖 review_claimed_at。
                return active_claim, state, derived, integrity_warnings, is_self_review
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
            event = _make_event(task_id, agent_id, EVENT_REVIEW_CLAIMED, **extra)
        else:
            event = _make_event(
                task_id, agent_id, EVENT_CLAIMED, role=role,
                files_claimed=files_claimed)
        store.append_event(event)
        new_state = store.replay()
        store.update_checkpoint(new_state)
        return event, state, derived, integrity_warnings, is_self_review
    finally:
        store.release_lock()
