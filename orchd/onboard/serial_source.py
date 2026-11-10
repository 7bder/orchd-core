"""同 source 串行包的忙态兄弟判定（叶模块：claim 与 request 共用单一谓词）。

口径真源：``.orchd/rules/intake.md`` serial-package 节——同 source 多卡逐张推进，
只有兄弟全终态（completed / cancelled）才放行下一张。本模块只做判定，不做剔除与
报错：claim 侧据此抛 E041（唯一强制点），request 侧据此前置剔除候选（减少无效推送，
仍受 TOCTOU 约束）。

依赖方向：只依赖 contracts / ledger 类型，不导入 onboard 其它域。
"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW
from orchd.contracts.task_schema import ID_FIELD, SOURCE_FIELD
from orchd.ledger import TaskState

# 在手集合：pending 兄弟不拦（批量注册骨架常态），终态兄弟放行下一张。
SERIAL_BUSY_STATUSES: frozenset[str] = frozenset(
    {STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW}
)


def serial_busy_siblings(
    task: dict[str, Any],
    tasks: list[dict[str, Any]],
    state: dict[str, TaskState],
) -> list[str]:
    """返回同 source 且处于在手（claimed / done / in_review）的兄弟任务 id。

    无 source 的任务、以及状态记录缺失（视为 pending）的兄弟一律不拦；
    返回值按任务定义顺序，不含自身。
    """
    source = task.get(SOURCE_FIELD)
    if not source:
        return []
    task_id = task.get(ID_FIELD)
    siblings: list[str] = []
    for other in tasks:
        other_id = other.get(ID_FIELD)
        if other_id == task_id or other.get(SOURCE_FIELD) != source:
            continue
        if other_id is None:
            continue
        ts = state.get(str(other_id))
        if ts is not None and ts.status in SERIAL_BUSY_STATUSES:
            siblings.append(str(other_id))
    return siblings
