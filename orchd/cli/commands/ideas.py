"""Orchd CLI 路由：ideas 命令 handler + 二级子命令注册。

迁移自 orchd/cli.py（task-split-cli-cmds-ideas-session-lessons）：
  - _cmd_ideas_archive: ideas-archive 一级命令（自动归档已完结 IDEAS 条目）
  - _cmd_idea_propose: idea propose（为灵感追加 status: study 条目）
  - _cmd_idea_confirm: idea confirm（study → pending）
  - _cmd_idea_drop: idea drop（study → dropped）
  - register: idea 二级子命令组注册（原样搬自 _build_parser）

3a 阶段说明：本模块是 ideas 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)



def register(sub):
    """注册 ideas-archive 一级命令 + idea 二级子命令组（原样搬自 _build_parser，3a 纯移动，机制未变）。"""
    # ideas-archive 一级命令（handler 住在本模块，注册也随模块走）
    p = sub.add_parser("ideas-archive", help="自动归档已完结的 IDEAS 条目")
    p.set_defaults(func=_cmd_ideas_archive)

    # idea（2026-08-15 idea-write-gate）：灵感写入 IDEAS 写入门禁（propose / confirm / drop）
    p = sub.add_parser("idea", help="灵感写入 IDEAS 写入门禁：propose 记入 study，confirm/drop 裁决")
    idea_sub = p.add_subparsers(dest="idea_action", required=True)

    _p = idea_sub.add_parser("propose", help="为灵感追加 status: study 条目到 IDEAS.md（agent 执行）")
    _p.add_argument("--title", required=True, help="灵感标题（须内嵌「（id: <slug>）」后缀声明条目 id，如“标题（id: my-idea）”；缺 id 即 missing_idea_id 拒绝）")
    _p.add_argument("--feasibility", required=True, help="可行性论证（写入 - 论证: 字段）")
    _p.set_defaults(func=_cmd_idea_propose)

    _p = idea_sub.add_parser("confirm", help="将 status: study 条目升为 pending（仅用户执行）")
    _p.add_argument("--title", required=True, help="灵感标题（完整标题含「（id: <slug>）」后缀，或去日期前缀标题，或裸 slug；not_found 时返回近似候选）")
    _p.set_defaults(func=_cmd_idea_confirm)

    _p = idea_sub.add_parser("drop", help="将 status: study 条目降为 dropped（仅用户执行）")
    _p.add_argument("--title", required=True, help="灵感标题（完整标题含「（id: <slug>）」后缀，或去日期前缀标题，或裸 slug；not_found 时返回近似候选）")
    _p.set_defaults(func=_cmd_idea_drop)


def _cmd_ideas_archive(args) -> dict:
    """手动触发 IDEAS 自动归档（一次性回填存量条目 + 后续可手动触发）。

    CLI 参数: 无。
    返回: 归档结果字典（archived 标题列表 + kept 数量 + 可选 commit 字段）。
    """
    from orchd.cli import _load_tasks
    from orchd.cli import _maybe_archive_ideas
    _, orchd_dir, _ = _load_tasks()
    return _maybe_archive_ideas(orchd_dir)

def _cmd_idea_propose(args) -> dict:
    """为灵感追加 status: study 条目到 IDEAS.md（idea-write-gate，agent 执行）。

    CLI 参数: args.title / args.feasibility。
    返回: 提案结果字典（proposed / title / commit）；被拒时（missing_idea_id /
    duplicate 等）附顶层 error 键，退出码非零（task-cli-exit-honesty，
    E-15：失败不再静默 exit 0）。
    """
    from orchd.intake import idea_propose

    orchd_dir = _find_orchd_dir()
    result = idea_propose(orchd_dir.parent, args.title, args.feasibility)
    if not result.get("proposed"):
        result["error"] = {
            "code": "idea_rejected",
            "reason": result.get("reason", "unknown"),
            "hint": result.get("hint", ""),
        }
    return result

def _cmd_idea_confirm(args) -> dict:
    """将 status: study 条目升为 pending（idea-write-gate，仅用户执行）。

    CLI 参数: args.title。
    返回: 确认结果字典（confirmed / title / new_status / commit）。
    """
    from orchd.intake import idea_confirm

    orchd_dir = _find_orchd_dir()
    return idea_confirm(orchd_dir.parent, args.title)

def _cmd_idea_drop(args) -> dict:
    """将 status: study 条目降为 dropped（idea-write-gate，仅用户执行）。

    CLI 参数: args.title。
    返回: 丢弃结果字典（dropped / title / new_status / commit）。
    """
    from orchd.intake import idea_drop

    orchd_dir = _find_orchd_dir()
    return idea_drop(orchd_dir.parent, args.title)
