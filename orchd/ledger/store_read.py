"""``orchd.ledger.Store`` 读侧 mixin（事件重放与派生读取）（file-split-program W1-L8，2026-10-16）。

承载 ``_StoreReadMixin``：``replay`` / ``replay_full`` / ``_load_checkpoint`` /
``_count_ledger_lines`` / ``_replay_prefix`` / ``_read_ledger_lines`` / ``replay_violations`` /
``scan_task_derived`` / ``ledger_exists`` / ``ledger_line_count``；方法体与 L7 基线逐字一致。

依赖方向：标准库 + ``orchd.contracts.events`` / ``orchd.errors`` / ``orchd.lockfile`` +
``orchd.ledger.store_types``；对 ``resolve_store_dir`` 用**函数级惰性导入**（保持
``orchd.ledger.<名>`` 的 monkeypatch 补丁面）。不得导入 ``orchd.ledger`` 聚合入口。
"""
from __future__ import annotations

import warnings

from orchd.names import (
    LEDGER_ARCHIVE_FILE,
    LEDGER_FILE,
)
from orchd.contracts.events import (
    EVENT_DONE,
    EVENT_RETRACT,
    EVENT_REVIEW_CLAIMED,
    EVENT_REVIEW_SUBMITTED,
)
from orchd.ledger.store_types import TaskDerived, TaskState
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchd.storage import StorageBackend

