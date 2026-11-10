"""Orchd CLI 路由：session 命令 handler + 二级子命令注册。

迁移自 orchd/cli.py（task-split-cli-cmds-ideas-session-lessons）：
  - _cmd_session_start: session start（开启新会话）
  - _cmd_session_current: session current（显示当前会话）
  - _cmd_session_end: session end（结束当前会话）
  - register: session 二级子命令组注册（原样搬自 _build_parser）

打点约定（task-cli-session-test-rebind，2026-09-10）：3a 收尾已删除 legacy
``orchd/cli.py``，本模块是 session 命令的唯一运行时实现。**cli/commands 内的
monkeypatch 打点目标必须是命令模块自身的命名空间**（如
``orchd.cli.commands.session._find_orchd_dir``），不得打在 ``orchd.cli`` 包
聚合命名空间或 ``orchd.cli._util`` 源模块上——本模块对 ``_find_orchd_dir``
等辅助函数做模块级 from-import 绑定，打在包/源模块命名空间不会生效，会击穿
测试隔离（session runtime 落到真实账本根）。新增/拆分 cli 命令模块时沿用本约定。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.cli.identity import _resolve_agent_id
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)
from orchd.contracts.responses import (
    COMMAND_KEY,
    HINT_KEY,
    NEXT_ACTION_KEY,
)



def register(sub):
    """注册 session 二级子命令组（原样搬自 _build_parser，3a 纯移动，机制未变）。"""
    # ``ledger`` 对本模块是 deps.json 里的 **delayed** 边，取值必须走函数内 import
    # （提到模块级会把延迟边变硬边，被依赖方向门禁判红）；取值域用真源常量，不另立词表
    from orchd.ledger import SESSION_KINDS

    # session（Session Identity Layer）：引擎显式会话生命周期
    p = sub.add_parser("session", help="会话生命周期：start / current / end")
    session_sub = p.add_subparsers(dest="session_action", required=True)

    _p = session_sub.add_parser("start", help="开启新会话并输出 session_token/session_id")
    _p.add_argument("--agent", default=None, help="具名 agent（可选，如 codex-1）")
    _p.add_argument("--kind", default=None, choices=list(SESSION_KINDS),
                    help="会话角色声明（lineage 观测面，可选）：实现者会话给独立 subagent "
                         "取号时填 reviewer；缺省不填，认领审查时会据此发未声明告警")
    _p.add_argument("--parent", default=None,
                    help="父会话 session_id（lineage 观测面，可选）：subagent 填派发它的会话 id；"
                         "仅接受 12–64 位小写 hex，非法值 E007")
    _p.add_argument("--adopt", action="store_true",
                    help="为当前 ORCHD_SESSION_ID 指向的 token 建立/复用 runtime，不生成"
                         "第二身份：已有 active runtime 原样复用，无则按该 token 补建"
                         "（宿主接入层对已注入 token 的宿主用此形态取号）")
    _p.set_defaults(func=_cmd_session_start)

    _p = session_sub.add_parser("current", help="显示当前会话")
    _p.set_defaults(func=_cmd_session_current)

    _p = session_sub.add_parser("end", help="结束当前会话")
    _p.add_argument("--force", action="store_true",
                help="工作区存在未提交改动时强制放行（记录说明，审计可查）")
    _p.add_argument("--force-reason", default="",
                help="--force 放行的原因说明（写入 session runtime，审计可查）")
    _p.set_defaults(func=_cmd_session_end)


def _local_auto_clean(project_root: Path,
                      *,
                      trigger: str,
                      result: dict,
                      session_id: str | None = None,
                      task_id: str | None = None,
                      exclude_paths: list[Path] | None = None) -> None:
    """局部 auto_clean 触发（设计 §6.3 session 触发点）。

    只清理本会话/本任务相关残留，按 ``trigger`` 上限执行；best-effort——任何异常
    与失败都不影响 session 命令本身（结果仅在确有动作时并入 ``result["auto_clean"]``）。

    ``exclude_paths`` 用于排除本轮刚写下的审计件（如 session end 的 runtime json），
    避免它被同一轮当成 zombie 清掉导致留痕丢失。
    """
    try:
        from orchd.doctor import auto_clean

        ac = auto_clean(
            project_root,
            emit_stderr=False,
            trigger=trigger,
            task_id=task_id,
            session_id=session_id,
            exclude_paths=exclude_paths,
        )
        if ac.get("auto_cleaned") or ac.get("auto_moved") or ac.get("manual_notice"):
            result["auto_clean"] = ac
    except Exception:
        pass


def _session_runtime_exclude(orchd_dir: Path, session_id: str) -> list[Path]:
    """本轮清理须排除的审计件：刚写下的 ``<store>/sessions/<sid>.json``。

    store 根与检测器同源（``resolve_store_dir``）：container 布局落
    ``<容器>/.orchd-runtime``、flat 落 ``<root>/.orchd``；解析失败时返回空表
    （宁可少排除，也不猜错根）——此时 auto_clean 仍按局部范围执行。
    """
    if not session_id:
        return []
    try:
        from orchd.ledger import resolve_store_dir

        return [resolve_store_dir(orchd_dir) / "sessions" / f"{session_id}.json"]
    except Exception:
        return []


def _cmd_session_start(args) -> dict:
    """开启新的引擎级会话（Session Identity Layer）。

    CLI 参数: args.agent（可选具名 agent）；args.kind / args.parent（lineage 观测面，
    可选——档 1 只记录不判定，见 ``orchd/ledger/identity.py::session_start``）；
    args.adopt（可选，task-cont-session-adopt-end-guard）：为当前 ORCHD_SESSION_ID
    指向的 token 建立/复用 runtime，不生成新 token / 第二身份。
    返回: session runtime 信息（session_id / session_token / fingerprint / kind /
    parent_session_id / path；adopt 时附 adopted / reused 标记）。
    宿主接入层应将 session_token 写入 ORCHD_SESSION_ID，供后续命令解析身份。

    task-cont-autoclean-triggers：会话启动即触发一次本会话局部清理（上限 10 项）。
    """
    from orchd.ledger import session_start

    orchd_dir = _find_orchd_dir()
    result = session_start(
        orchd_dir,
        agent_name=args.agent,
        kind=args.kind,
        parent_session_id=args.parent,
        adopt=bool(getattr(args, "adopt", False)),
    )
    _local_auto_clean(
        orchd_dir.parent,
        trigger="session",
        result=result,
        session_id=result.get("session_id"),
    )
    return result

def _cmd_session_current(args) -> dict:
    """显示当前会话 runtime 信息；未开启时返回 E033。"""
    from orchd.ledger import session_current

    orchd_dir = _find_orchd_dir()
    return session_current(orchd_dir)

def _claim_next_action(claimed_tasks: list[dict[str, Any]]) -> dict[str, str]:
    """把活跃认领翻译成可直接执行的下一步（命令用 task id，不留事件 id 占位符）。"""
    first = claimed_tasks[0]
    task_id = first["task_id"]
    if first.get("phase") == "review":
        phase = str(first.get("review_phase") or "unified")
        # review 的 --type choices 仅 spec/code；unified 单阶段须省略该参数
        # （与 orchd/guide/stage.py 的 submit_review 构造同口径）。
        type_arg = "" if phase == "unified" else f" --type {phase}"
        return {
            "action": "review",
            COMMAND_KEY: (
                "python .orchd/__main__.py review --task "
                f"{task_id}{type_arg} "
                "--verdict '<APPROVED|CHANGES_REQUESTED>' "
                "--comments '<审查意见>'"
            ),
            "reason": (
                f"任务 {task_id} 仍处于 in_review（{phase}）且审查由本会话"
                "认领：先按实际结论提交 review 或撤认后再结束会话"
            ),
        }
    return {
        "action": "done",
        COMMAND_KEY: (
            "python .orchd/__main__.py done --task "
            f"{task_id} --changes '<本次变更摘要>'"
        ),
        "reason": (
            f"任务 {task_id} 仍处于 claimed：先完成 done 闭环或撤认后再结束会话"
        ),
    }


def _retract_commands(claimed_tasks: list[dict[str, Any]]) -> list[str]:
    """为每个活跃认领生成可复制的撤认命令（--task + --type 自动定位事件）。"""
    return [
        (
            "python .orchd/__main__.py retract --task "
            f"{c['task_id']} --type "
            f"{'REVIEW_CLAIMED' if c.get('phase') == 'review' else 'CLAIMED'} "
            "--reason '<撤认原因>'"
        )
        for c in claimed_tasks
    ]


def _cmd_session_end(args) -> dict:
    """结束当前会话：标记 runtime inactive + best-effort 释放 session lock。

    红线 #5 硬化（task-audit-session-end-clean-gate）：结束前校验工作区无
    已跟踪文件改动（untracked 不视为脏），脏则拒绝结束并输出待提交文件清单
    与处置指引；``--force`` 可显式放行，放行说明写入 session runtime（审计可查）。

    活跃认领守卫（task-cont-session-adopt-end-guard）：当前会话名下存在活跃
    认领（实现 claimed / 审查 in_review 已领审）时拒绝结束并列出 claim 任务与
    处置指引（done / retract 后再结束）；``--force`` 显式放行并把清单写入
    force_bypass 留痕（审计可查）。
    """
    from orchd.gitops import list_tracked_changes, release_session_lock_if_owned
    from orchd.ledger import (
        active_claims_for_session, resolve_session_identity, session_end,
    )

    orchd_dir = _find_orchd_dir()
    project_root = orchd_dir.parent
    dirty_files = list_tracked_changes(project_root) or []
    forced = bool(getattr(args, "force", False))
    identity = resolve_session_identity(orchd_dir)
    claimed_tasks = active_claims_for_session(
        orchd_dir, identity.get("session_id") or "")
    if claimed_tasks and not forced:
        _listing = "、".join(
            f"{c['task_id']}（{c['phase']}）" for c in claimed_tasks)
        raise OrchdError(
            ErrorCode.E017,
            "active_claim_at_session_end: 当前会话存在活跃认领，拒绝结束会话",
            [{
                "claimed_tasks": claimed_tasks,
                NEXT_ACTION_KEY: _claim_next_action(claimed_tasks),
                "retract_commands": _retract_commands(claimed_tasks),
                HINT_KEY: (
                    f"活跃认领：{_listing}。先按 next_action 闭环（实现认领走 done，"
                    "审查认领走 review）；不再继续的任务用 retract_commands "
                    "中的命令撤认（--task + --type 自动"
                    "定位 CLAIMED/REVIEW_CLAIMED 事件，无需先查事件 id；"
                    f"'orchd show {claimed_tasks[0]['task_id']}' 可回看事件）。"
                    "如确需带活跃认领强制结束，请使用 --force（--force-reason "
                    "注明原因，审计可查）"
                ),
            }],
        )
    if dirty_files and not forced:
        from orchd.gitops.guard import commit_hint_for_branch
        try:
            from orchd.gitops import get_current_branch
            _branch = get_current_branch(project_root)
        except Exception:
            _branch = None
        raise OrchdError(
            ErrorCode.E017,
            "dirty_workspace_at_session_end: 工作区存在未提交的已跟踪文件改动，拒绝结束会话",
            [{
                "dirty_files": dirty_files,
                HINT_KEY: (commit_hint_for_branch(_branch)
                         + "；如确需携带未提交改动结束，请使用 --force 显式放行"
                           "（--force-reason 注明原因，审计可查）"),
            }],
        )
    force_bypass = None
    if (dirty_files or claimed_tasks) and forced:
        force_bypass = {
            "reason": getattr(args, "force_reason", "") or "agent 显式 --force 放行",
            "dirty_files": dirty_files,
        }
        if claimed_tasks:
            force_bypass["claimed_tasks"] = claimed_tasks
            force_bypass["claim_semantics"] = {
                "kept": [c["task_id"] for c in claimed_tasks],
                "released": [],
                "note": (
                    "任务保持 claimed/in_review，不随 session end 释放；"
                    "由 watchdog 按 CLAIMED/REVIEW_CLAIMED 超时回收"
                ),
            }
    result = session_end(orchd_dir, force_bypass=force_bypass)
    agent_id = result.get("fingerprint") or _resolve_agent_id(orchd_dir)
    result["session_lock_released"] = release_session_lock_if_owned(orchd_dir, agent_id)
    if force_bypass:
        result["force_bypass"] = force_bypass
    # task-cont-autoclean-triggers：会话结束触发本会话局部清理（上限 10 项）。
    # 本轮刚结束的 session runtime 是审计件（force_bypass 等留痕），显式排除。
    sid = identity.get("session_id") or ""
    _local_auto_clean(
        project_root,
        trigger="session",
        result=result,
        session_id=sid,
        exclude_paths=_session_runtime_exclude(orchd_dir, sid) or None,
    )
    return result
