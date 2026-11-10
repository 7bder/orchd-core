"""orchd CLI 路由：surfaces 落地面子命令组（S2a）。

  - ``surfaces list``：列出注册表（mode / path / dir / required / 来源）；
  - ``surfaces validate``：只校验注册表（不碰片段/主文件）；
  - ``surfaces check``：只读校验片段格式、重复、未装配状态与必填面；
    ``--require-assembled`` 时存在未汇总片段即非零退出（提示 assemble）；
  - ``surfaces assemble``：把未装配片段汇总进主文件（``--dry-run`` 预览、
    ``--keep-fragments`` 保留片段）。

设计基线：``docs/shared-surfaces.md`` §8、§9。所有输出走统一 JSON 封套
（:func:`orchd.cli.skeleton._output`），错误走 ``OrchdError``，不 print 裸栈。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from orchd import surface_assembly, surfaces
from orchd.cli._util import _find_orchd_dir

__all__ = ("register",)


def _project_root() -> Path:
    """与其它 CLI 命令同口径：``.orchd`` 的父目录即项目根。"""
    return Path(_find_orchd_dir()).parent


def _surface_source(spec: surfaces.SurfaceSpec, defaults: dict[str, surfaces.SurfaceSpec]) -> str:
    """面来源：引擎默认 / 宿主覆盖（``.orchd/shared/surfaces.json`` 或旧配置）。"""
    default = defaults.get(spec.name)
    if default is None or default != spec:
        return "host"
    return "engine"


def _cmd_surfaces_list(args: argparse.Namespace) -> dict:
    """``surfaces list``：注册表只读清单。"""
    root = _project_root()
    specs = surfaces.load_surfaces(root)
    defaults = surfaces.default_surfaces()
    items = []
    for name, spec in specs.items():
        item = {
            **surfaces.surface_descriptor(spec),
            "required": spec.required,
            "source": _surface_source(spec, defaults),
            "categories": list(spec.categories),
            "sections": dict(spec.sections),
        }
        if spec.generator:
            item["generator"] = spec.generator
        items.append(item)
    return {"ok": True, "count": len(items), "surfaces": items}


def _cmd_surfaces_validate(args: argparse.Namespace) -> dict:
    """``surfaces validate``：只校验注册表（非法 → E007）。"""
    root = _project_root()
    specs = surfaces.load_surfaces(root)
    surfaces.validate_surfaces(specs)
    return {"ok": True, "count": len(specs), "validated": sorted(specs)}


def _cmd_surfaces_check(args: argparse.Namespace) -> tuple[dict, int]:
    """``surfaces check``：只读校验（含 ``--require-assembled`` 硬门）。"""
    result = surface_assembly.check_surfaces(
        _project_root(),
        names=[args.name] if getattr(args, "name", None) else None,
        require_assembled=bool(getattr(args, "require_assembled", False)),
    )
    return result, (0 if result["ok"] else 1)


def _cmd_surfaces_assemble(args: argparse.Namespace) -> tuple[dict, int]:
    """``surfaces assemble``：汇总未装配片段。"""
    result = surface_assembly.assemble(
        _project_root(),
        names=[args.name] if getattr(args, "name", None) else None,
        dry_run=bool(getattr(args, "dry_run", False)),
        keep_fragments=bool(getattr(args, "keep_fragments", False)),
    )
    return result, 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 ``surfaces`` 二级子命令组（list / validate / check / assemble）。"""
    parser = subparsers.add_parser(
        "surfaces",
        help="落地面：surfaces list / validate / check / assemble",
    )
    sub = parser.add_subparsers(dest="surfaces_command", metavar="<子命令>")

    list_parser = sub.add_parser("list", help="列出落地面（mode/path/required/来源）")
    list_parser.set_defaults(func=_cmd_surfaces_list)

    validate_parser = sub.add_parser("validate", help="校验落地面注册表")
    validate_parser.set_defaults(func=_cmd_surfaces_validate)

    check_parser = sub.add_parser(
        "check",
        help="校验片段/汇总状态（--require-assembled 时存在未汇总片段即失败）",
    )
    check_parser.add_argument("name", nargs="?", default=None, help="只检查某面（缺省全部）")
    check_parser.add_argument(
        "--require-assembled",
        action="store_true",
        dest="require_assembled",
        help="存在未汇总片段即非零退出（发版门）",
    )
    check_parser.add_argument(
        "--json", action="store_true", help="JSON 输出（默认即 JSON；保留兼容）"
    )
    check_parser.set_defaults(func=_cmd_surfaces_check)

    assemble_parser = sub.add_parser("assemble", help="汇总未装配片段进主文件")
    assemble_parser.add_argument("name", nargs="?", default=None, help="只汇总某面（缺省全部）")
    assemble_parser.add_argument(
        "--dry-run", action="store_true", dest="dry_run", help="只输出计划，不写不删"
    )
    assemble_parser.add_argument(
        "--keep-fragments",
        action="store_true",
        dest="keep_fragments",
        help="写盘成功后保留片段（默认删除）",
    )
    assemble_parser.set_defaults(func=_cmd_surfaces_assemble)
