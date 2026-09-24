"""orchd CLI 路由：check 命令 handler（task-check-command）。

  - _cmd_check: 静态门禁（ruff + mypy，与 .githooks/pre-push R-6 同口径）

存在意义（pass7 P1-3 残余）：mypy 已在 pre-push 强制，但「未 push 的本地类型漂移」
无人可见。本命令使静态门禁可**显式主动**跑（批次收尾检查点 / 本地日常），
从而在 push 之前暴露类型红；且不污染 ``full-regression`` 的 pytest-only 契约。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

# 静态门禁清单（单一真源；口径与 .githooks/pre-push R-6 一致）
DEFAULT_CHECKS: dict[str, list[str]] = {
    "ruff": ["-m", "ruff", "check", "orchd/", "scripts/", "tests/", "release/"],
    "mypy": ["-m", "mypy", "orchd/"],
}


def run_check(
    project_root: Path,
    checks: dict[str, list[str]] | None = None,
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


def _cmd_check(args) -> tuple[dict, int]:
    """orchd check：跑静态门禁；全过 exit 0，否则非零。"""
    from orchd.cli._util import _find_orchd_dir

    orchd_dir = _find_orchd_dir()
    result = run_check(Path(orchd_dir).parent)
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
