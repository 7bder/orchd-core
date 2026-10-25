"""``orchd.ledger`` Store 域的类型、常量与派生辅助（file-split-program W1-L8，2026-10-16）。

自 ``orchd/ledger/store.py`` 切出：``_CHECKPOINT_SCHEMA_VERSION`` / ``TaskState`` /
``_REVIEW_STALE_DEFAULT_S`` / ``review_stale_timeout_s`` / ``review_claim_age_s`` /
``stale_review_claims`` / ``generate_event_id`` / ``TaskDerived``。切出动机：四段 mixin 模块
（``store_read`` / ``store_checks`` / ``store_write`` / ``store_apply``）都要用这些类型与常量，
若它们留在 ``store.py`` 而 ``store.py`` 又要导入四个 mixin 作基类，就会形成循环导入；集中到本
模块后依赖单向：``store.py`` 与四个 mixin 都 → 本模块，无环。

依赖方向：标准库 + ``orchd.contracts.events``（任务状态名真源）+ ``orchd.errors``。
"""
from __future__ import annotations

import os
import uuid

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orchd.constants import ORCHD_REVIEW_STALE_SECS_ENV
from orchd.contracts.events import STATUS_IN_REVIEW, STATUS_PENDING

# checkpoint 字段 schema 版本（P2-10 / ROADMAP 1.4.1 引擎性能）：
# update_checkpoint 稳态下用增量 state 写快照（O(tail)）；仅当 checkpoint 的
# schema_version 落后于本常量（新字段引入/升级）才 replay_full() 自愈一次。
# 之后新增 TaskState 字段时递增本常量即可触发一次全量重建（字段漂移自愈）。
# v2（2026-08-27）：review_claimed_session 引入（e7e70a8）时漏 bump，既有
# checkpoint 缺该字段且自愈永不触发 → E030 持续告警；bump 至 2 触发一次自愈。
# v3（2026-08-28，W-2）：新增 review_claimed_at（僵尸审查认领判定）后 bump，
# 触发一次 replay_full 自愈，避免旧 checkpoint 缺该字段自我传播 → E030。
# v4（2026-09-15，停服升级）：新增 review_self_review——自审事实落账到 REVIEW_CLAIMED /
# REVIEW_SUBMITTED 事件的 is_self_review 字段与派生状态，事后可直接回查，不再依赖
# DONE.agent_id == REVIEW_CLAIMED.agent_id 的启发式推导。属 conventions.md「安全边界」
# 第 2 条（事件格式与 _apply_event 语义）改动，按约定人工停服升级、不走自托管任务管线。
# 同样 bump 触发一次 replay_full 自愈，避免旧 checkpoint 缺该字段。
_CHECKPOINT_SCHEMA_VERSION = 4

