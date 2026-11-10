"""宿主 guard 通用层（task-host-guard-config / task-host-guard-done-wiring）。

本包承载「宿主仓可自行启用的 guard」的**配置层 + 分派骨架**：

- 配置层：读取 + 严格校验 ``.orchd/shared/guards.json``，并给
  ``orchd guards list`` 与 ``orchd doctor`` 提供只读视图。缺省（无配置 /
  空配置）时全部 guard 为 ``off``，引擎行为不变；配置非法时 fail-closed
  返回结构化错误。
- 评估器注册表：各 ``host-guard-*`` 卡把自身判据实现为
  :data:`GuardEvaluator`（接收 :class:`GuardEvalContext`，返回
  :class:`GuardFinding` 序列），经 :func:`register_guard_evaluator` 注册到
  :data:`GUARD_EVALUATORS`；done 期由
  ``orchd/onboard/lifecycle/core/entry_guard.py::_record_host_guards``
  按配置档位分派（``off`` 不执行 / ``warn`` 只写 ``degraded_guards`` /
  ``hard`` 抛 E039 ``details.rule=<guard>``）。
- 评估器子模块（``orchd/guards/<guard>.py``）在模块 import 期自行注册；
  :func:`evaluate_guard` 在注册表未命中时**按需 import 一次**，因此 done 链路
  无需预先导入全部 guard 实现。子模块可复用引擎只读判据模块（如
  ``orchd.drift_closure``），但本包 ``__init__`` 仍不 import ``orchd.cli`` /
  ``orchd.doctor`` 等上层模块（避免循环与副作用）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchd.guards.config import (
    CONFIG_REL,
    GUARD_NAMES,
    MODE_HARD,
    MODE_OFF,
    MODE_WARN,
    MODES,
    SCHEMA_VERSION,
    GuardEntry,
    GuardsConfig,
    GuardsConfigError,
    doctor_echo,
    guards_list_payload,
    load_guards_config,
    parse_guards_config,
)

__all__ = (
    "CONFIG_REL",
    "GUARD_NAMES",
    "GUARD_EVALUATORS",
    "MODE_HARD",
    "MODE_OFF",
    "MODE_WARN",
    "MODES",
    "SCHEMA_VERSION",
    "GuardEntry",
    "GuardEvalContext",
    "GuardEvaluator",
    "GuardFinding",
    "GuardsConfig",
    "GuardsConfigError",
    "available_guard_evaluators",
    "clear_guard_evaluators",
    "doctor_echo",
    "evaluate_guard",
    "guards_list_payload",
    "load_guards_config",
    "parse_guards_config",
    "register_guard_evaluator",
)


@dataclass(frozen=True)
class GuardEvalContext:
    """一次 done 评估的只读上下文（评估器禁止在此之上写盘 / 起子进程）。"""

    project_root: Path | None
    task_id: str
    verify_cmd: str = ""
    changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardFinding:
    """单条 guard 命中（``guard`` 必须与配置里的 guard 名一致）。"""

    guard: str
    message: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """转成可进 ``degraded_guards`` / ``E039 details`` 的纯 dict。"""
        return {
            "guard": self.guard,
            "message": self.message,
            "context": dict(self.context),
        }


#: 评估器签名：纯函数风格，返回 ``None`` 表示该 guard 尚无评估器（调用方留痕）。
GuardEvaluator = Callable[[GuardEvalContext], Sequence[GuardFinding] | Iterable[GuardFinding]]

#: 已注册评估器（guard 名 → 评估器）。各 ``host-guard-*`` 卡在自己的模块里
#: 于 import 时显式调用 :func:`register_guard_evaluator` 注册（本包不自动发现）。
GUARD_EVALUATORS: dict[str, GuardEvaluator] = {}


def register_guard_evaluator(name: str, evaluator: GuardEvaluator) -> None:
    """注册（或替换）一个 guard 评估器。

    Args:
        name: 必须 ∈ :data:`GUARD_NAMES`（配置层认识的 guard 名）。
        evaluator: 接收 :class:`GuardEvalContext` 的可调用对象。

    Raises:
        ValueError: guard 名未在 :data:`GUARD_NAMES` 登记。
        TypeError: ``evaluator`` 不可调用。
    """
    if name not in GUARD_NAMES:
        raise ValueError(f"guard 名未登记：{name!r}（须先加入 GUARD_NAMES）")
    if not callable(evaluator):
        raise TypeError(f"guard 评估器必须可调用：{evaluator!r}")
    GUARD_EVALUATORS[name] = evaluator


def clear_guard_evaluators() -> None:
    """清空评估器注册表（仅供测试隔离使用；生产路径不应调用）。"""
    GUARD_EVALUATORS.clear()


def available_guard_evaluators() -> tuple[str, ...]:
    """当前已接入评估器的 guard 名（按 :data:`GUARD_NAMES` 顺序）。"""
    return tuple(name for name in GUARD_NAMES if name in GUARD_EVALUATORS)


def _import_guard_module(name: str) -> None:
    """按需导入 ``orchd.guards.<name>``（子模块在 import 期自行注册评估器）。

    导入失败（模块不存在 / 模块内异常）按「尚无评估器」处理：调用方会据此留痕
    ``not_applicable``，不得让 done 裸崩。
    """
    import importlib

    try:
        module = importlib.import_module(f"{__name__}.{name}")
    except Exception:  # noqa: BLE001 - 子模块缺失/导入失败 ⇒ 视为尚无评估器
        return
    if name in GUARD_EVALUATORS:
        return
    # 模块可能已被更早的 import 缓存（import 期注册不会重放）：约定子模块暴露
    # ``register()``，命中缓存时显式补注册一次。
    register = getattr(module, "register", None)
    if callable(register):
        register()


def evaluate_guard(name: str, context: GuardEvalContext) -> tuple[GuardFinding, ...] | None:
    """调用一个 guard 评估器（未注册时按需导入 ``orchd.guards.<name>``）。

    Args:
        name: guard 名（仍无评估器时返回 ``None``，由调用方决定留痕方式）。
        context: 评估上下文。

    Returns:
        命中项元组；``None`` 表示该 guard 尚无评估器。
    """
    evaluator = GUARD_EVALUATORS.get(name)
    if evaluator is None:
        _import_guard_module(name)
        evaluator = GUARD_EVALUATORS.get(name)
    if evaluator is None:
        return None
    return tuple(evaluator(context))
