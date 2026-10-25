from __future__ import annotations

from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD,
    EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD,
)
from orchd.contracts.events import TERMINAL_STATUSES
from orchd.schema_policy import amendable_fields, terminal_attachable_fields
from typing import Any
from .validate import _TERMINAL_TEXT_REVISABLE_FIELDS

def _amend_route_state_matrix(
    *,
    task,
    tid,
    status,
    old_task,
    errors,
    project_root,
    revise_terminal,
    revision_reason,
    terminal_decl_sync,
    terminal_text_revisions,
    unchanged_tasks,
    updated_tasks,
    whitelisted_updates,
    attachable_sync,
) -> None:
    """逐任务状态矩阵准入段（原 _amend_route_tasks for 体末块 L856-1086，231 行）。

    单任务：claimed 白名单字段门 / review 组 / 终态四支状态矩阵判定 + 字段差异
    归一化 + 声明同步 + E025 源引用校验；累加器（updated/unchanged/whitelisted/
    terminal_* / attachable_sync / errors）按引用就地写入，供编排器汇总。
    块内 4 处宿主级 `continue` 机械改写为 `return`——本块是 for 体**最后**一条
    语句，二者语义等价（跳过本迭代剩余部分 == 直接结束本迭代）；嵌套循环内的
    continue/break 原样保留。
    """
    # 有变更，检查约束
    if status == "claimed":
        changed_fields = {
            key
            for key in set(task) | set(old_task)
            if task.get(key) != old_task.get(key)
        }
        if changed_fields <= amendable_fields():
            # task-amend-scope-add：claimed 任务 files_to_edit 只增不删
            # 添加遗漏连带文件允许，删除已声明文件拒绝（E007）
            if FILES_TO_EDIT_FIELD in changed_fields:
                old_files = set(old_task.get(FILES_TO_EDIT_FIELD, []))
                new_files = set(task.get(FILES_TO_EDIT_FIELD, []))
                removed = old_files - new_files
                if removed:
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            "claimed task files_to_edit 只增不删："
                            f"禁止删除已声明文件 {sorted(removed)}，"
                            "仅允许添加遗漏连带文件"
                        ),
                        "hint": (
                            "出路（task-amend-patch-write-after-validate）："
                            "确需删声明请 retract --disposition retry（免认领冷却）"
                            "退回 pending 后 amend，再 claim（无需 --force）"
                        ),
                    })
                    return
            updated_tasks.append(tid)
            whitelisted_updates.append({
                "task_id": tid,
                "fields": sorted(changed_fields),
            })
        else:
            _rejected = set(changed_fields) - amendable_fields()
            errors.append({
                "task_id": tid,
                "status": status,
                "message": (
                    "claimed task only allows whitelist fields "
                    f"{sorted(amendable_fields())}, "
                    f"got {sorted(changed_fields)}"
                ),
                # task-spec-hygiene-flat-sweep AC2：AC 类字段被拒时附加
                # 状态感知路由（pending 改 / 终态 revise-terminal）。
                **({"hint": _ac_field_routing_hint(status, _rejected)}
                   if _rejected & _TERMINAL_TEXT_REVISABLE_FIELDS else {}),
            })
    elif status in TERMINAL_STATUSES:
        # task-amend-terminal-drift-repair：终态任务 master≠snapshot 时，
        # 区分"合法附加字段增量"与"核心字段变更"。附加字段白名单 → 自动
        # 以 master 为准同步 snapshot（不报 E007）；含核心字段 → 仍 E007。
        #
        # task-terminal-spec-revision-channel：开关指向本任务时，先剥离纯文本
        # 规格字段（有 AMEND 审计事件），**其余字段**仍须逐项落入既有两条通道
        # （terminal attachable / 声明路径规范化），否则 E007——结构性护栏，
        # 挡住"用文本字段夹带执行字段"的组合式规避。
        changed_fields = {
            key
            for key in set(task) | set(old_task)
            if task.get(key) != old_task.get(key)
        }
        text_revised: set[str] = set()
        if revise_terminal == tid:
            text_revised = changed_fields & _TERMINAL_TEXT_REVISABLE_FIELDS
        remaining = changed_fields - text_revised

        if not changed_fields:
            # 值级无差异（e.g. master 以 null 占位、快照缺键）→ 视为未变更，
            # 避免 key 存在性差异把空 diff 误判为"终态不可修改"（假阳性 E007）。
            unchanged_tasks.append(tid)
        elif not remaining:
            # 仅文本字段变更 → 走修订通道（审计事件在快照同步后写入）。
            # task-spec-hygiene-flat-sweep AC4：AC 条数启发式——文本修订不得
            # 改变 acceptance_criteria 条数（阈值 0，确定性）：条数增减会改变
            # E029 粒度语义与验收面，属范围变更而非文本对齐。命中即 E007，
            # 指引用户裁决后走逃生口（force-status 回 pending）。
            if revise_terminal == tid and ACCEPTANCE_CRITERIA_FIELD in text_revised:
                _old_ac = old_task.get(ACCEPTANCE_CRITERIA_FIELD) or []
                _new_ac = task.get(ACCEPTANCE_CRITERIA_FIELD) or []
                if len(_old_ac) != len(_new_ac):
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            f"{status} 任务 acceptance_criteria 条数变更"
                            f"（{len(_old_ac)} → {len(_new_ac)}）："
                            "文本修订通道只放行条数不变的文本对齐"
                        ),
                        "hint": (
                            "AC 增删改变验收面，请先经用户裁决走逃生口回退："
                            f"python .orchd/__main__.py force-status --task {tid} "
                            "--status pending --reason \"<理由>\" --force"
                            "（completed→pending 另需 --evidence-sha <提交>）"
                        ),
                    })
                    return
            updated_tasks.append(tid)
        elif remaining <= terminal_attachable_fields():
            updated_tasks.append(tid)
            attachable_sync.append({
                "task_id": tid,
                "status": status,
                "fields": sorted(remaining),
            })
        elif remaining <= {FILES_TO_EDIT_FIELD, EXEMPT_FILES_FIELD}:
            # task-terminal-decl-drift-channel：终态声明路径规范化通道。
            # 仅当删的全不存在、增的全存在（相对 project_root）时放行并
            # 同步 snapshot；否则仍 E007。防"借规范化之名篡改声明"。
            sync_entries: list[dict[str, Any]] = []
            normalized_ok = True
            for field in (FILES_TO_EDIT_FIELD, EXEMPT_FILES_FIELD):
                if field not in remaining:
                    continue
                old_decl = old_task.get(field, []) or []
                new_decl = task.get(field, []) or []
                removed_decl = [p for p in old_decl if p not in new_decl]
                added_decl = [p for p in new_decl if p not in old_decl]
                if any((project_root / p).exists() for p in removed_decl):
                    normalized_ok = False
                    break
                if any(not (project_root / p).exists() for p in added_decl):
                    normalized_ok = False
                    break
                sync_entries.append({
                    "task_id": tid,
                    "field": field,
                    "removed": sorted(removed_decl),
                    "added": sorted(added_decl),
                })
            if normalized_ok:
                updated_tasks.append(tid)
                terminal_decl_sync.extend(sync_entries)
            else:
                errors.append({
                    "task_id": tid,
                    "status": status,
                    "message": (
                        f"{status} 为终态，声明路径变更非规范化"
                        "（删的须不存在、增的须存在），不可修改"
                    ),
                    "changed_fields": sorted(remaining),
                    "diff": {
                        field: {
                            "snapshot": old_task.get(field),
                            "master": task.get(field),
                        }
                        for field in sorted(remaining)
                    },
                    "hint": _terminal_rejection_hint(tid, remaining, status),
                })
                return
        else:
            # AC6：列出差异字段两侧取值（snapshot vs master）+ 可执行指引，
            # 使同类漂移可自查（此前仅一句"为终态，不可修改"）。
            errors.append({
                "task_id": tid,
                "status": status,
                "message": (
                    f"{status} 为终态，字段 {sorted(remaining)} 不可修改"
                    "（非附加字段 / 非文本修订白名单）"
                ),
                "changed_fields": sorted(remaining),
                "diff": {
                    field: {
                        "snapshot": old_task.get(field),
                        "master": task.get(field),
                    }
                    for field in sorted(remaining)
                },
                "hint": _terminal_rejection_hint(tid, remaining, status),
            })
            return

        if text_revised:
            terminal_text_revisions.append({
                "task_id": tid,
                "status": status,
                "fields": sorted(text_revised),
                "rationale": revision_reason,
            })
    elif status in ("done", "in_review"):
        # T2（2026-08-08）+ M-2（2026-08-12）+ Bug #20c（2026-08-27）：
        # 仅允许修改附加字段白名单（reviewers / verify_command /
        # exempt_files / verify_timeout_seconds / files_to_edit /
        # files_to_read）。把新定义的附加字段全部还原为旧值后与旧定义
        # 比对，相等才说明"只有白名单字段变了"。
        normalized = dict(task)
        for field in amendable_fields():
            if field in old_task:
                normalized[field] = old_task.get(field)
            else:
                # old_task（snapshot）无该键时从 new 定义中移除，与旧定义对齐
                normalized.pop(field, None)
        if normalized == old_task:
            updated_tasks.append(tid)
            changed_fields = {
                key
                for key in set(task) | set(old_task)
                if task.get(key) != old_task.get(key)
            }
            if changed_fields:
                whitelisted_updates.append({
                    "task_id": tid,
                    "fields": sorted(changed_fields),
                })
        else:
            _normalized_changed = {
                key
                for key in set(task) | set(old_task)
                if task.get(key) != old_task.get(key)
            } - amendable_fields()
            errors.append({
                "task_id": tid,
                "status": status,
                "message": (
                    "review 阶段仅允许修改附加字段 "
                    "(reviewers / verify_command / exempt_files / "
                    "verify_timeout_seconds)；检测到其他字段同时被修改"
                ),
                # task-spec-hygiene-flat-sweep AC2：AC 类字段被拒时附加
                # 状态感知路由（pending 改 / 终态 revise-terminal）。
                **({"hint": _ac_field_routing_hint(status, _normalized_changed)}
                   if _normalized_changed & _TERMINAL_TEXT_REVISABLE_FIELDS else {}),
            })
    else:
        # pending：可改全部（含 acceptance_criteria；合法 CLI 通道为
        # amend --task <id> --acceptance-criteria ...，独立旗标 +
        # 显式 pending 校验，不走白名单——task-amend-ac-pending-channel）
        updated_tasks.append(tid)

