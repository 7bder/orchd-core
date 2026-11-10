"""orchd/guards/drift.py — 宿主「真源 → 镜像」与「共享入口 → 登记测试」done 期门禁。

把内建漂移闭环（``orchd/drift_closure.py``：改了真源却没同批带上镜像）与 E039
（``orchd/shared_entries.py``：改了共享入口但 ``verify_command`` 没覆盖登记测试）泛化为
**宿主可配置**的 done 期门禁。宿主在 ``.orchd/shared/guards.json`` 里声明 ``guards.drift``：

.. code-block:: json

    {
      "schema_version": 1,
      "guards": {
        "drift": {
          "mode": "warn",
          "entries": [
            {"source": "orchd/cli/parser.py", "mirrors": ["docs/_generated/commands.md"]}
          ],
          "shared_entries": {
            "orchd/cli/__init__.py": ["tests/test_cli_workflow.py", "tests/test_cli_contract.py"]
          }
        }
      }
    }

- ``entries``：``{source, mirrors}`` 列表；source 在改动集内而 mirrors 未同批出现即命中。判据与
  内建 drift closure **同源**（复用 :func:`orchd.drift_closure.host_mapping_gaps`，其内部收敛口径
  即 :func:`orchd.drift_closure.hard_gaps`），宿主侧不另造判定实现。
- ``shared_entries``：入口键 → 登记测试文件列表；入口改动而 done 期 ``verify_command`` 未覆盖
  其中任一测试即命中。判据与 E039 **同源**（复用
  :func:`orchd.shared_entries.shared_entry_coverage_gaps`）。键支持尾 ``/`` 包级前缀通配
  （与 E039 的 ``shared_entry_key_matches`` 同一语义）。

**分派由既有 wiring 承担**（``entry_guard._record_host_guards``）：``off`` 不执行；``warn`` 只写
``degraded_guards``（E030，不阻断 done）；``hard`` 复用 E039 且 ``details.rule == "drift"``
（guard 名，遵守 runtime-spec §2.1.1「details.rule 是具体 guard 名」）。具体判据标识放在
``findings[].context["rule"]``：``"drift_closure"``（镜像缺失）/ ``"shared_entry_verify"``
（verify 未覆盖登记测试）。

**降级纪律**：配置非法、映射源为空（显式配置了 ``drift`` 却没声明 ``entries`` /
``shared_entries``）⇒ 抛 :class:`RuntimeError`，由 wiring 记
``degraded_guards(status=failed)`` 留痕，**不阻断 done**；无 ``guards.json`` 时 wiring 直接
零行为（本模块不会被调用）。

**边界（如实声明）**：① 只判改动集路径，不做内容语义过滤（done 链路已把纯注释/空白改动过滤后
再传入 ``context.changed``）；② ``hard`` 档对「多镜像只带了一部分」（``mirror_partial``）同样
命中 —— 宿主契约：登记的镜像对须同批出现，内容驱动的可选镜像请拆成独立 ``entries`` 或改用
``warn`` 档；findings 的 ``context.blocking`` 标明该档位是否阻断、``context.mode`` 带出档位，
``hard`` 档下 ``mirror_partial`` 与 ``generated`` 同属阻断；③ 不读被判定路径的文件内容、不遍历
目录树、不起子进程（只读 ``guards.json``）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.drift_closure import host_mapping_gaps
from orchd.guards import (
    GuardEvalContext,
    GuardFinding,
    GuardsConfigError,
    load_guards_config,
    register_guard_evaluator,
)
from orchd.shared_entries import shared_entry_coverage_gaps

#: 配置层注册表里的 guard 名（``GUARD_NAMES`` 之一；E039 ``details.rule`` 即此值）。
GUARD_NAME = "drift"

#: 镜像缺失判据的 rule 标识（与内建 drift closure 的 ``details.rule`` 同字面量）。
RULE_MIRROR = "drift_closure"
#: 共享入口 verify 覆盖判据的 rule 标识。
RULE_SHARED_ENTRY = "shared_entry_verify"

ENTRIES_OPTION = "entries"
SHARED_ENTRIES_OPTION = "shared_entries"
#: 本 guard 认识的 options 键（未知键忽略：config 层原样透传，便于前向兼容）。
KNOWN_OPTIONS = frozenset({ENTRIES_OPTION, SHARED_ENTRIES_OPTION})

_SOURCE_KEY = "source"
_MIRRORS_KEY = "mirrors"
_ENTRY_KEYS = frozenset({_SOURCE_KEY, _MIRRORS_KEY})
_TESTS_PREFIX = "tests/"


class DriftGuardConfigError(ValueError):
    """``guards.drift`` 的 options 非法（:func:`evaluate` 会转 ``RuntimeError`` 降级留痕）。"""


@dataclass(frozen=True)
class DriftMapping:
    """一条宿主「真源 → 镜像」映射。"""

    source: str
    mirrors: tuple[str, ...]


@dataclass(frozen=True)
class DriftGuardPolicy:
    """``guards.drift`` 的解析结果；``empty`` = 显式配置了 drift 却没声明映射源。"""

    mappings: tuple[DriftMapping, ...] = ()
    shared_entries: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: 宿主配置档位（``off`` / ``warn`` / ``hard``）；``hard`` 档下所有 findings 均阻断 done。
    mode: str = "warn"

    @property
    def empty(self) -> bool:
        """两侧映射均为空（配置缺陷：由 :func:`evaluate` 降级留痕，不静默放行）。"""
        return not self.mappings and not self.shared_entries

    @property
    def shared_registry(self) -> dict[str, tuple[str, ...]]:
        """共享入口登记表（供 :func:`shared_entry_coverage_gaps` 注入）。"""
        return dict(self.shared_entries)


def _normalize_repo_path(raw: Any, *, label: str) -> str:
    """校验并归一化一条仓库相对路径（``/`` 分隔、禁绝对/盘符/``..``/裸文件名）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise DriftGuardConfigError(f"{label} 必须是非空字符串：{raw!r}")
    value = raw.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    head = value.split("/", 1)[0]
    if value.startswith("/") or value.startswith("~") or ":" in head:
        raise DriftGuardConfigError(f"{label} 必须是仓库相对路径（禁绝对路径/盘符）：{raw!r}")
    if any(part in ("", "..") for part in value.split("/")):
        raise DriftGuardConfigError(f"{label} 含空段或 '..' 段：{raw!r}")
    if "/" not in value:
        raise DriftGuardConfigError(
            f"{label} 必须含目录分隔符 '/'（防裸文件名跨目录误配）：{raw!r}"
        )
    return value


