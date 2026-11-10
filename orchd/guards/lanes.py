"""宿主车道台账 guard（task-host-guard-lanes）。

把 ``scripts/update_stable_lanes.py`` 里的台账判据抽成本模块的**纯函数**，脚本与
guard **共用同一份判据**（脚本只保留 CLI 外壳与文件读取，不再自带解析/校验实现）：

- :func:`parse_stable_entries` / :func:`parse_fast_entries` / :func:`fast_section_text`：
  解析 ``tests/STABLE_LANES.md`` 的 ``## Stable`` 与 ``## Fast`` 两节；
- :func:`job_block` / :func:`check_ci_wiring_text`：CI 三车道接线与台账声明一致性；
- :func:`check_fast_lane_text`：Fast 节唯一性 / 成员存在 / 无重复 / 预算锚一致；
- :func:`check_ledger_text`：汇总「晋升/降级规则说明在场、Stable 条目文件存在且无重复、
  CI 接线一致、Fast 节合法」，并可叠加配置里声明的车道成员（见下）。

宿主配置（``.orchd/shared/guards.json``，options 由配置层原样透传）：``guards.lanes`` 的
``mode`` 与 ``ledger`` / ``ci`` / ``stable`` / ``fast`` 四个 options（见 ``KNOWN_OPTIONS``）。

- ``ledger`` / ``ci``：台账与 CI 工作流路径（项目内相对路径，缺省如上）；
- ``stable`` / ``fast``：**可选**的车道成员声明；声明后校验台账清单与声明逐条一致
  （删条目 / 私加条目都会逐条命中），省略则只做台账自身一致性校验。

判据语义与降级纪律（与 deps/discipline 家族一致）：

- 无台账文件 ⇒ 返回空集（``not_applicable``，宿主未采用车道台账，不误红）；
- 台账不可读 / 非 UTF-8 / 配置非法 ⇒ 抛 ``RuntimeError``（done 期降级留痕、不阻断）；
- 命中项返回 :class:`GuardFinding`，``context.rule`` 为稳定规则名（另带 ``entry``）；本模块只读。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from orchd.guards import GuardEvalContext, GuardFinding, load_guards_config

__all__ = (
    "DEFAULT_CI_REL",
    "DEFAULT_LEDGER_REL",
    "FAST_LANE_BUDGET_S",
    "FAST_LANE_CASES_CAP",
    "FAST_LANE_SPAWN_CAP",
    "KNOWN_OPTIONS",
    "LaneProblem",
    "LanesGuardConfigError",
    "LanesLedgerError",
    "LanesPolicy",
    "check_ci_wiring_text",
    "check_fast_lane_text",
    "check_ledger_text",
    "evaluate",
    "fast_section_text",
    "job_block",
    "parse_fast_entries",
    "parse_policy",
    "parse_stable_entries",
    "read_ledger",
    "register",
)

DEFAULT_LEDGER_REL = "tests/STABLE_LANES.md"
DEFAULT_CI_REL = ".github/workflows/ci.yml"
KNOWN_OPTIONS = frozenset({"ledger", "ci", "stable", "fast"})

# 快车道成本锚（B2，task-guard-drift-b2-fastlane-cost）：双计数替代绝对墙钟门禁。
# 单一真源移入本模块后由 ``scripts/update_stable_lanes.py`` 再导出（``FAST_LANE_*``），
# 脚本、guard、测试三处引用同一常量。实测（2026-10-31，spawn_census 口径 -o addopts=）：
# spawn 217 / 用例 359 / 墙钟 ~50s。
FAST_LANE_SPAWN_CAP = 240
FAST_LANE_CASES_CAP = 400
FAST_LANE_BUDGET_S = 45

# CI 接线断言（task-stable-lanes-semantics）：台账头声明必须与 ci.yml 实际参数一致——
# pass5 R-2(c) 的根因正是「文档说 A、CI 做 B」且无人校验。
_SLOW_JOB = "test-slow:"
_GATE_JOB = "full-regression-gate:"
_SLOW_JOB_NEEDLE = "update_stable_lanes.py --pytest-args"
_GATE_JOB_NEEDLE = "python -m pytest tests/"
_GATE_JOB_FORBIDDEN = ('-m "not slow"', "--pytest-args", "continue-on-error: true")

_ENTRY_RE = re.compile(r"^- (tests/test_.*\.py)\s*$")
_JOB_LINE_RE = re.compile(r"^  [A-Za-z0-9_-]+:\s*$")
_FAST_SECTION_RE = re.compile(r"^## Fast\b")
# Fast 成员允许目录（如 tests/contract）与文件，与 Stable 的单文件口径不同。
_FAST_ENTRY_RE = re.compile(r"^- (tests/[A-Za-z0-9_./-]+)\s*$")
_BUDGET_RE = re.compile(r"预算\s*\**\s*(\d+)\s*s")
_STABLE_DECLARED_RE = re.compile(r"^tests/test_.*\.py$")


class LanesGuardConfigError(ValueError):
    """lanes guard 配置非法（未知键 / 坏路径 / 成员声明非法）。"""


class LanesLedgerError(ValueError):
    """台账文件不可读或编码非法（脚本与 guard 共用同一错误面）。"""


@dataclass(frozen=True)
class LanesPolicy:
    """宿主可配置的车道台账扫描面。"""

    ledger_rel: str = DEFAULT_LEDGER_REL
    ci_rel: str = DEFAULT_CI_REL
    stable_members: tuple[str, ...] = ()
    fast_members: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaneProblem:
    """一条台账问题（``rule`` 稳定可机读，脚本只打印 ``message``）。"""

    rule: str
    message: str
    context: Mapping[str, str] = field(default_factory=dict)


def _normalize_rel_path(text: object, field_name: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise LanesGuardConfigError(f"{field_name} 必须是非空字符串")
    raw = text.strip().replace("\\", "/")
    if raw.startswith("/") or ":" in raw:
        raise LanesGuardConfigError(f"{field_name} 必须是项目内相对路径：{text!r}")
    rel = raw.strip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        raise LanesGuardConfigError(f"{field_name} 不能包含空段/./..：{text!r}")
    return rel


def _normalize_member(value: object, field_name: str, *, stable: bool) -> str:
    rel = _normalize_rel_path(value, field_name)
    if not rel.startswith("tests/"):
        raise LanesGuardConfigError(f"{field_name} 成员必须是 tests/ 下的路径：{value!r}")
    if stable and not _STABLE_DECLARED_RE.match(rel):
        raise LanesGuardConfigError(
            f"{field_name} 成员必须是 tests/test_*.py（Stable 单文件口径）：{value!r}"
        )
    return rel


def _member_list(options: Mapping[str, object], key: str, *, stable: bool) -> tuple[str, ...]:
    raw = options.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LanesGuardConfigError(f"{key} 必须是字符串数组")
    members = [_normalize_member(item, key, stable=stable) for item in raw]
    if len(set(members)) != len(members):
        raise LanesGuardConfigError(f"{key} 含重复成员")
    return tuple(members)


def parse_policy(options: object) -> LanesPolicy:
    """把 guards.json 的 lanes options 解析为 :class:`LanesPolicy`（严格、fail-closed）。"""
    if options is None:
        return LanesPolicy()
    if not isinstance(options, Mapping):
        raise LanesGuardConfigError("lanes 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise LanesGuardConfigError(f"lanes 存在未知配置键：{', '.join(unknown)}")
    ledger_rel = _normalize_rel_path(options.get("ledger", DEFAULT_LEDGER_REL), "ledger")
    ci_rel = _normalize_rel_path(options.get("ci", DEFAULT_CI_REL), "ci")
    return LanesPolicy(
        ledger_rel=ledger_rel,
        ci_rel=ci_rel,
        stable_members=_member_list(options, "stable", stable=True),
        fast_members=_member_list(options, "fast", stable=False),
    )


def read_ledger(ledger: Path) -> str:
    """读台账文本；编码/IO 故障统一转 :class:`LanesLedgerError`（可读错误，不裸崩）。"""
    try:
        return ledger.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LanesLedgerError(
            f"台账不是合法 UTF-8（{exc.reason}，字节位置 {exc.start}）：{ledger.name}"
        ) from exc
    except OSError as exc:
        raise LanesLedgerError(f"台账不可读：{type(exc).__name__}: {exc}") from exc


def parse_stable_entries(text: str) -> tuple[str, ...]:
    """读取 ``## Stable`` 节的条目（保持台账顺序）。"""
    in_stable = False
    entries: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            in_stable = line.strip() == "## Stable"
            continue
        if in_stable:
            match = _ENTRY_RE.match(line.strip())
            if match:
                entries.append(match.group(1))
    return tuple(entries)


