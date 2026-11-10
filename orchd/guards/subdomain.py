"""宿主 guard ``subdomain``：子域完备性（宿主可配置包清单 / 豁免理由）。

把引擎 ``orchd/subdomain_census.py`` 的五条纯只读判据（作用域包内未决策模块、未决策包、
命名空间目录、未决策顶层模块、登记面死条目）泛化为宿主可在 ``.orchd/shared/guards.json``
里声明的扫描面；五条判据函数**不复制**，直接复用引擎模块（本 guard 只做
「配置 → 每根扫描面」适配与 findings 映射）。

配置示例::

    {"schema_version": 1,
     "guards": {"subdomain": {"mode": "warn", "roots": ["src"],
                              "packages": {"src/app/core": {
                                  "modules": ["src/app/core/api.py"],
                                  "reason": "由 tests/test_core.py 覆盖"}},
                              "other_packages": {"src/app/legacy": "历史域，包级豁免"},
                              "top_level": {"src/app/main.py": "入口，由 e2e 覆盖"},
                              "registered_modules": ["src/app/shared/util.py"]}}}

- ``roots``：必填扫描根（项目内相对目录，可多个；每根独立构造一层扫描面）；
- ``packages``：作用域包（模块级决策）→ ``{"modules": [...], "reason": "非空理由"}``；
- ``other_packages`` / ``top_level``：包级 / 顶层单文件豁免理由；
- ``registered_modules``：已登记共享入口的模块（对齐引擎 ``SHARED_ENTRY_TESTS`` 语义）。

纪律：只读（零 git、零写盘、零子进程）；未配置 / options 为空 → 返回空
（``not_applicable``）不误红；配置存在但非法 → 抛 ``RuntimeError`` 降级留痕（不阻断
done）；roots 指向的目录不存在时逐根报 ``missing_scan_root``（防守空）。宿主侧没有引擎的
shared-entry gate，故本 guard 额外校验 ``registered_modules`` 指向的文件存在
（``stale_registered_module``）——这是唯一宿主侧增补判据，其余五条与引擎同源。

findings 的 ``context`` 带稳定 ``rule`` 与 ``root`` / ``entry``，供 warn/hard 两档生成
可操作报告。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from orchd.guards import GuardEvalContext, GuardFinding, load_guards_config
from orchd.subdomain_census import (
    CensusScope,
    SubdomainDecision,
    stale_registry_entries,
    subdomain_unaccounted,
    undeclared_namespace_dirs,
    undeclared_packages,
    undeclared_top_level_modules,
)

__all__ = (
    "KNOWN_OPTIONS",
    "SubdomainGuardConfigError",
    "SubdomainPolicy",
    "evaluate",
    "parse_policy",
    "register",
)

OPTION_ROOTS = "roots"
OPTION_PACKAGES = "packages"
OPTION_OTHER_PACKAGES = "other_packages"
OPTION_TOP_LEVEL = "top_level"
OPTION_REGISTERED_MODULES = "registered_modules"
KNOWN_OPTIONS = frozenset(
    {
        OPTION_ROOTS,
        OPTION_PACKAGES,
        OPTION_OTHER_PACKAGES,
        OPTION_TOP_LEVEL,
        OPTION_REGISTERED_MODULES,
    }
)

# 稳定 rule id（findings 的可机器读面；warn/hard 两档报告与测试都依赖它们）。
RULE_UNACCOUNTED_MODULE = "unaccounted_module"
RULE_UNDECLARED_PACKAGE = "undeclared_package"
RULE_NAMESPACE_DIR = "namespace_dir"
RULE_UNDECLARED_TOP_LEVEL = "undeclared_top_level"
RULE_STALE_REGISTRY = "stale_registry"
RULE_STALE_REGISTERED_MODULE = "stale_registered_module"
RULE_MISSING_ROOT = "missing_scan_root"


class SubdomainGuardConfigError(ValueError):
    """``subdomain`` guard 选项非法（降级留痕用；不是引擎错误码）。"""


@dataclass(frozen=True)
class SubdomainPolicy:
    """宿主可配置的子域扫描面（``roots`` 为空 = 未配置 → not_applicable）。"""

    roots: tuple[str, ...] = ()
    packages: Mapping[str, SubdomainDecision] = field(default_factory=dict)
    other_packages: Mapping[str, str] = field(default_factory=dict)
    top_level: Mapping[str, str] = field(default_factory=dict)
    registered_modules: tuple[str, ...] = ()


def _normalize_rel_path(text: object, field_name: str) -> str:
    """项目内相对路径规范化（拒绝绝对路径 / 盘符 / ``..`` / 空段）。"""
    if not isinstance(text, str):
        raise SubdomainGuardConfigError(f"{field_name} 必须是字符串路径")
    raw = text.strip().replace("\\", "/")
    if not raw:
        raise SubdomainGuardConfigError(f"{field_name} 不能为空")
    path = Path(raw)
    if path.is_absolute() or path.drive or raw.startswith("/"):
        raise SubdomainGuardConfigError(f"{field_name} 必须是项目内相对路径：{text!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise SubdomainGuardConfigError(f"{field_name} 含非法路径段：{text!r}")
    return "/".join(parts)


def _path_list(value: object, field_name: str) -> tuple[str, ...]:
    """字符串数组 → 规范化后的去重元组（重复项视为配置错误，避免静默折叠）。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SubdomainGuardConfigError(f"{field_name} 必须是字符串数组")
    items: list[str] = []
    for index, item in enumerate(value):
        rel = _normalize_rel_path(item, f"{field_name}[{index}]")
        if rel in items:
            raise SubdomainGuardConfigError(f"{field_name} 存在重复项：{rel}")
        items.append(rel)
    return tuple(items)


