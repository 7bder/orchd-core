"""report — 完整性与复活/intake 审计域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

任务完整性审计 / 复活标记扫描 / revive 与 intake 审计。

**叶子域**：零包内相对导入。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.task_schema import (
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
)
from orchd.contracts.events import (
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
    TERMINAL_STATUSES,
    EVENT_CLAIMED,
    EVENT_DONE,
    EVENT_FORCE_STATUS,
    EVENT_RETRACT,
    EVENT_REVIEW_SUBMITTED,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root



def task_integrity_audit(
    store: Store,
    tasks: list[dict[str, Any]],
    project_root: Path | None,
    scope: str = "active",
) -> dict[str, Any]:
    """任务完整性只读巡检：发现跨 worktree 脏写 / 分支 diff 缺失声明文件。

    ``scope="active"``（watchdog 附加字段 ``task_integrity``）：扫描活跃任务
    （claimed / done / in_review）的 ``files_to_edit`` 声明文件——
    - ``main_dirty_overlap``：main 工作树存在同名未提交改动（跨 worktree 脏写：
      实现者在任务 worktree 提交后 main 上仍有残留改动，疑似漏提交/错 worktree）；
    - ``missing_declared_files``：任务分支相对 main 的实际 diff 缺失声明文件
      （实现者改了未声明文件却漏改/漏提交声明文件，branch diff 兜底核对）。

    ``scope="merged"``（status --audit-task 附加字段 ``audit_task``）：扫描
    completed（merged）任务的历史缺失/残留——
    - ``main_residual``：main 工作树仍残留该任务声明文件的未提交改动（合并后
      未提交的残留，无论巡检入口在 main 还是任务 worktree 都能发现）；
    - ``historical_missing``：任务分支 diff 缺失声明文件（历史缺失）。

    只读：不修改工作树、不建分支、不提交（对齐 ``merge_audit`` 先例）。
    非 git / git 不可用 / 无 project_root 时返回 ``{"skipped": True, ...}``，
    不抛异常（best-effort）。

    Args:
        store: ledger Store（replay 取任务状态）。
        tasks: _master.json 的 tasks[] 定义列表。
        project_root: 任意 worktree 根（git 命令 cwd；None → skipped）。
        scope: "active"（活跃任务）或 "merged"（completed 任务历史）。

    Returns:
        ``{"skipped": False, "scope": scope, "issues": [...], "summary": str}``
        或 ``{"skipped": True, "reason": "..."}``。
    """
    import subprocess

    if project_root is None:
        return {"skipped": True, "reason": "no_project_root"}
    root = Path(project_root).resolve()
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
        if check.returncode != 0:
            return {"skipped": True, "reason": "not_a_git_repo"}
    except (subprocess.SubprocessError, OSError):
        return {"skipped": True, "reason": "git_unavailable"}

    state = store.replay()
    # canonical 主工作树脏文件一次性读取（merged 残留检测；flat 单 worktree 即自身）
    try:
        from orchd.gitops import list_tracked_changes, main_worktree_root

        main_dirty = list_tracked_changes(main_worktree_root(root)) or []
    except Exception:
        main_dirty = []

    issues: list[dict[str, Any]] = []
    for task in tasks:
        tid = task.get(ID_FIELD, "")
        ts = state.get(tid)
        s = ts.status if ts else STATUS_PENDING
        if scope == "active":
            if s not in (STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW):
                continue
        elif scope == "merged":
            if s != STATUS_COMPLETED:
                continue
        declared = list(task.get(FILES_TO_EDIT_FIELD, []) or [])
        if not declared:
            continue
        entry: dict[str, Any] = {"task_id": tid, "status": s}
        if scope == "active":
            try:
                from orchd.worktree import main_worktree_dirty_overlap

                overlap = main_worktree_dirty_overlap(root, declared)
                if overlap:
                    entry["main_dirty_overlap"] = overlap
            except Exception:
                pass
        else:
            # 目录式声明感知交集（task-decl-dir-match-guards）
            from orchd.pool import _prefix_overlap
            residual = sorted(_prefix_overlap(main_dirty, declared))
            if residual:
                entry["main_residual"] = residual
        try:
            from orchd.worktree import missing_declared_branch_files

            missing = missing_declared_branch_files(root, tid, declared)
            if missing:
                if scope == "active":
                    entry["missing_declared_files"] = missing
                else:
                    entry["historical_missing"] = missing
        except Exception:
            pass
        if len(entry) > 2:
            issues.append(entry)

    label = "活跃任务" if scope == "active" else "merged 任务"
    return {
        "skipped": False,
        "scope": scope,
        "issues": issues,
        "summary": f"{len(issues)} 个{label}存在完整性告警",
    }



def _scan_revive_markers(store: Store) -> dict[str, dict[str, Any]]:
    """扫描 ledger，返回曾被 completed→pending 复活的任务 → 最近一次复活标记。

    复活 = ``FORCE_STATUS`` 将任务从 ``completed`` 强制回到 ``pending``
    （task-force-status-revive-guard 门禁：需 ``--force + --evidence-sha`` 的 git 证据）。
    标记供 ``status`` / ``claim_preview`` 透明展示，正常任务（无复活历史）不在结果中
    （零误伤）。被撤回（RETRACT）的 FORCE_STATUS 事件不计入（与 replay 状态机一致）。

    Returns:
        ``{task_id: {"revived_at", "revived_by", "reason", "evidence_sha"}}``；
        无 ledger / 读取异常时返回空 dict。
    """
    if not store.ledger_exists():
        return {}
    try:
        events = store._read_ledger_lines(from_line=1)
        retracted = store._collect_retracted_event_ids()
    except Exception:
        return {}
    status_by_task: dict[str, str] = {}
    markers: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") == EVENT_RETRACT or ev.get("event_id", "") in retracted:
            continue
        tid = ev.get("task_id", "")
        etype = ev.get("type")
        if etype == EVENT_CLAIMED:
            status_by_task[tid] = STATUS_CLAIMED
        elif etype == EVENT_DONE:
            status_by_task[tid] = STATUS_DONE
        elif etype == EVENT_REVIEW_SUBMITTED:
            # review-unify-r2：unified 单阶段（无 review_type）或 code APPROVED
            # 均视为终审完成；老事件含 review_type: spec 保持两阶段语义。
            if ev.get("verdict") == "APPROVED" and (
                ev.get("review_type") == "code" or ev.get("review_type") is None
            ):
                status_by_task[tid] = STATUS_COMPLETED
        elif etype == EVENT_FORCE_STATUS:
            target = ev.get("target_status", STATUS_PENDING)
            # 测试数据（test_data=True）标记（task-p2-ledger-audit-noise）：测试注入的
            # FORCE_STATUS 事件（如 completed→pending 复活 fixture）须与真实生产事件区分，
            # 避免 revive_audit 审计噪音。仅跳过告警生成，仍推进状态机状态
            # （status_by_task 照常更新）以防误伤后续真实事件判定。
            if not ev.get("test_data"):
                # 复活：当前状态为 completed 且被强制回到 pending
                if (target == STATUS_PENDING
                        and status_by_task.get(tid) == STATUS_COMPLETED):
                    markers[tid] = {
                        "revived_at": ev.get("timestamp"),
                        "revived_by": ev.get("agent_id"),
                        "reason": ev.get("reason"),
                        "evidence_sha": ev.get("evidence_sha"),
                    }
            status_by_task[tid] = target
    return markers



def task_revive_markers(store: Store) -> dict[str, dict[str, Any]]:
    """公开封装：返回 ``{task_id: revive_marker}``（曾被 completed→pending 复活的任务）。"""
    return _scan_revive_markers(store)



def revive_audit(
    store: Store,
    tasks: list[dict[str, Any]],
    project_root: Path | None = None,
) -> dict[str, Any]:
    """只读巡检：扫描 ledger 中 FORCE_STATUS 将 completed→pending 的复活操作，列 warning。

    与 ``merge_audit`` / ``intake_audit`` 同级只读先例：仅告警不阻断（audit 照常返回，
    warning 列表新增复活项），提示「请确认是否有授权」（revive-guard 要求
    ``--force + --evidence-sha`` 的 git 证据）。

    Args:
        store: ledger Store（replay 取任务状态）。
        tasks: _master.json 的 tasks[] 定义列表（占位兼容，当前扫描全 ledger）。
        project_root: 仓库根目录（占位兼容，当前未使用）。

    Returns:
        ``{"skipped": False, "warnings": [{task_id, revived_at, revived_by,
        reason, evidence_sha, hint}]}``；无 ledger 时 ``{"skipped": True, ...}``。
    """
    markers = _scan_revive_markers(store)
    warnings = [
        {
            "task_id": tid,
            "revived_at": m["revived_at"],
            "revived_by": m["revived_by"],
            "reason": m["reason"],
            "evidence_sha": m["evidence_sha"],
            "hint": "该任务曾被从 completed 强制复活为 pending（completed→pending），"
                    "请确认是否有授权（revive-guard 要求 --force + --evidence-sha 的 git 证据）",
        }
        for tid, m in sorted(markers.items())
    ]
    return {"skipped": False, "warnings": warnings}



def intake_audit(project_root: Path) -> dict[str, Any]:
    """只读巡检：检测未提交的摄入产物改动（intake-commit-enforcement，2026-08-14）。

    摄入产物 = ``.orchd/_master.json``、``IDEAS.md`` / ``ROADMAP.md``（根布局与
    发布态 ``.orchd`` 布局）。检测工作区中这些文件的未提交改动 → 告警清单，
    让"摄入/注册后改动未入库"可被巡检发现（对齐 ``--audit-merge`` 只读先例）。

    只读：不执行 add/commit/checkout/reset。非 git 仓库或 git 不可用返回 skipped。
    """
    root = Path(project_root)
    dirty = list_tracked_changes(root)
    if dirty is None:
        return {"skipped": True, "reason": "git_unavailable"}
    # 摄入产物白名单（task-inventory-honesty 起以 orchd.intake._INTAKE_PRODUCT_FILES
    # 为单一真源，禁双写漂移；此前三处手写集合已分叉）。
    from orchd.intake import _INTAKE_PRODUCT_FILES as products
    hit = sorted(f for f in dirty if f in products)
    warnings = [
        {
            "file": f,
            "reason": "uncommitted_intake_product",
            "hint": (
                "摄入/注册产物未提交：请运行 orchd intake（提交 IDEAS/ROADMAP）"
                "或 orchd amend（注册+提交），或人工 git commit"
            ),
        }
        for f in hit
    ]
    return {"skipped": False, "warnings": warnings}
