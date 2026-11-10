"""宿主发版 guard 配置层：静态检查面 / 基线参数 / 入口脚本 / milestone 覆盖值的解析校验。

宿主在 `.orchd/shared/guards.json` 的 `release` 段声明（全部键可省）::

    {"schema_version": 1,
     "guards": {"release": {
         "mode": "warn",
         "check": {
             "ruff": {"module": "ruff", "args": ["check", "src/"],
                      "paths": ["src/"]}
         },
         "baseline": {"path": ".orchd/test_baseline.json",
                      "pytest_args": ["tests/", "-n", "4"],
                      "targets": ["tests/"]},
         "scripts": {"entry": ".orchd/__main__.py",
                     "release_ready": "scripts/check_release_ready.py",
                     "baseline": "scripts/check_test_baseline.py"},
         "milestone": {"EXIT_VERSIONS": {"host-ready": "v2.0.0"}}
     }}}

校验口径（fail-closed，全部结构化错误）：

- 缺省逐条等于引擎现值（:data:CHECK_SPECS / :data:DEFAULT_PYTEST_ARGS /
  `DEFAULT_*_SCRIPT`），故引擎仓无配置时命令行为与响应逐条不变；
- `check.<name>`：`paths` 必须非空、必须出现在 `args` 里、不得与子命令同名、不得紧跟选项
  （路径只允许位置参数）；覆盖 `args` 而未声明 `paths` 直接拒绝 —— 不允许「靠猜哪些参数是路径」；
- `baseline.targets` 必须出现在 `pytest_args` 里（判据不得盯一条永不执行的路径）；
- `milestone` 只做「必须是对象」的形状校验，语义校验归
  :func:orchd.milestone.constants.resolve_overrides（不建第二份真源）。

本模块只读配置并做纯校验：不写盘、不起子进程、不执行检查；
计划构建与 done 期评估在 :mod:orchd.guards.release。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchd.guards import GuardsConfigError, load_guards_config

__all__ = (
    "CHECK_SPECS",
    "DEFAULT_BASELINE_PATH",
    "DEFAULT_BASELINE_SCRIPT",
    "DEFAULT_BASELINE_TARGETS",
    "DEFAULT_CHECKS",
    "DEFAULT_ENTRY_SCRIPT",
    "DEFAULT_PYTEST_ARGS",
    "DEFAULT_READY_SCRIPT",
    "CheckSpec",
    "ReleaseGuardConfigError",
    "ReleasePolicy",
    "load_release_policy",
    "parse_policy",
)


@dataclass(frozen=True)
class CheckSpec:
    """单个静态检查的声明（python ``-m <module> <args...>``）。

    ``paths`` 是 **必须存在的目标路径**（非空）：空表会让「路径存在性」判据整体失效，
    等于把不存在的路径直接交给子进程报 ENOENT，故构造期即拒绝（fail-closed）。
    """

    name: str
    module: str
    args: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    #: 该检查的子命令 token（如 ruff 的 `check`）：既不判存在性，也不当未声明路径。
    subcommands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.paths:
            raise ValueError(f"check {self.name} 的 paths 不得为空（须显式声明目标路径）")

    def python_args(self, args: tuple[str, ...] | None = None) -> list[str]:
        """转成 ``python`` 参数列表（``-m`` 形态，与 ``run_check`` 契约一致）。

        ``args`` 给出时表示「剔除缺失路径后的参数」——计划期与默认口径共用同一实现，
        不手撮第二份。
        """
        return ["-m", self.module, *(self.args if args is None else args)]


#: 引擎默认静态门禁清单（与 .githooks/pre-push R-6 同口径）。
CHECK_SPECS: tuple[CheckSpec, ...] = (
    CheckSpec(
        name="ruff",
        module="ruff",
        args=("check", "orchd/", "scripts/", "tests/", "release/"),
        paths=("orchd/", "scripts/", "tests/", "release/"),
        subcommands=("check",),
    ),
    CheckSpec(name="mypy", module="mypy", args=("orchd/",), paths=("orchd/",)),
)

#: ``orchd check`` 的默认检查清单（名称 → ``python`` 参数）；由 :data:`CHECK_SPECS` 派生。
DEFAULT_CHECKS: dict[str, list[str]] = {spec.name: spec.python_args() for spec in CHECK_SPECS}

DEFAULT_BASELINE_PATH = ".orchd/test_baseline.json"
DEFAULT_PYTEST_ARGS: tuple[str, ...] = ("tests/", "-n", "8", "--max-worker-restart=5")
#: 基线步须判存在性的 pytest 目标路径（显式声明，不靠「像不像路径」猜）。
DEFAULT_BASELINE_TARGETS: tuple[str, ...] = ("tests/",)
DEFAULT_ENTRY_SCRIPT = ".orchd/__main__.py"
DEFAULT_READY_SCRIPT = "scripts/check_release_ready.py"
DEFAULT_BASELINE_SCRIPT = "scripts/check_test_baseline.py"

_KNOWN_TOP_KEYS = frozenset({"check", "baseline", "scripts", "milestone"})
_KNOWN_CHECK_KEYS = frozenset({"module", "args", "paths"})
_KNOWN_BASELINE_KEYS = frozenset({"path", "pytest_args", "targets"})
_KNOWN_SCRIPT_KEYS = frozenset({"entry", "release_ready", "baseline"})


class ReleaseGuardConfigError(ValueError):
    """release guard 配置非法：``errors`` 为结构化错误项（kind / message / 可选 key）。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors: tuple[dict[str, str], ...] = tuple(errors)
        summary = "；".join(
            f"{item.get('kind')}: {item.get('message')}" for item in self.errors[:5]
        )
        super().__init__(f"release guard 配置非法（{len(self.errors)} 项）：{summary}")


