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



def register(sub):
    """注册 session 二级子命令组（原样搬自 _build_parser，3a 纯移动，机制未变）。"""
    # session（Session Identity Layer）：引擎显式会话生命周期
    p = sub.add_parser("session", help="会话生命周期：start / current / end")
    session_sub = p.add_subparsers(dest="session_action", required=True)

    _p = session_sub.add_parser("start", help="开启新会话并输出 session_token/session_id")
    _p.add_argument("--agent", default=None, help="具名 agent（可选，如 codex-1）")
    _p.set_defaults(func=_cmd_session_start)

    _p = session_sub.add_parser("current", help="显示当前会话")
    _p.set_defaults(func=_cmd_session_current)

    _p = session_sub.add_parser("end", help="结束当前会话")
    _p.add_argument("--force", action="store_true",
                help="工作区存在未提交改动时强制放行（记录说明，审计可查）")
    _p.add_argument("--force-reason", default="",
                help="--force 放行的原因说明（写入 session runtime，审计可查）")
    _p.set_defaults(func=_cmd_session_end)


def _cmd_session_start(args) -> dict:
    """开启新的引擎级会话（Session Identity Layer）。

    CLI 参数: args.agent（可选具名 agent）。
    返回: session runtime 信息（session_id / session_token / fingerprint / path）。
    宿主接入层应将 session_token 写入 ORCHD_SESSION_ID，供后续命令解析身份。
    """
    from orchd.ledger import session_start

    orchd_dir = _find_orchd_dir()
    return session_start(orchd_dir, agent_name=args.agent)

def _cmd_session_current(args) -> dict:
    """显示当前会话 runtime 信息；未开启时返回 E033。"""
    from orchd.ledger import session_current

    orchd_dir = _find_orchd_dir()
    return session_current(orchd_dir)

def _cmd_session_end(args) -> dict:
    """结束当前会话：标记 runtime inactive + best-effort 释放 session lock。

    红线 #5 硬化（task-audit-session-end-clean-gate）：结束前校验工作区无
    已跟踪文件改动（untracked 不视为脏），脏则拒绝结束并输出待提交文件清单
    与处置指引；``--force`` 可显式放行，放行说明写入 session runtime（审计可查）。
    """
    from orchd.gitops import list_tracked_changes, release_session_lock_if_owned
    from orchd.ledger import session_end

    orchd_dir = _find_orchd_dir()
    project_root = orchd_dir.parent
    dirty_files = list_tracked_changes(project_root) or []
    forced = bool(getattr(args, "force", False))
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
                "hint": (commit_hint_for_branch(_branch)
                         + "；如确需携带未提交改动结束，请使用 --force 显式放行"
                           "（--force-reason 注明原因，审计可查）"),
            }],
        )
    force_bypass = None
    if dirty_files and forced:
        force_bypass = {
            "reason": getattr(args, "force_reason", "") or "agent 显式 --force 放行",
            "dirty_files": dirty_files,
        }
    result = session_end(orchd_dir, force_bypass=force_bypass)
    agent_id = result.get("fingerprint") or _resolve_agent_id(orchd_dir)
    result["session_lock_released"] = release_session_lock_if_owned(orchd_dir, agent_id)
    if force_bypass:
        result["force_bypass"] = force_bypass
    return result
