"""ledger sync — 入站事件校验域（W6-C2 拆分，纯搬移）。

来源：``orchd/ledger_sync.py``（1622 行，W6-C2 纯搬移）。

入站撤回判定 / ``_validate_inbound_events`` / 入站拒绝统计。

依赖 ``merge``（``_claim_stale_timeout_s`` / ``_trunc``，顶层导入）。
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
from .merge import (
    _claim_stale_timeout_s,
    _trunc,
)



# 入站整类拒收的事件类型（task-sync-inbound-force-gate，P0-3 修法②）：
# FORCE_STATUS 是**本地人工动作**——要人显式二次确认，语义绑定本机上下文
# （分支 / worktree / 锁），跨机复制等于把一端的人工善后强加给另一端；它在
# TRANSITION_TABLE 里 target=None，旧实现因此完全绕过跃迁校验（伪造终态静默落盘的
# 入口）。拒收走既有 rejected 明细 + E030 告警通道，不静默丢弃。
# task-sync-inbound-retract-authz：RETRACT 移出整类拒收，改窄口径放行（见
# ``_inbound_retract_verdict``）——入站只认本地可验证的时间事实（目标认领已
# 超时），不认入站署名与 admin（口径单一真源见
# ``docs/system-design-online.md`` §3.6）。
_INBOUND_LOCAL_ONLY_TYPES = frozenset({"FORCE_STATUS"})


# 入站拒收的判定基准二分（task-sync-push-kept-events 返工轮）：
#   absolute —— **同一引擎版本下**与设备无关地非法（非 dict / 契约违约 / 本地专用
#     类型），因此可以从共享 ref 的 delta 载体里剔除，不必让每台 push 过的设备反复
#     携带同一枚垃圾事件；
#   relative —— 只对**本机**派生态或**本机**账本非法（跃迁白矩阵、RETRACT 窄口径），
#     对状态落后的另一端可能完全合法，故只能本机不入账，不得据本机判定删掉他端
#     需要的转录项（否则一台设备获得了删除另一台设备历史事件的权力）。
# 「同一引擎版本」这个限定不是客套：``contract:`` 里含 ``unknown event type``
# （``orchd/contracts/events.py``），而 ``docs/compat-policy.md`` 许可新增事件类型——
# 升级窗口内旧引擎会把新类型判成 absolute 并把它从共享转录里删掉。跨版本收紧另立
# idea 讨论，本模块的口径以此处为准。
# 分类真源就是 ``_validate_inbound_events`` 里各条拒收点自带的 basis 键；下游只读该
# 键，禁止用 reason 字符串前缀嗅探重造一套分类。
_INBOUND_REJECT_ABSOLUTE = "absolute"

_INBOUND_REJECT_RELATIVE = "relative"



def _inbound_retract_verdict(
    ev: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> tuple[bool, str]:
    """入站 RETRACT 窄口径判定（task-sync-inbound-retract → task-sync-inbound-retract-authz）。

    口径 A（2026-10-08 用户裁决，来源 idea:sync-inbound-trust-hygiene / pass16 P2-6）：
    **入站授权只取本地可验证的时间事实**——目标为 ``CLAIMED`` / ``REVIEW_CLAIMED``
    且已超阈值才放行，其余一律拒收。原「作者本人或 ``admin``」两条分支已删：

    - ``ev.agent_id`` 取自**入站事件自身**，``target.agent_id`` 虽取自本地账本，
      但共享 ref 的 ``delta.jsonl`` 原样携带 CLAIMED 事件的 ``agent_id``
      （``_write_ledger_ref`` 写的就是事件流），署名对任何能读该 ref 的对端都是
      公开的 ⇒ 两条分支用的都是可自证、可预知的凭据，不是授权；
    - 本地那条 ``E034`` 归属守卫（``orchd/onboard/control.py::retract``）比的是
      **会话身份**（由 session 机制约束），与入站不同源，此前 docstring 称
      「镜像本地守卫」并不成立；本地仍认 ``admin`` 属既有留账（control.py 注释
      记为 P2-11，根治需 Registry 认证机制），本卡只收窄入站侧，不对称的理由与
      代价写进 ``docs/system-design-online.md`` §3.6。

    代价与自愈路径（与边界声明同口径，勿写成「自动解」）：对端**未超时**的自撤认
    会被拒收，本端该认领停在 ``claimed``，直至超阈值后经僵尸接管通道
    （``.orchd/rules/session.md`` 接管 SOP 第 4 步，即本函数的 ``stale_claim``
    分支）或人工 ``force-status`` 处置；接管与人工动作都可被 report 的 stale-claim
    检测观测。``admin`` 的跨机纠正实际走 ``force_status`` → ``FORCE_STATUS``，
    而后者入站**整类拒收**（见 ``_INBOUND_LOCAL_ONLY_TYPES``），故删去 ``admin``
    分支不削减任何当前可达功能。

    Returns:
        ``(allowed, reason)``。
    """
    target_eid = ev.get("target_event_id")
    target = by_id.get(target_eid) if isinstance(target_eid, str) else None
    if target is None:
        return False, f"target_not_found: {target_eid!r}"
    if target.get("type") == "FORCE_STATUS":
        return False, "cannot_retract_force_status"
    ttype = target.get("type")
    age = review_claim_age_s(target.get("timestamp"))
    if ttype == "REVIEW_CLAIMED" and age is not None and age >= review_stale_timeout_s():
        return True, "stale_review_claim"
    if ttype == "CLAIMED" and age is not None and age >= _claim_stale_timeout_s():
        return True, "stale_claim"
    return False, (
        f"not_authorized: target '{target_eid}' owned by "
        f"'{target.get('agent_id')}', retractor '{ev.get('agent_id')}' "
        "（入站授权只认目标已超时）"
    )



def _validate_inbound_events(
    local_events: list[dict[str, Any]],
    new_remote: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """入站校验：远端增量逐事件过契约 + 跃迁白矩阵（fail-closed 逐条丢弃）。

    与写路径（``Store.append_event``）同判据，但语义为「丢弃 + 明细」而非抛错：
    - ``validate_event`` 直接作用于原始事件（不做 ``normalize_event``——缺 ``v``
      必须拒收，而不是被补缺省放行）；
    - ``_event_target_status`` + ``validate_transition`` 相对合并序下的运行状态
      校验（运行状态由本地事件按序派生、全 pending 起点；通过的远端事件即时
      应用状态，后续远端事件基于最新状态校验——伪造 DONE 被丢弃后不再污染
      合法 CLAIMED 的判定）；
    - ``FORCE_STATUS``（见 ``_INBOUND_LOCAL_ONLY_TYPES``）**整类拒收**：本地人工
      动作（要人二次确认），不跨机复制；且它在转移表里 ``target=None``，放行等于
      跳过跃迁校验（历史缺陷：伪造 ``completed`` 静默落盘后，此后合法 CLAIMED 被
      E007 永久拒绝）。本地事件流仍由 ``_track`` 按其 ``target_status`` 派生运行状态，
      拒收只作用于远端增量；
    - ``RETRACT`` **窄口径放行**（task-sync-inbound-retract-authz，见
      ``_inbound_retract_verdict`` 与 ``docs/system-design-online.md`` §3.6）：
      只认目标认领已超时的时间事实（``CLAIMED`` / ``REVIEW_CLAIMED`` 分走
      ``claim_stale_timeout_s`` / ``review_stale_timeout_s`` 阈值），不认入站
      署名与 ``admin``；其余拒收。
      放行的 RETRACT 经 ``_track`` 应用到派生态（目标事件从 applied 移除并重算），
      使本端回到「无活跃认领」；
    - 非 dict 输入（正常经 ``_parse_delta_events`` 已过滤，此处纵深兜底）拒收。

    非法事件逐条丢弃 + 明细返回，不抛错、不中止整库合并（可用性语义不变）。

    Returns:
        ``(kept, rejected)``：保留事件与拒收明细
        （``event_id`` / ``type`` / ``task_id`` / ``reason`` / ``basis``）。``basis``
        取 ``absolute``（同一引擎版本下与设备无关地非法，可自共享转录剔除）或
        ``relative``（只对本机非法，必须继续携带），是 push 组 delta 排除面的唯一真源。
    """
    status: dict[str, str] = {}
    # task-sync-inbound-retract：放行的 RETRACT 要应用到派生态，否则后续合法
    # CLAIMED 被跃迁校验误拒（目标认领撤销后本端应回 pending）。按 task 维护
    # 已应用（未被撤回）事件序，RETRACT 时移除目标事件并重算该任务状态。
    applied: dict[str, list[dict[str, Any]]] = {}

    def _recompute(tid: str) -> None:
        st = "pending"
        for e in applied.get(tid, []):
            if e.get("type") == "FORCE_STATUS":
                ts = e.get("target_status", "pending")
                st = ts if isinstance(ts, str) else "pending"
            else:
                tgt = _event_target_status(e)
                if tgt is not None:
                    st = tgt
        status[tid] = st

    def _track(ev: dict[str, Any]) -> None:
        tid = ev.get("task_id")
        if not tid or not isinstance(tid, str):
            return
        if ev.get("type") == "RETRACT":
            target_eid = ev.get("target_event_id")
            applied[tid] = [
                e for e in applied.get(tid, [])
                if e.get("event_id") != target_eid
            ]
            _recompute(tid)
            return
        applied.setdefault(tid, []).append(ev)
        if ev.get("type") == "FORCE_STATUS":
            target_status = ev.get("target_status", "pending")
            status[tid] = (target_status if isinstance(target_status, str)
                           else "pending")
            return
        target = _event_target_status(ev)
        if target is not None:
            status[tid] = target

    # 事件按 event_id 索引：供入站 RETRACT 解析被撤目标（本地 + 已保留远端）。
    by_id: dict[str, dict[str, Any]] = {}
    for local_ev in local_events:
        if isinstance(local_ev, dict):
            # task-ledger-sync-mypy-index-fix：取出并 isinstance 收窄为 str——
            # `.get()` 静态类型为 Any|None，`if local_ev.get(...)` 只证真值不收窄，
            # 直接索引 dict[str, ...] 触发 mypy [index] 红（event_id 契约恒为 str）。
            _ev_id = local_ev.get("event_id")
            if isinstance(_ev_id, str) and _ev_id:
                by_id[_ev_id] = local_ev
            _track(local_ev)

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    dict_remote: list[dict[str, Any]] = []
    for remote_ev in new_remote:
        if not isinstance(remote_ev, dict):
            rejected.append({
                "event_id": None,
                "type": None,
                "task_id": None,
                "reason": "not_a_dict: 远端事件不是 JSON 对象",
                "basis": _INBOUND_REJECT_ABSOLUTE,
            })
            continue
        dict_remote.append(remote_ev)
    for ev in sorted(dict_remote, key=event_sort_key):
        eid = ev.get("event_id")
        etype = ev.get("type")
        tid = ev.get("task_id")
        violations = validate_event(ev)
        if violations:
            rejected.append({
                "event_id": eid,
                "type": etype,
                "task_id": tid,
                "reason": "contract: " + "; ".join(violations),
                "basis": _INBOUND_REJECT_ABSOLUTE,
            })
            continue
        if etype in _INBOUND_LOCAL_ONLY_TYPES:
            # 整类拒收（本地人工动作不跨机复制）：不入 kept、不 _track，
            # 明细挂 E030 的 rejected_events，避免伪造终态静默污染后续判定。
            # task-sync-inbound-retract：仅 FORCE_STATUS 仍整类拒收；RETRACT 走
            # 下方窄口径（_inbound_retract_verdict）。
            rejected.append({
                "event_id": eid,
                "type": etype,
                "task_id": tid,
                "reason": (
                    "local_only: 本地人工动作不跨机复制，整类拒收"
                    f"（target_status={_trunc(ev.get('target_status'))!r}）"
                ),
                "basis": _INBOUND_REJECT_ABSOLUTE,
                "target_status": _trunc(ev.get("target_status")),
            })
            continue
        if etype == "RETRACT":
            allowed, verdict_reason = _inbound_retract_verdict(ev, by_id)
            if not allowed:
                rejected.append({
                    "event_id": eid,
                    "type": etype,
                    "task_id": tid,
                    "reason": f"retract_narrow: {verdict_reason}",
                    "basis": _INBOUND_REJECT_RELATIVE,
                    "target_event_id": _trunc(ev.get("target_event_id")),
                })
                continue
            if eid:
                by_id[eid] = ev
            _track(ev)
            kept.append(ev)
            continue
        target = _event_target_status(ev)
        if tid and target:
            try:
                validate_transition(
                    ev.get("type", ""),
                    status.get(tid, "pending") if isinstance(tid, str)
                    else "pending",
                    target,
                )
            except OrchdError as exc:
                rejected.append({
                    "event_id": eid,
                    "type": etype,
                    "task_id": tid,
                    "reason": f"transition: {exc.message}",
                    "basis": _INBOUND_REJECT_RELATIVE,
                })
                continue
        if eid:
            by_id[eid] = ev
        _track(ev)
        kept.append(ev)
    return kept, rejected



def _count_inbound_rejected(warnings: list[dict[str, Any]]) -> int:
    """从合并告警中汇总入站校验拒收数（调用方结果字段用）。"""
    total = 0
    for warning in warnings:
        context = warning.get("inbound_validation")
        if isinstance(context, dict):
            total += int(context.get("rejected", 0) or 0)
    return total



def _inbound_rejected_ids(warnings: list[dict[str, Any]]) -> set[str]:
    """从合并告警中取**绝对非法**拒收事件的 event_id 集合（push 组 delta 排除用）。

    与 :func:`_count_inbound_rejected` 同一明细源（E030 的
    ``inbound_validation.rejected_events``，构造点见 ``_reconcile_local``）——计数与
    点名共用一份真源，不另设第二套判定。

    只点名 ``basis == "absolute"`` 那批（同一引擎版本下与设备无关地非法：非 dict /
    契约违约 / 本地专用类型）。``relative``（跃迁白矩阵、RETRACT 窄口径）的判定基准是本机派生态
    与本机账本，同一事件对状态落后的另一端完全合法——按它删共享转录，等于给一台
    设备删除另一台设备历史事件的权力。条目缺 ``basis`` 键（旧告警、手构明细）一律
    按 relative 处理：不确定就继续携带，转录完整性优先于净化。
    """
    ids: set[str] = set()
    for warning in warnings:
        context = warning.get("inbound_validation")
        if not isinstance(context, dict):
            continue
        for entry in context.get("rejected_events") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("basis") != _INBOUND_REJECT_ABSOLUTE:
                continue
            if entry.get("event_id"):
                ids.add(str(entry["event_id"]))
    return ids
