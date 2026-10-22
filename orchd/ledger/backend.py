"""orchd 存储后端选择域（file-split-program W1-L5，2026-10-15）。

本模块自 ``orchd/ledger/__init__.py`` 整块搬出后端选择族（6 函数 + 1 私有常量，语句级等价，
零逻辑改动）：账本状态可用性单一真源（``ledger_status_available`` + 两个空态探测）、
``ORCHD_STORAGE_BACKEND`` 归一化（``resolve_backend_kind``）、后端构造（``_select_backend``）
与对外的 ``open_store``。

依赖方向：顶层依赖标准库、``orchd.errors``、``orchd.storage``（``StorageBackend``）与
``orchd.storage.filesystem``（``FilesystemBackend``）；``SqliteBackend`` 沿用搬移前的函数级
惰性导入。对 ``resolve_store_dir``（paths 域）与 ``Store``（store 域，L7 起在 ledger/store.py）
的引用一律用**函数级惰性导入** ``from orchd.ledger import ...``：① 避免与聚合入口形成加载期环；
② 保持「调用点解析」语义——既有探针 ``monkeypatch.setattr("orchd.ledger.resolve_store_dir", ...)``
（tests/test_store_isolation_guard.py、tests/test_doctor.py）的补丁面不变。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from orchd.errors import ErrorCode, OrchdError
from orchd.storage import StorageBackend
from orchd.storage.filesystem import FilesystemBackend

# 仅类型检查期存在：运行期由 open_store 内的函数级惰性导入解析，避免与聚合入口成环
if TYPE_CHECKING:
    from orchd.ledger import Store


def ledger_status_available(store_dir: Path) -> bool:
    """账本状态可读性的**单一真源**（后端无关）：事件载体非空才算可用。

    卡 2（task-terminal-exemption-availability）：``validate`` 的终态豁免降级、
    脚本侧 ``verify_registry_paths._ledger_status_available``（转发本函数）与声明
    门禁的 skip 谓词共用同一判定，禁写第二份。

    判据：``_ledger.jsonl`` 非空 / 归档件非空 / ``_ledger.sqlite3`` 的 ``events``
    表有活跃事件 / ``_checkpoint.json`` **可解析为非空对象**（checkpoint 是合法
    状态载体：``Store.replay`` 能从中重建任务状态）。判定实质是「状态可重建」，
    故排除的是**空载体**（构造即建库的空库、0 字节 jsonl、空对象 checkpoint），
    而非某个载体类型——空载体判成可用会让 ``replay()`` 返回空、终态集合变空，
    进而把终态任务的历史缺陷整片误报（CI 新克隆实测 E037×16）。
    修正记录：task-ledger-status-checkpoint-carrier（不得按类型排除 checkpoint）。

    Args:
        store_dir: 账本根（:func:`resolve_store_dir` 的返回值）。

    Returns:
        事件载体非空为 ``True``；其余（含任何读取异常）为 ``False``
        （fail-closed：调用方据此走**显式降级**，而非当作可用后误报）。
    """
    from orchd.storage import ARCHIVE_FILENAME

    try:
        ledger = store_dir / "_ledger.jsonl"
        if ledger.is_file() and ledger.stat().st_size > 0:
            return True
        archive = store_dir / ARCHIVE_FILENAME
        if archive.is_file() and archive.stat().st_size > 0:
            return True
        sqlite_path = store_dir / "_ledger.sqlite3"
        if sqlite_path.is_file() and _sqlite_has_state(sqlite_path):
            return True
        # checkpoint 是**合法**状态载体（`Store.replay` 可从中重建任务状态，实测见
        # tests/test_gate_ci_wiring.py::TestLedgerStatusIsReadFromEngine 的夹具）：
        # 只按「内容非空」排除空载体，不按载体类型排除
        # （task-ledger-status-checkpoint-carrier 修正过度收紧）。注意 sqlite 后端下
        # checkpoint 落在 meta 表而非该文件，故上一个分支必须同时看 meta。
        return _checkpoint_has_state(store_dir / "_checkpoint.json")
    except (OSError, ValueError, TypeError):
        return False


def _checkpoint_has_state(path: Path) -> bool:
    """file 后端的 checkpoint 是否含非空状态（0 字节 / 非法 JSON / 空对象 ``{}`` → False）。"""
    import json as _json

    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data)


def _sqlite_has_state(path: Path) -> bool:
    """库内是否存在可重建状态（只读 URI 连接：不建库、不迁移）。

    两条通道任一非空即算「状态可重建」：``events`` 表有活跃事件；或 ``meta`` 表有键
    ——sqlite 后端的 checkpoint 经 ``save_checkpoint`` 落 meta 表，此时 events 为空
    但状态仍在（实测 tests/test_gate_ci_wiring.py::TestLedgerStatusIsReadFromEngine
    的夹具正是此形态）。**构造即建库的空库**（events 与 meta 均 0 行）必须判
    ``False``——「载体存在 ≠ 状态可重建」的关键样本。读失败一律 ``False``（fail-closed）。
    """
    import sqlite3

    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            (n,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
            if int(n) > 0:
                return True
            (m,) = conn.execute("SELECT COUNT(*) FROM meta").fetchone()
            return int(m) > 0
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


#: 存储后端选择环境变量（task-sqlite-cutover-default，B-max 切默认）。
#: 取值 ``sqlite``（缺省，行为与 ROADMAP v1.4.6 P1 一致）/ ``file``（显式回滚
#: 路径，行为与切默认前完全一致）/ 非法非空值：结构化 E007（拼写错误不得静默回退）。
_STORAGE_BACKEND_ENV = "ORCHD_STORAGE_BACKEND"


def resolve_backend_kind() -> str:
    """归一化 ``ORCHD_STORAGE_BACKEND`` 取值（缺省 ``sqlite``）。

    doctor 两处分发与 :func:`_select_backend` 共用此单一源，避免缺省判定散落：
    未设 / 空 / ``sqlite`` → ``sqlite``；``file`` 与非法非空值原样返回由调用方分流。
    """
    return os.environ.get(_STORAGE_BACKEND_ENV, "sqlite").strip().lower() or "sqlite"


def _select_backend(orchd_dir: Path, *, readonly: bool = False) -> StorageBackend:
    """按 ``ORCHD_STORAGE_BACKEND`` 选择存储后端（缺省 SqliteBackend）。

    - 未设 / 空 / ``sqlite``：SqliteBackend；触发条件收敛到
      ``SqliteBackend.needs_migration``（jsonl 在场、无备份、库内物理表空，
      一次性 ``import_jsonl`` 迁移，幂等；成功后存量文件改名备份，失败分两种：
      导入失败 best-effort 保留 jsonl 可重试，备份改名失败抛 E007——不给
      "库里有数、文件里是旧数"的分裂部署开绿灯）。
    - ``file``：FilesystemBackend（显式回滚路径；与切默认前行为完全一致）。
    - 其他非空值：结构化 E007（拼写错误不得静默回退）。
    - ``readonly=True``（task-readonly-no-migrate）：跳过迁移分支——
      只读入口（doctor / check_repo）经此构造，绝不建库 / 导入 / 改名。
      其中库文件缺失时回退文件后端直读 jsonl（读零副作用，数据不少看）；
      库存在时以 ``mode=ro`` 打开已迁移库。
    """
    from orchd.ledger import resolve_store_dir
    kind = resolve_backend_kind()
    store_dir = resolve_store_dir(orchd_dir)
    if kind == "file":
        return FilesystemBackend(store_dir)
    if kind == "sqlite":
        from orchd.storage.sqlite import SqliteBackend

        if readonly and not (store_dir / "_ledger.sqlite3").is_file():
            return FilesystemBackend(store_dir)
        backend = SqliteBackend(store_dir, readonly=readonly)
        if readonly:
            return backend
        try:
            # task-migration-store-lock：迁移全程持 store 文件锁（双检）——
            # 两进程同时首开同一账本根时串行化，判空与导入不再交错双插。
            # 仅迁移待定（jsonl 在、无备份）时才取锁，常规构造零额外等待。
            if backend.needs_migration():
                backend.acquire_lock()
                try:
                    if backend.needs_migration():
                        backend.import_jsonl()
                finally:
                    backend.release_lock()
        except OrchdError:
            # 备份 E007 必须透出（fail-closed）：数据已入库，此时留陈旧文件即
            # "库里有数、文件里是旧数"的分裂部署；导入本身的失败仍 best-effort。
            # （import_jsonl 仅备份步骤抛 OrchdError，见其 docstring。）
            raise
        except Exception:
            pass  # best-effort：迁移失败保留 jsonl，后续命令可重试
        return backend
    raise OrchdError(
        ErrorCode.E007,
        f"storage_backend_unknown: ORCHD_STORAGE_BACKEND={kind!r} 非法",
        [{"env": _STORAGE_BACKEND_ENV, "value": kind,
          "allowed": ["file", "sqlite"],
          "hint": "取值仅 file / sqlite（缺省 sqlite）"}],
    )


def open_store(orchd_dir: Path | str) -> Store:
    """打开或创建 Store 实例。

    推荐使用场景：外部模块（如 CLI、spec、pool）应通过本函数获取 Store 实例，
    因为它会校验 ``orchd_dir`` 是否存在，未初始化时抛出 E013 错误提示用户执行
    ``orchd init``。直接调用 ``Store(orchd_dir)`` 构造函数则跳过此校验，
    适用于内部流程已确保目录存在的场景（如 ``orchd init`` 本身）。

    Args:
        orchd_dir: ``.orchd`` 目录路径，支持 ``str`` 和 ``Path``。

    Raises:
        OrchdError E013: orchd_dir 不存在（未初始化）。
    """
    from orchd.ledger import Store
    orchd_dir = Path(orchd_dir)
    if not orchd_dir.is_dir():
        raise OrchdError(
            ErrorCode.E013,
            f"not_initialized: {orchd_dir} does not exist",
            [{"path": str(orchd_dir), "message": ".orchd/ 目录缺失，请先执行 orchd init"}],
        )
    return Store(orchd_dir)
