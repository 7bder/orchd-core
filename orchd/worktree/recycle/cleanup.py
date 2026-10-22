"""worktree recycle cleanup 域（W6-C5 纯搬移）。

来源：``orchd/worktree/recycle.py``（1023 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchd.worktree.bindings import (
    _TASK_WT_PREFIX, _task_wt_name, load_bindings, unbind_task_wt,
)
from orchd.worktree.layout import _GIT_TIMEOUT, detect_layout



def _cleanup_stale_session_locks(store_root: Path, task_wt_root: Path) -> list[str]:
    """清理 worktree 已不存在的会话锁残留（best-effort）。

    task-workspace-docs-isolation：任务 worktree 终态回收后，其会话锁标记
    （``.session-<wt>.lock`` / ``.session-gate-<wt>.lock``）残留在共享账本根。
    仅清理 worktree 名以 ``task-`` 开头且对应目录已不存在的锁；删除前探活
    flock（他人仍持活锁则跳过，防 flock-unlink 竞态，见
    ``gitops._probe_session_lock_os_active``）。主 worktree 锁
    （``.session.lock`` / ``.session.gate.lock``）不在匹配范围，不触碰。

    Returns:
        已清理的锁文件名清单。
    """
    from orchd.gitops import _probe_session_lock_os_active

    cleaned: list[str] = []
    try:
        for pattern in (".session-*.lock", ".session-gate-*.lock"):
            for p in sorted(store_root.glob(pattern)):
                name = p.name
                if pattern == ".session-*.lock" and name.startswith(".session-gate-"):
                    # review W-17：``.session-*.lock`` 通配也会命中门锁
                    # （``.session-gate-<wt>.lock``），门锁交由专用 pattern 处理，
                    # 避免同一文件在一轮回收里被访问两次。
                    continue
                if name.startswith(".session-gate-"):
                    wt = name[len(".session-gate-"):-len(".lock")]
                elif name.startswith(".session-"):
                    wt = name[len(".session-"):-len(".lock")]
                else:
                    continue
                if not wt.startswith(_TASK_WT_PREFIX):
                    continue  # 仅任务 worktree 维度锁；主 worktree / 其他锁不碰
                if (task_wt_root / wt).exists():
                    continue  # worktree 仍存在 → 活跃，跳过
                if _probe_session_lock_os_active(p).get("active"):
                    continue  # 他人仍持活锁 → 不删（flock-unlink 竞态）
                try:
                    p.unlink()
                    cleaned.append(name)
                except OSError:
                    pass
    except OSError:
        pass
    return cleaned



def _rmtree_force(path: Path) -> bool:
    """删除目录树；Windows 下先清只读属性再删（git 对象文件只读导致 rmtree 失败）。

    task-workspace-docs-isolation：测试残留（如 .pytest-tmp 内 git 仓库）对象文件
    带只读属性，``shutil.rmtree`` 在 Windows 上删除失败（PermissionError WinError 5）。
    onerror 回调清只读后重试单文件删除，其余错误静默跳过。

    Returns:
        ``True`` 目录已不存在（删除成功或本就不存在）。
    """
    from orchd.gitops import _os_delete_tree

    return _os_delete_tree(path)
