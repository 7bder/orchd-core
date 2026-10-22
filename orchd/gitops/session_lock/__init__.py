"""Orchd session lock / gate 协调（聚合入口）。

W8-5（task-split2-w8-5-session-lock-pkg）：旧 orchd/gitops/session_lock.py（910 行）
纯搬移拆为本包十三域。本 __init__.py 纯重导出（零顶层定义），外部
``from orchd.gitops.session_lock import X`` 与 ``orchd.gitops.session_lock.X`` 属性访问零改动。

依赖方向：constants/degrade/gate/write/probe（叶）<- state/paths <- payload/reclaim <-
acquire/check/release <- ensure 编排器。

monkeypatch 打点面（W8-5 迁移）：session_lock_acquire/_lock_state -> ensure；
session_lock_check -> ensure + release（双 target）。
"""

from .constants import (
    _SESSION_LOCK_FILENAME,
    _SESSION_GATE_FILENAME,
    _SESSION_GATE_TIMEOUT_S,
    _SESSION_GATE_ATTEMPTS,
    _SESSION_GATE_BACKOFF_S,
    _SESSION_LOCK_REGISTRY,
    _SESSION_LOCK_TIMEOUT_MIN,
    _SESSION_LOCK_FLOCK_MARKER,
)
from .degrade import _log_session_lock_degrade
from .state import _lock_state, _gate_unavailable_state
from .gate import _acquire_session_gate
from .paths import (
    _resolve_store_root,
    _git_worktree_name,
    _get_session_lock_path,
    _get_session_gate_path,
)
from .payload import _prepare_session_lock_payload
from .write import _acquire_and_write_session_lock
from .probe import _probe_session_lock_os_active
from .reclaim import _reclaim_stale_lock, _linked_worktree_names, reclaim_orphan_session_locks
from .acquire import session_lock_acquire
from .check import session_lock_check
from .release import session_lock_release, release_session_lock_if_owned
from .ensure import ensure_session_lock

__all__ = (
    '_SESSION_LOCK_FILENAME',
    '_SESSION_GATE_FILENAME',
    '_SESSION_GATE_TIMEOUT_S',
    '_SESSION_GATE_ATTEMPTS',
    '_SESSION_GATE_BACKOFF_S',
    '_SESSION_LOCK_REGISTRY',
    '_SESSION_LOCK_TIMEOUT_MIN',
    '_SESSION_LOCK_FLOCK_MARKER',
    '_log_session_lock_degrade',
    '_lock_state',
    '_gate_unavailable_state',
    '_acquire_session_gate',
    '_resolve_store_root',
    '_git_worktree_name',
    '_get_session_lock_path',
    '_get_session_gate_path',
    '_prepare_session_lock_payload',
    '_acquire_and_write_session_lock',
    '_probe_session_lock_os_active',
    '_reclaim_stale_lock',
    '_linked_worktree_names',
    'reclaim_orphan_session_locks',
    'session_lock_acquire',
    'session_lock_check',
    'session_lock_release',
    'release_session_lock_if_owned',
    'ensure_session_lock',
)
