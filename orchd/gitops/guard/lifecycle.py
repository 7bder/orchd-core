"""gitops guard — 任务生命周期释放守卫域（W6-C1 拆分，纯搬移）。

来源：``orchd/gitops/guard.py``（1652 行，W6-C1 纯搬移）。

``release_task_lifecycle`` / ``compat_checked_out_main`` /
``_audit_lifecycle_release`` / ``_implementer_ensure_trunk`` /
``_audit_trunk_restore``。

依赖 ``checkout``（``managed_checkout_branch``，顶层导入，单向）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

from orchd.gitops.query import check_workspace_state, get_default_branch, is_task_worktree

from orchd.line_ctx import resolve_trunk_for

from .checkout import managed_checkout_branch

# ------------------------------------------------------------------
# 生命周期终态收敛（task-pass9-lifecycle-release-invariant）
# ------------------------------------------------------------------

def release_task_lifecycle(
    store,
    project_root: Path | None,
    *,
    task_id: str,
    agent_id: str | None = None,
    command: str,
    mode: str = "abandon",
) -> dict[str, Any]:
    """生命周期终态收敛点：任务离开活跃态后恢复主工作树 trunk 基线（单一出口）。

    不变量（task-pass9-lifecycle-release-invariant）：主工作树停留 ``task/{id}``
    分支当且仅当该任务存在活跃认领。凡写终态事件（DONE / RETRACT /
    FORCE_STATUS / 打回 REVIEW_SUBMITTED）的路径必须经本函数恢复分支基线，
    由 tests/contract/test_lifecycle_release_invariant.py AST 锁死。

    两种模式（语义差异刻意保留，不得互相吞并）：

    - ``mode="strict"``（终态先切后写：done / review 打回）：切换失败抛
      E017/E018 阻断，调用方**不写事件**，可安全重试、无中间态；
    - ``mode="abandon"``（放弃态先写后尽力切：retract / force-status）：放弃
      认领正是解困动作，不被 git 故障阻断——任何失败降级为结构化
      ``stranded`` 明细随响应透出（账本事件已落账不受影响；历史滞留由读侧
      自愈 :func:`_implementer_ensure_trunk` 兜底消化）。

    底层统一 :func:`managed_checkout_branch`（与 reviewer auto-checkout /
    amend 受管往返同一实现，禁第三套切换逻辑）。

    Returns:
        ``{"checked_out_main": <trunk|None>, "changed": bool,
        "stranded": <dict|None>, "skip_reason": <str|None>}``。
    """
    result: dict[str, Any] = {
        "checked_out_main": None, "changed": False,
        "stranded": None, "skip_reason": None,
    }
    if project_root is None:
        result["skip_reason"] = "no_project_root"
        return result
    try:
        if is_task_worktree(Path(project_root)):
            # 任务 worktree 恒 checkout task/{id}（AC3），trunk 基线由主工作树承担。
            result["skip_reason"] = "task_worktree"
            return result
        # 与 checkout_default_strict 同序：先判定 git 可用性（无 git 概念可切换），
        # 再判定默认分支——nogit 下 skip 口径为 git_unavailable（两处一致）。
        if not check_workspace_state(Path(project_root)).get("available"):
            result["skip_reason"] = "git_unavailable"
            return result
        default = get_default_branch(Path(project_root))
    except Exception as exc:
        if mode == "strict":
            raise
        result["stranded"] = {"reason": "probe_failed", "detail": f"{exc}"[:200]}
        return result
    if not default:
        # 与 checkout_default_strict 的 skipped 降级契约一致：无默认分支概念则跳过。
        result["skip_reason"] = "no_default_branch"
        return result
    try:
        outcome = managed_checkout_branch(Path(project_root), default, command=command)
    except OrchdError as exc:
        # 脏工作区：managed_checkout_branch 抛 E017。abandon 模式降级为 stranded
        # （脏改动留在任务分支上，分支尚在可日后收拾）；strict 原样上抛阻断。
        if mode == "strict":
            raise
        result["stranded"] = {"reason": "dirty_workspace", "detail": f"{exc}"[:200]}
        return result
    except Exception as exc:
        if mode == "strict":
            raise
        result["stranded"] = {"reason": "checkout_failed", "detail": f"{exc}"[:200]}
        return result
    if outcome.get("ok"):
        result["checked_out_main"] = outcome.get("checked_out")
        result["changed"] = bool(outcome.get("changed"))
        if result["changed"]:
            _audit_lifecycle_release(
                store, task_id, agent_id, command,
                outcome.get("checked_out"), outcome.get("from_branch"),
            )
        return result
    if outcome.get("reason") == "not_available":
        # 无 git 概念可切换（非 git 仓库 / git 不可用）：沿用
        # checkout_default_strict 的 skipped 契约（task-pass9-done-review-finalizer-unify），
        # 不视为切换失败——strict 亦不阻断（nogit done 照常推进），abandon 同理
        # 不再记 stranded（无滞留对象可言）。
        result["skip_reason"] = "git_unavailable"
        return result
    if mode == "strict":
        raise OrchdError(
            ErrorCode.E018,
            f"{command}_switch_branch: 受管切回默认分支失败（{outcome.get('reason')}）",
            [{"task_id": task_id, "reason": outcome.get("reason"),
              "hint": "strict 模式下终态事件未写，处理 git 状态后可安全重试"}],
        )
    else:
        result["stranded"] = {
            "from_branch": outcome.get("from_branch"),
            "reason": outcome.get("reason"),
        }
    return result

def compat_checked_out_main(release: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 :func:`release_task_lifecycle` 结果译为旧 ``checked_out_main`` 响应形状
    （task-pass9-done-review-finalizer-unify）。

    done / review CHANGES_REQUESTED 迁移到统一出口后，响应字段保持兼容：
    切换成功 → ``{"checked_out_to": <trunk>}``；跳过 → ``{"skipped": True,
    "reason": ...}``；无调用（project_root 为空）→ None（字段省略）。
    形状翻译唯一实现于此，调用点不得手写分支拼形状。
    """
    if not isinstance(release, dict):
        return None
    if release.get("skip_reason") is not None:
        return {"skipped": True, "reason": release["skip_reason"]}
    if release.get("checked_out_main") is not None:
        return {"checked_out_to": release["checked_out_main"]}
    return None

