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
# task-14-git-policy-layer：判定类逻辑（checkout_default_strict / guard_review_write）
# 已收敛到 orchd.gitops（专用 git 判定模块）；共享辅助 make_event 仍自
# orchd.gitops_ops 导入。
from orchd.gitops import (
    checkout_default_strict,
    get_head_commit,
    guard_review_write,
    hook_uninstall,
    main_worktree_root,
    release_session_lock_if_owned,
    session_lock_check,
    session_lock_release,
)
from orchd.gitops_ops import (
    make_event,
    try_auto_resolve_conflict,
    try_delete_task_branch,
    try_git_merge,
)
from orchd.ledger import (
    Store,
    TaskDerived,
    TaskState,
    # task-fp-identity-single-source：指纹判定单一事实源（本子域不导入 onboard，
    # 统一从 ledger 导入，消除私有副本的同步漂移风险）
    is_fingerprint_agent_id as _is_fingerprint_agent_id,
    resolve_review_mode,
    resolve_store_dir,
)


def _recent_transitions(
    store: Store, task_id: str, limit: int = 5, derived: TaskDerived | None = None,
) -> list[dict[str, Any]]:
    """提取任务最近 N 条状态变迁事件（P0-9 E007 信息增强）。

    从 ledger 中提取该任务最近的 type/agent_id/timestamp，用于 E007 报错时
    展示状态迁移轨迹，帮助用户定位"为什么任务不在预期状态"。
    """
    events = store._read_ledger_lines(from_line=1)
    transitions = [
        {
            "type": ev.get("type"),
            "agent_id": ev.get("agent_id"),
            "timestamp": ev.get("timestamp"),
        }
        for ev in events
        if ev.get("task_id") == task_id and ev.get("type")
    ]
    return transitions[-limit:]


