"""report — status 命令域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行）。``status`` 主流程（阶段聚合 + 文本渲染 +
自动清理 stderr 提示）；只读派生计算拆入 ``resume`` 并重导出（同一对象）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD, BRIEF_FIELD, DEPENDS_ON_FIELD, EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD, ID_FIELD, IMPORTANCE_FIELD, MODULE_FIELD, NAME_FIELD,
    REVIEWERS_FIELD, SOURCE_FIELD, VERIFY_COMMAND_FIELD,
)
from orchd.contracts.events import (
    STATUS_CANCELLED, STATUS_CLAIMED, STATUS_COMPLETED, STATUS_DONE,
    STATUS_IN_REVIEW, STATUS_PENDING, TERMINAL_STATUSES,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store, TaskState
from .checks import (
    _scan_revive_markers,
)

from .phase import (
    phase_aggregate,
)

from .resume import (
    _fallback_next_action, _hook_runtime_degraded_guards,
    _parse_uncommitted_status, _resume_next_action, _task_event_facts,
)

from .text import (
    _auto_clean_emit_stderr, _format_auto_clean_text,
    _format_phase_text, _format_text,
)


def _format_degraded_text(guards: list[dict[str, Any]]) -> str:
    """人类可读的 degraded 标红块（--text 模式追加，JSON 模式走字段）。"""
    lines = ["", "=== Degraded Guards ==="]
    for guard in guards:
        detail = guard.get("error") or ""
        lines.append(
            f"- {guard.get('guard')} {guard.get('task_id')}: "
            f"{guard.get('reason')}" + (f"（{detail}）" if detail else ""))
        hint = guard.get("hint")
        if hint:
            lines.append(f"  {hint}")
    return "\n".join(lines)


def _ac_item_text(item: Any) -> str:
    """AC 条目归一为文本（master 口径为 str，兼容 dict 形态）。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("text") or item.get(ID_FIELD)
        return str(value) if value is not None else str(item)
    return str(item)


