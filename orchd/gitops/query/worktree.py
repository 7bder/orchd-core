"""工作树根与任务工作树判定（_has_linked_worktrees / main_worktree_root / is_task_worktree）。"""

from __future__ import annotations

from pathlib import Path
import subprocess

from orchd.gitops._const import _GIT_TIMEOUT
from .cache import _PROBE_CACHE, _probe_cache_key


def _has_linked_worktrees(project_root: Path) -> bool:
    """判定仓库是否处于多 worktree 并行场景（存在 linked worktrees）。

    ``git worktree list --porcelain`` 每个 worktree 块以 ``worktree <path>`` 行开头；
    计数 >1 即存在 linked worktree（主 worktree + 至少一个 linked）。

    W-16（2026-09-15）：改按**行前缀**匹配，不再用子串 ``stdout.count("worktree ")``
    ——``locked <reason>`` / ``prunable <reason>`` 行的 reason 文本、或登记路径本身
    含 ``worktree `` 子串时，旧实现会把块数算多，从而在单 worktree 场景误判为多
    worktree（进而走 merge-wt 相关分支）。

    单 worktree（默认）场景返回 False——不创建 merge-wt，保持既有行为零回归
    （checkout_default_strict 仍可在 agent worktree 内切回 main）。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            return False
        blocks = [
            ln for ln in (proc.stdout or "").splitlines()
            if ln.startswith("worktree ")
        ]
        return len(blocks) > 1
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def main_worktree_root(project_root: Path) -> Path:
    """从任意 worktree 定位主工作树根（task-14-merge-main-tree，AC1）。

    ``git rev-parse --git-common-dir`` 返回公共 git 目录：主 worktree 返回
    ``<根>/.git``，linked worktree（任务 worktree）返回同一主 ``.git``。
    取其父目录即主工作树根（merge 在主工作树内执行、永不切任务 worktree 的 main）。
    非 git / 解析失败回退 ``project_root``（best-effort，flat 单会话零回归）。

    按次缓存：同键重复调用零 spawn（common-dir 由 .git 指针派生，指针不变则
    答案不变；指针变化 → 键变化 → 自动 miss）。确定性失败（非零退出/空输出）
    同样缓存；抛异常路径永不缓存。
    """
    key = _probe_cache_key(project_root)
    ckey = ("main_root", key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            value = project_root
        else:
            git_dir = proc.stdout.strip()
            if not git_dir:
                value = project_root
            else:
                p = Path(git_dir)
                if not p.is_absolute():
                    p = (project_root / p).resolve()
                else:
                    p = p.resolve()
                value = p.parent if p.name == ".git" else project_root
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return project_root


def is_task_worktree(project_root: Path) -> bool:
    """判定当前目录是否为 linked worktree（任务 worktree，task-14-merge-main-tree AC3）。

    ``git rev-parse --git-dir``：linked worktree 返回 ``<common>/.git/worktrees/<name>``
    （含 ``worktrees/`` 段），主 worktree 返回 ``<根>/.git``。含 ``worktrees/`` → True。
    非 git / 解析失败返回 False（best-effort）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return False
        return "worktrees/" in (proc.stdout.strip() or "")
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
