"""orchd/line.py — 多主干（line）解析层（task-line-core）。

设计真源：``design/line-split-design-20260923.md``。本模块是**纯解析层**：只读显式
配置（``project.lines``）与 ``ORCHD_LINE`` 环境变量，**不做 ref 探测、不调用 git**；
依赖方向 ``line.py → errors.py``（不导入 gitops / onboard，无环）。

opt-in 增量语义（单线零回归）：

- 未配置 ``project.lines`` ⇒ 唯一线 ``default``，trunk = ``main``；
- 任务分支名 = ``task/{id}``（不得泄漏为 ``{line}/task/{id}``）。

M2 判据锚点（``orchd/milestone.py``）：

- A1：``resolve_trunk`` 可调用；
- F3：``resolve_task_branch_name("t1") == "task/t1"``。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from orchd.errors import ErrorCode, OrchdError

DEFAULT_LINE = "default"
DEFAULT_TRUNK = "main"
LINE_ENV_VAR = "ORCHD_LINE"

# 配置入参：project 为 ``_master.json`` 的 ``project`` 段；env 供测试注入。
ProjectConfig = Mapping[str, Any] | None
Env = Mapping[str, str] | None


def _lines_config(project: ProjectConfig) -> dict[str, str]:
    """解析 ``project.lines`` 为 ``{line: trunk}``；缺省 → 空（单线）。

    task-line-config-contract：**不静默跳过非法项**——线名或 trunk 非法即 E005
    结构化上报（与「不静默回退」口径一致）。形态以 schema 为唯一契约
    （``{line: {"trunk": <branch>}}``），故不再支持 ``"online": "online"`` 字符串
    简写（该形态 schema 不认，原为死分支）。
    """
    if not isinstance(project, Mapping):
        return {}
    raw = project.get("lines")
    if not isinstance(raw, Mapping):
        return {}
    config: dict[str, str] = {}
    for name, spec in raw.items():
        trunk = spec.get("trunk") if isinstance(spec, Mapping) else None
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(trunk, str)
            or not trunk
        ):
            raise OrchdError(
                ErrorCode.E005,
                f"project.lines 登记非法：line '{name}' 须形如 "
                '{"trunk": "<分支名>"}（trunk 为非空字符串）',
                [{"line": name, "spec": spec}],
            )
        config[name] = trunk
    return config


def _unknown_line(line: str, known: list[str]) -> OrchdError:
    """未知线错误（E005 reference_not_found，硬拒绝不静默回退）。"""
    return OrchdError(
        ErrorCode.E005,
        f"line '{line}' 未在 project.lines 登记（已知线：{known}）",
        [{"line": line, "known_lines": known}],
    )


def is_multi_line(project: ProjectConfig = None) -> bool:
    """是否启用多线（``project.lines`` 至少有一个合法登记）。"""
    return bool(_lines_config(project))


def line_registry(project: ProjectConfig = None) -> dict[str, str]:
    """line → trunk 映射；单线模式返回 ``{"default": "main"}``（显式配置为唯一来源）。"""
    config = _lines_config(project)
    return dict(config) if config else {DEFAULT_LINE: DEFAULT_TRUNK}


def _default_line_name(project: ProjectConfig, config: Mapping[str, str]) -> str:
    """默认线：``project.default_line``（**须为 lines 的键**）→ 首个登记线；单线恒 ``default``。

    task-line-config-contract：``default_line`` 非登记线 → E005（schema 已承诺
    「须为 lines 的键」，此为其执行点；此前静默回退首个登记线）。
    """
    if not config:
        return DEFAULT_LINE
    declared = project.get("default_line") if isinstance(project, Mapping) else None
    if isinstance(declared, str) and declared:
        if declared not in config:
            raise _unknown_line(declared, sorted(config))
        return declared
    return next(iter(config))


def resolve_line(project: ProjectConfig = None, env: Env = None) -> str:
    """当前线名。

    解析序：``ORCHD_LINE``（在册）→ ``project.default_line``（在册）→ 首个登记线；
    单线模式恒 ``default``。``ORCHD_LINE`` 指向未登记线 → ``E005``（硬拒绝，不静默回退）。
    """
    config = _lines_config(project)
    environ = os.environ if env is None else env
    requested = environ.get(LINE_ENV_VAR)
    if isinstance(requested, str) and requested.strip():
        requested = requested.strip()
        if requested not in config:
            raise _unknown_line(requested, sorted(config) or [DEFAULT_LINE])
        return requested
    return _default_line_name(project, config)


def resolve_trunk(line: str | None = None, project: ProjectConfig = None) -> str:
    """返回该 line 的 trunk；单线恒 ``main``。未知 line → ``E005``。

    显式配置是唯一来源，**不探测 ref**：``project.lines`` 缺失即单线。
    """
    config = _lines_config(project)
    if not config:
        if line is None or line == "" or line == DEFAULT_LINE:
            return DEFAULT_TRUNK
        raise _unknown_line(line, [DEFAULT_LINE])
    target = line or _default_line_name(project, config)
    if target not in config:
        raise _unknown_line(target, sorted(config))
    return config[target]


def resolve_task_branch_name(
    task_id: str, line: str | None = None, project: ProjectConfig = None
) -> str:
    """任务分支名：单线 ``task/{id}``；多线 ``{line}/task/{id}``。未知 line → ``E005``。"""
    config = _lines_config(project)
    if not config:
        if line is not None and line != "" and line != DEFAULT_LINE:
            raise _unknown_line(line, [DEFAULT_LINE])
        return f"task/{task_id}"
    target = line or _default_line_name(project, config)
    if target not in config:
        raise _unknown_line(target, sorted(config))
    return f"{target}/task/{task_id}"
