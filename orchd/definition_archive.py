"""终态任务定义归档（「定义归档 + 回读契约」的写入侧，task-master-archive-write-channel）。

背景（灵感 ``master-terminal-definition-archive``）：``_master.json`` 的**任务定义**
永不缩减——账本侧已有 ``orchd ledger-compact`` 把终态任务事件搬进共享归档，定义侧
却没有出口；实测 2026-10-14（HEAD ``3ac9787c``）该文件 2,295,834 B / 798 任务且随
任务数单调增长。更要紧的是**门禁信号的性质**：``orchd validate`` 的绿灯建立在
「终态豁免」中和 468 条告警之上（``exempted_terminal_warnings=468``），而豁免依赖
**运行时账本**（``orchd/spec.py`` 由 ``Store.replay()`` 求终态集合）——CI 新克隆无
账本即整片误报（实测 E037×16）。本模块给定义侧一个真正的归档出口。

卡 1 只做**写入侧**；卡 2（``task-master-archive-readback-merge``）在本模块追加
两个回读原语——:func:`read_archive_ids`（供 ``depends_on`` 引用解析）与
:func:`merge_archived_definitions`（供 ``orchd/cli/_util.py::_load_tasks`` 的
回读合并）；终态集合真源脱离 ``Store.replay()`` 仍归卡 3。

契约（三条，均为 fail-closed）：
- **清单是唯一机读锚**：master 顶层 ``definition_archive`` 键承载
  ``{path, count, sha256}``（schema v3 起为 required）；归档位置**只**由它的
  ``path`` 解析。本模块刻意不含任何归档文件名字面量——首次建立清单时由调用方
  传入 relpath，此后一律以清单为准（单一真源，禁第二处默认值）。
- **在场即须可信**：清单在场则归档文件必须存在、可解析、条目 id 唯一、
  ``count`` 与 ``sha256`` 与内容逐项相符；任一不符抛具名异常，**绝不静默返回
  空表**（静默即「已有定义凭空消失」，正是本通道要消灭的失败模式）。
- **不伪造、不改写**：任务定义逐字搬运，不补默认值、不重排字段、不改写内容；
  重复执行按 id 幂等（既有条目逐字保留）。

依赖方向：顶层仅标准库；``orchd`` 侧依赖一律**函数内惰性导入**（``orchd.ledger``
的 ``_write_text_atomic`` / ``Store`` / ``ledger_status_available`` /
``resolve_store_dir`` 与 ``orchd.errors`` 的 ``ErrorCode`` / ``OrchdError``），
不 import 其他引擎业务子模块（与 ``orchd/migrate.py`` 同约束）。``Store`` 只以
``readonly=True`` 用于重建终态集合，本模块不写账本。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

#: 归档文件自身的 schema 版本（与 master 的 ``schema_version`` 同代际）。
ARCHIVE_SCHEMA_VERSION = 3

#: master 顶层承载归档清单的键名（schema v3 的 required 成员）。
ARCHIVE_KEY = "definition_archive"

#: 摘要算法与十六进制长度（清单 ``sha256`` 的形态约束单一来源）。
_DIGEST_ALGO = "sha256"
_DIGEST_HEX_LEN = 64


class DefinitionArchiveError(Exception):
    """终态定义归档通道的基类异常（全部 fail-closed）。"""


class ArchiveMissingError(DefinitionArchiveError):
    """清单声明归档在场，但归档文件缺失。"""


class ArchiveMalformedError(DefinitionArchiveError):
    """清单或归档文件形态非法（非 JSON / 类型不符 / 清单字段缺失）。"""


class ArchiveDigestMismatchError(DefinitionArchiveError):
    """归档内容与清单 ``sha256`` 不符（内容被改写或清单过期）。"""


class ArchiveDuplicateIdError(DefinitionArchiveError):
    """归档内出现重复任务 id（幂等合并的前提被破坏）。"""


class ArchiveOrphanedError(DefinitionArchiveError):
    """清单为空骨架但归档文件在场且非空（无锚孤儿）——拒绝覆盖。

    该态下磁盘归档无法与任何清单核对（``count``/``sha256``/id 唯一性都无基准），
    若按「无归档」处理则 :func:`write_archive` 会以本次终态定义覆盖它，历史定义
    **静默消失**。故 fail-closed 拒绝：要么恢复清单，要么确认后手工删除该文件。
    """


def archive_digest(tasks: list[dict[str, Any]]) -> str:
    """归档任务列表的内容摘要（规范化 JSON 的 sha256）。

    规范化口径是本模块的单一来源：``sort_keys=True`` + 紧凑分隔符 +
    ``ensure_ascii=False`` + UTF-8 编码。对「键序不同但语义相同」的同一批
    定义结果稳定，对任何内容改动必变。

    **列表顺序参与摘要**（``sort_keys`` 只规整 dict 键序，不动列表序）：调用方
    须保证归档条目顺序稳定——本模块的写入路径由 :func:`merge_definitions`
    「既有前缀序 + 本次追加序」与 master.tasks 的保序读取共同保证。

    Args:
        tasks: 任务定义列表。

    Returns:
        64 位小写十六进制摘要字符串。
    """
    canonical = json.dumps(
        tasks, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_manifest(archive_relpath: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """构造归档清单（``{path, count, sha256}``，schema v3 的 required 形态）。"""
    return {
        "path": archive_relpath,
        "count": len(tasks),
        "sha256": archive_digest(tasks),
    }


def split_terminal_definitions(
    tasks: list[dict[str, Any]], terminal_ids: set[str] | frozenset[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按终态 id 集合把任务定义划分为 (活跃, 终态)，两侧均保持原相对顺序。

    划分是完备且互斥的：活跃 ∪ 终态 == 输入，交集为空。id 不在 ``terminal_ids``
    中（含无 id 的畸形条目）一律归入活跃侧——状态未知时**留在 master** 是保守
    方向（留在 master 只是体积不降，误摘则会丢定义）。

    Args:
        tasks: master.tasks（保序）。
        terminal_ids: 终态（completed/cancelled）任务 id 集合。

    Returns:
        ``(active, terminal)``。
    """
    active: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    known = frozenset(terminal_ids or ())
    for task in tasks:
        tid = task.get("id") if isinstance(task, dict) else None
        if isinstance(tid, str) and tid in known:
            terminal.append(task)
        else:
            active.append(task)
    return active, terminal


