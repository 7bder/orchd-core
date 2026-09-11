"""Orchd CLI 命令注册表（3b 阶段启用）。

统一注册入口：``register(subparsers)`` 按模块顺序调用各 commands 子模块的
``register(subparsers)``，完成全部 23 个顶层命令 + 3 个二级子命令组的注册。

模块顺序即 argparse help 中的命令显示顺序：
  1. init      — validate / bootstrap / init
  2. control   — amend / retract / force-status / merge-ack
  3. workflow  — request / claim / done / review
  4. query     — pool / status / watchdog / doctor
  5. misc      — ideas-archive / layout-migrate / full-regression / intake / roadmap-land
  6. ideas     — idea 二级子命令组
  7. session   — session 二级子命令组
  8. lessons   — lesson 二级子命令组
  9. sync      — sync（账本 git 跨设备同步，task-ledger-git-sync）
"""

from __future__ import annotations

from orchd.cli.commands import (
    init as _init,
    control as _control,
    workflow as _workflow,
    query as _query,
    misc as _misc,
    ideas as _ideas,
    session as _session,
    lessons as _lessons,
    sync as _sync,
)

_REGISTER_ORDER = [
    _init,
    _control,
    _workflow,
    _query,
    _misc,
    _ideas,
    _session,
    _lessons,
    _sync,
]


def register(subparsers) -> None:
    """按模块顺序注册全部 CLI 子命令。

    Args:
        subparsers: ``argparse.ArgumentParser.add_subparsers()`` 返回的
            ``_SubParsersAction`` 实例。
    """
    for mod in _REGISTER_ORDER:
        mod.register(subparsers)
