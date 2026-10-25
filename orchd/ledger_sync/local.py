"""ledger sync — 本地账本重建与调和域（W6-C2 拆分，纯搬移）。

来源：``orchd/ledger_sync.py``（1622 行，W6-C2 纯搬移）。

哨兵 checkpoint / 本地账本重建 / 从远端状态引导本地 / ``_reconcile_local``。

依赖 ``refs`` / ``merge`` / ``inbound``（顶层导入，单向）。
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
from .refs import (
    _store_runtime_root,
)

from .merge import (
    _STATE_SCHEMA_VERSION,
    _merge_events_detailed,
)

from .inbound import (
    _validate_inbound_events,
)
from orchd.contracts.responses import (
    MESSAGE_KEY,
    TASKS_KEY,
)




# ------------------------------------------------------------------
# 订阅命令（push / pull / compact）
# ------------------------------------------------------------------


def _write_sentinel_checkpoint(store: Store) -> None:
    """replace 前写入哨兵 checkpoint：声明「0 行 + 空快照」（L-1）。

    ``_rebuild_local_ledger`` 在 replace 之后才调 ``update_checkpoint``；若进程
    在两者之间崩溃/掉电，旧 checkpoint 声明的 ``ledger_line`` 会与新账本错配
    （快照不一致告警、甚至状态回退）。先把 checkpoint 降为哨兵（0 行 + 空任务），
    崩溃后 replay 必从第 1 行**全量重放**，绝不丢事件、也不会错配。

    哨兵本身不是错误状态：它只是「重建进行中」的标记，任何一次成功重建或后续
    写路径都会立刻以真实快照覆盖它。写入失败静默降级（best-effort：宁可不写
    哨兵，也不能阻断重建）。

    Args:
        store: 目标 Store。
    """
    try:
        payload: dict[str, Any] = {
            "ledger_line": 0,
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            TASKS_KEY: {},
            "sentinel": "ledger_rewrite_in_progress",
        }
        # 快照基随哨兵原样存续（task-sync-compact-bootstrap-from-state）：重建尾部的
        # update_checkpoint 以「现存 checkpoint」判定是否保留它，哨兵若抹掉它，
        # 基就在重建过程中丢失、下一次重建退回空基 replay_full。
        prev_ckpt = store.backend.load_checkpoint() or {}
        if prev_ckpt.get("bootstrap_tasks") is not None:
            payload["bootstrap_tasks"] = prev_ckpt["bootstrap_tasks"]
        store.backend.save_checkpoint(payload)
    except (OSError, OrchdError):
        pass



def _rebuild_local_ledger(store: Store, events: list[dict[str, Any]]) -> int:
    """把合并后（去重排序）事件原子重写本地 ledger 并重建 checkpoint。

    ledger 为事件审计唯一权威：重写保留全部事件、仅规范化确定性全局序，
    随后 replay_full → update_checkpoint 重建快照（O(任务数) checkpoint）。

    写路径走 StorageBackend 端口（task-sync-sqlite-backend-support，B-max
    阶段 3）：``replace_active_events`` 全量替换活跃事件（文件后端原子重写
    活跃文件，sqlite 后端单事务重建事件表并同步计数器）——旧实现在此处做 raw
    文件手术（拼 jsonl → tmp → ``_atomic_replace`` 到 ``ledger_path``），在
    sqlite 下会把合并事件写进读侧不可见的遗留 jsonl（静默丢数据），故此前整
    个 sync 对非文件后端 E007 硬拒。端口化后双后端同一语义。

    落盘序列（L-1）：（哨兵 checkpoint）→ 端口替换 → 父目录 fsync
    → ``update_checkpoint``。要点：
    - replace 前写哨兵 → 中途崩溃只会触发全量 replay，不会「旧行号 + 新账本」错配；
    - 端口替换内部保证原子（文件 tmp+replace / sqlite 单事务），父目录 fsync
      保住目录项持久化（与旧序列同耐久）；
    - 崩溃后 checkpoint 停在哨兵（0 行）→ replay 必从第 1 行全量重放。

    归档前缀不得入活跃段（task-sync-rebuild-active-suffix）：入参 ``events`` 是
    **逻辑**事件流（读侧 = 归档前缀 + 活跃后缀，见 ``StorageBackend.read_events``
    的 ``filesystem`` 实现），而 ``replace_active_events`` 的契约只接**活跃后缀**
    （口径真源 ``orchd/storage/__init__.py``），两后端均按「归档解析数 + 本次重插
    数」记逻辑总数。喂全量会把归档段再写进活跃载体，于是后端计数器把归档事件
    重复计入（HEAD 实测 archive=10 / merged=12 时 ``event_count()`` 由 12 变 22），
    而读侧按 event_id 去重仍只见 12 条——计数器与 ``read_events()`` 背离、
    ``read_events(from, to)`` 窗口起点错位（HEAD 实测窗口只返回归档段尾部，其后的
    活跃后缀整段不见）。事件本身不丢（全量 ``replay_full()`` 恒可见）；增量
    ``replay()`` 的回退触发只以 ``orchd/ledger.py::_load_checkpoint``（:1909）
    所列两支为准（checkpoint 解析失败回退支、compact journal 在位支，
    :1926-:1948），本文件不断言第三支。故此处按归档 event_id
    **集合**切出活跃后缀（不按位置切片：合并序下归档与活跃事件可交错），并把 H4
    计数器落到「归档解析数 + 活跃后缀数」，与 ``Store.compact_archive`` 的同名赋值
    同口径（``_line_count = len(moved) + len(suffix)``，见 ``orchd/ledger.py``）——
    ``_line_count`` 语义是**逻辑总数**，不是活跃行数。

    bootstrap 快照接力（task-sync-compact-bootstrap-from-state）：本地事件流可能
    只是「compact 之后的新事件」（新设备 bootstrap 后本地账本为空，之后 pull 到
    的事件不带 pre-compact 前缀）。此时从空基 ``replay_full()`` 重建会冲掉快照
    派生态，故先取现存 checkpoint 的 ``bootstrap_tasks``（快照基键，普通
    checkpoint 无此键）：命中则以该 pre-compact 原始快照为
    ``_rebuild_after_retract`` 的基叠加本地事件；否则维持原 ``replay_full()``
    语义。

    Returns:
        本地逻辑事件总数（归档解析数 + 活跃后缀数）。
    """
    root = _store_runtime_root(store)
    root.mkdir(parents=True, exist_ok=True)
    # 基必须在写哨兵**之前**取——哨兵会把 checkpoint 覆盖成空快照。判据是
    # checkpoint 的 `bootstrap_tasks`（快照基），**不是** ledger_line==0：接力重建
    # 自身会把 ledger_line 抬到物理行数，用行号判定只能护住首次承载内容的 pull。
    # 另一关键：基恒取**原始快照** `bootstrap_tasks`，而不是会被 update_checkpoint
    # 改写的合成态 `tasks`——否则「base + 重放全部物理事件」会把已折入 base 的事件
    # 二次应用，正确性就静默依赖「事件类型幂等」这条不变量。
    prev_ckpt = store.backend.load_checkpoint() or {}
    carry: dict[str, TaskState] = {}
    raw_base = prev_ckpt.get("bootstrap_tasks")
    if isinstance(raw_base, dict):
        carry = {tid: TaskState.from_dict(ts)
                 for tid, ts in raw_base.items() if isinstance(ts, dict)}
    _write_sentinel_checkpoint(store)
    archived, _ = read_archive_events(root)
    archived_ids = {ev.get("event_id") for ev in archived if ev.get("event_id")}
    active_suffix = [ev for ev in events if ev.get("event_id") not in archived_ids]
    store.backend.replace_active_events(active_suffix)
    _fsync_dir(root)
    store._line_count = len(archived) + len(active_suffix)
    if carry:
        # 快照基 + 本地事件：走 _rebuild_after_retract 的「两遍扫描 + 以 base 为基」
        # 语义（而非 _apply_events：它遇 RETRACT 会递归重建并从空基重放，快照仍会
        # 被冲）。此时 replace 已写完，物理账本即合并后的本地事件。
        state = carry
        store._replay_violations = []
        store._rebuild_after_retract(state, set(), base=carry)
    else:
        state = store.replay_full()
    store.update_checkpoint(state)
    return store._line_count



def _bootstrap_local_from_state(
    store: Store,
    remote_state: dict[str, Any],
    local_events: list[dict[str, Any]],
    remote_delta: list[dict[str, Any]],
    state_error: str | None,
) -> dict[str, Any]:
    """新设备 bootstrap：由远端 ``state.json`` 构造本地 checkpoint（口径①）。

    task-sync-compact-bootstrap-from-state。触发形态：另一端 push 后执行
    ``compact``（远端 ``delta.jsonl`` 被置空），此后加入的新设备 ``pull`` 只能
    拿到空 delta——本地账本 0 条事件、派生态全空，而 ``state.json`` 此前只被
    用于响应回显。本函数把该快照落到本地 checkpoint，使 ``replay()`` 由快照
    起步、后续增量事件照常参与 replay：

    - **不伪造事件**：只写 checkpoint（``ledger_line=0`` 表示本地尚无事件），
      事件转录保持纯净；后续事件既参与纯 ``replay()``，也在合并重建路径
      （``_rebuild_local_ledger``，见其「bootstrap 快照接力」段）里以本快照为基
      叠加——否则首次承载内容的增量 pull 会把快照派生态冲掉；
    - **fail-closed**：仅在「远端 delta 为空 + 本地无事件 + state 可解析且
      ``schema_version`` 受支持 + tasks 形状合法」时写入；任一不满足即返回
      ``applied=False`` + 结构化 reason，**不落半态**；
    - **幂等**：重复 pull 重写同一快照，派生态不变。

    已知边界（如实声明）：``state.json`` 是派生快照，不含 RETRACT 集合与事件级
    审计（本函数不写 ``retracted``），新设备的撤回集合由后续增量事件补齐；远端
    快照自身的正确性由 compact 端负责（本函数不做跨端交叉校验）。

    Args:
        store: 本地 Store（调用方须已持账本锁）。
        remote_state: 远端 ``state.json`` 解析结果（非 dict 时传空 dict）。
        local_events: 本地现存逻辑事件（判「本地是否已有账本内容」）。
        remote_delta: 本轮远端 delta（非空即走增量路径，不进 bootstrap）。
        state_error: ``state.json`` 解析失败的分类（None 表示可解析）。

    Returns:
        ``{"applied": bool, "tasks": int, "reason": str | None}``。
    """
    if state_error is not None:
        return {"applied": False, TASKS_KEY: 0, "reason": state_error}
    if remote_delta:
        return {"applied": False, TASKS_KEY: 0, "reason": "delta_present"}
    if local_events:
        return {"applied": False, TASKS_KEY: 0, "reason": "local_events_present"}
    if not remote_state:
        return {"applied": False, TASKS_KEY: 0, "reason": "no_remote_state"}
    version = remote_state.get("schema_version")
    if version != _STATE_SCHEMA_VERSION:
        return {"applied": False, TASKS_KEY: 0,
                "reason": "state_schema_unsupported",
                "schema_version": version}
    raw_tasks = remote_state.get("tasks")
    if not isinstance(raw_tasks, dict):
        return {"applied": False, TASKS_KEY: 0, "reason": "state_tasks_malformed"}
    snapshot: dict[str, dict[str, Any]] = {}
    try:
        for tid, ts in raw_tasks.items():
            if isinstance(tid, str) and isinstance(ts, dict):
                snapshot[tid] = TaskState.from_dict(ts).to_dict()
    except (TypeError, ValueError, KeyError):
        return {"applied": False, TASKS_KEY: 0,
                "reason": "state_task_fields_malformed"}
    if not snapshot:
        return {"applied": False, TASKS_KEY: 0, "reason": "state_tasks_empty"}
    store.backend.save_checkpoint({
        "ledger_line": 0,
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        TASKS_KEY: snapshot,
        # 快照基（task-sync-compact-bootstrap-from-state）：携带 **pre-compact
        # 原始快照**，供重建路径以它为基叠加物理事件、完整性校验豁免前缀比对。
        # 它必须恒为「原始」——update_checkpoint / 哨兵只保留、绝不更新其值，
        # 否则 base 会掺进本地事件的派生态，与「重放全部物理事件」二次应用。
        "bootstrap_tasks": snapshot,
    })
    return {"applied": True, TASKS_KEY: len(snapshot), "reason": None}



def _reconcile_local(
    store: Store,
    local_events: list[dict[str, Any]],
    new_remote: list[dict[str, Any]],
    *,
    source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """合并远端增量并按【内容比较】决定是否重建本地账本（L-6）。

    旧判据是事件**条数**（``len(merged) != len(local_events)``）：当「去重丢弃」
    与「新增」条数相抵时被判为「无变化」→ 远端事件永久不落本地、两机静默发散，
    且无任何告警。改为内容比较（``merged != local_events``）后，相抵场景同样
    触发重建，并产出结构化 E030 告警（不再静默）。

    Args:
        store: 本地 Store。
        local_events: 合并前的本地事件（原序）。
        new_remote: 本次要并入的远端增量（已按 event_id 过滤本地存量）。
        source: 触发来源（pull / push / compact），写入告警上下文。

    Returns:
        ``(merged_events, warnings)``；warnings 含 id 碰撞告警、相抵重建告警、
        入站校验拒收告警（E030，拒收明细挂 ``inbound_validation``）。
    """
    kept_remote, inbound_rejected = _validate_inbound_events(
        local_events, new_remote)
    merged, collisions = _merge_events_detailed(local_events + kept_remote)
    warnings: list[dict[str, Any]] = list(collisions)
    # 接触即纠接入（task-counter-cache-reconcile D2）：既存计数漂移进入本
    # 汇聚点即归位并留 info 级痕——幂等分支（merged == local）原先对漂移完全
    # 静默（V3 实锤形态），此后每次 sync 都是纠偏触点。与下方 counter_drift
    # 重建告警正交：那条比「缓存 vs 去重逻辑流」（承载重复探测），本条比
    # 「缓存 vs 实体物理数」（缓存归位），两层哨兵各司其职。
    rec = store.reconcile_counters()
    if rec.get("drift"):
        warnings.append({
            "code": "counter_recalibrated",
            "severity": "info",
            "source": source,
            MESSAGE_KEY: (
                "sync 接触即纠：计数缓存漂移 "
                f"{rec.get('from')} → 实体真值 {rec.get('to')} 已归位"
                if rec.get("recalibrated") else
                "计数缓存漂移 "
                f"{rec.get('from')} ≠ 实体 {rec.get('to')}——"
                "当前为只读通道未归位，可写进程接触即自愈"
            ),
            "counter_recalibrate": {
                "from": rec.get("from"),
                "to": rec.get("to"),
                "repaired": bool(rec.get("recalibrated")),
            },
        })
    if inbound_rejected:
        warnings.append({
            "code":
            ErrorCode.E030.name,
            "severity":
            "warning",
            MESSAGE_KEY: (f"sync 入站校验拒收 {len(inbound_rejected)} 条非法远端事件"
                        "（逐条丢弃，整库合并继续）"),
            "source":
            source,
            "inbound_validation": {
                "rejected": len(inbound_rejected),
                "kept": len(kept_remote),
                "rejected_events": inbound_rejected,
            },
        })
    if merged == local_events:
        # 未触发重建也要刷新软校验观测（L-3）：合并结果与本地一致时仍可能存在
        # 序列级违规（例如远端冲突事件被 event_id 去重丢弃，但冲突本身值得暴露）
        store.replay()
    else:
        if len(merged) == len(local_events):
            warnings.append({
                "code":
                ErrorCode.E030.name,
                "severity":
                "warning",
                MESSAGE_KEY: ("sync 重建判据命中原「条数相抵」盲区：合并后事件数与本地相同"
                            "但内容已变（去重丢弃与新增相抵），已按内容比较触发重建"),
                "context": {
                    "source": source,
                    "local_events": len(local_events)
                },
            })
        # 内含 replay_full → 顺带采集序列级软校验违规
        _rebuild_local_ledger(store, merged)

        # 计数不变量（task-sync-rebuild-active-suffix）：后端持久计数器与读侧逻辑流
        # 必须同数——活跃段重复承载归档前缀时，event_count 把归档事件二次计入，而
        # read_events 按 event_id 去重掩盖了它，两者当场背离。重建后立刻核对，不等
        # 下一个进程（或另一台设备）去发现。只在重建分支查：幂等重复合并不落盘，
        # 无需为此多付一遍读侧成本。
        _counted = store.backend.event_count()
        _read_len = len(store.backend.read_events())
        if _counted != _read_len:
            _archive_n = len(read_archive_events(_store_runtime_root(store))[0])
            warnings.append({
                "code":
                ErrorCode.E030.name,
                "severity":
                "warning",
                MESSAGE_KEY: (
                    f"sync 重建后计数背离：后端 event_count={_counted} 与 "
                    f"len(read_events())={_read_len} 不符"
                    "（活跃段疑似重复承载归档前缀，读侧靠 event_id 去重掩盖）"
                ),
                "source":
                source,
                "counter_drift": {
                    "archive_events": _archive_n,
                    "active_events": _counted - _archive_n,
                    "logical_total": _read_len,
                    "backend_event_count": _counted,
                    "read_events_len": _read_len,
                },
            })

    # L-3（task-ledger-replay-visibility）：replay 序列级软校验违规汇总——只告警、
    # 不阻断、不改派生状态，以结构化字段（violations）挂在 warning 上，随 sync
    # 合并响应返回（pull / push / compact 的 warnings 与 replay_violations 字段）。
    violations = store.replay_violations
    if violations:
        warnings.append({
            "code":
            ErrorCode.E030.name,
            "severity":
            "warning",
            MESSAGE_KEY: (f"replay 软校验发现 {len(violations)} 条序列级不变量违规"
                        "（如跨设备双认领）——事件已照常应用，此处仅告警"),
            "source":
            source,
            "violations":
            violations,
        })
    return merged, warnings
