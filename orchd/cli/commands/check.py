"""orchd CLI 路由：check 命令 handler（task-check-command）。

  - _cmd_check: 静态门禁（ruff + mypy，与 .githooks/pre-push R-6 同口径）

存在意义（pass7 P1-3 残余）：mypy 已在 pre-push 强制，但「未 push 的本地类型漂移」
无人可见。本命令使静态门禁可**显式主动**跑（批次收尾检查点 / 本地日常），
从而在 push 之前暴露类型红；且不污染 ``full-regression`` 的 pytest-only 契约。

宿主化（task-host-guard-release）：检查清单改由 ``orchd/guards/release.py`` 的
:data:`~orchd.guards.release.DEFAULT_CHECKS` 派生，宿主可在 ``guards.json`` 的
``release.check`` 段注入 tool 模块与目标路径；目标路径不存在 ⇒ 该检查降级为
``not_applicable``（结构化理由），不再把不存在的路径交给子进程去报 ENOENT。
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orchd.guards.release import (
    DEFAULT_CHECKS,
    ReleaseGuardConfigError,
    build_check_plan,
    load_release_policy,
)

__all__ = ("DEFAULT_CHECKS", "check_payload", "run_check")


def run_check(
    project_root: Path,
    checks: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """顺序执行静态门禁；返回结构化结果（不抛异常）。

    Args:
        project_root: 仓库根（子进程 cwd）。
        checks: 检查清单（名称 → ``python`` 参数列表）；缺省 :data:`DEFAULT_CHECKS`。

    Returns:
        ``{"ok", "checks": {name: {"ok", "exit_code", "output_summary"?}}, "hint"}``。
    """
    checks = checks if checks is not None else DEFAULT_CHECKS
    results: dict[str, Any] = {}
    ok = True
    for name, args in checks.items():
        passed = False
        try:
            proc = subprocess.run(
                [sys.executable, *args],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            passed = proc.returncode == 0
            results[name] = {
                "ok": passed,
                "exit_code": proc.returncode,
                "output_summary": ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:],
            }
        except (subprocess.SubprocessError, OSError) as exc:
            results[name] = {"ok": False, "exit_code": None, "error": str(exc)}
        ok = ok and passed
    return {
        "ok": ok,
        "checks": results,
        "hint": (
            "静态门禁全绿（ruff + mypy）" if ok
            else "静态门禁未过：修复后重跑（口径与 .githooks/pre-push R-6 一致）"
        ),
    }


#: 降级原因 → 人话（三种病因不得混述，否则用户会被指向错误方向）。
_SKIP_REASON_TEXT = {
    "missing_paths": "目标路径全部缺失",
    "ambiguous_option_value": "缺失路径是选项值，无法安全剔除",
    "ambiguous_undeclared_path": "存在未声明且缺失的路径类 token（与选项值不可区分）",
}


def _skip_reasons(skipped: tuple) -> str:
    """把降级项渲染成「检查名: 病因」列表。"""
    return "、".join(
        f"{item.name}: {_SKIP_REASON_TEXT.get(item.reason, item.reason)}" for item in skipped
    )


def check_payload(project_root: Path) -> dict[str, Any]:
    """按宿主策略构建计划并执行静态门禁。

    无配置时与旧行为逐条一致；配置非法返回结构化 ``error``（不抛堆栈）；
    目标路径全缺的检查项进 ``not_applicable``（不进失败计数）。
    """
    try:
        plan = build_check_plan(project_root, load_release_policy(project_root))
    except ReleaseGuardConfigError as exc:
        return {
            "ok": False,
            "checks": {},
            "error": {
                "kind": "release_guard_config",
                "message": str(exc),
                "errors": [dict(item) for item in exc.errors],
            },
            "hint": "guards.json 的 release 段非法：修正后重跑（不静默放行）",
        }
    if not plan.executable:
        return {
            "ok": False,
            "checks": {},
            "status": "not_applicable",
            "not_applicable": [item.as_dict() for item in plan.skipped],
            "hint": (
                "无任何可执行静态检查（not_applicable）："
                f"{_skip_reasons(plan.skipped)}；不能把「什么都没查」当成通过"
            ),
        }
    result = run_check(project_root, plan.commands)
    if plan.skipped:
        result["not_applicable"] = [item.as_dict() for item in plan.skipped]
        result["hint"] = f"{result['hint']}；not_applicable：{_skip_reasons(plan.skipped)}"
    return result


def _cmd_check(args) -> tuple[dict, int]:
    """orchd check：跑静态门禁；全过 exit 0，否则非零。"""
    from orchd.cli._util import _find_orchd_dir

    orchd_dir = _find_orchd_dir()
    result = check_payload(Path(orchd_dir).parent)
    return result, (0 if result["ok"] else 1)


def register(sub) -> None:
    """注册 check 模块的子命令。"""
    p = sub.add_parser(
        "check",
        help=(
            "静态门禁：ruff（orchd/ scripts/ tests/ release/）"
            "+ mypy（orchd/），与 pre-push 同口径"
        ),
    )
    p.set_defaults(func=_cmd_check)