def _reason(text: object, field_name: str) -> str:
    """豁免理由必须非空（留空通过 = 事实上未决策）。"""
    if not isinstance(text, str) or not text.strip():
        raise SubdomainGuardConfigError(f"{field_name} 的理由不能为空")
    return text.strip()


def _reason_map(value: object, field_name: str) -> dict[str, str]:
    """``{路径: 理由}`` 对象 → 规范化键的理由表。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SubdomainGuardConfigError(f"{field_name} 必须是对象（路径 → 理由）")
    result: dict[str, str] = {}
    for key, reason in value.items():
        rel = _normalize_rel_path(key, f"{field_name} 的键")
        if rel in result:
            raise SubdomainGuardConfigError(f"{field_name} 存在重复项：{rel}")
        result[rel] = _reason(reason, f"{field_name}[{rel}]")
    return result


def _package_decisions(value: object) -> dict[str, SubdomainDecision]:
    """``{包路径: {modules: [...], reason: ...}}`` → 引擎同构的决策表。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SubdomainGuardConfigError(f"{OPTION_PACKAGES} 必须是对象（包路径 → 决策）")
    result: dict[str, SubdomainDecision] = {}
    for key, decision in value.items():
        pkg = _normalize_rel_path(key, f"{OPTION_PACKAGES} 的键")
        if not isinstance(decision, Mapping):
            raise SubdomainGuardConfigError(f"{OPTION_PACKAGES}[{pkg}] 必须是对象")
        unknown = sorted(set(decision) - {"modules", "reason"})
        if unknown:
            raise SubdomainGuardConfigError(
                f"{OPTION_PACKAGES}[{pkg}] 存在未知键：{', '.join(unknown)}"
            )
        modules = _path_list(decision.get("modules"), f"{OPTION_PACKAGES}[{pkg}].modules")
        reason = _reason(decision.get("reason"), f"{OPTION_PACKAGES}[{pkg}]")
        for module in modules:
            if not module.startswith(pkg + "/"):
                raise SubdomainGuardConfigError(
                    f"{OPTION_PACKAGES}[{pkg}].modules 越出所属包：{module}"
                )
        result[pkg] = SubdomainDecision(modules=modules, reason=reason)
    return result


def _check_within_roots(roots: tuple[str, ...], declared: list[tuple[str, str]]) -> None:
    """所有声明对象必须落在某个扫描根内（否则永远不会被扫到 = 静默失效）。"""
    for path, field_name in declared:
        if not any(path == root or path.startswith(root + "/") for root in roots):
            raise SubdomainGuardConfigError(
                f"{field_name} 不在 roots 扫描面内：{path!r}（roots={list(roots)}）"
            )


