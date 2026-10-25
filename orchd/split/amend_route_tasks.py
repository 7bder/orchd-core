from __future__ import annotations

from orchd.contracts.task_schema import (
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
    SOURCE_FIELD,
)
from orchd.contracts.events import (
    STATUS_CLAIMED,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import TaskState
from orchd.spec import Master, validate_source
from pathlib import Path
from typing import Any
import re
from .amend_route_state import _amend_route_state_matrix

def _amend_route_tasks(
    *,
    tasks: list[dict[str, Any]],
    state: dict[str, TaskState],
    existing_tasks: dict[str, dict[str, Any]],
    master: Master,
    project_root: Path,
    orchd_dir: Path,
    revise_terminal: str | None,
    revision_reason: str | None,
    errors: list[dict[str, Any]],
    updated_tasks: list[str],
    whitelisted_updates: list[dict[str, Any]],
    new_tasks: list[str],
    unchanged_tasks: list[str],
    sources_missing: list[str],
    sources_invalid: list[str],
    source_errors: list[dict[str, Any]],
    attachable_sync: list[dict[str, Any]],
    terminal_decl_sync: list[dict[str, Any]],
    terminal_text_revisions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """amend 分派段：逐任务变更比对与状态矩阵准入 + 源引用校验（原 L611-961）。

    累加器按引用就地增量写入（调用方可见）；返回 (conflict_warnings, removed_tasks)。
    """
    new_task_ids = set()
    conflict_warnings: list[dict[str, Any]] = []
    for task in tasks:
        tid = task.get(ID_FIELD, "")
        new_task_ids.add(tid)
        ts = state.get(tid)
        status = ts.status if ts else STATUS_PENDING

        if tid not in existing_tasks:
            # 新增任务强制 source 声明（2026-08-11 硬约束 + 存量豁免）。
            # 存量任务（snapshot 中存在）grandfather：不要求 source、不校验引用。
            # A4（task-amend-terminal-exempt）：快照漂移的终态卡不按新卡要求溯源。
            # existing_tasks 来自 snapshot，快照落后账本时终态任务会被误判为新卡，
            # 进而因归档 idea 恒 E025（执行者被迫回灌引擎自己的引用完整性）。账本
            # 终态为准：completed/cancelled 跳过 source 硬要求（与 validate_source
            # 的 P2-2 豁免同源）。
            _terminal_drifted = status in TERMINAL_STATUSES
            source = task.get(SOURCE_FIELD)
            if not _terminal_drifted and (
                    not source or not isinstance(source, str)
                    or not source.strip()):
                sources_missing.append(tid)
                source_errors.append({
                    "task_id": tid,
                    "status": "new",
                    "message": (
                        "新增任务缺 source 字段，拒绝注册（须声明来源 "
                        "idea:<ref> 或 roadmap:<ref>，存量任务豁免）"
                    ),
                })
            # 新增任务：与在池 pending/claimed 任务（含同批次新任务）的
            # files_to_edit 冲突降级为 warning（不阻断注册）。冲突硬边界
            # 在 claim E010（活跃集合判定）+ request 依赖感知强制过滤
            # （与 claimed 冲突、与 pending 非依赖任务冲突均被过滤）。
            new_files = set(task.get(FILES_TO_EDIT_FIELD, []))
            if new_files:
                conflicts: list[dict[str, Any]] = []
                for other in tasks:
                    oid = other.get(ID_FIELD)
                    if not isinstance(oid, str):
                        continue
                    if oid == tid:
                        continue
                    ots = state.get(oid)
                    ostat = ots.status if ots else STATUS_PENDING
                    if ostat not in (STATUS_PENDING, STATUS_CLAIMED):
                        continue
                    overlap = new_files & set(other.get(FILES_TO_EDIT_FIELD, []))
                    if overlap:
                        conflicts.append({
                            "task_id": oid,
                            "status": ostat,
                            "files": sorted(overlap),
                        })
                if conflicts:
                    conflict_warnings.append({
                        "task_id": tid,
                        "status": "new",
                        "message": (
                            "新任务与在池任务存在文件冲突（warning，不阻断）："
                            f"{conflicts}。依赖链上共享文件为合法串行序列；"
                            "其余冲突将由 request 依赖感知强制过滤与 claim E010 拦截"
                        ),
                    })
            new_tasks.append(tid)
            continue

        old_task = existing_tasks[tid]
        if old_task == task:
            unchanged_tasks.append(tid)
            continue

        _amend_route_state_matrix(
            task=task,
            tid=tid,
            status=status,
            old_task=old_task,
            errors=errors,
            project_root=project_root,
            revise_terminal=revise_terminal,
            revision_reason=revision_reason,
            terminal_decl_sync=terminal_decl_sync,
            terminal_text_revisions=terminal_text_revisions,
            unchanged_tasks=unchanged_tasks,
            updated_tasks=updated_tasks,
            whitelisted_updates=whitelisted_updates,
            attachable_sync=attachable_sync,
        )
    # 被删除的任务（snapshot 中存在但 master 已移除）：不阻止，仅报告
    removed_tasks = sorted(set(existing_tasks) - new_task_ids)

    # E025：新增任务 source 引用硬校验（2026-08-11，task-source-amend-enforce）。
    # validate_source 对全量校验（含存量），但仅对新增任务硬阻断；存量任务
    # grandfather 豁免（不校验引用、不要求 source）。按 path 定位到 task id，
    # 仅收集新增任务命中。
    if new_tasks:
        new_task_set = set(new_tasks)
        violations = validate_source(master, project_root=orchd_dir.parent)
        for v in violations:
            # task-split-additional-sources-gate：主 source 与附加引用同等
            # 硬门（此前正则仅匹配 `.source` 结尾，新任务附加引用非法被静默
            # 跳过；validate 告警仍在，只丢 amend 硬阻断）。
            m = re.match(
                r"\$\.tasks\[(\d+)\]\.(?:source|additional_sources\[\d+\])$",
                v.path)
            if not m:
                continue
            idx = int(m.group(1))
            if not (0 <= idx < len(master.tasks)):
                continue
            tid = master.tasks[idx].get(ID_FIELD, "")
            if tid in new_task_set:
                sources_invalid.append(tid)
                source_errors.append({
                    "task_id": tid,
                    "status": "new",
                    "message": v.message,
                })

    if source_errors:
        raise OrchdError(
            ErrorCode.E025,
            f"source_validation_failed: {len(source_errors)} new task(s) "
            "missing/invalid source, amend aborted。"
            " 提示：外部来源任务可用 debug:manual 标记。",
            source_errors,
        )

    if errors:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: {len(errors)} task(s) cannot be amended",
            [{**errors[0], "reason": "cannot_amend"}, *errors[1:]],
        )
    return conflict_warnings, removed_tasks