class _StoreReadMixin:

    # 宿主接口（**仅类型层声明**，运行期不产生任何类属性，故不进入 AST / vars() 扫描面）：
    # 属性与派生路径由 ``store.Store`` 的 ``__init__`` 与骨架 property 提供，
    # ``if TYPE_CHECKING:`` 块内的方法由兄弟段经 ``Store`` 的 MRO 拼装提供。
    # 声明只服务 mypy 严格门禁——拆段前它们同属一个类，语义零差异。
    orchd_dir: Path
    backend: StorageBackend
    _replay_violations: list[dict[str, Any]]

    if TYPE_CHECKING:
        @property
        def checkpoint_path(self) -> Path: ...
        def _apply_events(self, events: list[dict[str, Any]], tasks: dict[str, TaskState],
                          retracted: set[str], *, handle_retract: bool = True) -> None: ...
        def reconcile_counters(self) -> dict[str, Any]: ...
        def _current_line_count(self) -> int: ...

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay(self) -> dict[str, TaskState]:
        """从 checkpoint + 增量 replay 重建任务状态。

        增量优化：优先加载 checkpoint 快照，仅读取 checkpoint 之后的增量事件，
        避免每次全量遍历整个 ledger，大幅提升大型项目的 replay 性能。

        容错规则：
        - checkpoint 解析失败 → 回退全量 replay
        - 最后一行 JSON 解析失败 → 跳过 + warning
        - 中间行解析失败 → 降级跳过 + E030 warning（AC4，不再抛 E002）
        """
        checkpoint_line, tasks, retracted = self._load_checkpoint()
        events = self._read_ledger_lines(from_line=checkpoint_line + 1)
        self._replay_violations = []
        self._apply_events(events, tasks, retracted)
        return tasks

    def replay_full(self) -> dict[str, TaskState]:
        """全量 replay（忽略 checkpoint）。"""
        tasks: dict[str, TaskState] = {}
        retracted: set[str] = set()
        events = self._read_ledger_lines(from_line=1)
        self._replay_violations = []
        self._apply_events(events, tasks, retracted)
        return tasks

    def _load_checkpoint(self) -> tuple[int, dict[str, TaskState], set[str]]:
        """加载 checkpoint 文件，返回 ``(ledger_line, tasks, retracted)``。

        回退行为：
            - checkpoint 文件不存在：返回 ``(0, {}, set())``，调用者将从第 1 行全量 replay。
            - checkpoint 解析失败（JSON 损坏 / 字段缺失）：打印 warning，自动调用
              :meth:`replay_full` 全量重建状态，并返回 ``(total_lines, tasks, set())``。
              此时 ``total_lines`` 是 ledger 的总行数，表示已全量 replay 完毕，
              调用者无需再读取增量事件。

        Returns:
            三元组：
            - ``int``: checkpoint 对应的 ledger 行号（增量 replay 从该行之后开始）。
            - ``dict[str, TaskState]``: 截至该行号的任务状态快照。
            - ``set[str]``: 已撤回的事件 ID 集合。
        """
        # L7：保持调用点解析（补丁点仍为 orchd.ledger.resolve_store_dir）。
        from orchd.ledger import resolve_store_dir
        data = self.backend.load_checkpoint()
        if data is None:
            # backend 返回 None：区分「checkpoint 不存在」与「解析失败」。
            # 前者正常返回空快照；后者回退全量 replay（保持既有 warning 语义）。
            if not self.checkpoint_path.exists():
                return 0, {}, set()
            warnings.warn("checkpoint 解析失败，回退全量 replay", stacklevel=2)
            tasks = self.replay_full()
            # 返回一个特殊标记让调用方知道已全量 replay。写标记前先接触即纠
            # （task-counter-cache-reconcile code 审查返修）：total_lines 必须
            # 取实体口径——漂移值会让调用方把已重放的头部事件二次喂入（漂小）
            # 或跳过未读增量（漂大）。
            self.reconcile_counters()
            total_lines = self._count_ledger_lines()
            return total_lines, tasks, set()
        # task-ledger-archive-compact：compact 中断恢复。journal 存在说明上次
        # 三写（归档→活跃→checkpoint）未走完：checkpoint 行号不可信，直接全量
        # 重放（去重读天然幂等），与解析失败分支同形。
        try:
            from orchd.storage import compact_journal_path

            if compact_journal_path(
                    resolve_store_dir(self.orchd_dir)).is_file():
                tasks = self.replay_full()
                # journal 在场的三写中断态与漂移并存时，兜底总行数同样须取
                # 实体口径（code 审查返修，封口同上一分支）。
                self.reconcile_counters()
                total_lines = self._count_ledger_lines()
                return total_lines, tasks, set()
        except Exception:
            pass
        # 兼容旧格式：tasks 为 dict[str, TaskState.from_dict]
        ledger_line = data.get("ledger_line", 0)
        tasks = {
            tid: TaskState.from_dict(ts) for tid, ts in data.get("tasks", {}).items()
        }
        retracted = set(data.get("retracted", []))
        return ledger_line, tasks, retracted

    def _count_ledger_lines(self) -> int:
        return self.backend.event_count()

    def _replay_prefix(self, n: int) -> dict[str, TaskState]:
        """重放 ledger 前 ``n`` 条事件，返回派生任务状态（忽略 checkpoint）。

        供 :meth:`check_integrity` 使用：以 checkpoint 声明的 ``ledger_line``
        为界，重放该前缀事件并与 checkpoint 的 ``tasks`` 快照比对，检测运行时
        文件（ledger / checkpoint）被手改的篡改。

        注意：停用 RETRACT 递归（``handle_retract=False``，task-audit-ledger-
        state-machine-dedup 合并去重后统一走 :meth:`_apply_events`）——前缀重放只
        关心前 ``n`` 条事件的派生结果，若 RETRACT 触发全量重建会越过前缀边界读到
        ``n`` 之后的事件，破坏前缀语义。

        L-11（2026-09-15）：``n`` 是**物理行号**（checkpoint.ledger_line 的口径），
        故按物理行切片而非 ``read_events()[:n]``（解析事件序号）——撕裂/损坏行会让
        两者错位（少一行事件 ⇒ 序号 n 对应物理行 n+1），旧实现因此多带一条事件，
        派生结果与 checkpoint 快照不符，把引擎自身的撕裂行误报成「疑似被篡改」。
        """
        events = self.backend.read_events(from_line=1, to_line=n)
        tasks: dict[str, TaskState] = {}
        retracted: set[str] = set()
        # 前缀重放是独立观测：重置违规收集，避免与调用方先前的全量 replay 混淆
        self._replay_violations = []
        # 与 _rebuild_after_retract 一致：先完整收集前缀内的 RETRACT 目标，
        # 再应用非撤回事件，避免被撤回事件「复活」。
        for ev in events:
            if ev.get("type") == EVENT_RETRACT:
                te = ev.get("target_event_id", "")
                if te:
                    retracted.add(te)
        self._apply_events(events, tasks, retracted, handle_retract=False)
        return tasks

    def _read_ledger_lines(self, from_line: int) -> list[dict[str, Any]]:
        """读取 ledger 从 ``from_line``（1-based）起的所有事件。

        参数 ``from_line`` 使用 1-based 行号，即 ``from_line=1`` 表示从文件第一行开始读取。
        内部实现委托 ``backend.read_events(from_line)``，在文件层先跳过前
        ``from_line - 1`` 行，从而定位到起始行（B-1 修复：被跳过的早期损坏行
        不参与解析，恢复 checkpoint 增量 replay 的容错语义）。

        容错：末行解析失败跳过 + warning；中间行失败降级跳过 + E030 warning
        （AC4，不再抛 E002 中断引擎）。
        """
        return self.backend.read_events(from_line=from_line)

    @property
    def replay_violations(self) -> list[dict[str, Any]]:
        """最近一次 replay 收集到的序列级软校验违规（L-3，只读观测）。

        与 :meth:`check_integrity` 的区别：这里记录的是**语义可疑但派生状态照旧
        应用**的序列（如跨设备双认领），不构成篡改判定，也不新增 E030 误报；
        消费方是 sync 合并响应（结构化字段 / warnings）。
        """
        return list(getattr(self, "_replay_violations", []) or [])

    def scan_task_derived(self) -> TaskDerived:
        """单次扫描 ledger 构建 per-task 派生信息缓存（H2，2026-08-13 性能审核）。

        一次 O(L) 遍历同时提取 DONE 最近事件 / REVIEW_SUBMITTED 意见 /
        REVIEW_CLAIMED baseline 三类信息，供 request/claim/done/review_submit
        的多次查询复用（原实现每个 ``_extract_*`` 查询都从头全扫，单命令内
        可能 2-4 次 O(L)）。ledger 不存在时返回空缓存。
        """
        info = TaskDerived()
        if not self.ledger_exists():
            return info
        # M-1 合并扫描：单次读取后同时收集撤回集合与派生信息（3→1）
        all_events = self._read_ledger_lines(from_line=1)
        retracted: set[str] = set()
        for ev in all_events:
            if ev.get("type") == EVENT_RETRACT:
                te = ev.get("target_event_id", "")
                if te:
                    retracted.add(te)
        for event in all_events:
            if event.get("event_id", "") in retracted:
                continue
            etype = event.get("type", "")
            tid = event.get("task_id", "")
            if not tid:
                continue
            if etype == EVENT_DONE:
                # 正序遍历，后者覆盖前者 → last_done 保持「最近一次」
                info.last_done[tid] = event
            elif etype == EVENT_REVIEW_SUBMITTED:
                comments = event.get("comments")
                if comments:
                    info.review_comments.setdefault(tid, []).append(comments)
                # task-review-comments-gate-and-stale-timeout（B）：与
                # extract_review_comments 对齐，历史空打回注入占位（derived 缓存路径）。
                elif event.get("verdict") == "CHANGES_REQUESTED":
                    info.review_comments.setdefault(tid, []).append(
                        "[该次打回未附审查意见（历史数据），请联系审查者补充；"
                        "当前版本已强制要求 CHANGES_REQUESTED 必须附意见]"
                    )
            elif etype == EVENT_REVIEW_CLAIMED:
                aid = event.get("agent_id")
                if aid:
                    # 正序遍历后者覆盖 → (tid, aid) 保持「最近一次」的 baseline
                    _baseline_sha = event.get("baseline_sha")
                    if isinstance(_baseline_sha, str):
                        info.review_baselines[(tid, aid)] = _baseline_sha
        return info

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def ledger_exists(self) -> bool:
        """账本是否存在（三载体先判，无后端副作用）。

        task-sqlite-robustness-batch：直接判 ``_ledger.jsonl`` /
        ``_ledger.sqlite3`` / 共享归档任一存在——不再经后端构造即建库的
        sqlite 语义恒真（此前新建 Store 即 True，无账本状态不可检测，
        12 处 ``if not ledger_exists`` 守卫退化为读空列表）。
        """
        # L7：保持调用点解析（补丁点仍为 orchd.ledger.resolve_store_dir）。
        from orchd.ledger import resolve_store_dir
        try:
            store_dir = resolve_store_dir(self.orchd_dir)
        except Exception:
            store_dir = Path(self.orchd_dir)
        return (
            (store_dir / LEDGER_FILE).is_file()
            or (store_dir / "_ledger.sqlite3").is_file()
            or (store_dir / LEDGER_ARCHIVE_FILE).is_file()
        )

    def ledger_line_count(self) -> int:
        """返回 ledger 文件的总行数（即事件总数），用于进度展示和 checkpoint 对齐。"""
        return self._current_line_count()