def _ac_field_routing_hint(status: str, fields: set[str]) -> str:
    """AC 类字段被拒时的状态感知路由指引（task-spec-hygiene-flat-sweep AC2）。

    AC 类字段 = :data:`_TERMINAL_TEXT_REVISABLE_FIELDS`（acceptance_criteria /
    brief / name / deliverables）：pending 态可直接改；claimed/done/in_review
    锁死；终态走 ``amend --revise-terminal``。调用方在 E007 中附加本指引，
    使用户按状态找到合法通道，而不是只看到"不可修改"。
    """
    names = sorted(fields & _TERMINAL_TEXT_REVISABLE_FIELDS)
    if status == "claimed":
        return (
            f"字段 {names} 属 AC 类文本：claimed 态不可改；回 pending 改 "
            "（retract --disposition retry 免冷却），或等终态走 "
            "amend --revise-terminal <task_id> --reason \"<理由>\""
        )
    if status in ("done", "in_review"):
        return (
            f"字段 {names} 属 AC 类文本：review 阶段不可改；退回 pending 改，"
            "或等终态（completed/cancelled）走 "
            "amend --revise-terminal <task_id> --reason \"<理由>\""
        )
    return (
        f"字段 {names} 属 AC 类文本：pending 态可直接改——合法通道："
        "amend --task <id> --acceptance-criteria \"<ac>\" ...（整体覆写）；终态走 "
        "amend --revise-terminal <task_id> --reason \"<理由>\""
    )

def _terminal_rejection_hint(task_id: str, fields: set[str], status: str) -> str:
    """终态字段被拒时的可执行指引（AC6："可自查"而非只报不可修改）。"""
    revisable = sorted(fields & _TERMINAL_TEXT_REVISABLE_FIELDS)
    if revisable:
        return (
            f"字段 {revisable} 属规格文本，可走修订通道（写 AMEND 审计事件、"
            "快照随 master 同步）："
            f"python .orchd/__main__.py amend --revise-terminal {task_id} "
            "--reason \"<修订理由>\""
        )
    return (
        f"{status} 任务的 {sorted(fields)} 属执行字段，不因文本修订通道放行；"
        f"确需变更请先经用户裁决走逃生口回退："
        f"python .orchd/__main__.py force-status --task {task_id} --status pending "
        "--reason \"<理由>\" --force（completed→pending 另需 --evidence-sha <提交>）"
    )
