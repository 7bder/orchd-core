"""契约文档生成器（P1-①，task-docsgen-relocate）：真源直读，纯函数可单测。

生成器实现随引擎包发布（``orchd/docsgen``），宿主安装后仍可 import 并只读生成到
临时目录；``scripts/gen_contract_docs.py`` 仅保留兼容入口。

根路径按引擎包位置推导：源码布局 ``<repo>/orchd`` → ``<repo>``；宿主布局
``<host>/.orchd/orchd`` → ``<host>``（与 ``orchd.deps._project_root_for`` 同口径）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import sys
from typing import cast

import orchd

_FILE_BUDGET = 500

_EMPTY_DEPS_RAW: dict = {
    "schema_version": 1,
    "hard": {},
    "delayed": {},
    "wildcards": {},
    "owner_notes": {"_global": {"reason": "deps.json 不存在（宿主降级空图）", "card": ""}},
    "edge_notes": {},
}


def _project_root_for(pkg_dir: pathlib.Path) -> pathlib.Path:
    """由引擎包目录解析项目根（源码 ``<root>/orchd`` / 宿主 ``<root>/.orchd/orchd``）。"""
    parent = pkg_dir.parent
    return parent.parent if parent.name == ".orchd" else parent


_PACKAGE_DIR = pathlib.Path(orchd.__file__).resolve().parent
ROOT = _project_root_for(_PACKAGE_DIR)


def _resource_root() -> pathlib.Path:
    """资源根：引擎源码在项目根，宿主的 schema 等资源在 ``.orchd/`` 下。"""
    return ROOT if (ROOT / "schema").is_dir() else ROOT / ".orchd"


def _owner_of(path: pathlib.Path) -> str:
    """``orchd/`` 内模块 → 依赖图 owner（``__init__`` 退化为包名，根为空串）。"""
    rel = path.relative_to(_PACKAGE_DIR).with_suffix("").as_posix()
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _known_submodules() -> frozenset[str]:
    """``orchd/`` 下可作为 ``from orchd import <名>`` 目标的子模块 / 子包名。"""
    names: set[str] = set()
    for path in _PACKAGE_DIR.iterdir():
        if path.is_dir() and (path / "__init__.py").is_file():
            names.add(path.name)
        elif path.suffix == ".py" and path.name != "__init__.py":
            names.add(path.stem)
    return frozenset(names)


_KNOWN_SUBMODULES = _known_submodules()


def _module_exists(name: str) -> bool:
    """``orchd.<name>`` 是否为真实模块 / 子包（相对 ``from . import`` 用）。"""
    base = _PACKAGE_DIR
    for part in name.split(".") if name else []:
        base = base / part
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _targets_of(path: pathlib.Path) -> set[str]:
    """模块的全部 orchd 内部 import 目标（只读视图口径，门禁判定见契约测试）。

    与 ``tests/contract/test_dependency_direction.py::_targets_of`` 同语义：
    裸 ``import orchd`` 不记边，相对导入按所属包上溯 ``level-1`` 层。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _owner_of(path)
    is_init = path.name == "__init__.py"
    package = owner if is_init else (owner.rsplit(".", 1)[0] if "." in owner else "")
    parts = package.split(".") if package else []
    out: set[str] = set()

    def add(name: str) -> None:
        if name == "orchd":
            return
        out.add(name[len("orchd."):] if name.startswith("orchd.") else name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("orchd."):
                    add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "orchd":
                for alias in node.names:
                    if alias.name in _KNOWN_SUBMODULES:
                        add("orchd." + alias.name)
            elif node.module is not None and node.module.startswith("orchd."):
                add(node.module)
            elif node.level > 0:
                up = node.level - 1
                base = parts[: len(parts) - up] if up else parts
                if node.module:
                    add("orchd." + ".".join(base + [node.module]))
                else:
                    for alias in node.names:
                        candidate = ".".join(base + [alias.name])
                        if _module_exists(candidate):
                            add("orchd." + candidate)
    return out


def _self_edge_count(files: list[pathlib.Path]) -> int:
    """AST 自身 / 子孙边计数（owner → owner 或 owner.*）。"""
    total = 0
    for path in files:
        owner = _owner_of(path)
        for target in _targets_of(path):
            if target == owner or target.startswith(owner + "."):
                total += 1
    return total


def _layer_counts(hard: dict[str, tuple[str, ...]]) -> dict[int, int]:
    """按 hard 边做依赖深度分层（无依赖 = 0，层 = 1 + max(依赖层)）。

    走 Kahn 拓扑推进而非记忆化 DFS：hard 图一旦出现环，DFS 的截断返回值会被
    memo 污染并被其它上下文复用，分层随集合迭代顺序变化（生成物非确定）。
    环内节点无法拓扑定序，统一记 0 层并在概览里由「hard 顶层环」显式披露。
    """
    nodes = set(hard) | {t for ts in hard.values() for t in ts}
    deps = {n: sorted(t for t in hard.get(n, ()) if t != n) for n in nodes}
    rdeps: dict[str, list[str]] = {n: [] for n in nodes}
    for node, targets in deps.items():
        for target in targets:
            rdeps[target].append(node)
    indegree = {n: len(deps[n]) for n in nodes}
    layer = {n: 0 for n in nodes}
    queue = sorted(n for n in nodes if indegree[n] == 0)
    done = 0
    while queue:
        node = queue.pop(0)
        done += 1
        for depender in sorted(rdeps[node]):
            layer[depender] = max(layer[depender], layer[node] + 1)
            indegree[depender] -= 1
            if indegree[depender] == 0:
                queue.append(depender)
    if done < len(nodes):
        for node in sorted(nodes):
            if indegree[node] > 0:
                layer[node] = 0
    counts: dict[int, int] = {}
    for value in layer.values():
        counts[value] = counts.get(value, 0) + 1
    return counts


def gen_dependency_graph_md() -> str:
    """依赖图视图：分层概览 + 逐模块影响面（真源 deps.json + AST 现算）。"""
    from orchd import deps
    from orchd.shared_entries import SHARED_ENTRY_TESTS, shared_entry_key_matches
    from orchd.spec.declarations import derive_related_test_files

    try:
        graph = deps.load()
    except (OSError, ValueError):
        # 宿主未生成 deps.json：降级空图，仍可只读生成到临时目录。
        graph = deps.decode(_EMPTY_DEPS_RAW)
    repo_root = ROOT
    try:
        deps_digest = hashlib.sha256(
            (repo_root / ".orchd" / "shared" / "deps.json")
            .read_text(encoding="utf-8")
            .encode()
        ).hexdigest()
    except OSError:
        deps_digest = "unavailable（宿主缺 deps.json）"
    all_files = sorted(_PACKAGE_DIR.rglob("*.py"))
    files = [p for p in all_files if p.name != "__init__.py"]
    self_edges = _self_edge_count(all_files)
    layers = _layer_counts(dict(graph.hard))
    try:
        hard_cycles = deps.cycles()
        all_cycles = deps.cycles(include_delayed=True)
    except (OSError, ValueError):
        hard_cycles = []
        all_cycles = []
    max_scc = max((len(c) for c in all_cycles), default=1)
    cycle_members = {name for group in all_cycles for name in group}

    forward_edges = [
        (owner, target) for owner, targets in graph.forward.items() for target in targets
    ]
    wildcard_hits = sum(
        1
        for owner, target in forward_edges
        if any(
            target == pattern or target.startswith(pattern + ".")
            for pattern in graph.wildcards.get(owner, ())
        )
    )
    wildcard_pct = (
        100.0 * wildcard_hits / len(forward_edges) if forward_edges else 0.0
    )

    lines = [
        "# 依赖图视图（生成物，勿手改）",
        "",
        "> 真源：`.orchd/shared/deps.json` + `orchd/**/*.py` 的 AST 现算；",
        "> 重新生成：`python scripts/gen_contract_docs.py --write docs/_generated`。",
        "",
        "## 1. 分层概览",
        "",
        f"- hard 图节点：{len(set(graph.hard) | {t for ts in graph.hard.values() for t in ts})}"
        f"（全部 .py {len(all_files)}，含 {len(all_files) - len(files)} 个包入口）",
        f"- 层数：{len(layers)}（无依赖=0，沿 hard 边取 1 + max(依赖层)）",
        "- 各层节点数：" + " / ".join(f"{k}:{v}" for k, v in sorted(layers.items())),
        f"- 最大 SCC：{max_scc} 节点（"
        + " / ".join(() + max(all_cycles, key=len, default=()))
        + f"；hard 顶层环 {len(hard_cycles)} 个）",
        f"- 边覆盖：hard {sum(len(v) for v in graph.hard.values())} / "
        f"delayed {sum(len(v) for v in graph.delayed.values())} / "
        f"自身子孙 {self_edges} / wildcards {sum(len(v) for v in graph.wildcards.values())}"
        f"（命中 {wildcard_hits} 条，{wildcard_pct:.1f}%）",
        f"- 文件：{len(files)} 个非包入口模块（预算 {_FILE_BUDGET} 行/文件）",
        f"- deps.json sha256：{deps_digest}",
        "",
        "## 2. 逐模块影响面",
        "",
        "| 路径 | 上游 | 下游 | 命名测试 | E039 | 行数/预算 | risk |",
        "|---|---:|---:|---|---|---:|---|",
    ]
    no_anchor = over_budget = cycle_rows = 0
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        owner = _owner_of(path)
        try:
            named = derive_related_test_files(rel, ROOT / "tests")
        except (OSError, ValueError):
            named = []
        named_text = named[0] if named else "-"
        e039 = [k for k in SHARED_ENTRY_TESTS if shared_entry_key_matches(k, rel)]
        e039_text = ", ".join(e039) if e039 else "-"
        count = len(path.read_text(encoding="utf-8").splitlines())
        if owner in cycle_members:
            risk = "cycle"
            cycle_rows += 1
        elif count > _FILE_BUDGET:
            risk = "over-budget"
            over_budget += 1
        elif named_text == "-" and e039_text == "-":
            risk = "no-anchor"
            no_anchor += 1
        else:
            risk = "ok"
        lines.append(
            f"| `{rel}` | {len(graph.forward.get(owner, ()))} | "
            f"{len(graph.reverse.get(owner, ()))} | {named_text} | {e039_text} | "
            f"{count}/{_FILE_BUDGET} | {risk} |"
        )
    ok_rows = len(files) - cycle_rows - no_anchor - over_budget
    lines.extend(
        [
            "",
            f"> risk 汇总（共 {len(files)} 行）：cycle {cycle_rows} 条"
            "（包入口不单独成行，成员见第 1 节）；"
            f"no-anchor {no_anchor} 条；over-budget {over_budget} 条；ok {ok_rows} 条。",
            "",
        ]
    )
    return "\n".join(lines)


def gen_error_codes_md() -> str:
    from orchd.errors import ErrorCode

    lines = ["# 错误码表（生成物，勿手改）", ""]
    for err in ErrorCode:
        lines.append(f"- {err.name}")
    lines.append("")
    return "\n".join(lines)


def gen_commands_md() -> str:
    from orchd.cli.parser import _build_parser

    parser = _build_parser()
    cmds: list[str] = []
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices:
            cmds = sorted(action.choices)
    lines = ["# 命令表（生成物，勿手改）", ""]
    for cmd in cmds:
        lines.append(f"- {cmd}")
    lines.append("")
    return "\n".join(lines)


def gen_command_table_md() -> str:
    """命令总览表（正文生成块）：由 ``orchd/cli/manifest.py`` 元数据单源生成。

    每个顶层命令一行：参数列取 ``args``（子命令名 / 选项 / 占位符原样空格连接，
    无参记 ``-``）；读写 / 锁取 ``read_only`` / ``requires_lock``；用途即 ``purpose``
    （与 ``orchd docs list`` 的 ``commands`` 元数据同源）。块体约定：以换行开头
    结尾（``extract_block`` 取 begin 标记后到 end 标记前的原文，首尾换行使标记
    独占一行）。
    """
    from orchd.cli.manifest import COMMANDS

    lines = [
        "| 命令 | 参数 | 读写 | 锁 | 用途 |",
        "|------|------|------|----|------|",
    ]
    for item in COMMANDS:
        args = " ".join(item.args) if item.args else "-"
        lines.append(
            f"| `{item.name}` | {args} | {'只读' if item.read_only else '写'} "
            f"| {'是' if item.requires_lock else '否'} | {_md_cell(item.purpose)} |"
        )
    return "\n" + "\n".join(lines) + "\n"


def gen_compat_gates_md() -> str:
    """compat-policy §4 门禁映射表（正文生成块）：真源 ``docs_manifest.COMPAT_GATES``。"""
    from orchd.docs_manifest import COMPAT_GATES

    lines = ["| 变更面 | 门禁 |", "|---|---|"]
    for row in COMPAT_GATES:
        lines.append(f"| {row['surface']} | {row['gate']} |")
    return "\n" + "\n".join(lines) + "\n"


def gen_invariants_md() -> str:
    """kernel-contract §4 不变量清单（正文生成块）：真源 ``docs_manifest.KERNEL_INVARIANTS``。"""
    from orchd.docs_manifest import KERNEL_INVARIANTS

    lines = ["| # | 不变量 | 说明 | 测试锚点 |", "|---|---|---|---|"]
    for row in KERNEL_INVARIANTS:
        lines.append(
            f"| {row['id']} | {row['name']} | {row['note']} | {row['anchors']} |"
        )
    return "\n" + "\n".join(lines) + "\n"


def gen_constraints_md() -> str:
    """constraint-hardening §4 约束清单（正文生成块）：真源 ``docs_manifest.SINK_CONSTRAINTS``。"""
    from orchd.docs_manifest import SINK_CONSTRAINTS

    lines = ["| 约束 | 现状层 | 可沉性 | 状态 |", "|------|--------|--------|------|"]
    for row in SINK_CONSTRAINTS:
        lines.append(
            f"| {row['constraint']} | {row['layer']} | {row['sink']} | {row['status']} |"
        )
    return "\n" + "\n".join(lines) + "\n"


def gen_deps_summary_md() -> str:
    """conventions 依赖节快照（正文生成块）：由 ``.orchd/shared/deps.json`` 现算。"""
    import json as _json

    # deps.json 在引擎仓与宿主布局下都位于 <root>/.orchd/shared/。
    data = _json.loads(
        (ROOT / ".orchd" / "shared" / "deps.json").read_text(encoding="utf-8")
    )
    hard = data.get("hard", {})
    delayed = data.get("delayed", {})
    wildcards = data.get("wildcards", {})
    ratchet = (data.get("scc_ratchet") or {}).get("max_nodes", 0)
    lines = [
        f"- hard：{len(hard)} 模块 / {sum(len(v) for v in hard.values())} 边",
        f"- delayed：{len(delayed)} 模块 / {sum(len(v) for v in delayed.values())} 边",
        f"- 包级通配（限纯重导出）：{len(wildcards)} 项"
        f"（{', '.join(sorted(wildcards))}）",
        f"- SCC 棘轮：max_nodes {ratchet}（顶层 hard 图最大环上限，只降不升）",
    ]
    return "\n" + "\n".join(lines) + "\n"


def gen_drift_matrix_md() -> str:
    """契约漂移矩阵（整文件生成物）：真源 ``docs_manifest.DRIFT_MATRIX``。"""
    from orchd.docs_manifest import DRIFT_MATRIX
    lines = list(cast("list[str]", DRIFT_MATRIX["head_lines"]))
    lines.append("| 漂移域 | 真源 | 断言 | 门禁 |")
    lines.append(cast(str, DRIFT_MATRIX["sep"]))
    for row in cast("tuple[dict[str, str], ...]", DRIFT_MATRIX["rows"]):
        lines.append(
            f"| {row['domain']} | {row['source']} | {row['assertions']} "
            f"| {row['gates']} |"
        )
    lines.extend(cast("list[str]", DRIFT_MATRIX["tail_lines"]))
    return "\n".join(lines) + "\n"


def _walk_schema_fields(
    node: object, path: str, rows: list, xkeys: set
) -> None:
    """递归收集可达 property：properties 逐层、$defs 命名域、items 元素。

    rows 追加 ``(路径, property 节点, 是否 required)``；xkeys 汇总途经节点的
    全部 ``x-*`` 注解键（产物列由其动态出列，AC「每个 x-* 有对应列」）。
    """
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        required = set(node.get("required") or [])
        for name, prop in props.items():
            child = f"{path}.{name}" if path else name
            rows.append((child, prop, name in required))
            for key in prop:
                if key.startswith("x-"):
                    xkeys.add(key)
            _walk_schema_fields(prop, child, rows, xkeys)
    for defs_key in ("$defs", "definitions"):
        defs = node.get(defs_key)
        if isinstance(defs, dict):
            for name, sub in defs.items():
                _walk_schema_fields(
                    sub, f"{path}#{name}" if path else f"#{name}", rows, xkeys
                )
    items = node.get("items")
    if isinstance(items, dict):
        _walk_schema_fields(items, f"{path}[]", rows, xkeys)


def _md_cell(value: object) -> str:
    """markdown 表格单元格：标量直出，容器 JSON 紧凑；竖线与换行转义。"""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text or "-"


def gen_fields_md() -> str:
    """schema 全量字段表（A2 物化）：路径 / 类型 / required / enum / default /
    描述 + 全部 x-* 注解列。真源唯一 = ``schema/_master.schema.json``。"""
    schema = json.loads(
        (_resource_root() / "schema" / "_master.schema.json").read_text(
            encoding="utf-8"
        )
    )
    rows: list = []
    xkeys: set = set()
    _walk_schema_fields(schema, "", rows, xkeys)
    xcols = sorted(xkeys)
    lines = [
        "# 字段表（生成物，勿手改）",
        "",
        f"- 真源唯一 = `schema/_master.schema.json`；可达 property {len(rows)} 处"
        "（properties / $defs / items 递归）",
        "- `x-amendable: true` = amend 声明域白名单"
        "（`orchd/schema_policy.py::amendable_fields` 同源）；"
        "`x-terminal-attachable: true` = 终态可附加字段集",
        "- 重新生成：`python scripts/gen_contract_docs.py --write docs/_generated`",
        "",
        "| 字段路径 | 类型 | required | enum | default | 描述 |"
        + "".join(f" {key} |" for key in xcols),
        "|---|---|---|---|---|---|" + "---|" * len(xcols),
    ]
    for path, prop, required in rows:
        if "$ref" in prop:
            typ = "$ref:" + str(prop["$ref"]).split("/")[-1]
        elif "enum" in prop:
            typ = "enum"
        elif "oneOf" in prop:
            typ = "oneOf"
        elif "anyOf" in prop:
            typ = "anyOf"
        else:
            typ = prop.get("type") or "-"
        cells = [
            f"`{path}`",
            _md_cell(typ),
            "required" if required else "optional",
            _md_cell(prop.get("enum")),
            _md_cell(prop.get("default")),
            _md_cell(prop.get("description")),
        ] + [_md_cell(prop.get(key)) for key in xcols]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def gen_events_md() -> str:
    from orchd.contracts.events import CONTRACTS

    lines = ["# 事件表（生成物，勿手改）", ""]
    for etype in sorted(CONTRACTS):
        contract = CONTRACTS[etype]
        required = sorted(contract["required"])
        optional = sorted(contract["optional"])
        lines.append(f"## {etype}")
        lines.append("")
        lines.append(f"- required: {', '.join(required) or '（无）'}")
        lines.append(f"- optional: {', '.join(optional) or '（无）'}")
        lines.append("")
    return "\n".join(lines)


def gen_statemachine_md() -> str:
    from orchd.contracts.events import TRANSITION_TABLE

    lines = ["# 状态表（生成物，勿手改）", ""]
    for etype in sorted(TRANSITION_TABLE):
        entry = TRANSITION_TABLE[etype]
        allowed = sorted(entry["allowed_from"]) or ["（不受约束）"]
        target = entry["target"] or "（条件分支/无变更）"
        lines.append(f"## {etype}")
        lines.append("")
        lines.append(f"- allowed_from: {', '.join(allowed)}")
        lines.append(f"- target: {target}")
        lines.append("")
    return "\n".join(lines)


GENERATORS = {
    "error-codes.md": gen_error_codes_md,
    "commands.md": gen_commands_md,
    "fields.md": gen_fields_md,
    "events.md": gen_events_md,
    "statemachine.md": gen_statemachine_md,
    "dependency-graph.md": gen_dependency_graph_md,
}


def write_docs(out_dir: str | pathlib.Path) -> list[str]:
    """把六份产物写到 ``out_dir``（只读生成，调用方决定目标目录）。"""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for filename, func in GENERATORS.items():
        (out / filename).write_text(func(), encoding="utf-8")
        written.append(filename)
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "--write":
        print("usage: gen_contract_docs.py --write <out_dir>", file=sys.stderr)
        return 2
    write_docs(argv[2])
    return 0