def _error(kind: str, message: str, *, key: str | None = None) -> dict[str, str]:
    item = {"kind": kind, "message": message}
    if key is not None:
        item["key"] = key
    return item


def _rel_path(value: object, field_name: str) -> str:
    """校验并归一化「项目内相对路径」（禁绝对路径 / 盘符 / ``..`` 越界）。"""
    if not isinstance(value, str) or not value.strip():
        raise ReleaseGuardConfigError(
            [
                _error(
                    "invalid_type",
                    f"{field_name} 必须是非空字符串，实际为 {value!r}",
                    key=field_name,
                )
            ]
        )
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ReleaseGuardConfigError(
            [_error("path_escape", f"{field_name} 不允许绝对路径或盘符：{value!r}", key=field_name)]
        )
    segments = [part for part in raw.split("/") if part not in ("", ".")]
    if not segments or any(part == ".." for part in segments):
        raise ReleaseGuardConfigError(
            [_error("path_escape", f"{field_name} 不能为空或含 .. 越界：{value!r}", key=field_name)]
        )
    normalized = "/".join(segments)
    return normalized + "/" if raw.endswith("/") else normalized


def _str_tuple(value: object, field_name: str, *, allow_flags: bool) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ReleaseGuardConfigError(
            [_error("invalid_type", f"{field_name} 必须是字符串数组", key=field_name)]
        )
    items: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ReleaseGuardConfigError(
                [
                    _error(
                        "invalid_type",
                        f"{field_name} 的元素必须是非空字符串，实际为 {raw!r}",
                        key=field_name,
                    )
                ]
            )
        text = raw.strip()
        if not allow_flags and text.startswith("-"):
            raise ReleaseGuardConfigError(
                [
                    _error(
                        "invalid_path",
                        f"{field_name} 是路径清单，不接受选项：{text!r}",
                        key=field_name,
                    )
                ]
            )
        items.append(text)
    return tuple(items)


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseGuardConfigError(
            [_error("invalid_type", f"{field_name} 必须是 JSON 对象", key=field_name)]
        )
    return value