def _task_branch_tip(project_root: Path, task_id: str) -> str | None:
    """best-effort 取 ``task/{task_id}`` 分支 tip SHA（task-merge-warning-resolve-sha）。

    供 merge_warning 事件附加 ``resolve_sha``：audit-merge 以此判定 main 是否
    已含实现（手工补 merge 落地后自动销账）。git 不可用 / 分支不存在 / 异常
    → None（省略字段，行为向后兼容）。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", f"task/{task_id}"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def request_reviewer(
    store: Store,
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    agent_id: str,
    derived: TaskDerived | None = None,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    """查找处于 in_review 且未被审查者 claim 的任务。

    排序规则：spec 阶段的审查优先于 code 阶段。同等阶段内按 ledger 遍历顺序排列。
    调用方（onboard.request）在 reviewer 角色下转发到此函数。

    self-review（实现者 == 审查者）：默认仅标注 ``is_self_review`` 并照常进入
    候选；``enforce_self_review_block=True``（线上版）时排除（AC1）。
    """
    review_candidates: list[dict[str, Any]] = []
    not_in_list: list[dict[str, Any]] = []
    task_map = {t.get("id", ""): t for t in tasks}
    for tid, ts in state.items():
        if ts.status == "in_review" and ts.review_claimed_by is None:
            task_def = task_map.get(tid, {})
            # 向后兼容：reviewers 名单存在且非空时不再名单内 → not_in_list；
            # 字段缺失/为空（指纹身份模型）则跳过名单门禁，仅按实现指纹去重。
            # 指纹豁免（task-fp-review-priority-exempt，对齐 claim 侧 E007）：
            # 指纹形态 agent_id（12 位 hex）不在名单内也正常进入候选（不落入
            # not_in_list）；具名 agent（名单外）仍记 not_in_list（向后兼容）。
            designated = task_def.get("reviewers")
            if designated and agent_id not in designated \
                    and not _is_fingerprint_agent_id(agent_id):
                not_in_list.append({
                    "task_id": tid,
                    "review_phase": ts.review_phase or "spec",
                    "reviewers": task_def.get("reviewers", []),
                })
                continue
            # self-review：DONE 实现指纹 == 当前 reviewer 指纹。
            # 默认仅标注照常分配；enforce=True 时排除（AC1）。
            from orchd.ledger import resolve_session_identity
            current_session = resolve_session_identity(store.orchd_dir)["session_id"]
            done_author, _ = extract_last_done(store, tid, derived)
            done_ev = find_last_done_event(store, tid, derived)
            done_session = done_ev.get("session_id") if done_ev else None
            if done_author:
                if done_session and current_session:
                    is_self = done_session == current_session and done_author == agent_id
                else:
                    is_self = done_author == agent_id
            else:
                is_self = False
            if is_self and enforce_self_review_block:
                continue
            entry = {
                "task_id": tid,
                "task": task_def,
                "review_phase": ts.review_phase,
            }
            if is_self:
                entry["is_self_review"] = True
            review_candidates.append(entry)
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
        # review-unify-r2：unified 单阶段（review_phase 为 None）展示为 unified。
        "review_type": best["review_phase"] or "unified",
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
    if best.get("is_self_review"):
        candidate["is_self_review"] = True

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
    review_type: str | None,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """提交审查结果（task-session-lock-lifecycle：异常路径也保证释放会话锁）。

    review_type 为 None 表示 unified 单阶段审查（review-unify-r2）。

    ``_review_submit_impl`` 的包装：``finally`` 中经 :func:`release_session_lock_if_owned`
    条件释放本 agent 的 session 锁（仅持有者==本 agent 才释放，幂等）。正常路径
    由 ``_review_submit_impl`` 尾部释放并写 ``session_lock_released``；异常/提前
    返回路径由本包装器的 finally 兜底，杜绝漏放锁（此前需 60min 超时 + watchdog 兜底）。
    """
    try:
        return _review_submit_impl(
            store, tasks, agent_id, task_id, review_type, verdict, comments, project_root
        )
    finally:
        if project_root:
            release_session_lock_if_owned(project_root / ".orchd", agent_id)


def _review_submit_impl(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """提交审查结果（APPROVED 或 CHANGES_REQUESTED）。

    review_type 为 None 表示 unified 单阶段审查（review-unify-r2）。

    锁内校验：任务必须处于 in_review 状态，审查阶段（spec/code）须匹配，
    且审查者须为当前 agent。

    spec APPROVED 自动推进到 code review；code APPROVED 先锁外 git merge，
    成功才写完成事件（任务 completed），merge 冲突则停留 in_review。
    CHANGES_REQUESTED 时任务回退 pending。
    """
    # 意图化守卫（task-14-git-policy-layer）：任意分支、不要求干净。
    guard_review_write(
        project_root,
        orchd_dir=store.orchd_dir,
        agent_id=agent_id,
    )
    # task-14-worktree-lifecycle（AC2）：目标 root == 任务 worktree（不一致 E018，
    # 防错目录审查）。flat 单会话绑定=主工作树 → 恒通过；无绑定 → best-effort 跳过。
    if project_root:
        from orchd.worktree import guard_task_root

        guard_task_root(project_root, resolve_store_dir(store.orchd_dir), task_id, "review")

    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        derived = store.scan_task_derived()
        ts = state.get(task_id)

        # P0-9：预计算最近状态变迁（仅 E007 路径使用，happy path 不触发开销）
        def _err_transitions() -> list[dict[str, Any]]:
            return _recent_transitions(store, task_id, derived=derived)

        if not ts or ts.status != "in_review":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: task '{task_id}' not in_review",
                [{"task_id": task_id, "actual": ts.status if ts else "pending",
                  "recent_transitions": _err_transitions(),
                  "hint": f"任务当前状态为 {ts.status if ts else 'pending'}（非 in_review），"
                          f"可能尚未 done 或已被审查打回 pending"}],
            )
        if ts.review_phase != review_type:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review phase mismatch (expected '{ts.review_phase}', got '{review_type}')",
                [{"task_id": task_id, "expected_phase": ts.review_phase,
                  "got_phase": review_type,
                  "recent_transitions": _err_transitions(),
                  "hint": f"审查阶段不匹配：任务处于 {ts.review_phase} 阶段，"
                          f"你提交的是 {review_type}，请确认审查类型"}],
            )
        from orchd.ledger import resolve_session_identity
        current_session = resolve_session_identity(store.orchd_dir)["session_id"]
        if ts.review_claimed_session and current_session:
            claimed_other = not (
                ts.review_claimed_session == current_session
                and ts.review_claimed_by == agent_id
            )
        else:
            claimed_other = ts.review_claimed_by != agent_id
        if claimed_other:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review not claimed by this session "
                f"(claimed_by='{ts.review_claimed_by}', claimed_session='{ts.review_claimed_session}')",
                [{"task_id": task_id, "claimed_by": ts.review_claimed_by,
                  "claimed_session": ts.review_claimed_session, "agent": agent_id,
                  "current_session": current_session,
                  "recent_transitions": _err_transitions(),
                  "hint": "审查已被其他 session 认领或当前 session 不匹配；"
                          "可用 orchd retract --task <id> --type REVIEW_CLAIMED 释放后重新认领"}],
            )

        baseline_sha = extract_review_baseline(store, task_id, agent_id, derived)
        current_sha = get_head_commit(project_root) if project_root else None
        baseline_drift = bool(baseline_sha and current_sha and baseline_sha != current_sha)

        event = make_event(
            task_id, agent_id, "REVIEW_SUBMITTED",
            verdict=verdict,
        )
        # review-unify-r2：unified 单阶段（review_type 为 None）不写 review_type
        # 字段（R2-b：新事件无 review_type）；two_phase 保留 spec/code 供 replay
        # 按两阶段语义解释，与老事件兼容。
        if review_type is not None:
            event["review_type"] = review_type
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

        elif verdict == "APPROVED" and (review_type == "code" or review_type is None):
            # review-unify-r2：unified 单阶段（review_type 为 None）与 code 终审
            # 一样走 merge → completed；two_phase 的 spec APPROVED 走上一分支。
            result["task_status"] = "in_review"
            pending_code_event = event

        elif verdict == "CHANGES_REQUESTED":
            # 打回前强约束切回默认分支(main/master)，避免工作区滞留 task/{id} 分支、
            # 后接 agent 认领时报 E018。切换失败抛 E018/E017 → 不写事件、任务仍
            # in_review、审查 claim 保留，reviewer 处理后重试即可（与 done 强约束一致）。
            if project_root:
                result["checked_out_main"] = checkout_default_strict(
                    project_root, command="review"
                )
            store.append_event(event)
            result["task_status"] = "pending"
            result["back_to_pool"] = True
            new_state = store.replay()
            store.update_checkpoint(new_state)

        else:
            # P2-5：未知 verdict / 不支持 review_type 组合不得静默无操作，
            # 显式报错，避免调用方拿到无 task_status 的「成功」结果。
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_verdict: unsupported verdict={verdict!r} review_type={review_type!r}",
                [{"task_id": task_id, "verdict": verdict, "review_type": review_type,
                  "hint": "verdict 仅支持 APPROVED / CHANGES_REQUESTED"}],
            )
    finally:
        store.release_lock()

    if pending_code_event is not None:
        # task-engine-review-merge-diff-gate：code APPROVED 前校验声明文件已全部
        # 进入任务分支 diff；缺失则拒绝 merge，避免“实现/测试未进分支”被终审放行。
        if project_root:
            try:
                task_map = {t.get("id", ""): t for t in tasks}
                task_def = task_map.get(task_id) or {}
                from orchd.worktree import missing_declared_branch_files

                missing = missing_declared_branch_files(
                    project_root, task_id, task_def.get("files_to_edit", [])
                )
                if missing:
                    raise OrchdError(
                        ErrorCode.E010,
                        "file_conflict: 声明文件未进入任务分支 diff，拒绝 merge",
                        [{
                            "task_id": task_id,
                            "missing_declared_files": missing,
                            "hint": (
                                "请回任务分支确认这些文件已提交；若无需修改，"
                                "请从 files_to_edit 移除或补充说明"
                            ),
                        }],
                    )
            except OrchdError:
                raise
            except Exception:
                pass

        # 并发 merge 串行（task-14-merge-main-tree AC4）：以主工作树锁互斥，
        # 多个 code APPROVED 同时进主工作树 merge 时排队，互不干扰。
        # flat（任务 worktree == 主工作树 == 本 store）不加锁——review_submit
        # 已在开头持有并释放同一把 store 锁，此处复用会重复 acquire 死锁（零回归）。
        merge_lock: Any | None = None
        if project_root:
            main_wt = main_worktree_root(project_root)
            if main_wt != Path(project_root).resolve():
                merge_lock = Store(main_wt / ".orchd")
                merge_lock.acquire_lock()
        try:
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
                    # P0-18：增强冲突指引——worktree 位置、文件清单、重试路径、回退命令
                    _auto_action = (auto or {}).get("action")
                    if _auto_action:
                        result["action"] = _auto_action
                    else:
                        _cf_list = ", ".join(conflict_files) if conflict_files else "未知"
                        result["action"] = (
                            f"merge 冲突（main 已恢复）。冲突文件：{_cf_list}。\n"
                            f"【解决步骤】\n"
                            f"  1. 进入任务 worktree 目录：cd ../task-{task_id}/\n"
                            f"     （container 布局下主工作树内无法 checkout task/{task_id} 分支）\n"
                            f"  2. 执行 git merge main，解决冲突后 git commit\n"
                            f"  3. 由同一 reviewer 重试 code APPROVED\n"
                            f"【放弃本次审查】\n"
                            f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason 'merge冲突放弃'\n"
                            f"  orchd force-status --task {task_id} --status pending --reason 'merge冲突回退'"
                        )

            if merge_result is None and project_root is not None:
                # P0-18：rename merge_not_executed → merge_env_error（语义更精确）
                result["merged"] = False
                result["reason"] = "merge_env_error"
                result["task_status"] = "in_review"
                result["action"] = (
                    "git merge 未执行（git 不可用 / 非 git 仓库 / 环境异常）：任务保持 "
                    "in_review，未标记完成、未回收任务 worktree。\n"
                    "【修复步骤】\n"
                    "  1. 确认 git 可用且当前目录是有效 git 仓库\n"
                    "  2. 运行 orchd doctor 检查 git 完整性\n"
                    "  3. 修复后由同一 reviewer 重试 code APPROVED\n"
                    "【放弃本次审查】\n"
                    f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason 'git环境异常'\n"
                    f"  orchd force-status --task {task_id} --status pending --reason 'git环境异常回退'"
                )
            elif merge_result is None or not merge_result.get("conflict") or auto_resolved:
                # project_root 为 None（非 git / 无 worktree）时不进入 merge 分支，
                # 不计算 store_root（此时 merge 必然未执行，remove_task_wt 不会命中）。
                store_root = (
                    resolve_store_dir(project_root / ".orchd") if project_root else None
                )
                # container（merge_lock 与 store_root/sl 落在同一共享账本根 .lock）下，
                # merge_lock 已持那把 .lock 排他锁：完成事件写入 + 终态回收（unbind）
                # 复用它而非再次 flock，避免 E012 同进程双 fd 死锁（task-14-review
                # -double-lock）。flat（merge_lock=None）与原路径一致：按需自加锁。
                merge_lock_path = (
                    merge_lock.lock_path.resolve() if merge_lock is not None else None
                )
                # 写完成事件所用锁：与 store 同根才复用；否则按需 self 加锁。
                reuse_write = bool(
                    project_root
                    and merge_lock_path is not None
                    and merge_lock_path == store.lock_path.resolve()
                )
                # 终态回收（unbind）所用锁：与 store_root 同根才复用——即使 store 与
                # project 不同根（如测试注入 detached store），只要 merge_lock 已持
                # store_root 那把 .lock，解绑就不得再次 flock（防 E012）。
                reuse_recycle = bool(
                    project_root
                    and store_root is not None
                    and merge_lock_path is not None
                    and merge_lock_path == (store_root / ".lock").resolve()
                )
                if not reuse_write:
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
                        store.append_event(pending_code_event)
                        new_state = store.replay()
                        store.update_checkpoint(new_state)
                        result["task_status"] = "completed"
                finally:
                    if not reuse_write:
                        store.release_lock()
                if result.get("reason") != "state_changed_during_merge":
                    if merge_result is None:
                        # 无 git 上下文（project_root=None，单元测试/无仓库）best-effort：
                        # 无实际合并，不回收 worktree、不删分支。
                        result["merged"] = None
                    else:
                        result["merged"] = True
                        if auto_resolved:
                            result["auto_resolved"] = True
                            result["action"] = (
                                f"merge 冲突已自动化解（abort + 分支预演合并），"
                                f"任务已 completed"
                            )
                        # task-14-review-branch-cleanup（AC1/AC2）：先回收任务 worktree，
                        # 释放 task/{task_id} 分支占用，再删分支。修复前顺序颠倒——
                        # 先 try_delete 时任务 worktree 仍 checkout 该分支，git 拒绝删除
                        # 被占用分支（即使 cwd 已改为主工作树 git -C），branch_deleted 恒为
                        # False；worktree 移除后分支不再被占用，-d 方能成功。
                        # task-14-worktree-lifecycle（AC3）：回收任务 worktree =
                        # git worktree remove + 删分支 + 解绑（best-effort）。
                        # ExclusiveFileLock 原语自动判定同进程持锁（depth 计数），
                        # 无需 lock_held 透传，不会触发 E012 死锁。
                        from orchd.worktree import remove_task_wt

                        result["worktree_recycled"] = remove_task_wt(
                            project_root, task_id, store_root, lock_held=reuse_recycle
                        )
                        # 分支删除以**主工作树**为稳定 cwd：容器布局下 remove_task_wt 已
                        # 回收并删除任务 worktree（project_root 目录不再存在），再以
                        # project_root 调用会因 cwd 失效失败。main_wt 在回收前解析，
                        # 回收后仍稳定存在（container=main/，flat=project_root）。
                        delete_root = main_wt if main_wt is not None else project_root
                        result["branch_deleted"] = try_delete_task_branch(
                            delete_root, task_id
                        )
        finally:
            if merge_lock is not None:
                merge_lock.release_lock()

    # L3 pre-commit hook 卸载（best-effort）：code APPROVED 终态后不再需要任务级
    # hook，防止残留（与 done/retract 一致；覆盖 merge 降级/成功全部完成路径）。
    if project_root and result.get("task_status") == "completed":
        hook_uninstall(project_root)

    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    return result
