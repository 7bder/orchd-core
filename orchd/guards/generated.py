"""宿主 guard ``generated``：生成物新鲜度（generator → artifact 清单）。

把引擎 A2 的「生成物内容比对」判据（:func:`orchd.drift_closure.compare_generated_artifacts`）
泛化为**宿主可声明**的清单：宿主在 ``.orchd/shared/guards.json`` 里为 ``generated`` 登记
``artifacts``，done 期（以及后续 ``guards check generated`` 只读入口）逐产物比对
「生成器函数输出」与「磁盘内容」，不一致即命中；是否阻断由 guard 档位（``warn`` /
``hard``）决定，off 时评估器不会被调用。

配置形态（选项字段在本 guard 内校验，非法即降级留痕、不阻断）::

    {
      "schema_version": 1,
      "guards": {
        "generated": {
          "mode": "warn",
          "artifacts": [
            {
              "module": "scripts.gen_contract_docs",
              "function": "gen_commands_md",
              "artifact": "docs/_generated/commands.md"
            }
          ]
        }
      }
    }

- ``module`` / ``function``：生成器可调用对象（``importlib`` 导入后取属性，零参数调用，
  返回期望的产物文本）；
- ``artifact``：仓库相对 POSIX 路径（禁止绝对路径与 ``..``），磁盘上缺失按「求值失败」
  降级留痕而不是当过期的假阳性。

纪律：只读（不写盘、不起子进程）；单次评估受
:data:`orchd.drift_closure.GENERATED_STALE_BUDGET_S` 预算约束，超预算 / 生成器不可读 /
产物读取失败一律**降级留痕、不阻断 done**。判据本体与引擎 ``generated_stale`` 完全同源
（AC4：引擎仓既有行为不变）。

本模块 import 期即注册 ``generated`` 评估器（``evaluate_guard`` 会按需导入本模块）。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from orchd.drift_closure import compare_generated_artifacts
from orchd.guards import (
    GuardEvalContext,
    GuardFinding,
    load_guards_config,
    register_guard_evaluator,
)

#: ``generated`` guard 的配置选项键（选项里登记 generator → artifact 清单）。
ARTIFACTS_OPTION = "artifacts"

#: 允许出现在单个 artifact 条目里的字段（未知字段视为配置错误，避免拼写造成静默漏跑）。
_ARTIFACT_FIELDS = frozenset({"module", "function", "artifact"})


class GeneratedGuardConfigError(ValueError):
    """``generated`` guard 选项非法（降级留痕用；不是引擎错误码）。"""


def _normalize_artifact_path(raw: Any) -> str:
    """校验 ``artifact`` 为仓库相对路径，返回归一后的 POSIX 字符串。"""
    if not isinstance(raw, str) or not raw.strip():
        raise GeneratedGuardConfigError("artifact 必须是非空字符串（仓库相对路径）")
    text = raw.strip().replace("\\", "/")
    if ":" in text:  # Windows 盘符（C:/...）在 POSIX 语义下不是绝对路径，需显式拒绝
        raise GeneratedGuardConfigError(f"artifact 必须是仓库相对路径：{raw!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] == ("",):
        raise GeneratedGuardConfigError(f"artifact 必须是仓库相对路径：{raw!r}")
    return str(path)


def _normalize_generator_name(raw: Any, field: str) -> str:
    """校验 ``module`` / ``function`` 名字非空且是普通点分标识符。"""
    if not isinstance(raw, str) or not raw.strip():
        raise GeneratedGuardConfigError(f"{field} 必须是非空字符串")
    text = raw.strip()
    if any(part and not part.isidentifier() for part in text.split(".")):
        raise GeneratedGuardConfigError(f"{field} 不是合法点分标识符：{raw!r}")
    return text


def parse_artifacts(options: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """解析并校验条目化清单，返回规范化的 ``(module, function, artifact)`` 字典元组。

    Raises:
        GeneratedGuardConfigError: ``artifacts`` 不是列表、条目不是对象、字段缺失/多余、
            或 ``artifact`` 不是仓库相对路径。
    """
    raw = options.get(ARTIFACTS_OPTION)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GeneratedGuardConfigError("artifacts 必须是列表")
    parsed: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise GeneratedGuardConfigError(f"artifacts[{index}] 必须是对象")
        extra = set(item) - _ARTIFACT_FIELDS
        missing = _ARTIFACT_FIELDS - set(item)
        if extra:
            raise GeneratedGuardConfigError(f"artifacts[{index}] 含未知字段：{sorted(extra)}")
        if missing:
            raise GeneratedGuardConfigError(f"artifacts[{index}] 缺少字段：{sorted(missing)}")
        parsed.append(
            {
                "module": _normalize_generator_name(item["module"], "module"),
                "function": _normalize_generator_name(item["function"], "function"),
                "artifact": _normalize_artifact_path(item["artifact"]),
            }
        )
    return tuple(parsed)


def _generator_callable(module: str, function: str) -> Callable[[], str]:
    """按 ``module:function`` 组装零参生成器（导入/取属性延迟到求值点，便于单产物降级）。"""

    def _call() -> str:
        target = importlib.import_module(module)
        generator = getattr(target, function)
        return generator()

    return _call


def _configured_artifacts(project_root: Path) -> tuple[dict[str, str], ...]:
    """从 ``guards.json`` 读取 ``generated`` 的清单（未配置 / off ⇒ 空）。

    评估器拿不到自身选项（:class:`GuardEvalContext` 是公共只读上下文），故重读一次配置；
    配置已由 done 分派层校验过合法性，这里的异常（文件级）由分派层统一留痕。
    """
    config = load_guards_config(project_root)
    for entry in config.entries:
        if entry.name == "generated":
            return parse_artifacts(entry.options)
    return ()


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """``generated`` guard 评估器：逐产物比对，返回过期项（无过期/清单空 ⇒ 空元组）。

    Raises:
        RuntimeError: 配置非法 / 生成器不可读 / 产物读取失败 / 评估超预算——
            由 done 分派层统一记为 ``degraded_guards``（failed），**不阻断** done。
    """
    root = context.project_root
    if root is None:
        return ()
    try:
        artifacts = _configured_artifacts(root)
    except GeneratedGuardConfigError as exc:
        raise RuntimeError(f"generated guard 配置非法：{exc}") from exc
    if not artifacts:
        return ()
    pairs = [
        (item["artifact"], _generator_callable(item["module"], item["function"]))
        for item in artifacts
    ]
    stale, degradates, _elapsed = compare_generated_artifacts(pairs, root)
    if degradates:
        reasons = "；".join(str(item.get("reason", "")) for item in degradates)
        errors = "；".join(str(item.get("error", "")) for item in degradates if item.get("error"))
        detail = f"{reasons}（{errors}）" if errors else reasons
        raise RuntimeError(f"generated guard 降级：{detail}")
    findings: list[GuardFinding] = []
    for artifact in stale:
        findings.append(
            GuardFinding(
                guard="generated",
                message=f"生成物过期：{artifact} 与生成器当前输出不一致",
                context={
                    "artifact": artifact,
                    "generator": _generator_label(artifacts, artifact),
                },
            )
        )
    return tuple(findings)


def _generator_label(artifacts: Sequence[Mapping[str, str]], artifact: str) -> str:
    """命中项里回显生成器来源（``module:function``），便于修复定位。"""
    for item in artifacts:
        if item["artifact"] == artifact:
            return f"{item['module']}:{item['function']}"
    return ""


def register() -> None:
    """注册 ``generated`` 评估器（import 期调用；``evaluate_guard`` 懒加载时也会补调）。"""
    register_guard_evaluator("generated", evaluate)


#: 模块 import 期注册（``evaluate_guard`` 未命中时会按需导入本模块）。
register()

__all__ = (
    "ARTIFACTS_OPTION",
    "GeneratedGuardConfigError",
    "evaluate",
    "parse_artifacts",
    "register",
)
