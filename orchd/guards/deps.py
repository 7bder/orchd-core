"""宿主依赖方向 guard：按宿主 ``deps.json`` 对拍 AST import 边与环。

判定复用引擎查询层 :mod:`orchd.deps`（零复制实现）：

- ``ast_edges`` 解析扫描 root 下全部 ``.py`` 的 import（去自身/子孙边）；
- ``coverage`` 判「未声明边 / 仅被 legacy 包级记法覆盖」；
- hard/delayed 分组与真源表逐边对拍（顶层 import 归 hard、函数/类内归 delayed）；
- ``cycles`` 只看顶层边报强连通分量（默认不含 delayed 假环）。

宿主配置（``guards.json``）：

```json
{"schema_version": 1,
 "guards": {"deps": {"mode": "warn",
                     "deps": ".orchd/shared/deps.json",
                     "root": "src/app",
                     "package": "app"}}}
```

- ``deps``：宿主依赖真源路径（项目内相对路径，默认 ``.orchd/shared/deps.json``）；
- ``root``：扫描根目录（项目内相对路径，默认 ``orchd``）；
- ``package``：扫描根对应的包名（可省略，缺省取 ``root`` 目录名）；
  目录名与包名不一致（如 ``src/app`` 包名 ``app``）时必须显式给出。

缺 deps.json、扫描 root 不可读或真源结构非法时，评估器抛 ``RuntimeError``
（done 期降级留痕，不误红）；命中未声明边/分组漂移/环时逐条返回 ``GuardFinding``，
``context`` 带 ``kind`` 与 ``owner``/``target`` 或 ``members``，供 warn/hard 两档
生成可操作报告。本模块只读：不写盘、不起子进程，AST 解析与 JSON 读取均纯本地。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import orchd.deps as deps_query
from orchd.guards import (
    MODE_HARD,
    MODE_OFF,
    MODE_WARN,
    GuardEvalContext,
    GuardFinding,
    load_guards_config,
)
from orchd.repo_role import is_engine_repo

__all__ = (
    "DEFAULT_DEPS_REL",
    "DEFAULT_ROOT",
    "DepsGateDecision",
    "DepsPolicy",
    "DepsGuardConfigError",
    "GATE_OFF",
    "GATE_OK",
    "GATE_SKIPPED",
    "GATE_WARN",
    "GATE_HARD",
    "REASON_NO_DEPS",
    "REASON_GRAPH_UNAVAILABLE",
    "decide_gate",
    "gate_policy",
    "parse_policy",
    "scan_dependency_direction",
    "evaluate",
    "register",
)

# 统一严重度判定结果（注册预检 / done 门禁 / 宿主 guard 三处共用）。
GATE_OFF = "off"
GATE_SKIPPED = "skipped"
GATE_WARN = "warn"
GATE_HARD = "hard"
GATE_OK = "ok"

REASON_NO_DEPS = "no_deps"
REASON_GRAPH_UNAVAILABLE = "deps_graph_unavailable"

DEFAULT_DEPS_REL = ".orchd/shared/deps.json"
DEFAULT_ROOT = "orchd"
KNOWN_OPTIONS = frozenset({"deps", "root", "package"})


class DepsGuardConfigError(ValueError):
    """deps guard 配置非法（未知键/坏路径）。"""


@dataclass(frozen=True)
class DepsPolicy:
    """宿主可配置的依赖扫描面。"""

    deps_rel: str = DEFAULT_DEPS_REL
    root: str = DEFAULT_ROOT
    package: str | None = None


def _normalize_rel_path(text: object, field: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise DepsGuardConfigError(f"{field} 必须是非空字符串")
    raw = text.strip().replace("\\", "/")
    if raw.startswith("/") or ":" in raw:
        raise DepsGuardConfigError(f"{field} 必须是项目内相对路径：{text!r}")
    rel = raw.strip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        raise DepsGuardConfigError(f"{field} 不能包含空段/./..：{text!r}")
    return rel


def parse_policy(options: object) -> DepsPolicy:
    """把 guards.json 的 deps options 解析为 :class:`DepsPolicy`。"""
    if options is None:
        return DepsPolicy()
    if not isinstance(options, Mapping):
        raise DepsGuardConfigError("deps 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise DepsGuardConfigError(f"deps 存在未知配置键：{', '.join(unknown)}")
    deps_rel = _normalize_rel_path(
        options.get("deps", DEFAULT_DEPS_REL), "deps"
    )
    root = _normalize_rel_path(options.get("root", DEFAULT_ROOT), "root")
    package = options.get("package")
    if package is not None:
        if not isinstance(package, str) or not package.strip():
            raise DepsGuardConfigError("package 必须是非空字符串")
        package = package.strip()
        if "/" in package or "\\" in package or ":" in package:
            raise DepsGuardConfigError(f"package 必须是点分模块名：{package!r}")
    return DepsPolicy(deps_rel=deps_rel, root=root, package=package)


@dataclass(frozen=True)
class DependencyDirectionScan:
    """一次依赖方向扫描的结构化结果。"""

    undeclared: tuple[str, ...]
    wildcard_only: tuple[str, ...]
    partition_diffs: tuple[str, ...]
    cycles: tuple[tuple[str, ...], ...]


def _actual_partition(
    edges: Mapping[str, Mapping[str, bool]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """AST 边 → (hard, delayed) 分组（去自身/子孙边）。"""
    hard: dict[str, set[str]] = {}
    delayed: dict[str, set[str]] = {}
    for owner, targets in edges.items():
        for target, top_level in targets.items():
            if target == owner or target.startswith(owner + "."):
                continue
            (hard if top_level else delayed).setdefault(owner, set()).add(target)
    return hard, delayed


def _as_edge_set(table: Mapping[str, set[str]]) -> set[tuple[str, str]]:
    return {(owner, target) for owner, targets in table.items() for target in targets}


def _table_as_sets(
    table: Mapping[str, tuple[str, ...]],
) -> dict[str, set[str]]:
    return {owner: set(targets) for owner, targets in table.items()}


def scan_dependency_direction(
    project_root: Path, policy: DepsPolicy
) -> DependencyDirectionScan:
    """扫描宿主依赖方向（读 deps.json + AST），返回结构化违规。"""
    deps_path = project_root / policy.deps_rel
    root_dir = project_root / policy.root
    if not deps_path.is_file():
        raise RuntimeError(
            f"deps guard 降级（no_deps）：宿主依赖真源不存在：{policy.deps_rel}"
        )
    if not root_dir.is_dir():
        raise RuntimeError(
            f"deps guard 降级（no_root）：扫描根目录不存在：{policy.root}"
        )
    try:
        graph = deps_query.load(str(deps_path))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"deps guard 降级（bad_deps）：deps.json 非法：{exc}") from exc

    edges = deps_query.ast_edges(root=root_dir, package=policy.package)
    actual = {
        owner: tuple(sorted(targets)) for owner, targets in edges.items()
    }
    coverage = deps_query.coverage(actual, graph=graph)

    actual_hard, actual_delayed = _actual_partition(edges)
    json_hard = _table_as_sets(graph.hard)
    json_delayed = _table_as_sets(graph.delayed)
    partition_diffs: list[str] = []
    if actual_hard != json_hard:
        extra = sorted(
            f"{owner} -> {target}"
            for owner in set(actual_hard) | set(json_hard)
            for target in actual_hard.get(owner, set()) ^ json_hard.get(owner, set())
        )
        partition_diffs.append(
            "hard 表与 AST 顶层 import 分组不一致：" + ", ".join(extra[:8])
        )
    if actual_delayed != json_delayed:
        extra = sorted(
            f"{owner} -> {target}"
            for owner in set(actual_delayed) | set(json_delayed)
            for target in actual_delayed.get(owner, set())
            ^ json_delayed.get(owner, set())
        )
        partition_diffs.append(
            "delayed 表与 AST 函数内 import 分组不一致：" + ", ".join(extra[:8])
        )
    overlap = _as_edge_set(actual_hard) & _as_edge_set(actual_delayed)
    if overlap:
        partition_diffs.append(
            "hard 与 delayed 存在交集："
            + ", ".join(sorted(f"{o} -> {t}" for o, t in overlap)[:8])
        )

    try:
        cycle_components = deps_query.cycles(path=str(deps_path))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"deps guard 降级（bad_deps）：deps.json 非法：{exc}") from exc

    return DependencyDirectionScan(
        undeclared=tuple(coverage["undeclared"]),
        wildcard_only=tuple(coverage["wildcard_only"]),
        partition_diffs=tuple(partition_diffs),
        cycles=tuple(cycle_components),
    )


def gate_policy(project_root: str | Path) -> tuple[DepsPolicy, str]:
    """读宿主配置，返回 ``(扫描面, 档位)``；无 deps 段时为默认扫描面 + ``off``。

    注册预检、done 门禁与 ``orchd deps`` 子命令共用本函数，保证三者对
    ``deps`` / ``root`` / ``package`` 的解读单一真源（``off`` 由调用方决定
    是否按引擎默认档位覆盖）。
    """
    config = load_guards_config(Path(project_root))
    entry = next((entry for entry in config.entries if entry.name == "deps"), None)
    if entry is None:
        return DepsPolicy(), MODE_OFF
    return parse_policy(entry.options), entry.mode


@dataclass(frozen=True)
class DepsGateDecision:
    """deps 门禁严重度统一判定（注册预检 / done 门禁 / 宿主 guard 共用）。

    ``status`` 取值见 ``GATE_*``：``ok`` 表示图可用（``graph`` 非空）、
    ``skipped`` 表示宿主缺图（不阻断、不告警）、``warn`` 表示宿主坏图仅告警、
    ``hard`` 表示坏图必须硬阻断（引擎仓恒 hard，宿主按 ``guards.deps=hard``）、
    ``off`` 表示宿主显式关闭。
    """

    status: str
    mode: str
    is_engine: bool
    policy: DepsPolicy
    graph: deps_query.DepsGraph | None = None
    reason: str = ""
    message: str = ""


def decide_gate(project_root: str | Path) -> DepsGateDecision:
    """按仓角色与配置判定 deps 门禁档位；坏图不再 fail-open。

    - 引擎仓（自托管）：扫描面固定 ``DepsPolicy()``，档位恒 ``hard``，坏图 → hard；
    - 宿主仓：档位取 ``guards.json`` 的 ``deps.mode``；缺图 → skipped，
      坏图按 warn/hard 分档，``off`` 连读都不读。

    缺图（``no_deps``）在引擎仓同样跳过：引擎仓必有图，缺失视为尚未初始化，
    保持既有 ``skip`` 语义；坏图（坏 JSON / 坏 schema）在引擎仓必须硬阻断。
    """
    root = Path(project_root)
    engine = is_engine_repo(root)
    policy, mode = gate_policy(root)
    if engine:
        policy = DepsPolicy()
        mode = MODE_HARD
    if mode == MODE_OFF:
        return DepsGateDecision(
            status=GATE_OFF, mode=mode, is_engine=engine, policy=policy
        )
    deps_path = root / policy.deps_rel
    if not deps_path.is_file():
        return DepsGateDecision(
            status=GATE_SKIPPED,
            mode=mode,
            is_engine=engine,
            policy=policy,
            reason=REASON_NO_DEPS,
            message=f"依赖真源不存在：{policy.deps_rel}（deps 门禁跳过）",
        )
    try:
        graph = deps_query.load(str(deps_path))
    except (ValueError, OSError) as exc:
        status = GATE_HARD if mode == MODE_HARD else GATE_WARN
        return DepsGateDecision(
            status=status,
            mode=mode,
            is_engine=engine,
            policy=policy,
            reason=REASON_GRAPH_UNAVAILABLE,
            message=f"deps.json 不可用（{policy.deps_rel}）：{exc}",
        )
    return DepsGateDecision(
        status=GATE_OK, mode=mode, is_engine=engine, policy=policy, graph=graph
    )


def _findings(scan: DependencyDirectionScan) -> tuple[GuardFinding, ...]:
    found: list[GuardFinding] = []
    for edge in scan.undeclared:
        owner, _, target = edge.partition(" -> ")
        found.append(
            GuardFinding(
                guard="deps",
                message=f"未声明依赖边：{edge}",
                context={"kind": "undeclared", "owner": owner, "target": target},
            )
        )
    for edge in scan.wildcard_only:
        owner, _, target = edge.partition(" -> ")
        found.append(
            GuardFinding(
                guard="deps",
                message=f"仅被 legacy 包级记法覆盖的依赖边：{edge}",
                context={"kind": "wildcard_only", "owner": owner, "target": target},
            )
        )
    for diff in scan.partition_diffs:
        found.append(
            GuardFinding(guard="deps", message=diff, context={"kind": "partition"})
        )
    for members in scan.cycles:
        found.append(
            GuardFinding(
                guard="deps",
                message="依赖环：" + " -> ".join(members) + " -> " + members[0],
                context={"kind": "cycle", "members": list(members)},
            )
        )
    return tuple(found)


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """按宿主配置扫描依赖方向；缺图 skip、坏图按档位 warn/hard 留痕。

    坏图不再抛 ``RuntimeError``（那会在 hard 档被通用异常捕获降级为 failed，
    形同放行）：改为返回一条 ``kind=deps_graph_unavailable`` 的 finding，由
    ``_record_host_guards`` 按 ``warn``（degraded）/ ``hard``（E039）分派。
    """
    if context.project_root is None:
        return ()
    try:
        decision = decide_gate(context.project_root)
    except DepsGuardConfigError as exc:
        raise RuntimeError(f"deps guard 配置非法：{exc}") from exc
    if decision.status in (GATE_OFF, GATE_SKIPPED):
        return ()
    if decision.status in (GATE_WARN, GATE_HARD):
        return (
            GuardFinding(
                guard="deps",
                message=decision.message,
                context={
                    "kind": REASON_GRAPH_UNAVAILABLE,
                    "reason": decision.reason,
                    "mode": decision.mode,
                    "is_engine": decision.is_engine,
                },
            ),
        )
    try:
        scan = scan_dependency_direction(context.project_root, decision.policy)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError(f"deps guard 降级（scan_failed）：{exc}") from exc
    return _findings(scan)


def register() -> None:
    """把本评估器登记到 guards 注册表（幂等）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator("deps", evaluate)


register()
