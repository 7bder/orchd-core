"""ledger sync — 同步命令入口域（W6-C2 拆分，纯搬移）。

来源：``orchd/ledger_sync.py``（1622 行，W6-C2 纯搬移）。

``pull`` / ``push`` / ``compact`` / ``status_remote`` 四命令与同步锁。

依赖 ``refs`` / ``merge`` / ``local`` / ``inbound``（顶层导入，单向）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import validate_event
from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock
from orchd.ledger import (
    _CHECKPOINT_SCHEMA_VERSION,
    _event_target_status,
    _fsync_dir,
    _write_text_atomic,
    Store,
    TaskState,
    event_sort_key,
    resolve_store_dir,
    review_claim_age_s,
    review_stale_timeout_s,
    validate_transition,
)
from orchd.storage import read_archive_events
from .refs import (
    ORCHD_LEDGER_REF,
    _DEFAULT_REMOTE,
    _REF_DELTA,
    _REF_STATE,
    _check_git_repo,
    _fetch_head_sha,
    _fetch_ref,
    _load_marker,
    _local_ref_sha,
    _push_ref,
    _read_ref_file,
    _save_marker,
    _store_runtime_root,
    _update_local_ref,
    _write_ledger_ref,
)

from .merge import (
    _events_after,
    _inject_deterministic_ids,
    _merge_events_detailed,
    _parse_delta_events,
    _state_summary,
    build_state,
)

from .inbound import (
    _count_inbound_rejected,
    _inbound_rejected_ids,
)

from .local import (
    _bootstrap_local_from_state,
    _reconcile_local,
)



# sync 专用锁（L-4，账本根下）：Git 网络段（fetch/push 最坏约 210s）不再占账本锁，
# 并发 sync 之间由本锁串行；账本锁只在「本地快照 + 重建落盘」临界区短暂持有。
_SYNC_LOCK_NAME = ".sync.lock"

# 获取 .sync.lock 的阻塞等待上限（秒）：需覆盖最坏网络段，故显著大于账本锁 10s。
# ORCHD_SYNC_LOCK_WAIT_SECS 可覆盖（须为正数）。
_SYNC_LOCK_WAIT_SECS = 300



def _sync_lock_path(store: Store) -> Path:
    """sync 专用锁路径（账本根下，与 `.lock` / `.intake.lock` 同级）。"""
    return _store_runtime_root(store) / _SYNC_LOCK_NAME



def _sync_lock_wait_secs() -> float:
    """获取 `.sync.lock` 的阻塞等待上限（秒）。

    默认 :data:`_SYNC_LOCK_WAIT_SECS`（300s）；环境变量
    ``ORCHD_SYNC_LOCK_WAIT_SECS`` 可覆盖（须为正数）。
    """
    env = os.environ.get("ORCHD_SYNC_LOCK_WAIT_SECS")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return float(_SYNC_LOCK_WAIT_SECS)



@contextmanager
def _ledger_section(store: Store):
    """账本锁临界区（L-4 收缩锁窗）：本地快照读取 + 合并重建 + 状态快照。

    只有「读本地事件 → 合并 → 重写 ledger/checkpoint → 取 state 快照」这一段
    需要与账本写者（claim/done/amend 等）互斥；Git 网络阶段（fetch/push）与
    本地 ref 构造刻意放在本临界区**之外**，避免 sync 期并发写命令落 E012。
    """
    store.acquire_lock()
    try:
        yield
    finally:
        store.release_lock()



def _sync_lock_and_call(store: Store, fn):
    """sync 写路径统一持 **独立 `.sync.lock`**（L-4）。

    旧实现持账本全局锁跑全部 git 网络 I/O：fetch/push 最坏约 210s，期间任何并发
    claim/done 必然撞账本锁落 E012。现口径：

    - 整个 sync 只持专用 ``.sync.lock``（并发 sync 之间仍严格串行，不与账本写者
      争锁；等待上限 300s 覆盖最坏网络段，可经 ``ORCHD_SYNC_LOCK_WAIT_SECS`` 覆盖）；
    - 账本锁仅在 :func:`_ledger_section` 临界区短暂持有（本地快照 + 重建落盘），
      网络阶段完全不占账本锁 → 并发 claim/done 不被阻塞、不落 E012。
    """
    lock = ExclusiveFileLock(_sync_lock_path(store))
    lock.acquire(blocking=True, timeout_s=_sync_lock_wait_secs())
    try:
        return fn()
    finally:
        lock.release()



def pull(
    project_root: Path,
    store: Store,
    remote: str = _DEFAULT_REMOTE,
) -> dict[str, Any]:
    """fetch 远端 ref → 远端 delta 合并进本地账本（event_id 去重）→ 重建 checkpoint。

    幂等：远端 delta 中本地已有的事件被跳过，重复 pull 无副作用。

    信任边界（task-pass9-ref-tx-sync-hardening，pass9 F9）：远端事件无签名 /
    哈希链校验（单机可信协作假设与 2.0 演进要求见
    ``docs/implementation-design.md`` §4.6「跨设备账本同步信任边界」），但入库前
    逐事件过入站校验（``_validate_inbound_events``：契约 + 跃迁白矩阵，非法逐条
    丢弃 + E030 可观测计数，不中止整库合并）。
    Returns:
        {pulled, local_events, remote, merged}：合并摘要；远端尚无 ref 时
        ``remote_ref`` 为 None。
    """

    def _do() -> dict[str, Any]:
        has_remote = _fetch_ref(project_root, remote)
        if not has_remote:
            return {
                "performed":
                True,
                "pulled":
                0,
                "local_events":
                store.ledger_line_count() if store.ledger_exists() else 0,
                "remote_ref":
                None,
                "bootstrap": {
                    "applied": False,
                    "tasks": 0,
                    "reason": "no_remote_ref",
                },
                "note":
                f"远端 '{remote}' 尚无 {ORCHD_LEDGER_REF} ref",
            }
        delta_text = _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA)
        state_text = _read_ref_file(project_root, "FETCH_HEAD", _REF_STATE)
        remote_delta, malformed_skipped = _parse_delta_events(delta_text)
        missing_id_count = _inject_deterministic_ids(remote_delta)
        # 远端快照解析分类：非 dict / 不可解析分别留痕，供 bootstrap 判据返回
        # 结构化 reason（正常 pull 里它仍只是回显摘要的来源）。
        remote_state: dict[str, Any] = {}
        state_error: str | None = None
        if state_text:
            try:
                parsed_state = json.loads(state_text)
            except (json.JSONDecodeError, ValueError):
                state_error = "state_unparsable"
            else:
                if isinstance(parsed_state, dict):
                    remote_state = parsed_state
                else:
                    state_error = "state_not_object"
        # 阶段 2（持账本锁，短暂）：本地快照 + 合并重建（网络段已在锁外完成）
        with _ledger_section(store):
            local_events = store.backend.read_events() if store.ledger_exists(
            ) else []
            existing = {
                e.get("event_id")
                for e in local_events if e.get("event_id")
            }
            new_remote = [
                e for e in remote_delta
                if e.get("event_id") and e["event_id"] not in existing
            ]
            merged, reconcile_warnings = _reconcile_local(store,
                                                          local_events,
                                                          new_remote,
                                                          source="pull")
            # 新设备 bootstrap（task-sync-compact-bootstrap-from-state）：远端
            # delta 已被 compact 置空时，用远端 state 快照建本地 checkpoint，
            # 否则新设备派生态全空。判据与写入细节见 _bootstrap_local_from_state。
            bootstrap = _bootstrap_local_from_state(store, remote_state,
                                                    local_events,
                                                    remote_delta, state_error)
        result: dict[str, Any] = {
            "performed": True,
            "pulled": len(new_remote),
            "remote_events_without_id": missing_id_count,
            "remote_malformed_skipped": malformed_skipped,
            "remote_rejected":
            _count_inbound_rejected(reconcile_warnings),
            "local_events": len(merged),
            "remote_ref": ORCHD_LEDGER_REF,
            "remote": _state_summary(remote_state),
            "bootstrap": bootstrap,
        }
        if reconcile_warnings:
            result["warnings"] = reconcile_warnings
        # L-3：序列级软校验违规以独立结构化字段返回（warnings 内亦含 violations）
        replay_violations = store.replay_violations
        if replay_violations:
            result["replay_violations"] = replay_violations
        return result

    _check_git_repo(project_root)
    return _sync_lock_and_call(store, _do)



def push(
    project_root: Path,
    store: Store,
    remote: str = _DEFAULT_REMOTE,
) -> dict[str, Any]:
    """显式推送本地账本增量到远端 orchd/ledger ref。

    流程（pull-first）：
    1. fetch 远端 ref；远端 delta 中本地缺失事件合并进本地账本（去重重建）；
    2. 本地新事件 = marker 之后的本地事件；新 delta = 远端存量 delta（剔除本次被判
       ``basis=absolute`` 的入站拒收事件，relative 类继续携带）∪ 本地新事件（去重），
       确保不丢他端尚未 compact 的事件尾；
    3. 从合并后本地账本重算 state.json（O(任务数)）；
    4. 单提交 tree（state.json + delta.jsonl），``--force-with-lease`` 推送。
    push 成功后更新本地 marker 为本地最后事件 id。
    """

    def _do() -> dict[str, Any]:
        # 阶段 1（不持账本锁）：Git 网络 —— fetch 远端 ref + 读远端 delta/state
        has_remote = _fetch_ref(project_root, remote)
        remote_delta: list[dict[str, Any]] = []
        expected_sha: str | None = None
        missing_id_count = 0
        malformed_skipped = 0
        if has_remote:
            remote_delta, malformed_skipped = _parse_delta_events(
                _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA))
            missing_id_count = _inject_deterministic_ids(remote_delta)
            expected_sha = _fetch_head_sha(project_root)

        # 阶段 2（持账本锁，短暂）：本地快照 + pull-first 合并重建 + delta/state 快照
        with _ledger_section(store):
            local_events = store.backend.read_events() if store.ledger_exists(
            ) else []
            # pull-first：远端 delta 中本地缺失事件并入本地（确定性全局序重建 checkpoint）
            existing = {
                e.get("event_id")
                for e in local_events if e.get("event_id")
            }
            new_remote = [
                e for e in remote_delta
                if e.get("event_id") and e["event_id"] not in existing
            ]
            merged, reconcile_warnings = _reconcile_local(store,
                                                          local_events,
                                                          new_remote,
                                                          source="push")

            # 本地新事件（基于本地存量 + marker，不含本次拉入的远端事件）
            # + 远端存量 delta → 新 delta（有界于同步间隔，event_id 去重）
            mark = _load_marker(store)
            local_after = _events_after(local_events,
                                        mark.get("last_pushed_event_id"))
            # 被**绝对判非法**的入站拒收事件不得再被 republish（task-sync-push-kept-events）：
            # 合并侧已逐条判过（_validate_inbound_events），此处只排除其中 basis=absolute
            # 那批（同一引擎版本下与设备无关地非法）。两条边界都得守住：
            # ① 不对 remote_delta 全集重跑判据——本地状态领先于转录时，合法旧事件会被判
            #    invalid_transition（如 done --CLAIMED--> claimed）而遭踢出 delta；
            # ② 也不排除 basis=relative 那批——它只对本机派生态/本机账本非法，对状态落后
            #    的他端完全合法，删它就是让一台设备删掉另一台设备的历史事件。
            rejected_ids = _inbound_rejected_ids(reconcile_warnings)
            kept_remote_delta = (
                [ev for ev in remote_delta
                 if ev.get("event_id") not in rejected_ids]
                if rejected_ids else remote_delta)
            delta, delta_collisions = _merge_events_detailed(local_after +
                                                             kept_remote_delta)
            reconcile_warnings.extend(delta_collisions)

            violations_snapshot = store.replay_violations
            state = build_state(store)
            pushed = len(local_after)

        # 阶段 3（不持账本锁）：本地 ref 单提交 + 推送（网络）+ marker 落盘
        parent_sha = expected_sha or _local_ref_sha(project_root)
        commit_sha = _write_ledger_ref(project_root, state, delta, parent_sha)
        _update_local_ref(project_root, commit_sha)
        _push_ref(project_root, remote, expected_sha)

        last_event_id = merged[-1].get("event_id") if merged else None
        _save_marker(store, last_event_id)
        result: dict[str, Any] = {
            "performed": True,
            "pushed_delta": pushed,
            "remote_delta_merged": len(new_remote),
            "remote_events_without_id": missing_id_count,
            "remote_malformed_skipped": malformed_skipped,
            "remote_rejected":
            _count_inbound_rejected(reconcile_warnings),
            "local_events": len(merged),
            "commit": commit_sha,
            "delta_events": len(delta),
            "state_tasks": len(state.get("tasks", {})),
        }
        if reconcile_warnings:
            result["warnings"] = reconcile_warnings
        # L-3：序列级软校验违规以独立结构化字段返回（warnings 内亦含 violations）
        # NEW-L2：使用 build_state 之前的快照，防止 store.replay() 重置覆盖
        replay_violations = violations_snapshot
        if replay_violations:
            result["replay_violations"] = replay_violations
        return result

    _check_git_repo(project_root)
    return _sync_lock_and_call(store, _do)



def compact(
    project_root: Path,
    store: Store,
    remote: str = _DEFAULT_REMOTE,
) -> dict[str, Any]:
    """把 delta.jsonl 归并入 state.json 并清空 delta，写回 ref（单提交）。

    pull-first：先合并远端 delta 进本地，再以合并后状态重建 state.json；
    delta 置空后推送，git 承载体积收敛到 O(任务数)。
    """

    def _do() -> dict[str, Any]:
        # 阶段 1（不持账本锁）：Git 网络 —— fetch 远端 ref + 读远端 delta/state
        has_remote = _fetch_ref(project_root, remote)
        remote_delta: list[dict[str, Any]] = []
        expected_sha: str | None = None
        missing_id_count = 0
        malformed_skipped = 0
        if has_remote:
            remote_delta, malformed_skipped = _parse_delta_events(
                _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA))
            missing_id_count = _inject_deterministic_ids(remote_delta)
            expected_sha = _fetch_head_sha(project_root)

        # 阶段 2（持账本锁，短暂）：本地快照 + 合并重建 + state 快照
        with _ledger_section(store):
            local_events = store.backend.read_events() if store.ledger_exists(
            ) else []
            existing = {
                e.get("event_id")
                for e in local_events if e.get("event_id")
            }
            new_remote = [
                e for e in remote_delta
                if e.get("event_id") and e["event_id"] not in existing
            ]
            merged, reconcile_warnings = _reconcile_local(store,
                                                          local_events,
                                                          new_remote,
                                                          source="compact")
            violations_snapshot = store.replay_violations
            state = build_state(store)

        # 阶段 3（不持账本锁）：delta 置空的单提交 + 推送（网络）+ marker 落盘
        parent_sha = expected_sha or _local_ref_sha(project_root)
        commit_sha = _write_ledger_ref(project_root, state, [], parent_sha)
        _update_local_ref(project_root, commit_sha)
        _push_ref(project_root, remote, expected_sha)

        last_event_id = merged[-1].get("event_id") if merged else None
        _save_marker(store, last_event_id)
        result: dict[str, Any] = {
            "performed": True,
            "compacted_delta": len(remote_delta) + 0,
            "merged_from_remote": len(new_remote),
            "remote_events_without_id": missing_id_count,
            "remote_malformed_skipped": malformed_skipped,
            "remote_rejected":
            _count_inbound_rejected(reconcile_warnings),
            "local_events": len(merged),
            "commit": commit_sha,
            "state_tasks": len(state.get("tasks", {})),
        }
        if reconcile_warnings:
            result["warnings"] = reconcile_warnings
        # L-3：序列级软校验违规以独立结构化字段返回（warnings 内亦含 violations）
        # NEW-L2：使用 build_state 之前的快照，防止 store.replay() 重置覆盖
        replay_violations = violations_snapshot
        if replay_violations:
            result["replay_violations"] = replay_violations
        return result

    _check_git_repo(project_root)
    return _sync_lock_and_call(store, _do)



def status_remote(
    project_root: Path,
    store: Store,
    remote: str = _DEFAULT_REMOTE,
) -> dict[str, Any]:
    """只读查看远端 orchd/ledger ref 的进度摘要（不修改本地任何状态）。

    供 ``status --remote`` 使用；远端尚无 ref / git 异常时返回相应字段。
    """
    try:
        _check_git_repo(project_root)
        has_remote = _fetch_ref(project_root, remote)
        if not has_remote:
            return {"performed": True, "remote_ref": None}
        state_text = _read_ref_file(project_root, "FETCH_HEAD", _REF_STATE)
        delta_text = _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA)
        state = json.loads(state_text) if state_text else {}
        delta, delta_malformed = _parse_delta_events(delta_text)
        return {
            "performed":
            True,
            "remote_ref":
            ORCHD_LEDGER_REF,
            "remote_tip":
            _fetch_head_sha(project_root),
            "state":
            _state_summary(state),
            "delta_events":
            len(delta),
            "delta_malformed_skipped":
            delta_malformed,
            "local_events":
            store.ledger_line_count() if store.ledger_exists() else 0,
        }
    except OrchdError as exc:
        return {
            "performed": False,
            "error": {
                "code": exc.code.name,
                "message": exc.message
            }
        }
