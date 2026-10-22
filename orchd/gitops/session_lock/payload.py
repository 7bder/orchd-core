"""session lock payload 构造（_prepare_session_lock_payload）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import os

from .constants import _SESSION_LOCK_FLOCK_MARKER
from .paths import _get_session_lock_path


def _prepare_session_lock_payload(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None,
    session_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    """准备 session lock：解析 session_id、计算 lock_path、构建 lock_data。"""
    from datetime import datetime, timezone

    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    lock_path = _get_session_lock_path(orchd_dir)
    lock_data = {
        "agent_id": agent_id,
        "session_id": session_id or "",
        "nonce": os.urandom(8).hex(),
        "branch": branch,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        _SESSION_LOCK_FLOCK_MARKER: True,
    }
    return lock_path, lock_data
