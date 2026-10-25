"""report — watchdog 域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

``watchdog``（364 行单函数）及其私有助手（自动清理处置摘要 / 陈旧认领双动作）。

依赖 ``text`` / ``checks`` / ``phase``（顶层导入，单向）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_REVIEW_CLAIMED,
    TERMINAL_STATUSES,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root
from .checks import (
    task_integrity_audit,
)

from .phase import (
    _DIAGNOSTICS_TOP_N,
    _aggregate_top,
)

from .text import (
    _auto_clean_emit_stderr,
)




def _auto_clean_disposition_summary(
    auto_clean: dict[str, Any] | None,
    top_n: int = _DIAGNOSTICS_TOP_N,
) -> dict[str, Any] | None:
    """auto_clean 聚合（分档复用 doctor 单一真源，manual 优先）。

    分档一律取 :func:`orchd.doctor._residue_disposition`，档位集合取
    ``AUTO_CLEAN_DISPOSITIONS``；manual 档实例优先入选 top-N（新的覆盖维度
    与既有 disposition 语义对齐，不另立分档表）。
    """
    if not auto_clean:
        return None
    try:
        from orchd.doctor import AUTO_CLEAN_DISPOSITIONS, _residue_disposition
    except Exception:
        return None
    items: list[dict[str, Any]] = []
    for key in ("auto_cleaned", "auto_moved", "manual_notice"):
        for it in auto_clean.get(key, []) or []:
            if isinstance(it, dict):
                items.append({**it, "_src": key})
    def _disp(it: dict[str, Any]) -> str:
        try:
            return str(_residue_disposition(it.get("type"), it.get("action")))
        except Exception:
            return "manual"
    counts: dict[str, int] = {}
    for it in items:
        d = _disp(it)
        counts[d] = counts.get(d, 0) + 1
    # manual 优先，其余保持输入序；档位合法性对齐 AUTO_CLEAN_DISPOSITIONS + manual。
    allowed = set(AUTO_CLEAN_DISPOSITIONS) | {"manual"}
    ordered = sorted(
        items, key=lambda it: (0 if _disp(it) == "manual" else 1)
    )
    total = len(items)
    return {
        "total": total,
        "truncated": total > top_n,
        "top": [{k: v for k, v in it.items() if not k.startswith("_")} for it in ordered[:top_n]],
        "by_disposition": {k: v for k, v in counts.items() if k in allowed},
    }



def _stale_claim_dual_action(task_id: str) -> str:
    """stale_claims 双出路建议（task-diagnostics-aggregate-budget 移交项）。

    既有唯一出路（force-status 回退 pending）会让 agent 停在“等死会话回来”；
    另一条合法续做路径为新会话先 done 记账再接管（done 按认领者记账、不比对
    caller，任务从 claimed 推进到审查；审查打回回 pending 后新会话再 claim
    接管实现）。两者并列给出，不改判定口径。
    """
    return (
        f"建议二选一: (1) orchd force-status --task {task_id} --status pending"
        " --reason 'stale session takeover'（回退重做）; "
        f"(2) 新会话先 done 记账再接管（先执行 done --task {task_id}"
        " 按原认领者记账、不比对 caller，任务从 claimed 推进到审查；"
        "审查通过即完成；若审查打回（CHANGES_REQUESTED）回 pending 后"
        f"再 claim --task {task_id} --confirm 接管实现。注意：claimed 状态下"
        "直接 claim 会撞 E009（claim 无 stale 豁免），不得跳过 done 直接认领）"
    )



def watchdog(
    store: Store,
    tasks: list[dict[str, Any]],
    timeout_min: int = 60,
    project_root: Path | None = None,
    agent_id: str | None = None,
    takeover: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """巡检：检测两类僵死任务 + L2 session 锁僵死锁清理。

    两类僵死任务：
    - implementer（实现者僵死）：任务处于 claimed 状态且超过 timeout 分钟，
      说明实现者认领后长时间未交付。
    - reviewer（审查者僵死）：任务处于 in_review 状态、存在 review_claimed_by
      且超过 timeout 分钟，说明审查者认领评审后长时间未给出结论。

    L2 session 锁：判定「审查中锁 ≠ 僵死锁」——锁有效（未超时）时核对
    「锁持有者 agent_id + 活跃任务」：
    - 新式 flock 活性锁（``flock_active``）：``session_lock_check`` 先做 OS
      非阻塞探活——持锁进程已死则自动清理（reason=stale_cleaned），本巡检
      直接上报 released（reason=stale_os_probe），不依赖 timeout/no_active_task；
    - 持有者有进行中的实现（claimed_by == 持有者 && claimed）或审查
      （review_claimed_by == 持有者 && in_review）→ 活锁，报告不释放；
    - 持有者已无活跃任务 → 会话已结束但锁未释放（僵死锁），立即释放
      （reason=no_active_task，即使锁年龄未达 timeout）；
    - 锁已超时（timeout 自动释放锁）→ 释放（reason=timeout）。
    - 旧纯 JSON 锁（无 flock_active）保持兼容：仅按 timeout/no_active_task 判定，
      不探活、不误清。

    判定依据来自 ledger 中 CLAIMED / REVIEW_CLAIMED 事件的时间戳。

    Returns:
        JSON 含 stuck 列表 + stuck_kind + session_lock 巡检结果。
        exit code 由 CLI 层决定。
    """
    state = store.replay()
    now = datetime.now(timezone.utc)
    stuck: list[dict[str, Any]] = []

    # 读取 ledger 获取事件时间戳
    events = store._read_ledger_lines(from_line=1)
    # 构建 task_id → 最近 CLAIMED / REVIEW_CLAIMED 事件信息
    claim_times: dict[str, datetime] = {}
    review_claims: dict[str, dict[str, Any]] = {}
    for ev in events:
        tid = ev.get("task_id", "")
        ts_str = ev.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ev_time = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ev.get("type") == EVENT_CLAIMED:
            claim_times[tid] = ev_time
        elif ev.get("type") == EVENT_REVIEW_CLAIMED:
            review_claims[tid] = {
                "time": ev_time,
                "event_id": ev.get("event_id", ""),
                "agent": ev.get("agent_id", ""),
            }

    for tid, ts in sorted(state.items()):
        if ts.status == "claimed" and tid in claim_times:
            elapsed = (now - claim_times[tid]).total_seconds() / 60
            if elapsed > timeout_min:
                stuck.append({
                    "task_id": tid,
                    "stuck_kind": "implementer",
                    "claimed_by": ts.claimed_by,
                    "stuck_minutes": round(elapsed, 1),
                    "action": f"建议运行: orchd force-status --task {tid} --status pending",
                })
        elif ts.status == "in_review" and ts.review_claimed_by and tid in review_claims:
            elapsed = (now - review_claims[tid]["time"]).total_seconds() / 60
            if elapsed > timeout_min:
                claim_event_id = review_claims[tid].get("event_id", "")
                if claim_event_id:
                    action = (
                        f"如审查已中断：orchd retract "
                        f"--event {claim_event_id} --reason 'abandoned review'（释放审查认领，"
                        f"任务保持 in_review 可被重新认领）；如审查无法继续也可 "
                        f"orchd force-status --task {tid} --status pending（打回实现，较重）"
                    )
                else:
                    action = (
                        f"审查认领事件缺 event_id，无法直接 retract：可 "
                        f"orchd force-status --task {tid} --status pending（打回实现，较重）"
                    )
                stuck.append({
                    "task_id": tid,
                    "stuck_kind": "reviewer",
                    "review_claimed_by": ts.review_claimed_by,
                    "review_claim_event_id": claim_event_id,
                    "stuck_minutes": round(elapsed, 1),
                    "action": action,
                })

    # Session Identity Layer：僵死 session 检测（task-session-watchdog-stale）。
    # 对 claimed/done/in_review 任务，若实现者或审查者的 session runtime 已缺失、
    # inactive 或过期（TTL 惰性过期：last_seen 超过 TTL 即判过期，判定即生效、
    # 不依赖清理动作），则计入 stale_claims。不自动释放有活跃 session 的任务锁。
    stale_sessions: list[dict[str, Any]] = []
    stale_claims: list[dict[str, Any]] = []
    try:
        from orchd.ledger import is_session_expired, session_runtime_dir

        session_dir = session_runtime_dir(store.orchd_dir)
        sessions_by_id: dict[str, dict[str, Any]] = {}
        if session_dir.exists():
            for f in sorted(session_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                sid = data.get("session_id")
                if sid:
                    sessions_by_id[sid] = data

        for tid, ts in sorted(state.items()):
            if ts.status not in ("claimed", "done", "in_review"):
                continue
            for role, sid, owner in (
                ("implementer", ts.claimed_session, ts.claimed_by),
                ("reviewer", ts.review_claimed_session, ts.review_claimed_by),
            ):
                if not sid:
                    continue
                runtime = sessions_by_id.get(sid)
                reason = ""
                if runtime is None:
                    reason = "missing_runtime"
                elif not runtime.get("active", True):
                    reason = "inactive"
                elif is_session_expired(runtime):
                    # TTL 惰性过期：runtime 仍在且标称 active，但 last_seen 已超
                    # TTL → 宿主会话早已中断（session end 未被调用）。判定即生效，
                    # 过期文件即使仍在磁盘也不计入活跃，交接管流程处置。
                    reason = "session_expired"
                if reason:
                    stale_sessions.append({
                        "session_id": sid,
                        "task_id": tid,
                        "role": role,
                        "owner": owner,
                        "reason": reason,
                    })
                    stale_claims.append({
                        "task_id": tid,
                        "role": role,
                        "owner": owner,
                        "session_id": sid,
                        "reason": reason,
                        "action": _stale_claim_dual_action(tid),
                    })
    except Exception:
        # best-effort：session runtime 不可用时静默降级，不影响既有 watchdog 行为
        pass

    takeover_results: list[dict[str, Any]] = []
    if takeover and stale_claims:
        try:
            from orchd.onboard import force_status

            for claim_info in stale_claims:
                try:
                    result = force_status(
                        store,
                        agent_id=agent_id or "admin",
                        task_id=claim_info["task_id"],
                        target_status="pending",
                        reason="stale session takeover",
                        project_root=project_root,
                    )
                    takeover_results.append({
                        "task_id": claim_info["task_id"],
                        "role": claim_info["role"],
                        "session_id": claim_info["session_id"],
                        "ok": True,
                        "new_status": result.get("new_status"),
                    })
                except OrchdError as exc:
                    takeover_results.append({
                        "task_id": claim_info["task_id"],
                        "role": claim_info["role"],
                        "session_id": claim_info["session_id"],
                        "ok": False,
                        "error": str(exc),
                    })
        except Exception:
            # takeover 属 best-effort：失败不阻断 watchdog 巡检
            pass

    # L2 session 锁巡检：超时自动释放僵死锁
    # 判定语义（审查中锁 ≠ 僵死锁）：锁有效（未超时）时，不能仅凭锁年龄判活，
    # 必须核对「锁持有者(agent_id) + 活跃任务」——
    #   活跃实现   : status == claimed 且 claimed_by == 持有者
    #   活跃审查   : status == in_review 且 review_claimed_by == 持有者
    # 持有者仍有进行中的实现/审查 → 活锁（审查中锁），报告但不释放；
    # 持有者已无活跃任务 → 会话已结束但锁未释放（僵死锁，如已交付实现者
    # 遗留的年轻锁），即使未超时也立即释放，避免误触发 E019 workspace_busy。
    session_lock_stale: dict[str, Any] | None = None
    check = session_lock_check(store.orchd_dir, timeout_min=timeout_min)
    if check.get("locked"):
        lock_agent = check.get("agent_id")
        has_active_task = any(
            (t.claimed_by == lock_agent and t.status == "claimed")
            or (t.review_claimed_by == lock_agent and t.status == "in_review")
            for t in state.values()
        )
        # P0-2：timeout <= 0 语义为"强制清理"——无论持锁者是否有活跃任务，
        # 一律释放锁（用户显式要求强制解锁僵死会话）。
        force_release = timeout_min <= 0
        if force_release or not has_active_task:
            # 锁持有者已无活跃任务（僵死锁）或用户强制清理 → 立即释放
            release = session_lock_release(store.orchd_dir)
            session_lock_stale = {
                "status": "released",
                "reason": "force_release" if force_release else "no_active_task",
                "agent_id": lock_agent,
                "session_id": check.get("session_id") or "",
                "age_min": round(check.get("age_min", 0), 1),
                "release_result": release,
            }
        else:
            # 持有者仍有进行中的实现或审查：审查中锁 ≠ 僵死锁，报告但不释放
            session_lock_stale = {
                "status": "active",
                "agent_id": lock_agent,
                "session_id": check.get("session_id") or "",
                "branch": check.get("branch"),
                "age_min": round(check.get("age_min", 0), 1),
            }
    elif check.get("reason") == "stale_cleaned":
        # 新式 flock 活性锁：持锁进程已死，session_lock_check 已自动清理
        # （复用同一探活逻辑，不再只依赖 timeout/no_active_task）
        session_lock_stale = {
            "status": "released",
            "reason": "stale_os_probe",
            "agent_id": check.get("agent_id") or "",
            "session_id": check.get("session_id") or "",
            "branch": check.get("branch"),
            "age_min": round(check.get("age_min", 0), 1),
            "cleanup_result": check.get("cleanup_result"),
        }
    elif check.get("reason") == "timeout":
        # 锁已超时：自动释放
        release = session_lock_release(store.orchd_dir)
        session_lock_stale = {
            "status": "released",
            "reason": "timeout",
            "age_min": round(check.get("age_min", 0), 1),
            "release_result": release,
        }

    # task-14-worktree-lifecycle（AC5）：孤儿 worktree 惰性清理（best-effort，
    # 绑定任务已终态但 worktree 仍在 → 回收；无独立 worktree 场景零操作）。
    worktree_pruned: dict[str, Any] | None = None
    watchdog_auto_clean: dict[str, Any] | None = None
    if project_root is not None:
        try:
            from orchd.doctor import auto_clean
            from orchd.ledger import resolve_store_dir
            from orchd.worktree import prune_orphans

            pruned = prune_orphans(project_root, resolve_store_dir(store.orchd_dir), state)
            if pruned.get("pruned"):
                worktree_pruned = pruned
            # task-doctor-auto-clean：watchdog 读路径自动清洁（同 status，并入返回）。
            ac = auto_clean(
                project_root,
                emit_stderr=_auto_clean_emit_stderr(project_root),
            )
            if ac.get("auto_cleaned") or ac.get("auto_moved") or ac.get("manual_notice"):
                watchdog_auto_clean = ac
        except Exception:
            pass

    # 准入写锁巡检（task-admission-lock-engine：B 项）：.intake.lock 僵死结构化告警，
    # 不自动强夺——flock 在持锁进程退出时由内核自动释放，无需接管。
    intake_lock_alert: dict[str, Any] | None = None
    try:
        from orchd.ledger import intake_lock_check

        ic = intake_lock_check(store.orchd_dir)
        if ic.get("locked") or ic.get("reason") == "timeout":
            intake_lock_alert = {
                "status": "alert" if ic.get("locked") else "stale_marker",
                "locked": ic.get("locked", False),
                "agent_id": ic.get("agent_id"),
                "age_s": ic.get("age_s"),
                "reason": ic.get("reason"),
                "hint": (
                    "准入写锁 .intake.lock 被持有或残留超时标记。flock 在持锁进程退出时"
                    "自动释放，请勿强行强夺；若持锁进程已僵死，请等待其退出或人工接管后再重试。"
                ),
            }
    except Exception:
        # best-effort：准入锁巡检失败不影响既有 watchdog 行为
        pass

    # 诊断预算：默认聚合视图（total/truncated/top + by_type），--full 回退全量。
    # 排序键：stuck 按 stuck_minutes 降序；stale 按既有确定序（state/tid 序）保持，
    # 不引入新排序以免与 task-output-order-determinism 的确定序冲突。
    # manual 档优先：auto_clean.manual_notice 如存在，按 disposition 置 manual 在前
    # （分档取自 doctor._residue_disposition 单一真源，此处仅排序不另立表）。
    def _stuck_sort_key(it: dict[str, Any]) -> float:
        try:
            return float(it.get("stuck_minutes", 0))
        except (TypeError, ValueError):
            return 0.0

    stuck_agg = _aggregate_top(
        stuck,
        sort_key=_stuck_sort_key,
        group_key=lambda it: it.get("stuck_kind", "unknown"),
    )
    stale_claims_agg = _aggregate_top(
        stale_claims,
        group_key=lambda it: it.get("reason", "unknown"),
    )
    stale_sessions_agg = _aggregate_top(
        stale_sessions,
        group_key=lambda it: it.get("reason", "unknown"),
    )
    if full:
        stuck_field: Any = stuck
        stale_claims_field: Any = stale_claims
        stale_sessions_field: Any = stale_sessions
    else:
        stuck_field = stuck_agg
        stale_claims_field = stale_claims_agg
        stale_sessions_field = stale_sessions_agg

    result = {
        "stuck_tasks": stuck_field,
        "stuck_tasks_summary": stuck_agg,
        "intake_lock": intake_lock_alert,
        "summary": f"{len(stuck)} 个任务可能僵死",
        # 扩展字段（便于脚本集成）
        "stuck_count": len(stuck),
        "timeout_min": timeout_min,
        "session_lock": session_lock_stale,
        "stale_sessions": stale_sessions_field,
        "stale_sessions_summary": stale_sessions_agg,
        "stale_claims": stale_claims_field,
        "stale_claims_summary": stale_claims_agg,
        # task-engine-audit-coverage：任务完整性巡检（只读 best-effort；
        # project_root 为 None / 非 git 时返回 skipped 结构，不影响既有字段）。
        "task_integrity": task_integrity_audit(
            store, tasks, project_root, scope="active"
        ),
    }
    if takeover_results:
        result["takeover_results"] = takeover_results
    if worktree_pruned:
        result["worktree_pruned"] = worktree_pruned
    if watchdog_auto_clean:
        result["auto_clean"] = watchdog_auto_clean
        auto_summary = _auto_clean_disposition_summary(watchdog_auto_clean)
        if auto_summary is not None:
            result["auto_clean_summary"] = auto_summary
    return result
