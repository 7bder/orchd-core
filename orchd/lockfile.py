"""orchd/lockfile.py — 统一排他文件锁原语（ExclusiveFileLock）。

提供进程级可重入的排他文件锁原语，封装跨平台 flock（POSIX）/ msvcrt（Windows）
差异，并内嵌「同进程 depth 登记」语义：同一进程重复 acquire 同一锁返回成功
（depth+1），release 到 0 才真正释放 flock；跨进程仍互斥。

设计要点：
- mode A（flock）：acquire 成功 ⇔ 本进程真持锁，无 check-then-act 窗口。
  flock 由内核托管：fd 关闭（进程退出/崩溃）时内核自动释放，不会遗留僵死锁。
- mode B（降级）：文件系统被探测为无法验证 flock 时，退化为「探测即告警/降级」，
  输出明确告警，不伪装成有效锁。
- probe_lock_support 在目标路径执行一次 flock 探测，判定本地文件系统是否支持
  可靠的排他 flock；不支持时调用方应降级使用或放弃。

依赖方向：lockfile.py → errors.py（不导入 ledger / spec / onboard）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

# 进程级 depth 登记：路径 → (fd, depth)。
# 同一进程内所有 ExclusiveFileLock 实例共享，保证跨实例重入语义与释放安全。
_depth_registry: dict[str, tuple[int, int]] = {}


class ExclusiveFileLock:
    """进程级可重入的排他文件锁原语。

    用法::

        lock = ExclusiveFileLock(path)
        lock.acquire()   # 首次：获取 flock，depth=1
        lock.acquire()   # 同进程重入：depth=2，返回成功
        lock.release()   # depth=1，仍持锁
        lock.release()     # depth=0，真正释放 flock
    """

    def __init__(self, lock_path: Path | str) -> None:
        self._lock_path = Path(lock_path)
        self._fd: int | None = None
        self._depth: int = 0

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def depth(self) -> int:
        return self._depth

    def acquire(self, *, blocking: bool = False, timeout_s: float = 10.0) -> bool:
        """获取排他锁。

        - 同进程已持锁：depth+1，立即返回 True（可重入）。
        - 跨进程竞争：
          - ``blocking=False``（默认）：非阻塞尝试，失败抛 E012。
          - ``blocking=True``：阻塞等待，超时抛 E012。

        Returns:
            True 表示成功获取（含重入）。

        Raises:
            OrchdError: E012 获取失败（被其他进程持有且未在超时内释放）。
        """
        key = str(self._lock_path.resolve())

        # 同进程重入：已有 fd 且属于本实例/进程 → depth+1
        if self._fd is not None and key in _depth_registry:
            fd, depth = _depth_registry[key]
            if fd == self._fd:
                _depth_registry[key] = (fd, depth + 1)
                self._depth = depth + 1
                return True

        # 新获取：打开 fd 并 flock
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)

        if blocking:
            self._acquire_blocking(fd, timeout_s)
        else:
            self._acquire_nonblocking(fd)

        self._fd = fd
        self._depth = 1
        _depth_registry[key] = (fd, 1)
        return True

    def _acquire_nonblocking(self, fd: int) -> None:
        """非阻塞获取 flock，失败抛 E012。"""
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            os.close(fd)
            raise OrchdError(
                ErrorCode.E012,
                "lock_timeout: failed to acquire lock (held by another process)",
                [{"path": str(self._lock_path), "hint": "另一进程正持有该锁。稍后重试，或检查僵死锁（watchdog）。"}],
            ) from exc

    def _acquire_blocking(self, fd: int, timeout_s: float) -> None:
        """阻塞获取 flock（带超时），超时抛 E012。"""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (OSError, IOError):
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise OrchdError(
                        ErrorCode.E012,
                        f"lock_timeout: failed to acquire lock within {timeout_s}s",
                        [{"path": str(self._lock_path),
                         "timeout_s": timeout_s,
                         "hint": "超时未获取锁。稍后重试，或检查僵死锁（watchdog）。"}],
                    )
                time.sleep(0.05)

    def release(self) -> bool:
        """释放排他锁。

        - depth > 1：depth-1，仍持锁，返回 False。
        - depth == 1：真正释放 flock + close fd + 清理登记，返回 True。

        Returns:
            True 表示已真正释放；False 表示仍持锁（depth > 1）。
        """
        key = str(self._lock_path.resolve())

        if self._fd is None or self._depth <= 0:
            return True

        self._depth -= 1
        if self._depth > 0:
            _depth_registry[key] = (self._fd, self._depth)
            return False

        # depth == 0：真正释放
        self._release_flock(self._fd)
        _depth_registry.pop(key, None)
        self._fd = None
        return True

    def _release_flock(self, fd: int) -> None:
        """释放 flock 并 close fd（best-effort）。"""
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def check(self) -> dict[str, Any]:
        """检查锁状态（不阻塞）。

        Returns:
            ``{"held": True, "by_current_process": bool, "depth": int}``
            当前进程持锁时 by_current_process=True。
            ``{"held": False}`` 未被持有。
        """
        key = str(self._lock_path.resolve())

        if self._fd is not None and key in _depth_registry:
            fd, depth = _depth_registry[key]
            if fd == self._fd and depth > 0:
                return {"held": True, "by_current_process": True, "depth": depth}

        # 尝试非阻塞获取：成功=未持有（我们刚拿到），失败=被其他进程持有
        if not self._lock_path.exists():
            return {"held": False}
        try:
            fd = os.open(str(self._lock_path), os.O_RDWR)
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # 获取成功 → 原无持有者
                self._release_flock(fd)
                return {"held": False}
            except (OSError, IOError):
                os.close(fd)
                return {"held": True, "by_current_process": False}
        except OSError:
            return {"held": False}

    def clear(self) -> None:
        """强制清理（best-effort）：释放 flock、close fd、清理登记。

        用于 watchdog 接管僵死锁。不抛异常。
        """
        key = str(self._lock_path.resolve())
        if self._fd is not None:
            self._release_flock(self._fd)
        _depth_registry.pop(key, None)
        self._fd = None
        self._depth = 0


def probe_lock_support(target_dir: Path | str) -> dict[str, Any]:
    """探测目标目录所在文件系统是否支持可靠的排他 flock。

    在同一目录创建临时文件、flock、再用第二个 fd 探测是否真被持有：
    - 第二 fd 非阻塞获取失败 → flock 可靠，返回 ``{"mode": "flock"}``。
    - 第二 fd 获取成功 → flock 不可靠（内核未真正互斥），返回
      ``{"mode": "degraded", "warning": "..."}``，调用方应降级。

    Args:
        target_dir: 目标目录（通常与后续锁文件同目录）。

    Returns:
        ``{"mode": "flock"}`` 或 ``{"mode": "degraded", "warning": str}``。
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        dir=str(target_dir), prefix=".probe_lock_", suffix=".tmp", delete=False
    )
    tmp_path = Path(tmp.name)
    fd1 = os.open(str(tmp_path), os.O_CREAT | os.O_RDWR)
    try:
        # fd1 获取排他 flock
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd1, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            return {"mode": "flock"}

        # fd2 尝试非阻塞获取：应失败（fd1 持有中）
        fd2 = os.open(str(tmp_path), os.O_RDWR)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd2, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # 获取成功 → flock 未真正互斥 → 降级
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd2, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd2, fcntl.LOCK_UN)
            os.close(fd2)
            return {
                "mode": "degraded",
                "warning": (
                    f"文件系统 {target_dir} 的 flock 不可靠（内核未真正互斥）。"
                    "锁原语已降级为「探测即告警/降级」：不要依赖它保护并发写。"
                ),
            }
        except (OSError, IOError):
            os.close(fd2)
            return {"mode": "flock"}
    except OSError as e:
        return {"mode": "degraded", "warning": f"flock 探测异常：{e}"}
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd1, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd1, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        try:
            os.close(fd1)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
