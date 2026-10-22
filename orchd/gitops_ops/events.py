"""gitops_ops events 域 — 时间戳与事件构造。

来源：``orchd/gitops_ops.py（1220 行）``（W6-C6 纯搬移）。

``now_iso`` 的进程内严格单调保证与 ``make_event`` 的标准事件 schema。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from orchd.ledger import generate_event_id, resolve_session_identity


# 进程内严格单调保证的状态（L-2）：同进程连续生成的时间戳严格递增，
# 使同一写入方的事件永不落入归并平局分支（见 event_sort_key 的第三元）。
_LAST_TIMESTAMP: datetime | None = None


_TS_LOCK = threading.Lock()


def now_iso() -> str:
    """返回当前时刻的 UTC ISO 8601 字符串（微秒精度，进程内严格单调）。

    历史实现为「本地时区 + 秒精度」（``datetime.now(timezone.utc).astimezone()
    .isoformat(timespec="seconds")``）。该字符串被 ``ledger_sync`` 当作**跨设备
    归并的排序键第一元**，而带本地 offset 的字符串按字典序比较会跨时区因果倒置
    （``-08:00`` 机器的 ``00:00:00+08:00`` 会被排到 ``17:00:00+09:00`` 之后），
    两机因此收敛到不同状态、「幂等收敛」契约失效（task-ledger-order-determinism
    L-2；用户裁决 A —— 按 AC 原样执行）。

    现口径：

    - **UTC 绝对时刻**：``2026-09-15T05:40:12.123456+00:00``（不再依赖本地时区，
      归并侧按 aware 绝对时刻比较，跨时区天然可比）；
    - **微秒精度**：同秒事件不再并列（旧秒精度下 48% 的历史事件与同写入方事件撞秒）；
    - **进程内严格单调**：同一进程内若当前时刻 <= 上次发出的时刻（同微秒连续调用、
      时间回拨），自动 +1µs 递增，保证同一写入方事件绝不共享时间戳 —— 这使归并
      排序键的平局分支只在跨写入方时生效，从而既满足跨设备确定性归并，又不破坏
      同写入方事件的语义顺序（CLAIMED→DONE→REVIEW_READY；2026-09-10 实踩防线）。

    兼容性：旧本地时区/秒精度事件**原样保留不重写**，归并侧按带偏移绝对时刻比较
    （见 ``orchd.ledger.parse_event_time``）。
    """
    global _LAST_TIMESTAMP
    with _TS_LOCK:
        ts = datetime.now(timezone.utc)
        if _LAST_TIMESTAMP is not None and ts <= _LAST_TIMESTAMP:
            ts = _LAST_TIMESTAMP + timedelta(microseconds=1)
        _LAST_TIMESTAMP = ts
    return ts.isoformat(timespec="microseconds")


def make_event(
    task_id: str, agent_id: str, etype: str, **extra: Any
) -> dict[str, Any]:
    """构造标准事件字典，用于追加到 ledger（与 onboard._make_event 行为一致）。

    事件 schema 字段：
        v          - 事件版本号（当前固定为 1），便于未来 schema 演进时做兼容判断。
        event_id   - 全局唯一事件 ID，由 generate_event_id() 生成。
        timestamp  - 事件发生的本地时区 ISO 8601 时间戳（精确到秒）。
        task_id    - 关联的任务 ID。
        agent_id   - 触发此事件的 agent 标识。
        type       - 事件类型（如 CLAIMED / DONE / REVIEW_SUBMITTED / FORCE_STATUS 等）。

    **extra 中的键值对会直接合并到事件字典，用于各类型事件的差异化字段
    （如 changes_description、verdict、target_status 等）。
    """
    session_identity = resolve_session_identity()
    ev = {
        "v": 1,
        "event_id": generate_event_id(),
        "timestamp": now_iso(),
        "task_id": task_id,
        "agent_id": agent_id,
        "type": etype,
        "session_id": session_identity["session_id"],
    }
    ev.update(extra)
    return ev
