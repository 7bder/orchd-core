"""Orchd 任务生命周期管理 - control 域。

迁移自 orchd/onboard.py（task-split-onboard-control-config）：
  - retract: 撤回事件（级联）
  - _task_completed_epoch: 扫描 ledger 求任务最近一次进入 completed 状态的 epoch 秒
  - _validate_revive_evidence: 严格校验复活证据 commit
  - force_status: 强制设置任务状态
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_default_branch as _get_default_branch, hook_uninstall
from orchd.gitops_ops import make_event as _make_event
from orchd.ledger import Store, TaskState, resolve_store_dir, review_claim_age_s, review_stale_timeout_s

# force-status 合法目标：force_status() 只允许将任务强制设置到这四种状态，
# 其余状态（如 in_review、done）由正常事件流驱动，不可被强制跳转。
_FORCE_TARGETS = {"pending", "claimed", "completed", "cancelled"}


# _is_fingerprint_agent_id 单一事实源已上移 ledger.is_fingerprint_agent_id
# （task-fp-identity-single-source），见顶部 import 区（导入为旧名别名保持调用点不变）。

# M-3 逃生口（2026-08-12 全面审计）：默认跳转矩阵 _ALLOWED_FROM 外的两条
# "合理但不鼓励"的跳转，仅当调用方显式 force=True（CLI --force 二次确认）时放行。
# 格式为 (target_status, current_status)，与 _ALLOWED_FROM 的 key 语义一致。
# - ("completed", "claimed")：claimed → completed（实现者弃坑但功能已完成，善后）
# - ("pending", "cancelled")：cancelled → pending（误取消后复活）
# 其余默认非法跳转（如跳过审查的 pending→completed、终态互转 completed→cancelled）
# 不在此白名单内，--force 对它们无效，防止逃生口被用来绕过正常审查/终态保护。
_FORCE_ESCAPE_HATCHES = frozenset({
    ("completed", "claimed"),
    ("pending", "cancelled"),
})

# verify_command 超时（秒）：120s 是在"给测试套件足够执行时间"与
# "避免无限期阻塞 done 流程"之间的折中值；大部分单测 / lint 命令在 2 分钟内可完成。
_VERIFY_TIMEOUT = 120
# 方案 C（task-full-regression-gate）：全量 pytest 冒烟超时上限（秒）。
# 全量套件不含慢用例时约 35s，预留足够余量；失败仅告警不阻断 done。
_FULL_REGRESSION_TIMEOUT = 300


def retract(
    store: Store,
    agent_id: str,
    target_event_id: str | None = None,
    reason: str = "",
    project_root: Path | None = None,
    *,
    task_id: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    """撤回事件（级联）。

    支持两种定位方式：
    - ``target_event_id``：按事件 ID 精确撤回（向后兼容）；
    - ``task_id`` + ``event_type``：自动定位该任务最近一条匹配类型的事件（P0-10）。

    找到目标事件后，自动撤回该事件及其后同 task_id 的所有后续事件（级联撤回）。
    注意：FORCE_STATUS 事件不可撤回——它是管理员强制操作，具有不可逆语义，
    若需修正应再次调用 force_status 而非 retract。
    """
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        # 读取全部事件找 target
        all_events = store._read_ledger_lines(from_line=1)

        # P0-10：task_id + event_type 自动定位最近匹配事件
        if target_event_id is None and task_id and event_type:
            target_event = None
            for ev in reversed(all_events):
                if (ev.get("task_id") == task_id
                        and ev.get("type") == event_type
                        and not ev.get("retracted")):
                    target_event = ev
                    target_event_id = ev.get("event_id")
                    break
            if target_event is None:
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: no active {event_type} event found for task '{task_id}'",
                    [{"task_id": task_id, "event_type": event_type,
                      "hint": "确认任务 ID 和事件类型是否正确；可用 python .orchd/__main__.py status 查看事件历史"}],
                )
        elif target_event_id is None:
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: retract requires --event <id> or --task <id> --type <type>",
                [{"hint": "请提供事件 ID 或任务 ID + 事件类型"}],
            )

        target_event = None
        for ev in all_events:
            if ev.get("event_id") == target_event_id:
                target_event = ev
                break

        if target_event is None:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: event '{target_event_id}' not found",
                [{"event_id": target_event_id}],
            )
        if target_event.get("type") == "FORCE_STATUS":
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: cannot retract FORCE_STATUS event",
                [{"event_id": target_event_id, "type": "FORCE_STATUS"}],
            )

        # E034: 撤认归属守卫（task-retract-ownership-guard）。
        # 仅允许事件作者本人或 admin 撤回；跨 agent 撤认他人事件需走
        # force-status/admin 控制面，防止实现者撤认独立审查者的 REVIEW_CLAIMED
        # 以绕过独立审查（2026-08-24 自审+撤认绕过事件后的引擎层修复）。
        # 注：admin 控制面语义为有意保留（test_admin_cross_retract_allowed 断言）；
        # 其「无认证」属 P2-11，根治需 1.6 Registry 认证机制，暂不在此移除。
        target_author = target_event.get("agent_id")
        if target_author is not None and target_author != agent_id and agent_id != "admin":
            # W-2 僵尸审查认领接管：跨 agent 撤认他人 REVIEW_CLAIMED 仅在目标
            # 认领已超时（stale，押注作者/会话失联）时放行——这正是"接管僵死审查"
            # 的必要路径；未超时仍受 E034 保护，防实现者借撤认绕过独立审查。
            stale_release = False
            if target_event.get("type") == "REVIEW_CLAIMED":
                _age = review_claim_age_s(target_event.get("timestamp"))
                if _age is not None and _age >= review_stale_timeout_s():
                    stale_release = True
            if not stale_release:
                raise OrchdError(
                    ErrorCode.E034,
                    f"retract_not_authorized: event '{target_event_id}' owned by "
                    f"'{target_author}', caller '{agent_id}' cannot retract",
                    [{"event_id": target_event_id, "owner": target_author,
                      "caller": agent_id,
                      "hint": "跨 agent 撤认需事件作者本人或 admin 操作，或目标为超时（僵尸）"
                              "审查认领（W-2，引擎按时间判定放行）；如确须纠正，请事件作者撤回"
                              "或管理员 force-status"}],
                )

        # 找级联事件（同 task_id，在 target 之后的事件）
        task_id = target_event.get("task_id", "")
        target_idx = next(i for i, ev in enumerate(all_events)
                         if ev.get("event_id") == target_event_id)
        cascade_ids = [target_event_id]
        for ev in all_events[target_idx + 1:]:
            if ev.get("task_id") == task_id and ev.get("type") != "RETRACT":
                cascade_ids.append(ev.get("event_id"))

        # 逐条写 RETRACT
        retracted_events = []
        for eid in cascade_ids:
            retract_ev = _make_event(
                task_id, agent_id, "RETRACT",
                target_event_id=eid,
                reason=reason,
            )
            store.append_event(retract_ev)
            retracted_events.append(eid)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    # L3 pre-commit hook 卸载（best-effort，锁外）
    if project_root:
        hook_uninstall(project_root)

    result = {
        "retracted": True,
        "retracted_events": retracted_events,
        "task_id": task_id,
        "new_status": new_state.get(task_id, TaskState()).status,
    }
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result


# ------------------------------------------------------------------
# force_status（写操作，锁内）
# ------------------------------------------------------------------


def _task_completed_epoch(store: Store, task_id: str) -> float | None:
    """扫描 ledger 求任务最近一次进入 completed 状态的 epoch 秒；从未完成返回 None。

    与 replay 状态机同源（task-force-status-revive-guard）：
    - 先收集全部 RETRACT 的 ``target_event_id``，跳过被撤回事件；
    - 对 ``REVIEW_SUBMITTED``（code+APPROVED）与 ``FORCE_STATUS``（→completed）
      视为进入 completed，取最后一条的 ``timestamp``（ISO 8601 → epoch 秒）。
    timestamp 缺失或解析失败返回 None（调用方按"无法证明晚于完成"从严拒绝）。
    """
    if not store.ledger_exists():
        return None
    events = store._read_ledger_lines(from_line=1)
    retracted = store._collect_retracted_event_ids()
    completed_iso: str | None = None
    for ev in events:
        if ev.get("type") == "RETRACT" or ev.get("event_id", "") in retracted:
            continue
        if ev.get("task_id") != task_id:
            continue
        etype = ev.get("type")
        if etype == "REVIEW_SUBMITTED":
            # review-unify-r2：unified 单阶段（无 review_type）与 code APPROVED
            # 均视为进入 completed 的终审。
            if ev.get("verdict") == "APPROVED" and (
                ev.get("review_type") == "code" or ev.get("review_type") is None
            ):
                completed_iso = ev.get("timestamp")
        elif etype == "FORCE_STATUS" and ev.get("target_status") == "completed":
            completed_iso = ev.get("timestamp")
    if not completed_iso:
        return None
    try:
        return datetime.fromisoformat(completed_iso).timestamp()
    except (ValueError, TypeError):
        return None


def _validate_revive_evidence(
    project_root: Path | None,
    evidence_sha: str,
    completed_epoch: float | None,
    task_id: str,
) -> None:
    """严格校验复活证据 commit（task-force-status-revive-guard，completed→pending 门禁）。

    三项校验，任一不满足抛 E007 拒绝复活：
      1. git 仓库可用（``project_root`` 非空）。
      2. ``evidence_sha`` 指向真实 commit，且是默认分支（main/master）的祖先。
      3. 该 commit 的提交时间（epoch 秒）**晚于**任务最近一次完成时间。

    git 证据保证复活"事后有据可查"：commit 必须落在任务完成后且已进入主干，
    避免用任意/陈旧 sha 蒙混过关。``completed_epoch`` 无法确定时同样拒绝。
    """
    if project_root is None:
        raise OrchdError(
            ErrorCode.E007,
            f"revive_blocked: 复活 '{task_id}' 需 git 仓库做证据校验（project_root 缺失）",
            [{"task_id": task_id, "missing": "project_root"}],
        )

    def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    try:
        # 1. sha 必须是有效 commit（rev-parse --verify 失败返回非 0）
        verify = _git(["rev-parse", "--verify", "--quiet", f"{evidence_sha}^{{commit}}"])
        if verify.returncode != 0:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 证据 sha '{evidence_sha}' 不是有效 commit",
                [{"task_id": task_id, "evidence_sha": evidence_sha, "reason": "not_a_commit"}],
            )

        # 2. 必须是默认分支祖先
        branch = _get_default_branch(project_root)
        if branch is None:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 无法判定默认分支，证据 sha '{evidence_sha}' 校验中止",
                [{"task_id": task_id, "evidence_sha": evidence_sha, "reason": "no_default_branch"}],
            )
        anc = _git(["merge-base", "--is-ancestor", evidence_sha, branch])
        if anc.returncode != 0:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 证据 sha '{evidence_sha}' 不是 {branch} 的祖先",
                [{"task_id": task_id, "evidence_sha": evidence_sha, "branch": branch,
                  "reason": "not_ancestor"}],
            )

        # 3. 提交时间必须晚于任务最近一次完成时间（严格证据，无法证明则拒绝）
        if completed_epoch is None:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 无法确定任务完成时间，证据 sha '{evidence_sha}' 校验中止",
                [{"task_id": task_id, "evidence_sha": evidence_sha, "reason": "no_completion_time"}],
            )
        show = _git(["show", "-s", "--format=%ct", evidence_sha])
        if show.returncode != 0 or not show.stdout.strip():
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 无法读取证据 sha '{evidence_sha}' 提交时间",
                [{"task_id": task_id, "evidence_sha": evidence_sha, "reason": "no_commit_time"}],
            )
        commit_epoch = float(show.stdout.strip())
        if commit_epoch <= completed_epoch:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 证据 sha '{evidence_sha}' 早于任务完成时间"
                "（须晚于最近一次 completed 时刻）",
                [{"task_id": task_id, "evidence_sha": evidence_sha,
                  "commit_epoch": commit_epoch, "completed_epoch": completed_epoch,
                  "reason": "before_completion"}],
            )
    except OrchdError:
        raise
    except (subprocess.SubprocessError, OSError):
        raise OrchdError(
            ErrorCode.E007,
            f"revive_blocked: 复活 '{task_id}' git 证据校验不可用（git 命令失败）",
            [{"task_id": task_id, "reason": "git_unavailable"}],
        ) from None


def force_status(
    store: Store,
    agent_id: str,
    task_id: str,
    target_status: str,
    reason: str,
    assignee: str | None = None,
    force: bool = False,
    project_root: Path | None = None,
    evidence_sha: str | None = None,
    test_data: bool = False,
) -> dict[str, Any]:
    """强制设置任务状态。

    受两层约束：
      1. 目标状态必须在 _FORCE_TARGETS 集合内（pending / claimed / completed / cancelled）。
      2. "允许从"矩阵：每个目标状态仅允许从特定源状态跳转，
         例如 claimed 只能从 pending 强制，completed 只能从 in_review 强制，
         cancelled 可从 pending / claimed / done / in_review 强制。
    这避免了不合逻辑的状态跳转（如从 pending 直接强制到 completed 跳过审查）。

    逃生口（M-3，2026-08-12）：矩阵外的两条"合理但不鼓励"跳转——
    claimed→completed（弃坑但功能已完成）与 cancelled→pending（误取消复活）——
    仅当 ``force=True``（CLI ``--force`` 显式二次确认）时放行；否则仍抛 E007。
    ``force`` 对白名单之外的非法跳转不生效，防止绕过正常审查/终态保护。

        复活门禁（task-force-status-revive-guard，2026-08-24 事故修复）：completed→pending
        已**移出** ``_ALLOWED_FROM["pending"]`` 默认矩阵（不再无 --force 直接放行），
        纳入需 ``--force + --evidence-sha <commit>``（git 证据严格校验）的复活路径：
        sha 必须存在、是 main 祖先、提交时间晚于该任务完成时间——任一不满足 E007 拒绝。

        ``test_data``（task-p2-ledger-audit-noise）：测试注入的 FORCE_STATUS 事件
        标记 ``test_data=True``，使 ``revive_audit`` / ``status`` 的复活扫描能区分
        生产复活与测试污染（测试数据不产生审计告警）。仅作事件标记，不影响状态机
        流转与任何门禁逻辑；生产路径默认 ``False``（零回归）。
        """
    if target_status not in _FORCE_TARGETS:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: cannot force to '{target_status}' (legal: {sorted(_FORCE_TARGETS)})",
            [{"target": target_status, "legal_targets": sorted(_FORCE_TARGETS)}],
        )
    if target_status == "claimed" and not assignee:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_state: force to 'claimed' requires --assignee",
            [{"target": "claimed", "missing": "assignee"}],
        )

    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        ts = state.get(task_id)
        current = ts.status if ts else "pending"

        # "允许从" 校验矩阵（completed→pending 已移出，见复活门禁）
        _ALLOWED_FROM = {
            "pending": {"claimed", "done", "in_review"},
            "claimed": {"pending"},
            "completed": {"in_review"},
            "cancelled": {"pending", "claimed", "done", "in_review"},
        }
        allowed = _ALLOWED_FROM.get(target_status, set())
        if current not in allowed:
            in_hatch = (target_status, current) in _FORCE_ESCAPE_HATCHES
            # 复活门禁：completed→pending 严格路径（--force + --evidence-sha git 证据）
            is_revive = current == "completed" and target_status == "pending"
            if is_revive:
                if not force:
                    raise OrchdError(
                        ErrorCode.E007,
                        f"revive_blocked: completed→pending 复活 '{task_id}' 需 --force 显式确认"
                        "（防止无授权复活已完成任务）",
                        [{"task_id": task_id, "missing": "--force"}],
                    )
                if not evidence_sha:
                    raise OrchdError(
                        ErrorCode.E007,
                        f"revive_blocked: completed→pending 复活 '{task_id}' 需 "
                        "--evidence-sha <commit>（git 证据，须为 main 祖先且晚于任务完成）",
                        [{"task_id": task_id, "missing": "--evidence-sha"}],
                    )
                _validate_revive_evidence(
                    project_root, evidence_sha, _task_completed_epoch(store, task_id), task_id
                )
            elif force and in_hatch:
                pass  # 显式逃生口：放行
            else:
                hint = (
                    "；如需走逃生口（claimed→completed / cancelled→pending）请加 --force"
                    if in_hatch
                    else ""
                )
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: cannot force '{task_id}' from '{current}' to '{target_status}'"
                    f" (allowed from: {sorted(allowed)}){hint}",
                    [{"task_id": task_id, "current": current, "target": target_status,
                      "allowed_from": sorted(allowed)}],
                )

        # W-5 逃生舱语义一致性：completed 终态 = 状态终态 + 代码落 main，二者由引擎一并
        # 保证。任务分支领先 main 时自动 --ff-only 快进落码后再置终态；与 main 分叉则拒绝
        # 自动合并（仅接受快进，防止静默并入错误代码）并返回冲突指引交人工。
        merge_state: dict[str, Any] | None = None
        if target_status == "completed" and project_root:
            from orchd.gitops_ops import try_ff_merge_to_main

            merge_state = try_ff_merge_to_main(project_root, task_id)
            if merge_state and merge_state.get("state") == "diverged":
                raise OrchdError(
                    ErrorCode.E007,
                    f"force_complete_diverged: 任务分支 {merge_state.get('branch')} 与 main "
                    "已分叉，无法自动快进合并（逃生舱仅接受 --ff-only）。请先手工解决分叉"
                    "（合并/rebase）使任务分支可快进到 main，再重新 force-status completed，"
                    "防止静默并入错误代码。",
                    [{"task_id": task_id, "branch": merge_state.get("branch"),
                      "guidance": "resolve_divergence_then_retry"}],
                )

        event = _make_event(
            task_id, agent_id, "FORCE_STATUS",
            target_status=target_status,
            reason=reason,
        )
        if assignee:
            event["assignee"] = assignee
        # 持久化复活证据（task-force-status-revive-audit 依赖）：completed→pending 复活
        # 经 revive-guard 校验的 evidence_sha 必须落库，否则 revive 标记/巡检无法展示证据。
        if evidence_sha:
            event["evidence_sha"] = evidence_sha
        if test_data:
            event["test_data"] = True
        store.append_event(event)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    result = {
        "forced": True,
        "task_id": task_id,
        "previous_status": current,
        "new_status": target_status,
        "reason": reason,
    }
    # W-5：completed 逃生隐含的落码结果透出（merged / already_in_main / None）。
    if target_status == "completed" and merge_state is not None:
        result["merge"] = merge_state
    # task-14-worktree-lifecycle（AC3）：终态（completed/cancelled）自动回收任务
    # worktree（git worktree remove + 删分支 + 解绑，best-effort）。
    if target_status in ("completed", "cancelled") and project_root:
        from orchd.worktree import remove_task_wt

        result["worktree_recycled"] = remove_task_wt(
            project_root, task_id, resolve_store_dir(store.orchd_dir)
        )
        # L3 pre-commit hook 卸载（best-effort）：force_status 直达终态时兜底，
        # 防止任务级 hook 残留（与 done/retract 一致）。
        hook_uninstall(project_root)
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result

# task-errexit-weak-polish-batch: E007 hint polish placeholder
