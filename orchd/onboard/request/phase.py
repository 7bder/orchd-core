"""活跃阶段判定与阶段闸门（active_task_phase/_phase_has_open_task/_apply_phase_gate）。"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.pool import Candidate


def active_task_phase(tasks: list[dict[str, Any]], state: dict[str, Any]) -> str | None:
    """活跃阶段 = tasks[] 首次出现序中首个「存在未完成任务」的阶段；无 phase 任务返回 None。

    阶段优先序按 tasks[] 中 phase 首次出现序（无需新增序数字段，与 IDEAS 条目
    设计一致）。「未完成任务」= 状态不在 TERMINAL_STATUSES（pending/claimed/
    in_review/rework 等均算 open；completed/cancelled 算关闭）。

    **单一真源**：task-phase-status-aggregate（orchd/report.py）须调用本函数，
    不得各写一份活跃阶段判定（P3 的包内扫描用例兜底该约束）。
    """
    seen_order: list[str] = []
    for t in tasks:
        ph = t.get("phase")
        if ph and ph not in seen_order:
            seen_order.append(ph)
    for ph in seen_order:
        if _phase_has_open_task(ph, tasks, state):
            return ph
    return None


def _phase_has_open_task(phase: str, tasks: list[dict[str, Any]], state: dict[str, Any]) -> bool:
    """阶段 ``phase`` 是否存在未完成任务（open）。"""
    for t in tasks:
        if t.get("phase") != phase:
            continue
        ts = state.get(t.get("id", ""))
        s = ts.status if ts else "pending"
        if s not in TERMINAL_STATUSES:
            return True
    return False


def _apply_phase_gate(
    kept: list[Candidate],
    tasks: list[dict[str, Any]],
    state: dict[str, Any],
    explicit_phase: str | None = None,
) -> tuple[str | None, list[Candidate], bool]:
    """阶段闸门：候选跨多阶段时仅保留「活跃阶段 + 中立（无 phase）」候选。

    - ``explicit_phase`` 由 CLI ``--phase`` 显式指定，覆盖自动推进（调用方已
      校验其存在于 phase 取值集合）；为 None 时按 ``active_task_phase`` 自动推进。
    - 中立任务（无 phase）始终保留——永不因闸门被排除（AC3）。
    - 返回 ``(active_phase, gated_kept, phase_gated)``；``phase_gated`` 标记本次
      是否因闸门裁剪过候选（用于响应体的机读上下文）。

    设计要点：闸门是「收益落地机制」——原条目实证「零跨阶段返工」靠串行分阶段
    取得，故多阶段并存时只派活跃阶段任务，避免 agent 跨阶段接活。
    """
    active_phase: str | None
    if explicit_phase is not None:
        active_phase = explicit_phase
    else:
        active_phase = active_task_phase(tasks, state)
    if active_phase is None:
        # 无活跃阶段（无 phase 任务，或所有阶段已关闭）→ 不裁剪，零回归。
        return None, kept, False
    gated = [
        c for c in kept
        if (c.task.get("phase") or None) in (active_phase, None)
    ]
    phase_gated = len(gated) != len(kept)
    return active_phase, gated, phase_gated
