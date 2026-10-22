from __future__ import annotations

from typing import Any
import json
import sys

def _log_amend_guard_degrade(entry: dict[str, Any]) -> None:
    """amend 准入守卫降级留痕（``orchd ▸ [amend-guard]``，best-effort，R2-4）。

    stderr 是留痕通道（stdout 恒为 JSON 机器契约，见 conventions.md「命令输出通道
    契约」）；任何异常静默跳过，不阻断 amend 主流程。不被 ``ORCHD_QUIET`` 抑制：
    与 worktree ``[回收]`` 这类常规噪声不同，准入守卫失效属安全相关降级决策，
    必须可追溯（R2-4 的原缺陷正是零留痕）。

    Args:
        entry: 结构化降级条目（guard / severity / status / reason / error / hint）。
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
        print(
            f"orchd ▸ [amend-guard] {json.dumps(entry, ensure_ascii=False)}",
            file=sys.stderr,
        )
    except Exception:
        pass