def merge_definitions(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 id 幂等合并：``existing`` 逐字保留，``incoming`` 只补缺失 id。

    Args:
        existing: 归档既有条目（顺序即最终顺序前缀）。
        incoming: 本次待并入的条目（保序追加）。

    Returns:
        ``(merged, added)``；``added`` 为空即本次无新增（幂等重跑）。
    """
    seen: set[Any] = set()
    merged: list[dict[str, Any]] = []
    for task in existing:
        seen.add(task.get("id"))
        merged.append(task)
    added: list[dict[str, Any]] = []
    for task in incoming:
        tid = task.get("id")
        if tid in seen:
            continue
        seen.add(tid)
        merged.append(task)
        added.append(task)
    return merged, added


def merge_archived_definitions(
    active: list[dict[str, Any]], archived: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """回读合并：活跃定义（保序在前）+ 归档条目（按归档序追加）。

    **去重口径（确定性，无隐式行为）**：以 ``active`` 为准——归档侧与活跃侧
    同 id 的条目被忽略（活跃定义是权威副本；同一 id 在合并视图中只出现一次）。
    该分支在**生产流程不可达**：归档条目恒来自「曾被写侧摘除的终态定义」，
    活跃侧不含同 id；此处保留是防御手工编排/外部篡改的确定性收敛。

    Args:
        active: ``master.tasks``（活跃任务定义，保序）。
        archived: 归档条目（归档序）。

    Returns:
        合并后的定义列表；``archived`` 为空时与 ``active`` 逐字相同。
    """
    merged: list[dict[str, Any]] = list(active)
    seen: set[Any] = {
        t.get("id") for t in merged if isinstance(t, dict) and t.get("id") is not None
    }
    for task in archived:
        tid = task.get("id") if isinstance(task, dict) else None
        if tid is not None and tid in seen:
            continue
        if tid is not None:
            seen.add(tid)
        merged.append(task)
    return merged


def read_archive_ids(
    master: dict[str, Any], master_path: Path | str
) -> frozenset[str]:
    """归档中已登记的任务 id 集合（清单缺席/空骨架 → 空集）。

    供引用解析（``depends_on`` 存在性）作真源扩展用：归档 id 与 ``master.tasks``
    的 id 并集即「可解析的引用目标全集」。

    清单在场但不可读（缺文件 / 不可解析 / ``sha256`` 或 ``count`` 不符 / 条目 id
    重复）→ 传播具名异常（fail-closed）；调用方决定呈现方式——gate 侧转 E003、
    只读入口转 E007，**都不得静默当作「无归档」**。

    Args:
        master: **已加载**的 master 字典（复用调用方内存态，避免二次读盘——
            也消除「内存 raw 与磁盘不一致」造成的语义缝隙）。
        master_path: master 文件路径（归档位置由清单 ``path`` 相对项目根解析）。

    Returns:
        归档任务 id 集合（``manifest`` 缺席或空骨架时为空集）。
    """
    tasks, manifest = read_archive_with_manifest(master, master_path)
    if manifest is None:
        return frozenset()
    return frozenset(
        t["id"] for t in tasks
        if isinstance(t, dict) and isinstance(t.get("id"), str)
    )


def _orphan_archive_task_count(path: Path) -> int:
    """孤儿归档（清单为空骨架但文件在场）的条目数。

    不可解析 / 形态非法一律抛 :class:`ArchiveMalformedError`——**无法证明其为空
    即拒绝**，不静默当作空归档（那会让覆盖成为静默数据丢失）。

    Args:
        path: 归档文件路径。

    Returns:
        归档内 ``tasks`` 条目数。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveMalformedError(
            f"清单为空骨架但归档文件在场且不可解析，拒绝覆盖: {path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list):
        raise ArchiveMalformedError(
            f"清单为空骨架但归档文件形态非法（缺 tasks 列表），拒绝覆盖: {path}"
        )
    return len(tasks)


def _validate_unique_ids(tasks: list[dict[str, Any]]) -> None:
    """归档条目 id 必须唯一且为非空字符串（幂等合并的前提）。"""
    seen: set[Any] = set()
    for task in tasks:
        tid = task.get("id") if isinstance(task, dict) else None
        if not isinstance(tid, str) or not tid:
            raise ArchiveMalformedError(f"归档条目缺合法 id: {task!r}")
        if tid in seen:
            raise ArchiveDuplicateIdError(f"归档条目 id 重复: {tid!r}")
        seen.add(tid)


def _project_root_for(master_path: Path) -> Path:
    """master 文件所属的项目根（清单 ``path`` 的相对基准）。

    ``<root>/.orchd/_master.json`` → ``<root>``；其余位置（测试夹具、自定义路径）
    以 master 所在目录为根。判据只看目录名，不做启发式向上搜索。
    """
    parent = master_path.parent
    return parent.parent if parent.name == ".orchd" else parent


def resolve_archive_path(
    master_path: Path | str, manifest: Any
) -> Path | None:
    """由清单解析归档文件绝对路径；清单缺席/空骨架时返回 ``None``。

    这是归档位置的**唯一**解析入口：除清单 ``path`` 外不读任何默认值、不做
    文件名猜测。清单在场但形态非法（非对象 / path 非字符串）抛
    :class:`ArchiveMalformedError`。

    Args:
        master_path: master 文件路径（其目录名决定相对基准，见
            :func:`_project_root_for`）。
        manifest: master 的 ``definition_archive`` 值。

    Returns:
        归档文件绝对路径；清单缺席或为空骨架（``{}``/``None``）时返回 ``None``。
    """
    if manifest is None or manifest == {}:
        return None
    if not isinstance(manifest, dict):
        raise ArchiveMalformedError(
            f"{ARCHIVE_KEY} 必须是对象，实为 {type(manifest).__name__}"
        )
    relpath = manifest.get("path")
    if not isinstance(relpath, str) or not relpath.strip():
        raise ArchiveMalformedError(f"{ARCHIVE_KEY}.path 缺失或非字符串: {relpath!r}")
    root = _project_root_for(Path(master_path))
    return (root / relpath).resolve()


def _load_master_dict(master_path: Path) -> dict[str, Any]:
    """读取 master 字典（缺失/不可解析均抛具名异常）。"""
    if not master_path.is_file():
        raise ArchiveMissingError(f"master 文件缺失: {master_path}")
    try:
        data = json.loads(master_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveMalformedError(
            f"master 不可解析: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ArchiveMalformedError("master 顶层必须是对象")
    return data


def read_archive(
    master_path: Path | str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """读取归档条目（经 master 的清单解析，fail-closed）。

    清单缺席或为空骨架 ⇒ ``([], None)``（尚无归档，非错误）。清单在场则逐项校验：
    文件存在 → JSON 可解析 → 顶层对象含 ``tasks`` 列表 → 条目 id 唯一非空 →
    ``count`` 与 ``sha256`` 与内容相符。任一不符抛具名异常。

    Args:
        master_path: master 文件路径。

    Returns:
        ``(tasks, manifest)``；无归档时 ``([], None)``。
    """
    master = _load_master_dict(Path(master_path))
    return read_archive_with_manifest(master, master_path)


def read_archive_with_manifest(
    master: dict[str, Any], master_path: Path | str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """:func:`read_archive` 的内核：调用方已持有 master 字典时避免重复读盘。"""
    manifest = master.get(ARCHIVE_KEY)
    path = resolve_archive_path(master_path, manifest)
    if path is None:
        return [], None
    if not path.is_file():
        raise ArchiveMissingError(
            f"{ARCHIVE_KEY} 声明归档在场但文件缺失: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArchiveMalformedError(
            f"归档文件不可解析: {path}: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ArchiveMalformedError(f"归档顶层必须是对象: {path}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ArchiveMalformedError(f"归档缺 tasks 列表: {path}")
    _validate_unique_ids(tasks)
    assert isinstance(manifest, dict)  # resolve_archive_path 已保证
    declared_count = manifest.get("count")
    if declared_count != len(tasks):
        raise ArchiveMalformedError(
            f"清单 count={declared_count!r} 与归档条目数 {len(tasks)} 不符: {path}"
        )
    declared_digest = manifest.get("sha256")
    actual_digest = archive_digest(tasks)
    if declared_digest != actual_digest:
        raise ArchiveDigestMismatchError(
            f"清单 sha256={declared_digest!r} 与归档内容摘要 "
            f"{actual_digest} 不符: {path}"
        )
    return tasks, manifest


def write_archive(path: Path | str, tasks: list[dict[str, Any]]) -> str:
    """原子写入归档文件，返回内容摘要。

    写前校验条目 id 唯一非空；写入走 ``ledger._write_text_atomic``（tmp + fsync +
    原子替换 + 目录 fsync）——不会留下半截文件。

    Args:
        path: 归档文件绝对路径（由调用方经清单解析后传入）。
        tasks: 归档条目（逐字写入，不改写内容）。

    Returns:
        写入内容的 :func:`archive_digest`。
    """
    from orchd.ledger import _write_text_atomic

    _validate_unique_ids(tasks)
    payload = {"schema_version": ARCHIVE_SCHEMA_VERSION, "tasks": tasks}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(
        target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )
    return archive_digest(tasks)


def run_master_archive(
    master_path: Path | str,
    *,
    archive_relpath: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """写入通道：把终态任务定义从 master 摘入归档（幂等、原子、自复验）。

    步骤（任一前置不满足即 fail-closed 拒绝，绝不部分执行）：
    ① master 须为 schema v3 及以上（``definition_archive`` 是 v3 的 required 成员；
       低版本请先跑 ``orchd migrate``——迁移与数据搬迁职责分离，见卡面决断）；
    ② 账本**状态可重建**才执行（复用 ``ledger.ledger_status_available`` 单一真源：
       空载体 / 缺载体一律拒绝——把「不可用」静默当 0 条会让终态定义**反向**全部
       留在 master 且清单写成空，是本通道最危险的失败模式）；
    ③ ``Store.replay()`` 求终态 id 集合 → ``split_terminal_definitions`` 摘出；
    ④ 按 id 幂等并入归档，写归档 → 重写 master（tasks 只留活跃 + 新清单）；
    ⑤ 自复验：重读两文件、重算摘要、断言 master 中不再含任何终态 id；失败即回滚
       归档到执行前内容后再抛错（两文件各为原子替换，回滚亦为原子替换，故不存在
       「归档已并、master 未摘」或反之的半写状态）。

    Args:
        master_path: master 文件路径。
        archive_relpath: 首次建立清单时使用的归档相对路径（项目根为基准）；
            清单已存在时以其 ``path`` 为准，本参数被忽略（单一真源）。
        dry_run: 只算计划，零落盘。

    Returns:
        执行摘要（JSON 可序列化）。

    Raises:
        orchd.errors.OrchdError: 前置或复验失败（E007/E013 等），带修复指引。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import (
        Store,
        _write_text_atomic,
        ledger_status_available,
        resolve_store_dir,
    )

    target = Path(master_path)
    master = _load_master_dict(target)
    version = master.get("schema_version", 1)
    if not isinstance(version, int) or version < ARCHIVE_SCHEMA_VERSION:
        raise OrchdError(
            ErrorCode.E007,
            f"master_archive_requires_v{ARCHIVE_SCHEMA_VERSION}: "
            f"当前 schema_version={version!r}",
            [{"hint": f"先执行 orchd migrate（--path {target}）前移到 v"
                      f"{ARCHIVE_SCHEMA_VERSION} 再归档"}],
        )

    orchd_dir = target.parent
    store_dir = resolve_store_dir(orchd_dir)
    if not ledger_status_available(store_dir):
        raise OrchdError(
            ErrorCode.E007,
            f"ledger_unavailable: 终态集合不可重建（载体缺失或为空）：{store_dir}",
            [{"hint": "空载体判成 0 条会让全部终态定义留在 master 且清单写成空，"
                      "故此处 fail-closed；请在账本可用的工作区执行"}],
        )
    state = Store(orchd_dir, readonly=True).replay()
    terminal_ids = {
        tid for tid, ts in state.items()
        if getattr(ts, "status", None) in ("completed", "cancelled")
    }

    existing, manifest = read_archive_with_manifest(master, target)
    effective_relpath = archive_relpath
    if isinstance(manifest, dict) and isinstance(manifest.get("path"), str):
        effective_relpath = manifest["path"]
    resolved = resolve_archive_path(target, {"path": effective_relpath})
    if resolved is None:  # pragma: no cover - 上面恒给 path
        raise ArchiveMalformedError("归档路径解析失败")
    archive_path = Path(resolved)

    # 孤儿归档守卫（含 --dry-run）：清单为空骨架/缺席（read_archive_with_manifest
    # 对两态都返回 manifest=None）但归档文件在场且非空 ⇒ 磁盘归档是无锚孤儿，
    # 无法与任何清单核对。若按「无归档」处理，write_archive 会以本次终态定义
    # 覆盖它并返回成功信号 —— 历史定义**静默消失**（前次运行已把它们从 master
    # 摘除，故为双向永久丢失）。故 fail-closed 拒绝；空 tasks 文件无害，放行。
    if manifest is None and archive_path.is_file():
        orphan_count = _orphan_archive_task_count(archive_path)
        if orphan_count:
            raise ArchiveOrphanedError(
                f"清单为空骨架但归档文件在场且非空（{orphan_count} 条），"
                f"拒绝覆盖以免静默丢失历史定义: {archive_path}；"
                "确认后手工删除该文件，或恢复 master 的 definition_archive 清单"
                "（path/count/sha256）再执行"
            )

    all_tasks = master.get("tasks")
    if not isinstance(all_tasks, list):
        raise OrchdError(
            ErrorCode.E007,
            f"master_tasks_malformed: tasks 非列表（{type(all_tasks).__name__}）",
            [{"path": str(target)}],
        )
    active, terminal = split_terminal_definitions(all_tasks, terminal_ids)
    merged, added = merge_definitions(existing, terminal)
    manifest_next = build_manifest(effective_relpath, merged)

    summary: dict[str, Any] = {
        "path": str(target),
        "archive_path": str(archive_path),
        "dry_run": dry_run,
        "schema_version": version,
        "terminal_ids": len(terminal_ids),
        "to_archive": len(terminal),
        "archived_added": len(added),
        "archive_count_before": len(existing),
        "archive_count_after": len(merged),
        "remaining": len(active),
        "manifest": manifest_next,
    }
    if dry_run:
        summary["performed"] = False
        summary["reason"] = "dry_run"
        return summary

    if not added and manifest == manifest_next:
        summary["performed"] = False
        summary["reason"] = "no_change"
        return summary

    try:
        before_archive = (
            archive_path.read_text(encoding="utf-8")
            if archive_path.is_file() else None
        )
        before_master = target.read_text(encoding="utf-8")
    except OSError as exc:
        # 执行前快照读失败：具名 fail-closed（裸 OSError 会绕过本模块的异常面，
        # 且没有快照就无法保证失败可回滚）。
        raise ArchiveMalformedError(
            f"执行前快照读取失败（无快照即无法保证失败可回滚）: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    master_next = dict(master)
    master_next["tasks"] = active
    master_next[ARCHIVE_KEY] = manifest_next
    try:
        write_archive(archive_path, merged)
        _write_text_atomic(
            target, json.dumps(master_next, ensure_ascii=False, indent=2) + "\n"
        )
        summary["verified"] = _verify_archive_result(
            target, archive_path, manifest_next, terminal_ids
        )
    except BaseException:
        # 两个文件各自原子替换 ⇒ 单文件不会半写；但「归档已并、master 已摘」与
        # 「归档已并、master 未摘」都是跨文件的中间态。此处把两文件一并回滚到
        # 执行前内容（回滚同为原子替换），失败即恢复原状，不留中间态。
        _write_text_atomic(target, before_master)
        if before_archive is None:
            archive_path.unlink(missing_ok=True)
        else:
            _write_text_atomic(archive_path, before_archive)
        raise
    summary["performed"] = True
    summary["reason"] = "archived"
    return summary


def _verify_archive_result(
    master_path: Path,
    archive_path: Path,
    manifest: dict[str, Any],
    terminal_ids: set[str],
) -> dict[str, Any]:
    """执行后自复验：两文件重读、摘要重算、终态 id 在 master 中归零。"""
    master = _load_master_dict(master_path)
    remain = [
        str(t.get("id")) for t in master.get("tasks") or []
        if isinstance(t, dict) and t.get("id") in terminal_ids
    ]
    if remain:
        raise DefinitionArchiveError(
            f"自复验失败：master 仍含终态定义 {sorted(remain)[:5]}"
        )
    if master.get(ARCHIVE_KEY) != manifest:
        raise DefinitionArchiveError("自复验失败：master 清单与写入值不一致")
    # 归档侧的存在性 / count / sha256 由 read_archive_with_manifest 逐项校验（缺文件
    # 抛 ArchiveMissingError、count 与 digest 不符各自抛具名异常），此处不再重复
    # 断言——重复只会制造「多重防线」假象（旧实现里的两处死检查即此，已删）。
    tasks, _ = read_archive_with_manifest(master, master_path)
    return {
        "master_tasks": len(master.get("tasks") or []),
        "archive_count": len(tasks),
        "archive_sha256": archive_digest(tasks),
    }
