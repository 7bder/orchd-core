"""宿主 guard ``docs``：落地任务须在 CHANGELOG 在册（宿主可配置路径/任务号正则/锚）。

把引擎 merge 审计里的「CHANGELOG 落地缺行」判据
（:func:`orchd.report.audit.changelog_landing_scan`）泛化为宿主可配置：宿主在
``.orchd/shared/guards.json`` 为 ``docs`` 登记 ``changelog`` / ``task_pattern`` /
``anchor``，done 期（以及后续 ``guards check docs`` 只读入口）扫描「锚..HEAD 已落地
任务」与 CHANGELOG 全文，缺行即命中；是否阻断由 guard 档位（``warn`` / ``hard``）
决定，off 时评估器不会被调用。

配置形态（字段在本 guard 内校验，非法即降级留痕、不阻断）::

    {
      "schema_version": 1,
      "guards": {
        "docs": {
          "mode": "warn",
          "changelog": "CHANGELOG.md",
          "task_pattern": "task-[a-z0-9-]+",
          "anchor": "v1.2.3"
        }
      }
    }

- ``changelog``：仓库相对 POSIX 路径（默认 ``CHANGELOG.md``；禁止绝对路径与 ``..``）；
- ``task_pattern``：落地任务号正则（默认 ``task-[a-z0-9-]+``，对 sqid 做 fullmatch 过滤——
  未命中的落地 id 不参与缺行判定，便于宿主收敛各自的任务号口径）；
- ``anchor``：显式锚（tag/commit）；缺省沿用引擎 ``previous_release_tag``。

纪律：只读（不写盘）；git 读取一律走 ``orchd.gitops.query`` 真源；无 tag / git 不可读 /
CHANGELOG 不可读、锚之后无落地任务（或正则过滤后为空）一律**降级留痕、不误红**。判据本体与引擎
``orchd.report.audit._changelog_landing_gaps`` **同源同口径**（回归断言见
``tests/test_host_guard_docs.py::TestParityWithEngineAudit``）。

本模块 import 期即注册 ``docs`` 评估器（``evaluate_guard`` 会按需导入本模块）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.guards import (
    GuardEvalContext,
    GuardFinding,
    GuardsConfigError,
    load_guards_config,
    register_guard_evaluator,
)

#: ``docs`` guard 的配置选项键。
CHANGELOG_OPTION = "changelog"
TASK_PATTERN_OPTION = "task_pattern"
ANCHOR_OPTION = "anchor"
KNOWN_OPTIONS = frozenset({CHANGELOG_OPTION, TASK_PATTERN_OPTION, ANCHOR_OPTION})

#: 缺省 CHANGELOG 仓库相对路径（与引擎 merge 审计一致）。
DEFAULT_CHANGELOG_REL = "CHANGELOG.md"

#: 缺省落地任务号正则（与 ``orchd.gitops.query`` 的 id 形态一致）。
DEFAULT_TASK_PATTERN = r"task-[a-z0-9-]+"


class DocsGuardConfigError(ValueError):
    """``docs`` guard 的配置选项非法（消息可直接进入降级 / 错误上下文）。"""


@dataclass(frozen=True)
class DocsPolicy:
    """``docs`` guard 的规范化配置（所有字段均已校验）。"""

    changelog_rel: str = DEFAULT_CHANGELOG_REL
    task_pattern: str = DEFAULT_TASK_PATTERN
    anchor: str | None = None


def _normalize_changelog_rel(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocsGuardConfigError("changelog 必须是非空字符串（仓库相对路径）")
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or ":" in text:
        raise DocsGuardConfigError(f"changelog 必须是仓库相对路径：{value!r}")
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise DocsGuardConfigError(f"changelog 路径非法：{value!r}")
    return "/".join(parts)


def _normalize_task_pattern(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DocsGuardConfigError("task_pattern 必须是非空正则字符串")
    try:
        re.compile(value)
    except re.error as exc:
        raise DocsGuardConfigError(f"task_pattern 不是合法正则：{exc}") from exc
    return value


def _normalize_anchor(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DocsGuardConfigError("anchor 必须是字符串（显式 tag/commit）或省略")
    return value.strip()


def parse_policy(options: Mapping[str, Any] | None) -> DocsPolicy:
    """校验并规范化 ``docs`` guard 选项（未知键 fail-closed）。"""
    if options is None:
        return DocsPolicy()
    if not isinstance(options, Mapping):
        raise DocsGuardConfigError("docs guard 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise DocsGuardConfigError(
            f"docs guard 存在未知选项：{', '.join(unknown)}"
            f"（已知：{', '.join(sorted(KNOWN_OPTIONS))}）"
        )
    return DocsPolicy(
        changelog_rel=_normalize_changelog_rel(
            options.get(CHANGELOG_OPTION, DEFAULT_CHANGELOG_REL)
        ),
        task_pattern=_normalize_task_pattern(
            options.get(TASK_PATTERN_OPTION, DEFAULT_TASK_PATTERN)
        ),
        anchor=_normalize_anchor(options.get(ANCHOR_OPTION)),
    )


def _configured_policy(project_root: Path) -> DocsPolicy:
    """从宿主管 guard 配置里取 ``docs`` 选项（缺失时用缺省口径）。"""
    try:
        config = load_guards_config(project_root)
    except GuardsConfigError as exc:
        raise RuntimeError(f"docs guard 配置非法：{exc}") from exc
    for entry in config.entries:
        if entry.name == "docs":
            return parse_policy(entry.options)
    return DocsPolicy()


def _degradation_message(reason: str, detail: str) -> str:
    if reason == "no_anchor":
        return "无上一发版 tag 可定锚，落地集无从定义"
    if reason == "git_unreadable":
        return "git 历史不可读（非仓库 / ref 非法）"
    if reason == "empty_landing_set":
        return "锚之后无落地任务（或任务号正则过滤后为空），缺行无从定义"
    if reason == "changelog_unreadable":
        return f"CHANGELOG 不可读（{detail}）"
    return f"{reason}（{detail}）" if detail else reason


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """逐项比对「已落地任务」与 CHANGELOG 全文，缺行即产出 finding。

    降级（无锚 / git 不可读 / CHANGELOG 不可读 / 配置非法）以 ``RuntimeError``
    交给 done 分派层记录 ``degraded_guards``（failed），**不把降级误判为命中**。
    """
    project_root = context.project_root
    if project_root is None:
        return ()
    root = Path(project_root)
    try:
        policy = _configured_policy(root)
    except DocsGuardConfigError as exc:
        raise RuntimeError(f"docs guard 配置非法：{exc}") from exc
    # 引擎判据真源：函数级导入（守卫包保持叶子，不在 import 期拉起 report/ledger）。
    from orchd.report.audit import changelog_landing_scan

    scan = changelog_landing_scan(
        root,
        changelog_rel=policy.changelog_rel,
        task_pattern=re.compile(policy.task_pattern),
        anchor=policy.anchor,
    )
    if scan.degradation is not None:
        message = _degradation_message(scan.degradation, scan.detail)
        raise RuntimeError(f"docs guard 降级（{scan.degradation}）：{message}")
    if not scan.landed:
        # 空落地集（锚之后无任务 / task_pattern 过滤后为空）同样无法判定
        # 「缺行」——按降级留痕（failed）而不是静默通过，避免 AC2 的
        # 「缺失一律降级留痕」在边界漏账。
        raise RuntimeError(
            "docs guard 降级（empty_landing_set）：锚之后无落地任务"
            "（或 task_pattern 过滤后为空），缺行无从定义"
        )
    findings: list[GuardFinding] = []
    for task_id in scan.missing:
        findings.append(
            GuardFinding(
                guard="docs",
                message=f"落地任务未在 CHANGELOG 在册：{task_id}",
                context={
                    "task_id": task_id,
                    "changelog": policy.changelog_rel,
                    "anchor": scan.anchor,
                },
            )
        )
    return tuple(findings)


def register() -> None:
    """把 ``docs`` 评估器登记进 guards 注册表（重复调用幂等）。"""
    register_guard_evaluator("docs", evaluate)


register()


__all__ = (
    "ANCHOR_OPTION",
    "CHANGELOG_OPTION",
    "DEFAULT_CHANGELOG_REL",
    "DEFAULT_TASK_PATTERN",
    "DocsGuardConfigError",
    "DocsPolicy",
    "KNOWN_OPTIONS",
    "TASK_PATTERN_OPTION",
    "evaluate",
    "parse_policy",
    "register",
)
