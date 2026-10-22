"""session lock 检查（session_lock_check）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from .constants import _SESSION_LOCK_FLOCK_MARKER, _SESSION_LOCK_TIMEOUT_MIN
from .paths import _get_session_lock_path
from .probe import _probe_session_lock_os_active
from .reclaim import _reclaim_stale_lock


def session_lock_check(
    orchd_dir: Path,
    timeout_min: int = _SESSION_LOCK_TIMEOUT_MIN,
) -> dict[str, Any]:
    """检查 session lock 状态：是否存在、是否超时、内容是否合法。

    task-session-lock-autoclean（改 B）：新式 flock 活性锁（JSON 含
    ``flock_active: true``）优先做 **OS 非阻塞探活**——
    - 原持锁进程已死（能获取 flock）→ 判定 stale 并**自动清理**（删除 JSON），
      返回 ``{"locked": False, "reason": "stale_cleaned", ...}``；
    - 活锁（不能获取 flock）→ 返回 locked（调用方拒绝写入 E019），
      此时不再仅凭 timeout 判死（活锁即使超时也由 watchdog 另行处理）。
    旧纯 JSON 锁（无 ``flock_active`` 字段）保持兼容：按 timeout 判定，
    不探活、不误清。

    Args:
        orchd_dir: .orchd 目录路径。
        timeout_min: 超时分钟数（默认 60）。超时视为僵死锁，可覆盖。

    Returns:
        结构化结果，永不抛异常：
            - ``{"locked": False}`` 无锁文件 / 锁已超时 / 锁文件损坏（可覆盖）。
            - ``{"locked": False, "reason": "stale_cleaned", "agent_id": <str>,
                 "session_id": <str>, "age_min": <float>,
                 "cleanup_result": {...}}``
              新式锁且持锁进程已死，已自动清理（可覆盖）。
            - ``{"locked": True, "agent_id": <str>, "branch": <str|None>,
                 "timestamp": <str>, "age_min": <float>}``
              锁有效且未超时 / 新式活锁，调用方应拒绝写入（E019 workspace_busy）。

    Note:
        锁文件损坏（JSON 解析失败、缺少必要字段）视为可覆盖（容错），
        返回 ``{"locked": False, "reason": "corrupted"}``。
    """
    import json
    from datetime import datetime, timezone

    lock_path = _get_session_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"locked": False}

    # 读取锁内容：优先走本进程持锁 fd（Windows msvcrt 字节锁阻止新句柄读取）
    from orchd.lockfile import read_locked_text

    content = read_locked_text(lock_path)
    if content is None:
        try:
            content = lock_path.read_text(encoding="utf-8")
        except (OSError, IOError):
            content = None
    if content is None:
        # 读取失败：Windows 下多为「他进程持锁」字节锁阻止读取；探活判定
        probe = _probe_session_lock_os_active(lock_path)
        _read_err = (
            f"lock file unreadable ({lock_path.name}): "
            "read_locked_text returned None, fallback read_text also failed; "
            "likely held by another process (msvcrt byte lock on Windows)"
        )
        if probe.get("active"):
            return {"locked": True, "reason": "no_marker", "read_error": _read_err}
        return {"locked": False, "reason": "corrupted", "read_error": _read_err}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 锁文件损坏：视为可覆盖
        return {"locked": False, "reason": "corrupted"}

    # 校验必要字段
    agent_id = data.get("agent_id")
    timestamp_str = data.get("timestamp")
    if not agent_id or not timestamp_str:
        return {"locked": False, "reason": "corrupted"}

    # 解析时间戳
    try:
        lock_time = datetime.fromisoformat(timestamp_str)
        if lock_time.tzinfo is None:
            # 兼容无时区的时间戳（视为 UTC）
            lock_time = lock_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"locked": False, "reason": "corrupted"}

    # 计算锁年龄
    now = datetime.now(timezone.utc)
    age_seconds = (now - lock_time).total_seconds()
    age_min = age_seconds / 60.0

    # 新式 flock 活性锁：优先 OS 探活判定 stale（task-session-lock-autoclean）
    if data.get(_SESSION_LOCK_FLOCK_MARKER):
        # pass9 F10：持探测锁内 rename-aside 后删除（窗口归零）；held/error
        # 均保守判活锁（fail-safe：不误清他人锁）
        reclaim = _reclaim_stale_lock(lock_path)
        if reclaim.get("reclaimed"):
            cleaned = True
            return {
                "locked": False,
                "reason": "stale_cleaned",
                "agent_id": agent_id,
                "session_id": data.get("session_id") or "",
                "branch": data.get("branch"),
                "age_min": age_min,
                "cleanup_result": {"cleaned": cleaned, "path": str(lock_path)},
            }
        # 活锁：进程仍持有 flock → 有效锁（即使超时也不仅凭 timeout 判死）
        return {
            "locked": True,
            "agent_id": agent_id,
            "session_id": data.get("session_id") or "",
            "nonce": data.get("nonce") or "",
            "branch": data.get("branch"),
            "timestamp": timestamp_str,
            "age_min": age_min,
            _SESSION_LOCK_FLOCK_MARKER: True,
        }

    # 旧纯 JSON 锁兼容：无 flock 活性标记，按 timeout 判定，不探活不误清
    # 超时视为僵死锁，可覆盖
    if age_min >= timeout_min:
        return {"locked": False, "reason": "timeout", "age_min": age_min}

    # 锁有效且未超时
    return {
        "locked": True,
        "agent_id": agent_id,
        "session_id": data.get("session_id") or "",
        "nonce": data.get("nonce") or "",
        "branch": data.get("branch"),
        "timestamp": timestamp_str,
        "age_min": age_min,
    }