def _parse_entries(raw: Any) -> tuple[DriftMapping, ...]:
    """``entries`` option → :class:`DriftMapping` 元组（逐条结构化校验）。"""
    if not isinstance(raw, list):
        raise DriftGuardConfigError(f"{ENTRIES_OPTION} 必须是列表：{raw!r}")
    mappings: list[DriftMapping] = []
    for index, item in enumerate(raw):
        label = f"{ENTRIES_OPTION}[{index}]"
        if not isinstance(item, Mapping):
            raise DriftGuardConfigError(f"{label} 必须是对象：{item!r}")
        unknown = sorted(set(item) - _ENTRY_KEYS)
        if unknown:
            raise DriftGuardConfigError(f"{label} 含未知键 {unknown}（只支持 source / mirrors）")
        source = _normalize_repo_path(item.get(_SOURCE_KEY), label=f"{label}.{_SOURCE_KEY}")
        raw_mirrors = item.get(_MIRRORS_KEY)
        if not isinstance(raw_mirrors, list) or not raw_mirrors:
            raise DriftGuardConfigError(f"{label}.{_MIRRORS_KEY} 必须是非空列表：{raw_mirrors!r}")
        mirrors = tuple(
            _normalize_repo_path(mirror, label=f"{label}.{_MIRRORS_KEY}[{pos}]")
            for pos, mirror in enumerate(raw_mirrors)
        )
        if any(mapping.source == source and mapping.mirrors == mirrors for mapping in mappings):
            raise DriftGuardConfigError(
                f"{label} 与前面的条目完全重复（source={source!r}, mirrors={list(mirrors)!r}）"
            )
        mappings.append(DriftMapping(source, mirrors))
    return tuple(mappings)