def parse_policy(options: object) -> SubdomainPolicy:
    """把 guards.json 的 subdomain options 解析为 :class:`SubdomainPolicy`（严格）。"""
    if options is None:
        return SubdomainPolicy()
    if not isinstance(options, Mapping):
        raise SubdomainGuardConfigError("subdomain 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise SubdomainGuardConfigError(f"subdomain 存在未知配置键：{', '.join(unknown)}")
    roots = _path_list(options.get(OPTION_ROOTS), OPTION_ROOTS)
    packages = _package_decisions(options.get(OPTION_PACKAGES))
    other_packages = _reason_map(options.get(OPTION_OTHER_PACKAGES), OPTION_OTHER_PACKAGES)
    top_level = _reason_map(options.get(OPTION_TOP_LEVEL), OPTION_TOP_LEVEL)
    registered = _path_list(
        options.get(OPTION_REGISTERED_MODULES), OPTION_REGISTERED_MODULES
    )
    if not roots:
        if packages or other_packages or top_level or registered:
            raise SubdomainGuardConfigError(
                "subdomain 声明了扫描对象却缺 roots（无从扫描，登记面会静默失效）"
            )
        return SubdomainPolicy()
    declared: list[tuple[str, str]] = []
    for pkg, decision in packages.items():
        declared.append((pkg, OPTION_PACKAGES))
        declared.extend(
            (module, f"{OPTION_PACKAGES}[{pkg}].modules") for module in decision.modules
        )
    declared.extend((pkg, OPTION_OTHER_PACKAGES) for pkg in other_packages)
    declared.extend((rel, OPTION_TOP_LEVEL) for rel in top_level)
    declared.extend((rel, OPTION_REGISTERED_MODULES) for rel in registered)
    _check_within_roots(roots, declared)
    return SubdomainPolicy(
        roots=roots,
        packages=packages,
        other_packages=other_packages,
        top_level=top_level,
        registered_modules=registered,
    )


def _configured_policy(project_root: Path) -> SubdomainPolicy:
    config = load_guards_config(project_root)
    entry = next((entry for entry in config.entries if entry.name == "subdomain"), None)
    return parse_policy(entry.options if entry is not None else None)


def _under_root(rel: str, root: str) -> bool:
    return rel.startswith(root + "/")


def _root_scope(project_root: Path, policy: SubdomainPolicy, root: str) -> CensusScope:
    """把宿主策略裁剪到单根扫描面（多根互不串台；引擎判据按 repo 相对路径工作）。"""
    return CensusScope(
        base=project_root / root,
        repo_root=project_root,
        prefix=f"{root}/",
        declared={k: v for k, v in policy.packages.items() if _under_root(k, root)},
        other={k: v for k, v in policy.other_packages.items() if _under_root(k, root)},
        top_level={k: v for k, v in policy.top_level.items() if _under_root(k, root)},
        shared={m: () for m in policy.registered_modules if _under_root(m, root)},
    )


def _finding(rule: str, message: str, root: str, entry: str, **extra: object) -> GuardFinding:
    return GuardFinding(
        guard="subdomain",
        message=message,
        context={"rule": rule, "root": root, "entry": entry, **extra},
    )


def _stale_kind(rel: str, policy: SubdomainPolicy) -> str:
    """死条目归属（便于报告直接指出是哪张表登记错了）。"""
    if rel in policy.top_level:
        return "top_level"
    if rel in policy.other_packages:
        return "other_packages"
    if rel in policy.packages:
        return "packages"
    return "package_modules"


def _findings_for_root(
    project_root: Path, policy: SubdomainPolicy, root: str
) -> list[GuardFinding]:
    """复用引擎五条判据（+ 宿主侧 registered_modules 死条目），映射成 findings。"""
    scope = _root_scope(project_root, policy, root)
    found: list[GuardFinding] = []
    for rel in subdomain_unaccounted(scope):
        found.append(
            _finding(RULE_UNACCOUNTED_MODULE, f"作用域包内未决策模块：{rel}", root, rel)
        )
    for rel in undeclared_packages(scope):
        found.append(_finding(RULE_UNDECLARED_PACKAGE, f"未决策的包：{rel}", root, rel))
    for rel in undeclared_namespace_dirs(scope):
        found.append(
            _finding(
                RULE_NAMESPACE_DIR,
                f"未决策的命名空间目录（含 .py 却无 __init__.py）：{rel}",
                root,
                rel,
            )
        )
    for rel in undeclared_top_level_modules(scope):
        found.append(
            _finding(RULE_UNDECLARED_TOP_LEVEL, f"未决策的顶层模块：{rel}", root, rel)
        )
    for rel in stale_registry_entries(scope):
        kind = _stale_kind(rel, policy)
        found.append(
            _finding(
                RULE_STALE_REGISTRY,
                f"登记面指向不存在的对象：{rel}（{kind}）",
                root,
                rel,
                kind=kind,
            )
        )
    for rel in scope.shared:
        if not (scope.repo_root / rel).is_file():
            found.append(
                _finding(
                    RULE_STALE_REGISTERED_MODULE,
                    f"registered_modules 指向不存在的文件：{rel}",
                    root,
                    rel,
                )
            )
    return found


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """按宿主配置扫描子域完备性；未配置返回空，坏配置 / 扫描故障降级留痕。"""
    if context.project_root is None:
        return ()
    try:
        policy = _configured_policy(context.project_root)
    except SubdomainGuardConfigError as exc:
        raise RuntimeError(f"subdomain guard 配置非法：{exc}") from exc
    if not policy.roots:
        return ()
    found: list[GuardFinding] = []
    try:
        for root in policy.roots:
            if not (context.project_root / root).is_dir():
                found.append(
                    _finding(
                        RULE_MISSING_ROOT,
                        f"扫描根不存在或不是目录：{root}",
                        root,
                        root,
                    )
                )
                continue
            found.extend(_findings_for_root(context.project_root, policy, root))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"subdomain guard 降级（scan_failed）：{exc}") from exc
    return tuple(found)


def register() -> None:
    """把本评估器登记到 guards 注册表（幂等）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator("subdomain", evaluate)


register()
