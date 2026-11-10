"""结构化依赖真源查询层（task-deps-graph-truth-source）。

真源：``.orchd/shared/deps.json``。五张表：

- ``hard``：顶层 import 边（import 不在 ``FunctionDef`` / ``AsyncFunctionDef`` /
  ``ClassDef`` 内），键为 owner 模块，值为精确目标模块名；
- ``delayed``：仅函数 / 类内 import 边（同一目标两处都出现则归 ``hard``）；
- ``wildcards``：``conventions.md`` 原 ``X 包`` / ``X.*`` 记法的迁移溯源，**不参与**
  门禁放行（门禁只认 ``hard`` / ``delayed`` 的精确边，外加 owner 自身子孙边）；
- ``owner_notes`` / ``edge_notes``：迁移口径与例外边说明。

分组唯一判据见 :data:`owner_notes` 的 ``_global`` 条目：只看 import 是否位于模块顶层，
不使用其他启发式。``hard`` 与 ``delayed`` 交集恒为空，二者并集 + 自身子孙边 = 全量 AST 边
（边界：裸 ``import orchd`` 是包级锚点，不记边——W7-A 起既有口径；``from orchd import
<子模块>`` 按已知模块名解析为 ``orchd.<子模块>``，属性名如 ``__version__`` 不记边）。

查询方向约定（避免 up/down 歧义）：

- :func:`upstream` = 本模块**直接依赖**的模块（forward 表）；
- :func:`downstream` = **直接依赖本模块**的模块（反向索引）；
- :func:`impact` = 传递依赖本模块的模块闭包（影响面）；
- :func:`cycles` = 强连通分量（默认只看顶层边；函数内延迟导入会制造假环）；
- :func:`coverage` = 给定实际边集，逐条判定 hard / delayed / 自身 / 未声明。

本模块只读 JSON / AST、只依赖标准库与 ``orchd`` 包锚点：不读 git、不写盘。
AST 边解析（``ast_edges`` / ``undeclared_edges``）由注册期预检与契约测试共用。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import orchd

_SCHEMA_VERSION = 1


def _project_root_for(pkg_dir: Path) -> Path:
    """由引擎包目录解析项目根（源码 ``<root>/orchd`` / 宿主 ``<root>/.orchd/orchd``）。

    宿主布局多一层 ``.orchd``：包目录的父目录即 ``.orchd`` 时，项目根再上一层。
    """
    parent = pkg_dir.parent
    return parent.parent if parent.name == ".orchd" else parent


@lru_cache(maxsize=1)
def _deps_path() -> Path:
    """``<repo>/.orchd/shared/deps.json``（锚点取包根，不依赖本文件深度）。

    宿主安装布局的包位于 ``<root>/.orchd/orchd``，故用 :func:`_project_root_for`
    归一，避免解析成 ``<root>/.orchd/.orchd/shared/deps.json``。

    ``lru_cache`` 是查询热路径的一部分：1000 次查询若每次重算 ``resolve()``，
    实测会从 <5 ms 退化到 ~140 ms（AC 的 5 ms 预算）。
    """
    pkg_dir = Path(orchd.__file__).resolve().parent
    return _project_root_for(pkg_dir) / ".orchd" / "shared" / "deps.json"


def _table(raw: Mapping[str, Any], name: str) -> dict[str, tuple[str, ...]]:
    """校验并规范化一张 ``owner -> [目标]`` 表（去重、排序、拒绝非字符串项）。"""
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"deps.json: {name} 必须是对象")
    out: dict[str, tuple[str, ...]] = {}
    for owner, items in value.items():
        if not isinstance(owner, str):
            raise ValueError(f"deps.json: {name} 的键必须是字符串")
        # 空串是合法 owner：orchd/__init__.py 的模块名为空（包根）。

        if not isinstance(items, (list, tuple)):
            raise ValueError(f"deps.json: {name}[{owner!r}] 必须是数组")
        if not all(isinstance(item, str) and item for item in items):
            raise ValueError(f"deps.json: {name}[{owner!r}] 只能含非空字符串")
        out[owner] = tuple(sorted(set(items)))
    return out


def _notes(raw: Mapping[str, Any], name: str) -> dict[str, dict[str, str]]:
    """校验并规范化 ``键 -> {reason, card}`` 表（结构不合法即 ``ValueError``）。"""
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"deps.json: {name} 必须是对象")
    out: dict[str, dict[str, str]] = {}
    for key, note in value.items():
        if not isinstance(key, str):
            raise ValueError(f"deps.json: {name} 的键必须是字符串")
        if not isinstance(note, dict):
            raise ValueError(
                f"deps.json: {name}[{key!r}] 必须是 reason/card 对象")
        reason = note.get("reason")
        card = note.get("card")
        if not isinstance(reason, str) or not reason:
            raise ValueError(
                f"deps.json: {name}[{key!r}].reason 必须是非空字符串")
        if not isinstance(card, str):
            raise ValueError(f"deps.json: {name}[{key!r}].card 必须是字符串")
        out[key] = {"reason": reason, "card": card}
    return out


def _edge_notes(raw: Mapping[str, Any], name: str) -> dict[str, str]:
    """校验并规范化一张 ``键 -> 说明`` 表（值为非空字符串）。"""
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"deps.json: {name} 必须是对象")
    out: dict[str, str] = {}
    for key, note in value.items():
        if not isinstance(key, str) or not isinstance(note, str) or not note:
            raise ValueError(f"deps.json: {name} 的键值必须是非空字符串")
        out[key] = note
    return out


def _merge(
    hard: Mapping[str, tuple[str, ...]], delayed: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """合并两表为 forward 索引（同一 owner 的边取并集）。"""
    merged: dict[str, set[str]] = {owner: set(v) for owner, v in hard.items()}
    for owner, targets in delayed.items():
        merged.setdefault(owner, set()).update(targets)
    return {owner: tuple(sorted(targets)) for owner, targets in sorted(merged.items())}


def _reverse(forward: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """forward 表 → 反向索引（目标模块 → 直接依赖它的模块）。"""
    rev: dict[str, set[str]] = {}
    for owner, targets in forward.items():
        for target in targets:
            rev.setdefault(target, set()).add(owner)
    return {target: tuple(sorted(owners)) for target, owners in sorted(rev.items())}


@dataclass(frozen=True)
class DepsGraph:
    """``deps.json`` 的不可变视图（含预构建的正向 / 反向索引）。

    Attributes:
        schema_version: 真源 schema 版本。
        hard: 顶层 import 精确边。
        delayed: 仅函数 / 类内 import 精确边。
        wildcards: 迁移自 ``conventions.md`` 的包级记法（仅溯源，不参与放行）。
        owner_notes: owner 级迁移说明。
        edge_notes: 边级例外说明。
        forward: ``hard ∪ delayed`` 的正向索引。
        forward_top: 仅 ``hard`` 的正向索引。
        reverse: ``forward`` 的反向索引。
        reverse_top: ``forward_top`` 的反向索引。
    """

    schema_version: int
    hard: Mapping[str, tuple[str, ...]]
    delayed: Mapping[str, tuple[str, ...]]
    wildcards: Mapping[str, tuple[str, ...]]
    owner_notes: Mapping[str, Mapping[str, str]]
    edge_notes: Mapping[str, str]
    forward: Mapping[str, tuple[str, ...]]
    forward_top: Mapping[str, tuple[str, ...]]
    reverse: Mapping[str, tuple[str, ...]]
    reverse_top: Mapping[str, tuple[str, ...]]


def decode(raw: Any) -> DepsGraph:
    """把已解析的 JSON 对象解码为 :class:`DepsGraph`（格式非法即 ``ValueError``）。

    Args:
        raw: ``json.loads`` 的结果。

    Returns:
        规范化后的依赖图（去重、排序、预构建索引）。

    Raises:
        ValueError: 缺表、类型不符、``schema_version`` 不匹配，或 hard / delayed 有交集。

    空图合法（``hard`` / ``delayed`` 同时为空）：宿主新仓尚无内部依赖时
    ``deps init`` 需要产出可通过本函数解码的五表真源。
    """
    if not isinstance(raw, dict):
        raise ValueError("deps.json: 顶层必须是对象")
    version = raw.get("schema_version")
    if version != _SCHEMA_VERSION:
        raise ValueError(
            f"deps.json: schema_version 必须是 {_SCHEMA_VERSION}，实际 {version!r}"
        )
    hard = _table(raw, "hard")
    delayed = _table(raw, "delayed")
    overlap = sorted(
        f"{owner} -> {target}"
        for owner, targets in hard.items()
        for target in targets
        if target in delayed.get(owner, ())
    )
    if overlap:
        raise ValueError("deps.json: hard 与 delayed 存在交集：" + ", ".join(overlap))
    forward = _merge(hard, delayed)
    return DepsGraph(
        schema_version=version,
        hard=MappingProxyType(hard),
        delayed=MappingProxyType(delayed),
        wildcards=MappingProxyType(_table(raw, "wildcards")),
        owner_notes=MappingProxyType(dict(_notes(raw, "owner_notes"))),
        edge_notes=MappingProxyType(_edge_notes(raw, "edge_notes")),
        forward=MappingProxyType(forward),
        forward_top=MappingProxyType(dict(sorted(hard.items()))),
        reverse=MappingProxyType(_reverse(forward)),
        reverse_top=MappingProxyType(_reverse(hard)),
    )


@lru_cache(maxsize=4)
def _load_cached(path: str) -> DepsGraph:
    """按路径缓存解码结果（``lru_cache`` 单例入口，见 :func:`load`）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return decode(raw)


