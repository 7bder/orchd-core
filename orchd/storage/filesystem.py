"""文件适配器：默认文件系统存储后端（task-storage-port-adapter-split）。

端口 / 适配器分层：本模块**只含文件适配器实现**；端口（``StorageBackend``）在
``orchd.storage``。行为与路径与拆分前完全一致（纯结构移动、零行为变更）。

``_atomic_replace`` / ``_fsync_dir`` 属 ledger 的文件系统工具，仍定义于
``orchd.ledger``（其被 ledger 侧复用）；为避免顶层 import 环，本模块在
``save_checkpoint`` 内**惰性导入**它们。
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock, _depth_registry
from orchd.storage import StorageBackend


class FilesystemBackend(StorageBackend):
    """默认文件系统存储后端：行为与路径与改造前完全一致。

    持有三个文件路径（ledger / checkpoint / lock）与锁文件描述符；Store 的
    ``ledger_path`` / ``checkpoint_path`` / ``lock_path`` / ``_lock_fd``
    属性转发到本后端，保证既有测试与调用方兼容。
    """

    def __init__(self, orchd_dir: Path) -> None:
        self.orchd_dir = orchd_dir
        self.ledger_path = orchd_dir / "_ledger.jsonl"
        self.checkpoint_path = orchd_dir / "_checkpoint.json"
        self.lock_path = orchd_dir / ".lock"
        self._file_lock = ExclusiveFileLock(self.lock_path)

    def append_event(self, event: dict[str, Any]) -> None:
        """以 append 模式写一条事件到 ledger（持锁断言 + 追加 + fsync）。

        持锁断言（task-audit-ledger-write-atomicity AC1）：写路径必须持锁——
        单次 O_APPEND 在 Windows 下不保证原子，长事件行并发会撕裂 ledger。
        绕过持锁直接写入视为引擎纪律违反，抛 E007。同进程其他 Store 实例
        （如 review 合并流 merge_lock）已持同路径锁时视为持锁（_depth_registry
        进程级登记），避免容器布局下复用锁场景误报。
        """
        if (
            self._file_lock._fd is None
            and str(self.lock_path.resolve()) not in _depth_registry
        ):
            raise OrchdError(
                ErrorCode.E007,
                "append_event 必须在持锁状态下调用（acquire_lock 之后）",
                [{
                    "path": str(self.ledger_path),
                    "hint": ("写路径须先 store.acquire_lock() 再 append_event，"
                             "确保并发 append 串行原子"),
                }],
            )
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(str(self.ledger_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def read_events(
        self, from_line: int = 1, to_line: int | None = None
    ) -> list[dict[str, Any]]:
        """读取 ledger 事件（``from_line`` / ``to_line`` 均为 1-based **物理行号**）。

        ``from_line`` 语义：在文件层先跳过前 ``from_line-1`` 行，再解析剩余行。
        这样 checkpoint 之前（已被快照覆盖）的损坏行不会被解析——B-1 修复，
        恢复增量 replay 的容错语义（重构前 ``_read_ledger_lines`` 直接在文件层
        跳过，不解析被跳过的行）。

        ``to_line``（L-11，task-ledger-replay-visibility）：只解析到第 ``to_line``
        **物理行**（含）。checkpoint.ledger_line 是物理行号，撕裂/损坏行会让
        「解析事件序号」与「物理行号」错位——按序号切片（``read_events()[:n]``）
        会多带一条事件，导致 check_integrity 把引擎自身的撕裂行误报成「疑似被篡改」。

        损坏行（L-12）：除 ``warnings.warn``（向后兼容）外，同时把结构化条目
        追加到 ``self.corrupt_lines``（每次调用重置），供
        :meth:`Store.check_integrity` 汇总进 integrity_warnings / guidance——
        agent 因此可见「丢了哪些事件」，不再只有 stderr 之外的沉默。

        容错规则（task-audit-ledger-write-atomicity AC4）：
        - 最后一行 JSON 解析失败 → 跳过 + warning（可能写入未完成）
        - 中间行解析失败 → warning（E030 语义）+ 跳过，不再硬抛 E002 中断引擎；
          撕裂行（并发 append 被中断）因此降级为可读，数据可继续恢复
        """
        self.corrupt_lines = []
        if not self.ledger_path.exists():
            return []
        events: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
        # 文件层跳过前 from_line-1 行：被跳过的行不参与解析（含损坏行）
        start = max(0, from_line - 1)
        stop = len(raw_lines) if to_line is None else max(
            start, min(to_line, len(raw_lines))
        )
        for i in range(start, stop):
            stripped = raw_lines[i].strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                # 末行（含末尾空行）损坏 → 可能写入未完成，仅 warning 跳过；
                # 中间行损坏 → E030 warning + 跳过（数据可能撕裂，引擎降级继续，
                # 不再抛 E002 中断——AC4）。
                is_last = i == len(raw_lines) - 1 or all(
                    not raw_lines[j].strip() for j in range(i + 1, len(raw_lines))
                )
                if is_last:
                    warnings.warn(
                        f"ledger 最后一行解析失败，已跳过: {stripped[:80]}",
                        stacklevel=2,
                    )
                else:
                    warnings.warn(
                        f"[E030] ledger 中间行 JSON 解析失败（第 {i + 1} 行），"
                        f"已跳过（可能为并发写入撕裂行，引擎降级继续）: {stripped[:80]}",
                        stacklevel=2,
                    )
                # 结构化留痕（L-12）：不因 warn 无法被程序化消费而静默
                self.corrupt_lines.append({
                    "code": ErrorCode.E030.name,
                    "severity": "warning",
                    "message": (
                        f"ledger 第 {i + 1} 行（物理行号）JSON 解析失败，该行事件"
                        "已被跳过（replay 派生状态缺少这条事件）"
                    ),
                    "line": i + 1,
                    "path": str(self.ledger_path),
                    "kind": (
                        "truncated_last_line" if is_last else "torn_middle_line"
                    ),
                    "snippet": stripped[:80],
                })
        return events

    def event_count(self) -> int:
        if not self.ledger_path.exists():
            return 0
        count = 0
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for _ in f:
                count += 1
        return count

    def load_checkpoint(self) -> dict[str, Any] | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def save_checkpoint(self, data: dict[str, Any]) -> None:
        # 惰性导入：_atomic_replace / _fsync_dir 仍定义于 orchd.ledger，
        # 顶层导入会与 ledger → storage.filesystem 形成 import 环。
        from orchd.ledger import _atomic_replace, _fsync_dir

        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.checkpoint_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # 原子替换（L-8）：Windows 句柄争用有界重试，耗尽抛结构化 E007
        # （不再让「事件已 append、checkpoint 未更新」以 E999 逃逸）
        _atomic_replace(tmp_path, self.checkpoint_path)
        # fsync 父目录，确保持久化目录项（与 append_event 对齐；Windows 下
        # 目录 fsync 不可用，best-effort 忽略）
        _fsync_dir(self.checkpoint_path.parent)

    def acquire_lock(self) -> None:
        """获取排他文件锁（ExclusiveFileLock 原语，阻塞等待 + 超时）。

        外部接口与改造前一致：失败抛 E012。
        """
        try:
            self._file_lock.acquire(blocking=True, timeout_s=10.0)
        except OrchdError:
            raise
        except Exception as exc:
            raise OrchdError(
                ErrorCode.E012,
                "lock_timeout: failed to acquire .orchd/.lock",
                [{"path": str(self.lock_path), "hint": str(exc)}],
            ) from exc

    def release_lock(self) -> None:
        """释放文件锁（ExclusiveFileLock 原语，depth 到 0 才真正释放）。"""
        self._file_lock.release()
