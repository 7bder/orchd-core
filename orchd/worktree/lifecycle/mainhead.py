"""lifecycle.mainhead —— 自 ``orchd/worktree/lifecycle.py`` 拆出。

主工作树 HEAD 读取、守护与还原。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchd.worktree.layout import _GIT_TIMEOUT

def _read_main_worktree_head(project_root: Path) -> str | None:
    """读取主工作树 ``.git/HEAD`` 原文（best-effort）。

    AC4（HEAD 落点加固）的对照组采集：仅当 project_root 确为主工作树
    （``.git`` 是目录且含 HEAD 文件）时返回内容；linked worktree（``.git`` 为
    gitfile）/ 读取异常 → None（守卫自动失效，不误报）。
    """
    try:
        head = Path(project_root) / ".git" / "HEAD"
        if head.is_file():
            return head.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _check_main_head_unchanged(project_root: Path, before: str | None) -> str | None:
    """比对主工作树 HEAD 是否被改写；未漂移返回 None，漂移返回原因串。"""
    if not before:
        return None
    after = _read_main_worktree_head(project_root)
    if after is None or after == before:
        return None
    return (
        "HEAD 落点异常：git worktree add 改写了主工作树 .git/HEAD"
        f"（{before.strip()} → {after.strip()}）；已回滚本次 worktree 创建"
        "（任务 worktree 只写 .git/worktrees/<name>/HEAD）"
    )


def _restore_main_head(project_root: Path, before: str) -> bool:
    """best-effort 还原主工作树 HEAD（原文须为 symbolic ref，否则不动作）。"""
    ref = before.strip()
    if not ref.startswith("ref: "):
        return False
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "HEAD", ref[len("ref: "):]],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
