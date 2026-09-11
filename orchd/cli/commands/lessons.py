"""Orchd CLI 路由：lessons 命令 handler + 二级子命令注册。

迁移自 orchd/cli.py（task-split-cli-cmds-ideas-session-lessons）：
  - _cmd_lesson_stage: lesson stage（执行中静默打点到任务暂存区）
  - _cmd_lesson_add: lesson add（人工/事后手动入库）
  - _cmd_lesson_report: lesson report（只记问题不记解法）
  - _cmd_lesson_review: lesson review（人工批量确认任务暂存建议）
  - _cmd_lesson_resolve: lesson resolve（人工确认信任分级）
  - _cmd_lesson_archive: lesson archive（手动归档）
  - _cmd_lesson_list: lesson list（查看 lesson 库/暂存区）
  - _cmd_lesson_show: lesson show（查看完整条目）
  - register: lesson 二级子命令组注册（原样搬自 _build_parser）

3a 阶段说明：本模块是 lessons 域的目标落点。当前 cli.py（legacy）仍
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
from orchd.cli.identity import (
    _require_agent_id,
    _resolve_agent_id,
)
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)



def register(sub):
    """注册 lesson 二级子命令组（原样搬自 _build_parser，3a 纯移动，机制未变）。"""
    # lesson（经验回灌引擎，task-lesson-feedback-engine）：stage/add/report/review/
    # resolve/list/show/archive 七子命令。
    p = sub.add_parser("lesson", help="经验回灌：自愈经验沉淀与触发注入")
    lesson_sub = p.add_subparsers(dest="lesson_action", required=True)

    _p = lesson_sub.add_parser("stage", help="执行中静默打点到任务暂存区")
    _p.add_argument("--task", required=True)
    _p.add_argument("--trigger", required=True, help="触发键：错误码名或 <command>/<step>")
    _p.add_argument("--type", dest="trigger_type", default="error_code",
                choices=["error_code", "scene"], help="触发键类型（默认 error_code）")
    _p.add_argument("--scene", default=None, help="场景补充上下文（如 container/flat）")
    _p.add_argument("--symptom", required=True)
    _p.add_argument("--solution", default="")
    _p.add_argument("--resolved", action="store_true", help="已自愈解决（verify 通过）")
    _p.add_argument("--severity", default="blocking", choices=["blocking", "warning"])
    _p.add_argument("--urgent", action="store_true", help="紧急：即时提示人工")
    _p.set_defaults(func=_cmd_lesson_stage)

    _p = lesson_sub.add_parser("add", help="人工/事后手动入库（不经任务流程）")
    _p.add_argument("--trigger", required=True)
    _p.add_argument("--type", dest="trigger_type", default="error_code",
                choices=["error_code", "scene"])
    _p.add_argument("--scene", default=None)
    _p.add_argument("--symptom", required=True)
    _p.add_argument("--solution", required=True)
    _p.add_argument("--severity", default="blocking", choices=["blocking", "warning"])
    _p.set_defaults(func=_cmd_lesson_add)

    _p = lesson_sub.add_parser("report", help="只记问题不记解法（--guidance-flaw 标记指引缺陷）")
    _p.add_argument("--trigger", required=True)
    _p.add_argument("--type", dest="trigger_type", default="error_code",
                choices=["error_code", "scene"])
    _p.add_argument("--scene", default=None)
    _p.add_argument("--symptom", required=True)
    _p.add_argument("--severity", default="blocking", choices=["blocking", "warning"])
    _p.add_argument("--guidance-flaw", dest="guidance_flaw", action="store_true")
    _p.set_defaults(func=_cmd_lesson_report)

    _p = lesson_sub.add_parser("review", help="人工批量确认任务暂存建议")
    _p.add_argument("--task", required=True)
    _p.add_argument("--approve-all", dest="approve_all", action="store_true")
    _p.add_argument("--reject", type=int, nargs="*", default=None,
                help="拒绝的暂存条目序号（0-based）")
    _p.set_defaults(func=_cmd_lesson_review)

    _p = lesson_sub.add_parser("resolve", help="人工确认信任分级（proposed↔verified/archived）")
    _p.add_argument("--id", required=True)
    _p.add_argument("--approve", action="store_true", help="-> verified（正式触发）")
    _p.add_argument("--reject", dest="reject_flag", action="store_true", help="-> archived")
    _p.set_defaults(func=_cmd_lesson_resolve)

    _p = lesson_sub.add_parser("archive", help="手动归档（不再触发）")
    _p.add_argument("--id", required=True)
    _p.set_defaults(func=_cmd_lesson_archive)

    _p = lesson_sub.add_parser("list", help="查看 lesson 库/暂存区")
    _p.add_argument("--status", default=None, choices=["proposed", "verified", "archived"])
    _p.add_argument("--trigger", default=None)
    _p.add_argument("--staged", action="store_true", help="查看暂存区")
    _p.add_argument("--all", dest="all_flag", action="store_true")
    _p.set_defaults(func=_cmd_lesson_list)

    _p = lesson_sub.add_parser("show", help="查看完整条目（含完整 solution）")
    _p.add_argument("--id", required=True)
    _p.set_defaults(func=_cmd_lesson_show)


def _cmd_lesson_stage(args) -> dict:
    """lesson stage：执行中静默打点（设计 §7/§8.6）。需会话身份。"""
    from orchd import __version__
    from orchd.lessons import is_lessons_enabled, stage

    orchd_dir = _find_orchd_dir()
    if not is_lessons_enabled(orchd_dir):
        raise OrchdError(
            ErrorCode.E007,
            "lessons_disabled: lessons.enabled=false，stage 被拒绝",
            [{"hint": "经验回灌功能已关闭"}],
        )
    agent_id = _require_agent_id(orchd_dir)
    source = {
        "agent": agent_id,
        "session": os.environ.get("ORCHD_SESSION_ID", ""),
        "engine_version": __version__,
    }
    result = stage(
        orchd_dir,
        task_id=args.task,
        trigger_type=args.trigger_type,
        trigger_key=args.trigger,
        scene=args.scene,
        symptom=args.symptom,
        solution=args.solution,
        resolved=args.resolved,
        severity=args.severity,
        urgent=args.urgent,
        source=source,
    )
    # 紧急通道（§8.6）：stage --urgent 即时提示人工
    if args.urgent and not result.get("skipped"):
        print("orchd ▸ 存在紧急 guidance 建议，建议尽快处理", file=sys.stderr)
    return result

def _cmd_lesson_add(args) -> dict:
    """lesson add：人工/事后手动入库（设计 §7）。"""
    from orchd import __version__
    from orchd.lessons import add, is_lessons_enabled

    orchd_dir = _find_orchd_dir()
    if not is_lessons_enabled(orchd_dir):
        raise OrchdError(
            ErrorCode.E007,
            "lessons_disabled: lessons.enabled=false，add 被拒绝",
            [{"hint": "经验回灌功能已关闭"}],
        )
    agent_id = _resolve_agent_id(orchd_dir) or "human"
    source = {
        "agent": agent_id,
        "session": os.environ.get("ORCHD_SESSION_ID", ""),
        "engine_version": __version__,
    }
    return add(
        orchd_dir,
        trigger_type=args.trigger_type,
        trigger_key=args.trigger,
        scene=args.scene,
        symptom=args.symptom,
        solution=args.solution,
        severity=args.severity,
        source=source,
    )

def _cmd_lesson_report(args) -> dict:
    """lesson report：只记问题不记解法（设计 §7）。"""
    from orchd import __version__
    from orchd.lessons import is_lessons_enabled, report

    orchd_dir = _find_orchd_dir()
    if not is_lessons_enabled(orchd_dir):
        raise OrchdError(
            ErrorCode.E007,
            "lessons_disabled: lessons.enabled=false，report 被拒绝",
            [{"hint": "经验回灌功能已关闭"}],
        )
    agent_id = _resolve_agent_id(orchd_dir) or "human"
    source = {
        "agent": agent_id,
        "session": os.environ.get("ORCHD_SESSION_ID", ""),
        "engine_version": __version__,
    }
    return report(
        orchd_dir,
        trigger_type=args.trigger_type,
        trigger_key=args.trigger,
        scene=args.scene,
        symptom=args.symptom,
        severity=args.severity,
        source=source,
        guidance_flaw=args.guidance_flaw,
    )

def _cmd_lesson_review(args) -> dict:
    """lesson review：人工批量确认任务暂存建议（设计 §7/§8.6）。"""
    from orchd.lessons import review_task

    orchd_dir = _find_orchd_dir()
    reject = args.reject if args.reject else None
    return review_task(
        orchd_dir,
        task_id=args.task,
        approve_all=args.approve_all,
        reject_indices=reject,
    )

def _cmd_lesson_resolve(args) -> dict:
    """lesson resolve：人工确认信任分级（设计 §7/§9）。"""
    from orchd.lessons import resolve_lesson

    orchd_dir = _find_orchd_dir()
    if not (args.approve or args.reject_flag):
        raise OrchdError(
            ErrorCode.E007,
            "lesson_resolve: 须指定 --approve 或 --reject",
            [{"hint": "--approve → verified；--reject → archived"}],
        )
    return resolve_lesson(orchd_dir, lesson_id=args.id, approve=args.approve)

def _cmd_lesson_archive(args) -> dict:
    """lesson archive：手动归档（设计 §7/§9）。"""
    from orchd.lessons import archive_lesson

    orchd_dir = _find_orchd_dir()
    return archive_lesson(orchd_dir, lesson_id=args.id)

def _cmd_lesson_list(args) -> dict:
    """lesson list：查看 lesson 库/暂存区（设计 §7）。"""
    from orchd.lessons import list_lessons

    orchd_dir = _find_orchd_dir()
    rows = list_lessons(
        orchd_dir,
        status=args.status,
        trigger=args.trigger,
        staged=args.staged,
        all=args.all_flag,
    )
    return {"lessons": rows, "count": len(rows)}

def _cmd_lesson_show(args) -> dict:
    """lesson show：查看完整条目（设计 §7）。"""
    from orchd.lessons import show_lesson

    orchd_dir = _find_orchd_dir()
    entry = show_lesson(orchd_dir, lesson_id=args.id)
    if entry is None:
        raise OrchdError(ErrorCode.E007, f"lesson '{args.id}' 不存在",
                         [{"lesson_id": args.id}])
    return {"lesson": entry}
