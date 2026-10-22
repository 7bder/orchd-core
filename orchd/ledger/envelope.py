"""orchd.ledger.envelope — 通道 C 结构化错误封套域（task-split2-ledger-l6-events-envelope）。

自 ``orchd/ledger/__init__.py`` 整块搬出（L6）：函数体与注释逐字保留，聚合入口改为重导出。
依赖方向（单源清单：``.orchd/shared/conventions.md`` 依赖方向节）：顶层仅标准库 +
``orchd.errors``；对 ``orchd.guide`` 的取用沿用搬移前的**函数级惰性导入**，保持
``ledger → errors`` 主干不膨胀、无加载期环。
"""

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError, to_json_response

# ------------------------------------------------------------------
# 通道 C 结构化收敛（task-errexit-channel-c-structured）
# ------------------------------------------------------------------
# 手工 dict 错误（不冒泡到 cli 统一异常处理器的 {code,...} 裸 dict）拿不到
# 按码指引。structured_error 内部统一 to_json_response 语义 +
# attach_error_guidance，是通道 C 的唯一收敛入口（设计稿 §6.2）。
#
# 宿主位置选 ledger：cli / spec / gitops 均可按既有依赖方向引用
# （cli→ledger 已文档化；spec→ledger 与 validate_source 同向；
# gitops→ledger 无环），guide 依赖用函数内惰性导入保持 ledger→errors
# 主干不膨胀。


def structured_error(
    code: str,
    message: str,
    details: Any = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """通道 C 结构化错误：to_json_response + attach_error_guidance 一体化收敛。

    保证两条契约（设计稿 §8.8 / §6.2）：

    - ``error.details`` 恒为 ``list[dict]``——None/str/dict/list/其他一律归一，
      消灭 E032 类 details 为字符串的契约违例；
    - 响应恒带 ``guidance``（recovery/command/exit_type 等），按静态表挂接，
      details 含 ``hint`` 时场景化覆盖通用 recovery（对齐异常通道行为）。

    Args:
        code: 错误码名（如 ``"E032"``）；未知码回退 E007（best-effort）。
        message: 错误消息。
        details: 任意形态，归一为 ``list[dict]``。
        base_dir: guidance 路径解析用 ``.orchd`` 根（可 None）。

    Returns:
        ``{"error": {code, message, details, severity, suggest_report},
           "guidance": {...}}``。
    """
    if details is None:
        norm_details: list[dict[str, Any]] = []
    elif isinstance(details, str):
        norm_details = [{"message": details}]
    elif isinstance(details, dict):
        norm_details = [details]
    elif isinstance(details, list):
        norm_details = [
            {"message": d} if isinstance(d, str)
            else (d if isinstance(d, dict) else {"value": str(d)})
            for d in details
        ]
    else:
        norm_details = [{"value": str(details)}]
    try:
        code_enum = ErrorCode[code] if isinstance(code, str) else code
    except KeyError:
        code_enum = ErrorCode.E007
    err = OrchdError(code_enum, message, norm_details)
    resp = to_json_response(err)
    try:
        from orchd.guide import attach_error_guidance

        resp = attach_error_guidance(resp, code_enum.name, base_dir)
    except Exception:
        # guidance 为加法式附加：挂接失败不得击穿错误主体输出（通道 C best-effort）
        pass
    return resp


def _attach_structured_guidance(
    entry: dict[str, Any], base_dir: Path | None = None
) -> dict[str, Any]:
    """给 E030 完整性/降级告警条目附加结构化 ``details`` + ``guidance``。

    保留条目原有键（code/severity/message/path/guard/...）不改变既有消费方
    断言面，仅加法式附加；任何异常静默降级（告警链不得因指引挂接失败）。

    注意：details 必须用条目的**浅拷贝**——直接传条目自身会让归一后的
    ``details[0]`` 与条目互指，回填 ``entry["details"]`` 后形成循环引用，
    JSON 序列化即崩（实测 E999 Circular reference）。
    """
    try:
        resp = structured_error(
            entry.get("code", ErrorCode.E030.name),
            entry.get("message", ""),
            [dict(entry)],
            base_dir,
        )
        entry["details"] = resp.get("error", {}).get("details", [])
        guidance = resp.get("guidance")
        if guidance:
            entry["guidance"] = guidance
    except Exception:
        pass
    return entry
