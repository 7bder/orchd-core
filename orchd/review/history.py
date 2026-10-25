"""账本审查历史提取（_recent_transitions/extract_review_comments/history/last_done/find_last_done_event/baseline），叶域。"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import (
    EVENT_DONE,
    EVENT_REVIEW_CLAIMED,
    EVENT_REVIEW_SUBMITTED,
)
from orchd.ledger import Store
from orchd.ledger import TaskDerived


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
        if ev.get("task_id") != task_id or ev.get("type") != EVENT_REVIEW_SUBMITTED:
            continue
        if ev.get("comments"):
            comments.append(ev["comments"])
        # task-review-comments-gate-and-stale-timeout（B）：历史空打回事件
        # （CHANGES_REQUESTED 但无 comments）注入占位，避免返工时 review_comments=[]
        # 导致实现者看不到任何意见。A 的强制门已阻止新空打回，此处仅兜底历史数据。
        elif ev.get("verdict") == "CHANGES_REQUESTED":
            comments.append(
                "[该次打回未附审查意见（历史数据），请联系审查者补充；"
                "当前版本已强制要求 CHANGES_REQUESTED 必须附意见]"
            )
    return comments


def extract_review_history(store: Store, task_id: str) -> list[dict[str, Any]]:
    """该任务全部 REVIEW_SUBMITTED 的结构化意见（只读回看，task-review-comments-readback）。

    每项 {"review_type", "verdict", "timestamp", "comments"}，与 claim 侧
    extract_review_comments 同源扫描（空打回历史占位口径一致）。
    只读：不写事件、不改任务状态；completed（含归档）任务同样可读。
    """
    history: list[dict[str, Any]] = []
    if not store.ledger_exists():
        return history
    events = store._read_ledger_lines(from_line=1)
    for ev in events:
        if ev.get("task_id") != task_id or ev.get("type") != EVENT_REVIEW_SUBMITTED:
            continue
        if ev.get("comments"):
            body = ev["comments"]
        # 空打回历史占位口径与 extract_review_comments 一致（此处只读呈现）。
        elif ev.get("verdict") == "CHANGES_REQUESTED":
            body = (
                "[该次打回未附审查意见（历史数据），请联系审查者补充；"
                "当前版本已强制要求 CHANGES_REQUESTED 必须附意见]"
            )
        else:
            continue
        history.append({
            "review_type": ev.get("review_type"),
            "verdict": ev.get("verdict"),
            "timestamp": ev.get("timestamp"),
            "comments": body,
        })
    return history


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
        if ev.get("task_id") == task_id and ev.get("type") == EVENT_DONE:
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
        if ev.get("task_id") == task_id and ev.get("type") == EVENT_DONE:
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
            event.get("type") == EVENT_REVIEW_CLAIMED
            and event.get("task_id") == task_id
            and event.get("agent_id") == agent_id
        ):
            return event.get("baseline_sha")
    return None
