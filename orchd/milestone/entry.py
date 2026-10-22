"""对外入口：milestone_names / build_context / evaluate（编排器）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from .constants import EXIT_VERSIONS, MILESTONE_IDS
from .context import MilestoneContext
from .table import MILESTONES


def milestone_names() -> list[str]:
    """可用里程碑名（CLI choices 单一来源）。"""
    return sorted(MILESTONES)


def build_context(
    *,
    orchd_dir: Path,
    tasks: list[dict[str, Any]],
    store: Any,
) -> MilestoneContext:
    """构造判据上下文（master 目录 / 主工作树 / 账本状态）。"""
    orchd_dir = Path(orchd_dir)
    # task-master-path-residual-convergence：经单一真源解析（本地优先 →
    # canonical 回退；read_layout best-effort，损坏按缺失处理，故无需 try）。
    from orchd.worktree import resolve_master_path_from_dir

    master_path = resolve_master_path_from_dir(orchd_dir)
    try:
        state = store.replay()
    except Exception:  # noqa: BLE001 - 无账本时按空状态求值（A 组会如实报 not_ready）
        state = {}
    return MilestoneContext(
        project_root=master_path.parent.parent,
        master_dir=master_path.parent,
        store=store,
        tasks=tasks,
        state=state,
    )


def evaluate(milestone: str, ctx: MilestoneContext) -> dict[str, Any]:
    """求值一个里程碑的全部判据，返回结构化结论（**纯读**，不写任何状态）。

    Args:
        milestone: ``freeze`` / ``split-ready``（见 :func:`milestone_names`）。
        ctx: 判据上下文。

    Returns:
        ``{"milestone", "milestone_id", "title", "exit_version", "ready",
        "summary", "groups", "not_ready", "single_source"}``。
    """
    if milestone not in MILESTONES:
        raise OrchdError(
            ErrorCode.E007,
            f"unknown_milestone: 未知里程碑 '{milestone}'",
            [{
                "milestone": milestone,
                "available": milestone_names(),
                "hint": "用法：orchd milestone-check freeze | split-ready",
            }],
        )
    spec = MILESTONES[milestone]
    groups: list[dict[str, Any]] = []
    not_ready: list[dict[str, Any]] = []
    total = 0
    ready_count = 0
    for gid, gtitle, checks in spec["groups"]:
        items: list[dict[str, Any]] = []
        for check in checks:
            total += 1
            try:
                ok, detail, hint = check.fn(ctx)
            except Exception as exc:  # noqa: BLE001 - 判据故障按未达成并留痕，不吞
                ok, detail, hint = False, f"判据故障：{type(exc).__name__}: {exc}", (
                    "判据自身异常按未达成处理（fail-closed）；请修判据实现或环境后重试"
                )
            if ok:
                ready_count += 1
            else:
                not_ready.append({
                    "id": check.id, "group": gid, "title": check.title,
                    "detail": detail, "hint": hint,
                })
            items.append({"id": check.id, "title": check.title, "ready": ok,
                          "detail": detail, "hint": hint})
        groups.append({
            "id": gid,
            "title": gtitle,
            "ready": all(i["ready"] for i in items),
            "checks": items,
        })
    ready = ready_count == total
    return {
        "milestone": milestone,
        "milestone_id": MILESTONE_IDS[milestone],
        "title": spec["title"],
        "exit_version": EXIT_VERSIONS[milestone],
        "ready": ready,
        "summary": {"total": total, "ready": ready_count,
                    "not_ready": total - ready_count},
        "groups": groups,
        "not_ready": not_ready,
        "single_source": "orchd/milestone.py（判据清单只在此定义；ROADMAP 只引用不复述）",
    }
