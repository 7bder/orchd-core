"""CLI 命令 handler（amend/retract/force-status/restore/merge-ack），依赖 amend_impl+roundtrip。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.cli.identity import _require_agent_id
from orchd.cli.skeleton import _cli_skeleton
from .amend_impl import _amend_impl
from .roundtrip import _enter_amend_roundtrip, _finish_amend_roundtrip, _rollback_amend_roundtrip


@_cli_skeleton
def _cmd_amend(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """amend 入口：flat/降级态下受管分支往返 + 增量更新 snapshot。

    task-flat-amend-channel：非 default 分支调用时——

    - container 布局（存在独立任务 worktree）→ 交 :func:`_amend_impl` 既有 E007
      守卫（提示回主工作树，零回归）；
    - flat / 降级布局 + ``--task`` 附加字段补丁 + 任务分支 → 引擎执行**受管分支
      往返**（checkout default → 写 canonical master 并提交 → checkout 回任务
      分支），落 AMEND 审计事件（``reason=implementer_amend_roundtrip``）；任一步
      失败即回滚到原任务分支并结构化上报（不静默、不留半完成态）。
    """
    from orchd.gitops import get_current_branch, get_default_branch

    caller_root = Path.cwd()
    caller_branch = get_current_branch(caller_root)
    default_branch = get_default_branch(caller_root) or "main"

    roundtrip: dict[str, Any] | None = None
    if caller_branch is not None and caller_branch != default_branch:
        roundtrip = _enter_amend_roundtrip(
            args, caller_root, caller_branch, default_branch)

    if roundtrip is None:
        # 未进入往返：既有 E007 守卫在 _amend_impl 内按原语义拒绝
        # （container 任务 worktree / 裸 amend / --register / 非任务分支）。
        return _amend_impl(args, tasks, orchd_dir, master, store, agent_id)

    try:
        result = _amend_impl(args, tasks, orchd_dir, master, store, agent_id)
    except BaseException as exc:
        _rollback_amend_roundtrip(caller_root, roundtrip, exc)
        raise
    _finish_amend_roundtrip(caller_root, roundtrip, store, agent_id)
    if isinstance(result, dict):
        result["amend_roundtrip"] = {
            "from_branch": roundtrip["from_branch"],
            "default_branch": roundtrip["default_branch"],
            "checked_out": roundtrip["from_branch"],
        }
    return result


def _cmd_retract(args) -> dict:
    from orchd.cli import _load_tasks
    """撤回已提交的事件。

    CLI 参数: args.event（可选，事件 ID 精确撤回）或 args.task + args.event_type
    （可选，按任务+类型自动定位最近匹配事件）、args.reason（必需）。
    返回: 撤回事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import retract

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)

    event_id = getattr(args, "event", None)
    task_id = getattr(args, "task", None)
    event_type = getattr(args, "event_type", None)

    if not event_id and not (task_id and event_type):
        return {
            "error": "retract 需要 --event <事件ID> 或 --task <任务ID> --type <事件类型>"
        }

    return retract(
        store,
        agent_id=agent_id,
        target_event_id=event_id,
        reason=args.reason,
        project_root=orchd_dir.parent,
        task_id=task_id,
        event_type=event_type,
        disposition=getattr(args, "disposition", None) or "abandon",
    )


def _cmd_force_status(args) -> dict:
    from orchd.cli import _load_tasks
    from orchd.cli import _maybe_archive_ideas
    """强制设置任务状态（用于恢复僵死任务或手动干预）。

    CLI 参数: args.task（必需）、args.status（必需，目标状态）、
    args.reason（必需）、args.assignee（可选，指定认领人）、
    args.force（可选，逃生口二次确认——claimed→completed / cancelled→pending）、
    args.evidence_sha（可选，completed→pending 复活所需的 git 证据 commit SHA）、
    args.force_recycle（可选，终态回收时显式丢弃未合并的 task/<id> 分支——用
    git branch -D，提交不可恢复；缺省拒绝删除未合并分支并留痕 branch_delete_refused）。
    agent 身份由引擎自动按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），
    不再有 --agent。
    返回: 强制状态变更事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import force_status

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    result = force_status(
        store,
        agent_id=agent_id,
        task_id=args.task,
        target_status=args.status,
        reason=args.reason,
        assignee=args.assignee,
        force=args.force,
        project_root=orchd_dir.parent,
        evidence_sha=args.evidence_sha,
        force_recycle=getattr(args, "force_recycle", False),
    )
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("new_status") == "cancelled":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _cmd_restore(args) -> dict:
    """受管工作树还原（task-restore-channel）：指定路径回到 HEAD，不碰历史。

    CLI 参数: args.path（必需，相对仓库根的文件路径，可多个）。
    返回: ``{"restored": [...], "project_root": str}``；任一路径被拒则整体
    E007（一个也不执行）。目录 / 仓库外 / 未跟踪新建一律拒绝。
    """
    from orchd.cli import _load_tasks
    from orchd.gitops.guard import restore_worktree_paths

    _, orchd_dir, _ = _load_tasks()
    agent_id = _require_agent_id(orchd_dir)
    result = restore_worktree_paths(
        orchd_dir.parent, list(getattr(args, "path", None) or []))
    result["agent_id"] = agent_id
    return result


def _cmd_merge_ack(args) -> dict:
    from orchd.cli import _load_tasks
    """merge_warning 人工销账：登记 .orchd/merge-acks.json（task-merge-warning-ack）。

    CLI 参数: args.task（必需，已人工确认的 task_id）、args.reason（必需，确认原因）。
    与 resolve_sha 自动销账互补：人工路径兜底旧事件 / 无 sha 场景，登记后
    audit-merge 不再报 merge_warning_unresolved。
    返回: 登记结果 {acked, task_id, acked_at, reason}。
    """
    from orchd.report import merge_ack

    _, orchd_dir, _ = _load_tasks()
    return merge_ack(orchd_dir.parent, args.task, args.reason)
