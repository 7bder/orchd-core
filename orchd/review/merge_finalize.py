"""review merge 收口段（_review_submit_merge_finalize），依赖 branch+residual。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.contracts.events import (
    EVENT_FORCE_STATUS,
)
from orchd.ledger import Store
from orchd.gitops_ops import make_event
from orchd.ledger import resolve_store_dir
from orchd.gitops_ops import try_delete_task_branch
from .branch import _task_branch_tip
from .residual import _merge_diagnostic_action, _own_merge_residual, _own_residual_action


def _review_submit_merge_finalize(
    *,
    result: dict[str, Any],
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    project_root: Path | None,
    merge_lock: Any | None,
    main_wt: Path | None,
    _merge_audit_fn: Any,
    pending_code_event: dict[str, Any],
    merge_result: dict[str, Any] | None,
    auto_resolved: bool,
    _nogit_single_dir: bool,
    review_type: str | None,
) -> None:
    """merge 终态收口段（原嵌套 try 体 L1051-1276，单条 If）。

    三分支：merge_env_error（未执行 / 带诊断，保持 in_review）→ 完成路径（锁内写完成
    事件、回收 worktree/分支、内联 merge 审计、自残留分档：bounce 退 in_review /
    目录残留仅注解）。锁复用判定（reuse_write/reuse_recycle）防同进程双锁 E012。
    """
    if merge_result is None and project_root is not None and not _nogit_single_dir:
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
    elif (merge_result is not None and merge_result.get("merge_diagnostic")
            and project_root is not None):
        # task-merge-failure-diagnostic：非冲突失败带诊断——保持 in_review
        # 不合并；reason 沿用 merge_env_error（下游与既有测试稳定），action
        # 按分类给可执行指引（untracked 碰撞为文件系统处置，非 git 写）。
        _diag = merge_result["merge_diagnostic"]
        result["merged"] = False
        result["reason"] = "merge_env_error"
        result["task_status"] = "in_review"
        result["merge_diagnostic"] = _diag
        result["action"] = _merge_diagnostic_action(task_id, _diag)
    elif merge_result is None or not merge_result.get("conflict") or auto_resolved:
        # project_root 为 None（非 git / 无 worktree）时不进入 merge 分支，
        # 不计算 store_root（此时 merge 必然未执行，remove_task_wt 不会命中）。
        store_root = (
            resolve_store_dir(project_root / ".orchd") if project_root else None
        )
        # 内联审计根（task-merge-audit-inline）：回收会删掉任务 worktree，
        # 审计 git 探针必须以主工作树为稳定 cwd（与删分支的 delete_root 同口径）。
        # main_wt 仅 project_root 非空时绑定，嵌套取值防 NameError。
        _audit_root = (
            (main_wt if main_wt is not None else project_root)
            if project_root else None
        )
        # 回收/删分支前捕获任务分支 tip：之后分支可能已删无法再取；
        # best-effort，取不到则跳过 tip 落 main 项（不拦）。
        _tip_sha: str | None = None
        if _audit_root is not None:
            try:
                _tip_sha = _task_branch_tip(_audit_root, task_id)
            except Exception:
                _tip_sha = None
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
                # A1（spec 审查 AC1 回归修复）：完成事件已落账——标记之，使下方
                # 失败路径兜底不再把 submitted 置 False，否则 own_merge_residual
                # 回退 in_review 时会再现「账已落、报未落」的反向背离。
                result["review_event_recorded"] = True
        finally:
            if not reuse_write:
                store.release_lock()
        if result.get("reason") != "state_changed_during_merge":
            if merge_result is None:
                # 无 git 上下文（project_root=None，单元测试/无仓库）best-effort：
                # 无实际合并，不回收 worktree、不删分支。审计恒跳过但仍附
                # 响应（task-merge-audit-inline AC1/AC4：nogit 单目录 /
                # 无上下文如实标记 skipped，不阻断完成）。
                result["merged"] = None
                if _nogit_single_dir:
                    result["merge_audit"] = {
                        "skipped": True, "reason": "nogit_single_dir",
                    }
                elif project_root is None:
                    result["merge_audit"] = {
                        "skipped": True, "reason": "no_project_root",
                    }
                else:
                    result["merge_audit"] = {
                        "skipped": True, "reason": "merge_not_executed",
                    }
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

                assert project_root is not None
                assert store_root is not None
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
                # 内联 merge 审计（task-merge-audit-inline，AC1/AC2/AC3）：
                # code APPROVED 在 merge 成功后自动跑 merge_audit 并附响应。
                # 只读 best-effort：探针失败 / 跳过（非 git / 无 main /
                # 无 project_root）不阻断完成。_merge_audit_fn 已在
                # merge/回收前提前绑定（task-audit-hint-show），此处不再
                # 延迟导入——回收后任务 worktree 源码副本已删，现导入必败。
                if _merge_audit_fn is not None and _audit_root is not None:
                    try:
                        _audit = _merge_audit_fn(store, tasks, _audit_root)
                    except Exception:
                        _audit = {"skipped": True, "reason": "audit_failed"}
                else:
                    _audit = {"skipped": True, "reason": "no_project_root"}
                result["merge_audit"] = _audit
                # 窄口径自家残留（AC2）：仅本任务残留退回 in_review +
                # 可执行指引；无关告警只注解不拦（AC3：completed 维持）。
                _own: list[dict[str, Any]] = []
                try:
                    _own = _own_merge_residual(
                        _audit_root, task_id, _tip_sha,
                        result.get("worktree_recycled"),
                        result.get("branch_deleted"),
                    )
                except Exception:
                    _own = []
                if _own:
                    if not _audit.get("skipped", False):
                        _audit.setdefault("warnings", []).extend(_own)
                    else:
                        _audit["own_residual"] = _own
                    # task-recycle-noblock：残留分两档处置——
                    # ① 分支类（未合入 / 未清理）：代码未落地，不得标记完成，
                    #    仍打回 in_review（下不变）；
                    # ② 目录残留（worktree_not_recycled，Windows 下调用方
                    #    shell 句柄占用常见）：改警告注解 + completed 维持，
                    #    不再空转第二轮审查；回收 journal 保留（仅全成功才删），
                    #    引擎下次回收该任务时按 journal 重放收敛（延迟回收）。
                    # 未知 reason 按分支档处理（fail-closed 方向）。
                    _bounce = [w for w in _own
                               if w.get("reason") != "worktree_not_recycled"]
                    _deferred = [w for w in _own
                                 if w.get("reason") == "worktree_not_recycled"]
                    if _bounce:
                        # 退回 in_review：FORCE_STATUS 通道（replay 层 ungated，
                        # target=in_review 保留审查认领字段，同一 reviewer 可重试；
                        # revive 巡检只认 completed→pending，本通道零审计噪音）。
                        _reopen = make_event(
                            task_id, agent_id, EVENT_FORCE_STATUS,
                            target_status="in_review",
                            reason="own_merge_residual",
                            residual=_bounce,
                        )
                        if not reuse_write:
                            store.acquire_lock()
                        try:
                            store.append_event(_reopen)
                            _reopened_state = store.replay()
                            store.update_checkpoint(_reopened_state)
                            result["task_status"] = "in_review"
                        finally:
                            if not reuse_write:
                                store.release_lock()
                        result["reason"] = "own_merge_residual"
                        result["action"] = _own_residual_action(
                            task_id, _bounce, _audit_root)
                    elif _deferred:
                        # 目录残留：completed 维持，仅注解（merge_audit
                        # warnings 已含该项；延迟回收指引见完成态 guidance）。
                        result["worktree_residual_deferred"] = {
                            "task_id": task_id,
                            "residual": _deferred,
                            "hint": (
                                "任务已 completed；目录残留改警告注解，不再打回 "
                                "in_review。回收 journal 已保留，引擎下次回收该任务"
                                "时按 journal 重放收敛；目录残留可运行 doctor --fix "
                                "清理（doctor_check=worktree_residual）"
                            ),
                        }

    # A1/A4（debug-pack A 组）：merge 未落地的失败路径统一兜底——审查结论
    # （verdict/comments）仍须落账，使 review --show 可回看，且响应 submitted
    # 与账本 REVIEW_SUBMITTED 落账状态一致（不再出现「报成功、账已回滚」的背离）。
    # 事件带 merge_pending 标记，状态机据此保持 in_review（不推进终态），审查
    # 认领与阶段字段不变，同一 reviewer 可重试 code APPROVED。
    if (
        pending_code_event is not None
        and result.get("task_status") == "in_review"
        and not result.get("review_event_recorded")
    ):
        if result.get("reason") == "merge_conflict":
            # A4（debug-pack A 组）：E015 冲突——verdict 落账（带 merge_pending
            # 标记，状态机据此保持 in_review、不清审查认领），使 review --show
            # 可回看结论且同一 reviewer 可重试；不写完成事件（原契约保持）。
            _pending = dict(pending_code_event)
            _pending["merge_pending"] = True
            # review --show 仅收录带 comments 的 REVIEW_SUBMITTED；无附注时补
            # 一条留痕说明，保证结论可回看。
            if not _pending.get("comments"):
                _pending["comments"] = (
                    f"merge 冲突未落地：审查结论 {_pending.get('verdict')} 已留痕，"
                    f"待同一 reviewer 解决冲突后重试"
                )
            store.acquire_lock()
            try:
                store.append_event(_pending)
                _pending_state = store.replay()
                store.update_checkpoint(_pending_state)
            finally:
                store.release_lock()
            result["review_event_recorded"] = True
            result["submitted"] = True
        else:
            # A1（debug-pack A 组）：非冲突的 merge 未落地（merge_env_error /
            # merge_diagnostic）——不落完成事件（原契约保持），但响应 submitted
            # 从账本终态反查（无 REVIEW_SUBMITTED → False），不再背离。
            result["submitted"] = False
