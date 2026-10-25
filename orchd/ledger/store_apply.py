"""``orchd.ledger.Store`` 事件应用 mixin（file-split-program W1-L8，2026-10-16）。

承载 ``_StoreApplyMixin``：``_apply_events`` / ``_apply_event`` / ``_rebuild_after_retract`` /
``_collect_retracted_event_ids``；方法体与 L7 基线逐字一致。

依赖方向：标准库 + ``orchd.contracts.events`` + ``orchd.ledger.events``
（``_event_target_status`` 顶层导入——补丁面判据要求其走模块全局查找）+
``orchd.ledger.store_types``。不得导入聚合入口。
"""
from __future__ import annotations

import warnings

from orchd.contracts.events import (
    STATUS_CLAIMED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    TASK_STATUSES,
    EVENT_CLAIMED,
    EVENT_DONE,
    EVENT_FORCE_STATUS,
    EVENT_RETRACT,
    EVENT_REVIEW_CLAIMED,
    EVENT_REVIEW_READY,
    EVENT_REVIEW_SUBMITTED,
)
from orchd.ledger.events import _event_target_status
from orchd.ledger.store_types import TaskState
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchd.storage import StorageBackend

class _StoreApplyMixin:

    # 宿主接口（**仅类型层声明**，运行期不产生任何类属性，故不进入 AST / vars() 扫描面）：
    # ``backend`` 等属性由 ``store.Store`` 的 ``__init__`` 提供，
    # ``if TYPE_CHECKING:`` 块内的方法由兄弟段经 ``Store`` 的 MRO 拼装提供。
    backend: StorageBackend

    if TYPE_CHECKING:
        def ledger_exists(self) -> bool: ...
        def _read_ledger_lines(self, from_line: int) -> list[dict[str, Any]]: ...
        def _record_soft_violation(self, event: dict[str, Any], ts: TaskState) -> None: ...

    def _apply_events(
        self,
        events: list[dict[str, Any]],
        tasks: dict[str, TaskState],
        retracted: set[str],
        *,
        handle_retract: bool = True,
    ) -> None:
        """事件驱动的状态机：逐条应用事件列表，原地修改 ``tasks`` 和 ``retracted``。

        已被撤回（``retracted`` 集合中）的事件会被跳过；没有 ``task_id`` 的事件
        也会被忽略。单个事件的跃迁逻辑收敛到 :meth:`_apply_event`（去重后单一实现）。

        ``handle_retract``（合并去重的唯一分歧点，2026-08-29）：
        - ``True``（活跃路径 replay / replay_full）：遇到 RETRACT 时把目标事件标记
          撤回并触发 :meth:`_rebuild_after_retract` 全量重建，随后 **return**——
          重建已从头重放全部事件（含本 RETRACT 之后的事件），``tasks`` 已是最终
          状态，继续循环会重复应用；且可避免 K 个 RETRACT 触发 K 次 O(L) 级联重建
          （O(K·L) → 单次重建后即 O(L)）。
        - ``False``（_rebuild_after_retract / _replay_prefix 内部）：仅记录
          ``target_event_id`` 到 ``retracted``，不递归重建，避免无限递归 / 越过前缀边界。
        """
        for event in events:
            eid = event.get("event_id", "")
            if eid in retracted:
                # 跳过已被 RETRACT 撤回的事件，不纳入状态计算
                continue

            task_id = event.get("task_id", "")
            if not task_id:
                # 没有 task_id 的事件（如系统级事件）不影响任务状态，直接跳过
                continue

            if event.get("type") == EVENT_RETRACT:
                # RETRACT 在任务初始化之前处理：它是系统级回滚事件，不应为某任务
                # 凭空创建状态条目（活跃路径与重建路径一致，见 _apply_event 注释）。
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
                if handle_retract:
                    # RETRACT 具有级联效应：撤回某事件后，后续依赖该事件的状态变化
                    # 都需要重新计算，因此必须从头全量重建（_rebuild_after_retract
                    # 已重放全部事件，tasks 已是最终状态，此处必须 return）
                    self._rebuild_after_retract(tasks, retracted)
                    return
                continue

            if task_id not in tasks:
                # 首次出现的 task_id，初始化默认状态（pending）
                tasks[task_id] = TaskState()
            self._apply_event(event, tasks[task_id])

    def _rebuild_after_retract(
        self, tasks: dict[str, TaskState], retracted: set[str],
        base: dict[str, TaskState] | None = None,
    ) -> None:
        """RETRACT 后从头全量重建任务状态。

        为什么不能增量修补：RETRACT 撤回的事件可能已被后续事件依赖
        （如 CLAIMED → DONE → REVIEW_READY 链条中撤回 CLAIMED），
        单纯"反向操作"无法正确还原状态，必须从第 1 行开始重新应用
        所有未被撤回的事件，以保证最终状态的完整性和一致性。

        正确性关键（P0 修复）：重建前必须先完整收集所有 RETRACT 事件的
        ``target_event_id``，再应用非撤回事件。若边应用边累积 ``retracted``，
        则「早于 RETRACT 事件出现的被撤回事件」会先被应用、后才发现被撤回，
        导致已撤回事件「复活」（例如 checkpoint 之前发生过 retract、之后又
        来一次 retract 触发重建时，checkpoint 前的 retract 集合若缺失就会
        静默错乱状态）。
        """
        # base 必须在 clear 之前取副本：调用方可能把同一个字典同时当作 tasks 与
        # base（就地重建入口即如此），先 clear 会把基也清空。
        snapshot = dict(base) if base is not None else None
        tasks.clear()
        # 快照基（task-sync-compact-bootstrap-from-state）：物理账本可能不含
        # compact 清空的转录前缀，其派生态只能来自快照。① 调用方显式传入 base
        # （合并重建路径——那时 checkpoint 已是「重建中」的哨兵，靠不住）；
        # ② 否则回退读现存 checkpoint 的 bootstrap_tasks 快照基（replay 路径——那时
        # checkpoint 仍是真快照）。
        if snapshot is not None:
            tasks.update(snapshot)
        else:
            ckpt = self.backend.load_checkpoint() or {}
            raw_base = ckpt.get("bootstrap_tasks")
            if isinstance(raw_base, dict):
                for tid, ts in raw_base.items():
                    if isinstance(ts, dict):
                        tasks[tid] = TaskState.from_dict(ts)
        all_events = self._read_ledger_lines(from_line=1)
        # 先完整收集所有 RETRACT 的目标事件 ID，再应用事件（两遍扫描）
        for event in all_events:
            if event.get("type") == EVENT_RETRACT:
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
        self._apply_events(all_events, tasks, retracted, handle_retract=False)

    def _apply_event(self, event: dict[str, Any], ts: TaskState) -> None:
        """应用单个非 RETRACT 事件到任务状态（原地修改 ``ts``）。

        支持的事件类型及其效果（task-audit-ledger-state-machine-dedup 合并去重后
        的单一实现，活跃路径与全量重建路径共用，根除「改一处漏一处」缺陷类别）：
            - ``CLAIMED``          : 任务被 agent 认领，状态 → ``claimed``，清空审核字段。
            - ``DONE``             : agent 提交完成，状态 → ``done``，递增 ``attempt_count``。
            - ``REVIEW_READY``     : 进入审核，状态 → ``in_review``，设置 ``review_phase``。
            - ``REVIEW_CLAIMED``   : reviewer 认领审核，设置 ``review_claimed_by``。
            - ``REVIEW_SUBMITTED`` : 审核结果提交。``APPROVED``(code) → ``completed``；
                                    ``CHANGES_REQUESTED`` → 回退 ``pending``。
            - ``FORCE_STATUS``     : 强制覆盖状态到指定值，附带相关字段重置逻辑；
                                    ``target_status`` 不在 :data:`TASK_STATUSES`
                                    内时不写入（记 UserWarning，派生态保持不变）。

        RETRACT 由调用方 :meth:`_apply_events` 在进入本方法前拦截（活跃路径触发
        全量重建、重建路径仅记录），此处只处理状态跃迁事件。跃迁合法性由白矩阵
        （:func:`validate_transition`）在写入时校验。

        L-3（2026-09-15）：入口先做**序列级软校验**（:meth:`_record_soft_violation`）
        ——只观测收集、不阻断也不改变下方派生结果，供 sync 合并响应暴露
        「跨设备双认领」类语义冲突。

        W2-③：各分支只写字段副作用；状态赋值统一收敛到末尾的转移表推导
        （:func:`_event_target_status`），分支内不得再手写 ``ts.status``。
        """
        self._record_soft_violation(event, ts)
        etype = event.get("type", "")
        if etype == EVENT_CLAIMED:
            ts.claimed_by = event.get("agent_id")
            ts.claimed_session = event.get("session_id")
            ts.review_phase = None
            ts.review_claimed_by = None
            ts.review_claimed_session = None
            ts.review_claimed_at = None
            ts.review_self_review = False

        elif etype == EVENT_DONE:
            ts.attempt_count = event.get("attempt_count", ts.attempt_count + 1)

        elif etype == EVENT_REVIEW_READY:
            ts.review_phase = event.get("review_type")
            ts.review_claimed_by = None
            ts.review_claimed_session = None
            ts.review_claimed_at = None
            # v4：新一轮审查开始，自审标记随上一轮清零（事实由历史事件承载）
            ts.review_self_review = False

        elif etype == EVENT_REVIEW_CLAIMED:
            ts.review_claimed_by = event.get("agent_id")
            ts.review_claimed_session = event.get("session_id")
            ts.review_claimed_at = event.get("timestamp")
            # v4（2026-09-15 停服升级）：自审事实落账——事件缺失该字段的历史数据
            # 视为非自审（False），需回查历史时仍可用 DONE/REVIEW_CLAIMED 的
            # agent_id 相等性启发式推导，二者不冲突。
            ts.review_self_review = bool(event.get("is_self_review"))

        elif etype == EVENT_REVIEW_SUBMITTED:
            verdict = event.get("verdict", "")
            review_type = event.get("review_type")
            if verdict == "APPROVED":
                if event.get("merge_pending"):
                    # A4（debug-pack A 组）：merge 未落地——verdict 落账但保持
                    # in_review；不清审查字段，目标状态由转移表推导为 in_review
                    # （同一 reviewer 可重试 code APPROVED）。
                    pass
                elif review_type == "code" or review_type is None:
                    # review-unify-r2：code review 通过，或 unified 单阶段
                    # （事件无 review_type 字段）APPROVED → 任务彻底完成；
                    # 老事件含 review_type: spec 仍按两阶段语义（仅 spec 通过，
                    # 等待 code），保持 checkpoint 与历史一致。
                    ts.review_phase = None
                    ts.review_claimed_by = None
                    ts.review_claimed_session = None
                    ts.review_claimed_at = None
                    # v4（2026-09-15 停服升级）：自审标记不随审查字段清空——completed
                    # 之后仍需能回查「本次完成系自审通过」；用 or 语义合并，使历史事件
                    # （无 is_self_review 字段）不清掉 REVIEW_CLAIMED 已落的值。
                    ts.review_self_review = ts.review_self_review or bool(
                        event.get("is_self_review")
                    )
                    # B1（2026-08-13 full-audit-v2）：merge 降级标记随事件持久化，
                    # completed 但 merge 未落地时保留 merge_warning 供 audit-merge 告警
                    ts.merge_warning = event.get("merge_warning")
                else:
                    # spec review 通过但 code review 尚未开始，
                    # CLI 层会自动生成 REVIEW_READY(code) 事件，此处无需处理
                    pass
            elif verdict == "CHANGES_REQUESTED":
                # 审核被驳回 → 任务回退到 pending，清空认领和审核信息。
                # 注意：不重置 attempt_count——attempt_count 累计「打回次数」，
                # 供 request 的 max_attempts 上限警告（exceeded_max_attempts）；
                # 仅 force-status pending 才重置计数（人工恢复手段）。
                ts.claimed_by = None
                ts.claimed_session = None
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                # v4：审查被打回 → 回退 pending，本轮自审标记随回退清零
                # （事实仍由历史 REVIEW_SUBMITTED 事件承载，缺口不回退）
                ts.review_self_review = False
            else:
                # B5（task-gate-cleanup-batch）：未知 verdict 不再静默 no-op——
                # 写路径 review 已 E007 拦截，此处守历史/手造/跨设备脏事件。
                # replay 是纯派生路径（无 store/响应可挂 E030），故记进程级
                # warnings（与既有未知事件语义一致），不阻断、不抛错。
                warnings.warn(
                    f"unknown REVIEW_SUBMITTED verdict "
                    f"{event.get('verdict')!r} for task "
                    f"{event.get('task_id', '?')} "
                    f"(event {event.get('event_id', '?')})",
                    UserWarning, stacklevel=2,
                )

        elif etype == EVENT_FORCE_STATUS:
            # 强制状态覆盖：根据 target_status 重置关联字段，
            # 确保派生状态与被强制设置的状态保持一致
            target = event.get("target_status", STATUS_PENDING)
            if not isinstance(target, str) or target not in TASK_STATUSES:
                # 与未知 REVIEW_SUBMITTED verdict 同口径（B5）：replay 是纯派生路径，
                # 无 store/响应可挂 E030，故记进程级 warning 并**保持派生态不变**——
                # 非法 target_status 不再静默写进运行态（target=None 让下方字段分支
                # 整体 no-op，等价于该事件对状态无影响）。
                # 判据是 TASK_STATUSES（六态词表）而不是人工白名单 FORCE_TARGET_STATUSES：
                # 引擎自写的 FORCE_STATUS 允许 in_review（review.py:1232 终审残留回退）。
                warnings.warn(
                    f"unknown FORCE_STATUS target_status {target!r} for task "
                    f"{event.get('task_id', '?')} "
                    f"(event {event.get('event_id', '?')}) "
                    f"(legal: {sorted(TASK_STATUSES)}), status unchanged",
                    UserWarning, stacklevel=2,
                )
                target = None
            if target is not None:
                ts.status = target
            if target == STATUS_PENDING:
                # 回退到待认领：清空所有执行和审核相关字段
                ts.attempt_count = 0
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                ts.review_self_review = False
                ts.claimed_by = None
                ts.claimed_session = None
            elif target == STATUS_CLAIMED:
                # 强制认领：设置指派的 agent
                ts.claimed_by = event.get("assignee")
                ts.claimed_session = event.get("session_id")
            elif target == STATUS_CANCELLED:
                # 强制取消：清空所有执行和审核相关字段
                ts.claimed_by = None
                ts.claimed_session = None
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                ts.review_self_review = False
            elif target == STATUS_COMPLETED:
                # 强制完成：保留实现者信息（claimed_by），仅清空审查字段，
                # 与 code APPROVED 语义对齐（避免 completed 状态残留审查阶段/审查者）
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                # v4：人工强制完成无审查事件来源，自审标记一并清零（避免残留
                # 上一轮的 True 被误读为「本次完成系自审通过」）
                ts.review_self_review = False

        # W2-③：集中状态赋值（转移表推导；None = 无状态变更）。
        # FORCE_STATUS 分支已自行设置 ts.status（目标推导对其返回 None）。
        target_status = _event_target_status(event)
        if target_status is not None:
            ts.status = target_status

    def _collect_retracted_event_ids(self) -> set[str]:
        """扫描 ledger 中所有 RETRACT 事件的 ``target_event_id``，得到完整撤回集合。

        用于 ``update_checkpoint`` 在调用方未显式传入 ``retracted`` 时自算持久化集合
        （P0 修复：checkpoint 必须记录 checkpoint 之前的撤回，否则之后的 retract
        重建会复活已撤回事件）。ledger 不存在时返回空集合。
        """
        retracted: set[str] = set()
        if not self.ledger_exists():
            return retracted
        for event in self._read_ledger_lines(from_line=1):
            if event.get("type") == EVENT_RETRACT:
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
        return retracted
