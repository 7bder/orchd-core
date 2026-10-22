"""store root / worktree 名 / lock 与 gate 路径解析（叶，除自身互相依赖）。"""

from __future__ import annotations

from pathlib import Path
import os
import subprocess

from orchd.gitops._const import _GIT_TIMEOUT
from .constants import _SESSION_GATE_FILENAME, _SESSION_LOCK_FILENAME


def _resolve_store_root(orchd_dir: Path) -> Path:
    """解析会话锁的账本根（与 ``orchd.ledger.resolve_store_dir`` 组织语义完全一致）。

    惰性委托 ``orchd.ledger.resolve_store_dir``（单一来源）：
    - ``ORCHD_HOME`` 设置时重定向到外部目录（多 worktree 共享账本根）；
    - container 布局（``.orchd/.layout.json``）→ ``<容器>/.orchd-runtime``；
    - flat 场景回退 ``orchd_dir``（零回归）。

    task-session-lock-lifecycle（改 A）：此前只认 ``ORCHD_HOME``、不读 container
    布局标记，导致 container 下会话锁落进 ``main/.orchd/.session.lock`` 而非与
    Store 锁同根（``.orchd-runtime/``），两把互斥锁根不一致。惰性导入避免循环
    依赖（``orchd.ledger`` 模块级仅引用 ``orchd.errors``）。
    """
    from orchd.ledger import resolve_store_dir

    return resolve_store_dir(orchd_dir)


def _git_worktree_name(orchd_dir: Path) -> str | None:
    """解析当前 worktree 的 git 名称（``git worktree`` 场景）。

    主 worktree / 非 git / git 不可用时返回 ``None``（会话锁回退主维度）。
    ``git rev-parse --git-dir``：linked worktree 返回 ``<common>/.git/worktrees/<name>``
    （含 ``worktrees/`` 段），主 worktree 返回 ``<root>/.git``（或无后缀）。
    取 ``worktrees/`` 之后的段作为 worktree 维度名；解析失败静默降级。
    """
    project_root = orchd_dir.parent
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
            return None
        git_dir = proc.stdout.strip()
        marker = "worktrees/"
        if marker not in git_dir:
            return None
        name = git_dir.rsplit(marker, 1)[-1].split(os.sep)[0].strip()
        return name or None
    except (OSError, subprocess.SubprocessError):
        return None


def _get_session_lock_path(orchd_dir: Path) -> Path:
    """返回 session lock 文件路径（worktree 维度唯一）。

    - 账本根解析遵循 ``ORCHD_HOME`` 重定向（多 worktree 共享同一账本根）；
    - worktree 维度后缀：git linked worktree 场景解析出 worktree 名时，
      锁文件按 ``.session-<worktree>`` 命名，不同 worktree 可分别持有锁不互踩；
      主 worktree / 非 git / 解析失败回退 ``.session.lock``（默认单 worktree 场景，
      worktree 维度唯一即全局唯一，行为与改造前一致）。
    """
    base = _resolve_store_root(orchd_dir) / _SESSION_LOCK_FILENAME
    wt = _git_worktree_name(orchd_dir)
    if wt:
        base = _resolve_store_root(orchd_dir) / f".session-{wt}.lock"
    return base


def _get_session_gate_path(orchd_dir: Path) -> Path:
    """返回 session 门锁文件路径（与 session lock 同根，worktree 维度唯一）。

    门锁为 flock gate，用于串行化 :func:`ensure_session_lock` 的"检查+写入"；
    与 session lock 文件（标记文件）分离，避免"标记文件被删除导致门锁失效"。
    """
    base = _resolve_store_root(orchd_dir) / _SESSION_GATE_FILENAME
    wt = _git_worktree_name(orchd_dir)
    if wt:
        base = _resolve_store_root(orchd_dir) / f".session-gate-{wt}.lock"
    return base
