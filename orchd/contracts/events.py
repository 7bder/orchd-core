"""事件契约表（W2-①）。每类事件的字段契约（required/optional/版本）。

已接入：`orchd/ledger.py` 经 `normalize_event` / `validate_event` 做 append 门禁，
`TRANSITION_TABLE` / `REVIEW_SUBMITTED_TARGETS` 驱动 `_apply_event` 与文档生成。
字段清单取自各事件构造点（claim/done/review/split/control）实际写入项；
``_apply_event`` 实际读取项均为 required 或带缺省的 optional。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

EVENT_VERSION = 1

BASE_REQUIRED = frozenset({
    "v",
    "event_id",
    "timestamp",
    "task_id",
    "agent_id",
    "type",
})

BASE_OPTIONAL = frozenset({
    # session_id：会话身份引入后的事件才携带；史前事件缺失属合法 v1 形态
    #（2026-09 全账本扫描实证：除 session_id 外无其他缺口）。
    "session_id",
})

CONTRACTS: dict[str, dict[str, Any]] = {
    "CLAIMED": {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({"role", "files_claimed"}),
    },
    "DONE": {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({
            "changes_description", "attempt_count", "concerns", "verify",
        }),
    },
    "REVIEW_READY": {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({"review_type"}),
    },
    "REVIEW_CLAIMED": {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({
            "baseline_sha", "is_self_review", "review_type",
        }),
    },
    "REVIEW_SUBMITTED": {
        "version": 1,
        "required": frozenset({"verdict"}),
        "optional": frozenset({
            "review_type", "is_self_review", "merge_warning",
            "comments", "rework_scope",
        }),
    },
    "FORCE_STATUS": {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({
            "target_status", "reason", "assignee", "evidence_sha",
            "test_data",
        }),
    },
    "RETRACT": {
        "version": 1,
        "required": frozenset({"target_event_id"}),
        "optional": frozenset({"reason", "disposition"}),
    },
    "AMEND": {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({"reason", "fields", "rationale", "hint"}),
    },
}


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """v1 兼容归一化：只补文档化缺省，不改已存语义，不重写账本。

    - 缺 ``v`` → 1（make_event 历史版本恒为 1）。
    - REVIEW_CLAIMED / REVIEW_SUBMITTED 缺 ``is_self_review`` → False
     （v4 口径：缺失视为非自审，``_apply_event`` 同语义）。
    - 缺 ``event_id`` / ``timestamp`` → 补占位值（测试手写 legacy 事件与
      史前形态；仅用于校验，落盘仍用生产者原字典，不伪造身份）。
    """
    normalized = dict(event)
    normalized.setdefault("v", EVENT_VERSION)
    normalized.setdefault("event_id", f"evt-normalized-{uuid.uuid4().hex[:12]}")
    normalized.setdefault(
        "timestamp", datetime.now(timezone.utc).isoformat(timespec="microseconds")
    )
    if normalized.get("type") in ("REVIEW_CLAIMED", "REVIEW_SUBMITTED"):
        normalized.setdefault("is_self_review", False)
    return normalized


# 状态转移表（W2-③）。同一张表驱动三处，禁止分叉：
# 1. ``ledger._event_target_status`` 的目标推导；
# 2. ``ledger.validate_transition`` 的来源校验（经下方派生别名）；
# 3. ``docs/_generated/statemachine.md`` 的生成。
#
# ``target`` 为 None = 条件分支（REVIEW_SUBMITTED 看 verdict）或
# 无状态变更（REVIEW_CLAIMED / FORCE_STATUS 自行处理 / RETRACT / AMEND）。
TRANSITION_TABLE: dict[str, dict[str, Any]] = {
    "CLAIMED": {
        "allowed_from": frozenset({"pending"}),
        "target": "claimed",
        "gated": True,
    },
    "DONE": {
        "allowed_from": frozenset({"claimed"}),
        "target": "done",
        "gated": True,
    },
    "REVIEW_READY": {
        "allowed_from": frozenset({"done", "in_review"}),
        "target": "in_review",
        "gated": True,
    },
    "REVIEW_CLAIMED": {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
    "REVIEW_SUBMITTED": {
        "allowed_from": frozenset({"in_review"}),
        "target": None,
        "gated": True,
    },
    "FORCE_STATUS": {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
    "RETRACT": {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
    "AMEND": {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
}

# REVIEW_SUBMITTED verdict 分支映射（目标状态的条件部分；
# CHANGES_REQUESTED 不看 review_type，一律 pending）。
REVIEW_SUBMITTED_TARGETS = {
    ("APPROVED", "code"): "completed",
    ("APPROVED", None): "completed",
    ("APPROVED", "spec"): "in_review",
}

# 任务状态词表（六态）——派生层的**单一真源**：``ledger._apply_event`` 的
# FORCE_STATUS 分支只接受这六个值，白矩阵外的字符串不得静默写进运行态。
TASK_STATUSES = frozenset(
    {"pending", "claimed", "done", "in_review", "completed", "cancelled"})

# 终态词表（completed / cancelled）——**单一真源**：原散落多处硬编码
# （init / workflow / guidance / doctor / ideas / scripts.check_release_ready），
# task-terminal-single-source 逐批收敛到此处。终态 = 不再入池、不再变更的状态
# （amend 对终态拒绝；``--revise-terminal`` 仅放行纯文本字段）。
TERMINAL_STATUSES = frozenset({"completed", "cancelled"})

# 人工 force-status 的合法目标（``onboard.control._FORCE_TARGETS`` 白名单）——
# TASK_STATUSES 的真子集：done / in_review 由正常事件流（DONE / REVIEW_READY）
# 驱动，不开放给人工强制跳转。
# **两份枚举不可合并**：引擎内部路径只受 TASK_STATUSES 约束，不受人工白名单约束
# ——``review.py:1232`` 的终审残留回退就写 ``target_status="in_review"``（绕开 CLI
# 的 _FORCE_TARGETS，直接经 make_event 落账）。派生层若按人工四态校验，会把这个
# 引擎自写事件静默吞掉（task-sync-inbound-force-gate 实现期全量回归实测到）。
# 共用点：onboard.control（人工写路径白名单）/ ledger replay（派生态守卫）/
# ledger_sync 入站整类拒收（见 ``_INBOUND_LOCAL_ONLY_TYPES``）。
FORCE_TARGET_STATUSES = frozenset({"pending", "claimed", "completed", "cancelled"})


def validate_event(event: dict[str, Any]) -> list[str]:
    """fail-closed 契约校验：返回违规描述列表，空即通过。"""
    errors: list[str] = []
    etype = event.get("type")
    contract = CONTRACTS.get(etype) if isinstance(etype, str) else None
    if contract is None:
        return [f"unknown event type: {etype!r}"]
    for field in sorted(BASE_REQUIRED | contract["required"]):
        if field not in event:
            errors.append(f"{etype} missing required field: {field}")
    if not isinstance(event.get("v"), int):
        errors.append(f"{etype} field 'v' must be int")
    return errors
