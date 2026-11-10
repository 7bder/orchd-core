"""E007 原因分档恢复文案（task-engine-defect-fix-unified-u8）。

E007 是状态/参数族大码：注册缺字段、stale_base、状态不合法三类共用「任务状态不合法」
模板会把 agent 导向错误动作（例如注册缺字段时去查状态机）。本域只放数据与纯文案构造
（无 IO、无 raise）；``errors.attach_error_guidance`` 在渲染处按 ``details.reason`` 消费。

独立成域的预算理由：errors.py 的棘轮（tests/test_guide_pkg.py）只紧不松，分流文案若就地
增行会越界；本域上限后登记为 120 行。read 以**元组**形态登记在分档数据里，由 errors.py
的渲染处消费后重算 rules 摘要（手写 ``{"read": [...]}`` 字面量会被
test_guide_routing_meta 门禁拒绝，故此处不出现 dict 字面量）。
"""

from __future__ import annotations

from typing import Any

from .digest import _ENTRY_CMD


#: reason → guidance 覆盖（recovery/command/exit_type/read）；未登记 reason 走状态不合法档。
E007_REASON_GUIDANCE: dict[str, dict[str, Any]] = {
    "register_proposal_missing_fields": {
        "recovery": (
            "注册提案缺 schema 必填字段（details.missing）：按 schema/_master.schema.json "
            "的 tasks[].required 补齐后重试 amend --register——带 default 的字段由注册"
            "自动补齐，无需手填"
        ),
        "command": f"{_ENTRY_CMD} amend --register <proposal.json>",
        "exit_type": "exec-command",
        "read": ("rules/intake.md",),
    },
}


def stale_base_recovery(detail: dict[str, Any]) -> str:
    """stale_base 按站点分流：CAS 冲突可直接重试；head-drift 须先同步 main 再重试。

    两个 raise 站点语义不同（task-concurrent-amend-lost-update）：
    ``orchd/split/amend_admission.py``（CAS，details 带 expected_head/head_now）拒绝时
    未写任何内容，重试即重新读 base 重放；``orchd/split/amend.py``（head-drift，
    details 带 base_sha/head_sha）说明本地 main 落后于并行推进，直接重试会再次撞同一
    漂移。故按 detail 字段分流两种动作，而不是一句「状态不合法」。
    """
    if detail.get("expected_head") is not None or detail.get("head_now") is not None:
        return (
            "stale_base(CAS)：主工作树已被并行推进，本次未写入任何内容——直接重试原命令"
            "即可（引擎持锁后会重读基线并重放变更）"
        )
    return (
        "stale_base(head-drift)：main 已被并行推进（base 与本地 HEAD 分叉），本次未写入"
        "任何内容——先在主工作树把 main 同步到最新提交（等待/拉取并行 amend 落定）后再"
        "重试原命令；直接重试会再次撞同一漂移"
    )


def state_invalid_recovery(detail: dict[str, Any]) -> str | None:
    """状态不合法类 E007 的场景化恢复文案（代入 details 实际状态，不外漏占位符）。"""
    current = detail.get("current_status", detail.get("current"))
    target = detail.get("target_status", detail.get("target"))
    event = detail.get("event_type")
    allowed = detail.get("allowed_from")
    if current is None and target is None and not event:
        return None
    arrow = f" --{event}--> " if event else " → "
    allowed_text = f"，允许集合 {sorted(allowed)}" if allowed else ""
    return (
        f"任务状态不合法：当前 {current}{arrow}{target}{allowed_text}；用 status 查任务"
        "当前状态后按允许来源重试（注册类命令另需确认提案字段齐全、基线未陈旧）"
    )


def e007_guidance_override(detail: Any) -> dict[str, str] | None:
    """单个 detail → guidance 覆盖（recovery/hint/command/exit_type）；不适用返回 None。"""
    if not isinstance(detail, dict):
        return None
    reason = detail.get("reason")
    if isinstance(reason, str) and reason in E007_REASON_GUIDANCE:
        entry = E007_REASON_GUIDANCE[reason]
        return {
            "recovery": entry["recovery"],
            "hint": entry["recovery"],
            "command": entry["command"],
            "exit_type": entry["exit_type"],
            "read": entry["read"],
        }
    if reason == "stale_base":
        text = stale_base_recovery(detail)
        return {
            "recovery": text,
            "hint": text,
            "command": f"{_ENTRY_CMD} status",
            "exit_type": "manual-action",
            "read": ("rules/git.md",),
        }
    if reason in (None, "invalid_usage"):
        text = state_invalid_recovery(detail)
        if text is None:
            return None
        return {
            "recovery": text,
            "hint": text,
            "command": f"{_ENTRY_CMD} status",
            "exit_type": "manual-action",
        }
    return None
