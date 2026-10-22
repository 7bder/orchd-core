"""amend 往返（roundtrip）事务族，段内自依赖。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError


def _amend_roundtrip_applicable(
    caller_root: Path,
    caller_branch: str,
    patch_task: str | None,
    args: Any,
) -> bool:
    """判定是否进入 amend 受管分支往返（task-flat-amend-channel AC1/AC3）。

    适用条件（缺一即 False，交 :func:`_amend_impl` 既有 E007 守卫）：

    - ``--task`` 补丁通道：裸 amend / ``--register`` / ``--revise-terminal`` 不走
      往返——注册与终态文本修订超出「附加字段补丁域」，属补丁越域；
    - 当前分支为任务分支（单根命名空间 ``task/`` 下，``task/{id}`` 或
      ``task/{line}/{id}``）；
    - 非 container 布局的独立任务 worktree（flat / 降级）——container 下任务
      worktree 走既有「cd 主工作树」路径，零回归。

    说明：CLI 补丁域的主体 ⊆ ``schema_policy.amendable_fields``，唯一的例外是
    pending 限定的 ``--acceptance-criteria``（task-amend-ac-pending-channel：
    独立旗标 + 显式 pending 校验，与白名单通道正交）；实际矩阵仍由
    ``split.amend`` 强制执行；本判据只决定「是否走往返」，不放宽 amend 允许集。
    """
    if patch_task is None:
        return False
    if getattr(args, "register", None) is not None:
        return False
    if getattr(args, "revise_terminal", None) is not None:
        return False
    from orchd.line import parse_task_branch

    if parse_task_branch(caller_branch) is None:
        return False
    # container 布局的任务 worktree 是 linked worktree（``git rev-parse --git-dir``
    # 含 ``worktrees/``）——不触发往返，走既有「cd 主工作树」E007 路径（零回归）。
    # flat / 降级（单 worktree，含 container 主工作树）→ 触发。判据不依赖布局标记
    # （任务 worktree 可能未携带标记），与 ``is_task_worktree`` 单一真源一致。
    try:
        from orchd.gitops import is_task_worktree

        if is_task_worktree(caller_root):
            return False
    except Exception:
        return False
    return True


def _enter_amend_roundtrip(
    args: Any,
    caller_root: Path,
    caller_branch: str,
    default_branch: str,
) -> dict[str, Any] | None:
    """进入 amend 受管分支往返：干净校验（E017）+ ``git checkout <default>``。

    Returns:
        往返上下文 ``{"from_branch", "default_branch", "task_id"}``；不适用返回
        None（交既有 E007 守卫）。
    """
    patch_task = getattr(args, "task", None)
    if not _amend_roundtrip_applicable(caller_root, caller_branch, patch_task, args):
        return None
    from orchd.gitops.guard import managed_checkout_branch

    outcome = managed_checkout_branch(caller_root, default_branch, command="amend")
    if not outcome.get("ok"):
        raise OrchdError(
            ErrorCode.E018,
            "amend_roundtrip_enter_failed: 受管分支往返无法切到 default"
            f"（{default_branch}）分支",
            [{
                "command": "amend",
                "from_branch": caller_branch,
                "target_branch": default_branch,
                "reason": outcome.get("reason"),
                "hint": (
                    "往返入口切换失败（工作区脏 / 分支缺失 / git 故障）；本次未写"
                    "任何内容，可安全重试。脏工作区请先在任务分支提交"
                    "（git add + git commit，红线 #1 唯一豁免）或 orchd restore 还原"
                ),
            }],
        )
    return {
        "from_branch": caller_branch,
        "default_branch": default_branch,
        "task_id": patch_task,
    }


def _emit_amend_roundtrip_event(
    store,
    agent_id: str | None,
    roundtrip: dict[str, Any],
) -> None:
    """落 AMEND 审计事件（``reason=implementer_amend_roundtrip``；best-effort）。"""
    try:
        from orchd.gitops_ops import make_event

        _agent = agent_id if isinstance(agent_id, str) else ""
        store.acquire_lock()
        try:
            ev = make_event(
                roundtrip["task_id"], _agent, "AMEND",
                reason="implementer_amend_roundtrip",
                branch=roundtrip["from_branch"],
                from_branch=roundtrip["default_branch"],
                hint=("flat/降级布局下实现者持任务分支执行 amend --task：引擎受管"
                      "往返（切到 default 写 canonical master 并提交后切回任务分支）"),
            )
            store.append_event(ev)
            store.update_checkpoint(store.replay())
        finally:
            store.release_lock()
    except Exception:
        pass  # best-effort：审计落账失败不阻断 amend（往返本身已生效）


def _finish_amend_roundtrip(
    caller_root: Path,
    roundtrip: dict[str, Any],
    store,
    agent_id: str | None,
) -> None:
    """成功路径：checkout 回任务分支，并落 AMEND 审计事件。"""
    from orchd.gitops.guard import managed_checkout_branch

    from_branch = roundtrip["from_branch"]
    outcome = managed_checkout_branch(caller_root, from_branch, command="amend")
    if not outcome.get("ok"):
        raise OrchdError(
            ErrorCode.E018,
            "amend_roundtrip_exit_failed: amend 已写入 canonical master，但受管往返"
            f"未能切回任务分支 {from_branch}",
            [{
                "command": "amend",
                "task_id": roundtrip.get("task_id"),
                "from_branch": roundtrip.get("default_branch"),
                "target_branch": from_branch,
                "reason": outcome.get("reason"),
                "hint": (
                    "amend 已提交到 default 分支（未丢），但工作区仍停留在 "
                    f"{roundtrip.get('default_branch')}；请重试本命令以完成切回，"
                    "或报告人工处置"
                ),
            }],
        )
    _emit_amend_roundtrip_event(store, agent_id, roundtrip)


def _rollback_amend_roundtrip(
    caller_root: Path,
    roundtrip: dict[str, Any],
    cause: BaseException,
) -> None:
    """失败路径：回滚到原任务分支；回滚失败结构化上报（链式原始异常）。"""
    from orchd.gitops.guard import managed_checkout_branch

    from_branch = roundtrip["from_branch"]
    try:
        outcome = managed_checkout_branch(caller_root, from_branch, command="amend")
        ok = bool(outcome.get("ok"))
        reason = outcome.get("reason")
    except Exception as exc:
        ok = False
        reason = f"{type(exc).__name__}: {exc}"[:200]
    if ok:
        return
    raise OrchdError(
        ErrorCode.E018,
        "amend_roundtrip_rollback_failed: 受管往返失败后无法切回任务分支 "
        f"{from_branch}（半完成态，须人工核对）",
        [{
            "command": "amend",
            "task_id": roundtrip.get("task_id"),
            "from_branch": roundtrip.get("default_branch"),
            "target_branch": from_branch,
            "reason": reason,
            "cause": f"{type(cause).__name__}: {cause}"[:300],
            "hint": (
                "受管往返的 amend 步骤失败且回滚未成功：工作区可能停留在 "
                f"{roundtrip.get('default_branch')}。请核对分支后重试或报告人工处置"
            ),
        }],
    ) from cause
