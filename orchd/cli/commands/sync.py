"""Orchd CLI 路由：sync 命令 handler。

账本 git 跨设备同步（task-ledger-git-sync）：显式 ``orchd sync`` 子命令
（push / pull / compact / status），使账本进度可经 git 跨设备读取，引擎热路径
零改动。核心逻辑见 ``orchd.ledger_sync``。
"""

from __future__ import annotations

from typing import Any

from orchd.errors import ErrorCode, OrchdError


def _run_sync(args, action: str) -> dict[str, Any]:
    """定位项目根 + Store，委托 ledger_sync 对应动作。"""
    from orchd.ledger import Store
    from orchd.ledger_sync import compact, pull, push, status_remote

    from orchd.cli._util import _find_orchd_dir

    orchd_dir = _find_orchd_dir()
    project_root = orchd_dir.parent
    store = Store(orchd_dir)
    remote = getattr(args, "remote", None) or "origin"
    if action == "push":
        return push(project_root, store, remote=remote)
    if action == "pull":
        return pull(project_root, store, remote=remote)
    if action == "compact":
        return compact(project_root, store, remote=remote)
    if action == "status":
        return status_remote(project_root, store, remote=remote)
    raise OrchdError(
        ErrorCode.E007,
        f"unknown sync action: {action}",
        [{
            "hint": "sync 支持 --push / --pull / --compact / --status"
        }],
    )


def _cmd_sync(args) -> dict[str, Any]:
    """sync 命令统一入口：按 --push/--pull/--compact/--status 分发。"""
    if getattr(args, "push", False):
        return _run_sync(args, "push")
    if getattr(args, "pull", False):
        return _run_sync(args, "pull")
    if getattr(args, "compact", False):
        return _run_sync(args, "compact")
    if getattr(args, "status", False):
        return _run_sync(args, "status")
    raise OrchdError(
        ErrorCode.E007,
        "sync must specify one of --push / --pull / --compact / --status",
        [{
            "hint": "如：python .orchd/__main__.py sync --push"
        }],
    )


def register(sub) -> None:
    """注册 sync 子命令（push / pull / compact / status）。"""
    p = sub.add_parser("sync", help="账本 git 跨设备同步（push / pull / compact）")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--push",
                     action="store_true",
                     help="推送本地账本增量到远端 orchd/ledger ref")
    grp.add_argument("--pull",
                     action="store_true",
                     help="拉取远端 delta 合并进本地账本并重建 checkpoint")
    grp.add_argument("--compact",
                     action="store_true",
                     help="把 delta 归并入 state.json 并清空（git 体积收敛）")
    grp.add_argument("--status",
                     action="store_true",
                     help="只读查看远端 ref 进度摘要（不改本地）")
    p.add_argument("--remote", default="origin", help="git 远端名（默认 origin）")
    p.set_defaults(func=_cmd_sync)
