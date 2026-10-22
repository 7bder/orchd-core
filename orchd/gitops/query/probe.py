"""git 仓库就绪探测（_probe_git_repo_ready），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import shutil
import subprocess

import orchd.gitops as _gitops_pkg


def _probe_git_repo_ready(project_root: Path) -> dict[str, Any] | None:
    """探测 git 可用 + 仓库状态；返回 None 表示通过，否则返回结构化失败结果。"""
    if shutil.which("git") is None:
        return {"performed": False, "reason": "git_unavailable"}
    try:
        check = _gitops_pkg._run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {"performed": False, "reason": "not_a_git_repo"}
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "not_a_git_repo"}
    return None