def load(path: str | None = None) -> DepsGraph:
    """加载依赖图（默认真源，结果按路径缓存）。

    Args:
        path: 可选的真源路径（测试用）；``None`` 取 :func:`_deps_path`。

    Returns:
        缓存的 :class:`DepsGraph`（同一路径重复调用返回同一对象）。
    """
    return _load_cached(str(Path(path) if path else _deps_path()))


def _direct(index: Mapping[str, tuple[str, ...]], module: str) -> tuple[str, ...]:
    """索引查表（未知模块返回空元组，不抛异常）。"""
    return index.get(module, ())


def upstream(module: str, *, path: str | None = None) -> tuple[str, ...]:
    """本模块直接依赖的模块（含 delayed 边；排序去重）。"""
    return _direct(load(path).forward, module)


def downstream(module: str, *, path: str | None = None) -> tuple[str, ...]:
    """直接依赖本模块的模块（反向索引；排序去重）。"""
    return _direct(load(path).reverse, module)


def _closure(
    seeds: Iterable[str], index: Mapping[str, tuple[str, ...]], origin: str
) -> tuple[str, ...]:
    """在索引上做 BFS 闭包（不含起点自身）。"""
    seen: set[str] = set()
    stack = list(seeds)
    while stack:
        current = stack.pop()
        if current == origin or current in seen:
            continue
        seen.add(current)
        stack.extend(index.get(current, ()))
    return tuple(sorted(seen))