def _parse_check(raw: object, base: CheckSpec) -> CheckSpec:
    data = _mapping(raw, f"check.{base.name}")
    unknown = sorted(set(data) - _KNOWN_CHECK_KEYS)
    if unknown:
        raise ReleaseGuardConfigError(
            [
                _error(
                    "unknown_key",
                    f"check.{base.name} 存在未知配置键：{', '.join(unknown)}",
                    key=f"check.{base.name}.{unknown[0]}",
                )
            ]
        )
    module = data.get("module", base.module)
    if not isinstance(module, str) or not module.strip():
        raise ReleaseGuardConfigError(
            [
                _error(
                    "invalid_type",
                    f"check.{base.name}.module 必须是非空字符串，实际为 {module!r}",
                    key=f"check.{base.name}.module",
                )
            ]
        )
    args = (
        _str_tuple(data["args"], f"check.{base.name}.args", allow_flags=True)
        if "args" in data
        else base.args
    )
    if "args" in data and "paths" not in data:
        # 改 args 就必须同时声明 paths：否则「哪些参数是要判存在的目标路径」只能靠猜，
        # 漏判就会把不存在的路径交给子进程报 ENOENT（AC1 的实质破口）。
        raise ReleaseGuardConfigError(
            [
                _error(
                    "paths_required",
                    f"check.{base.name} 覆盖 args 时必须同时声明 paths"
                    "（paths 为 args 中须判存在性的目标路径子集）",
                    key=f"check.{base.name}.paths",
                )
            ]
        )
    paths = (
        tuple(
            _rel_path(item, f"check.{base.name}.paths")
            for item in _str_tuple(data["paths"], f"check.{base.name}.paths", allow_flags=False)
        )
        if "paths" in data
        else base.paths
    )
    if not paths:
        raise ReleaseGuardConfigError(
            [
                _error(
                    "invalid_paths",
                    f"check.{base.name}.paths 不得为空（空表等于关闭存在性判据）",
                    key=f"check.{base.name}.paths",
                )
            ]
        )
    declared = {item.rstrip("/") for item in paths}
    stray = [item for item in paths if item.rstrip("/") not in {a.rstrip("/") for a in args}]
    if stray:
        raise ReleaseGuardConfigError(
            [
                _error(
                    "paths_not_in_args",
                    f"check.{base.name}.paths 含未出现在 args 里的路径：{', '.join(stray)}",
                    key=f"check.{base.name}.paths",
                )
            ]
        )
    # 反向校验：args 里**未声明**的路径类 token 会绕过存在性判据直接进子进程
    # （正是 ENOENT 的来源）。子命令白名单 = 引擎默认 args 中不属于目标路径的裸 token。
    base_paths = {item.rstrip("/") for item in base.paths}
    subcommands = {
        token for token in base.args
        if not token.startswith("-") and token.rstrip("/") not in base_paths
    }
    shadowed = sorted(declared & subcommands)
    if shadowed:
        # 路径名与子命令同名时，剔除会连子命令一起删掉（命令变成非法调用）。
        raise ReleaseGuardConfigError(
            [
                _error(
                    "path_shadows_subcommand",
                    f"check.{base.name}.paths 与子命令同名：{', '.join(shadowed)}"
                    "（剔除时无法区分，请改名或换目标路径）",
                    key=f"check.{base.name}.paths",
                )
            ]
        )
    # 位置参数判定（与 argv 语义一致）：非选项 token **且不紧跟选项**才算目标路径；
    # 紧跟选项的既可能是选项值（`--select E501`）也可能是路径（`--strict gone/`），
    # argv 上不可区分，故这里放行、由计划期 ambiguous_option_value / ambiguous_undeclared_path
    # 兜底降级（见 `orchd.guards.release.build_check_plan`），既不猜 arity 也不照旧下发。
    # 明确的位置参数（前一 token 不是选项）未声明进 paths ⇒ 一律拒绝。
    undeclared: list[str] = []
    for index, token in enumerate(args):
        if token.startswith("-"):
            continue
        if index and args[index - 1].startswith("-"):
            continue
        if token.rstrip("/") in declared or token.rstrip("/") in subcommands:
            continue
        undeclared.append(token)
    if undeclared:
        raise ReleaseGuardConfigError(
            [
                _error(
                    "path_not_declared",
                    f"check.{base.name}.args 里的位置参数未声明进 paths：{', '.join(undeclared)}"
                    "（未声明的路径不会被剔除，会直接把不存在的路径交给子进程）",
                    key=f"check.{base.name}.paths",
                )
            ]
        )
    return CheckSpec(
        name=base.name,
        module=module.strip(),
        args=args,
        paths=paths,
        subcommands=tuple(sorted(subcommands)),
    )


def _parse_checks(raw: object) -> tuple[CheckSpec, ...]:
    data = _mapping(raw, "check")
    unknown = sorted(set(data) - {spec.name for spec in CHECK_SPECS})
    if unknown:
        raise ReleaseGuardConfigError(
            [
                _error(
                    "unknown_check",
                    f"check 存在未登记的检查项：{', '.join(unknown)}",
                    key=f"check.{unknown[0]}",
                )
            ]
        )
    bases = {spec.name: spec for spec in CHECK_SPECS}
    overrides = {name: _parse_check(value, bases[name]) for name, value in data.items()}
    return tuple(overrides.get(spec.name, spec) for spec in CHECK_SPECS)


@dataclass(frozen=True)
class ReleasePolicy:
    """宿主发版/静态门禁策略（缺省即引擎现值）。"""

    checks: tuple[CheckSpec, ...] = CHECK_SPECS
    baseline_path: str = DEFAULT_BASELINE_PATH
    pytest_args: tuple[str, ...] = DEFAULT_PYTEST_ARGS
    baseline_targets: tuple[str, ...] = DEFAULT_BASELINE_TARGETS
    entry_script: str = DEFAULT_ENTRY_SCRIPT
    ready_script: str = DEFAULT_READY_SCRIPT
    baseline_script: str = DEFAULT_BASELINE_SCRIPT
    milestone: Mapping[str, Any] = field(default_factory=dict)