def parse_fast_entries(text: str) -> tuple[str, ...]:
    """从台账全文取 ``## Fast`` 节条目（文件或目录，保持台账顺序）。"""
    in_fast = False
    entries: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            in_fast = bool(_FAST_SECTION_RE.match(line.strip()))
            continue
        if in_fast:
            match = _FAST_ENTRY_RE.match(line.strip())
            if match:
                entries.append(match.group(1))
    return tuple(entries)


def fast_section_text(text: str) -> str:
    """取 Fast 节正文（节头之后到下一个二级节为止），供预算锚解析。"""
    section: list[str] = []
    in_fast = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_fast = bool(_FAST_SECTION_RE.match(line.strip()))
            continue
        if in_fast:
            section.append(line)
    return "\n".join(section)


def job_block(ci_text: str, job: str) -> str:
    """截取 ci.yml 中某 job 的文本块（从 job 行到下一个同级 job 行）。"""
    lines = ci_text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == job), None)
    if start is None:
        return ""
    block = [lines[start]]
    for line in lines[start + 1:]:
        if _JOB_LINE_RE.match(line):
            break
        block.append(line)
    return "\n".join(block)


def check_ci_wiring_text(ci_text: str) -> tuple[LaneProblem, ...]:
    """校验 ci.yml 三车道接线与台账声明一致（纯文本判据，脚本/guard 共用）。"""
    problems: list[LaneProblem] = []
    slow = job_block(ci_text, _SLOW_JOB)
    if not slow:
        problems.append(
            LaneProblem("ci_slow_job_missing", f"ci.yml 缺慢车道 job：{_SLOW_JOB}")
        )
    elif _SLOW_JOB_NEEDLE not in slow:
        problems.append(
            LaneProblem(
                "ci_slow_not_consuming",
                f"慢车道 job 未消费台账清单（应含 {_SLOW_JOB_NEEDLE}）——"
                "台账声明与实际分流不一致",
            )
        )
    gate = job_block(ci_text, _GATE_JOB)
    if not gate:
        problems.append(
            LaneProblem("ci_gate_job_missing", f"ci.yml 缺发版门禁 job：{_GATE_JOB}")
        )
    elif _GATE_JOB_NEEDLE not in gate:
        problems.append(
            LaneProblem(
                "ci_gate_not_full",
                f"门禁 job 未跑真全量（应含 {_GATE_JOB_NEEDLE}）",
            )
        )
    else:
        for banned in _GATE_JOB_FORBIDDEN:
            if banned in gate:
                problems.append(
                    LaneProblem(
                        "ci_gate_filtered", f"门禁 job 不得含过滤/放行参数：{banned}"
                    )
                )
    return tuple(problems)