def impact(
    module: str, *, include_delayed: bool = True, path: str | None = None
) -> tuple[str, ...]:
    """传递依赖本模块的模块闭包（影响面）。

    Args:
        module: 起点模块。
        include_delayed: ``True`` 计入函数内延迟导入（默认，影响面更保守）；
            ``False`` 只看顶层依赖。
        path: 可选真源路径。

    Returns:
        排序后的模块名元组（不含起点）。
    """
    graph = load(path)
    index = graph.reverse if include_delayed else graph.reverse_top
    return _closure(_direct(index, module), index, module)


def _sccs(forward: Mapping[str, tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Kosaraju 强连通分量（迭代实现，避免深递归）。"""
    nodes = sorted(set(forward) | {t for ts in forward.values() for t in ts})
    visited: set[str] = set()
    order: list[str] = []
    for start in nodes:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, done = stack.pop()
            if done:
                order.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            for nxt in forward.get(node, ()):
                if nxt not in visited:
                    stack.append((nxt, False))
    backward: dict[str, list[str]] = {}
    for owner, targets in forward.items():
        for target in targets:
            backward.setdefault(target, []).append(owner)
    assigned: set[str] = set()
    result: list[tuple[str, ...]] = []
    for node in reversed(order):
        if node in assigned:
            continue
        group: list[str] = []
        pending: list[str] = [node]
        while pending:
            current = pending.pop()
            if current in assigned:
                continue
            assigned.add(current)
            group.append(current)
            pending.extend(backward.get(current, ()))
        if len(group) > 1:
            result.append(tuple(sorted(group)))
    return sorted(result)


def cycles(*, include_delayed: bool = False, path: str | None = None) -> list[tuple[str, ...]]:
    """依赖环（强连通分量，仅返回成员数 > 1 的分量）。

    Args:
        include_delayed: ``False``（默认）只看顶层 import 边——函数内延迟导入会制造
            「A 顶层依赖 B、B 函数内回调 A」的假环；``True`` 纳入延迟边。
        path: 可选真源路径。

    Returns:
        排序后的分量列表，每个分量为排序后的模块名元组。
    """
    graph = load(path)
    return _sccs(graph.forward if include_delayed else graph.forward_top)


def _wildcard_covers(graph: DepsGraph, owner: str, target: str) -> bool:
    """目标是否被 owner 的 legacy 包级记法覆盖（仅溯源，不参与门禁放行）。

    不再保留 onboard 包级候选：旧门禁的 onboard 豁免已在迁移时展开为精确边，
    查询层对 owner 一视同仁。
    """
    for pattern in graph.wildcards.get(owner, ()):
        if target == pattern or target.startswith(pattern + "."):
            return True
    return False


def coverage(
    edges: Mapping[str, Iterable[str]],
    *,
    graph: DepsGraph | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """逐条判定实际边在真源中的归属。

    Args:
        edges: ``owner -> 目标模块`` 的实际边集（AST 侧产物）。
        graph: 已解码的依赖图（优先）；``None`` 时按 ``path`` 加载。
        path: 可选真源路径。

    Returns:
        含 ``total`` / ``self`` / ``hard`` / ``delayed`` / ``covered`` 计数与
        ``wildcard_only``（仅被 legacy 记法覆盖，门禁视为未声明）/ ``undeclared``
        列表的字典。
    """
    deps = graph or load(path)
    counts: dict[str, Any] = {
        "total": 0, "self": 0, "hard": 0, "delayed": 0, "covered": 0
    }
    wildcard_only: list[str] = []
    undeclared: list[str] = []
    for owner, targets in edges.items():
        for target in set(targets):
            counts["total"] += 1
            if target == owner or target.startswith(owner + "."):
                counts["self"] += 1
                counts["covered"] += 1
            elif target in deps.hard.get(owner, ()):
                counts["hard"] += 1
                counts["covered"] += 1
            elif target in deps.delayed.get(owner, ()):
                counts["delayed"] += 1
                counts["covered"] += 1
            elif _wildcard_covers(deps, owner, target):
                wildcard_only.append(f"{owner} -> {target}")
            else:
                undeclared.append(f"{owner} -> {target}")
    counts["wildcard_only"] = sorted(wildcard_only)
    counts["undeclared"] = sorted(undeclared)
    return counts


# AST 边解析（注册期预检 + 依赖方向契约共用，单一解析器）
def _orchd_root() -> Path:
    return Path(orchd.__file__).resolve().parent


def _owner_of(root: Path, path: Path) -> str:
    """文件 → 模块 owner（``__init__.py`` 归其包名；``orchd/`` 根为空串）。"""
    rel = path.relative_to(root).with_suffix("").as_posix()
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _module_exists(root: Path, name: str) -> bool:
    """``<包>.<name>`` 是否为真实模块 / 子包（供相对 ``from . import`` 解析）。"""
    base = root
    for part in name.split(".") if name else []:
        base = base / part
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


@lru_cache(maxsize=8)
def _known_submodules(root: str) -> frozenset[str]:
    """扫描根下可作为 ``from <包> import <名>`` 目标的子模块 / 子包名。"""
    base = Path(root)
    names: set[str] = set()
    for path in base.iterdir():
        if path.is_dir() and (path / "__init__.py").is_file():
            names.add(path.name)
        elif path.suffix == ".py" and path.name != "__init__.py":
            names.add(path.stem)
    return frozenset(names)


def _package_of(path: Path, owner: str) -> str:
    """模块所属包（``orchd/`` 根为空串；``__init__`` 的 owner 即包名）。"""
    if path.name == "__init__.py":
        return owner
    return owner.rsplit(".", 1)[0] if "." in owner else ""


def _targets_of(root: Path, path: Path, root_package: str) -> dict[str, set[bool]]:
    """owner 的每个目标 → 出现位置集合（``True`` = 顶层 import）。

    ``root_package`` 是扫描根对应的包名（引擎为 ``orchd``；宿主可为任意包名），
    绝对导入按该前缀识别并剥离，相对导入按该前缀展开；缺省由根目录名派生。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _owner_of(root, path)
    package = _package_of(path, owner)
    parts = package.split(".") if package else []
    known = _known_submodules(str(root))
    prefix = root_package + "."
    out: dict[str, set[bool]] = {}

    def add(name: str, nested: bool) -> None:
        if name == root_package:
            return
        if name.startswith(prefix):
            name = name[len(prefix):]
        out.setdefault(name, set()).add(not nested)

    def visit(node: ast.AST, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inner = nested or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name == root_package or alias.name.startswith(prefix):
                        add(alias.name, inner)
            elif isinstance(child, ast.ImportFrom):
                if child.module is not None and (
                    child.module == root_package
                    or child.module.startswith(prefix)
                ):
                    add(child.module, inner)
                    if child.module == root_package:
                        for alias in child.names:
                            if alias.name in known:
                                add(prefix + alias.name, inner)
                elif child.level > 0:
                    up = child.level - 1
                    base = parts[: len(parts) - up] if up else parts
                    if child.module:
                        add(prefix + ".".join(base + [child.module]), inner)
                    else:
                        for alias in child.names:
                            candidate = ".".join(base + [alias.name])
                            if _module_exists(root, candidate):
                                add(prefix + candidate, inner)
            visit(child, inner)

    visit(tree, False)
    return out


def ast_edges(
    paths: Iterable[str | Path] | None = None,
    *,
    root: str | Path | None = None,
    package: str | None = None,
) -> dict[str, dict[str, bool]]:
    """扫描 Python 文件，返回 ``owner -> {目标模块: 是否顶层 import}``。

    Args:
        paths: 待扫描文件（绝对路径，或相对 ``root`` 的父目录）；``None`` 扫描
            ``root`` 下全部 ``.py``。
        root: 包目录（引擎缺省 ``orchd/``；宿主可传任意扫描根）。
        package: 包名；缺省取 ``root.name``（如 ``orchd`` / 宿主包名）。

    Returns:
        owner 为相对包根的模块名（``orchd/__init__.py`` → 空串）。
    """
    base = Path(root) if root else _orchd_root()
    root_package = package or base.name
    if paths is None:
        files = sorted(base.rglob("*.py"))
    else:
        files = [Path(p) for p in paths]
    out: dict[str, dict[str, bool]] = {}
    for raw in files:
        path = raw if raw.is_absolute() else base.parent / raw
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            path.relative_to(base)
        except ValueError:
            continue
        owner = _owner_of(base, path)
        out[owner] = {
            target: True in flags
            for target, flags in _targets_of(base, path, root_package).items()
        }
    return out


def undeclared_edges(
    paths: Iterable[str | Path],
    *,
    graph: DepsGraph | None = None,
    root: str | Path | None = None,
    package: str | None = None,
) -> dict[str, tuple[str, ...]]:
    deps = graph or load()
    base = Path(root) if root else _orchd_root()
    missing: dict[str, tuple[str, ...]] = {}
    for owner, targets in ast_edges(paths, root=base, package=package).items():
        uncovered = sorted(
            target for target in targets
            if target != owner
            and not target.startswith(owner + ".")
            and target not in deps.hard.get(owner, ())
            and target not in deps.delayed.get(owner, ())
        )
        if uncovered:
            missing[owner] = tuple(uncovered)
    return missing


def undeclared_edges_for_files(
    files: Iterable[str | Path],
    *,
    project_root: str | Path,
    graph: DepsGraph | None = None,
    exclude: Iterable[str | Path] = (),
    root: str | Path = "orchd",
    package: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """声明/实际改动文件集 → 未登记边（注册期与 done 期共用口径）。

    ``files`` 相对 ``project_root``（如 ``orchd/foo.py``）；只统计 ``root``
    （项目内相对目录或绝对目录，缺省 ``orchd``）下**存在**的 Python 模块，
    ``exclude``（任务 exempt_files）先行剔除。空集/全被剔除返回 ``{}``；
    图缺失或不可解码由调用方降级（不在此处抛）。

    Args:
        root: 扫描根（相对 ``project_root`` 或绝对路径）；宿主布局可传自有包目录。
        package: 包名；缺省由 ``root`` 目录名派生（与 :func:`ast_edges` 一致）。
    """
    project = Path(project_root)
    root_dir = Path(root)
    if not root_dir.is_absolute():
        root_dir = project / root_dir
    try:
        root_rel = root_dir.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        root_rel = root_dir.as_posix()
    prefix = root_rel.rstrip("/") + "/"
    excluded = {Path(f).as_posix() for f in exclude}
    scoped: list[Path] = []
    for raw in files:
        rel = Path(raw)
        if rel.is_absolute():
            try:
                rel = rel.relative_to(project)
            except ValueError:
                continue
        posix = rel.as_posix()
        if not posix.startswith(prefix) or not posix.endswith(".py"):
            continue
        if posix in excluded:
            continue
        target = project / posix
        if not target.is_file():
            continue
        scoped.append(target)
    if not scoped:
        return {}
    return undeclared_edges(
        scoped, graph=graph, root=root_dir, package=package
    )
