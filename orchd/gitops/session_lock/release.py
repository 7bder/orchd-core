"""session lock 释放（session_lock_release/release_session_lock_if_owned）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from .check import session_lock_check
from .constants import _SESSION_LOCK_REGISTRY
from .paths import _get_session_lock_path
from .reclaim import _reclaim_stale_lock


def session_lock_release(orchd_dir: Path) -> dict[str, Any]:
    """释放 session lock（幂等：锁文件不存在时不报错）。

    task-session-lock-autoclean（改 B）：若本进程持有该锁的 flock fd
    （注册表登记），先释放 flock 并关闭 fd，再删除 JSON 标记文件；
    非本进程持有的锁（watchdog 清理他人僵死锁）直接删除标记文件
    （flock 由持锁进程退出/释放时内核回收）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"released": True, "reason": "removed"}`` 锁文件已删除。
            - ``{"released": True, "reason": "not_exists"}`` 锁文件本就不存在（幂等）。
            - ``{"released": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    lock_path = _get_session_lock_path(orchd_dir)
    # 先释放本进程持有的 flock fd（若持有），再处理标记文件
    flock = _SESSION_LOCK_REGISTRY.pop(str(lock_path), None)
    if flock is not None:
        flock.release()
    if not lock_path.exists():
        return {"released": True, "reason": "not_exists"}
    # P2-6 + task-pass9-session-lock-unlink（pass9 F10）：他人持活锁时不得
    # unlink；stale 时在**持探测锁内** rename-aside 后删除——消除旧流程
    # 「probe 释放 → unlink」间隙的分裂互斥竞态（flock-unlink TOCTOU）。
    reclaim = _reclaim_stale_lock(lock_path)
    if reclaim.get("held"):
        return {"released": False, "reason": "held_by_other"}
    if reclaim.get("reclaimed"):
        return {"released": True, "reason": "removed"}
    return {
        "released": False,
        "reason": "io_error",
        "error": reclaim.get("error", "unknown"),
    }


def release_session_lock_if_owned(
    orchd_dir: Path,
    agent_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """条件释放 session 锁：仅当锁存在且持有者 == ``agent_id`` 且 session 一致才释放。

    task-session-lock-lifecycle：写命令（done/review/claim）在退出时（含异常路径）
    调用，确保持有本 agent 的锁不会因异常漏放；绝不误释放他人锁/他 session 锁。

    Returns:
        ``{"released": bool, "reason": str}``。锁缺失 / 持他人锁时
        ``released=False``（如 ``reason="not_owner_or_absent"``）。
    """
    if orchd_dir is None:
        return {"released": False, "reason": "no_project"}
    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    check = session_lock_check(orchd_dir)
    holder_session = check.get("session_id") or ""
    if (
        check.get("locked")
        and check.get("agent_id") == agent_id
        and (not holder_session or not session_id or holder_session == session_id)
    ):
        return session_lock_release(orchd_dir)
    return {"released": False, "reason": "not_owner_or_absent"}
