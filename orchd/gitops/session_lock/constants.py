"""session lock/gate 常量（文件名/超时/gate 退避/registry/flock marker），叶域。"""

from __future__ import annotations

from typing import Any


_SESSION_LOCK_FILENAME = ".session.lock"


_SESSION_GATE_FILENAME = ".session.gate.lock"


_SESSION_GATE_TIMEOUT_S = 10.0


_SESSION_GATE_ATTEMPTS = 3


_SESSION_GATE_BACKOFF_S = 0.25


_SESSION_LOCK_REGISTRY: dict[str, Any] = {}


_SESSION_LOCK_TIMEOUT_MIN = 60


_SESSION_LOCK_FLOCK_MARKER = "flock_active"
