"""claim 域 —— 认领主入口编排（run）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`claim`：认领任务主入口（锁内 check-then-act）。按角色分派 implementer /
  reviewer 流程，串联 precheck → execute（写事件 + worktree 就绪）→ review_face，
  并组装响应（files_to_read / scope_warning / pending_conflicts / 各类降级留痕）。

依赖：precheck / execute / review_face / scope 四域（DAG 汇聚层，不被同包其它域导入）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.constants import CLAIMED_BY_PENDING
from orchd.gitops import release_session_lock_if_owned
from orchd.ledger import Store
from orchd.line_ctx import resolve_task_branch_for
from orchd.pool import detect_file_conflict
from orchd.review import extract_review_comments as _extract_review_comments

from .execute import _claim_setup_worktree, _claim_write_event
from .precheck import (
    _check_master_uncommitted,
    _claim_precheck,
    _declaration_path_warnings,
)
from .review_face import _claim_review_branch
from .scope import (
    _extract_previous_changes,
    _is_high_risk,
    build_scope_warning,
)


def claim(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str | None = None,
    project_root: Path | None = None,
    shared: dict[str, Any] | None = None,
    review_type: str | None = None,
    with_context: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    (task_def, role, session_id, degraded_guards, auto_checkout, trunk_restore,
     verify_preview) = _claim_precheck(
         store, tasks, agent_id, task_id, role, project_root, review_type,
         force=force)
    event, state, derived, integrity_warnings, is_self_review = _claim_write_event(
        store, tasks, agent_id, task_id, task_def, role, session_id, review_type,
        project_root, force=force)
    worktree_path = None
    degraded_warning = None
    if role == "implementer" and project_root:
        worktree_path, degraded_warning = _claim_setup_worktree(
            project_root, task_id, task_def, store, degraded_guards)
    _review_ts = store.replay().get(task_id)
    review_phase = _review_ts.review_phase if _review_ts else None
    if role == "reviewer":
        rb = _claim_review_branch(
            store, task_id, task_def, project_root, role, derived, review_phase,
            is_self_review, event, degraded_guards, shared)
        if rb is not None:
            # task-engine-defect-fix-unified-u7：claim 响应统一带 verify 预演
            # （审查认领亦回显，便于审查者核对 verify 目标是否就绪）。
            rb.setdefault("verify_preview", verify_preview)
            if auto_checkout is not None:
                # task-review-auto-checkout：reviewer 分支提前返回前挂载，
                # 与 checked_out_main 往返对称。
                rb["checked_out_task_branch"] = auto_checkout.get("checked_out")
            return rb
    files_to_read = list(task_def.get("files_to_read", []))
    if shared:
        if with_context:
            for key in ("architecture", "conventions"):
                path = shared.get(key)
                if path:
                    files_to_read.append({"path": path, "priority": "reference", "hint": "shared"})
        elif _is_high_risk(task_def):
            conv = shared.get("conventions")
            if conv:
                files_to_read.append({"path": conv, "priority": "reference", "hint": "high risk"})
    previous_changes = _extract_previous_changes(store, task_id, derived)
    pending_conflicts = [
        {"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by}
        for c in detect_file_conflict(state, tasks, task_def, include_pending=True)
        if c.claimed_by == CLAIMED_BY_PENDING
    ]
    # task-amend-scope-add：claim 连带文件预警（与 claim_preview 共用 build_scope_warning 单一来源）
    scope_warning = build_scope_warning(task_def, project_root=project_root)
    # task-engine-defect-fix-unified-u1：声明路径的磁盘事实预警（path_state 单一
    # 来源产出；ghost 带 amend 撤回出路，tracked-deleted 提示删除目标）。
    declaration_warnings = _declaration_path_warnings(task_def, project_root)
    result = {
        "claimed": True, "task": task_def,
        "files_to_read": files_to_read,
        "files_to_edit": task_def.get("files_to_edit", []),
        "review_comments": _extract_review_comments(store, task_id, derived),
        "previous_changes": previous_changes,
        "branch": resolve_task_branch_for(project_root, task_id),
        "pending_conflicts": pending_conflicts,
        "event_id": event["event_id"],
    }
    if scope_warning:
        result["scope_warning"] = scope_warning
    if declaration_warnings:
        result["declaration_warnings"] = declaration_warnings
    # task-engine-defect-fix-unified-u7 AC2：claim 响应新增 verify_preview
    # （classification / hint；expected_pending 提示「先创建 verify 目标文件」）。
    result["verify_preview"] = verify_preview
    if role == "implementer" and worktree_path is not None:
        result["worktree_path"] = worktree_path
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if degraded_warning:
        result["degraded_warning"] = degraded_warning
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    if auto_checkout is not None:
        # task-review-auto-checkout：引擎自动切分支与 checked_out_main 往返对称，
        # 认领响应显式挂载（账本另有 reason=auto_branch_prepare 的 AMEND 事件）。
        result["checked_out_task_branch"] = auto_checkout.get("checked_out")
    if trunk_restore is not None:
        # task-pass9-lifecycle-release-invariant：滞留自愈透出（与 checked_out_task_branch
        # / checked_out_main 往返对称）。
        result["checked_out_trunk"] = trunk_restore.get("checked_out_trunk")
    if role == "implementer" and project_root:
        from orchd.worktree import detect_layout
        layout = detect_layout(project_root)
        if layout.get("layout") == "container":
            result["session_lock_released"] = release_session_lock_if_owned(
                project_root / ".orchd", agent_id).get("released", False)
    # task-engine-cli-friction-fix：检测主工作树 _master.json 未提交，提示 orchd intake
    master_warning = _check_master_uncommitted(project_root)
    if master_warning is not None:
        result["master_uncommitted_warning"] = master_warning
    return result