def check_fast_lane_text(
    text: str,
    *,
    project_root: Path,
    budget_s: int = FAST_LANE_BUDGET_S,
) -> tuple[LaneProblem, ...]:
    """校验 ``## Fast`` 节：唯一 / 非空 / 成员存在 / 无重复 / 节头预算与常量一致。"""
    entries = parse_fast_entries(text)
    problems: list[LaneProblem] = []
    headers = [
        line.strip()
        for line in text.splitlines()
        if line.startswith("## ") and _FAST_SECTION_RE.match(line.strip())
    ]
    if len(headers) != 1:
        problems.append(
            LaneProblem(
                "fast_section_duplicate",
                f"Fast 节必须唯一，实际命中 {len(headers)} 个同名节"
                "（语义只能有一份真源）",
            )
        )
    if not entries:
        problems.append(
            LaneProblem(
                "fast_empty",
                "Fast 节缺失或无成员条目（快车道成员须是机器可读清单）",
            )
        )
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            problems.append(
                LaneProblem(
                    "fast_duplicate", f"Fast 重复条目：{entry}", {"entry": entry}
                )
            )
            continue
        seen.add(entry)
        if not (project_root / entry).exists():
            problems.append(
                LaneProblem(
                    "fast_entry_missing",
                    f"Fast 条目对应文件不存在：{entry}",
                    {"entry": entry},
                )
            )
    budget_match = _BUDGET_RE.search(fast_section_text(text))
    if budget_match is None:
        problems.append(
            LaneProblem(
                "fast_budget_missing",
                f"Fast 节头缺预算锚（应写「预算 {budget_s}s」并与常量一致）",
            )
        )
    elif int(budget_match.group(1)) != budget_s:
        problems.append(
            LaneProblem(
                "fast_budget_mismatch",
                f"Fast 节头预算 {budget_match.group(1)}s 与常量 "
                f"FAST_LANE_BUDGET_S={budget_s}s 不一致",
            )
        )
    return tuple(problems)


