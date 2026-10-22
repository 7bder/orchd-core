"""session gate 原语（_acquire_session_gate），叶域。"""

from __future__ import annotations

from typing import Any
import time

from orchd.errors import OrchdError
from .constants import _SESSION_GATE_ATTEMPTS, _SESSION_GATE_BACKOFF_S, _SESSION_GATE_TIMEOUT_S


def _acquire_session_gate(gate: Any) -> dict[str, Any]:
    """有界重试（退避）获取会话门锁，返回结构化结果（永不抛异常）。

    门锁失败的旧处置只留痕便继续「检查 + 写入」，恰重开了门锁本欲关闭的
    check-then-act 竞态（task-session-gate-timeout-fail-closed）：并发下两个
    session 可同时通过检查、各自写锁标记（后写覆盖先写），即 E019 双取。
    故改为**有界重试**——退避重试 ``_SESSION_GATE_ATTEMPTS`` 次仍不可得即返回
    ``acquired=False``，由 :func:`ensure_session_lock` 收口为拒绝态（fail-closed）。

    Args:
        gate: 已构造的 ``ExclusiveFileLock``（``.session.gate.lock``）。

    Returns:
        ``{"acquired": True, "attempts": <int>}``；或
        ``{"acquired": False, "attempts": <int>, "error": <str>}``
        （``error`` 为最后一次失败摘要；门锁 IO 故障 ``OSError`` 同样按「不可得」
        处置，不抛异常穿透调用方）。
    """
    last_error = ""
    for attempt in range(1, _SESSION_GATE_ATTEMPTS + 1):
        if attempt > 1 and _SESSION_GATE_BACKOFF_S > 0:
            time.sleep(_SESSION_GATE_BACKOFF_S * (attempt - 1))
        try:
            gate.acquire(blocking=True, timeout_s=_SESSION_GATE_TIMEOUT_S)
        except (OrchdError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        return {"acquired": True, "attempts": attempt}
    return {
        "acquired": False,
        "attempts": _SESSION_GATE_ATTEMPTS,
        "error": last_error,
    }
