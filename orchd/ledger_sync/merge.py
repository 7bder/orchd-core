"""ledger sync — 事件合并与状态构建域（W6-C2 拆分，纯搬移）。

来源：``orchd/ledger_sync.py``（1622 行，W6-C2 纯搬移）。

``build_state`` / delta 解析 / 事件指纹与确定性 id 注入 / 事件合并 / 状态摘要。

**叶子域**：零包内相对导入。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import validate_event
from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock
from orchd.ledger import (
    _CHECKPOINT_SCHEMA_VERSION,
    _event_target_status,
    _fsync_dir,
    _write_text_atomic,
    Store,
    TaskState,
    event_sort_key,
    resolve_store_dir,
    review_claim_age_s,
    review_stale_timeout_s,
    validate_transition,
)
from orchd.storage import read_archive_events


# ref tree 内 state.json 的快照格式版本（build_state 写入；新设备 bootstrap 校验，
# task-sync-compact-bootstrap-from-state）：加法式演进——补字段不提升版本。
_STATE_SCHEMA_VERSION = 1



# ------------------------------------------------------------------
# 状态派生 / 事件合并（纯逻辑）
# ------------------------------------------------------------------


def build_state(store: Store) -> dict[str, Any]:
    """从本地合并后账本派生 state.json 紧凑任务状态表（O(任务数)）。

    每任务字段：status / attempt_count / updated_event_id + 非空可选字段
    （review_phase / claimed_by / claimed_session / review_claimed_at /
    review_self_review / review_claimed_by / review_claimed_session /
    merge_warning）。后三项由 task-sync-compact-bootstrap-from-state 补齐：
    新设备 bootstrap 以本快照为**唯一**派生态来源（远端 delta 已被 compact
    置空），缺项即静默丢状态，故字段集须与 ``TaskState`` 对齐。
    ``updated_event_id`` 为该任务最近一次事件 id（正序扫描后者覆盖）。
    """
    state = store.replay()
    last_event: dict[str, str] = {}
    if store.ledger_exists():
        for ev in store.backend.read_events():
            tid = ev.get("task_id")
            eid = ev.get("event_id")
            if tid and eid:
                last_event[tid] = eid
    tasks: dict[str, dict[str, Any]] = {}
    for tid, ts in sorted(state.items()):
        entry: dict[str, Any] = {
            "status": ts.status,
            "attempt_count": ts.attempt_count,
            "updated_event_id": last_event.get(tid),
        }
        for key in ("review_phase", "claimed_by", "claimed_session",
                    "review_claimed_at", "review_self_review",
                    "review_claimed_by", "review_claimed_session",
                    "merge_warning"):
            val = getattr(ts, key, None)
            if val:
                entry[key] = val
        tasks[tid] = entry
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "generated_at":
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": tasks,
    }



def _parse_delta_events(text: str | None) -> tuple[list[dict[str, Any]], int]:
    """解析 delta.jsonl 文本为事件列表（空/None → 空列表）。

    非 dict 行（坏 JSON / 数字 / 字符串 / 数组 / null）显式跳过并计数，
    不再让 AttributeError 从 ``_merge_events_detailed`` 的 ``ev.get``
    逃逸导致整批同步中止。

    Returns:
        ``(events, malformed_skipped)``：事件列表与跳过的脏行数。
    """
    if not text or not text.strip():
        return [], 0
    events: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        events.append(obj)
    return events, skipped



def _event_fingerprint(ev: dict[str, Any]) -> str:
    """事件内容确定性指纹（排除 event_id 本身）。

    用于同 event_id 异内容且排序键全等时的确定性择一（NEW-L4）：
    指纹较小者保留，不依赖各机输入序（local 在前 / remote 在前）。
    """
    payload = {k: v for k, v in ev.items() if k != "event_id"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()



def _inject_deterministic_ids(events: list[dict[str, Any]]) -> int:
    """为缺失 event_id 的事件注入确定性 id（NEW-L3），返回注入条数。

    无 id 的远端事件此前被 new_remote 过滤静默丢弃，compact 清 delta 后
    从远端永久消失。改为基于内容哈希生成 ``evt-sync-<hash12>`` 注入，
    使其能参与常规去重合并路径；同一无 id 事件在各机生成相同 id，
    跨设备幂等。
    """
    count = 0
    for ev in events:
        if not ev.get("event_id"):
            ev["event_id"] = f"evt-sync-{_event_fingerprint(ev)[:12]}"
            count += 1
    return count



def _merge_events_detailed(
    events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 timestamp 稳定归并 + event_id 去重合并，并检出 id 碰撞（L-9）。

    两设备合并后的事件集相同、全局序相同 → replay 结果收敛一致。

    排序语义（L-2，用户裁决 A）：排序键为 ``ledger.event_sort_key``，即
    ``(aware 绝对时刻, 写入方标识, event_id)``：

    ① 旧实现直接对 timestamp **字符串**排序——带本地 offset 的字符串按字典序
    比较会跨时区因果倒置（``-08:00`` 机与 ``+09:00`` 机收敛到不同状态），
    「幂等收敛」契约不成立；
    ② 现按 aware 绝对时刻比较（``parse_event_time``），跨时区天然可比；旧本地
    时区/秒精度事件按带偏移绝对时刻参与比较，**不重写历史事件**；
    ③ 第二元为写入方标识（``session_id`` 回退 ``agent_id``），保证跨设备同瞬间
    事件的序一致；第三元 ``event_id`` **仅对亚秒精度事件生效**：秒精度旧事件
    保留输入顺序（稳定排序），以免按随机 id 打乱同写入方事件的语义顺序
    （CLAIMED→DONE→REVIEW_READY，2026-09-10 实踩防线；实测 48% 历史事件与
    同写入方事件撞秒，其中 382 组会被 event_id 序倒置）。
    输入约定：``local_events + new_remote``——本地在前、远端增量在后；排序键为
    事件的纯函数 + 稳定排序，故同一事件集合在任意设备上归并结果逐元素相同。

    id 碰撞检出（L-9）：event_id 相同但 ``task_id`` / ``type`` 不同 → 判为
    碰撞（旧 32 bit id 生日碰撞的典型表现），产出 E030 告警条目，**不再
    静默丢弃**（去重仍保留先到者，语义不变）。同 id 同 task/type 的重复
    行按既有语义静默去重（幂等重放，非碰撞）。

    无 event_id 的事件保守保留（不参与去重，防审计丢失）。

    Returns:
        ``(merged_events, collisions)``；collisions 为 E030 结构告警列表。
    """
    seen: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []
    for ev in sorted(
            events,
            key=event_sort_key,
    ):
        eid = ev.get("event_id", "")
        if eid:
            prev = seen.get(eid)
            if prev is not None:
                is_collision = (prev.get("task_id"),
                                prev.get("type")) != (ev.get("task_id"),
                                                      ev.get("type"))
                # NEW-L4：仅当排序键全等时才用指纹确定性择一（不依赖输入序）；
                # 排序键不同时先到者由排序决定（确定性），保留原语义。
                if event_sort_key(ev) == event_sort_key(prev):
                    if _event_fingerprint(ev) < _event_fingerprint(prev):
                        idx = out.index(prev)
                        out[idx] = ev
                        seen[eid] = ev
                        kept, dropped = ev, prev
                    else:
                        kept, dropped = prev, ev
                    tiebreak_msg = "已按内容指纹确定性保留"
                else:
                    kept, dropped = prev, ev
                    tiebreak_msg = "已保留先到事件"
                if is_collision:
                    collisions.append({
                        "code":
                        ErrorCode.E030.name,
                        "severity":
                        "warning",
                        "message": (f"event_id 碰撞：{eid} 同时对应不同 task/type，"
                                    f"疑似 32 bit id 生日碰撞；{tiebreak_msg}，请人工核对"),
                        "event_id":
                        eid,
                        "kept": {
                            "task_id": kept.get("task_id"),
                            "type": kept.get("type"),
                        },
                        "dropped": {
                            "task_id": dropped.get("task_id"),
                            "type": dropped.get("type"),
                        },
                    })
                continue
            seen[eid] = ev
        out.append(ev)
    return out, collisions