def _task_resume_block(
    store: Store,
    task_id: str,
    task_def: dict[str, Any],
    ts: TaskState | None,
    project_root: Path | None,
    *,

    agent_id: str | None = None,
) -> dict[str, Any]:
    """单任务 resume 块（task-cont-show-resume-fields）：只读、即时推导、不落盘。

    字段全部从 ledger + git + master 现算：task_id / status / claimed_by（账本
    replay）、branch / worktree_path / uncommitted / uncommitted_count /
    last_commit（git 即时查询）、last_verify / last_event（账本事件派生）、
    ac（{total, remaining, items}）/ ac_remaining（旧 int 契约）、next_action
    （复用 guide 状态分类器，不可用时回退原文案并标 next_action_degraded）。
    worktree 缺失或 git 不可用时相应字段安全降级为 ``None`` 并在 ``hints``
    附提示，不抛异常。
    """
    status_value = ts.status if ts else STATUS_PENDING
    facts = _task_event_facts(store, task_id)
    last_verify = facts["last_verify"]
    if isinstance(last_verify, dict):
        # 历史 DONE verify 记录未存 command：回退 master 的 verify_command。
        last_verify = dict(last_verify)
        if not last_verify.get("command"):
            last_verify["command"] = task_def.get(VERIFY_COMMAND_FIELD)
    acs = task_def.get(ACCEPTANCE_CRITERIA_FIELD) or []
    ac_items = [_ac_item_text(item) for item in acs]
    ac_remaining = 0 if status_value in TERMINAL_STATUSES else len(ac_items)
    next_action, next_action_degraded = _resume_next_action(
        task_id, task_def, ts, agent_id, store)
    block: dict[str, Any] = {
        "task_id": task_id,
        "status": status_value,
        "claimed_by": ts.claimed_by if ts else None,
        "branch": None,
        "worktree_path": None,
        "uncommitted": None,
        "uncommitted_count": None,
        "last_commit": None,
        "last_verify": last_verify,
        "ac_remaining": ac_remaining,
        "ac": {
            "total": len(ac_items),
            "remaining": ac_remaining,
            "items": ac_items,
        },
        "next_action": next_action,
        "next_action_degraded": next_action_degraded,
        "last_event": facts["last_event"],
        "hints": [],
    }
    if next_action_degraded:
        block["hints"].append(
            "状态分类器不可用：next_action 已回退原文案（degraded），"
            "不阻塞 resume 其余字段")

    if project_root is None:
        block["hints"].append("project_root 不可得：git 面字段降级为空")
        return block

    import subprocess

    branch = f"task/{task_id}"

    def _git(argv: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(cwd), *argv],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return None

    branch_exists = _git(
        ["show-ref", "--verify", f"refs/heads/{branch}"], project_root)
    if branch_exists is None:
        block["hints"].append("git 不可用：branch/commit 面字段降级为空")
    elif branch_exists.returncode == 0:
        block["branch"] = branch
    else:
        block["hints"].append(f"分支 {branch} 不存在（未认领或已回收）")

    # 任务 worktree 定位（container 布局独立目录；flat 无独立 worktree）。
    wt_path: Path | None = None
    try:
        from orchd.worktree.layout import detect_layout
        from orchd.worktree.bindings import _task_wt_name

        layout = detect_layout(project_root)
        wt_root = layout.get("task_wt_root")
        if wt_root is not None:
            candidate = Path(wt_root) / _task_wt_name(task_id)
            if (candidate / ".git").exists():
                wt_path = candidate
    except Exception:
        wt_path = None
    if wt_path is not None:
        block["worktree_path"] = str(wt_path)
        st = _git(
            ["status", "--porcelain=v1", "--untracked-files=all"], wt_path)
        if st is None or st.returncode != 0:
            block["hints"].append(
                "worktree git status 不可得：uncommitted/uncommitted_count 降级为空")
        else:
            uncommitted = _parse_uncommitted_status(st.stdout or "")
            block["uncommitted"] = uncommitted
            block["uncommitted_count"] = (
                uncommitted["tracked"] + uncommitted["untracked"])
    elif project_root and (project_root / ".git").exists():
        block["hints"].append(
            "任务 worktree 不存在（未认领 / 已回收 / flat 布局）：worktree 面字段降级为空")

    # last_commit：worktree 优先，缺失时从分支读（分支即已提交历史的真源）。
    if block["branch"]:
        log = _git(
            ["log", "-1", "--format=%H%x1f%s%x1f%cI"],
            wt_path if wt_path is not None else project_root,
        )
        if log is not None and log.returncode == 0 and (log.stdout or "").strip():
            parts = (log.stdout or "").strip().split("\x1f", 2)
            block["last_commit"] = {
                "sha": parts[0],
                "subject": parts[1] if len(parts) > 1 else "",
                "at": parts[2] if len(parts) > 2 and parts[2] else None,
            }
    return block




