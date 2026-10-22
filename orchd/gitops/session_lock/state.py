"""锁状态读取与 gate 不可用态（_lock_state/_gate_unavailable_state）。"""

from __future__ import annotations

from typing import Any

from .degrade import _log_session_lock_degrade


def _lock_state(acquire_result: dict[str, Any], gate_acquired: bool) -> dict[str, Any]:
    """把 :func:`session_lock_acquire` 结果收口为 :func:`ensure_session_lock` 的返回态。

    R2-7：``acquired=False``（flock / IO 失败）时必须**返回真实持锁态**并留痕，
    不得让调用方误以为已持锁（并发保护静默降级）。

    Args:
        acquire_result: ``session_lock_acquire`` 的结构化结果（唯一真源，不重算）。
        gate_acquired: 本次「检查 + 写入」是否真的被门锁串行化。

    Returns:
        以 acquire 结果为基础 + ``gate_acquired``；未持锁时追加 ``degraded``/``hint``
        并落 stderr 留痕。
    """
    state = dict(acquire_result)
    state["gate_acquired"] = gate_acquired
    if not acquire_result.get("acquired"):
        state["degraded"] = True
        state["hint"] = (
            "会话锁标记写入失败（best-effort 降级）：本会话**未持锁**，"
            "并发保护已失效；请排查锁目录可写性 / 文件占用后重跑"
        )
        _log_session_lock_degrade("acquire_failed", state)
    return state


def _gate_unavailable_state(gate_result: dict[str, Any]) -> dict[str, Any] | None:
    """门锁不可得的 fail-closed 收口态：拒绝本次锁获取（含 stderr 留痕）。

    ``reason="gate_timeout"`` + ``degraded=True`` 声明「本会话未持锁、本次写命令
    未经串行化保护」，``exit_type="await-external"`` 指引调用方等待持锁者释放后
    重试（而非降级续跑）。**本次「检查 + 写入」整体被放弃**，锁标记文件不被创建 /
    覆盖——这正是与旧 best-effort 续跑的本质差别。

    返回 ``None`` 仅作测试负控（模拟「拒绝收口」被移除的修复前形态：调用方退回
    非串行 check + 写入），生产路径恒返回拒绝态。
    """
    state: dict[str, Any] = {
        "acquired": False,
        "reason": "gate_timeout",
        "error": gate_result.get("error") or "gate_unavailable",
        "degraded": True,
        "gate_acquired": False,
        "attempts": gate_result.get("attempts"),
        "retriable": True,
        "exit_type": "await-external",
        "hint": (
            "会话门锁不可得（E012 超时，有界重试已耗尽）：本次「检查 + 写入」按 "
            "fail-closed 拒绝执行，本会话**未持锁**、并发保护未生效；请等待持锁者"
            "释放后重试同一命令（僵死持有者用 "
            "python .orchd/__main__.py watchdog --timeout 0 排查释放）"
        ),
    }
    _log_session_lock_degrade("gate_unavailable", {"guard": "session_gate", **state})
    return state
