"""session lock 原子落盘（_acquire_and_write_session_lock），叶域。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.errors import OrchdError
from .constants import _SESSION_LOCK_REGISTRY


def _acquire_and_write_session_lock(
    lock_path: Path,
    lock_data: dict[str, Any],
) -> dict[str, Any]:
    """执行 flock 获取 + JSON 写入 + 注册表登记，返回结构化结果。

    task-session-lock-degrade-observability AC1：失败原因**分类**。此前三类语义
    完全不同的失败统一标 ``reason="io_error"``，调用方与响应无法区分「正常并发
    争用」与「真实环境故障」，也无法据此决定是否收紧为 fail-closed：

    - ``flock_contended`` —— 非阻塞 flock 被其他进程持有（``ExclusiveFileLock.acquire``
      抛 E012）。**正常并发语义**（等价 E019 workspace_busy），不是环境故障；
      若按 ``acquired=False`` 直接 fail-closed，此类正常碰撞会被误伤成硬失败。
    - ``lock_dir_unwritable`` —— 锁目录创建失败（EROFS / EACCES / 磁盘满等），
      持久性环境故障。
    - ``lock_write_failed`` —— 锁文件打开 / JSON 写入失败（文件占用、杀软扫描、
      权限等），环境故障。

    ``error`` 摘要字段三种情况下都保留（原文透传，便于排查）。

    Returns:
        ``acquired=True``（新锁或 ``reused=True`` 刷新），或 ``acquired=False``
        并带分类后的 ``reason`` 与 ``error``。
    """
    import json

    from orchd.lockfile import ExclusiveFileLock

    try:
        # worktree 维度锁可能落在 ORCHD_HOME 重定向根下，父目录未必存在
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, IOError) as exc:
        return {"acquired": False, "reason": "lock_dir_unwritable", "error": str(exc)}
    try:
        # 本进程已持同一锁：复用 fd（同 session 刷新覆盖写，可重入）
        existing = _SESSION_LOCK_REGISTRY.get(str(lock_path))
        if existing is not None:
            existing.write_text(json.dumps(lock_data, ensure_ascii=False))
            return {"acquired": True, "reused": True, "path": str(lock_path)}
        # 先持有 OS flock（非阻塞），再写 JSON 标记；flock 由内核托管，
        # 进程退出/崩溃时自动释放，检查方可探活判定 stale。
        flock = ExclusiveFileLock(lock_path)
        try:
            flock.acquire(blocking=False, timeout_s=0.5)
        except OrchdError as exc:
            # E012 = 非阻塞获取失败（锁被其他进程持有）→ 并发争用，非环境故障
            return {
                "acquired": False,
                "reason": "flock_contended",
                "error": f"flock acquire failed: {exc}",
            }
        flock.write_text(json.dumps(lock_data, ensure_ascii=False))
    except (OSError, IOError) as exc:
        # 锁文件打开 / 写入失败（含 Windows 文件占用、权限、磁盘故障）
        return {"acquired": False, "reason": "lock_write_failed", "error": str(exc)}
    _SESSION_LOCK_REGISTRY[str(lock_path)] = flock
    return {"acquired": True, "path": str(lock_path)}
