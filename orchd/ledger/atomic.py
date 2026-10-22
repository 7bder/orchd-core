"""orchd 账本原子写与行式文本原语域（file-split-program W1-L2，2026-10-15）。

本模块自 ``orchd/ledger/__init__.py`` 整块搬出两组叶子原语（语句级等价，零逻辑改动）：

- **原子替换域**：``_atomic_replace``（Windows 句柄争用有界重试、耗尽抛结构化 E007）、
  ``_fsync_dir``、``_is_transient_replace_error`` 与两个重试常量；
- **原子写域**：``_write_text_atomic`` / ``_write_bytes_atomic``（tmp + flush + fsync +
  ``_atomic_replace`` + 目录项 fsync）；
- **行式台账文本原语**：``single_line`` / ``read_text_or_error``。

依赖方向：本模块只依赖标准库与 ``orchd.errors``——**不导入 orchd.ledger 及其任何子模块**，
故聚合入口 ``orchd/ledger/__init__.py`` 可安全地自本模块重导出（无环）。调用方
（intake / worktree / storage / cli 等）仍按 ``from orchd.ledger import <符号>`` 惰性导入，
``monkeypatch.setattr("orchd.ledger._atomic_replace", ...)`` 的补丁点因此不变。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from orchd.errors import ErrorCode, OrchdError

# 原子替换的 Windows 有界重试参数（L-8）
_ATOMIC_REPLACE_RETRIES = 5
_ATOMIC_REPLACE_BACKOFF_S = 0.05


def _is_transient_replace_error(exc: OSError) -> bool:
    """是否属「目标文件被短暂占用」类可重试错误（Windows WinError 5 / 32）。"""
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in (5, 32)


def _fsync_dir(path: Path) -> None:
    """best-effort fsync 目录项（Windows 下不可用 → 静默忽略 OSError）。"""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _atomic_replace(
    tmp_path: Path,
    target_path: Path,
    *,
    retries: int = _ATOMIC_REPLACE_RETRIES,
    backoff_s: float = _ATOMIC_REPLACE_BACKOFF_S,
) -> None:
    """原子替换 ``target_path`` ← ``tmp_path``，Windows 句柄争用有界重试（L-8）。

    Windows 下目标文件被短暂持有句柄（并行的 orchd 只读命令正在读、杀软扫描、
    索引器）时 ``os.replace`` 抛 ``PermissionError``（WinError 5/32）。旧实现
    无重试：一次争用即让「事件已 append、但 checkpoint 未更新」的中间态以
    E999 逃逸（命令失败、状态错配）。此处退避重试有限次，耗尽后抛结构化
    E007（invalid_state，通道 A 已登记且为错误级；同模块既有 git_sync_failed
    先例），**不无限重试、不静默吞掉**。

    Args:
        tmp_path: 已写完并 fsync 的临时文件。
        target_path: 目标运行时文件（ledger / checkpoint）。
        retries: 最大尝试次数（>=1）。
        backoff_s: 线性退避基数（秒）。

    Raises:
        OrchdError: E007，重试耗尽仍未成功（含最后异常与占用提示）。
    """
    attempts = max(1, retries)
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(str(tmp_path), str(target_path))
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc):
                raise
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(backoff_s * (attempt + 1))
    raise OrchdError(
        ErrorCode.E007,
        "atomic_replace_failed: 目标运行时文件被占用，重试耗尽",
        [
            {
                "reason": "atomic_replace_failed",
                "tmp": str(tmp_path),
                "target": str(target_path),
                "retries": attempts,
                "error": f"{type(last_exc).__name__}: {last_exc}",
                "hint": "关闭持有该文件的进程（编辑器 / 杀软扫描 / 并行的 orchd 只读命令）后重试",
            }
        ],
    )


def _write_text_atomic(path: Path, text: str) -> None:
    """session 运行时文件的原子写（task-pass9-runtime-atomic-writes，pass9 F13）。

    与 checkpoint / 事件文件同一硬化等级：tmp + fsync + _atomic_replace（含
    L-8 Windows 句柄争用重试）+ 目录项 fsync。此前 session 四处写为裸
    write_text，崩溃窗口会产生半截 JSON，读侧 json.loads 以 E999 逃逸——
    最常写的活性文件硬化反而最弱。tmp 名带 pid：session 文件无锁保护，
    同会话并行进程各写各的 tmp，replace 原子决胜，不会互写撕裂。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    _atomic_replace(tmp, path)
    _fsync_dir(path.parent)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """字节目标点的原子写（与 :func:`_write_text_atomic` 同档）。

    供写 bytes 的耐久目标点收敛（合并结果回写、文件搬运等）：tmp + flush +
    fsync + ``_atomic_replace``（含 WinError 5/32 重试）+ 目录 fsync。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    _atomic_replace(tmp, path)
    _fsync_dir(path.parent)


# --------------------------------------------------------- 行式台账文本原语（T4a）
# task-idea-archive-hardening：IDEAS.md / IDEAS-archive.md 是**行式**台账，
# 「自由文本拼进一行」与「读盘兜底」两件事此前在别处各写一遍，漏一处就出界
# （实测：归档件读有兜底、主文件读没有；四个自由文本入口只有一个收敛了换行）。
# 收敛到本层的理由是依赖方向而非归属偏好：``orchd/ideas.py`` 已模块级导入
# ``orchd.intake``，任何放在 ideas 的共用物都会让 intake 反向导入成环；而
# ideas / intake / cli.commands.ideas 三者都已依赖 ledger（见
# .orchd/shared/conventions.md 依赖方向），落此层零新增依赖边。


def single_line(text: str) -> str:
    """把文本折成单行（所有空白与换行归并为单个空格）。

    行式台账的安全网：``- 归档裁决:`` / ``- notes追记:`` / ``- 论证:`` / ``## 标题``
    四处都是「前缀 + 自由文本拼一行」，输入里一个换行就能伪造出假小节标题，并让按
    ``- id:`` 的判重读到空 id（实测探针：归档件 ``##`` 头 1→2）。归一在**写入点**
    完成，调用方无需记得检查。
    """
    return " ".join(text.split())


def read_text_or_error(path: Path) -> tuple[str, str | None]:
    """按 UTF-8 读文本；失败返回 ``("", "read_error")``，**绝不抛异常**。

    覆盖两类实测坏形态：非 UTF-8 文件（``UnicodeDecodeError``）与目录形路径
    （``IsADirectoryError`` / ``PermissionError``，均属 ``OSError``）。此前同一形状
    的兜底只在归档件读那处写了、主文件读那处没写，于是 ``idea_archive_decision``
    的「永不抛异常」docstring 失实、CLI 落到 E999 出界（task-idea-archive-hardening）。

    失败用 ``""`` 而非 ``None`` 承载文本：调用方一律先看第二个返回值再决定处置，
    用 ``""`` 可免去每处 ``str | None`` 收窄（且空文件与失败在业务上从不混用——
    看到 ``read_error`` 的调用方当场结构化拒绝，不会去读文本）。
    """
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, IOError, UnicodeDecodeError):
        return "", "read_error"
