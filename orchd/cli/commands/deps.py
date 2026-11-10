"""orchd CLI 路由：deps 依赖图子命令组（task-deps-host-init）。

  - ``deps init``：按 AST 生成五表 ``deps.json``（空图合法）；``--dry-run`` 只打印，
    ``--force`` 覆盖既有真源。
  - ``deps check``：把真源与 AST 对拍（未声明边 / 僵尸声明 / 分组漂移 / 环）；
    漂移退出码 1，缺真源或结构非法等配置错退出码 2。
  - ``deps sync``：把 hard / delayed 两张表重算为 AST 现值（保留 wildcards /
    owner_notes / edge_notes / scc_ratchet / dynamic_imports）；默认只打印，
    ``--apply`` 才写盘。

引擎仓（``root=orchd``）与宿主仓（任意包根）共用同一解析器
（:mod:`orchd.deps`）；本模块只读 AST / JSON，写盘仅限 ``init`` / ``sync --apply``
的目标文件。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orchd import deps as deps_query
from orchd.cli._util import _find_orchd_dir

__all__ = ("register",)

DEFAULT_DEPS_REL = ".orchd/shared/deps.json"
DEFAULT_ROOT = "orchd"
_OWNER_NOTE = (
    "deps init 生成：顶层 import→hard；函数/类内 import→delayed；"
    "自身及子孙边不入表；wildcards 仅溯源不放行。"
)


def _project_root_from_cwd() -> Path:
    """与其它 CLI 命令同口径：先找 ``.orchd``，项目根为其父目录。"""
    return Path(_find_orchd_dir()).parent


def _paths(args: argparse.Namespace, project_root: Path) -> tuple[Path, Path, str]:
    """解析 ``--deps`` / ``--root`` / ``--package`` 三元组。

    命令行未显式给出的项回退到宿主 ``guards.json`` 的 ``deps`` 段（与
    ``orchd/guards/deps.py`` 的 ``gate_policy`` 单一真源）；宿主未配置时用
    引擎默认值（``.orchd/shared/deps.json`` + ``orchd``）。
    """
    policy = None
    try:
        from orchd.guards.deps import gate_policy

        policy, _ = gate_policy(project_root)
    except Exception:  # noqa: BLE001 - 配置不可读/非法时回退引擎默认值
        policy = None
    deps_rel = args.deps or (policy.deps_rel if policy else DEFAULT_DEPS_REL)
    root = args.root or (policy.root if policy else DEFAULT_ROOT)
    deps_path = project_root / deps_rel
    root_dir = project_root / root
    package = args.package or (policy.package if policy else None) or root_dir.name
    return deps_path, root_dir, package


def _partition(
    edges: dict[str, dict[str, bool]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """AST 边 → (hard, delayed) 表（排除自身及子孙边，值排序去重）。"""
    hard: dict[str, set[str]] = {}
    delayed: dict[str, set[str]] = {}
    for owner, targets in edges.items():
        for target, top_level in targets.items():
            if target == owner or target.startswith(owner + "."):
                continue
            (hard if top_level else delayed).setdefault(owner, set()).add(target)
    return (
        {owner: sorted(targets) for owner, targets in sorted(hard.items())},
        {owner: sorted(targets) for owner, targets in sorted(delayed.items())},
    )


def _document(
    root_dir: Path, package: str, *, base: dict | None = None
) -> dict:
    """生成可 ``decode`` 的五表文档（``sync`` 可传入保留扩展表的 base）。"""
    hard, delayed = _partition(
        deps_query.ast_edges(root=root_dir, package=package)
    )
    doc: dict = dict(base or {})
    doc["schema_version"] = 1
    doc["hard"] = hard
    doc["delayed"] = delayed
    doc.setdefault("wildcards", {})
    doc.setdefault("owner_notes", {"_global": {"reason": _OWNER_NOTE, "card": ""}})
    doc.setdefault("edge_notes", {})
    return doc


def _write(path: Path, doc: dict) -> None:
    """写盘（保持 1 空格缩进的既有风格，末尾换行）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def _display(path: Path, project_root: Path) -> str:
    """路径展示口径：项目内相对 posix，越界时回退绝对路径。"""
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _error(reason: str, message: str, **extra: object) -> dict:
    """结构化配置错误（退出码 2 的载荷）。"""
    return {
        "ok": False,
        "reason": reason,
        "error": {"code": "E007", "message": message},
        **extra,
    }


