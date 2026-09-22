"""storage 端口（task-storage-port-adapter-split）。

端口 / 适配器物理分层（为大目标端口化铺路，与 registry 域同法）：本模块
**只含端口** —— ``StorageBackend``（ABC，Store 通过其访问 ledger / checkpoint /
lock 的全部 I/O）。文件适配器见 ``orchd.storage.filesystem``（一适配器一模块）。

依赖方向：本模块仅标准库（typing / json / pathlib）；适配器反向依赖本模块
（无顶层 import 环）。

归档共享（task-ledger-archive-compact）：``_ledger.archive.jsonl`` 为跨后端
共享的归档文件格式（JSONL 全量事件体，与活跃文件同编码/容错语义）。
本模块承载与后端无关的归档原语；两适配器各自接线（读合并/计数/迁移），
Store 侧（compact 操作与中断恢复）见 ``orchd.ledger``。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

#: 归档文件名（账本根下，与 ``_ledger.jsonl`` 同级）。
ARCHIVE_FILENAME = "_ledger.archive.jsonl"

#: 中断恢复标记（compact 三写中途崩溃的唯一可观测痕迹，见 Store.compact_archive）。
COMPACT_JOURNAL_FILENAME = ".compact-journal.json"


def archive_path(store_dir: Path | str) -> Path:
    """归档文件路径（跨后端共享；不存在即无归档）。"""
    return Path(store_dir) / ARCHIVE_FILENAME


def compact_journal_path(store_dir: Path | str) -> Path:
    """compact 中断恢复标记路径。"""
    return Path(store_dir) / COMPACT_JOURNAL_FILENAME


def read_archive_events(
    store_dir: Path | str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读取归档事件（去重保序）与损坏条目（task-ledger-archive-compact）。

    - 按 event_id 去重（保留首次出现）：中断崩溃可能导致同一事件体同时落在
      归档与活跃文件，读侧去重使重跑天然幂等；
    - 损坏行跳过 + 结构化条目（行号为归档文件内物理行号，调用方叠加偏移上报，
      与 ``corrupt_lines`` 同形）；
    - 文件不存在 → ([], [])。

    Returns:
        ``(events, corrupt)``。
    """
    path = archive_path(store_dir)
    events: list[dict[str, Any]] = []
    corrupt: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not path.is_file():
        return events, corrupt
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events, corrupt
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            corrupt.append({
                "code": "E030",
                "severity": "warning",
                "message": (
                    f"归档第 {lineno} 行 JSON 解析失败，已跳过"
                ),
                "line": lineno,
                "path": str(path),
                "kind": "archive_torn_line",
                "snippet": stripped[:80],
            })
            continue
        if not isinstance(ev, dict):
            corrupt.append({
                "code": "E030",
                "severity": "warning",
                "message": f"归档第 {lineno} 行非 JSON 对象，已跳过",
                "line": lineno,
                "path": str(path),
                "kind": "archive_torn_line",
                "snippet": stripped[:80],
            })
            continue
        eid = ev.get("event_id", "")
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        events.append(ev)
    return events, corrupt


def archive_event_count(store_dir: Path | str) -> int:
    """归档解析成功事件数（逻辑行号基数；损坏行不计）。"""
    events, _ = read_archive_events(store_dir)
    return len(events)


def write_archive_events(
    store_dir: Path | str, events: list[dict[str, Any]]
) -> Path:
    """原子重写归档文件（task-ledger-archive-compact，compact 专用）。

    全量重写（非追加）：compact 每次按当前逻辑前缀重算归档内容，幂等；
    tmp + os.replace 原子替换。行格式与活跃文件一致。
    """
    path = archive_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False,
                               separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
    return path


class StorageBackend(ABC):
    """存储后端抽象：Store 通过本接口访问 ledger / checkpoint / lock 的全部 I/O。

    引入目的（task-storage-backend-interface，roadmap:snapshotstore-m-p0）：
    将 Store 与底层存储位置解耦，为 ORCHD_HOME 账本根重定向、并行化 / 远程化
    打地基。默认实现 :class:`orchd.storage.filesystem.FilesystemBackend`
    保持当前行为与路径不变。

    接口暴露七个方法：append_event / read_events / event_count /
    load_checkpoint / save_checkpoint / acquire_lock / release_lock。
    """

    @abstractmethod
    def append_event(self, event: dict[str, Any]) -> None:
        """以 append 模式写一条事件到 ledger（追加 + fsync）。"""

    @abstractmethod
    def read_events(
        self, from_line: int = 1, to_line: int | None = None
    ) -> list[dict[str, Any]]:
        """读取 ledger 事件（``from_line`` / ``to_line`` 为 1-based **物理行号**）。

        容错语义：末行损坏跳过 + warning，中间行损坏降级跳过 + E030 warning
        （task-audit-ledger-write-atomicity AC4，不再抛 E002 中断引擎）。
        ``from_line`` 必须在文件层先跳过前 ``from_line-1`` 行再解析——保证
        checkpoint 之前（已被快照覆盖）的损坏行不会被解析（B-1 修复，
        恢复增量 replay 的容错语义）。

        ``to_line``（L-11）：只解析到第 ``to_line`` 物理行（含）。checkpoint 的
        ``ledger_line`` 是物理行号，撕裂/损坏行会让「解析事件序号」与「物理行号」
        错位——按序号切片会多带一条事件，进而把引擎自身的撕裂行误报成篡改。

        损坏行同时以结构化条目收集到 ``self.corrupt_lines``（每次调用重置），
        供 :meth:`Store.check_integrity` 汇总进 integrity_warnings / guidance（L-12）。
        """

    @abstractmethod
    def event_count(self) -> int:
        """返回 ledger 事件总数（行数）。"""

    @abstractmethod
    def load_checkpoint(self) -> dict[str, Any] | None:
        """读取 checkpoint。文件不存在或解析失败返回 None。"""

    @abstractmethod
    def save_checkpoint(self, data: dict[str, Any]) -> None:
        """原子写入 checkpoint（write-tmp + os.replace）。"""

    @abstractmethod
    def acquire_lock(self) -> None:
        """获取排他文件锁，指数退避重试（~3.55s 总窗口），全部失败抛 E012。"""

    @abstractmethod
    def release_lock(self) -> None:
        """释放文件锁。"""