def status(
    store: Store,
    tasks: list[dict[str, Any]],
    project: dict[str, Any] | None = None,
    text: bool = False,
    task_id: str | None = None,
    project_root: Path | None = None,
    active_only: bool = True,
    state: dict[str, Any] | None = None,
    phase: bool = False,
    active_phase: str | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """全局状态快照；给定 task_id 时返回单任务详情。

    当 text=True 时，返回结果中额外包含 ``_text`` 字段，其值为人类可读的
    纯文本表格（由 _format_text 生成），供 CLI 层直接打印，不混入 JSON 输出。

    Args:
        active_only: 仅输出活跃任务（status ∈ pending/claimed/done/in_review），
            终态 completed/cancelled 默认隐藏（task-status-active-only）。
            传 False 回退全量。summary.total 恒为全量任务数，另增 active（活跃数）。
        state: 可选的预计算 replay 状态（task-replay-command-scoped-once）。单命令
            链路内多个消费者共用同一 snapshot 时由调用方传入，避免重复全解析；
            缺省 None → 自行 ``store.replay()``（既有调用点零回归）。

    Returns:
        JSON 结构：project + tasks[] + summary（total + active + 六状态计数）；
        或单任务详情 {"task": {...}}。

    Raises:
        OrchdError E005: task_id 在 master 中不存在。
    """
    if state is None:
        state = store.replay()
    # 复活标记（task-force-status-revive-audit）：扫描 ledger 检测 completed→pending
    # 复活操作，对曾被复活的任务附加 revive_marker（reason+evidence_sha+时间），
    # 正常任务不附加（零误伤）。
    try:
        revive_by_task = _scan_revive_markers(store)
    except Exception:
        revive_by_task = {}
    task_map = {t.get(ID_FIELD, ""): t for t in tasks}

    if task_id is not None:
        task_def = task_map.get(task_id)
        if task_def is None:
            raise OrchdError(
                ErrorCode.E005,
                f"task '{task_id}' not found in master",
                [{"task_id": task_id}],
            )
        ts = state.get(task_id)
        detail: dict[str, Any] = {
            ID_FIELD: task_id,
            NAME_FIELD: task_def.get(NAME_FIELD, ""),
            BRIEF_FIELD: task_def.get(BRIEF_FIELD, ""),
            "status": ts.status if ts else STATUS_PENDING,
            MODULE_FIELD: task_def.get(MODULE_FIELD, ""),
            DEPENDS_ON_FIELD: task_def.get(DEPENDS_ON_FIELD, []),
            IMPORTANCE_FIELD: task_def.get(IMPORTANCE_FIELD, "normal"),
            REVIEWERS_FIELD: task_def.get(REVIEWERS_FIELD, []),
            VERIFY_COMMAND_FIELD: task_def.get(VERIFY_COMMAND_FIELD, ""),
            # task-hostfix-pack-a B1：单任务详情含声明域（看完整卡面不再绕道读
            # _master.json；加法字段，零破坏）。
            FILES_TO_EDIT_FIELD: task_def.get(FILES_TO_EDIT_FIELD, []),
            EXEMPT_FILES_FIELD: task_def.get(EXEMPT_FILES_FIELD, []),
            ACCEPTANCE_CRITERIA_FIELD: task_def.get(ACCEPTANCE_CRITERIA_FIELD, []),
        }
        if ts:
            if ts.claimed_by:
                detail["claimed_by"] = ts.claimed_by
            if ts.review_phase:
                detail["review_phase"] = ts.review_phase
            if ts.review_claimed_by:
                detail["review_claimed_by"] = ts.review_claimed_by
            # B1（2026-08-13 full-audit-v2）：completed 但 merge 未落地时展示
            # merge_warning，供 audit-merge 告警后的「人工确认 main 是否含实现」直接查看
            if ts.merge_warning:
                detail["merge_warning"] = ts.merge_warning
            detail["attempt_count"] = ts.attempt_count
            if task_id in revive_by_task:
                detail["revive_marker"] = revive_by_task[task_id]
        # task-cont-show-resume：事件派生时间面（claimed_at / review_claimed_at /
        # updated_at / latest_review；无对应事件为 null，不覆盖既有字段）。
        _facts = _task_event_facts(store, task_id)
        for _k in ("claimed_at", "review_claimed_at", "updated_at",
                   "latest_review"):
            detail[_k] = _facts[_k]
        # task-cont-show-resume：只读 resume 块（ledger+git+master 即时推导，
        # 不落盘、不新增状态文件；worktree/git 缺失时安全降级并附提示）。
        detail["resume"] = _task_resume_block(
            store, task_id, task_def, ts, project_root, agent_id=agent_id)
        # task-cont-hook-runtime-gate AC2：degraded 认领记录在单任务卡面也标红。
        _degraded = _hook_runtime_degraded_guards(state, project_root)
        if _degraded:
            detail["degraded_guards"] = _degraded
        # task-status-text-flag-silent-noop：单任务详情也支持 --text，
        # 构造兼容 _format_text 的最小结构（tasks=[detail] + summary），
        # 避免提前 return 跳过 _text 生成导致 --text 恒输出 JSON。
        if text:
            _s = detail.get("status", STATUS_PENDING)
            # _format_text 期望 task_id 字段，而单任务 detail 用 id，做一次映射
            _row = dict(detail)
            _row["task_id"] = _row.get(ID_FIELD, task_id)
            _single = {
                "tasks": [_row],
                "summary": {
                    "total": 1,
                    "active": 1 if _s in (
                        STATUS_PENDING, STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW,
                    ) else 0,
                    STATUS_PENDING: 1 if _s == STATUS_PENDING else 0,
                    STATUS_CLAIMED: 1 if _s == STATUS_CLAIMED else 0,
                    STATUS_DONE: 1 if _s == STATUS_DONE else 0,
                    STATUS_IN_REVIEW: 1 if _s == STATUS_IN_REVIEW else 0,
                    STATUS_COMPLETED: 1 if _s == STATUS_COMPLETED else 0,
                    STATUS_CANCELLED: 1 if _s == STATUS_CANCELLED else 0,
                },
            }
            _single_text = _format_text(_single)
            if _degraded:
                _single_text += _format_degraded_text(_degraded)
            return {"task": detail, "_text": _single_text}
        return {"task": detail}

    task_statuses: list[dict[str, Any]] = []
    counts = {
        STATUS_PENDING: 0, STATUS_CLAIMED: 0, STATUS_DONE: 0,
        STATUS_IN_REVIEW: 0, STATUS_COMPLETED: 0, STATUS_CANCELLED: 0,
    }

    for task in tasks:
        tid = task.get(ID_FIELD, "")
        ts = state.get(tid)
        s = ts.status if ts else STATUS_PENDING
        counts[s] = counts.get(s, 0) + 1
        # task-status-active-only：默认仅输出活跃任务（非终态），
        # 终态 completed/cancelled 在 tasks[] 中隐藏；summary 计数不受影响。
        if active_only and s in TERMINAL_STATUSES:
            continue
        entry: dict[str, Any] = {
            "task_id": tid,
            NAME_FIELD: task.get(NAME_FIELD, ""),
            "status": s,
            MODULE_FIELD: task.get(MODULE_FIELD, ""),
            "attempt_count": ts.attempt_count if ts else 0,
            SOURCE_FIELD: task.get(SOURCE_FIELD),
        }
        if ts and ts.claimed_by:
            entry["claimed_by"] = ts.claimed_by
        if ts and ts.review_phase:
            entry["review_phase"] = ts.review_phase
        if ts and ts.review_claimed_by:
            entry["review_claimed_by"] = ts.review_claimed_by
        if tid in revive_by_task:
            entry["revive_marker"] = revive_by_task[tid]
        task_statuses.append(entry)

    # task-status-active-only：summary 增 active 计数且保留 total。
    # active = 活跃任务数（非终态）；total 恒为全量任务数。
    summary = {
        "total": len(tasks),
        "active": sum(
            counts[k] for k in (
                STATUS_PENDING, STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW,
            )
        ),
        **counts,
    }
    result: dict[str, Any] = {
        "project": dict(project) if project else {},
        "tasks": task_statuses,
        "summary": summary,
    }
    # task-cont-hook-runtime-gate AC2：status 消费 degraded 认领记录并标红。
    _degraded = _hook_runtime_degraded_guards(state, project_root)
    if _degraded:
        result["degraded_guards"] = _degraded

    # task-14-worktree-lifecycle（AC5）：孤儿 worktree 惰性清理（best-effort，
    # 绑定任务已终态但 worktree 仍在 → 回收；无独立 worktree 场景零操作）。
    if project_root is not None:
        try:
            from orchd.doctor import auto_clean
            from orchd.ledger import resolve_store_dir
            from orchd.worktree import prune_orphans

            pruned = prune_orphans(project_root, resolve_store_dir(store.orchd_dir), state)
            if pruned.get("pruned"):
                result["worktree_pruned"] = pruned
            # task-doctor-auto-clean：读路径自动清洁（低风险残留自动清理，
            # legacy 移入备份区，高风险仅报告；有过拓扑才并入结果，保持无残留时干净）。
            ac = auto_clean(
                project_root,
                emit_stderr=_auto_clean_emit_stderr(project_root),
            )
            if ac.get("auto_cleaned") or ac.get("auto_moved") or ac.get("manual_notice"):
                result["auto_clean"] = ac
        except Exception:
            pass

    if text:
        result["_text"] = _format_text(result)
        ac_text: Any = result.get("auto_clean")
        if ac_text:
            result["_text"] += _format_auto_clean_text(ac_text)
        if _degraded:
            result["_text"] += _format_degraded_text(_degraded)

    # task-phase-status-aggregate：阶段聚合视图（仅 --phase 时附加，零回归）
    if phase:
        agg = phase_aggregate(tasks, state, active_phase)
        result["active_phase"] = agg["active_phase"]
        result["phases"] = agg["phases"]
        if text:
            result["_text"] += _format_phase_text(agg["phases"], agg["active_phase"])

    return result