"""orchd CLI 路由：line 命令 handler（task-line-sync）。

  - _cmd_line_sync: 跨线回移通道（定位源提交 → 目标线回移任务提案 → 回移记录）
"""

from __future__ import annotations

from pathlib import Path

from orchd.cli._util import _find_orchd_dir


def _cmd_line_sync(args) -> dict:
    """orchd line-sync：定位源提交 → 目标线回移任务提案 → 回移记录（禁影子改动）。"""
    from orchd.line_sync import plan_backport

    orchd_dir = _find_orchd_dir()
    project_root = Path(orchd_dir).parent
    return plan_backport(
        project_root,
        source_line=args.source_line,
        target_line=args.target_line,
        source_sha=args.sha,
        task_id=getattr(args, "task_id", None),
        title=getattr(args, "title", None),
        module=getattr(args, "module", None),
        source=getattr(args, "source", None),
    )


def register(sub) -> None:
    """注册 line 模块的子命令。"""
    # line-sync（task-line-sync，M2 判据 B1）：跨线回移通道
    p = sub.add_parser(
        "line-sync",
        help="跨线回移通道：定位源提交 → 目标线回移任务提案（带 source_sha）→ 回移记录；禁影子改动",
    )
    p.add_argument("--from", dest="source_line", required=True, help="源线名（须已登记）")
    p.add_argument("--to", dest="target_line", required=True, help="目标线名（须已登记）")
    p.add_argument("--sha", required=True, help="待回移的源提交 sha")
    p.add_argument(
        "--task-id", dest="task_id", default=None, help="回移任务 id（缺省由 sha 派生）"
    )
    p.add_argument("--title", default=None, help="回移任务标题")
    p.add_argument(
        "--module", default=None, help="回移任务 module（缺省取目标项目 modules[0]）"
    )
    p.add_argument(
        "--source",
        default=None,
        help="回移任务 source（缺省 debug:line-sync-backport，免文件引用校验）",
    )
    p.set_defaults(func=_cmd_line_sync)
