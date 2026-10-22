"""lifecycle.suppress —— 自 ``orchd/worktree/lifecycle.py`` 拆出。

任务 worktree 内的 master 副本抑制（_MASTER_IGNORE_PATTERNS / _suppress_task_master_copy）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .engine import _git_minor_version

# _master.json 副本抑制（task-master-single-copy）：container 任务 worktree 不再
# 保留 .orchd/_master.json，唯一权威 = 主工作树。用 git sparse-checkout --no-cone
# 仅排除该文件（.orchd/ 其余文件与全仓库仍正常检出）。4 条 pattern 已实证：
# worktree 内文件不存在、git status 干净、git add -A/commit 不记录删除、主工作树
# 前进后 merge 不复活（skip-worktree 方案 merge 时会复活，故为主方案）。
_MASTER_IGNORE_PATTERNS = ["/*", "!/.orchd/", "/.orchd/*", "!/.orchd/_master.json"]


def _suppress_task_master_copy(wt_path: Path) -> dict[str, Any]:
    """抑制任务 worktree 内 ``.orchd/_master.json`` 副本（best-effort，降级可审计）。

    必须经 Python ``subprocess.run`` 以参数列表直传（``shell=False``），避免
    Git for Windows 的 MSYS 路径转换改写 ``!/...`` pattern（``!/.orchd/`` 等
    会被当作参数做路径归一化，导致 pattern 失效）。

    Returns:
        ``{"ok": bool, "method": str, "reason": str|None}``；``method`` ∈
        ``sparse-checkout`` / ``skip-worktree`` / ``none``。sparse 不可用
        （git < 2.25 或失败）→ 降级 skip-worktree；再失败 → 保留副本 + 告警
        （``ok=False, method="none"``，由调用方以 degraded 透出）。
    """
    version = _git_minor_version()
    if version >= 225:  # W-14：口径 major*100+minor（2.25 → 225；git 3.0 → 300）
        init = subprocess.run(
            ["git", "sparse-checkout", "init", "--no-cone", str(wt_path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(wt_path),
        )
        # 陈旧 sparse 状态：残余 in-cone 标记/禁用；先 reset（--no-cone 下不保留
        # 先前 pattern），保证 set 前为纯 no-cone 基线。
        if init.returncode == 0:
            setp = subprocess.run(
                ["git", "sparse-checkout", "set", *_MASTER_IGNORE_PATTERNS],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                cwd=str(wt_path),
            )
            if setp.returncode == 0:
                # 收敛校验：明确的抑制目标文件应已从 worktree 消失。
                if not (wt_path / ".orchd" / "_master.json").exists():
                    return {"ok": True, "method": "sparse-checkout", "reason": None}
                return {
                    "ok": False, "method": "none",
                    "reason": "sparse-checkout set 成功但 .orchd/_master.json 仍存在",
                }
            reason = (setp.stderr or "").strip()[:200]
            return {
                "ok": False, "method": "none",
                "reason": f"sparse-checkout set 失败: {reason or 'exit' + str(setp.returncode)}",
            }
    # 降级：skip-worktree（merge 时副本可能复活，仍优于保留双副本）
    sw = subprocess.run(
        ["git", "update-index", "--skip-worktree", ".orchd/_master.json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        cwd=str(wt_path),
    )
    if sw.returncode == 0:
        return {
            "ok": True, "method": "skip-worktree",
            "reason": "sparse-checkout 不可用，回退 skip-worktree（merge 时副本可能复活）",
        }
    return {
        "ok": False, "method": "none",
        "reason": "sparse-checkout / skip-worktree 均不可用，保留副本（双副本漂移风险）",
    }


def _master_suppression_entry(wt_path: Path) -> dict[str, Any]:
    """生成 ``ensure_task_wt`` 返回字典中的 ``master_suppression`` 段。"""
    supp = _suppress_task_master_copy(wt_path)
    # 抑制失败 → 副本残留，随 claim 透出 degraded 供 agent 可见（禁止静默）
    return {
        "master_suppression": supp,
        "degraded": supp.get("ok") is False,
        "degraded_reason": (
            f"master 副本抑制失败（{supp.get('method')}）：{supp.get('reason')}"
            if supp.get("ok") is False else None
        ),
    }
