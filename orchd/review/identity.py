"""自审身份判定（is_self_review_author），叶域。"""

from __future__ import annotations

from typing import Any


def is_self_review_author(
    done_event: dict[str, Any] | None,
    agent_id: str,
    session_id: str | None,
) -> bool:
    """判定「本次审查/认领是否为自审」：实现者（DONE 作者）与审查者是否同一身份。

    判定口径为全仓单一事实源（v4，2026-09-15 停服升级时，由 claim 与
    select_review_candidate 的历史私有副本收敛于此，三段判定完全等价）：

    - DONE 事件与当前 ``session_id`` 均已知 → 要求 session 与 agent 同时匹配；
    - 否则退化为仅比较 ``agent_id``（旧数据 / 无 session 环境）。

    自审事实的落账由调用方完成：REVIEW_CLAIMED / REVIEW_SUBMITTED 事件写入
    ``is_self_review`` 字段（派生为 TaskState.review_self_review），使事后审计
    直接读账本即可认定，不再依赖 ``DONE.agent_id == REVIEW_CLAIMED.agent_id``
    的启发式推导（该推导在 session 维度不可靠）。
    """
    if not done_event:
        return False
    done_author = done_event.get("agent_id")
    done_session = done_event.get("session_id")
    if not done_author:
        return False
    if done_session and session_id:
        return done_session == session_id and done_author == agent_id
    return done_author == agent_id