def _cmd_deps_init(args: argparse.Namespace) -> tuple[dict, int]:
    """``deps init``：生成五表真源（空图合法）。"""
    project_root = _project_root_from_cwd()
    deps_path, root_dir, package = _paths(args, project_root)
    if deps_path.exists() and not args.force:
        return (
            _error(
                "exists",
                f"deps.json 已存在：{_display(deps_path, project_root)}（用 --force 覆盖）",
                path=_display(deps_path, project_root),
            ),
            2,
        )
    if not root_dir.is_dir():
        shown = _display(root_dir, project_root)
        return _error("no_root", f"扫描根目录不存在：{shown}", root=shown), 2
    doc = _document(root_dir, package)
    try:
        graph = deps_query.decode(doc)
    except ValueError as exc:
        return _error("bad_document", f"生成的 deps.json 不可解码：{exc}"), 2
    if not args.dry_run:
        _write(deps_path, doc)
    payload = {
        "ok": True,
        "command": "deps init",
        "path": _display(deps_path, project_root),
        "root": _display(root_dir, project_root),
        "package": package,
        "dry_run": bool(args.dry_run),
        "written": not args.dry_run,
        "counts": {
            "hard": sum(len(v) for v in graph.hard.values()),
            "delayed": sum(len(v) for v in graph.delayed.values()),
        },
        "document": doc,
    }
    return payload, 0


def _drift(
    deps_path: Path, root_dir: Path, package: str
) -> tuple[deps_query.DepsGraph | None, dict | None]:
    """对拍真源与 AST；返回 ``(graph, drift)``，配置错时返回 ``(None, error)``。"""
    if not deps_path.is_file():
        return None, _error(
            "missing_deps", f"依赖真源不存在：{deps_path.as_posix()}"
        )
    if not root_dir.is_dir():
        return None, _error("no_root", f"扫描根目录不存在：{root_dir.as_posix()}")
    try:
        graph = deps_query.load(str(deps_path))
    except (OSError, ValueError) as exc:
        return None, _error("bad_deps", f"deps.json 非法：{exc}")
    edges = deps_query.ast_edges(root=root_dir, package=package)
    actual = {owner: tuple(sorted(targets)) for owner, targets in edges.items()}
    coverage = deps_query.coverage(actual, graph=graph)
    actual_hard, actual_delayed = _partition(edges)
    json_hard = {owner: sorted(targets) for owner, targets in graph.hard.items()}
    json_delayed = {owner: sorted(targets) for owner, targets in graph.delayed.items()}
    partition_diffs: list[str] = []
    for name, actual_table, json_table in (
        ("hard", actual_hard, json_hard),
        ("delayed", actual_delayed, json_delayed),
    ):
        extra = sorted(
            f"{owner} -> {target}"
            for owner in set(actual_table) | set(json_table)
            for target in set(actual_table.get(owner, ()))
            ^ set(json_table.get(owner, ()))
        )
        if extra:
            partition_diffs.append(f"{name} 表与 AST 分组不一致：{', '.join(extra)}")
    actual_edges = {
        (owner, target)
        for owner, targets in actual_hard.items()
        for target in targets
    } | {
        (owner, target)
        for owner, targets in actual_delayed.items()
        for target in targets
    }
    declared = {
        (owner, target)
        for owner, targets in graph.hard.items()
        for target in targets
    } | {
        (owner, target)
        for owner, targets in graph.delayed.items()
        for target in targets
    }
    zombie = sorted(f"{owner} -> {target}" for owner, target in declared - actual_edges)
    try:
        cycles = deps_query.cycles(path=str(deps_path))
    except (OSError, ValueError) as exc:
        return None, _error("bad_deps", f"deps.json 非法：{exc}")
    drift = {
        "undeclared": list(coverage["undeclared"]),
        "wildcard_only": list(coverage["wildcard_only"]),
        "zombie": zombie,
        "partition_diffs": partition_diffs,
        "cycles": [list(group) for group in cycles],
    }
    return graph, drift


