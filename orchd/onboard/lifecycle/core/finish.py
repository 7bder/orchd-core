"""lifecycle core finish 域 — 锁内落账与 done 收尾组装。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

``_write_done_event`` 锁内 TOCTOU 二次校验 + DONE/REVIEW_READY 落账；
``_done_lessons_hook`` 经验回灌；``_assemble_done_result`` 组装响应。
"""

from __future__ import annotations

from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops_ops import make_event as _make_event
from orchd.guide import NEXT_ACTION_AWAIT_REVIEW
from orchd.ledger import Store, resolve_review_mode


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

    记账一律以任务认领者 claimed_by 为准，忽略 caller 当前指纹（宿主会话身份
    漂移时仍正常放行并按认领者记账）。返回组装所需字段，不在本函数拼完整响应。
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
        done_event = _make_event(
            task_id, claimed_by, "DONE",
            changes_description=changes_description,
            attempt_count=attempt_count,
        )
        if concerns:
            done_event["concerns"] = concerns
        if verify_record is not None:
            done_event["verify"] = verify_record
        store.append_event(done_event)

        # 自动 REVIEW_READY（review-unify-r2）
        if resolve_review_mode(store.orchd_dir) == "unified":
            review_type: str | None = None
            review_event = _make_event(task_id, claimed_by, "REVIEW_READY")
        else:
            # 延迟导入避免循环依赖
            from orchd.onboard import _load_config_blocked, _is_doc_single_stage
            blocked_config = _load_config_blocked(store)
            review_type = "code" if _is_doc_single_stage(
                task_def.get("files_to_edit", []), blocked=blocked_config
            ) else "spec"
            # task-review-rework-scope：code 范围打回的返工直达 code 审查，
            # 跳过 spec（缺省/历史事件无标记即 spec，与旧行为一致；标记随
            # 本次 done 消费隐式失效，见 ledger.latest_rework_scope）。
            if review_type == "spec":
                from orchd.ledger import latest_rework_scope

                if latest_rework_scope(store, task_id) == "code":
                    review_type = "code"
            review_event = _make_event(task_id, claimed_by, "REVIEW_READY", review_type=review_type)
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
        "review_created": {"type": review_type or "unified"},
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
