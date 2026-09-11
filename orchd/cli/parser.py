"""Orchd CLI argparse 解析器构建。

3b 注册表模式：``_build_parser()`` 仅创建顶层 parser 和 subparsers，
具体命令注册由 ``orchd.cli.commands.register(subparsers)`` 统一完成。
各 commands 模块（init/control/workflow/query/misc/ideas/session/lessons）
各自定义 ``register(subparsers)``，注册本模块的子命令。
"""

from __future__ import annotations

import argparse

from orchd import __version__
from orchd.cli.commands import register as _register_commands
from orchd.cli._util import (
    _flatten_nargs,
    _resolve_text_arg,
)


def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器并注册全部子命令。

    顶层参数（--version / --guidance）在此定义；子命令注册委托给
    ``orchd.cli.commands.register(subparsers)``，按模块顺序注册
    23 个顶层命令 + 3 个二级子命令组。

    Returns:
        配置完毕的 ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        prog="orchd",
        description="Cross-agent-platform task distribution CLI",
        epilog=(
            "无感引导：任意命令的 JSON 响应含 guidance 字段（step/command/hint，"
            "指导下一步行动）；运行 'orchd status --text' 查看任务池与下一步引导。"
        ),
    )
    parser.add_argument("--version", action="version", version=f"orchd {__version__}")
    parser.add_argument("--guidance", choices=["slim", "full"], default="slim",
                        help="guidance 输出模式：slim（默认，仅 step/command/hint 核心三字段，省 token）"
                             " / full（含 read/rules/branch_ctx 全量，调试或弱 LLM 场景）")
    sub = parser.add_subparsers(dest="command")

    # 统一注册全部子命令（按模块顺序）
    _register_commands(sub)

    return parser
