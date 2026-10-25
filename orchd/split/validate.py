from __future__ import annotations

from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD,
    BRIEF_FIELD,
    DELIVERABLES_FIELD,
    NAME_FIELD,
)
from orchd.errors import ErrorCode, OrchdError
from typing import Any

_TERMINAL_REVISION_EVENT_REASON = "terminal_spec_revision"

_TERMINAL_TEXT_REVISABLE_FIELDS = frozenset({
    ACCEPTANCE_CRITERIA_FIELD, BRIEF_FIELD, NAME_FIELD, DELIVERABLES_FIELD,
})

def validate_terminal_revision(reason: str | None) -> str:
    """校验并归一化 ``--revise-terminal`` 的 ``--reason``（非空、非纯空白）。

    对齐"CHANGES_REQUESTED 必须附意见"的既有取向：修订理由会写入 AMEND 审计事件，
    缺失即无法回答"谁在何时以何理由改了什么"，故硬拒绝（E007）。CLI 与 amend 共用
    本函数（单一事实源，避免两处文案漂移）。

    Args:
        reason: 命令行 ``--reason`` 原文（可 None）。

    Returns:
        归一化后的理由（strip 后）。

    Raises:
        OrchdError(E007): 缺失 / 纯空白——不写事件、不改快照、不落任何副作用。
    """
    cleaned = reason.strip() if isinstance(reason, str) else ""
    if not cleaned:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_state: --revise-terminal 需附带非空 --reason（终态规格修订理由）",
            [{
                "option": "--revise-terminal",
                "hint": (
                    "修订理由会写入 AMEND 审计事件（reason="
                    f"{_TERMINAL_REVISION_EVENT_REASON}）供事后回查，纯空白视为缺失。"
                    "示例：amend --revise-terminal <task_id> --reason \"AC 文本对齐用户裁定\""
                ),
            }],
        )
    return cleaned

def is_text_only_spec_revision(
    old_task: dict[str, Any], new_task: dict[str, Any]
) -> bool:
    """变更是否仅落在终态规格文本字段（供调用方跳过与之无关的 verify dry-run）。

    文本修订不改变可执行验收面（``verify_command`` 未变），重跑其 dry-run 无信息量；
    且会让本通道被目标的**存量** E024/E027（缺 --basetemp / 不安全段）误伤——终态
    任务多为历史定义，往往命中存量告警，一旦计入阻断集合通道即不可用。

    Args:
        old_task: 快照（mod-*/spec.json）中的任务定义。
        new_task: master 中的任务定义。

    Returns:
        存在变更且变更字段全部落在 :data:`_TERMINAL_TEXT_REVISABLE_FIELDS`。
    """
    changed = {
        key
        for key in set(old_task) | set(new_task)
        if old_task.get(key) != new_task.get(key)
    }
    return bool(changed) and changed <= _TERMINAL_TEXT_REVISABLE_FIELDS
