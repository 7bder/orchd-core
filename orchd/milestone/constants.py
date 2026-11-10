"""里程碑域常量：退出码阈值 / 里程碑 ID 表 / 预算 / 制品 / 诚实探针 / 契约锚点，叶域。

宿主覆盖层（task-host-guard-release）：:data:`HOST_OVERRIDABLE` 列出**开放覆盖**的常量，
:func:`resolve_overrides` 校验宿主声明（``guards.json`` 的 ``release.milestone``），
:func:`merge_overrides` 给出「默认 ⊕ 覆盖」的有效值视图。本模块保持纯字面量 + 纯函数：
不读盘、不读环境、零子进程；**不覆盖时** :func:`milestone_defaults` 与模块常量逐条相等。

覆盖面刻意只开放 ``EXIT_VERSIONS`` / ``MILESTONE_IDS``：它们是发版通道（tag → 版本 →
里程碑 id）的映射，宿主换一套里程碑口径是合理需求。其余常量（``PERF_SPAWN_BUDGET_MAX``
「不得上调」、``KERNEL_CONTRACT_ANCHORS``、``DRILL_ARTIFACT`` 等）由 M1/M2 判据在 import
期按冻结口径直读，**不开放覆盖**——开放了却驱动不到判据，等于给宿主一个假开关。

作用域（显式契约，防「同一常量两套读法」被当缺陷或当特性）：覆盖值**只作用于
``orchd release`` 通道**的 effective 视图与 ``milestone_id`` 反查；模块级常量本身不被改写，
``orchd milestone-check``（``orchd/milestone/entry.py`` 的 import 期常量）继续按引擎冻结
口径判定。两条命令的口径差异是**设计边界**，不是同步缺陷；本卡的用例钉死这一点。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchd.names import (
    FULL_REGRESSION_FILE,
    ROADMAP_FILE,
    TEST_BASELINE_FILE,
)


# 里程碑 → 出口版本
EXIT_VERSIONS: dict[str, str] = {
    "freeze": "v1.5.0",
    "split-ready": "v1.6.0",
}


# 里程碑 id（与 ROADMAP「## 里程碑」节声明一致）
MILESTONE_IDS: dict[str, str] = {
    "freeze": "local-kernel-freeze",
    "split-ready": "line-split-ready",
}


# 性能冻结口径：``tests/test_perf_budget.py`` 的 git 子进程上限**不得上调**
PERF_BUDGET_TEST = "tests/test_perf_budget.py"


PERF_BUDGET_CONST = "PERF_BASELINE_WATCHDOG_FLOW_GIT_SPAWNS"


PERF_SPAWN_BUDGET_MAX = 40


# M2 演练产物与命名口径
DRILL_ARTIFACT = "line-drill.json"


DRILL_TEST_FILE = "tests/test_line_drill.py"


NAMING_PROBE_TASK = "t1"


NAMING_PROBE_EXPECTED = "task/t1"


LINE_MODULE = "orchd.line"


LINE_SYNC_COMMAND = "line-sync"


RELEASE_SCRIPT = "scripts/sync_orchd_core.sh"


# 契约文档与关键锚点（文档缺失 / 锚点缺失即未达成）
KERNEL_CONTRACT_DOC = "docs/kernel-contract.md"


KERNEL_CONTRACT_ANCHORS = ("不变量", "端口")


ROADMAP_FILE = ROADMAP_FILE


ROADMAP_MILESTONE_ANCHOR = "## 里程碑"


_FULL_REGRESSION_FILE = FULL_REGRESSION_FILE


_BASELINE_FILE = TEST_BASELINE_FILE


class MilestoneOverrideError(ValueError):
    """milestone 覆盖值非法：``errors`` 为结构化错误项（key / kind / message）。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors: tuple[dict[str, str], ...] = tuple(errors)
        summary = "；".join(
            f"{item.get('key')}: {item.get('message')}" for item in self.errors[:5]
        )
        super().__init__(f"milestone 覆盖值非法（{len(self.errors)} 项）：{summary}")


#: 开放宿主覆盖的常量（发版通道映射面）。其余常量为 M1/M2 冻结判据口径，不开放。
HOST_OVERRIDABLE: tuple[str, ...] = (
    "EXIT_VERSIONS",
    "MILESTONE_IDS",
)