def _has_drift(drift: dict) -> bool:
    return any(drift[key] for key in drift)


def _cmd_deps_check(args: argparse.Namespace) -> tuple[dict, int]:
    """``deps check``：漂移退出码 1，配置错退出码 2。"""
    project_root = _project_root_from_cwd()
    deps_path, root_dir, package = _paths(args, project_root)
    graph, drift = _drift(deps_path, root_dir, package)
    if graph is None or drift is None:
        return drift or _error("unavailable", "deps check 不可用"), 2
    payload = {
        "ok": not _has_drift(drift),
        "command": "deps check",
        "path": _display(deps_path, project_root),
        "root": _display(root_dir, project_root),
        "package": package,
        "drift": drift,
        "counts": {
            "hard": sum(len(v) for v in graph.hard.values()),
            "delayed": sum(len(v) for v in graph.delayed.values()),
        },
    }
    return payload, 0 if payload["ok"] else 1


def _cmd_deps_sync(args: argparse.Namespace) -> tuple[dict, int]:
    """``deps sync``：默认只打印，``--apply`` 才写盘。"""
    project_root = _project_root_from_cwd()
    deps_path, root_dir, package = _paths(args, project_root)
    if not root_dir.is_dir():
        shown = _display(root_dir, project_root)
        return _error("no_root", f"扫描根目录不存在：{shown}"), 2
    base: dict | None = None
    if deps_path.is_file():
        try:
            raw = json.loads(deps_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _error("bad_deps", f"既有 deps.json 不可读：{exc}"), 2
        if not isinstance(raw, dict):
            return _error("bad_deps", "既有 deps.json 顶层必须是对象"), 2
        base = raw
    doc = _document(root_dir, package, base=base)
    try:
        deps_query.decode(doc)
    except ValueError as exc:
        return _error("bad_document", f"重算后的 deps.json 不可解码：{exc}"), 2
    if args.apply:
        _write(deps_path, doc)
    return {
        "ok": True,
        "command": "deps sync",
        "path": _display(deps_path, project_root),
        "root": _display(root_dir, project_root),
        "package": package,
        "applied": bool(args.apply),
        "written": bool(args.apply),
        "document": doc,
    }, 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 ``deps`` 二级子命令组（init / check / sync）。"""
    parser = subparsers.add_parser(
        "deps",
        help="依赖图真源：deps init / deps check / deps sync",
    )
    sub = parser.add_subparsers(dest="deps_command", metavar="<子命令>")

    def _common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--deps", default=None,
            help=f"依赖真源（项目内相对路径，缺省 {DEFAULT_DEPS_REL}；"
            "guards.json 未配置时生效）",
        )
        target.add_argument(
            "--root", default=None,
            help=f"AST 扫描根（项目内相对路径，缺省 {DEFAULT_ROOT}；"
            "guards.json 未配置时生效）",
        )
        target.add_argument(
            "--package", default="",
            help="包名（缺省取 guards.json 配置或扫描根目录名）",
        )

    init_parser = sub.add_parser("init", help="生成五表 deps.json（空图合法）")
    _common(init_parser)
    init_parser.add_argument("--dry-run", action="store_true", help="只打印，不落盘")
    init_parser.add_argument("--force", action="store_true", help="覆盖既有真源")
    init_parser.set_defaults(func=_cmd_deps_init)

    check_parser = sub.add_parser("check", help="对拍真源与 AST（漂移退出码 1）")
    _common(check_parser)
    check_parser.set_defaults(func=_cmd_deps_check)

    sync_parser = sub.add_parser("sync", help="重算 hard/delayed 表（默认只打印）")
    _common(sync_parser)
    sync_parser.add_argument(
        "--apply", action="store_true", help="写盘（缺省只打印不落盘）"
    )
    sync_parser.set_defaults(func=_cmd_deps_sync)
