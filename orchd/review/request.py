"""请求审查者（request_reviewer），依赖 identity+history。"""

from __future__ import annotations

from typing import Any

from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD,
    BRIEF_FIELD,
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
    IMPORTANCE_FIELD,
    MODULE_FIELD,
    REVIEWERS_FIELD,
)
from orchd.guide import NEXT_ACTION_EXIT
from orchd.ledger import Store
from orchd.ledger import TaskDerived
from orchd.ledger import TaskState
from orchd.ledger import is_fingerprint_agent_id as _is_fingerprint_agent_id
from .history import extract_last_done, find_last_done_event
from .identity import is_self_review_author


def request_reviewer(
    store: Store,
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    agent_id: str,
    derived: TaskDerived | None = None,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    """查找处于 in_review 且未被审查者 claim 的任务。

    排序规则：spec 阶段的审查优先于 code 阶段。同等阶段内按 ledger 遍历顺序排列。
    调用方（onboard.request）在 reviewer 角色下转发到此函数。

    self-review（实现者 == 审查者）：默认仅标注 ``is_self_review`` 并照常进入
    候选；``enforce_self_review_block=True``（线上版）时排除（AC1）。
    """
    review_candidates: list[dict[str, Any]] = []
    not_in_list: list[dict[str, Any]] = []
    task_map = {t.get(ID_FIELD, ""): t for t in tasks}
    for tid, ts in state.items():
        if ts.status == "in_review" and ts.review_claimed_by is None:
            task_def = task_map.get(tid, {})
            # 向后兼容：reviewers 名单存在且非空时不再名单内 → not_in_list；
            # 字段缺失/为空（指纹身份模型）则跳过名单门禁，仅按实现指纹去重。
            # 指纹豁免（task-fp-review-priority-exempt，对齐 claim 侧 E007）：
            # 指纹形态 agent_id（12 位 hex）不在名单内也正常进入候选（不落入
            # not_in_list）；具名 agent（名单外）仍记 not_in_list（向后兼容）。
            designated = task_def.get(REVIEWERS_FIELD)
            if designated and agent_id not in designated \
                    and not _is_fingerprint_agent_id(agent_id):
                not_in_list.append({
                    "task_id": tid,
                    "review_phase": ts.review_phase or "spec",
                    REVIEWERS_FIELD: task_def.get(REVIEWERS_FIELD, []),
                })
                continue
            # self-review：DONE 实现指纹 == 当前 reviewer 指纹。
            # 默认仅标注照常分配；enforce=True 时排除（AC1）。
            # v4（2026-09-15 停服升级）：判定收敛到 is_self_review_author（单一事实源），
            # 与 claim / review_submit 的落账口径严格一致，消除私有副本漂移。
            from orchd.ledger import resolve_session_identity
            current_session = resolve_session_identity(store.orchd_dir)["session_id"]
            is_self = is_self_review_author(
                find_last_done_event(store, tid, derived), agent_id, current_session
            )
            if is_self and enforce_self_review_block:
                continue
            entry: dict[str, Any] = {
                "task_id": tid,
                "task": task_def,
                "review_phase": ts.review_phase,
            }
            if is_self:
                entry["is_self_review"] = True
            review_candidates.append(entry)
    review_candidates.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    if not review_candidates:
        if not_in_list:
            return {
                "candidate": None,
                "message": f"有 {len(not_in_list)} 个待审查任务但你不在名单内",
                "next_action": NEXT_ACTION_EXIT,
                "pool_size": 0,
                "reason": "not_in_reviewer_list",
                "tasks": not_in_list,
            }
        return {
            "candidate": None,
            "message": "当前无待审查任务",
            "next_action": NEXT_ACTION_EXIT,
            "pool_size": 0,
        }
    best = review_candidates[0]
    task_def = best["task"]
    task_id = best["task_id"]
    author, changes_description = extract_last_done(store, task_id, derived)

    candidate: dict[str, Any] = {
        "task_id": task_id,
        # review-unify-r2：unified 单阶段（review_phase 为 None）展示为 unified。
        "review_type": best["review_phase"] or "unified",
        MODULE_FIELD: task_def.get(MODULE_FIELD, ""),
        BRIEF_FIELD: task_def.get(BRIEF_FIELD, ""),
        IMPORTANCE_FIELD: task_def.get(IMPORTANCE_FIELD, "normal"),
        "files_to_review": [
            {"path": p, "priority": "must_read"}
            for p in task_def.get(FILES_TO_EDIT_FIELD, [])
        ],
        ACCEPTANCE_CRITERIA_FIELD: task_def.get(ACCEPTANCE_CRITERIA_FIELD, []),
        "changes_description": changes_description,
    }
    if author:
        candidate["author"] = author
    if best.get("is_self_review"):
        candidate["is_self_review"] = True

    return {
        "candidate": candidate,
        "pool_size": len(review_candidates),
        "prompt": f"确认将此审查分配给 {agent_id}？(执行 / 跳过 / 重新声明能力)",
        "warnings": [],
    }