def milestone_defaults() -> dict[str, object]:
    """引擎默认值视图（逐条等于本模块常量；覆盖层与对拍共用同一入口）。"""
    return {name: globals()[name] for name in HOST_OVERRIDABLE}


def _error(kind: str, message: str, key: str) -> dict[str, str]:
    return {"key": key, "kind": kind, "message": message}


def _coerce(name: str, default: object, value: object) -> object:
    """按默认值类型校验覆盖值，形态不符即结构化报错（不静默丢弃）。

    当前开放面只有字典型常量（EXIT_VERSIONS / MILESTONE_IDS）；出现其它类型说明有人把
    常量加进了 :data:HOST_OVERRIDABLE 却没扩展本函数——直接拒绝并点名，不留半驱动分支。
    """
    if not isinstance(default, dict):
        raise MilestoneOverrideError(
            [
                _error(
                    "unsupported",
                    f"{name} 的类型（{type(default).__name__}）不支持覆盖；"
                    "开放新类型须同步扩展 constants._coerce",
                    name,
                )
            ]
        )
    if not isinstance(value, Mapping):
        raise MilestoneOverrideError(
            [_error("invalid_type", f"{name} 必须是对象，实际为 {value!r}", name)]
        )
    # 字典型常量按 key 合并：宿主可只覆盖自己关心的条目，其余保持引擎默认。
    out: dict[str, str] = {str(key): str(item) for key, item in default.items()}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise MilestoneOverrideError(
                [_error("invalid_type", f"{name} 的键必须是非空字符串：{key!r}", name)]
            )
        if not isinstance(item, str) or not item.strip():
            raise MilestoneOverrideError(
                [
                    _error(
                        "invalid_type",
                        f"{name}.{key} 必须是非空字符串，实际为 {item!r}",
                        name,
                    )
                ]
            )
        out[key] = item
    return out


def resolve_overrides(raw: object) -> dict[str, object]:
    """校验宿主覆盖声明，返回「仅覆盖项」的映射（无声明返回空 dict）。

    Args:
        raw: ``guards.json`` 的 ``release.milestone`` 段（``None`` 视为未声明）。

    Raises:
        MilestoneOverrideError: 未开放覆盖的名字 / 类型不符 / 空值（逐项结构化）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MilestoneOverrideError(
            [_error("invalid_type", f"milestone 必须是对象，实际为 {raw!r}", "milestone")]
        )
    defaults = milestone_defaults()
    resolved: dict[str, object] = {}
    errors: list[dict[str, str]] = []
    for name, value in raw.items():
        if name not in defaults:
            allowed = "、".join(HOST_OVERRIDABLE)
            errors.append(
                _error("unknown_key", f"不允许覆盖的常量：{name}（可覆盖：{allowed}）", str(name))
            )
            continue
        try:
            resolved[name] = _coerce(name, defaults[name], value)
        except MilestoneOverrideError as exc:
            errors.extend(exc.errors)
    if errors:
        raise MilestoneOverrideError(errors)
    return resolved


def merge_overrides(raw: object) -> dict[str, Any]:
    """返回「默认 ⊕ 覆盖」的有效值视图（未覆盖的常量逐条保持引擎默认）。"""
    effective: dict[str, Any] = dict(milestone_defaults())
    effective.update(resolve_overrides(raw))
    return effective


def milestone_for_version(tag_or_version: str, effective: Mapping[str, Any]) -> str | None:
    """按有效 ``EXIT_VERSIONS`` / ``MILESTONE_IDS`` 反查版本对应里程碑 id。

    版本号容忍 ``v`` 前缀（与 ``orchd release`` 的 tag 口径一致）；未声明返回 ``None``。
    """
    desired = tag_or_version if tag_or_version.startswith("v") else f"v{tag_or_version}"
    exit_versions = effective.get("EXIT_VERSIONS", {})
    milestone_ids = effective.get("MILESTONE_IDS", {})
    if not isinstance(exit_versions, Mapping) or not isinstance(milestone_ids, Mapping):
        return None
    for name, version in exit_versions.items():
        if version == desired:
            found = milestone_ids.get(name)
            return found if isinstance(found, str) else None
    return None
