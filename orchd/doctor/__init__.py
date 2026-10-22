"""orchd doctor：git 仓库完整性检测 + 残留清理入口（--fix / --dry-run）。

W2（task-split2 doctor 拆包，file-split-program 20261015 §4.3）把原单文件按域拆为
runtime / checks_runtime / proposals / locks / residue / checks_repo / fix 七个子模块（W2-D5 收口）
；本模块退化为**纯 re-export 聚合入口**（零顶层定义），保持 ``from orchd.doctor import <符号>`` 与
``monkeypatch.setattr("orchd.doctor.<符号>", ...)`` 的旧路径零断裂。补丁面注意：搬迁名的消费点
在各自子模块命名空间解析，故聚合入口上的补丁对已迁出的消费点失效，须改指对应子模块（``os`` /
``shutil`` 为单例模块对象，其属性补丁仍全局生效）；实测全集见 tests/test_doctor_pkg.py 的消费表。
"""

from __future__ import annotations

import os
import shutil

from orchd.doctor.runtime import (
    _DETECTION_UNAVAILABLE_RESIDUE, _GIT_ENCODING, _GIT_ERRORS, _GIT_TIMEOUT, _LOCK_FILE_PATTERNS,
    _PROTECTED_PATHS, _RUNTIME_STATE_FILES, _SOURCE_ASSETS, _RuntimeStateFiles, _UNREADABLE_ACTION,
    _UNREADABLE_RESIDUE, _cat_file_batch, _is_lock_like_name, _is_protected_path, _make_check,
    _make_check_unavailable, _pre_delete_lock_recheck, _probe_is_file, _resolve_git_dir,
    _resolve_runtime_dir, _run_git, _unavailable_residue, _unreadable_residue,
)
from orchd.doctor.checks_runtime import (
    _CHECKPOINT_LAG_FAIL_THRESHOLD, _EVENT_OPTIONAL_FIELDS, _EVENT_REQUIRED_FIELDS, _HUNG_GIT_AGE_S,
    _LESSONS_STAGED_AGING_S, _WIN_FIRST_SAMPLE_MIN, _WIN_PERSIST_FAIL_MIN, _WIN_RESAMPLE_DELAY_S,
    _check_checkpoint_consistency_files, _check_checkpoint_consistency_sqlite,
    _check_checkpoint_consistency, _check_event_schema_files, _check_event_schema_sqlite,
    _check_event_schema, _check_hung_git_processes, _check_lessons_runtime, _check_lessons_transit,
    _check_migration_consistency, _event_schema_problem, _lessons_oldest_age_s,
    _list_git_processes, _windows_pids_persist,
)
from orchd.doctor.proposals import (
    _PROPOSALS_DIR_ENTRY_REASON, _STALE_PROPOSAL_REASON, _STRAY_DRAFT_REASON,
    _detect_stale_proposals, _detect_proposals_dir_entries, _detect_stray_draft_files,
    _draft_content_ids, _is_draft_name, _load_master_task_ids, classify_proposals,
    inflight_proposals, proposals_dir_entries, stale_proposals, stray_draft_files,
)
from orchd.doctor.locks import (
    _SESSION_FILE_PREFIX, _SESSION_FILE_SUFFIX, _THIRD_PARTY_LOCK_NAMES, _detect_git_tracked_locks,
    _detect_orphan_session_locks, _detect_residual_intake_locks, _detect_zombie_sessions,
    _is_orchd_lock_path, _is_third_party_lock_name, _live_session_lock_holders,
)
from orchd.doctor.residue import (
    _GHOST_TERMINAL_STATUSES as _GHOST_TERMINAL_STATUSES, _AUTO_CLEAN_TYPES, _LEGACY_MOVE_TYPES,
    _MANUAL_ACTIONS, _classify_residual_dir, _detect_ghost_tasks, _detect_hook_enforcement_gaps,
    _detect_legacy_flat_residues, _detect_residual_dirs, _detect_stale_worktree_registry,
    _dir_contains_real_content, _ghost_detection_store, _ghost_retract_target, _residue_disposition,
    _run_detector, detect_residues,
)
from orchd.doctor.checks_repo import (
    _GENERATED_DOCS, _GENERATED_MARKER, _REFS_ROOT_IGNORE, _check_contract_drift,
    _check_in_review_worktree_integrity, _check_master_single_copy, check_repo,
)
from orchd.doctor.fix import (
    AUTO_CLEAN_DISPOSITIONS, _BACKUP_RETENTION_DAYS, _BACKUP_RETENTION_KEEP_NEWEST,
    _auto_clean_impl, _auto_clean_item, _auto_move_legacy, _delete_file,
    _dispose_file_to_backup, _dispose_residual_dir, _doctor_fix_impl, _prune_stale_backups,
    auto_clean, doctor, doctor_fix,
)
