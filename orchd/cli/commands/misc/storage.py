"""misc.storage —— 自 ``orchd/cli/commands/misc.py`` 拆出。

存储迁移与回滚（storage-recutover / storage-rollback / migrate / layout-migrate）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.cli._util import _find_orchd_dir
from orchd.errors import ErrorCode, OrchdError

# 本模块不读环境变量：hint 文案里的 ``ORCHD_STORAGE_BACKEND=file`` 是给用户的操作指引，
# 变量名真源见 ``orchd.constants.ORCHD_STORAGE_BACKEND_ENV``（Wave1-T3b）。

def _cmd_layout_migrate(args) -> dict:
    """flat → container 布局迁移（task-14-worktree-layout，AC4）。

    CLI 参数: args.path（flat 主工作树根，默认当前目录）。
    返回: 迁移结果字典（migrated / main_worktree / moved / marker /
    runtime_root / 可选 reason + hint）。
    """
    from orchd.worktree import layout_migrate

    return layout_migrate(Path(args.path))


def _cmd_storage_rollback(args) -> dict:
    """sqlite → file 回滚导出 + 复验（task-storage-rollback-cmd）。

    把库内逻辑事件原子导出回 ``_ledger.jsonl``，再经文件后端直读复验事件
    数与 event_id 集合；一致即报告可切 ``ORCHD_STORAGE_BACKEND=file``
    （命令不改进程外环境变量，只负责把回滚素材备齐并证明对得上）。
    非 sqlite 后端调用 → E007（回滚无对象）。

    CLI 参数：无。返回：exported / verified / match / hint。
    """
    from orchd.ledger import open_store, resolve_store_dir
    from orchd.storage.filesystem import FilesystemBackend
    from orchd.storage.sqlite import SqliteBackend

    orchd_dir = _find_orchd_dir()
    store = open_store(orchd_dir)
    if not isinstance(store.backend, SqliteBackend):
        raise OrchdError(
            ErrorCode.E007,
            "storage_rollback_requires_sqlite: 当前非 sqlite 后端，无需回滚",
            [{"hint": "仅 sqlite 缺省部署需要导出；file 后端账本即活名文件"}],
        )
    store.acquire_lock()
    try:
        exported = store.backend.export_jsonl()
        file_backend = FilesystemBackend(resolve_store_dir(orchd_dir))
        disk_ids = [e.get("event_id") for e in file_backend.read_events()]
        mem_ids = [e.get("event_id") for e in store.backend.read_events()]
    finally:
        store.release_lock()
    match = exported == len(disk_ids) and set(disk_ids) == set(mem_ids)
    return {
        "backend": "sqlite",
        "exported": exported,
        "verified": len(disk_ids),
        "match": match,
        "hint": ("一致时可设 ORCHD_STORAGE_BACKEND=file 切回文件后端"
                 if match else "导出与复验不一致，不得切换；检查账本完整性"),
    }


#: 并账消歧备份后缀（task-storage-recutover-merge-channel）。不复用
#: ``_MIGRATED_BACKUP_SUFFIX``：切库迁移已占用该目标名，复用会永远撞
#: ``_backup_migrated_files`` 的 fail-closed 预检。
_RECUTOVER_BACKUP_SUFFIX = ".recutover-bak"


def _recutover_backup_live_files(store_dir: Path) -> list[str]:
    """把活名账本文件改名备份，消解 doctor migration_consistency 的并存歧义。

    纪律与 ``SqliteBackend._backup_migrated_files`` 一致：改名**前**预检两个
    目标都不存在，任一被占用即 E007 fail-closed（不静默覆盖回滚素材）；改名
    经 ``orchd.ledger._atomic_replace`` 原语（不另造原子实现）。返回实际改名的
    文件名列表；调用方须已持 store 锁。
    """
    from orchd.ledger import _atomic_replace

    names = ("_ledger.jsonl", "_checkpoint.json")
    pending = [n for n in names if (store_dir / n).is_file()]
    for name in pending:
        target = store_dir / (name + _RECUTOVER_BACKUP_SUFFIX)
        if target.is_file():
            raise OrchdError(
                ErrorCode.E007,
                f"storage_recutover_backup_occupied: {target} 已存在",
                [{"path": str(target),
                  "hint": "并账数据已入库；备份目标被占用不覆盖，"
                          "人工核对后清理或改名再重试"}],
            )
    for name in pending:
        src = store_dir / name
        target = src.with_name(name + _RECUTOVER_BACKUP_SUFFIX)
        try:
            _atomic_replace(src, target)
        except (OrchdError, OSError) as exc:
            raise OrchdError(
                ErrorCode.E007,
                f"storage_recutover_backup_failed: {src} 改名备份失败",
                [{"path": str(src), "target": str(target),
                  "error": f"{type(exc).__name__}: {exc}",
                  "hint": "并账数据已入库；手动改名该文件后重试"
                          "（本次不再改动账本）"}],
            ) from exc
    return pending


def _cmd_storage_recutover(args) -> dict:
    """回滚残留并账：活名 file 增量按 event_id 差集并入 sqlite（task-storage-recutover-merge-channel）。

    触发形态：切库 → ``storage-rollback`` 导出 → 以
    ``ORCHD_STORAGE_BACKEND=file`` 追加事件 → 切回缺省 sqlite。此时库内停在
    切库时刻，活名 ``_ledger.jsonl`` 多出的新事件对引擎隐身
    （``needs_migration`` 把「活名 + 库内已有数」判为并存歧义态而返回假），
    唯一拦截是 doctor ``migration_consistency`` fail——此前其 hint 只教改名
    或删除、无并账通道。本命令补该通道：

    1. 读活名 jsonl（``FilesystemBackend``）与库内逻辑事件，取 event_id 差集；
    2. 库内活跃段（``read_events(from_line=归档段 + 1)``）与差集拼接，经
       ``replace_active_events`` 单事务原子替换活跃事件表（计数按「归档数 +
       活跃数」口径同步，归档段不重复写进活跃表）；
    3. 复验：库内 event_id 集合 == 原库 ∪ 活名、且条数一致；
    4. **仅当复验一致（match=True）**时把活名 ``_ledger.jsonl`` /
       ``_checkpoint.json`` 改名备份为 ``.recutover-bak``——消歧后 doctor
       ``migration_consistency`` 随之转 ok（回滚素材仍在备份文件里）；
       match=False 时**保留**活名文件不动，避免 doctor 因「活名文件不在场」
       把一次失败并账洗成绿灯。后缀不复用 ``_MIGRATED_BACKUP_SUFFIX``：该
       目标名已被切库迁移占用，复用必撞 ``_backup_migrated_files`` 的
       fail-closed 预检（实测确认）；预检纪律与之一致（目标占用即 E007、
       改名失败包 E007 并带「并账数据已入库」hint）。

    ``--dry-run`` 只算差集与计划，不写库、不改名。差集为空即幂等返回
    ``performed=False``（``no_missing_events``）。非 sqlite 后端 → E007
    （与 ``storage-rollback`` 对称：file 后端下活名文件即账本本体）。

    CLI 参数：args.dry_run（只算计划）。返回：backend / performed / dry_run /
    reason / live_events / db_events / missing_events / missing_event_ids /
    merged_events / verified / match / backed_up / hint。语义边界：
    ``performed`` = **检测到差集并尝试并账**（不代表生效——``match`` 才是一致性
    判据，mismatch 时 ``performed=True`` 但库未收敛）；``backed_up`` = 实际改名的
    活名文件（仅 ``match=True`` 时可能非空）；``merged_events`` = 计划并账后的
    活跃段条数（mismatch 时与实际的 ``verified`` 分叉属预期）；``verified`` =
    并账后库内**逻辑**事件总数（含归档段）。
    """
    from orchd.ledger import open_store, resolve_store_dir
    from orchd.storage import read_archive_events
    from orchd.storage.filesystem import FilesystemBackend
    from orchd.storage.sqlite import SqliteBackend

    orchd_dir = _find_orchd_dir()
    store = open_store(orchd_dir)
    backend = store.backend
    if not isinstance(backend, SqliteBackend):
        raise OrchdError(
            ErrorCode.E007,
            "storage_recutover_requires_sqlite: 当前非 sqlite 后端，"
            "活名文件即账本本体，无需并账",
            [{"hint": "并账对象是「缺省 sqlite 下被隐身的 file 增量」；"
                      "file 后端直接用文件账本或 storage-rollback"}],
        )
    dry_run = bool(getattr(args, "dry_run", False))
    store_dir = resolve_store_dir(orchd_dir)
    store.acquire_lock()
    try:
        live_events = list(FilesystemBackend(store_dir).read_events())
        db_events = list(backend.read_events())
        db_ids = {e.get("event_id") for e in db_events if e.get("event_id")}
        missing = [e for e in live_events
                   if e.get("event_id") and e["event_id"] not in db_ids]
        live_ids = {e.get("event_id") for e in live_events if e.get("event_id")}
        arch, _ = read_archive_events(store_dir)
        active = list(backend.read_events(from_line=len(arch) + 1))
        result: dict[str, Any] = {
            "backend": "sqlite",
            "dry_run": dry_run,
            "live_events": len(live_events),
            "db_events": len(db_events),
            "missing_events": len(missing),
            "missing_event_ids": [e["event_id"] for e in missing],
            "merged_events": len(active) + len(missing),
        }
        if dry_run:
            result.update({
                "performed": False,
                "reason": "dry_run",
                "verified": None,
                "match": None,
                "backed_up": [],
                "hint": (f"待并入 {len(missing)} 条活名增量并备份活名文件；"
                         "去掉 --dry-run 执行"),
            })
            return result
        if missing:
            backend.replace_active_events(active + missing)
        after = list(backend.read_events())
        verified_ids = {e.get("event_id") for e in after if e.get("event_id")}
        expected_ids = db_ids | live_ids
        # 第二子句按「有 event_id 的事件数」口径：len(after) 计全部事件，若库内
        # 存在无 event_id 事件（import_jsonl 逐行原样入库、不校验契约）或重复 id，
        # 拿 len(after) 比 len(expected_ids) 会让 match 恒假、命令永不收敛。
        match = (verified_ids == expected_ids
                 and len(verified_ids) == len(expected_ids))
        backed_up: list[str] = []
        if not match:
            # 复验不一致：**不得**改名活名文件——doctor 的 migration_consistency
            # 以「活名文件在场」为触发条件（orchd/doctor.py），改名会把一次失败
            # 并账洗成绿灯，状态面说谎。
            hint = ("并账后复验不一致（match=False）：活名文件保持原样、不得切库，"
                    "检查账本完整性后重跑")
        else:
            backed_up = _recutover_backup_live_files(store_dir)
            hint = ("并账完成且活名文件已改名备份，"
                    "doctor migration_consistency 应转 ok"
                    if missing else "库内已含活名全部事件，活名文件已改名备份")
        result.update({
            "performed": bool(missing),
            "reason": None if missing else "no_missing_events",
            "verified": len(after),
            "match": match,
            "backed_up": backed_up,
            "hint": hint,
        })
        return result
    finally:
        store.release_lock()


def _cmd_migrate(args) -> dict:
    """schema 版本前移（W1-③，停服迁移通道）。

    CLI 参数: args.path（master 文件，默认 canonical 主工作树
    .orchd/_master.json）；args.dry_run（只算计划，不写文件）；
    args.retreat（回退到最早版本，回滚通道）。
    返回: 迁移摘要（from_version / to_version / changed / dry_run）。
    改 schema_version 并**恒建 definition_archive 空骨架**（v3 为破坏性变更：
    顶层 required 收紧，master.tasks 语义收窄为「活跃任务定义」）；账本零触碰，
    归档内容搬迁属 master-archive 通道；回滚 = retreat 回 1（非空骨架时 fail-closed 拒绝）。
    """
    from orchd.migrate import migrate_master_file
    from orchd.worktree import resolve_master_path_from_dir

    orchd_dir = _find_orchd_dir()
    default = resolve_master_path_from_dir(orchd_dir)
    target = Path(getattr(args, "path", None) or default)
    return migrate_master_file(
        target,
        dry_run=bool(getattr(args, "dry_run", False)),
        retreat=bool(getattr(args, "retreat", False)),
    )