@dataclass
class TaskState:
    """单个任务的当前状态（由事件流 replay 得到的派生状态）。

    任务生命周期的 6 种状态：
        - ``pending``      : 等待被认领，可被任意 agent 领取。
        - ``claimed``      : 已被某 agent 认领，正在执行中。
        - ``done``         : agent 提交完成，等待进入审核流程。
        - ``in_review``    : 正在接受审核（spec review 或 code review）。
        - ``completed``    : code review 通过，任务彻底完成。
        - ``cancelled``    : 被强制取消，不再参与调度。

    字段说明：
        status:             当前状态字符串，对应上述 6 种之一。
        claimed_by:         认领该任务的 agent ID（仅 ``claimed`` 状态有值）。
        attempt_count:      累计尝试次数，每次 DONE 事件递增；FORCE_STATUS(pending) 时重置为 0。
        review_phase:       当前审核阶段类型（如 ``"spec"`` 或 ``"code"``），无审核时为 None。
        review_claimed_by:  认领该审核的 reviewer agent ID，未认领时为 None。
        review_claimed_at:  审查认领发生的 ISO 时间戳（源自 REVIEW_CLAIMED 事件的
                            timestamp）。用于僵尸审查认领判定（W-2）：in_review 且
                            认领超时未见提交 → 可接管。未认领/已提交时为 None。
        review_self_review: 本次审查是否为自审（实现者与审查者同一身份），源自
                            REVIEW_CLAIMED / REVIEW_SUBMITTED 事件的 ``is_self_review``
                            字段（v4，2026-09-15 停服升级）。把「自审」事实落账到事件与
                            派生状态后，事后审计直接读账本即可判定，不再依赖
                            ``DONE.agent_id == REVIEW_CLAIMED.agent_id`` 的启发式推导。
                            非自审时为 False（快照省略）。
        merge_warning:      代码审查通过后 git merge 未执行（环境异常/best-effort 降级），
                            标记完成但 merge 未落地，audit-merge 需告警。仅 completed 有值。
    """

    status: str = STATUS_PENDING
    claimed_by: str | None = None
    claimed_session: str | None = None
    attempt_count: int = 0
    review_phase: str | None = None
    review_claimed_by: str | None = None
    review_claimed_session: str | None = None
    review_claimed_at: str | None = None
    review_self_review: bool = False
    merge_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将任务状态序列化为字典，用于写入 checkpoint JSON。

        为保持 checkpoint 紧凑，值为 ``None`` 的可选字段（``claimed_by``、
        ``review_phase``、``review_claimed_by``、``merge_warning``）会被省略；
        ``review_self_review``（bool）仅在为 ``True`` 时写入。
        始终包含 ``status`` 和 ``attempt_count``。
        """
        d: dict[str, Any] = {"status": self.status, "attempt_count": self.attempt_count}
        if self.claimed_by:
            d["claimed_by"] = self.claimed_by
        if self.claimed_session:
            d["claimed_session"] = self.claimed_session
        if self.review_phase:
            d["review_phase"] = self.review_phase
        if self.review_claimed_by:
            d["review_claimed_by"] = self.review_claimed_by
        if self.review_claimed_session:
            d["review_claimed_session"] = self.review_claimed_session
        if self.review_claimed_at:
            d["review_claimed_at"] = self.review_claimed_at
        if self.review_self_review:
            d["review_self_review"] = True
        if self.merge_warning:
            d["merge_warning"] = self.merge_warning
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskState:
        """从 checkpoint 字典反序列化为 TaskState 实例。

        与 :meth:`to_dict` 互为逆操作；对缺失的可选字段使用安全默认值
        （``status`` 默认 ``"pending"``，其余可选字段默认 ``None``）。
        """
        return cls(
            status=d.get("status", STATUS_PENDING),
            claimed_by=d.get("claimed_by"),
            claimed_session=d.get("claimed_session"),
            attempt_count=d.get("attempt_count", 0),
            review_phase=d.get("review_phase"),
            review_claimed_by=d.get("review_claimed_by"),
            review_claimed_session=d.get("review_claimed_session"),
            review_claimed_at=d.get("review_claimed_at"),
            review_self_review=bool(d.get("review_self_review", False)),
            merge_warning=d.get("merge_warning"),
        )

# 僵尸审查认领（W-2）：审查认领超过该时长且未见提交，即由 request / status /
# doctor 浮出、可供接管。默认 10 分钟（复用本次实测校准值）。
_REVIEW_STALE_DEFAULT_S = 300

def review_stale_timeout_s() -> float:
    """返回审查认领超时秒数；环境变量 ``ORCHD_REVIEW_STALE_SECS`` 可覆盖（测试用）。"""
    env = os.environ.get(ORCHD_REVIEW_STALE_SECS_ENV)
    if env is not None:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    return _REVIEW_STALE_DEFAULT_S

def review_claim_age_s(claimed_at: str | None, now: str | None = None) -> float | None:
    """审查认领距今秒数。时间戳缺失/不可解析 → None（不判 stale，避免误伤）。"""
    if not claimed_at:
        return None
    try:
        t = datetime.fromisoformat(claimed_at)
        base = datetime.fromisoformat(now) if now else datetime.now(t.tzinfo)
        return (base - t).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None

def stale_review_claims(
    state: dict[str, TaskState],
    timeout_s: float | None = None,
    now: str | None = None,
) -> dict[str, dict[str, Any]]:
    """从派生状态找出「僵尸审查认领」：in_review 且有认领、认领超时未见提交。

    仅依据派生状态（status / review_claimed_by / review_claimed_at）判定，不加
    ledger 扫描、不引入独立标志位（与 replay 同源，判定即派生）。``timeout_s`` /
    ``now`` 缺省取常量/环境覆盖与当前时刻，便于测试注入。

    返回 ``{task_id: {claimed_by, claimed_session, review_phase, age_s, timeout_s}}``。
    """
    tmo = review_stale_timeout_s() if timeout_s is None else timeout_s
    stale: dict[str, dict[str, Any]] = {}
    for tid, ts in state.items():
        if not (ts.status == STATUS_IN_REVIEW and ts.review_claimed_by
                and ts.review_claimed_at):
            continue
        age = review_claim_age_s(ts.review_claimed_at, now)
        if age is not None and age >= tmo:
            stale[tid] = {
                "claimed_by": ts.review_claimed_by,
                "claimed_session": ts.review_claimed_session,
                "review_phase": ts.review_phase or "spec",
                "age_s": round(age),
                "timeout_s": tmo,
            }
    return stale

def generate_event_id() -> str:
    """生成事件 ID：``evt-{uuid4-hex-16}``（64 bit 熵，L-9）。

    历史形态为 ``evt-{8hex}``（仅 32 bit）：当前账本规模下生日碰撞概率约
    0.12%、万级约 1%，碰撞事件会被 ``orchd sync`` 的 event_id 去重**静默丢弃**，
    或被 RETRACT 误撤他人事件。加宽到 64 bit 后同规模碰撞概率可忽略。

    兼容性：旧 8hex 事件原样读取，去重与引用仍按字符串相等（不重写历史）；
    新旧混存安全，但**跨设备需同时升级**才能实质消除碰撞。
    """
    return f"evt-{uuid.uuid4().hex[:16]}"

@dataclass
class TaskDerived:
    """从 ledger 单次扫描得到的 per-task 派生信息（H2，2026-08-13 性能审核）。

    供 request / claim / done / review_submit 复用，消除「每个查询都从头
    全扫 ledger」的重复 O(L) 扫描（原实现单命令内可能触发 2-4 次全扫）。
    三类信息均与 ``_extract_*`` 辅助函数的语义一一对应：
    - ``last_done``      : task_id → 最近 DONE 事件 dict（正序遍历后者覆盖）。
    - ``review_comments``: task_id → 全部 REVIEW_SUBMITTED 的 comments（正序）。
    - ``review_baselines``: (task_id, agent_id) → 最近 REVIEW_CLAIMED 的
      ``baseline_sha``（正序遍历后者覆盖 = 最近一次）。

    与 replay 同源（同一份 ledger、同一 :meth:`Store._read_ledger_lines`
    容错语义：末行损坏跳过、中间行损坏降级跳过 + E030 warning），保证
    「读到的辅助信息」与「派生状态」一致。
    """

    last_done: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_comments: dict[str, list[str]] = field(default_factory=dict)
    review_baselines: dict[tuple[str, str], str] = field(default_factory=dict)
