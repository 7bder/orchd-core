"""降级日志（_log_session_lock_degrade），叶域。"""

from __future__ import annotations

from typing import Any
import json
import sys


def _log_session_lock_degrade(action: str, payload: dict[str, Any]) -> None:
    """会话锁降级留痕（``orchd ▸ [session-lock]``，best-effort，R2-7）。

    stderr 是留痕通道（stdout 恒为 JSON 机器契约，见 conventions.md「命令输出通道
    契约」）；任何异常静默跳过，不阻断取锁主流程。不被 ``ORCHD_QUIET`` 抑制：
    与 worktree ``[回收]`` 这类常规噪声不同，**会话锁失效意味着并发保护降级**
    （R2-7 的原缺陷正是获取失败零留痕、调用方误以为已持锁），必须可追溯。

    Args:
        action: 动作名（``gate_unavailable`` / ``acquire_failed``）。
        payload: 结构化条目（reason / error / gate_acquired / hint…）。
    """
    # 跨 mypy 版本免疫：getattr 取回 Any，无需 type-ignore（旧 ignore 码
    # [attr-defined] 在新版报 union-attr，pass8 F3）；缺失/异常仍静默跳过。
    _reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        record = {"action": action, **payload}
        print(
            f"orchd ▸ [session-lock] {json.dumps(record, ensure_ascii=False)}",
            file=sys.stderr,
        )
    except Exception:
        pass
