"""宿主发版 guard 执行层：静态门禁计划与 done 期只读评估。

配置解析与校验在 `orchd.guards.release_policy`；本模块只做两件事：

1. 策略 + 磁盘现状 → 可执行计划（`build_check_plan`）：宿主仓里不存在的目标路径从参数中
   剔除；整项无剩余路径即降级 `missing_paths`（不再把不存在的路径交给子进程报 ENOENT /
   "No such file"）；缺失路径若是某个选项的值（`--exclude src/`）整项降级
   `ambiguous_option_value`（不留悬空 flag）；未声明且缺失的路径类 token
   （`--strict gone/`、`--config=gone/x.toml`）与选项值在 argv 上不可区分，整项降级
   `ambiguous_undeclared_path`（不猜 arity）。
2. 策略 + 磁盘现状 → 结构化 finding（`evaluate`）：缺路径 / 缺脚本逐条点名，配置非法转
   `RuntimeError` 由 done 期降级留痕，不抛裸异常。

本模块只读：不写盘、不起子进程。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.guards import GuardEvalContext, GuardFinding
from orchd.guards.release_policy import (
    CHECK_SPECS,
    DEFAULT_BASELINE_PATH,
    DEFAULT_BASELINE_SCRIPT,
    DEFAULT_BASELINE_TARGETS,
    DEFAULT_CHECKS,
    DEFAULT_ENTRY_SCRIPT,
    DEFAULT_PYTEST_ARGS,
    DEFAULT_READY_SCRIPT,
    CheckSpec,
    ReleaseGuardConfigError,
    ReleasePolicy,
    load_release_policy,
    parse_policy,
)

__all__ = (
    "CHECK_SPECS",
    "DEFAULT_BASELINE_PATH",
    "DEFAULT_BASELINE_SCRIPT",
    "DEFAULT_BASELINE_TARGETS",
    "DEFAULT_CHECKS",
    "DEFAULT_ENTRY_SCRIPT",
    "DEFAULT_PYTEST_ARGS",
    "DEFAULT_READY_SCRIPT",
    "CheckPlan",
    "CheckSpec",
    "ReleaseGuardConfigError",
    "ReleasePolicy",
    "SkippedCheck",
    "build_check_plan",
    "evaluate",
    "load_release_policy",
    "missing_scripts",
    "parse_policy",
    "register",
)


@dataclass(frozen=True)
class SkippedCheck:
    """一项被降级为 not_applicable 的检查（结构化理由，供 JSON 直出）。"""

    name: str
    reason: str
    paths: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """转成可进 CLI 响应的纯 dict。"""
        payload: dict[str, Any] = {"name": self.name, "reason": self.reason}
        if self.paths:
            payload["paths"] = list(self.paths)
        return payload


@dataclass(frozen=True)
class CheckPlan:
    """一次静态门禁的执行计划：可执行命令 + 降级项。"""

    commands: Mapping[str, tuple[str, ...]]
    skipped: tuple[SkippedCheck, ...] = ()

    @property
    def executable(self) -> bool:
        """是否至少有一项可执行检查。"""
        return bool(self.commands)


def _drop_args(args: tuple[str, ...], dropped: tuple[str, ...]) -> tuple[str, ...]:
    """从参数列表里剔除缺失的目标路径（**双侧**去尾斜杠后比较）。

    只做半边归一会出现「paths:["src"] + args:["src/"] 声明被接受、剔除却匹不上」的漏洞，
    结果仍是把不存在的路径交给子进程（AC1 的原始症状）。
    """
    targets = {item.rstrip("/") for item in dropped}
    return tuple(arg for arg in args if arg.rstrip("/") not in targets)


def build_check_plan(project_root: str | Path, policy: ReleasePolicy) -> CheckPlan:
    """按宿主策略与磁盘现状构建静态门禁计划（纯只读，不 exec）。

    宿主仓里不存在的目标路径直接剔除；三种不可安全执行的情形整项降级为
    :class:`SkippedCheck`：全缺（``missing_paths``）、缺失路径是选项值
    （``ambiguous_option_value``）、未声明且缺失的路径类 token 与内联 ``--opt=missing``
    （``ambiguous_undeclared_path``）。都不再把不存在的路径交给子进程。
    """
    root = Path(project_root)
    commands: dict[str, tuple[str, ...]] = {}
    skipped: list[SkippedCheck] = []
    for spec in policy.checks:
        missing = tuple(item for item in spec.paths if not (root / item).exists())
        if len(missing) == len(spec.paths):
            skipped.append(SkippedCheck(name=spec.name, reason="missing_paths", paths=missing))
            continue
        ambiguous = _option_values(spec.args, missing)
        if ambiguous:
            # 缺失的「路径」其实是某个选项的值（如 --exclude src）：删掉会留下悬空 flag，
            # 保留会把缺失路径交给子进程 —— 整项降级并给可读理由。
            skipped.append(
                SkippedCheck(
                    name=spec.name, reason="ambiguous_option_value", paths=ambiguous
                )
            )
            continue
        undeclared = _undeclared_missing_paths(spec, root)
        if undeclared:
            # 布尔选项后的未声明路径（--strict gone/）与选项值在 argv 上不可区分：
            # 含路径分隔符且磁盘不存在的 token 一律判为不可判定 → 整项降级，不猜 arity。
            skipped.append(
                SkippedCheck(
                    name=spec.name, reason="ambiguous_undeclared_path", paths=undeclared
                )
            )
            continue
        kept = _drop_args(spec.args, missing)
        commands[spec.name] = tuple(spec.python_args(kept))
    return CheckPlan(commands=commands, skipped=tuple(skipped))


def _is_missing_path_like(value: str, declared: set[str], root: Path) -> bool:
    """「含路径分隔符 + 未声明 + 磁盘不存在」⇒ 不可判定的路径类值。"""
    if "/" not in value and "\\" not in value:
        return False
    if value.rstrip("/") in declared:
        return False
    return not (root / value).exists()


def _undeclared_missing_paths(spec: CheckSpec, root: Path) -> tuple[str, ...]:
    """返回「未声明、磁盘不存在、且含路径分隔符」的 token（含 ``--opt=value`` 内联值）。

    这类 token 与「布尔选项值」在 argv 上不可区分（``--strict gone/``）：既不猜 arity 也不
    照旧下发，一律整项降级（``ambiguous_undeclared_path``）。``E501`` / ``3.11`` 这类选项值
    不含路径分隔符，不受影响。
    """
    declared = {item.rstrip("/") for item in spec.paths}
    allowed = {item.rstrip("/") for item in spec.subcommands}
    found: list[str] = []
    for arg in spec.args:
        if arg.startswith("-"):
            # 内联形态 --opt=value：值部分无法单独剔除，同样只能整项降级。
            inline = arg.split("=", 1)[1] if "=" in arg else ""
            if inline and _is_missing_path_like(inline, declared, root):
                found.append(arg)
            continue
        if arg.rstrip("/") in declared or arg.rstrip("/") in allowed:
            continue
        if _is_missing_path_like(arg, declared, root):
            found.append(arg)
    return tuple(found)


def _option_values(args: tuple[str, ...], paths: tuple[str, ...]) -> tuple[str, ...]:
    """返回「紧跟选项且命中缺失路径」的 token（这些 token 是选项值，不能单独剔除）。"""
    wanted = {item.rstrip("/") for item in paths}
    return tuple(
        arg
        for index, arg in enumerate(args)
        if index
        and not arg.startswith("-")
        and args[index - 1].startswith("-")
        and arg.rstrip("/") in wanted
    )


def missing_scripts(
    project_root: str | Path, policy: ReleasePolicy
) -> tuple[tuple[str, str], ...]:
    """返回 ``(角色, 相对路径)`` 形式的缺失脚本清单（角色 ∈ entry/ready/baseline）。"""
    root = Path(project_root)
    roles = (
        ("entry", policy.entry_script),
        ("ready", policy.ready_script),
        ("baseline", policy.baseline_script),
    )
    return tuple((role, rel) for role, rel in roles if not (root / rel).is_file())


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """报告宿主配置里指向不存在路径的项（只读、零子进程）。

    配置非法 → ``RuntimeError``（done 期按降级留痕，不抛裸异常）。缺失项不是「配置错」
    而是「宿主仓不具备该检查面」，故以 finding 形式给出，由 warn / hard 档决定阻断。
    """
    if context.project_root is None:
        return ()
    try:
        policy = load_release_policy(context.project_root)
    except ReleaseGuardConfigError as exc:
        raise RuntimeError(f"release guard 配置非法：{exc}") from exc
    root = Path(context.project_root)
    findings: list[GuardFinding] = []
    for spec in policy.checks:
        for item in spec.paths:
            if not (root / item).exists():
                findings.append(
                    GuardFinding(
                        guard="release",
                        message=f"静态门禁目标路径不存在：{spec.name} -> {item}",
                        context={"kind": "missing_path", "check": spec.name, "path": item},
                    )
                )
    for role, rel in missing_scripts(root, policy):
        findings.append(
            GuardFinding(
                guard="release",
                message=f"发版脚本不存在：{role} -> {rel}",
                context={"kind": "missing_script", "role": role, "path": rel},
            )
        )
    return tuple(findings)


def register() -> None:
    """把本评估器登记到 guards 注册表（幂等）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator("release", evaluate)


register()