def _merge_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``_merge_events_detailed`` 的兼容包装（仅返回合并结果）。"""
    return _merge_events_detailed(events)[0]



def _events_after(events: list[dict[str, Any]],
                  last_pushed_event_id: str | None) -> list[dict[str, Any]]:
    """取 marker（已推送事件 id）之后的事件；marker 缺失/找不到 → 全量（保守）。"""
    if not last_pushed_event_id:
        return list(events)
    for i, ev in enumerate(events):
        if ev.get("event_id") == last_pushed_event_id:
            return events[i + 1:]
    return list(events)



def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """从远端 state.json 派生进度摘要（供 pull 响应展示）。"""
    tasks = state.get("tasks") or {}
    counts: dict[str, int] = {}
    for ts in tasks.values():
        status = ts.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"task_total": len(tasks), "by_status": counts}



def _claim_stale_timeout_s() -> float:
    """CLAIMED 超时阈值（复用本地 E034 判据，避免二套阈值）。

    延迟导入 orchd.onboard.control 规避模块加载期依赖环（control 处于更高层）。
    """
    from orchd.onboard.control import claim_stale_timeout_s

    return claim_stale_timeout_s()



def _trunc(value: Any, limit: int = 200) -> Any:
    """拒收明细回显截断（_parse_delta_events 无体积上限，防超长字段灌响应）。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value
