"""lifecycle core finish 域 — 锁内落账与 done 收尾组装。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

``_write_done_event`` 锁内 TOCTOU 二次校验 + DONE/REVIEW_READY 落账；
``_done_lessons_hook`` 经验回灌；``_assemble_done_result`` 组装响应。
"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import (
    EVENT_DONE,
    EVENT_REVIEW_READY,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops_ops import make_event as _make_event
from orchd.guide import NEXT_ACTION_AWAIT_REVIEW
from orchd.ledger import Store
from orchd.contracts.task_schema import (
    FILES_TO_EDIT_FIELD,
)


def _write_done_event(
    store: Store,
    task_id: str,
    task_def: dict[str, Any],
    agent_id: str,
    changes_description: str,
    concerns: str | None,
    verify_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """S-A2 阶段 6：锁内 TOCTOU 二次校验 + 写 DONE/REVIEW_READY + checkpoint。

    DONE 事件归属口径（task-cont-done-lineage-docs）：``agent_id`` 记任务认领者
    ``claimed_by``（归属主体，宿主会话身份漂移时仍按认领者记账），``session_id``
    记当前执行 done 的调用方会话（``make_event`` 从环境身份注入）。审查 lineage
    硬门（``orchd/ledger/session.py::require_review_lineage``）即以「该任务最后
    一次 DONE 事件的 ``session_id``」作为 expected parent——等价于：审查会话必须
    由执行 done 的那个实现会话 spawn。
    返回组装所需字段，不在本函数拼完整响应。
    """
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        ts = state.get(task_id)
        if not ts or ts.status != "claimed":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: TOCTOU - task state changed during verify",
                [{"task_id": task_id}],
            )

        claimed_by = ts.claimed_by
        assert claimed_by is not None
        author_mismatch = claimed_by != agent_id
        attempt_count = ts.attempt_count + 1
        # DONE 归属（task-cont-done-lineage-docs）：agent_id=claimed_by（认领者），
        # session_id 由 make_event 注入当前调用方会话——审查 lineage 硬门取的
        # expected parent 就是这里的 session_id。
        done_event = _make_event(
            task_id, claimed_by, EVENT_DONE,
            changes_description=changes_description,
            attempt_count=attempt_count,
        )
        if concerns:
            done_event["concerns"] = concerns
        if verify_record is not None:
            done_event["verify"] = verify_record
        store.append_event(done_event)

        # 自动 REVIEW_READY（review-unify-r2）
        # task-engine-defect-fix-unified-u13：review 路径由单一真源
        # ``resolve_review_created`` 推导（type/mode/reason），本函数只消费其
        # type 决定事件字段；延迟导入避免循环依赖（与历史一致）。
        from orchd.onboard import resolve_review_created

        review_created = resolve_review_created(store, task_def, task_id)
        if review_created["mode"] == "unified":
            review_type: str | None = None
            review_event = _make_event(task_id, claimed_by, EVENT_REVIEW_READY)
        else:
            review_type = review_created["type"]
            review_event = _make_event(
                task_id, claimed_by, EVENT_REVIEW_READY, review_type=review_type
            )
        store.append_event(review_event)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    return {
        "claimed_by": claimed_by,
        "author_mismatch": author_mismatch,
        "attempt_count": attempt_count,
        "done_event": done_event,
        "review_type": review_type,
        "review_created": review_created,
        "integrity_warnings": integrity_warnings,
    }


def _done_lessons_hook(
    store: Store,
    task_id: str,
    verify_record: dict[str, Any] | None,
    skip_lesson_review: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """S-A2 阶段 7：经验回灌 done 收尾 hook（设计 §8.6）。

    检测本任务 staged 暂存建议 → resolved 交叉验证 + detected_at 打点；硬门禁
    （require_review=true）下注入 next_action=await_review（收尾挂起待审）。
    --skip-lesson-review 或 lessons 关闭 → 跳过，暂存保留不丢失。
    best-effort：hook 任何异常绝不阻断 done 收尾。
    """
    if skip_lesson_review:
        return None, False
    lessons_summary: dict[str, Any] | None = None
    lessons_require_review = False
    try:
        from orchd.lessons import (
            is_lessons_enabled,
            load_lessons_config,
            run_done_lesson_hook,
        )
        if not is_lessons_enabled(store.orchd_dir):
            return None, False
        verify_passed = verify_record.get("ok") if verify_record else None
        hook_out = run_done_lesson_hook(store.orchd_dir, task_id, verify_passed)
        if hook_out.get("has_lessons"):
            lessons_summary = hook_out
            lessons_require_review = bool(
                load_lessons_config(store.orchd_dir).get("require_review", True)
            )
    except Exception:
        pass
    return lessons_summary, lessons_require_review


def _assemble_done_result(
    *,
    task_id: str,
    attempt_count: int,
    review_type: str | None,
    done_event_id: str,
    review_created: dict[str, str] | None = None,
    integrity_warnings: list[Any],
    degraded_guards: list[dict[str, Any]],
    author_mismatch: bool,
    claimed_by: str,
    caller_fingerprint: str,
    full_regression: dict[str, Any] | None,
    commit_result: dict[str, Any] | None,
    checked_out_main: dict[str, Any] | None,
    lessons_summary: dict[str, Any] | None,
    lessons_require_review: bool,
) -> dict[str, Any]:
    """S-A2 阶段 8：组装 done 完整响应（含降级留痕/author_mismatch/附带字段）。"""
    result: dict[str, Any] = {
        "done": True,
        "task_id": task_id,
        "status": "done",
        "attempt_count": attempt_count,
        "review_created": review_created or {"type": review_type or "unified"},
        "event_id": done_event_id,
    }
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    if author_mismatch:
        result["author_mismatch"] = {
            "claimed_by": claimed_by,
            "caller_fingerprint": caller_fingerprint,
            "message": (
                "done 已按任务认领者 claimed_by 记账；caller 指纹与认领者不一致"
                "（宿主会话身份可能漂移），仅提示、不阻断"
            ),
        }
    if full_regression is not None:
        result["full_regression"] = full_regression
    if commit_result is not None:
        result["commit"] = commit_result
    if checked_out_main is not None:
        result["checked_out_main"] = checked_out_main
    if lessons_summary is not None:
        result["lessons"] = lessons_summary
        if lessons_require_review:
            result["next_action"] = NEXT_ACTION_AWAIT_REVIEW
            result["lesson_review_hint"] = (
                f"本任务有 {lessons_summary.get('count', 0)} 条 guidance 增补建议待审核，"
                f"收尾挂起：请运行 `orchd lesson review --task {task_id}` 确认后完成收尾"
            )
    return result
