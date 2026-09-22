"""SQLite 适配器：StorageBackend 的第二实现（task-sqlite-backend）。

在 ``orchd/storage/`` 端口/适配器布局下落 SQLite 后端，一次性解决三问题：
① append 原子性（单事务 INSERT）；② retract 不再全量扫描（按 rowid 定位/删除）；
③ event_count O(1)（meta 表维护计数器）。

契约与文件适配器一致（``from orchd.storage.filesystem import FilesystemBackend``）：
- ``physical line`` 映射为 SQLite ``rowid``（AUTOINCREMENT 单调，一一对应）；
- ``corrupt_lines`` 恒为空（SQLite 事务保证行完整，无撕裂行）；
- 通过 ``Store(orchd_dir, backend=SqliteBackend(...))`` 注入即"后端可切"（Store 的
  ``backend`` 参数为既定切换点）。

默认路径：``<store_dir>/_ledger.sqlite3``；``_ledger.jsonl`` 存在时可一次性迁移。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock, _depth_registry
from orchd.storage import StorageBackend

_CHECKPOINT_KEY = "checkpoint"
_COUNT_KEY = "event_count"


class SqliteBackend(StorageBackend):
    """SQLite 存储后端：ledger 事件存于 SQLite 表，checkpoint 存于 meta 表。

    属性与 :class:`FilesystemBackend` 对齐（``orchd_dir`` / ``ledger_path`` /
    ``checkpoint_path`` / ``lock_path`` / ``_file_lock`` / ``corrupt_lines``），
    保证 Store 的路径属性与锁转发兼容。
    """

    name = "sqlite"

    def __init__(self, orchd_dir: Path, *, db_path: Path | None = None) -> None:
        self.orchd_dir = Path(orchd_dir)
        self.ledger_path = self.orchd_dir / "_ledger.jsonl"
        self.db_path = db_path or (self.orchd_dir / "_ledger.sqlite3")
        self.checkpoint_path = self.orchd_dir / "_checkpoint.json"
        self.lock_path = self.orchd_dir / ".lock"
        self._file_lock = ExclusiveFileLock(self.lock_path)
        self.corrupt_lines: list[dict[str, Any]] = []
        self._ensure_schema()

    # -- 内部 ----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "rowid INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
            )

    def _held(self) -> bool:
        return (
            self._file_lock._fd is not None
            or str(self.lock_path.resolve()) in _depth_registry
        )

    # -- StorageBackend 接口 --------------------------------------------------

    def append_event(self, event: dict[str, Any]) -> None:
        """以单事务 INSERT 追加事件（原子；持锁断言与文件适配器同口径）。"""
        if not self._held():
            raise OrchdError(
                ErrorCode.E007,
                "append_event 必须在持锁状态下调用（acquire_lock 之后）",
                [{
                    "path": str(self.db_path),
                    "hint": "写路径须先 store.acquire_lock() 再 append_event",
                }],
            )
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("INSERT INTO events (payload) VALUES (?)", (payload,))
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, '1') "
                "ON CONFLICT(k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT)",
                (_COUNT_KEY,),
            )

    def read_events(
        self, from_line: int = 1, to_line: int | None = None
    ) -> list[dict[str, Any]]:
        """读取事件（``from_line`` / ``to_line`` 为 1-based **逻辑行号**）。

        逻辑行 = 共享归档文件事件 + 本表去除归档重复后的行（按 rowid 序）。
        不依赖 rowid 稠密假设（replace_active 重建 / sqlite_sequence 不回绕 /
        崩溃残留均不改变映射规则；与文件后端"全量读 + 切片"同成本模型）。
        崩溃残留重叠按 event_id 去重（归档优先）。
        """
        from orchd.storage import read_archive_events

        self.corrupt_lines = []
        arch, arch_corrupt = read_archive_events(self.orchd_dir)
        for entry in arch_corrupt:
            self.corrupt_lines.append(entry)
        arch_ids = {e.get("event_id", "") for e in arch if e.get("event_id")}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM events ORDER BY rowid").fetchall()
        active: list[dict[str, Any]] = []
        for (payload,) in rows:
            try:
                ev = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("event_id") in arch_ids:
                continue
            active.append(ev)
        logical = arch + active
        stop = None if to_line is None else max(0, to_line)
        return logical[max(0, from_line - 1):stop]

    def event_count(self) -> int:
        """逻辑事件总数（与 read_events 全量口径一致）。"""
        return len(self.read_events())

    def replace_active_events(self, events: list[dict[str, Any]]) -> None:
        """全量重写活跃事件表（task-ledger-archive-compact，compact 专用）。

        清空后按序重插，rowid 从 1 重排（稠密），计数器同步。调用方（Store
        compact 流程）须已持 store 锁。
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM events")
            for ev in events:
                conn.execute(
                    "INSERT INTO events (payload) VALUES (?)",
                    (json.dumps(ev, ensure_ascii=False, separators=(",", ":")),))
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_COUNT_KEY, str(len(events))),
            )

    def load_checkpoint(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT v FROM meta WHERE k = ?", (_CHECKPOINT_KEY,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def save_checkpoint(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_CHECKPOINT_KEY, payload),
            )

    def acquire_lock(self) -> None:
        """获取排他文件锁（与文件适配器同一 ExclusiveFileLock 原语）。"""
        try:
            self._file_lock.acquire(blocking=True, timeout_s=10.0)
        except OrchdError:
            raise
        except Exception as exc:
            raise OrchdError(
                ErrorCode.E012,
                "lock_timeout: failed to acquire storage lock",
                [{"path": str(self.lock_path), "hint": str(exc)}],
            ) from exc

    def release_lock(self) -> None:
        self._file_lock.release()

    # -- 迁移 ----------------------------------------------------------------

    def import_jsonl(self) -> int:
        """把存量 ``_ledger.jsonl`` 一次性导入（幂等：仅当 events 表为空时执行）。

        task-ledger-archive-compact：归档文件优先导入（历史在前），再导活跃文件；
        导入后截断点推进到归档事件数（与归档文件口径对齐，后续读合并零重叠）。
        归档与活跃文件均缺失 → 0。

        Returns: 导入的事件条数（0 表示无需导入 / 源文件不存在）。
        """
        from orchd.storage import read_archive_events

        if not self.ledger_path.exists():
            return 0
        with self._connect() as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
            if n:
                return 0
            count = 0
            arch, _ = read_archive_events(self.orchd_dir)
            for ev in arch:
                conn.execute(
                    "INSERT INTO events (payload) VALUES (?)",
                    (json.dumps(ev, ensure_ascii=False, separators=(",", ":")),))
                count += 1
            count = 0
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        json.loads(stripped)
                    except json.JSONDecodeError:
                        continue  # 撕裂 / 损坏行跳过（与文件适配器的容错一致）
                    conn.execute(
                        "INSERT INTO events (payload) VALUES (?)", (stripped,))
                    count += 1
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_COUNT_KEY, str(count)),
            )
        return count
