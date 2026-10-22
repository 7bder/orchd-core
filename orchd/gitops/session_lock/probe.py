"""OS 级锁活跃探测（_probe_session_lock_os_active），叶域。"""

from __future__ import annotations

from typing import Any
from pathlib import Path


def _probe_session_lock_os_active(lock_path: Path) -> dict[str, Any]:
    """非阻塞 OS 活性探测：尝试对锁文件获取 flock/msvcrt 排他锁。

    task-session-lock-autoclean：新式锁持锁进程保持 fd 打开，flock 由内核
    托管——进程退出/崩溃时内核自动释放。因此「能获取 flock」⇔ 原持锁进程
    已死（stale）；「不能获取」⇔ 活锁（有进程存活持有）。

    Returns:
        ``{"stale": True}`` 原持锁进程已死（本进程刚拿到 flock，已释放）。
        ``{"stale": False, "active": True}`` 活锁（另一进程持锁中）。
    """
    import os as _os
    from orchd.lockfile import _flock_op

    # 探活前文件已消失（并发清理）：等同无锁，不创建新文件
    if not lock_path.exists():
        return {"stale": True, "active": False}

    # 直接用底层 fd + flock 探测，绕过 ExclusiveFileLock 的同进程跨实例重入
    # （重入会让探测实例"成功获取"，误判活锁为 stale）。
    try:
        fd = _os.open(str(lock_path), _os.O_RDWR)
    except OSError:
        # 打开失败（IO 等）：保守视为活锁，不误清
        return {"stale": False, "active": True}

    try:
        _flock_op(fd, "lock_nb")
    except (OSError, IOError):
        # 获取失败 → 活锁（本进程或其他进程持有）
        try:
            _os.close(fd)
        except OSError:
            pass
        return {"stale": False, "active": True}
    # 获取成功：原持锁进程已死，释放探测锁
    try:
        _flock_op(fd, "unlock")
    except (OSError, IOError):
        pass
    try:
        _os.close(fd)
    except OSError:
        pass
    return {"stale": True, "active": False}
