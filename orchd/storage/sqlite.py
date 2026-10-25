"""SQLite 适配器：StorageBackend 的第二实现（task-sqlite-backend）。

在 ``orchd/storage/`` 端口/适配器布局下落 SQLite 后端，一次性解决三问题：
① append 原子性（单事务 INSERT）；② retract 不再全量扫描（按 rowid 定位/删除）；
③ event_count O(1)（meta 表维护计数器）。

契约与文件适配器一致（``from orchd.storage.filesystem import FilesystemBackend``）：
- ``physical line`` 映射为 SQLite ``rowid``（AUTOINCREMENT 单调，一一对应）；
- ``corrupt_lines`` 收集归档段损坏行；``import_jsonl`` 跳过的迁移源
  撕裂行同样以同形条目记入（doctor 经既有 DR-14 通道告警，不静默丢弃）；
- 通过 ``Store(orchd_dir, backend=SqliteBackend(...))`` 注入即"后端可切"（Store 的
  ``backend`` 参数为既定切换点）。

默认路径：``<store_dir>/_ledger.sqlite3``；``_ledger.jsonl`` 存在时可一次性迁移。
启用方式（task-sqlite-cutover-default，B-max 切默认）：缺省即本后端（``Store``
构造经 ``ledger._select_backend`` 选中，``ORCHD_STORAGE_BACKEND`` 未设/空即
sqlite）；显式 ``ORCHD_STORAGE_BACKEND=file`` 切回文件后端。首次使用自动迁移
（``import_jsonl``，成功后存量文件改名备份；回滚用 ``export_jsonl`` 导出）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock, _depth_registry
from orchd.storage import StorageBackend

# 本文件不直接读环境变量：后端选择由 ``orchd.ledger.backend`` 按
# ``orchd.constants.ORCHD_STORAGE_BACKEND_ENV`` 决定（文首 docstring 里的变量名仅说明语义）。
_CHECKPOINT_KEY = "checkpoint"
_COUNT_KEY = "event_count"

#: 计数缓存纠偏哨兵键（task-counter-cache-reconcile D4）：累计纠正次数与
#: 末次明细（JSON：from/to/at）。持续非零 = 有新的写路径在制造漂移。
_RECALIB_COUNT_KEY = "count_recalibrations"
_RECALIB_LAST_KEY = "count_recalib_last"

#: 迁移跳过的源文件撕裂行留痕（meta 表键，task-readonly-no-migrate）。
_IMPORT_SKIPPED_KEY = "import_skipped"

#: 迁移备份后缀（见上）。
_MIGRATED_BACKUP_SUFFIX = ".pre-sqlite-bak"

#: 迁移备份后缀（task-sqlite-cutover-default）：``import_jsonl`` 成功后把
#: 存量 ``_ledger.jsonl`` / ``_checkpoint.json`` 改名备份（不删除）——既消除
#: 陈旧直读（hook grep / 旧工具按文件名读 jsonl 会拿到过期数据），又保留回滚
#: 路径（改回文件名 + 切回 file 后端即回滚）。``.bak`` 尾缀不命中 doctor 残留
#: 模式（残留只认锁/session/proposal 等固定模式），不会被清理盯上。


class SqliteBackend(StorageBackend):
    """SQLite 存储后端：ledger 事件存于 SQLite 表，checkpoint 存于 meta 表。

    属性与 :class:`FilesystemBackend` 对齐（``orchd_dir`` / ``ledger_path`` /
    ``checkpoint_path`` / ``lock_path`` / ``_file_lock`` / ``corrupt_lines``），
    保证 Store 的路径属性与锁转发兼容。
    """

    name = "sqlite"

    def __init__(
        self, orchd_dir: Path, *, db_path: Path | None = None,
        readonly: bool = False,
    ) -> None:
        """初始化后端。

        Args:
            orchd_dir: 账本根目录。
            db_path: 库文件覆写（缺省 ``<账本根>/_ledger.sqlite3``）。
            readonly: 只读模式（task-readonly-no-migrate）：跳过
                ``_ensure_schema``（不建库），连接以 SQLite ``mode=ro``
                打开——库文件缺失时直接抛错而非创建。doctor 等只读入口
                经此模式读取已迁移部署，绝不触发建库 / 迁移 / 改名。
        """
        self.orchd_dir = Path(orchd_dir)
        self.ledger_path = self.orchd_dir / "_ledger.jsonl"
        self.db_path = db_path or (self.orchd_dir / "_ledger.sqlite3")
        self.checkpoint_path = self.orchd_dir / "_checkpoint.json"
        self.lock_path = self.orchd_dir / ".lock"
        self._file_lock = ExclusiveFileLock(self.lock_path)
        self.corrupt_lines: list[dict[str, Any]] = []
        self._import_skipped: list[dict[str, Any]] = []
        self.readonly = readonly
        if not readonly:
            self._ensure_schema()

    # -- 内部 ----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            return sqlite3.connect(
                f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextmanager
    def _connected(self) -> Iterator[sqlite3.Connection]:
        """连接会话（task-sqlite-robustness-batch）：提交/回滚并显式 close。

        ``sqlite3.Connection`` 自身上下文只 commit 不 close；异常 traceback
        持有连接帧时 Windows 上库文件改名撞 WinError 32。本方法用法与
        :meth:`_connect` 的 ``with`` 形态一致，退出必 close。
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """建表（幂等）。

        并发构造（task-migration-store-lock）：两进程同时首开同一账本根时，
        ``PRAGMA journal_mode`` 会短暂撞 ``database is locked``——DDL 均为
        IF NOT EXISTS 幂等，有界重试（~5s）而非直接抛错；建表本身的并发安全
        由 SQLite 锁语义保证。
        """
        import time as _time

        last: Exception | None = None
        for _ in range(50):
            try:
                with self._connected() as conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS events ("
                        "rowid INTEGER PRIMARY KEY AUTOINCREMENT,"
                        " payload TEXT NOT NULL)"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS meta "
                        "(k TEXT PRIMARY KEY, v TEXT NOT NULL)"
                    )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last = exc
                _time.sleep(0.1)
        assert last is not None
        raise last

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
        with self._connected() as conn:
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
        不依赖 rowid 稠密假设（replace_active 重建后 AUTOINCREMENT 不回绕 /
        崩溃残留均不改变映射规则；窗口按位置（``OFFSET``）取，与文件后端
        ``_read_active_range`` 的物理切片同口径，读后去重）。
        崩溃残留重叠按 event_id 去重（归档优先）。

        增量（task-sqlite-robustness-batch）：表段按 ``ORDER BY rowid
        LIMIT/OFFSET`` 取，不再全表 SELECT——``to_line`` 落在归档段时免
        events 表扫描；``from_line`` 越过归档段时按位置跳行（与文件端"跳过
        前 from-1 行再解析"同成本承诺）。
        """
        from orchd.storage import read_archive_events

        # 迁移期跳过的源文件撕裂行常驻（内存 + meta 持久化双源；read_events
        # 每次重建 corrupt_lines，此处回填，doctor 经 DR-14 通道持续可见，
        # 跨进程不丢失，直至源文件被处理）。
        self.corrupt_lines = list(getattr(self, "_import_skipped", []))
        arch, arch_corrupt = read_archive_events(self.orchd_dir)
        for entry in arch_corrupt:
            self.corrupt_lines.append(entry)
        try:
            with self._connected() as _conn:
                _row = _conn.execute(
                    "SELECT v FROM meta WHERE k = ?",
                    (_IMPORT_SKIPPED_KEY,)).fetchone()
            if _row is not None:
                _persisted = json.loads(_row[0])
                if isinstance(_persisted, list):
                    _seen = {
                        (e.get("line"), e.get("message"))
                        for e in self.corrupt_lines
                        if isinstance(e, dict)
                    }
                    for e in _persisted:
                        if (isinstance(e, dict)
                                and (e.get("line"), e.get("message")) not in _seen):
                            self.corrupt_lines.append(e)
        except Exception:
            pass  # 旧库无该键 / 库不可读：best-effort，不阻断读
        ka = len(arch)
        s = max(0, from_line - 1)
        if to_line is not None and to_line <= ka:
            return arch[s:to_line]
        arch_ids = {e.get("event_id", "") for e in arch if e.get("event_id")}
        # 位置窗口（与文件端物理切片同口径，读后去重；禁 WHERE rowid BETWEEN——
        # replace_active 后 rowid 不回绕，值切片隐含稠密假设，spec 返工项）。
        a = max(1, from_line - ka)
        with self._connected() as conn:
            if to_line is None:
                rows = conn.execute(
                    "SELECT payload FROM events ORDER BY rowid "
                    "LIMIT -1 OFFSET ?", (a - 1,)).fetchall()
            else:
                limit = max(0, to_line - ka - (a - 1))
                rows = conn.execute(
                    "SELECT payload FROM events ORDER BY rowid "
                    "LIMIT ? OFFSET ?", (limit, a - 1)).fetchall()
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
        if from_line <= ka:
            return arch[s:] + active
        return active

    def event_count(self) -> int:
        """逻辑事件总数（O(1)：读 meta 计数器——**加速缓存，非权威**，D1）。

        权威口径是实体重算：``归档解析成功数 + COUNT(*) FROM events``，经
        :meth:`recalibrate_event_count` 归位本缓存；计数器由
        append_event（+1）/ replace_active_events（同步总数）/
        import_jsonl（归档 + 活跃总数）维护。meta 缺失（旧库）时回退全量
        口径（与旧实现一致，不抛错）。
        """
        with self._connected() as conn:
            row = conn.execute(
                "SELECT v FROM meta WHERE k = ?", (_COUNT_KEY,)).fetchone()
        if row is None:
            return len(self.read_events())
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return len(self.read_events())

    def _count_truth(self, conn: sqlite3.Connection) -> int:
        """实体重算真值：归档解析成功数 + 活跃表物理行数（与缓存口径同源）。

        归档读失败按 0 计（与 replace/import 写回口径一致——三处维护点与
        真值重算共用同一条「arch_n + active」公式，杜绝口径分叉）。
        """
        from orchd.storage import read_archive_events

        try:
            arch, _ = read_archive_events(self.orchd_dir)
            arch_n = len(arch)
        except Exception:
            arch_n = 0
        (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return arch_n + int(n)

    def recalibrate_event_count(self) -> dict[str, Any] | None:
        """计数缓存接触即纠（task-counter-cache-reconcile D2）。

        与 :meth:`_count_truth` 比对 meta 持久缓存：漂移则在**同一连接事务**
        内写回真值并累计哨兵键（D4）；只读后端无法写回，仍返回
        ``repaired=False`` 的明细供消费方取真值——漂移被命名，不被误审。

        只动计数缓存与观测键，**绝不触碰 events 实体**（AC 红线）。
        """
        with self._connected() as conn:
            if not self.readonly:
                # BEGIN IMMEDIATE：truth 读取与缓存写回同事务、写意图即刻互斥，
                # 与并发 append（持 store 锁 +1）串行化，杜绝「读旧写新」窗口。
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT v FROM meta WHERE k = ?", (_COUNT_KEY,)).fetchone()
            if row is None:
                return None  # 旧库无计数器：event_count 已回退全量口径，无缓存可纠
            try:
                cached = int(row[0])
            except (TypeError, ValueError):
                cached = -1  # 非数值缓存形态同样按漂移归位
            truth = self._count_truth(conn)
            if cached == truth:
                return None
            if not self.readonly:
                stamp = datetime.now(timezone.utc).isoformat(
                    timespec="seconds")
                conn.execute(
                    "INSERT INTO meta (k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (_COUNT_KEY, str(truth)))
                conn.execute(
                    "INSERT INTO meta (k, v) VALUES (?, '1') "
                    "ON CONFLICT(k) DO UPDATE SET v = "
                    "CAST(CAST(v AS INTEGER) + 1 AS TEXT)",
                    (_RECALIB_COUNT_KEY,))
                conn.execute(
                    "INSERT INTO meta (k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (_RECALIB_LAST_KEY, json.dumps(
                        {"from": cached, "to": truth, "at": stamp},
                        ensure_ascii=False)))
            return {"from": cached, "to": truth, "repaired": not self.readonly}

    def counter_reconcile_stats(self) -> dict[str, Any]:
        """纠偏哨兵读数（D4）：累计次数 + 末次明细；纯读，不触发纠偏。"""
        out: dict[str, Any] = {"count": 0, "last": None}
        try:
            with self._connected() as conn:
                row = conn.execute(
                    "SELECT v FROM meta WHERE k = ?", (_RECALIB_COUNT_KEY,)
                ).fetchone()
                if row is not None:
                    out["count"] = int(row[0])
                last = conn.execute(
                    "SELECT v FROM meta WHERE k = ?", (_RECALIB_LAST_KEY,)
                ).fetchone()
                if last is not None:
                    try:
                        out["last"] = json.loads(last[0])
                    except (json.JSONDecodeError, TypeError):
                        out["last"] = None
        except Exception:
            pass  # 观测面降级不阻断（旧库无键 / 读失败）
        return out

    def replace_active_events(self, events: list[dict[str, Any]]) -> None:
        """全量重写活跃事件表（task-ledger-archive-compact，compact 专用）。

        清空后按序重插，rowid 从 1 重排（稠密），计数器同步。调用方（Store
        compact 流程）须已持 store 锁。
        """
        with self._connected() as conn:
            conn.execute("DELETE FROM events")
            for ev in events:
                conn.execute(
                    "INSERT INTO events (payload) VALUES (?)",
                    (json.dumps(ev, ensure_ascii=False, separators=(",", ":")),))
            # N2（计数器口径对齐）：逻辑总数 = 归档文件数 + 本次重插活跃数，
            # 与 import_jsonl 口径一致。读失败按 0 计，不阻断 compact。
            from orchd.storage import read_archive_events

            try:
                _arch, _ = read_archive_events(self.orchd_dir)
                _arch_n = len(_arch)
            except Exception:
                _arch_n = 0
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_COUNT_KEY, str(_arch_n + len(events))),
            )

    def load_checkpoint(self) -> dict[str, Any] | None:
        with self._connected() as conn:
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
        with self._connected() as conn:
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

    def ledger_exists(self) -> bool:
        """账本库文件是否存在（构造即建库，故新项目也为真）。"""
        return self.db_path.exists()

    def create_empty_ledger(self) -> str | None:
        """sqlite 后端账本库在构造时已建（``_ensure_schema``），无需额外文件。"""
        return None

    # -- 迁移 ----------------------------------------------------------------

    def needs_migration(self) -> bool:
        """首次迁移触发条件（task-cutover-empty-check + 中断续跑）。

        Fresh：活跃 ``_ledger.jsonl`` 在场、无迁移备份、库内 ``events`` 表
        物理行数为 0（判空用 ``SELECT COUNT(*)`` 物理口径——旧条件
        ``event_count() == 0`` 是逻辑口径，做过 compact 的宿主因归档行数恒
        非零而永不迁移，活跃段永久隐身）。
        Resume（task-migrate-backup-atomicity）：账本备份已在、活名 jsonl
        已无、活名 checkpoint 仍在——即上次两步改名中断在第二步，重跑只续
        改名，不重导。活名 jsonl + 备份并存属歧义态（回滚后新写 vs 中断），
        一律 False，交 doctor mismatch 项判给人工。
        只读后端不调（``_select_backend`` 只读分支直接返回）。
        """
        live = self.ledger_path.is_file()
        ledger_bak = self.ledger_path.with_name(
            self.ledger_path.name + _MIGRATED_BACKUP_SUFFIX).is_file()
        if live and not ledger_bak:
            with self._connected() as conn:
                (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
            return n == 0
        cp_live = self.checkpoint_path.is_file()
        if ledger_bak and not live and cp_live:
            return True
        return False

    def import_jsonl(self) -> int:
        """把存量 ``_ledger.jsonl`` 一次性导入（幂等：仅当 events 表为空时执行）。

        task-ledger-archive-compact：归档文件优先导入（历史在前），再导活跃文件；
        导入后截断点推进到归档事件数（与归档文件口径对齐，后续读合并零重叠）。
        归档与活跃文件均缺失 → 0。

        task-sqlite-cutover-default：导入成功后把存量 ``_ledger.jsonl`` 与
        ``_checkpoint.json``（若存在）改名备份（``_MIGRATED_BACKUP_SUFFIX``），
        不删除——陈旧文件若保留原名会被文件直读路径（hook grep 等）当成真相
        （静默读到过期数据），改名既消歧又保留回滚路径。改名失败抛结构化
        E007（fail-closed：不给"库里有数、文件里是旧数"的分裂部署开绿灯）。

        Returns: 导入的事件条数（归档 + 活跃总数；N3：此前硬重置丢归档部分）。

        并发安全（task-migration-store-lock）：判空与插入在同一
        ``BEGIN IMMEDIATE`` 事务内——后到的进程在 BEGIN 即被阻塞，提交后重见
        非空表而跳过。归档已入库的 event_id 在活跃段跳过（归档优先，与读侧
        去重同口径），计数按实际入库行累计。

        跳过的源文件撕裂行以 ``{"line": 行号, "message": ...}`` 条目记入
        ``self.corrupt_lines``（与归档段损坏同形），并持久化进 meta 表
        （``import_skipped`` 键），doctor 的 sqlite event_schema 检查经既有
        通道一并告警——迁移丢行不再静默，且跨进程可见。
        """
        from orchd.storage import read_archive_events

        live = self.ledger_path.is_file()
        count = 0
        if live:
            with self._connected() as conn:
                conn.execute("BEGIN IMMEDIATE")
                (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
                if n == 0:
                    count = self._import_all_locked(conn)
        # 备份段（task-migrate-backup-atomicity）：续跑态（表非空但改名中断）
        # 同样落到此处收敛；无活名文件即 no-op。目标占用见
        # :meth:`_backup_migrated_files` 预检（fail-closed，不静默覆盖）。
        self._backup_migrated_files()
        return count

    def _import_all_locked(self, conn: "sqlite3.Connection") -> int:
        """在调用方已持 ``BEGIN IMMEDIATE`` 事务内全量导入（归档 + 活跃）。

        由 :meth:`import_jsonl` 调用；计数/留痕语义见该方法 docstring。
        """
        from orchd.storage import read_archive_events

        count = 0
        arch, _ = read_archive_events(self.orchd_dir)
        arch_ids = {e.get("event_id", "") for e in arch if e.get("event_id")}
        for ev in arch:
            conn.execute(
                "INSERT INTO events (payload) VALUES (?)",
                (json.dumps(ev, ensure_ascii=False, separators=(",", ":")),))
            count += 1
        # N3：此前此处 `count = 0` 硬重置丢归档部分——归档计数累计保留。
        skipped: list[dict[str, Any]] = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    active_ev = json.loads(stripped)
                except json.JSONDecodeError:
                    entry = {
                        "line": lineno,
                        "message": (
                            f"迁移跳过 _ledger.jsonl 第 {lineno} 行非法 JSON"
                            "（已丢弃，需人工核对源文件）"
                        ),
                    }
                    self.corrupt_lines.append(entry)
                    self._import_skipped.append(entry)
                    skipped.append(entry)
                    continue
                if (isinstance(active_ev, dict)
                        and active_ev.get("event_id") in arch_ids):
                    continue  # 归档已入库，活跃段重叠跳过（归档优先）
                conn.execute(
                    "INSERT INTO events (payload) VALUES (?)", (stripped,))
                count += 1
        conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (_COUNT_KEY, str(count)),
        )
        if skipped:
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (_IMPORT_SKIPPED_KEY,
                 json.dumps(skipped, ensure_ascii=False)),
            )
        return count

    def _backup_migrated_files(self) -> None:
        """迁移备份：存量账本文件改名（见 ``_MIGRATED_BACKUP_SUFFIX``）。

        task-migrate-backup-atomicity：两步改名前先预检两个目标都不存在——
        任一备份已占用即 E007 fail-closed（不静默覆盖回滚素材），且此时两个
        活名文件原封不动（上次是"第一个已改、第二个炸"，本次不再制造半吊子
        状态）。经加固原语 ``_atomic_replace``（Windows 句柄争用有界重试；与
        ``FilesystemBackend`` 同源，禁第二套实现——惰性导入避开
        ``ledger → storage`` 顶层环，依赖边见 conventions 声明图）。
        """
        from orchd.ledger import _atomic_replace

        names = ("_ledger.jsonl", "_checkpoint.json")
        pending = [n for n in names if (self.orchd_dir / n).is_file()]
        for name in pending:
            target = self.orchd_dir / (name + _MIGRATED_BACKUP_SUFFIX)
            if target.is_file():
                raise OrchdError(
                    ErrorCode.E007,
                    f"storage_migrate_backup_occupied: {target} 已存在",
                    [{"path": str(target),
                      "hint": "备份目标被占用，不覆盖；人工核对后清理或改名再重试"}],
                )
        for name in pending:
            src = self.orchd_dir / name
            try:
                _atomic_replace(src, src.with_name(name + _MIGRATED_BACKUP_SUFFIX))
            except OrchdError as exc:
                raise OrchdError(
                    ErrorCode.E007,
                    f"storage_migrate_backup_failed: {src} 改名备份失败",
                    [{"path": str(src), "error": str(exc),
                      "hint": "迁移数据已入库；手动改名该文件后重试"}],
                ) from exc
            except OSError as exc:
                raise OrchdError(
                    ErrorCode.E007,
                    f"storage_migrate_backup_failed: {src} 改名备份失败",
                    [{"path": str(src),
                      "error": f"{type(exc).__name__}: {exc}",
                      "hint": "迁移数据已入库；手动改名该文件后重试"}],
                ) from exc

    def export_jsonl(self) -> int:
        """把逻辑事件全量导出回 ``_ledger.jsonl``（回滚 file 后端用）。

        task-sqlite-cutover-default：覆盖写活跃文件（逻辑事件去重保序全量，
        与文件后端行格式一致）；归档文件原样保留（读侧按 event_id 去重，
        归档优先——与文件后端语义一致）；meta checkpoint 同步写回
        ``_checkpoint.json``。返回导出事件数。调用方（回滚流程）负责切回
        file 后端；db 文件保留（后续切回 sqlite 可直接复用，不触发重复导入）。

        task-storage-rollback-cmd：两处落盘均走原子档（tmp + fsync +
        ``_atomic_replace`` + 目录 fsync）——此前是全模块唯一的裸覆盖写，
        崩溃造撕裂 jsonl；成功后无 tmp 残留。
        """
        from orchd.ledger import _atomic_replace, _fsync_dir

        def _write_atomic(path: Path, text: str) -> None:
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            _atomic_replace(tmp, path)
            _fsync_dir(path.parent)

        events = self.read_events()
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            self.ledger_path,
            "".join(json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
                    + "\n" for ev in events),
        )
        cp = self.load_checkpoint()
        if cp is not None:
            _write_atomic(
                self.checkpoint_path,
                json.dumps(cp, ensure_ascii=False, indent=2) + "\n",
            )
        return len(events)
