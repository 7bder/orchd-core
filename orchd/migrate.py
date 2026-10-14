"""Schema 迁移通道（W1-③，停服核心）。

版本前移：``schema_version`` 1 → 2 → 3。v2 与 v1 同构（目录即版本标记）；
**v3 是破坏性变更**——顶层 ``required`` 增 ``definition_archive``，master 的
``tasks`` 语义由「全部任务定义」收窄为「活跃任务定义」（终态定义移入归档，
见 ``orchd/definition_archive.py``）。故 1/2 → 3 的迁移**恒设** ``definition_archive``
空骨架 ``{}``：该键是「定义归档在场」的唯一机读锚，允许缺席会让回读正确性
静默依赖隐式约定。反向（target < 3）移除该键；若它不是空骨架则 fail-closed
拒绝——回退不能静默丢弃归档指针。

幂等可重放；未知版本拒绝；账本零触碰（本通道只动 master 文件本身，归档内容
的搬迁属 :mod:`orchd.definition_archive` 的写入通道，回滚 = retreat 回 1）。

本模块仅依赖标准库（刻意不 import orchd 子模块，避免新增依赖边；
版本号与 spec.py 门禁字面量一致，由契约测试双边锁定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LATEST_SCHEMA_VERSION = 3
EARLIEST_SCHEMA_VERSION = 1

#: v3 起 master 顶层必须携带的归档清单键（与 definition_archive.ARCHIVE_KEY 同源；
#: 本模块刻意不 import 该模块以保持「仅标准库」约束，一致性由契约测试锁定）。
DEFINITION_ARCHIVE_KEY = "definition_archive"

#: 归档清单的最小「在场且非空」形态判据（``path`` 在场或有条目）。
_SCHEMA_V3 = 3


class SchemaVersionError(Exception):
    """未知 schema_version（版本闸门拒绝）。"""


def is_empty_archive_skeleton(value: Any) -> bool:
    """``definition_archive`` 是否为「空骨架」（缺席 / ``{}``）。

    判据与 :func:`orchd.definition_archive.resolve_archive_path` 的「无归档」口径
    **同源**（两者都只把缺席与空对象算空；本模块不 import 该模块以保持「仅标准库」
    约束，一致性由契约测试锁定）。``{"count": 0}`` / ``{"path": ""}`` 这类 schema
    合法但缺可用 ``path`` 的形态一律视为**非空指针**：回退 fail-closed 拒绝，否则
    同一 master 会在 ``migrate --retreat``（静默删键）与 ``master-archive``
    （抛 ``ArchiveMalformedError``）下语义分裂。

    空骨架表示「尚无归档内容」，可安全回退（删除该键）；非空（或形态可疑）表示
    归档指针可能在场，删除即静默丢失历史定义 → 调用方须 fail-closed 拒绝。
    """
    return value is None or value == {}


def migrate_master_data(
    data: dict[str, Any], target: int = LATEST_SCHEMA_VERSION
) -> tuple[dict[str, Any], bool]:
    """迁移单个 master 字典到目标版本。幂等，可 retreat。

    Args:
        data: master 字典（不被修改）。
        target: 目标版本，须在支持区间内（默认最新）。

    Returns:
        (迁移后字典, 是否发生变更)。输入字典不被修改。

    Raises:
        SchemaVersionError: 未知版本；或 target < 3 而 ``definition_archive``
            为非空骨架（回退会静默丢弃归档指针，fail-closed）。
    """
    version = data.get("schema_version", 1)
    for value in (version, target):
        if not isinstance(value, int) or not (
            EARLIEST_SCHEMA_VERSION <= value <= LATEST_SCHEMA_VERSION
        ):
            raise SchemaVersionError(
                f"unsupported schema_version: {value!r} "
                f"(supported {EARLIEST_SCHEMA_VERSION}"
                f"..{LATEST_SCHEMA_VERSION})"
            )
    if version == target:
        return data, False
    migrated = dict(data)
    migrated["schema_version"] = target
    if target >= _SCHEMA_V3:
        # 恒设空骨架；已有值（含非空清单）一律不覆盖——迁移不搬运数据。
        migrated.setdefault(DEFINITION_ARCHIVE_KEY, {})
    elif DEFINITION_ARCHIVE_KEY in migrated:
        if not is_empty_archive_skeleton(migrated[DEFINITION_ARCHIVE_KEY]):
            raise SchemaVersionError(
                "definition_archive 非空，拒绝回退到 v2 及以下："
                "会静默丢弃归档指针（先清空归档或保留 v3）"
            )
        del migrated[DEFINITION_ARCHIVE_KEY]
    return migrated, True


def migrate_master_file(
    path: Path | str, dry_run: bool = False, retreat: bool = False
) -> dict[str, Any]:
    """迁移磁盘上的 _master.json 文件。返回执行摘要。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    target = EARLIEST_SCHEMA_VERSION if retreat else LATEST_SCHEMA_VERSION
    migrated, changed = migrate_master_data(data, target=target)
    if changed and not dry_run:
        from orchd.ledger import _write_text_atomic

        _write_text_atomic(
            path,
            json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
        )
    return {
        "path": str(path),
        "from_version": data.get("schema_version", 1),
        "to_version": migrated.get("schema_version", 1),
        "changed": changed,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# lessons 运行时迁移（task-lessons-runtime-migrate-observe）
# ---------------------------------------------------------------------------
# 背景：container 布局下账本根经 resolve_store_dir 重定向到共享运行时根，
# lessons 读写早已落到运行时根；flat 时代写在 <main>/.orchd 下的两文件遂成
# 旧布局孤儿——开关名义开启、lookup 恒为 0。本函数把旧布局两文件幂等迁入
# 运行时根：目标缺失则原样迁入；目标已存在则只追加缺失行（不覆盖存量、保留
# 源行序）；成功后删除源文件（遗留即消除，可重放）。
# 真实孤儿实例（main/.orchd 42 行测试夹具）已由用户裁决删除（存档与 sha256
# 见任务 notes），此处仅通用通道，不含数据清理。

# 迁移视图表（lessons 两文件名）。canonical 运行时清单是
# doctor._RUNTIME_STATE_FILES（nogit 侧别名引用，单一真源）；此处二元组仅为
# 本通道的文件视图，不另立运行时清单（test_nogit_changedet 负控制只扫
# 7 成员枚举字面量，二元组不在其列）。
_LESSON_RUNTIME_FILES = ("lessons.jsonl", "lessons.staged.jsonl")


def _lessons_same_path(src: Path, dst: Path) -> bool:
    """源与目标是否为同一文件（flat 无重定向时恒真，调用方提前短路）。"""
    try:
        return src.resolve() == dst.resolve()
    except OSError:
        return src == dst


def _migrate_lessons_files(
    orchd_dir: Path, store_dir: Path, dry_run: bool
) -> tuple[list[dict[str, Any]], bool]:
    """迁移两文件的无锁内核（调用方须已持 lessons 锁，见下）。"""
    files: list[dict[str, Any]] = []
    changed = False
    for name in _LESSON_RUNTIME_FILES:
        src = orchd_dir / name
        dst = store_dir / name
        if _lessons_same_path(src, dst):
            files.append({"file": name, "status": "same_path"})
            continue
        if not src.is_file():
            files.append({"file": name, "status": "absent"})
            continue
        try:
            raw = src.read_text(encoding="utf-8")
        except OSError as exc:
            files.append({
                "file": name,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        # 逐行原样搬运（含损坏行）：行序即源序，reader 侧跳过语义不变。
        src_lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not dst.is_file():
            if src_lines and not dry_run:
                store_dir.mkdir(parents=True, exist_ok=True)
                from orchd.ledger import _write_bytes_atomic

                _write_bytes_atomic(dst, src.read_bytes())
            if not dry_run:
                try:
                    src.unlink(missing_ok=True)
                except OSError as exc:
                    files.append({
                        "file": name,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
            files.append({
                "file": name,
                "status": "migrated" if src_lines else "empty_src_removed",
                "moved": len(src_lines),
                "skipped_existing": 0,
            })
            changed = True
            continue
        try:
            dst_text = dst.read_text(encoding="utf-8")
        except OSError as exc:
            files.append({
                "file": name,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        seen = {
            ln.strip() for ln in dst_text.splitlines() if ln.strip()
        }
        fresh = [ln for ln in src_lines if ln.strip() not in seen]
        if fresh and not dry_run:
            prefix = "" if (not dst_text or dst_text.endswith("\n")) else "\n"
            with open(dst, "a", encoding="utf-8") as f:
                f.write(prefix + "\n".join(fresh) + "\n")
        if not dry_run:
            try:
                src.unlink(missing_ok=True)
            except OSError as exc:
                files.append({
                    "file": name,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
        files.append({
            "file": name,
            "status": "merged" if fresh else "already_present",
            "moved": len(fresh),
            "skipped_existing": len(src_lines) - len(fresh),
        })
        changed = True
    return files, changed


def migrate_lessons_to_runtime(
    orchd_dir: Path | str, dry_run: bool = False
) -> dict[str, Any]:
    """把旧布局经验库迁入运行时根（幂等、目标已存在不覆盖、保留原行序）。

    只在账本根重定向生效时扫描（container / ORCHD_HOME；flat 下源即运行
    时，返回 scanned=False 零操作）。持 lessons 锁执行（与 stage/review
    同锁，消除并发窗口；拿不到锁即 reason=lock_busy 跳过，绝不阻塞）。

    Args:
        orchd_dir: 主工作树 .orchd 目录（旧布局源）。
        dry_run: 只算计划，不写目标、不删源。

    Returns:
        JSON 可序列化摘要：{orchd_dir/store_dir/scanned/changed/dry_run/
        files:[{file/status/moved/skipped_existing}]}；未扫描时附 reason。
    """
    # 顶层保持纯标准库（模块契约）：orchd 依赖一律函数内惰性导入（lessons.py
    # _resolve_runtime 同模式——复用 resolve_store_dir 唯一事实源，不另立
    # 账本根解析）。
    from orchd.ledger import resolve_store_dir

    orchd_path = Path(orchd_dir)
    try:
        store_path = Path(resolve_store_dir(orchd_path))
    except Exception as exc:
        return {
            "orchd_dir": str(orchd_path),
            "store_dir": None,
            "scanned": False,
            "changed": False,
            "dry_run": dry_run,
            "reason": f"store_dir_unresolvable: {type(exc).__name__}: {exc}",
        }
    try:
        redirected = store_path.resolve() != orchd_path.resolve()
    except OSError:
        redirected = store_path != orchd_path
    if not redirected:
        return {
            "orchd_dir": str(orchd_path),
            "store_dir": str(store_path),
            "scanned": False,
            "changed": False,
            "dry_run": dry_run,
            "reason": "flat_no_redirect",
        }
    try:
        from orchd.errors import OrchdError
        from orchd.lessons import LOCK_TIMEOUT_S
        from orchd.lockfile import ExclusiveFileLock
    except ImportError as exc:
        return {
            "orchd_dir": str(orchd_path),
            "store_dir": str(store_path),
            "scanned": False,
            "changed": False,
            "dry_run": dry_run,
            "reason": f"lock_import_failed: {exc}",
        }
    lock = ExclusiveFileLock(store_path / "lessons.lock")
    try:
        lock.acquire(blocking=False, timeout_s=LOCK_TIMEOUT_S)
    except OrchdError:
        return {
            "orchd_dir": str(orchd_path),
            "store_dir": str(store_path),
            "scanned": True,
            "changed": False,
            "dry_run": dry_run,
            "reason": "lock_busy",
        }
    try:
        files, changed = _migrate_lessons_files(
            orchd_path, store_path, dry_run
        )
    finally:
        lock.release()
    return {
        "orchd_dir": str(orchd_path),
        "store_dir": str(store_path),
        "scanned": True,
        "changed": changed,
        "dry_run": dry_run,
        "files": files,
    }
