"""``orchd.ledger.Store`` 写侧 mixin（锁 / 追加 / checkpoint / 压缩）（file-split-program W1-L8）。

承载 ``_StoreWriteMixin``：``_lock_fd``（property + setter）/ ``acquire_lock`` /
``release_lock`` / ``append_event`` / ``update_checkpoint`` / ``compact_archive`` /
``_compact_archive_locked`` / ``_active_file_size``；方法体与 L7 基线逐字一致。

依赖方向：标准库 + ``orchd.contracts.events`` / ``orchd.errors`` / ``orchd.lockfile`` +
``orchd.ledger.events``（``_event_target_status`` 顶层导入——补丁面判据要求其走模块全局查找）+
``orchd.ledger.identity`` / ``orchd.ledger.store_types``；对 ``validate_transition`` 与
``resolve_store_dir`` 用**函数级惰性导入**。不得导入 ``orchd.ledger`` 聚合入口。
"""
from __future__ import annotations

import json

from datetime import datetime, timezone
from orchd.contracts.events import (
    STATUS_PENDING,
    TERMINAL_STATUSES,
    normalize_event,
    validate_event,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger.events import _event_target_status
from orchd.ledger.identity import _touch_session_last_seen
from orchd.ledger.store_types import TaskState, _CHECKPOINT_SCHEMA_VERSION
from orchd.lockfile import _depth_registry
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchd.storage import StorageBackend

class _StoreWriteMixin:

    # 宿主接口（**仅类型层声明**，运行期不产生任何类属性，故不进入 AST / vars() 扫描面）：
    # 属性与派生路径由 ``store.Store`` 的 ``__init__`` 与骨架 property 提供，
    # ``if TYPE_CHECKING:`` 块内的方法由兄弟段经 ``Store`` 的 MRO 拼装提供。
    orchd_dir: Path
    backend: StorageBackend
    _line_count: int | None

    if TYPE_CHECKING:
        @property
        def lock_path(self) -> Path: ...
        def replay(self) -> dict[str, TaskState]: ...
        def replay_full(self) -> dict[str, TaskState]: ...
        def _read_ledger_lines(self, from_line: int) -> list[dict[str, Any]]: ...
        def reconcile_counters(self) -> dict[str, Any]: ...
        def _current_line_count(self) -> int: ...
        def _collect_retracted_event_ids(self) -> set[str]: ...

    @property
    def _lock_fd(self) -> Any | None:
        """兼容属性：转发到 backend._file_lock._fd，保证既有测试与调用方兼容。

        FilesystemBackend docstring 明文承诺'Store 的 _lock_fd 属性转发到本后端'，
        未持锁时为 None，持锁时为 fd 真值。
        """
        try:
            return self.backend._file_lock._fd  # type: ignore[attr-defined]
        except AttributeError:
            return None

    @_lock_fd.setter
    def _lock_fd(self, value: Any | None) -> None:
        """no-op setter，兼容直接赋值测试（不实际改变锁状态）。"""
        try:
            self.backend._file_lock._fd = value  # type: ignore[attr-defined]
        except AttributeError:
            pass

    # ------------------------------------------------------------------
    # 文件锁
    # ------------------------------------------------------------------

    def acquire_lock(self) -> None:
        """获取排他文件锁。重试 50/100/200ms，全部失败抛 E012。"""
        self.backend.acquire_lock()

    def release_lock(self) -> None:
        """释放文件锁。"""
        self.backend.release_lock()

    # ------------------------------------------------------------------
    # Ledger 写入
    # ------------------------------------------------------------------

    def append_event(self, event: dict[str, Any]) -> None:
        """以 append 模式写入 JSONL，写入后 flush + fsync。

        写前校验跃迁白矩阵（引擎硬约束，2026-08-29）：受约束事件若引起非法状态
        跃迁（如 pending 直接 DONE）抛 E007、事件不落盘；self-transition /
        FORCE_STATUS / RETRACT / REVIEW_CLAIMED 放行。各写命令自身仍保留前置
        校验，此处为底层硬约束兜底，防调用方绕过状态机。

        持锁兜底（task-audit-ledger-write-atomicity AC1）：写命令在命令级
        ``acquire_lock`` 内调用（backend 持锁 → 直接写）；未持锁时（单测 /
        单次 append）自动加锁写一次保证原子，backed 层持锁断言因此恒满足。
        自动加锁前先查进程级锁登记（_depth_registry）：同进程其他 Store 实例
        （如 container 布局 review 合并流 merge_lock）已持同路径锁时不重复
        flock——同一进程双 fd 对同一区域加锁会自锁死（E012），须复用已持锁。

        锁序（L-5 修正）：**先加锁 → 锁内 replay+validate → 写事件**。旧实现把
        ``replay()`` 与 ``validate_transition`` 放在加锁之前，未持锁调用方存在
        check-then-act 窗口：两个进程各自基于陈旧状态通过校验后串行落盘，后者
        写入的是非法跃迁（校验形同虚设）。

        W2-②（task-event-append-gate）：入口先做事件契约 fail-closed 校验
        （``contracts.events``）：归一化后校验，未知类型 / 缺 required 字段
        抛 E003、事件不落盘。校验读归一化副本，落盘仍用生产者原字典
        （replay 派生语义逐字节一致）。
        """
        # L7：保持调用点解析（补丁点仍为 orchd.ledger.validate_transition）。
        from orchd.ledger import validate_transition
        violations = validate_event(normalize_event(event))
        if violations:
            raise OrchdError(
                ErrorCode.E003,
                "event_contract_violated: " + "; ".join(violations),
                [{"violations": violations}],
            )
        task_id = event.get("task_id", "")
        target = _event_target_status(event)
        held = (
            self.backend._file_lock._fd is not None  # type: ignore[attr-defined]
            or str(self.lock_path.resolve()) in _depth_registry
        )
        if not held:
            self.acquire_lock()
        try:
            # 锁内校验：replay 读到的与本临界区落盘的是同一状态，校验与写入之间
            # 不再有其它写者（校验失败抛 E007 由 finally 释放锁）。
            if task_id and target:
                current = self.replay().get(task_id)
                validate_transition(
                    event.get("type", ""),
                    current.status if current else STATUS_PENDING,
                    target,
                )
            self.backend.append_event(event)
        finally:
            if not held:
                self.release_lock()
        # H4：已校准时追加 1 行（写入成功才递增；未校准则保持 None 惰性校准）
        if self._line_count is not None:
            self._line_count += 1
        # Session TTL（task-audit-session-ttl-lazy-expiry）：写命令路径事件追加
        # 成功后刷新当前会话 last_seen——写事件是宿主活动的最强信号，惰性过期
        # 据此判活。best-effort，任何异常不反向影响已成功的账本写入。
        _touch_session_last_seen(self.orchd_dir)

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def update_checkpoint(self, state: dict[str, TaskState], retracted: set[str] | None = None) -> None:
        """原子更新 checkpoint：write-to-tmp + os.replace。

        原子写入的必要性：先写入 ``.tmp`` 临时文件再通过 ``os.replace`` 原子替换，
        确保在写入过程中进程崩溃或断电时，旧的 checkpoint 仍然完整可用，
        不会因写入中断而产生半截损坏的 JSON 文件。

        正确性（P0 修复）：``retracted`` 缺省（None）时，本方法不再依赖调用方
        透传，而是直接从 ledger 扫描所有 ``RETRACT`` 事件自算完整撤回集合，
        随快照一起持久化。此前所有写操作调用 ``update_checkpoint(new_state)``
        均不传 ``retracted``，导致 checkpoint 从不写入撤回集合；一旦 checkpoint
        之后又发生新 RETRACT，全量重建时会因撤回集合不完整而让已撤回事件「复活」，
        任务状态被静默改写。

        字段漂移自愈（task-merge-failed-completed 附带修复，2026-08-25）：`state`
        通常由增量 ``replay()`` 得到——若 checkpoint 早于某个 TaskState 字段引入
        （如 review_claimed_session），增量 replay 会从旧 checkpoint 继承「缺该字段」
        的状态，再把这些字段集写回 checkpoint，导致缺漏**自我传播**、E030
        （checkpoint 快照与 ledger 重放不一致）持续无法自愈。因此这里改为基于
        ``replay_full()``（从第 1 行重放，始终含全字段）构建快照；仅当全量重放
        失败时回退到传入 ``state``。
        """
        if retracted is None:
            retracted = self._collect_retracted_event_ids()
        # 写前对账封口（task-counter-cache-reconcile D2）：计数缓存必须先
        # 接触即纠再落 ``ledger_line``——否则漂移被焊进下一份快照、自我
        # 传染（check_integrity 随后会把假基准判成「篡改」，V2' 实锤形态）。
        self.reconcile_counters()
        # H4：行数由内存计数器提供（O(1)），不再全文件数行
        total_lines = self._current_line_count()
        # P2-10 / 1.4.1：稳态用增量 state（O(tail)）；仅 checkpoint schema 版本落后
        # 时才 replay_full() 自愈字段漂移一次。这样写命令不再每次 O(L) 全量重放。
        prev = self.backend.load_checkpoint() or {}
        needs_full = prev.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION
        if needs_full:
            try:
                full_state = self.replay_full()
                tasks = {tid: ts.to_dict() for tid, ts in full_state.items()}
            except (OrchdError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as _exc:
                # 收窄：仅捕获可预期的重放/序列化异常，不吞全部 Exception
                tasks = {tid: ts.to_dict() for tid, ts in state.items()}
        else:
            tasks = {tid: ts.to_dict() for tid, ts in state.items()}
        checkpoint = {
            "ledger_line": total_lines,
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "tasks": tasks,
        }
        if retracted:
            checkpoint["retracted"] = sorted(retracted)
        # 快照基随重写保留、且**不更新其值**（task-sync-compact-bootstrap-from-state）：
        # 它必须恒为 pre-compact 原始快照——若被本函数的合成态覆盖，重建时
        # 「base + 重放全部物理事件」会把已折入 base 的事件二次应用。
        if prev.get("bootstrap_tasks") is not None:
            checkpoint["bootstrap_tasks"] = prev["bootstrap_tasks"]

        self.backend.save_checkpoint(checkpoint)

    def compact_archive(self, *, dry_run: bool = False) -> dict[str, Any]:
        """压实：终态任务事件归档至共享归档文件（task-ledger-archive-compact）。

        前缀闭包规则：逻辑事件流（归档去重 + 活跃后缀）自头起的最大连续段，
        其中每条满足「task_id 为空」或「所属任务当前终态（completed/cancelled）」。
        已归档 event_id 直接跳过（不重复搬运）；首个不满足即停（其后纵有终态
        事件亦不动——前缀性保证读合并恒为"归档+活跃"拼接，无需索引）。
        RETRACT 事件按 task_id 同规则纳入（两遍收集与重建均经透明读，
        位置无关正确性）。

        原子性：持 store 锁全程；写序为 journal → 归档重写 → 活跃重写 →
        checkpoint 重写 → 删 journal。中断崩溃后：读侧去重天然幂等；
        下次运行重算收敛；``check_integrity`` / ``_load_checkpoint`` 凭 journal
        区分"中断残留"与"篡改"。

        checkpoint 语义：重写为 ``{ledger_line: 新逻辑总数, tasks: 全量状态
        （含已归档任务）, retracted: 当前集合}``——逻辑总数压实前后不变
        （只搬运不删除逻辑事件），故 H4 计数器与各处行号比较无需改动。

        Args:
            dry_run: 只计算搬运计划（prefix/stats），不写任何文件、不删 journal。

        Returns:
            ``{"moved_events", "archived_tasks", "active_before/after": {"lines",
            "bytes"}, "checkpoint_lines", "journal_recovered", "dry_run"}``——
            后者为 c2（双文件归档）决策的增长曲线输入。
        """
        # L7：保持调用点解析（补丁点仍为 orchd.ledger.resolve_store_dir）。
        from orchd.ledger import resolve_store_dir
        store_dir = resolve_store_dir(self.orchd_dir)
        self.acquire_lock()
        try:
            return self._compact_archive_locked(store_dir, dry_run=dry_run)
        finally:
            self.release_lock()

    def _compact_archive_locked(
        self, store_dir: Path, *, dry_run: bool
    ) -> dict[str, Any]:
        from orchd.storage import (
            compact_journal_path,
            read_archive_events,
            write_archive_events,
        )

        journal = compact_journal_path(store_dir)
        # 全量状态与终态集（与 ideas 归档"组内全终态"同源语义）
        state = self.replay_full()
        terminal = {
            tid for tid, ts in state.items()
            if ts.status in TERMINAL_STATUSES
        }
        events = self._read_ledger_lines(from_line=1)
        arch, _ = read_archive_events(store_dir)
        arch_ids = {e.get("event_id", "") for e in arch if e.get("event_id")}
        moved: list[dict[str, Any]] = []
        moved_tasks: set[str] = set()
        for ev in events:
            eid = ev.get("event_id", "")
            if eid and eid in arch_ids:
                moved.append(ev)
                _tid = ev.get("task_id")
                if isinstance(_tid, str):
                    moved_tasks.add(_tid)
                continue
            tid = ev.get("task_id", "")
            if tid and tid not in terminal:
                break
            moved.append(ev)
            if tid:
                moved_tasks.add(tid)
        # moved 为逻辑前缀：suffix 即其后段（去重读已保证无交集，直接切片）
        suffix = events[len(moved):]
        stats: dict[str, Any] = {
            "moved_events": len([e for e in moved if e.get("event_id")
                                 not in arch_ids]),
            "archived_tasks": sorted(moved_tasks),
            "logical_lines": len(events),
            "dry_run": dry_run,
            "journal_recovered": False,
        }
        active_before = self._active_file_size(store_dir)
        stats["active_before"] = active_before
        if not moved or all(e.get("event_id", "") in arch_ids for e in moved):
            # 无新搬运：一致即收尾（顺手清理残留 journal，中断自愈的一部分）
            try:
                if journal.is_file():
                    journal.unlink()
                    stats["journal_recovered"] = True
            except OSError:
                pass
            stats["active_after"] = active_before
            stats["checkpoint_lines"] = len(events)
            return stats
        if dry_run:
            return stats
        journal.write_text(json.dumps({
            "op": "compact_archive",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "pre_logical_lines": len(events),
        }), encoding="utf-8")
        try:
            write_archive_events(store_dir, moved)
            self.backend.replace_active_events(suffix)  # type: ignore[attr-defined]
            self.update_checkpoint(
                {tid: ts for tid, ts in state.items()},
                self._collect_retracted_event_ids(),
            )
            # H4：内存行计数器按新逻辑总数重校准（否则后续 append 在旧基数上递增漂移）
            self._line_count = len(moved) + len(suffix)
            try:
                journal.unlink()
            except OSError:
                pass
        except Exception:
            raise
        stats["active_after"] = self._active_file_size(store_dir)
        stats["checkpoint_lines"] = len(moved) + len(suffix)
        return stats

    @staticmethod
    def _active_file_size(store_dir: Path) -> dict[str, Any]:
        """活跃账本文件体积（c2 增长曲线输入；后端无关 best-effort）。"""
        info: dict[str, Any] = {}
        for name in ("_ledger.jsonl", "_ledger.sqlite3"):
            p = Path(store_dir) / name
            try:
                info[name] = p.stat().st_size if p.is_file() else 0
            except OSError:
                info[name] = 0
        return info