def check_ledger_text(
    text: str,
    *,
    project_root: Path,
    ci_text: str | None = None,
    ci_label: str | None = None,
    policy: LanesPolicy | None = None,
    budget_s: int = FAST_LANE_BUDGET_S,
) -> tuple[LaneProblem, ...]:
    """汇总台账判据（规则说明 / Stable / CI 接线 / Fast / 声明的车道成员）。

    Args:
        text: 台账全文。
        project_root: 宿主仓根（条目存在性按它解析）。
        ci_text: ci.yml 全文；``None`` 表示不校验 CI 接线（宿主无 CI 时跳过）。
        ci_label: 传入且 ``ci_text is None`` 时按「CI 工作流缺失」报一条（脚本 CLI 用）。
        policy: 宿主策略；其 ``stable_members`` / ``fast_members`` 非空时叠加成员一致性
            校验（删条目 / 私加条目都会命中）。
        budget_s: Fast 节头预算锚期望值（脚本传入自身常量，guard 用默认常量）。
    """
    problems: list[LaneProblem] = []
    if "晋升" not in text or "降级" not in text:
        problems.append(
            LaneProblem("rules_missing", "台账头缺晋升/降级规则说明")
        )
    stable = parse_stable_entries(text)
    seen: set[str] = set()
    for entry in stable:
        if entry in seen:
            problems.append(
                LaneProblem(
                    "stable_duplicate", f"重复条目：{entry}", {"entry": entry}
                )
            )
            continue
        seen.add(entry)
        if not (project_root / entry).is_file():
            problems.append(
                LaneProblem(
                    "stable_entry_missing",
                    f"条目对应文件不存在：{entry}",
                    {"entry": entry},
                )
            )
    if policy is not None and policy.stable_members:
        for member in policy.stable_members:
            if member not in seen:
                problems.append(
                    LaneProblem(
                        "stable_declared_missing",
                        f"台账缺配置声明的 Stable 成员：{member}",
                        {"entry": member},
                    )
                )
        for entry in dict.fromkeys(stable):
            if entry not in policy.stable_members:
                problems.append(
                    LaneProblem(
                        "stable_undeclared",
                        f"台账 Stable 条目未在配置声明：{entry}",
                        {"entry": entry},
                    )
                )
    if ci_text is None:
        if ci_label is not None:
            problems.append(
                LaneProblem("ci_missing", f"CI 工作流缺失：{ci_label}")
            )
    else:
        problems.extend(check_ci_wiring_text(ci_text))
    problems.extend(
        check_fast_lane_text(text, project_root=project_root, budget_s=budget_s)
    )
    if policy is not None and policy.fast_members:
        fast = parse_fast_entries(text)
        for member in policy.fast_members:
            if member not in fast:
                problems.append(
                    LaneProblem(
                        "fast_declared_missing",
                        f"台账缺配置声明的 Fast 成员：{member}",
                        {"entry": member},
                    )
                )
        for entry in dict.fromkeys(fast):
            if entry not in policy.fast_members:
                problems.append(
                    LaneProblem(
                        "fast_undeclared",
                        f"台账 Fast 条目未在配置声明：{entry}",
                        {"entry": entry},
                    )
                )
    return tuple(problems)


def _configured_policy(project_root: Path) -> LanesPolicy:
    config = load_guards_config(project_root)
    entry = next((item for item in config.entries if item.name == "lanes"), None)
    return parse_policy(entry.options if entry is not None else None)


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """按宿主配置校验车道台账。

    无台账文件 ⇒ 返回空集（``not_applicable``，不误红）；台账不可读/非 UTF-8、CI 文件
    不可读或配置非法 ⇒ 抛 ``RuntimeError``（done 期降级留痕）；命中项逐条返回
    ``GuardFinding``，``context.rule`` 稳定可机读。
    """
    if context.project_root is None:
        return ()
    root = context.project_root
    try:
        policy = _configured_policy(root)
    except LanesGuardConfigError as exc:
        raise RuntimeError(f"lanes guard 配置非法：{exc}") from exc
    ledger_path = root / policy.ledger_rel
    if not ledger_path.is_file():
        return ()  # 无台账 ⇒ not_applicable（宿主未采用车道台账）
    try:
        text = read_ledger(ledger_path)
    except LanesLedgerError as exc:
        raise RuntimeError(f"lanes guard 降级（bad_ledger）：{exc}") from exc
    ci_text: str | None = None
    ci_path = root / policy.ci_rel
    if ci_path.is_file():
        try:
            ci_text = ci_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"lanes guard 降级（bad_ci）：CI 工作流不可读：{exc}"
            ) from exc
    problems = check_ledger_text(
        text,
        project_root=root,
        ci_text=ci_text,
        policy=policy,
    )
    return tuple(
        GuardFinding(
            guard="lanes",
            message=problem.message,
            context={"rule": problem.rule, **dict(problem.context)},
        )
        for problem in problems
    )


def register() -> None:
    """把 ``lanes`` 评估器注册进全局注册表（import 期与懒加载都会调用）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator("lanes", evaluate)


register()
