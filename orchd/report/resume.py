"""report — status 派生计算域（自 ``status.py`` 拆出，纯搬移）。

``status`` 重导出本模块符号（单向依赖，无环）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.contracts.events import (
    STATUS_CANCELLED, STATUS_CLAIMED, STATUS_COMPLETED,
    STATUS_DONE, STATUS_IN_REVIEW, STATUS_PENDING,
)
from orchd.contracts.task_schema import PHASE_FIELD
from orchd.ledger import Store, TaskState

def _hook_runtime_degraded_guards(
    state: dict[str, TaskState],
    project_root: Path | None,
) -> list[dict[str, Any]]:
    """status 侧消费 degraded 认领记录（task-cont-hook-runtime-gate AC2）。

    claim 在 ``ORCHD_ALLOW_HOOK_DEGRADED=1`` 下放行时会写
    ``.orchd-runtime/hook_runtime_degraded.json``；doctor 已据此报
    ``hook_runtime_unavailable``，status 也必须标红，避免「claim 响应提示过、
    后续状态查看全部看不见」。

    口径与 doctor 一致：只对**当前实现者 claimed** 的任务展示——记录属于已完成
    任务时是历史残影，不能让 status 永久红。读取失败一律静默降级为空（只读诊断
    不得因观测面故障阻断）。
    """
    if project_root is None:
        return []
    try:
        # 经已登记的 ``report.status -> gitops`` 边取子模块属性，避免新增
        # gitops.hook 未登记边（同 precheck 的既有写法）。
        import orchd.gitops as _gitops

        record = _gitops.hook.install.read_hook_runtime_degraded(project_root)
    except Exception:
        return []
    if not isinstance(record, dict):
        return []
    task_id = str(record.get("task_id") or "")
    ts = state.get(task_id)
    if not task_id or ts is None or ts.status != STATUS_CLAIMED:
        return []
    return [{
        "guard": "hook_runtime",
        "task_id": task_id,
        "reason": "hook_runtime_unavailable",
        "shell": record.get("shell"),
        "error": record.get("error", ""),
        "recorded_at": record.get("recorded_at"),
        "hint": (
            "degraded 认领记录：L3 越界门禁（E020）在认领期不可用；"
            "修复 sh / 设置 ORCHD_HOOK_SHELL 后重新 claim 重烘焙，记录将自动清除"
        ),
    }]

def _task_event_facts(store: Store, task_id: str) -> dict[str, Any]:
    """从账本事件派生单任务时间面（task-cont-show-resume，只读、不落盘）。

    事件流 append-only 且按时间有序 → 顺序扫描、末值即最新。派生：
    ``claimed_at``（最近 CLAIMED）/ ``review_claimed_at``（最近 REVIEW_CLAIMED）/
    ``updated_at``（最近任意事件）/ ``latest_review``（最近 REVIEW_SUBMITTED，
    含 event_id/type/timestamp/phase/result）/ ``last_verify``（最近 DONE 的
    verify 存证摘要）/ ``last_event``（最近事件三元组）。无账本 / 读取异常 /
    无对应事件时相应值为 ``None``（AC：无对应事件时为 null），永不抛。
    """
    facts: dict[str, Any] = {
        "claimed_at": None,
        "review_claimed_at": None,
        "updated_at": None,
        "latest_review": None,
        "last_verify": None,
        "last_event": None,
    }
    if not store.ledger_exists():
        return facts
    try:
        events = store._read_ledger_lines(from_line=1)
    except Exception:
        return facts
    for ev in events:
        if not isinstance(ev, dict) or ev.get("task_id") != task_id:
            continue
        etype = ev.get("type") or ""
        ts = ev.get("timestamp")
        if ts:
            facts["updated_at"] = ts
        if etype == "CLAIMED":
            facts["claimed_at"] = ts
        elif etype == "REVIEW_CLAIMED":
            facts["review_claimed_at"] = ts
        elif etype == "REVIEW_SUBMITTED":
            facts["latest_review"] = {
                "event_id": ev.get("event_id"),
                "type": etype,
                "timestamp": ts,
                # PHASE_FIELD 常量作键（surface 域任务字段字面量守卫口径）
                PHASE_FIELD: ev.get("review_type"),
                "result": ev.get("verdict"),
            }
        elif etype == "DONE":
            verify = ev.get("verify")
            if isinstance(verify, dict):
                facts["last_verify"] = {
                    "ok": verify.get("ok"),
                    "exit_code": verify.get("exit_code"),
                    "at": ts,
                }
        facts["last_event"] = {
            "event_id": ev.get("event_id"),
            "type": etype,
            "timestamp": ts,
            "session_id": ev.get("session_id"),
        }
    return facts

def _parse_uncommitted_status(stdout: str, limit: int = 50) -> dict[str, Any]:
    """把 ``git status --porcelain=v1`` 输出解析为 uncommitted 明细。

    只读、不承诺文件内容：``tracked`` 为已跟踪改动行数，``untracked`` 为
    ``??`` 行数，``sample`` 取前 ``limit`` 条路径（重命名取目标路径）。
    """
    tracked = 0
    untracked = 0
    sample: list[str] = []
    for raw in (stdout or "").splitlines():
        if not raw.strip():
            continue
        code = raw[:2]
        path = raw[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if code == "??":
            untracked += 1
        else:
            tracked += 1
        if path and len(sample) < limit:
            sample.append(path)
    return {"tracked": tracked, "untracked": untracked, "sample": sample}

def _fallback_next_action(status_value: str, task_id: str) -> str | None:
    """分类器不可用时的原文案兜底（仅 degraded 路径，正常路径不硬编码中文）。"""
    return {
        STATUS_PENDING: f"request → claim --task {task_id} --confirm",
        STATUS_CLAIMED: (
            f"实现 files_to_edit → 任务分支提交 → done --task {task_id}"),
        STATUS_DONE: "等待审查：由实现会话 spawn 独立 subagent 领取审查",
        STATUS_IN_REVIEW: "等待审查结论（review --verdict approved / changes_requested）",
        STATUS_COMPLETED: "任务已收口（实现+审查+merge 全部完成）",
        STATUS_CANCELLED: "任务已取消",
    }.get(status_value)

def _resume_next_action(
    task_id: str,
    task_def: dict[str, Any],
    ts: TaskState | None,
    agent_id: str | None,
    store: Store,
) -> tuple[str | None, bool]:
    """复用 guide 状态分类器推导 next_action；失败回退原文案并标 degraded。

    分类器用「该任务」的聚焦视图（state + task_def），避免同会话其他任务
    的状态干扰单任务 resume 的下一步结论。
    """
    try:
        from orchd.guide.stage import _classify
        from orchd.ledger import resolve_review_mode

        focus_state = {task_id: ts} if ts is not None else {}
        step = _classify(
            focus_state,
            [task_def],
            agent_id,
            True,
            resolve_review_mode(store.orchd_dir),
        ).get("step")
        if step:
            return step, False
    except Exception:
        pass
    status_value = ts.status if ts else STATUS_PENDING
    return _fallback_next_action(status_value, task_id), True