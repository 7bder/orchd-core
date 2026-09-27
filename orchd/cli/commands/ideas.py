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

    _p = idea_sub.add_parser("append", help="按 - id: 定位条目并追加时间戳 notes 行（替代手改 IDEAS.md）")
    _p.add_argument("--id", required=True, help="条目 - id: 字段精确值（可用 ideas list 查询）")
    _p.add_argument("--notes", required=True, help="追记内容（自动加 UTC 时间戳前缀写入）")
    _p.set_defaults(func=_cmd_idea_append)

    # ideas 一级命令组（unified-intake-eng-ideas）：只读盘点，补齐 intake
    # not_found hint 引用的 ideas list 断链（此前被引用但不存在）。
    p = sub.add_parser("ideas", help="IDEAS 台账只读盘点")
    ideas_sub = p.add_subparsers(dest="ideas_action", required=True)

    _p = ideas_sub.add_parser("list", help="列出全部条目（title/id/status，只读 JSON）")
    _p.set_defaults(func=_cmd_ideas_list)


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


def _strip_idea_html_comments(text: str) -> str:
    """剔除 IDEAS.md 头部 HTML 注释（含格式示例），只留真实条目。

    intake 摄入协议前置过滤口径：HTML 注释块内的 ``## `` 行是格式示例，
    不是条目——list 输出供 agent 解析，必须与摄入口径一致。
    """
    import re

    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def ideas_list(project_root) -> dict[str, Any]:
    """列出 IDEAS.md 全部条目（只读：无锁、无提交、任意分支可用）。

    Args:
        project_root: 仓库根目录。

    Returns:
        ``{"ideas": [{"title", "id", "status"}], "count": n}``，永不抛异常；
        IDEAS.md 缺失 / 不可读 → 空列表（可解析的零结果，而非错误）。
    """
    from orchd.ideas import parse_ideas
    from orchd.ledger import resolve_workspace_root

    try:
        ws = resolve_workspace_root(Path(project_root))
        ideas = ws / "IDEAS.md"
        if not ideas.exists():
            return {"ideas": [], "count": 0}
        text = ideas.read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return {"ideas": [], "count": 0}
    entries = parse_ideas(_strip_idea_html_comments(text))
    items = [
        {"title": e["title"], "id": e["id"], "status": e["status"]}
        for e in entries
    ]
    return {"ideas": items, "count": len(items)}


def idea_append(project_root, entry_id: str, notes: str) -> dict[str, Any]:
    """按 ``- id:`` 定位条目并追加时间戳 notes 行（idea append 域函数）。

    写操作：与 propose / confirm 共用 ``.intake.lock`` 准入写锁（锁内
    读-改-写 + 提交，手改 IDEAS 绕锁的 TOCTOU 在此不存在）；追记行用
    ``- notes追记`` 键（parse_ideas 只认 ``- notes:``，历史追记不干扰
    notes 语义与孤儿巡检）。

    Args:
        project_root: 仓库根目录。
        entry_id: 条目 ``- id:`` 精确值。
        notes: 追记内容（原文写入，调用方保证单行语义）。

    Returns:
        成功 ``{"appended": True, "id", "commit"}``；未命中
        ``{"appended": False, "reason": "not_found", ...}``；前置守卫失败
        同 propose 口径（not_on_main / dirty_workspace）。
    """
    from orchd.gitops import ensure_committed
    from orchd.intake import (
        _atomic_write_text,
        _intake_guard,
        _resolve_lock_orchd_dir,
    )
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    project_root = Path(project_root)
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        return {"appended": False, **{k: v for k, v in guard_err.items() if k != "committed"}}

    import datetime

    orchd_dir = _resolve_lock_orchd_dir(project_root)
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        ws = resolve_workspace_root(project_root)
        ideas = ws / "IDEAS.md"
        if not ideas.exists():
            return {
                "appended": False,
                "reason": "not_found",
                "id": entry_id,
                "hint": "IDEAS.md 不存在或无该 id 条目，先 python .orchd/__main__.py ideas list 查看条目 id。",
            }
        text = ideas.read_text(encoding="utf-8")
        lines = text.splitlines()
        end = None
        for i, line in enumerate(lines):
            if not line.strip().startswith("## "):
                continue
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("## "):
                j += 1
            for k in range(i + 1, j):
                s = lines[k].strip()
                if (
                    (s.startswith("- id:") and s[len("- id:"):].strip() == entry_id)
                    or (s.startswith("id:") and s[len("id:"):].strip() == entry_id)
                ):
                    end = j
                    break
            if end is not None:
                break
        if end is None:
            return {
                "appended": False,
                "reason": "not_found",
                "id": entry_id,
                "hint": "未找到 - id: 为该值的条目，先 python .orchd/__main__.py ideas list 查看条目 id。",
            }
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        new_lines = lines[:end] + [f"- notes追记 {stamp}: {notes}"] + lines[end:]
        _atomic_write_text(ideas, "\n".join(new_lines) + "\n")

        commit = ensure_committed(
            project_root,
            [str(ideas)],
            f"chore(idea): orchd idea append — {entry_id}",
        )
        result: dict[str, Any] = {
            "appended": True,
            "id": entry_id,
            "commit": commit,
        }
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"idea append commit 未执行（{commit.get('reason')}）：IDEAS.md 改动"
                    "可能未入库，请人工核对"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def _cmd_ideas_list(args) -> dict:
    """列出 IDEAS.md 全部条目（ideas list，只读 JSON）。

    CLI 参数: 无。
    返回: ``{"ideas": [...], "count": n}``（stdout 纯 JSON，agent 可直接解析）。
    """
    orchd_dir = _find_orchd_dir()
    return ideas_list(orchd_dir.parent)


def _cmd_idea_append(args) -> dict:
    """按 - id: 追记 notes（idea append，持锁 + 提交）。

    CLI 参数: args.id / args.notes。
    返回: 追记结果字典（appended / id / commit）；未命中时附顶层 error 键，
    退出码非零（task-cli-exit-honesty，E-15）。
    """
    orchd_dir = _find_orchd_dir()
    result = idea_append(orchd_dir.parent, args.id, args.notes)
    if not result.get("appended"):
        result["error"] = {
            "code": "idea_rejected",
            "reason": result.get("reason", "unknown"),
            "hint": result.get("hint", ""),
        }
    return result
