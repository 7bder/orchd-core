"""report — status 命令域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

``status`` 主流程（阶段聚合 + 文本渲染 + 自动清理 stderr 提示）。

依赖 ``text`` / ``checks`` / ``phase``（顶层导入，单向）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root
from .checks import (
    _scan_revive_markers,
)

from .phase import (
    phase_aggregate,
)

from .text import (
    _auto_clean_emit_stderr,
    _format_auto_clean_text,
    _format_phase_text,
    _format_text,
)




def status(
    store: Store,
    tasks: list[dict[str, Any]],
    project: dict[str, Any] | None = None,
    text: bool = False,
    task_id: str | None = None,
    project_root: Path | None = None,
    active_only: bool = True,
    state: dict[str, Any] | None = None,
    phase: bool = False,
    active_phase: str | None = None,
) -> dict[str, Any]:
    """全局状态快照；给定 task_id 时返回单任务详情。

    当 text=True 时，返回结果中额外包含 ``_text`` 字段，其值为人类可读的
    纯文本表格（由 _format_text 生成），供 CLI 层直接打印，不混入 JSON 输出。

    Args:
        active_only: 仅输出活跃任务（status ∈ pending/claimed/done/in_review），
            终态 completed/cancelled 默认隐藏（task-status-active-only）。
            传 False 回退全量。summary.total 恒为全量任务数，另增 active（活跃数）。
        state: 可选的预计算 replay 状态（task-replay-command-scoped-once）。单命令
            链路内多个消费者共用同一 snapshot 时由调用方传入，避免重复全解析；
            缺省 None → 自行 ``store.replay()``（既有调用点零回归）。

    Returns:
        JSON 结构：project + tasks[] + summary（total + active + 六状态计数）；
        或单任务详情 {"task": {...}}。

    Raises:
        OrchdError E005: task_id 在 master 中不存在。
    """
    if state is None:
        state = store.replay()
    # 复活标记（task-force-status-revive-audit）：扫描 ledger 检测 completed→pending
    # 复活操作，对曾被复活的任务附加 revive_marker（reason+evidence_sha+时间），
    # 正常任务不附加（零误伤）。
    try:
        revive_by_task = _scan_revive_markers(store)
    except Exception:
        revive_by_task = {}
    task_map = {t.get("id", ""): t for t in tasks}

    if task_id is not None:
        task_def = task_map.get(task_id)
        if task_def is None:
            raise OrchdError(
                ErrorCode.E005,
                f"task '{task_id}' not found in master",
                [{"task_id": task_id}],
            )
        ts = state.get(task_id)
        detail: dict[str, Any] = {
            "id": task_id,
            "name": task_def.get("name", ""),
            "brief": task_def.get("brief", ""),
            "status": ts.status if ts else "pending",
            "module": task_def.get("module", ""),
            "depends_on": task_def.get("depends_on", []),
            "importance": task_def.get("importance", "normal"),
            "reviewers": task_def.get("reviewers", []),
            "verify_command": task_def.get("verify_command", ""),
            # task-hostfix-pack-a B1：单任务详情含声明域（看完整卡面不再绕道读
            # _master.json；加法字段，零破坏）。
            "files_to_edit": task_def.get("files_to_edit", []),
            "acceptance_criteria": task_def.get("acceptance_criteria", []),
        }
        if ts:
            if ts.claimed_by:
                detail["claimed_by"] = ts.claimed_by
            if ts.review_phase:
                detail["review_phase"] = ts.review_phase
            if ts.review_claimed_by:
                detail["review_claimed_by"] = ts.review_claimed_by
            # B1（2026-08-13 full-audit-v2）：completed 但 merge 未落地时展示
            # merge_warning，供 audit-merge 告警后的「人工确认 main 是否含实现」直接查看
            if ts.merge_warning:
                detail["merge_warning"] = ts.merge_warning
            detail["attempt_count"] = ts.attempt_count
            if task_id in revive_by_task:
                detail["revive_marker"] = revive_by_task[task_id]
        # task-status-text-flag-silent-noop：单任务详情也支持 --text，
        # 构造兼容 _format_text 的最小结构（tasks=[detail] + summary），
        # 避免提前 return 跳过 _text 生成导致 --text 恒输出 JSON。
        if text:
            _s = detail.get("status", "pending")
            # _format_text 期望 task_id 字段，而单任务 detail 用 id，做一次映射
            _row = dict(detail)
            _row["task_id"] = _row.get("id", task_id)
            _single = {
                "tasks": [_row],
                "summary": {
                    "total": 1, "active": 1 if _s in ("pending", "claimed", "done", "in_review") else 0,
                    "pending": 1 if _s == "pending" else 0,
                    "claimed": 1 if _s == "claimed" else 0,
                    "done": 1 if _s == "done" else 0,
                    "in_review": 1 if _s == "in_review" else 0,
                    "completed": 1 if _s == "completed" else 0,
                    "cancelled": 1 if _s == "cancelled" else 0,
                },
            }
            return {"task": detail, "_text": _format_text(_single)}
        return {"task": detail}

    task_statuses: list[dict[str, Any]] = []
    counts = {"pending": 0, "claimed": 0, "done": 0, "in_review": 0, "completed": 0, "cancelled": 0}

    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        s = ts.status if ts else "pending"
        counts[s] = counts.get(s, 0) + 1
        # task-status-active-only：默认仅输出活跃任务（非终态），
        # 终态 completed/cancelled 在 tasks[] 中隐藏；summary 计数不受影响。
        if active_only and s in TERMINAL_STATUSES:
            continue
        entry: dict[str, Any] = {
            "task_id": tid,
            "name": task.get("name", ""),
            "status": s,
            "module": task.get("module", ""),
            "attempt_count": ts.attempt_count if ts else 0,
            "source": task.get("source"),
        }
        if ts and ts.claimed_by:
            entry["claimed_by"] = ts.claimed_by
        if ts and ts.review_phase:
            entry["review_phase"] = ts.review_phase
        if ts and ts.review_claimed_by:
            entry["review_claimed_by"] = ts.review_claimed_by
        if tid in revive_by_task:
            entry["revive_marker"] = revive_by_task[tid]
        task_statuses.append(entry)

    # task-status-active-only：summary 增 active 计数且保留 total。
    # active = 活跃任务数（非终态）；total 恒为全量任务数。
    summary = {
        "total": len(tasks),
        "active": sum(
            counts[k] for k in ("pending", "claimed", "done", "in_review")
        ),
        **counts,
    }
    result: dict[str, Any] = {
        "project": dict(project) if project else {},
        "tasks": task_statuses,
        "summary": summary,
    }

    # task-14-worktree-lifecycle（AC5）：孤儿 worktree 惰性清理（best-effort，
    # 绑定任务已终态但 worktree 仍在 → 回收；无独立 worktree 场景零操作）。
    if project_root is not None:
        try:
            from orchd.doctor import auto_clean
            from orchd.ledger import resolve_store_dir
            from orchd.worktree import prune_orphans

            pruned = prune_orphans(project_root, resolve_store_dir(store.orchd_dir), state)
            if pruned.get("pruned"):
                result["worktree_pruned"] = pruned
            # task-doctor-auto-clean：读路径自动清洁（低风险残留自动清理，
            # legacy 移入备份区，高风险仅报告；有过拓扑才并入结果，保持无残留时干净）。
            ac = auto_clean(
                project_root,
                emit_stderr=_auto_clean_emit_stderr(project_root),
            )
            if ac.get("auto_cleaned") or ac.get("auto_moved") or ac.get("manual_notice"):
                result["auto_clean"] = ac
        except Exception:
            pass

    if text:
        result["_text"] = _format_text(result)
        ac_text: Any = result.get("auto_clean")
        if ac_text:
            result["_text"] += _format_auto_clean_text(ac_text)

    # task-phase-status-aggregate：阶段聚合视图（仅 --phase 时附加，零回归）
    if phase:
        agg = phase_aggregate(tasks, state, active_phase)
        result["active_phase"] = agg["active_phase"]
        result["phases"] = agg["phases"]
        if text:
            result["_text"] += _format_phase_text(agg["phases"], agg["active_phase"])

    return result
