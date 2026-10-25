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

# T7：8 类事件类型名单一真源（CONTRACTS / TRANSITION_TABLE 的键均由此派生；
# Wave2 再把各事件构造点的字面量替换为本模块常量）。
EVENT_CLAIMED = "CLAIMED"
EVENT_DONE = "DONE"
EVENT_REVIEW_READY = "REVIEW_READY"
EVENT_REVIEW_CLAIMED = "REVIEW_CLAIMED"
EVENT_REVIEW_SUBMITTED = "REVIEW_SUBMITTED"
EVENT_FORCE_STATUS = "FORCE_STATUS"
EVENT_RETRACT = "RETRACT"
EVENT_AMEND = "AMEND"

# T1：任务状态名（六态）的**唯一字面量来源**。生产消费方一律引用这些常量；
# 下方 TASK_STATUSES / TERMINAL_STATUSES / FORCE_TARGET_STATUSES 均由此派生，
# **禁止另立第二份状态字面量**。常量值逐字等于账本 / schema 的既有状态名。
STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_IN_REVIEW = "in_review"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

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
    EVENT_CLAIMED: {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({"role", "files_claimed"}),
    },
    EVENT_DONE: {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({
            "changes_description", "attempt_count", "concerns", "verify",
        }),
    },
    EVENT_REVIEW_READY: {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({"review_type"}),
    },
    EVENT_REVIEW_CLAIMED: {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({
            "baseline_sha", "is_self_review", "review_type",
        }),
    },
    EVENT_REVIEW_SUBMITTED: {
        "version": 1,
        "required": frozenset({"verdict"}),
        "optional": frozenset({
            "review_type", "is_self_review", "merge_warning",
            "comments", "rework_scope", "merge_pending",
        }),
    },
    EVENT_FORCE_STATUS: {
        "version": 1,
        "required": frozenset(),
        "optional": frozenset({
            "target_status", "reason", "assignee", "evidence_sha",
            "test_data",
        }),
    },
    EVENT_RETRACT: {
        "version": 1,
        "required": frozenset({"target_event_id"}),
        "optional": frozenset({"reason", "disposition"}),
    },
    EVENT_AMEND: {
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
    if normalized.get("type") in (EVENT_REVIEW_CLAIMED, EVENT_REVIEW_SUBMITTED):
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
    EVENT_CLAIMED: {
        "allowed_from": frozenset({STATUS_PENDING}),
        "target": STATUS_CLAIMED,
        "gated": True,
    },
    EVENT_DONE: {
        "allowed_from": frozenset({STATUS_CLAIMED}),
        "target": STATUS_DONE,
        "gated": True,
    },
    EVENT_REVIEW_READY: {
        "allowed_from": frozenset({STATUS_DONE, STATUS_IN_REVIEW}),
        "target": STATUS_IN_REVIEW,
        "gated": True,
    },
    EVENT_REVIEW_CLAIMED: {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
    EVENT_REVIEW_SUBMITTED: {
        "allowed_from": frozenset({STATUS_IN_REVIEW}),
        "target": None,
        "gated": True,
    },
    EVENT_FORCE_STATUS: {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
    EVENT_RETRACT: {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
    EVENT_AMEND: {
        "allowed_from": frozenset(),
        "target": None,
        "gated": False,
    },
}

# T7：事件类型全集（与 CONTRACTS / TRANSITION_TABLE 键集同源，测试对拍锁定）
EVENT_TYPES = frozenset(CONTRACTS)

# REVIEW_SUBMITTED verdict 分支映射（目标状态的条件部分；
# CHANGES_REQUESTED 不看 review_type，一律 pending）。
REVIEW_SUBMITTED_TARGETS = {
    ("APPROVED", "code"): STATUS_COMPLETED,
    ("APPROVED", None): STATUS_COMPLETED,
    ("APPROVED", "spec"): STATUS_IN_REVIEW,
}

# 任务状态词表（六态）——派生层的**单一真源**：``ledger._apply_event`` 的
# FORCE_STATUS 分支只接受这六个值，白矩阵外的字符串不得静默写进运行态。
# 集合元素由上方 STATUS_* 常量派生，不写字面量。
TASK_STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
})

# 终态词表（completed / cancelled）——**单一真源**：原散落多处硬编码
# （init / workflow / guidance / doctor / ideas / scripts.check_release_ready），
# task-terminal-single-source 逐批收敛到此处。终态 = 不再入池、不再变更的状态
# （amend 对终态拒绝；``--revise-terminal`` 仅放行纯文本字段）。
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_CANCELLED})

# 人工 force-status 的合法目标（``onboard.control._FORCE_TARGETS`` 白名单）——
# TASK_STATUSES 的真子集：done / in_review 由正常事件流（DONE / REVIEW_READY）
# 驱动，不开放给人工强制跳转。
# **两份枚举不可合并**：引擎内部路径只受 TASK_STATUSES 约束，不受人工白名单约束
# ——``review.py:1232`` 的终审残留回退就写 ``target_status="in_review"``（绕开 CLI
# 的 _FORCE_TARGETS，直接经 make_event 落账）。派生层若按人工四态校验，会把这个
# 引擎自写事件静默吞掉（task-sync-inbound-force-gate 实现期全量回归实测到）。
# 共用点：onboard.control（人工写路径白名单）/ ledger replay（派生态守卫）/
# ledger_sync 入站整类拒收（见 ``_INBOUND_LOCAL_ONLY_TYPES``）。
FORCE_TARGET_STATUSES = frozenset({
    STATUS_PENDING,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_CANCELLED,
})


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
