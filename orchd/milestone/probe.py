"""只读探针：git HEAD / sha 覆盖 / 状态计数 / JSON 读取 / 模块 spec / CLI 命令名 / 性能预算值。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import importlib.util
import json
import re
import subprocess

from .constants import PERF_BUDGET_CONST, PERF_BUDGET_TEST
from .context import MilestoneContext


def _git_head_short(project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root), capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _sha_covers(head: str, recorded: str) -> bool:
    """短 sha 双向前缀比较（集中一处的宽松口径，供本地记录点与 CI 记录点共用）。"""
    if not head or not recorded:
        return False
    return recorded.startswith(head[:7]) or head.startswith(recorded[:7])


def _status_counts(ctx: MilestoneContext) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ts in (ctx.state or {}).values():
        status = getattr(ts, "status", None) or "unknown"
        counts[str(status)] = counts.get(str(status), 0) + 1
    return counts


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _module_spec(name: str) -> Any:
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def _cli_command_names() -> set[str]:
    """已注册的 CLI 子命令名集合（走 parser 发现，与契约测试同口径）。"""
    try:
        from orchd.cli.parser import _build_parser

        parser = _build_parser()
    except Exception:
        return set()
    groups = [
        a for a in getattr(parser, "_subparsers", None)._group_actions  # type: ignore[union-attr]
        if hasattr(a, "choices") and a.choices
    ]
    if not groups:
        return set()
    return set(groups[0].choices)


def _perf_budget_value(root: Path) -> int | None:
    """解析 ``tests/test_perf_budget.py`` 的 git 子进程预算上限（冻结口径真源）。"""
    path = root / PERF_BUDGET_TEST
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(rf"^{PERF_BUDGET_CONST}\s*=\s*(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else None
