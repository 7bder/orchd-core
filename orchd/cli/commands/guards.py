"""orchd CLI 路由：guards 二级子命令组（task-host-guard-config / guards-init）。

  - guards list: 只读展示宿主 guard 配置（enabled / mode / reason / options）
  - guards check <guard>: 只读执行已注册评估器并把 findings 以 JSON 输出
  - guards init: 生成宿主 ``.orchd/shared/guards.json`` 最小可用配置

``list`` / ``check`` 保持严格只读；``init`` 是唯一写盘入口，只写固定路径
``.orchd/shared/guards.json``（``--dry-run`` 只打印不落盘）。缺省（文件不存在 /
空文件 / 空 guards）时全部 guard 输出 ``enabled=false`` 且 ``reason=no_config``；
配置非法时输出 ``ok=false`` + 结构化 ``errors`` 并以退出码 1 结束（fail-closed）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchd.cli._util import _find_orchd_dir
from orchd.guards import (
    CONFIG_REL,
    GUARD_NAMES,
    MODE_WARN,
    MODES,
    SCHEMA_VERSION,
    GuardEvalContext,
    evaluate_guard,
    guards_list_payload,
    load_guards_config,
)

__all__ = ("register",)

#: ``guards init`` 缺省启用的 guard（最小可用集合：文档 / 依赖 / 体量普查）。
DEFAULT_INIT_GUARDS: tuple[str, ...] = ("docs", "deps", "census")


def _project_root_from_cwd() -> Path:
    """与其它 CLI 命令同口径：先找 ``.orchd``，项目根为其父目录。"""
    return Path(_find_orchd_dir()).parent


def _cmd_guards_list(args: argparse.Namespace) -> tuple[dict, int]:
    """``orchd guards list``：返回只读配置视图（非法配置退出码 1）。"""
    del args  # 当前无参数；保留签名以适配 argparse 分发。
    payload = guards_list_payload(_project_root_from_cwd())
    return payload, 0 if payload.get("ok") else 1


def _cmd_guards_check(args: argparse.Namespace) -> tuple[dict, int]:
    """``orchd guards check <guard>``：执行单个评估器，findings 非空退出码 1。"""
    root = _project_root_from_cwd()
    name = args.guard_name
    try:
        config = load_guards_config(root)
    except Exception as exc:  # noqa: BLE001 - 配置层非法统一 fail-closed
        return (
            {
                "ok": False,
                "guard": name,
                "reason": "invalid_config",
                "message": str(exc),
            },
            1,
        )
    entry = next((entry for entry in config.entries if entry.name == name), None)
    mode = entry.mode if entry is not None else "off"
    if mode == "off":
        return (
            {
                "ok": True,
                "guard": name,
                "reason": "disabled",
                "configured": config.configured,
                "findings": [],
            },
            0,
        )
    context = GuardEvalContext(project_root=root, task_id="")
    try:
        findings = evaluate_guard(name, context)
    except Exception as exc:  # noqa: BLE001 - 评估器异常/降级统一结构化输出
        return (
            {
                "ok": False,
                "guard": name,
                "reason": "evaluator_error",
                "message": str(exc),
                "findings": [],
            },
            1,
        )
    if findings is None:
        return (
            {
                "ok": False,
                "guard": name,
                "reason": "no_evaluator",
                "findings": [],
            },
            1,
        )
    payload = {
        "ok": not findings,
        "guard": name,
        "mode": mode,
        "findings": [finding.as_dict() for finding in findings],
    }
    return payload, 0 if not findings else 1


def _parse_init_guards(raw: str) -> tuple[str, ...]:
    """``--guards`` 逗号清单 → 去重保序的 guard 名元组（未知名抛 ``ValueError``）。"""
    names = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not names:
        names = list(DEFAULT_INIT_GUARDS)
    unknown = [name for name in names if name not in GUARD_NAMES]
    if unknown:
        raise ValueError(
            f"未知 guard：{'、'.join(unknown)}（已登记：{'、'.join(GUARD_NAMES)}）"
        )
    return tuple(dict.fromkeys(names))


def _init_config(names: tuple[str, ...], mode: str) -> dict:
    """构造 schema v1 最小可用配置（仅 ``mode``，不写任何 guard 专属参数）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "guards": {name: {"mode": mode} for name in names},
    }


def _cmd_guards_init(args: argparse.Namespace) -> tuple[dict, int]:
    """``guards init``：生成最小可用配置（默认 docs/deps/census + warn）。

    写盘边界：只写 ``<project_root>/.orchd/shared/guards.json``；已有文件默认拒绝
    覆盖（``--force`` 才覆盖，``--dry-run`` 只打印不落盘）。未知 guard 名在写盘前
    拒绝，保证「报错即不落盘」。
    """
    root = _project_root_from_cwd()
    path = root / CONFIG_REL
    try:
        names = _parse_init_guards(args.guards)
    except ValueError as exc:
        return (
            {"ok": False, "command": "guards init", "reason": "unknown_guard", "message": str(exc)},
            1,
        )
    payload = _init_config(names, args.mode)
    if args.dry_run:
        return (
            {
                "ok": True,
                "command": "guards init",
                "dry_run": True,
                "written": False,
                "path": CONFIG_REL,
                "would_write": payload,
            },
            0,
        )
    if path.exists() and not args.force:
        return (
            {
                "ok": False,
                "command": "guards init",
                "reason": "config_exists",
                "path": CONFIG_REL,
                "message": (
                    "宿主 guard 配置已存在，默认不覆盖；确认要重建请加 --force"
                ),
            },
            1,
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return (
            {
                "ok": False,
                "command": "guards init",
                "reason": "write_failed",
                "path": CONFIG_REL,
                "message": f"{type(exc).__name__}: {exc}",
            },
            1,
        )
    return (
        {
            "ok": True,
            "command": "guards init",
            "dry_run": False,
            "written": True,
            "path": CONFIG_REL,
            "schema_version": SCHEMA_VERSION,
            "guards": {name: {"mode": args.mode} for name in names},
        },
        0,
    )


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 ``guards`` 二级子命令组（list / check / init）。"""
    parser = subparsers.add_parser(
        "guards",
        help="宿主 guard 配置与检查：guards list / guards check <guard> / guards init",
    )
    sub = parser.add_subparsers(dest="guards_command", metavar="<子命令>")

    list_parser = sub.add_parser(
        "list",
        help="展示宿主 guard 配置与启用状态（只读，不写盘/零子进程）",
    )
    list_parser.set_defaults(func=_cmd_guards_list)

    check_parser = sub.add_parser(
        "check",
        help="只读执行单个 guard 评估器并输出 findings（只读，不写盘）",
    )
    check_parser.add_argument("guard_name", choices=GUARD_NAMES, help="guard 名")
    check_parser.set_defaults(func=_cmd_guards_check)

    init_parser = sub.add_parser(
        "init",
        help="生成宿主 guard 最小可用配置（docs/deps/census，默认 warn）",
    )
    init_parser.add_argument(
        "--mode",
        choices=MODES,
        default=MODE_WARN,
        help="写入每项 guard 的档位（默认 warn）",
    )
    init_parser.add_argument(
        "--guards",
        default=",".join(DEFAULT_INIT_GUARDS),
        help="逗号分隔的 guard 清单（默认 docs,deps,census）",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="已有 guards.json 时覆盖重建（默认拒绝覆盖）",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将写入的配置，不落盘",
    )
    init_parser.set_defaults(func=_cmd_guards_init)
