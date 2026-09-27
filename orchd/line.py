"""orchd/line.py — 多主干（line）解析层（task-line-core）。

设计真源：``design/line-split-design-20260923.md``。本模块是**纯解析层**：只读显式
配置（``project.lines``）与 ``ORCHD_LINE`` 环境变量，**不做 ref 探测、不调用 git**；
依赖方向 ``line.py → errors.py``（不导入 gitops / onboard，无环）。

opt-in 增量语义（单线零回归）：

- 未配置 ``project.lines`` ⇒ 唯一线 ``default``，trunk = ``main``；
- 任务分支名前缀恒为 ``task/``（单根命名空间；多线为 ``task/{line}/{id}``）。

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
            or "/" in name
            or not isinstance(trunk, str)
            or not trunk
        ):
            raise OrchdError(
                ErrorCode.E005,
                f"project.lines 登记非法：line '{name}' 须形如 "
                '{"trunk": "<分支名>"}（trunk 为非空字符串，'
                '线名不得含 /——保障 task/{line}/{id} 可反解',
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
    单线模式恒 ``default``（含显式 ``ORCHD_LINE=default`` 别名，与 resolve_trunk
    对称；pass8 F8）。``ORCHD_LINE`` 指向未登记线 → ``E005``（硬拒绝，不静默回退）。
    """
    config = _lines_config(project)
    environ = os.environ if env is None else env
    requested = environ.get(LINE_ENV_VAR)
    if isinstance(requested, str) and requested.strip():
        requested = requested.strip()
        if not config:
            # 单线模式：仅接受 default 别名（与 resolve_trunk /
            # resolve_task_branch_name 同口径）；其余仍 E005
            if requested == DEFAULT_LINE:
                return DEFAULT_LINE
            raise _unknown_line(requested, [DEFAULT_LINE])
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
    """任务分支名：单线 ``task/{id}``；多线 ``task/{line}/{id}``。未知 line → ``E005``。

    单根命名空间（pass8 F1 设计级修复）：旧 ``{line}/task/{id}`` 形态已废除。
    trunk 分支 ``{line}`` 与其构成 git D/F 引用冲突（演练实证），``task/`` 前缀对全线恒成立。
    """
    config = _lines_config(project)
    if not config:
        if line is not None and line != "" and line != DEFAULT_LINE:
            raise _unknown_line(line, [DEFAULT_LINE])
        return f"task/{task_id}"
    target = line or _default_line_name(project, config)
    if target not in config:
        raise _unknown_line(target, sorted(config))
    return f"task/{target}/{task_id}"

TASK_BRANCH_ROOT = "task/"


def parse_task_branch(branch: object) -> tuple[str | None, str] | None:
    """任务分支反解 → ``(line|None, task_id)``；非任务分支 → None。

    单根命名空间（pass8 F1 设计级修复）的唯一反解点：
    ``task/{id}`` → ``(None, id)``；``task/{line}/{id}`` → ``(line, id)``。
    id 字符集不含 ``/``（schema ``^task-[a-z0-9-]+$``），故末段恒为完整 id；
    旧 ``{line}/task/{id}`` 形态不再识别（fail-closed：按非任务分支处置）。
    """
    if not isinstance(branch, str) or not branch.startswith(TASK_BRANCH_ROOT):
        return None
    rest = branch[len(TASK_BRANCH_ROOT):]
    if not rest:
        return None
    if "/" not in rest:
        return None, rest
    line, _, tid = rest.partition("/")
    if not line or not tid or "/" in tid:
        return None
    return line, tid
