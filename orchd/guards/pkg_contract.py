"""宿主包契约 guard：布局 / 预算 / ``__init__`` 纯净 / 公开面 / 包内 DAG / 补丁点。

判据从引擎 ``tests/test_pkg_contract.py`` 提取为宿主可配置的独立检查（零复制）：
宿主在 ``guards.json`` 的 ``pkg_contract`` 选项里声明包路径、公开面基线与 DAG
规则，六类检查逐包运行，命中项带包名 id 返回 ``GuardFinding``。

宿主配置（``guards.json``）：

```json
{"schema_version": 1,
 "guards": {"pkg_contract": {"mode": "warn",
                             "packages": [{"key": "review",
                                           "module": "orchd.review",
                                           "domains": ["branch", "..."],
                                           "public_surface": ["..."],
                                           "declared_in": [["...", "branch"]],
                                           "line_budget": 300,
                                           "init_budget": 60,
                                           "capabilities": ["layout", "budget"]}]}}}
```

每个包声明（字段名与引擎 ``tests/pkg_contract_data.py`` 的 ``PackageContract``
一致，宿主可按需裁剪）：

- ``key``：包 id（报错前缀，必填）；
- ``module``：点分模块路径（必填，映射到 ``<project_root>/<module with /``)；
- ``legacy_file``：拆分前单文件（相对 project_root，可选，须已消失）；
- ``domains``：声明域（``*.py`` 的 stem，必填）；
- ``public_surface`` / ``declared_in``：公开面基线及声明域（``[[name, owner], ...]``）；
- ``leaves`` / ``orchestrators``：DAG 分层；``required_edges`` / ``allowed_edges``；
- ``patch_points``：``[[owner, name], ...]`` monkeypatch 打点；
- ``line_budget`` / ``init_budget`` / ``domain_budgets``：行数预算；
- ``capabilities``：启用哪几类检查（``layout``/``budget``/``init``/
  ``public_surface``/``dag``/``patch``，缺省全开）；
- ``forbidden_surface``：聚合入口不得绑定的缝隙名。

无配置或 ``packages`` 为空时返回空集（not_applicable）；非 Python 包（目录不存在
或没有 ``*.py``）跳过不误报。本模块只读：AST 解析与文件读取均纯本地，不起子
进程、不写盘。
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.guards import GuardEvalContext, GuardFinding, load_guards_config

__all__ = (
    "ALL_CAPABILITIES",
    "DEFAULT_GUARD_NAME",
    "PackageSpec",
    "PkgContractGuardConfigError",
    "PackageContractScan",
    "parse_policy",
    "declaration_findings",
    "resolve_package_dir",
    "domain_path",
    "intra_package_edges",
    "check_legacy_file",
    "check_layout_files",
    "check_layout",
    "check_domain_budget",
    "check_init_budget",
    "check_budget",
    "check_init_no_definitions",
    "check_all_literal",
    "check_init_purity",
    "check_public_bindings",
    "check_public_surface",
    "check_forbidden_surface",
    "check_dag_acyclic",
    "check_leaf_orchestration",
    "check_required_edges",
    "check_allowed_edges",
    "check_import_dag",
    "check_patch_reachability",
    "scan_package",
    "scan_packages",
    "evaluate",
    "register",
)

DEFAULT_GUARD_NAME = "pkg_contract"
ALL_CAPABILITIES = ("layout", "budget", "init", "public_surface", "dag", "patch")
KNOWN_OPTIONS = frozenset({"packages"})
_SPEC_KEYS = frozenset(
    {
        "key",
        "module",
        "legacy_file",
        "domains",
        "public_surface",
        "declared_in",
        "leaves",
        "orchestrators",
        "required_edges",
        "allowed_edges",
        "patch_points",
        "line_budget",
        "init_budget",
        "capabilities",
        "forbidden_surface",
        "domain_budgets",
    }
)


class PkgContractGuardConfigError(ValueError):
    """pkg_contract guard 配置非法（未知键/坏结构）。"""


@dataclass(frozen=True)
class PackageSpec:
    """宿主可配置的包契约声明（字段与引擎 ``PackageContract`` 对齐）。"""

    key: str
    module: str
    domains: tuple[str, ...]
    legacy_file: str = ""
    public_surface: tuple[str, ...] = ()
    declared_in: tuple[tuple[str, str], ...] = ()
    leaves: tuple[str, ...] = ()
    orchestrators: tuple[str, ...] = ()
    required_edges: tuple[tuple[str, tuple[str, ...]], ...] = ()
    allowed_edges: tuple[tuple[str, tuple[str, ...]], ...] = ()
    patch_points: tuple[tuple[str, str], ...] = ()
    line_budget: int = 0
    init_budget: int = 0
    capabilities: tuple[str, ...] = ALL_CAPABILITIES
    forbidden_surface: tuple[str, ...] = ()
    domain_budgets: tuple[tuple[str, int], ...] = ()


def _normalize_str_sequence(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PkgContractGuardConfigError(f"{field} 必须是字符串数组")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PkgContractGuardConfigError(f"{field} 含非字符串或空串项：{item!r}")
        out.append(item.strip())
    return tuple(out)


def _normalize_pairs(value: object, field: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PkgContractGuardConfigError(f"{field} 必须是二元组数组")
    out: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise PkgContractGuardConfigError(f"{field} 项必须是 [a, b] 形式：{item!r}")
        left, right = item
        if not isinstance(left, str) or not isinstance(right, str):
            raise PkgContractGuardConfigError(f"{field} 项必须都是字符串：{item!r}")
        out.append((left, right))
    return tuple(out)


def _normalize_edges(
    value: object, field: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PkgContractGuardConfigError(f"{field} 必须是 [起点, [终点...]] 数组")
    out: list[tuple[str, tuple[str, ...]]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise PkgContractGuardConfigError(f"{field} 项必须是 [起点, [终点...]]：{item!r}")
        head, tail = item
        if not isinstance(head, str) or not head.strip():
            raise PkgContractGuardConfigError(f"{field} 起点必须是字符串：{item!r}")
        out.append((head.strip(), _normalize_str_sequence(tail, f"{field}.{head}")))
    return tuple(out)


def _normalize_budget(value: object, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise PkgContractGuardConfigError(f"{field} 必须是整数")
    if value < 0:
        raise PkgContractGuardConfigError(f"{field} 不能为负：{value}")
    return value


def _normalize_domain_budgets(value: object) -> tuple[tuple[str, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PkgContractGuardConfigError("domain_budgets 必须是 [域, 行数] 数组")
    out: list[tuple[str, int]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise PkgContractGuardConfigError(f"domain_budgets 项必须是 [域, 行数]：{item!r}")
        domain, cap = item
        if not isinstance(domain, str) or not domain.strip():
            raise PkgContractGuardConfigError(f"domain_budgets 域必须是非空字符串：{item!r}")
        out.append((domain.strip(), _normalize_budget(cap, f"domain_budgets.{domain}")))
    return tuple(out)


def _parse_spec(raw: object, index: int) -> PackageSpec:
    if not isinstance(raw, Mapping):
        raise PkgContractGuardConfigError(f"packages[{index}] 必须是对象")
    unknown = sorted(set(raw) - _SPEC_KEYS)
    if unknown:
        raise PkgContractGuardConfigError(
            f"packages[{index}] 存在未知配置键：{', '.join(unknown)}"
        )
    key = raw.get("key")
    module = raw.get("module")
    if not isinstance(key, str) or not key.strip():
        raise PkgContractGuardConfigError(f"packages[{index}].key 必须是非空字符串")
    if not isinstance(module, str) or not module.strip():
        raise PkgContractGuardConfigError(f"packages[{index}].module 必须是非空字符串")
    capabilities = _normalize_str_sequence(raw.get("capabilities"), "capabilities")
    if not capabilities:
        capabilities = ALL_CAPABILITIES
    beyond = sorted(set(capabilities) - set(ALL_CAPABILITIES))
    if beyond:
        raise PkgContractGuardConfigError(
            f"packages[{index}].capabilities 含未登记检查类：{', '.join(beyond)}"
        )
    domains = _normalize_str_sequence(raw.get("domains"), "domains")
    if not domains:
        raise PkgContractGuardConfigError(f"packages[{index}].domains 不能为空")
    legacy = raw.get("legacy_file", "")
    if not isinstance(legacy, str):
        raise PkgContractGuardConfigError(f"packages[{index}].legacy_file 必须是字符串")
    return PackageSpec(
        key=key.strip(),
        module=module.strip(),
        domains=domains,
        legacy_file=legacy.strip(),
        public_surface=_normalize_str_sequence(raw.get("public_surface"), "public_surface"),
        declared_in=_normalize_pairs(raw.get("declared_in"), "declared_in"),
        leaves=_normalize_str_sequence(raw.get("leaves"), "leaves"),
        orchestrators=_normalize_str_sequence(raw.get("orchestrators"), "orchestrators"),
        required_edges=_normalize_edges(raw.get("required_edges"), "required_edges"),
        allowed_edges=_normalize_edges(raw.get("allowed_edges"), "allowed_edges"),
        patch_points=_normalize_pairs(raw.get("patch_points"), "patch_points"),
        line_budget=_normalize_budget(raw.get("line_budget"), "line_budget"),
        init_budget=_normalize_budget(raw.get("init_budget"), "init_budget"),
        capabilities=capabilities,
        forbidden_surface=_normalize_str_sequence(
            raw.get("forbidden_surface"), "forbidden_surface"
        ),
        domain_budgets=_normalize_domain_budgets(raw.get("domain_budgets")),
    )


def parse_policy(options: object) -> tuple[PackageSpec, ...]:
    """把 guards.json 的 pkg_contract options 解析为包声明列表（空集=未配置）。"""
    if options is None:
        return ()
    if not isinstance(options, Mapping):
        raise PkgContractGuardConfigError("pkg_contract 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise PkgContractGuardConfigError(
            f"pkg_contract 存在未知配置键：{', '.join(unknown)}"
        )
    raw_packages = options.get("packages", ())
    if not isinstance(raw_packages, (list, tuple)):
        raise PkgContractGuardConfigError("packages 必须是数组")
    return tuple(_parse_spec(raw, index) for index, raw in enumerate(raw_packages))


def resolve_package_dir(project_root: Path, module: str) -> Path:
    """点分模块路径 → 包目录（``orchd.review`` → ``<root>/orchd/review``）。"""
    parts = [part for part in module.split(".") if part]
    return project_root.joinpath(*parts)


def domain_path(project_root: Path, module: str, domain: str) -> Path:
    """声明域文件路径（``<root>/orchd/<pkg>/<domain>.py``）。"""
    return resolve_package_dir(project_root, module) / f"{domain}.py"


def _read_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_bindings(tree: ast.Module) -> set[str]:
    """模块顶层可绑定的名字（def/class/赋值/import/注释性 ``__all__`` 不计）。"""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.update(
                        elt.id for elt in target.elts if isinstance(elt, ast.Name)
                    )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    return names


def _reexport_bindings(tree: ast.Module) -> dict[str, str]:
    """``__init__`` 顶层从本包域重导出的名字 → 来源域。"""
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    bindings[alias.asname or alias.name] = node.module.split(".")[0]
    return bindings


def declaration_findings(spec: Any, *, all_keys: Iterable[str] = ()) -> list[str]:
    """声明自洽（防空转）：公开面/域/边/预算互相一致。"""
    key = str(spec.key)
    domains = set(spec.domains)
    findings: list[str] = []
    if not key or not domains:
        findings.append(f"{key or '<未命名>'} 登记为空")
    keys = [*all_keys]
    if keys and len(set(keys)) != len(keys):
        findings.append(f"包 id 重复：{keys}")
    if set(spec.capabilities) - set(ALL_CAPABILITIES):
        findings.append(
            f"{key} 启用了未登记的检查类："
            f"{sorted(set(spec.capabilities) - set(ALL_CAPABILITIES))}"
        )
    if "public_surface" in spec.capabilities:
        if not spec.public_surface:
            findings.append(f"{key} 启用公开面检查却未登记公开面")
        elif [name for name, _ in spec.declared_in] != list(spec.public_surface):
            findings.append(f"{key} declared_in 键序与 public_surface 不一致")
    elif spec.public_surface or spec.declared_in:
        findings.append(f"{key} 未启用公开面检查却登记了公开面")
    if not set(spec.leaves) <= domains or not set(spec.orchestrators) <= domains:
        findings.append(f"{key} 叶/编排器引用了未声明域")
    overlap = sorted(set(spec.leaves) & set(spec.orchestrators))
    if overlap:
        findings.append(f"{key} 叶与编排器重叠：{overlap}")
    leaked = sorted(set(spec.forbidden_surface) & set(spec.public_surface))
    if leaked:
        findings.append(f"{key} 禁止面与公开面重叠：{leaked}")
    for name, owner in spec.declared_in:
        if owner not in domains:
            findings.append(f"{key} 公开名 {name} 的声明域 {owner} 未在 domains 内")
    for domain, targets in (*spec.required_edges, *spec.allowed_edges):
        if domain not in domains:
            findings.append(f"{key} 边起点 {domain} 未声明")
        beyond = sorted(set(targets) - domains)
        if beyond:
            findings.append(f"{key} 边终点越界：{beyond}")
    for owner, name in spec.patch_points:
        if owner not in domains:
            findings.append(f"{key} 补丁点归属域 {owner} 未声明")
    budget_keys = [domain for domain, _cap in spec.domain_budgets]
    if len(set(budget_keys)) != len(budget_keys):
        findings.append(f"{key} 逐域预算重复登记：{budget_keys}")
    for domain, cap in spec.domain_budgets:
        if domain not in domains:
            findings.append(f"{key} 逐域预算归属域 {domain} 未在 domains 内")
        if not isinstance(cap, int) or cap <= 0:
            findings.append(f"{key}/{domain} 逐域预算须为正整数：{cap!r}")
    return findings


def _enabled(spec: Any, capability: str) -> bool:
    return capability in spec.capabilities


def intra_package_edges(project_root: Path, spec: Any, domain: str) -> set[str]:
    """域文件对**本包**其它域的**顶层** import 集合（函数级惰性导入不计）。"""
    path = domain_path(project_root, spec.module, domain)
    tree = _read_tree(path)
    declared = set(spec.domains)
    edges: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            edges.add(node.module.split(".")[0])
        else:
            for alias in node.names:
                edges.add(alias.name)
    return edges & declared


def check_legacy_file(project_root: Path, spec: Any) -> list[str]:
    """拆分前单文件已消失（布局类）。"""
    if not _enabled(spec, "layout") or not spec.legacy_file:
        return []
    if not (project_root / spec.legacy_file).exists():
        return []
    return [f"{spec.key} {spec.legacy_file} 仍存在（应已被 {spec.module} 包接替）"]


def check_layout_files(project_root: Path, spec: Any) -> list[str]:
    """包目录 ``*.py`` 与声明域逐一相等（``__init__`` 另计）。"""
    if not _enabled(spec, "layout"):
        return []
    package_dir = resolve_package_dir(project_root, spec.module)
    if not package_dir.is_dir():
        return []
    files = sorted(package_dir.glob("*.py"))
    if not files:
        return []
    actual = {path.stem for path in files}
    declared = set(spec.domains) | {"__init__"}
    extra = sorted(actual - declared)
    missing = sorted(declared - actual)
    if not extra and not missing:
        return []
    return [f"{spec.key} 包文件与声明域不等：多出 {extra}，缺失 {missing}"]


def check_layout(project_root: Path, spec: Any) -> list[str]:
    """布局：包目录 ``*.py`` 与声明域逐一相等；拆分前单文件已消失。"""
    return [
        *check_layout_files(project_root, spec),
        *check_legacy_file(project_root, spec),
    ]


def check_domain_budget(project_root: Path, spec: Any) -> list[str]:
    """逐域文件行数不超过 ``line_budget``（``domain_budgets`` 可逐域覆盖）。"""
    if not _enabled(spec, "budget"):
        return []
    findings: list[str] = []
    budgets = dict(spec.domain_budgets)
    for domain in spec.domains:
        path = domain_path(project_root, spec.module, domain)
        if not path.is_file():
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        cap = budgets.get(domain, spec.line_budget)
        if cap and lines > cap:
            findings.append(f"{spec.key}/{domain}.py {lines} 行超预算 {cap}")
    return findings


def check_init_budget(project_root: Path, spec: Any) -> list[str]:
    """聚合入口行数不超过 ``init_budget``。"""
    if not _enabled(spec, "budget") or not spec.init_budget:
        return []
    init_path = resolve_package_dir(project_root, spec.module) / "__init__.py"
    if not init_path.is_file():
        return []
    lines = len(init_path.read_text(encoding="utf-8").splitlines())
    if lines <= spec.init_budget:
        return []
    return [f"{spec.key}/__init__.py {lines} 行超预算 {spec.init_budget}"]


def check_budget(project_root: Path, spec: Any) -> list[str]:
    """行数预算：逐域文件与聚合入口均不超过声明上限。"""
    return [
        *check_domain_budget(project_root, spec),
        *check_init_budget(project_root, spec),
    ]


def _all_literal(tree: ast.Module) -> tuple[str, ...] | None:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "__all__" not in targets:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None
        if not isinstance(value, (list, tuple)):
            return None
        return tuple(str(item) for item in value)
    return None


def _init_tree(project_root: Path, spec: Any) -> ast.Module | None:
    init_path = resolve_package_dir(project_root, spec.module) / "__init__.py"
    if not init_path.is_file():
        return None
    return _read_tree(init_path)


def check_init_no_definitions(project_root: Path, spec: Any) -> list[str]:
    """``__init__`` 零顶层函数/类定义（纯重导出）。"""
    if not _enabled(spec, "init"):
        return []
    tree = _init_tree(project_root, spec)
    if tree is None:
        return []
    bad = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if not bad:
        return []
    return [f"{spec.key}/__init__.py 含顶层定义（须纯重导出）：{bad}"]


def check_all_literal(project_root: Path, spec: Any) -> list[str]:
    """``__all__`` 是字面量且与登记公开面逐条相等。"""
    if not _enabled(spec, "public_surface"):
        return []
    tree = _init_tree(project_root, spec)
    if tree is None:
        return []
    exported = _all_literal(tree)
    if exported is None:
        return [f"{spec.key}/__init__.py 缺字面量 __all__"]
    extra = sorted(set(exported) - set(spec.public_surface))
    missing = sorted(set(spec.public_surface) - set(exported))
    if exported == tuple(spec.public_surface):
        return []
    return [f"{spec.key} __all__ 与登记公开面不等：多出 {extra}，缺失 {missing}"]


def check_init_purity(project_root: Path, spec: Any) -> list[str]:
    """``__init__`` 纯净：零顶层定义，``__all__`` 与登记公开面逐条相等。"""
    return [
        *check_init_no_definitions(project_root, spec),
        *check_all_literal(project_root, spec),
    ]


def _runtime_identity_findings(spec: Any) -> list[str]:
    """运行时同一对象判据（可选）：公开名在入口与声明域为同一对象。"""
    import importlib

    findings: list[str] = []
    try:
        module = importlib.import_module(spec.module)
    except Exception as exc:  # noqa: BLE001 - 导入失败按漂移报告，不上抛
        return [f"{spec.key} 聚合入口不可导入：{exc}"]
    for name, owner in spec.declared_in:
        try:
            owner_module = importlib.import_module(f"{spec.module}.{owner}")
        except Exception as exc:  # noqa: BLE001
            findings.append(f"{spec.key}/{owner} 域模块不可导入：{exc}")
            continue
        if getattr(module, name, None) is not getattr(owner_module, name, None):
            findings.append(
                f"{spec.key} 公开面绑定漂移：{name} 在 {spec.module} 与 "
                f"{spec.module}.{owner} 不是同一对象"
            )
    return findings


def check_public_bindings(project_root: Path, spec: Any) -> list[str]:
    """公开面绑定：每个公开名都从声明域重导出，且声明域确实定义该名。"""
    if not _enabled(spec, "public_surface"):
        return []
    package_dir = resolve_package_dir(project_root, spec.module)
    init_path = package_dir / "__init__.py"
    if not init_path.is_file():
        return []
    findings: list[str] = []
    init_tree = _read_tree(init_path)
    bindings = _reexport_bindings(init_tree)
    exported = _all_literal(init_tree)
    if exported is not None:
        for name in spec.public_surface:
            if name not in exported:
                findings.append(f"{spec.key} __all__ 未包含公开名：{name}")
    for name, owner in spec.declared_in:
        source = bindings.get(name)
        if source != owner:
            findings.append(
                f"{spec.key} 公开名 {name} 未从声明域 {owner} 重导出"
                f"（实际来源：{source or '未绑定'}）"
            )
            continue
        owner_module_path = package_dir / f"{owner}.py"
        if not owner_module_path.is_file():
            findings.append(f"{spec.key} 声明域文件缺失：{owner}.py")
            continue
        if name not in _module_bindings(_read_tree(owner_module_path)):
            findings.append(f"{spec.key} 公开名 {name} 在声明域 {owner} 未定义")
    return findings


def check_public_surface(
    project_root: Path, spec: Any, *, runtime_identity: bool = False
) -> list[str]:
    """公开面：输入从声明域重导出，且（可选）与声明域是同一对象。"""
    findings = check_public_bindings(project_root, spec)
    if runtime_identity and not findings:
        findings.extend(_runtime_identity_findings(spec))
    return findings


def check_forbidden_surface(project_root: Path, spec: Any) -> list[str]:
    """禁止面：聚合入口不得再绑定已迁移到域落点的缝隙名。"""
    if not spec.forbidden_surface:
        return []
    package_dir = resolve_package_dir(project_root, spec.module)
    init_path = package_dir / "__init__.py"
    if not init_path.is_file():
        return []
    tree = _read_tree(init_path)
    bindings = _reexport_bindings(tree)
    available = _module_bindings(tree) | set(bindings)
    rebound = sorted(name for name in spec.forbidden_surface if name in available)
    if not rebound:
        return []
    return [f"{spec.key} 聚合入口出现缝隙绑定 {rebound}（打点应打域模块落点）"]


def _dag_edges(project_root: Path, spec: Any) -> dict[str, set[str]]:
    return {
        domain: intra_package_edges(project_root, spec, domain)
        for domain in spec.domains
        if domain_path(project_root, spec.module, domain).is_file()
    }


def check_dag_acyclic(project_root: Path, spec: Any) -> list[str]:
    """包内顶层 import 无环（DFS 报首个回边路径）。"""
    if not _enabled(spec, "dag"):
        return []
    if not resolve_package_dir(project_root, spec.module).is_dir():
        return []
    edges = _dag_edges(project_root, spec)
    findings: list[str] = []
    visiting: list[str] = []
    done: set[str] = set()

    def walk(node: str) -> None:
        if node in done or node in visiting:
            if node in visiting:
                cycle = [*visiting[visiting.index(node):], node]
                findings.append(f"{spec.key} 包内 import 成环：{' -> '.join(cycle)}")
            return
        visiting.append(node)
        for target in sorted(edges.get(node, set())):
            walk(target)
        visiting.pop()
        done.add(node)

    for domain in spec.domains:
        walk(domain)
    return findings


def check_leaf_orchestration(project_root: Path, spec: Any) -> list[str]:
    """叶域不 import 编排域。"""
    if not _enabled(spec, "dag"):
        return []
    if not resolve_package_dir(project_root, spec.module).is_dir():
        return []
    edges = _dag_edges(project_root, spec)
    orchestrators = set(spec.orchestrators)
    findings: list[str] = []
    for leaf in spec.leaves:
        hits = sorted(edges.get(leaf, set()) & orchestrators)
        if hits:
            findings.append(f"{spec.key} 叶域 {leaf} import 了编排域：{hits}")
    return findings


def check_required_edges(project_root: Path, spec: Any) -> list[str]:
    """声明的必含 import 边全部在场。"""
    if not _enabled(spec, "dag"):
        return []
    if not resolve_package_dir(project_root, spec.module).is_dir():
        return []
    edges = _dag_edges(project_root, spec)
    findings: list[str] = []
    for domain, targets in spec.required_edges:
        missing = sorted(set(targets) - edges.get(domain, set()))
        if missing:
            findings.append(f"{spec.key} {domain} 缺必含 import 边：{missing}")
    return findings


def check_allowed_edges(project_root: Path, spec: Any) -> list[str]:
    """实际 import 边不越出允许集合。"""
    if not _enabled(spec, "dag"):
        return []
    if not resolve_package_dir(project_root, spec.module).is_dir():
        return []
    edges = _dag_edges(project_root, spec)
    findings: list[str] = []
    for domain, allowed in spec.allowed_edges:
        exceeded = sorted(edges.get(domain, set()) - set(allowed))
        if exceeded:
            findings.append(f"{spec.key} {domain} 越出允许 import 边：{exceeded}")
    return findings


def check_import_dag(project_root: Path, spec: Any) -> list[str]:
    """包内 import DAG：无环、叶不导编排器、必含边在场、允许边不越界。"""
    return [
        *check_dag_acyclic(project_root, spec),
        *check_leaf_orchestration(project_root, spec),
        *check_required_edges(project_root, spec),
        *check_allowed_edges(project_root, spec),
    ]


def check_patch_reachability(project_root: Path, spec: Any) -> list[str]:
    """补丁可达：打点在归属域模块顶层可绑定。

    公开名入口绑定一致性由 :func:`check_public_bindings`（及可选运行时同一对象
    判据）承担；此处只保证 monkeypatch 目标在归属域模块可达，防打点静默失效。
    """
    if "patch" not in spec.capabilities or not spec.patch_points:
        return []
    package_dir = resolve_package_dir(project_root, spec.module)
    findings: list[str] = []
    for owner, name in spec.patch_points:
        path = package_dir / f"{owner}.py"
        if not path.is_file():
            findings.append(f"{spec.key} 补丁点归属域文件缺失：{owner}.py")
            continue
        if name not in _module_bindings(_read_tree(path)):
            findings.append(f"{spec.key} 补丁点不可达：{spec.module}.{owner}.{name}")
    return findings


@dataclass(frozen=True)
class PackageContractScan:
    """一次包契约扫描的结构化结果。"""

    findings: tuple[GuardFinding, ...]

    def __bool__(self) -> bool:
        return bool(self.findings)


def scan_package(
    project_root: Path, spec: Any, *, runtime_identity: bool = False
) -> tuple[GuardFinding, ...]:
    """单包六类检查 → 带包名 id 的 findings（非 Python 包跳过）。"""
    package_dir = resolve_package_dir(project_root, spec.module)
    if not package_dir.is_dir():
        return ()
    if not any(package_dir.glob("*.py")):
        return ()
    messages: list[str] = []
    messages.extend(check_layout(project_root, spec))
    messages.extend(check_budget(project_root, spec))
    messages.extend(check_init_purity(project_root, spec))
    messages.extend(check_public_surface(project_root, spec, runtime_identity=runtime_identity))
    messages.extend(check_forbidden_surface(project_root, spec))
    messages.extend(check_import_dag(project_root, spec))
    messages.extend(check_patch_reachability(project_root, spec))
    return tuple(
        GuardFinding(
            guard=DEFAULT_GUARD_NAME,
            message=message,
            context={"package": str(spec.key)},
        )
        for message in messages
    )


def scan_packages(
    project_root: Path,
    specs: Iterable[Any],
    *,
    runtime_identity: bool = False,
) -> PackageContractScan:
    """逐包扫描，并先做声明自洽（含包 id 唯一性）。"""
    spec_list = list(specs)
    findings: list[GuardFinding] = []
    all_keys = [str(spec.key) for spec in spec_list]
    for spec in spec_list:
        declaration = declaration_findings(spec, all_keys=all_keys)
        findings.extend(
            GuardFinding(
                guard=DEFAULT_GUARD_NAME,
                message=message,
                context={"package": str(spec.key), "kind": "declaration"},
            )
            for message in declaration
        )
        findings.extend(
            scan_package(project_root, spec, runtime_identity=runtime_identity)
        )
    return PackageContractScan(findings=tuple(findings))


def _configured_packages(project_root: Path) -> tuple[PackageSpec, ...]:
    config = load_guards_config(project_root)
    entry = next(
        (entry for entry in config.entries if entry.name == DEFAULT_GUARD_NAME), None
    )
    return parse_policy(entry.options if entry is not None else None)


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """按宿主配置逐包跑六类检查；配置非法时抛 RuntimeError 降级留痕。"""
    if context.project_root is None:
        return ()
    try:
        packages = _configured_packages(context.project_root)
    except PkgContractGuardConfigError as exc:
        raise RuntimeError(f"pkg_contract guard 配置非法：{exc}") from exc
    if not packages:
        return ()
    try:
        scan = scan_packages(context.project_root, packages)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError(f"pkg_contract guard 降级（scan_failed）：{exc}") from exc
    return scan.findings


def register() -> None:
    """把本评估器登记到 guards 注册表（幂等）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator(DEFAULT_GUARD_NAME, evaluate)


register()
