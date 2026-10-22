"""session lock 获取（session_lock_acquire）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from .payload import _prepare_session_lock_payload
from .reclaim import reclaim_orphan_session_locks
from .write import _acquire_and_write_session_lock


def session_lock_acquire(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """写入 session lock 文件（agent_id + session_id + nonce + branch + timestamp）。

    并发互斥由 ensure_session_lock 内的 flock gate 保证；本函数自身覆盖写入（幂等）。
    新式锁持有 OS flock fd 作进程活性探针，进程退出时内核自动释放，检查方可
    探活判定 stale 并自动清理。锁文件仅作并发互斥载体，不承载 agent 身份。

    Args:
        orchd_dir: .orchd 目录路径。
        agent_id: 当前 session 的 agent ID。
        branch: 当前 git 分支名（可选）。
        session_id: 当前 session ID；缺省时从环境解析。

    Returns:
        结构化结果，永不抛异常：acquired=True（新锁或 reused=True 刷新），或
        acquired=False 且 reason ∈ {flock_contended, lock_dir_unwritable,
        lock_write_failed}（分类见 :func:`_acquire_and_write_session_lock`）。
    """
    lock_path, lock_data = _prepare_session_lock_payload(
        orchd_dir, agent_id, branch, session_id
    )
    result = _acquire_and_write_session_lock(lock_path, lock_data)
    # worktree 孤儿会话锁回收（task-audit-lock-residue-reclaim AC2）：session 路径
    # 每次写锁时 best-effort 识别并清理 worktree 已不存在的锁孤儿（不抛异常、
    # 不阻塞主流程；主 worktree 锁不触碰）。
    try:
        reclaim_orphan_session_locks(orchd_dir, orchd_dir.parent)
    except Exception:
        pass
    return result
