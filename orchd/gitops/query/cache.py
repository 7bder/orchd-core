"""查询结果的内存缓存（_PROBE_CACHE / clear_git_probe_cache / _probe_cache_key），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 只读探测按次缓存（task-gitops-probe-cache）
# ---------------------------------------------------------------------------
# 背景：Windows 上单次 git spawn 约 40–75ms，而 get_current_branch /
# main_worktree_root / _git_toplevel 等只读探测在单条命令内被反复调用
# （实测单 CLI 用例 33 subprocess 中 ~28 次为此类探测）。
#
# 新鲜度（零 spawn 信号）：缓存键 =（归一化绝对路径 + .git 形态快照）。
# 任何可能改变答案的变更都会改变键 → 自动 miss，绝不 stale：
#   - 切分支/建分支 → HEAD 内容或 mtime 变化；
#   - git init → 父目录 mtime 变化（.git 由无到有）；
#   - worktree 增删别处 → 本 cwd 信号不变 → 答案本就不变，命中正确。
# 信号不可读（目录被删、权限异常）→ 不缓存（fail-safe：宁可多 spawn）。
# 跨命令无需手动失效：键变化即失效；长生命进程可调 clear_git_probe_cache()。
_PROBE_CACHE: dict[tuple, Any] = {}


def clear_git_probe_cache() -> None:
    """清空只读探测缓存（测试隔离 / 长生命进程手动失效）。"""
    _PROBE_CACHE.clear()


def _probe_cache_key(project_root: Path) -> tuple | None:
    """构造零 spawn 新鲜度键；不可判定返回 None（调用方直通 git）。"""
    import os as _os

    try:
        p = Path(project_root).resolve()
    except OSError:
        return None
    base = _os.path.normcase(str(p))
    dot = p / ".git"
    try:
        if not dot.exists() and not dot.is_symlink():
            try:
                return (base, "nage", p.stat().st_mtime_ns)
            except OSError:
                return None
        if dot.is_file():
            try:
                target = dot.read_text(
                    encoding="utf-8", errors="replace").partition(
                        "gitdir:")[2].strip()
            except OSError:
                return None
            if not target:
                return (base, "odd", None)
            head = Path(target) / "HEAD"
            try:
                return (base, "file", target,
                        head.read_text(encoding="utf-8", errors="replace"),
                        head.stat().st_mtime_ns)
            except OSError:
                return (base, "file", target, None, None)
        if dot.is_dir():
            head = dot / "HEAD"
            try:
                return (base, "dir",
                        head.read_text(encoding="utf-8", errors="replace"),
                        head.stat().st_mtime_ns)
            except OSError:
                return (base, "dir", None, None)
        return (base, "odd", None)
    except OSError:
        return None
