"""claim 域 —— 认领前置守卫（precheck）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`_claim_precheck`：claim 预校验（无锁快速失败；声明权威解析 / 角色推断 /
  reviewer 自动切分支 / implementer 滞留自愈 / guard_claim）。
- :func:`_branch_degraded_guard`：try_git_branch failed 状态转 degraded_guard。
- :func:`_check_master_uncommitted`：主工作树 _master.json 未提交检测（best-effort）。
- :func:`_check_retract_cooldown`：retract 认领冷却检查。

叶子域：不导入同包其它域。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_RETRACT,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import guard_claim as _guard_claim
from orchd.gitops_ops import try_git_branch as _try_git_branch
from orchd.ledger import Store, resolve_session_identity

# retract 认领冷却期（秒）：与 control._RETRACT_COOLDOWN_S 保持一致（task-retract-bind-cooloff）。
# 任务被 retract（disposition=abandon）后默认 300s 内拒绝重新认领，--force 可绕过。
_RETRACT_COOLDOWN_S = 300


def _branch_degraded_guard(task_id: str, branch_state: dict[str, Any]) -> dict[str, Any]:
    """把 try_git_branch 的 failed 状态转成一条 degraded_guard（task-git-branch-fail-report）。

    与 bind_task_wt 失败先例一致：claim 结果 JSON 通过 degraded_guards 显式暴露
    git 任务分支 checkout / 创建失败的原因（step + stderr 摘要），使 agent 可见可查，
    而非静默吞错后在下游 checkout 崩溃。
    """
    return {
        "guard": "try_git_branch",
        "task_id": task_id,
        "state": branch_state.get("state"),
        "step": branch_state.get("step"),
        "error": branch_state.get("error"),
        "hint": "git 任务分支 checkout/创建失败，claim 已降级继续；请据 error 排查仓库状态"
        "（分支冲突 / 脏工作区 / 仓库损坏），必要时先 git 修复再重试",
    }


# ------------------------------------------------------------------
# claim（写操作，锁内）
# ------------------------------------------------------------------


def _claim_precheck(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str | None,
    project_root: Path | None,
    review_type: str | None,
    enforce_self_review_block: bool,
) -> tuple[
    dict[str, Any], str, str | None,
    list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None,
]:
    # task-flat-decl-authority：task_def 经 resolve_declaration_source 解析
    # （flat 下从 main blob 读权威声明，防任务分支本地副本陈旧）。
    degraded_guards: list[dict[str, Any]] = []
    from orchd.worktree import resolve_declaration_source
    tasks = resolve_declaration_source(project_root, tasks, degraded_guards)[0]
    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(
            ErrorCode.E005, f"task '{task_id}' not found in master",
            [{"task_id": task_id,
              "hint": f"任务 {task_id} 在 _master.json 中不存在，"
                      "检查 id 是否拼写正确或是否已注册"}])
    session_id = resolve_session_identity(store.orchd_dir)["session_id"]
    if role is None:
        pre_state = store.replay()
        pre_ts = pre_state.get(task_id)
        role = "reviewer" if (pre_ts and pre_ts.status == "in_review") else "implementer"
    auto_checkout: dict[str, Any] | None = None
    if role == "reviewer" and project_root:
        from orchd.worktree import _task_wt_name, detect_layout
        _layout = detect_layout(project_root)
        if _layout.get("layout") == "container":
            _wt_dir = _layout["task_wt_root"] / _task_wt_name(task_id)
            if not (_wt_dir / ".git").exists():
                branch_state = _try_git_branch(project_root, task_id)
                if branch_state and branch_state.get("state") == "failed":
                    degraded_guards.append(_branch_degraded_guard(task_id, branch_state))
        else:
            # task-review-auto-checkout：flat/降级无独立 worktree → 引擎自动切
            # 任务分支（+ AMEND 审计 reason=auto_branch_prepare；脏工作区 / 分支
            # 缺失时回落守卫 E017/E018）。container 行为逐字不变。
            from orchd.gitops.guard import _reviewer_auto_checkout
            auto_checkout = _reviewer_auto_checkout(
                store, project_root, task_id, agent_id, degraded_guards)
    trunk_restore: dict[str, Any] | None = None
    if role == "implementer" and project_root:
        # task-pass9-lifecycle-release-invariant：implementer claim 滞留自愈（读侧
        # 兜底）——主工作树滞留 task/{id} 且该任务无活跃认领时自动恢复 trunk
        # 基线；仍活跃则不动，交 _guard_claim 按既有语义 E018。
        from orchd.gitops.guard import _implementer_ensure_trunk
        trunk_restore = _implementer_ensure_trunk(
            store, project_root, agent_id=agent_id, degraded=degraded_guards)
    _guard_claim(
        project_root, role=role, task_id=task_id, orchd_dir=store.orchd_dir,
        agent_id=agent_id, degraded=degraded_guards)
    return task_def, role, session_id, degraded_guards, auto_checkout, trunk_restore


def _check_master_uncommitted(project_root: Path | None) -> dict[str, Any] | None:
    """task-engine-cli-friction-fix：检测主工作树 _master.json 是否有未提交改动。

    有则返回 intake_warning（hint 输出 orchd intake，而非人工 git commit）；
    无或检测失败返回 None。best-effort，不阻断认领。
    """
    if project_root is None:
        return None
    try:
        from orchd.gitops import list_tracked_changes
        dirty = list_tracked_changes(project_root)
        if dirty is None:
            return None
        master_dirty = [f for f in dirty if f.endswith("_master.json")]
        if not master_dirty:
            return None
        return {
            "type": "master_uncommitted",
            "files": master_dirty,
            "hint": (
                "_master.json 有未提交的摄入/注册产物，请运行 "
                "python .orchd/__main__.py intake 自动提交（勿人工 git commit）"
            ),
            "command": "python .orchd/__main__.py intake",
        }
    except Exception:
        return None


def _check_retract_cooldown(store: Store, task_id: str) -> dict[str, Any] | None:
    """检查任务是否在 retract 认领冷却期内（task-retract-bind-cooloff）。"""
    from datetime import datetime
    all_events = store._read_ledger_lines(from_line=1)
    last_retract = None
    last_claimed_ts = None
    for ev in all_events:
        if ev.get("task_id") != task_id:
            continue
        etype = ev.get("type")
        if etype == EVENT_RETRACT and not ev.get("retracted"):
            last_retract = ev
        elif etype == EVENT_CLAIMED and not ev.get("retracted"):
            last_claimed_ts = ev.get("timestamp")
    if last_retract is None:
        return None
    if last_claimed_ts:
        try:
            rt = datetime.fromisoformat(last_retract["timestamp"])
            ct = datetime.fromisoformat(last_claimed_ts)
            if ct > rt:
                return None
        except (ValueError, KeyError):
            pass
    disposition = last_retract.get("disposition", "abandon")
    if disposition != "abandon":
        return None
    # admin 执行的 retract 视为系统/管理员操作，不触发认领冷却（避免破坏既有测试
    # 中 admin 撤回后立即重新认领的场景；用户主动放弃由非 admin 操作者触发）。
    if last_retract.get("agent_id") == "admin":
        return None
    try:
        rt = datetime.fromisoformat(last_retract["timestamp"])
        now = datetime.now(rt.tzinfo) if rt.tzinfo else datetime.now()
        seconds_ago = (now - rt).total_seconds()
    except (ValueError, KeyError):
        return None
    if seconds_ago < _RETRACT_COOLDOWN_S:
        return {
            "retracted_at": last_retract["timestamp"],
            "seconds_ago": seconds_ago,
            "disposition": disposition,
        }
    return None