def _audit_lifecycle_release(
    store,
    task_id: str,
    agent_id: str | None,
    command: str,
    trunk: str | None,
    from_branch: str | None,
) -> None:
    """落 AMEND 审计事件（reason=lifecycle_release）。

    best-effort，与 reviewer auto-checkout 的审计同形。
    """
    try:
        from orchd.gitops_ops import make_event

        _audit_agent = agent_id if isinstance(agent_id, str) else ""
        store.acquire_lock()
        try:
            ev = make_event(
                task_id, _audit_agent, "AMEND",
                reason="lifecycle_release",
                branch=trunk,
                from_branch=from_branch,
                hint=f"{command} 生命周期释放：主工作树恢复 trunk 基线",
            )
            store.append_event(ev)
            store.update_checkpoint(store.replay())
        finally:
            store.release_lock()
    except Exception:
        pass  # best-effort：审计落账失败不阻断（分支恢复本身已生效）

def _implementer_ensure_trunk(
    store,
    project_root: Path | None,
    *,
    agent_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """implementer claim 滞留自愈（对称 :func:`_reviewer_auto_checkout`，读侧兜底）。

    历史滞留 / 漏网路径把主工作树留在 ``task/{id}`` 分支时，implementer claim
    前自愈：``task/`` 单根命名空间（pass8 F1-A）+ 工作区干净 + 分支对应任务
    **不在活跃态**（claimed / in_review 一律不动，防干扰他人 flat 在途实现）
    → 受管切回 trunk + AMEND 审计（``reason=auto_trunk_restore``）+ degraded
    留痕。任务仍活跃则不碰，交 :func:`guard_claim` 按既有语义 E018（hint 含
    恢复出路）。自愈永不阻断 claim 主流程（任何异常静默返回 None）。
    """
    if project_root is None:
        return None
    try:
        if is_task_worktree(Path(project_root)):
            return None  # 任务 worktree 恒 task/{id}，不存在「滞留」概念
        state = check_workspace_state(Path(project_root))
        if not state.get("available") or not state.get("clean"):
            return None
        branch = state.get("branch")
        if not isinstance(branch, str) or not branch.startswith("task/"):
            return None
        stranded_task_id = branch[len("task/"):].rsplit("/", 1)[-1]
        if not stranded_task_id:
            return None
        st = store.replay().get(stranded_task_id)
        if st is not None and st.status in ("claimed", "in_review"):
            return None  # 他人认领/审查在途：不自愈，交守卫 E018 + 指引
        trunk = resolve_trunk_for(Path(project_root))
        outcome = managed_checkout_branch(Path(project_root), trunk, command="claim")
        if not outcome.get("ok") or not outcome.get("changed"):
            return None
        from_branch = outcome.get("from_branch")
        _audit_trunk_restore(store, stranded_task_id, agent_id, trunk, from_branch)
        info = {"checked_out_trunk": trunk, "from_branch": from_branch}
        if degraded is not None:
            degraded.append({
                "guard": "implementer_trunk_restore",
                "task_id": stranded_task_id, **info,
            })
        return info
    except Exception:
        return None  # 自愈永不阻断 claim 主流程

def _audit_trunk_restore(
    store,
    task_id: str,
    agent_id: str | None,
    trunk: str,
    from_branch: str | None,
) -> None:
    """落 AMEND 审计事件（reason=auto_trunk_restore；best-effort，对称 auto_branch_prepare）。"""
    try:
        from orchd.gitops_ops import make_event

        _audit_agent = agent_id if isinstance(agent_id, str) else ""
        store.acquire_lock()
        try:
            ev = make_event(
                task_id, _audit_agent, "AMEND",
                reason="auto_trunk_restore",
                branch=trunk,
                from_branch=from_branch,
                hint="implementer claim 检测到滞留任务分支（无活跃认领），自动恢复 trunk 基线",
            )
            store.append_event(ev)
            store.update_checkpoint(store.replay())
        finally:
            store.release_lock()
    except Exception:
        pass  # best-effort：审计落账失败不阻断认领（切换本身已生效）
