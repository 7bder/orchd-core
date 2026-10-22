"""Orchd 事件存储引擎 —— 基于 Event Sourcing 模式的持久化层（聚合入口，纯 re-export）。

按职责域拆分子模块（file-split-program W1）：``atomic`` 原子写与行式文本原语、``identity``
身份与会话、``locks`` intake 准入锁、``paths`` 路径解析、``backend`` 后端选择、``events`` 状态
迁移白矩阵、``envelope`` 通道 C 错误封套、``store`` ``Store`` 与派生状态。本模块只重导出：
``from orchd.ledger import <符号>`` 与 ``monkeypatch.setattr("orchd.ledger.<符号>", ...)`` 的
调用面 / 补丁面零断裂；各域的取用方式（顶层导入 / 函数级惰性导入）见各子模块头注释与
``.orchd/shared/conventions.md`` 依赖方向节。本模块顶层仅依赖自身子模块 + ``errors`` /
``lockfile`` / ``contracts`` / ``storage(.filesystem)``，不导入 guide / worktree / spec / pool /
onboard。
"""

from __future__ import annotations

from orchd.contracts.events import TASK_STATUSES, TERMINAL_STATUSES, normalize_event, validate_event
from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock, _depth_registry, _flock_op, read_locked_text
# task-storage-port-adapter-split：StorageBackend（端口）迁至 orchd.storage、
# FilesystemBackend（文件适配器）迁至 orchd.storage.filesystem；此处重导出保持兼容。
from orchd.storage import StorageBackend
from orchd.storage.filesystem import FilesystemBackend

# 以下为各职责域（L2–L7）的重导出面：补丁点仍是 orchd.ledger.<符号>。
from orchd.ledger.atomic import (
    _ATOMIC_REPLACE_BACKOFF_S, _ATOMIC_REPLACE_RETRIES, _atomic_replace, _fsync_dir,
    _is_transient_replace_error, _write_bytes_atomic, _write_text_atomic, read_text_or_error,
    single_line,
)
from orchd.ledger.backend import (
    _STORAGE_BACKEND_ENV, _checkpoint_has_state, _select_backend, _sqlite_has_state,
    ledger_status_available, open_store, resolve_backend_kind,
)
from orchd.ledger.identity import (
    _ORCHD_SESSION_ID_ENV, _SESSION_RUNTIME_ACTIVE, _SESSION_RUNTIME_DIRNAME,
    _SESSION_TTL_DEFAULT_MIN, _SESSION_TTL_MIN_ENV, _derive_session_identity_from_token,
    _parse_session_ts, _session_runtime_path, _touch_session_last_seen, event_sort_key,
    is_fingerprint_agent_id, is_session_expired, parse_event_time, resolve_agent_id,
    resolve_session_identity, session_current, session_end, session_runtime_dir, session_start,
    session_ttl_minutes,
)
from orchd.ledger.locks import (
    _INTAKE_LOCK_FILENAME, _INTAKE_LOCK_TIMEOUT, _INTAKE_LOCK_WAIT_SECS, _intake_lock_registry,
    _intake_lock_registry_guard, _intake_lock_wait_secs, _mark_intake_lock_released,
    _reclaim_stale_lock_atomically, intake_lock_acquire, intake_lock_check, intake_lock_clear,
    intake_lock_path, intake_lock_release, reclaim_orphan_intake_locks,
)
from orchd.ledger.paths import (
    _ROADMAP_DOC, _WORKSPACE_DOCS, _find_orchd_dir, _legacy_roadmap_actionable,
    _log_legacy_roadmap, resolve_review_mode, resolve_roadmap_path, resolve_store_dir,
    resolve_workspace_root,
)
from orchd.ledger.envelope import _attach_structured_guidance, structured_error
from orchd.ledger.events import (
    _TRANSITION_WHITELIST, _UNGATED_TRANSITIONS, _event_target_status, latest_rework_scope,
    validate_transition,
)
from orchd.ledger.store import (
    Store, TaskDerived, TaskState, _CHECKPOINT_SCHEMA_VERSION, _REVIEW_STALE_DEFAULT_S,
    generate_event_id, review_claim_age_s, review_stale_timeout_s, stale_review_claims,
)
