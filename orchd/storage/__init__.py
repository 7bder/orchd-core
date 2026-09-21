"""storage 端口（task-storage-port-adapter-split）。

端口 / 适配器物理分层（为大目标端口化铺路，与 registry 域同法）：本模块
**只含端口** —— ``StorageBackend``（ABC，Store 通过其访问 ledger / checkpoint /
lock 的全部 I/O）。文件适配器见 ``orchd.storage.filesystem``（一适配器一模块）。

依赖方向：本模块仅标准库 typing；适配器反向依赖本模块（无顶层 import 环）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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