def _normalize_entry_key(raw: Any, *, label: str) -> str:
    """校验并归一化一个共享入口键（仓库相对路径；尾 ``/`` 为包级前缀通配）。

    与 :func:`_normalize_repo_path` 同级：禁绝对路径/盘符/``~``、禁 ``..``/空段/裸文件名，
    反斜杠统一归一为 ``/``；尾 ``/`` 保留（与 E039 的 ``shared_entry_key_matches`` 同语义）。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise DriftGuardConfigError(f"{label} 必须是非空字符串：{raw!r}")
    value = raw.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    head = value.split("/", 1)[0]
    if value.startswith("/") or value.startswith("~") or ":" in head:
        raise DriftGuardConfigError(f"{label} 必须是仓库相对路径（禁绝对路径/盘符）：{raw!r}")
    body = value[:-1] if value.endswith("/") else value
    if "/" not in body:
        raise DriftGuardConfigError(
            f"{label} 必须含目录分隔符 '/'（防裸文件名跨目录误配）：{raw!r}"
        )
    if any(part in ("", ".", "..") for part in body.split("/")):
        raise DriftGuardConfigError(f"{label} 含空段/'.'/'..' 段：{raw!r}")
    return value


def _normalize_test_path(raw: Any, *, label: str) -> str:
    """校验并归一化一条登记测试路径（``tests/`` 前缀、``.py`` 结尾、无 ``..``/空段）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise DriftGuardConfigError(f"{label} 必须是非空字符串：{raw!r}")
    value = raw.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    head = value.split("/", 1)[0]
    if value.startswith("/") or value.startswith("~") or ":" in head:
        raise DriftGuardConfigError(f"{label} 必须是仓库相对路径（禁绝对路径/盘符）：{raw!r}")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise DriftGuardConfigError(f"{label} 含空段/'.'/'..' 段：{raw!r}")
    if not value.startswith(_TESTS_PREFIX) or not value.endswith(".py"):
        raise DriftGuardConfigError(f"{label} 必须是 tests/ 下的 .py 文件：{raw!r}")
    return value


