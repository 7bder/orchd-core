"""report — 阶段聚合域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

``phase_aggregate`` / top-N 聚合 / 阶段判定（status 与 watchdog 的共同消费者，故独立成域）。

**叶子域**：零包内相对导入。
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



# 诊断输出预算（task-diagnostics-aggregate-budget）：默认聚合 top-N + --full 逃生舱。
# 聚合分档复用既有单一真源，不另立分档表：
# - watchdog 明细分档复用自身已有字段（stuck_kind / reason）；
# - auto_clean 档位复用 orchd.doctor._residue_disposition + AUTO_CLEAN_DISPOSITIONS，
#   manual 档优先入选 top-N。
_DIAGNOSTICS_TOP_N = 5



def _aggregate_top(
    items: list[dict[str, Any]],
    *,
    sort_key=None,
    group_key=None,
    top_n: int = _DIAGNOSTICS_TOP_N,
) -> dict[str, Any]:
    """通用聚合视图：{total, truncated, top, by_type}。

    Args:
        items: 全量明细（已按确定序排列，top 截取保持该序的头部语义）。
        sort_key: 可选二次排序键；为 None 时保持输入序（调用方保证确定序）。
        group_key: 分组计数键；为 None 时不产出 by_type。
        top_n: top 保留数，缺省 5。

    Returns:
        {"total": N, "truncated": bool, "top": [...], "by_type": {...}}，
        空列表时 by_type 为 {}。
    """
    total = len(items)
    ordered = sorted(items, key=sort_key, reverse=True) if sort_key is not None else list(items)
    top = ordered[:top_n]
    agg: dict[str, Any] = {
        "total": total,
        "truncated": total > top_n,
        "top": top,
    }
    if group_key is not None:
        counts: dict[str, int] = {}
        for it in items:
            try:
                k = str(group_key(it))
            except Exception:
                k = "unknown"
            counts[k] = counts.get(k, 0) + 1
        agg["by_type"] = counts
    else:
        agg["by_type"] = {}
    return agg



# 阶段聚合：活跃态由 total - completed - pending 派生，不另立集合常量。


def _task_phase_status(task: dict[str, Any], state: dict[str, Any]) -> str:
    """任务在 replay state 中的状态；未 claim（无 ledger 条目）视为 pending。"""
    ts = state.get(task.get("id", ""))
    return ts.status if ts else "pending"



def phase_aggregate(
    tasks: list[dict[str, Any]],
    state: dict[str, Any],
    active_phase: str | None = None,
) -> dict[str, Any]:
    """按 phase 分组输出每阶段 total / completed / active / pending 计数。

    阶段顺序按 tasks[] 中 phase 首次出现序（与 active_task_phase 同序）。
    分组口径复用 ``_aggregate_top`` 的 group_key 机制（phase 取 tasks[].phase，
    缺省归中立 None）；每阶段 ``total`` 直接取 ``_aggregate_top`` 同参 ``by_type``
    （AC3 口径复用，不手写第二套分组循环）。

    **活跃阶段单一真源**：``active_phase`` 由调用链经
    ``orchd.onboard.request.active_task_phase`` 计算并传入；本函数**不自行判定**
    活跃阶段——report.py 依赖方向禁止 import onboard，且保证 orchd/ 内活跃阶段
    判定唯一真源不被复制到 report 侧（AC1 / 单一真源）。中立（无 phase）任务
    不进入 phases 聚合数组（phases 是「阶段」视图），其计数仍体现在 summary /
    tasks[] 段，不改变既有结构（AC5 零回归）。
    """
    order: list[str] = []
    for t in tasks:
        ph = t.get("phase")
        if ph and ph not in order:
            order.append(ph)
    # 复用 _aggregate_top 的 group_key 口径（与 _aggregate_top 同参）
    groups = _aggregate_top(
        tasks, group_key=lambda t: t.get("phase") or None
    )["by_type"]
    phases: list[dict[str, Any]] = []
    for ph in order:
        total = groups.get(ph, 0)
        if total == 0:
            continue
        group_tasks = [t for t in tasks if t.get("phase") == ph]
        completed = sum(
            1 for t in group_tasks
            if _task_phase_status(t, state) in TERMINAL_STATUSES
        )
        pending = sum(
            1 for t in group_tasks
            if _task_phase_status(t, state) == "pending"
        )
        active = total - completed - pending
        phases.append({
            "phase": ph,
            "total": total,
            "completed": completed,
            "active": active,
            "pending": pending,
        })
    return {"active_phase": active_phase, "phases": phases}