def parse_policy(options: object) -> ReleasePolicy:
    """把 ``guards.json`` 的 release options 解析为 :class:`ReleasePolicy`。

    Raises:
        ReleaseGuardConfigError: 未知键 / 类型不符 / 路径越界（带结构化错误项）。
    """
    if options is None:
        return ReleasePolicy()
    data = _mapping(options, "release")
    unknown = sorted(set(data) - _KNOWN_TOP_KEYS)
    if unknown:
        raise ReleaseGuardConfigError(
            [
                _error(
                    "unknown_key",
                    f"release 存在未知配置键：{', '.join(unknown)}",
                    key=f"release.{unknown[0]}",
                )
            ]
        )
    checks = _parse_checks(data["check"]) if "check" in data else CHECK_SPECS

    baseline_path = DEFAULT_BASELINE_PATH
    pytest_args = DEFAULT_PYTEST_ARGS
    baseline_targets = DEFAULT_BASELINE_TARGETS
    if "baseline" in data:
        baseline = _mapping(data["baseline"], "baseline")
        bad = sorted(set(baseline) - _KNOWN_BASELINE_KEYS)
        if bad:
            raise ReleaseGuardConfigError(
                [
                    _error(
                        "unknown_key",
                        f"baseline 存在未知配置键：{', '.join(bad)}",
                        key=f"baseline.{bad[0]}",
                    )
                ]
            )
        if "path" in baseline:
            baseline_path = _rel_path(baseline["path"], "baseline.path")
        if "pytest_args" in baseline:
            pytest_args = _str_tuple(
                baseline["pytest_args"], "baseline.pytest_args", allow_flags=True
            )
        if "targets" in baseline:
            baseline_targets = tuple(
                _rel_path(item, "baseline.targets")
                for item in _str_tuple(
                    baseline["targets"], "baseline.targets", allow_flags=False
                )
            )
            if not baseline_targets:
                raise ReleaseGuardConfigError(
                    [
                        _error(
                            "invalid_paths",
                            "baseline.targets 不得为空（空表等于关闭目标路径判据）",
                            key="baseline.targets",
                        )
                    ]
                )

    arg_tokens = {item.rstrip("/") for item in pytest_args}
    stray_targets = [
        item for item in baseline_targets if item.rstrip("/") not in arg_tokens
    ]
    if stray_targets:
        # targets 是「判存在性」的路径子集，必须在 pytest_args 里真出现；否则判据会盯一条
        # 永远不会被跑的路径（判据与命令脱节）。
        raise ReleaseGuardConfigError(
            [
                _error(
                    "targets_not_in_args",
                    f"baseline.targets 含未出现在 pytest_args 里的路径："
                    f"{', '.join(stray_targets)}",
                    key="baseline.targets",
                )
            ]
        )

    scripts = {
        "entry": DEFAULT_ENTRY_SCRIPT,
        "release_ready": DEFAULT_READY_SCRIPT,
        "baseline": DEFAULT_BASELINE_SCRIPT,
    }
    if "scripts" in data:
        raw_scripts = _mapping(data["scripts"], "scripts")
        bad = sorted(set(raw_scripts) - _KNOWN_SCRIPT_KEYS)
        if bad:
            raise ReleaseGuardConfigError(
                [
                    _error(
                        "unknown_key",
                        f"scripts 存在未知配置键：{', '.join(bad)}",
                        key=f"scripts.{bad[0]}",
                    )
                ]
            )
        for role, value in raw_scripts.items():
            scripts[role] = _rel_path(value, f"scripts.{role}")

    milestone: Mapping[str, Any] = {}
    if "milestone" in data:
        milestone = dict(_mapping(data["milestone"], "milestone"))

    return ReleasePolicy(
        checks=checks,
        baseline_path=baseline_path,
        pytest_args=pytest_args,
        baseline_targets=baseline_targets,
        entry_script=scripts["entry"],
        ready_script=scripts["release_ready"],
        baseline_script=scripts["baseline"],
        milestone=milestone,
    )


def load_release_policy(project_root: str | Path) -> ReleasePolicy:
    """读取宿主配置并解析为 :class:`ReleasePolicy`（无配置即全默认）。

    Raises:
        ReleaseGuardConfigError: guards.json 非法或 release 段非法（结构化错误）。
    """
    try:
        config = load_guards_config(project_root)
    except GuardsConfigError as exc:
        raise ReleaseGuardConfigError(
            [dict(item) for item in exc.errors] or [_error("invalid_config", str(exc))]
        ) from exc
    entry = next((item for item in config.entries if item.name == "release"), None)
    return parse_policy(entry.options if entry is not None else None)