def _parse_shared_entries(raw: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``shared_entries`` option → ``(入口键, 登记测试元组)`` 有序元组。"""
    if not isinstance(raw, Mapping):
        raise DriftGuardConfigError(f"{SHARED_ENTRIES_OPTION} 必须是对象：{raw!r}")
    parsed: list[tuple[str, tuple[str, ...]]] = []
    for key in sorted(raw, key=str):
        entry = _normalize_entry_key(key, label=f"{SHARED_ENTRIES_OPTION} 的键 {key!r}")
        tests = raw[key]
        if not isinstance(tests, list) or not tests:
            raise DriftGuardConfigError(
                f"{SHARED_ENTRIES_OPTION}[{entry!r}] 必须是非空测试文件列表：{tests!r}"
            )
        normalized: list[str] = []
        for pos, test in enumerate(tests):
            value = _normalize_test_path(test, label=f"{SHARED_ENTRIES_OPTION}[{entry!r}][{pos}]")
            if value not in normalized:
                normalized.append(value)
        parsed.append((entry, tuple(normalized)))
    return tuple(parsed)


def parse_policy(options: Mapping[str, Any] | None) -> DriftGuardPolicy:
    """解析 ``guards.drift`` 的 options（未知顶层键忽略，前向兼容）。

    Raises:
        DriftGuardConfigError: options 结构/路径/测试登记非法。
    """
    if options is None:
        return DriftGuardPolicy()
    if not isinstance(options, Mapping):
        raise DriftGuardConfigError(f"{GUARD_NAME} 的 options 必须是对象：{options!r}")
    return DriftGuardPolicy(
        mappings=_parse_entries(options.get(ENTRIES_OPTION, [])),
        shared_entries=_parse_shared_entries(options.get(SHARED_ENTRIES_OPTION, {})),
    )


def _configured_policy(project_root: Path) -> DriftGuardPolicy:
    """读宿主配置并解析本 guard 的 options（配置层非法同样归入本 guard 降级）。"""
    try:
        config = load_guards_config(project_root)
    except GuardsConfigError as exc:
        raise DriftGuardConfigError(f"guards 配置非法：{exc}") from exc
    entry = next((item for item in config.entries if item.name == GUARD_NAME), None)
    policy = parse_policy(entry.options if entry is not None else None)
    return DriftGuardPolicy(
        mappings=policy.mappings,
        shared_entries=policy.shared_entries,
        mode=str(getattr(entry, "mode", None) or "warn"),
    )


def _normalize_changed(changed: Sequence[str]) -> tuple[str, ...]:
    """改动集归一化：``\\`` → ``/``、去 ``./`` 前缀、去空、去重保序。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in changed:
        value = str(raw).strip().replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        if not value or value in seen:
            continue
        ordered.append(value)
        seen.add(value)
    return tuple(ordered)


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """执行一次 drift 判定（只读；降级路径抛 :class:`RuntimeError` 由 wiring 留痕）。

    Returns:
        命中项元组；空元组 = 本次无缺口。

    Raises:
        RuntimeError: 配置非法 / 映射源为空（wiring 记 ``degraded_guards(failed)``，不阻断 done）。
    """
    project_root = context.project_root
    if project_root is None:
        return ()
    try:
        policy = _configured_policy(project_root)
    except DriftGuardConfigError as exc:
        raise RuntimeError(f"drift guard 配置非法：{exc}") from exc
    if policy.empty:
        raise RuntimeError(
            "drift guard 映射源为空（entries 与 shared_entries 均未配置）：本次降级留痕，"
            "不判定（显式配置 drift 时必须至少声明一侧映射）"
        )
    changed = _normalize_changed(context.changed)
    if not changed:
        return ()
    blocking = policy.mode == "hard"
    findings: list[GuardFinding] = []
    seen_mirror: set[tuple[str, tuple[str, ...]]] = set()
    # 逐条 entry 判定：同 source 的多组镜像各自成组，避免「部分命中」把硬缺口降成 mirror_partial。
    for mapping in policy.mappings:
        for gap in host_mapping_gaps([(mapping.source, mapping.mirrors)], changed):
            dedupe_key = (gap.source, gap.mirrors)
            if dedupe_key in seen_mirror:
                continue
            seen_mirror.add(dedupe_key)
            findings.append(
                GuardFinding(
                    guard=GUARD_NAME,
                    message=(
                        f"真源 {gap.source} 已改动，但未同批带上镜像："
                        f"{'、'.join(gap.mirrors)}（宿主 drift 配置）"
                    ),
                    context={
                        "rule": RULE_MIRROR,
                        **gap.as_dict(),
                        "blocking": blocking,
                        "mode": policy.mode,
                    },
                )
            )
    for coverage in shared_entry_coverage_gaps(policy.shared_registry, changed, context.verify_cmd):
        findings.append(
            GuardFinding(
                guard=GUARD_NAME,
                message=(
                    f"共享入口 {coverage['shared_entry']} 已改动，但 verify_command 未覆盖"
                    f"登记测试 {coverage['required_test']}（宿主 drift 配置）"
                ),
                context={
                    "rule": RULE_SHARED_ENTRY,
                    **coverage,
                    "blocking": blocking,
                    "mode": policy.mode,
                },
            )
        )
    return tuple(findings)


def register() -> None:
    """把本模块评估器登记进 guards 注册表（import 期即调用；缓存命中时由 registry 补调）。"""
    register_guard_evaluator(GUARD_NAME, evaluate)


register()
