"""orchd.ledger.events — 账本状态迁移白矩阵域（task-split2-ledger-l6-events-envelope）。

自 ``orchd/ledger/__init__.py`` 整块搬出（L6）：函数体与常量值逐字保留，聚合入口改为重导出。
依赖方向（单源清单：``.orchd/shared/conventions.md`` 依赖方向节）：顶层仅标准库 +
``orchd.contracts.events``（TRANSITION_TABLE / REVIEW_SUBMITTED_TARGETS 直连真源，不经聚合
入口）+ ``orchd.errors``；:func:`latest_rework_scope` 的 ``Store`` 类型引用收在
``if TYPE_CHECKING:`` 守卫块内（运行期不执行，避免与聚合入口成加载期环）。
"""

from typing import TYPE_CHECKING, Any

from orchd.contracts.events import (
    EVENT_REVIEW_SUBMITTED,
    REVIEW_SUBMITTED_TARGETS,
    STATUS_IN_REVIEW,
    TRANSITION_TABLE,
)
from orchd.errors import ErrorCode, OrchdError

if TYPE_CHECKING:  # 仅类型检查期存在：Store 由聚合入口定义，运行期按鸭子类型传入
    from orchd.ledger import Store

# 跃迁白矩阵（W2-③派生自 contracts.TRANSITION_TABLE 的只读视图，
# 禁止手写分叉；task-audit-ledger-state-machine-dedup 的原始语义保持）：
# 事件类型 → 允许的当前状态集合。仅约束「会改变状态」的事件；目标状态 ==
# 当前状态（幂等 self-transition，如两阶段审查 spec APPROVED 后任务仍
# in_review、REVIEW_READY(code) 再次到来）永远合法。FORCE_STATUS（强制逃生口，
# 支持 cancelled→pending 复活 / claimed→completed）、RETRACT（回滚）、
# REVIEW_CLAIMED（非状态跃迁）不受矩阵约束。
_TRANSITION_WHITELIST: dict[str, frozenset[str]] = {
    etype: entry["allowed_from"]
    for etype, entry in TRANSITION_TABLE.items()
    if entry["gated"] and entry["allowed_from"]
}
# 不受白矩阵约束的事件类型（引擎逃生口 / 回滚 / 非状态跃迁）
_UNGATED_TRANSITIONS = frozenset({
    etype for etype, entry in TRANSITION_TABLE.items() if not entry["gated"]
})


def _event_target_status(event: dict[str, Any]) -> str | None:
    """推导事件将设置的目标状态；不改变状态或不受约束的事件返回 None（跳过校验）。

    W2-③：静态映射读 contracts.TRANSITION_TABLE，REVIEW_SUBMITTED 读
    REVIEW_SUBMITTED_TARGETS；未知类型 / 未知 verdict 返回 None（沿旧语义）。
    """
    etype = event.get("type", "")
    if etype in _UNGATED_TRANSITIONS:
        return None
    entry = TRANSITION_TABLE.get(etype)
    if entry is None:
        return None
    if etype == EVENT_REVIEW_SUBMITTED:
        verdict = event.get("verdict", "")
        if verdict == "CHANGES_REQUESTED":
            return "pending"
        if event.get("merge_pending"):
            # A4（debug-pack A 组）：merge 未落地的审查结论落账后不推进终态，
            # 任务保持 in_review，同一 reviewer 可重试。
            return STATUS_IN_REVIEW
        return REVIEW_SUBMITTED_TARGETS.get((verdict, event.get("review_type")))
    return entry["target"]


def validate_transition(
    event_type: str,
    current_status: str,
    target_status: str,
) -> None:
    """跃迁白矩阵校验（引擎硬约束）：非法状态跃迁抛 E007（含当前/目标状态）。

    - 不受约束的事件（FORCE_STATUS / RETRACT / REVIEW_CLAIMED）直接放行；
    - 目标 == 当前（幂等 self-transition，状态不变）放行；
    - 否则要求 ``current_status`` 属于白矩阵中该事件允许的来源集合。
    未知事件类型保守放行（不阻塞未来事件扩展）。
    """
    if event_type in _UNGATED_TRANSITIONS:
        return
    allowed = _TRANSITION_WHITELIST.get(event_type)
    if allowed is None:
        return
    if target_status == current_status:
        return
    if current_status not in allowed:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_transition: {current_status} --{event_type}--> {target_status} "
            "不在跃迁白矩阵",
            [{
                "event_type": event_type,
                "current_status": current_status,
                "target_status": target_status,
                "hint": f"{event_type} 仅允许从 {sorted(allowed)} 状态跃迁到 "
                        f"{target_status}",
            }],
        )


def latest_rework_scope(store: "Store", task_id: str) -> str | None:
    """最近一次打回的返工范围（task-review-rework-scope）。

    扫描账本：最近一条该任务的 ``REVIEW_SUBMITTED`` 若为 CHANGES_REQUESTED
    且带 ``rework_scope``，返回其值（``spec`` 缺省不写，读不到即 None）；
    其后若有新的 REVIEW_SUBMITTED（任何 verdict），以最新一条为准——标记
    随 done 消费隐式失效，无需显式清除（pending 语义零污染）。
    """
    scope: str | None = None
    try:
        if not store.ledger_exists():
            return None
        events = store._read_ledger_lines(from_line=1)
    except Exception:
        return None
    for ev in events:
        if ev.get("task_id") != task_id or ev.get("type") != EVENT_REVIEW_SUBMITTED:
            continue
        if ev.get("verdict") == "CHANGES_REQUESTED":
            scope = ev.get("rework_scope") or "spec"
        else:
            scope = None
    return scope
