"""Orchd 账本 git 跨设备同步（orchd sync，task-ledger-git-sync）。

账本（``_ledger.jsonl`` / ``_checkpoint.json``）为本地运行时状态，不入 git。
本模块提供**显式** ``orchd sync`` 子命令（push / pull / compact），使任务进度
可经 git 跨设备读取，同时不破坏账本独立性、不造成 git 体积无限膨胀。

核心机制（可持续性方案见 .trae/documents/ledger-git-sync-sustainability-plan.md）：

- **专用账本 ref** ``refs/heads/orchd/ledger``：单提交、``--force-with-lease``
  推送，旧对象由 ``git gc`` 回收，git 体积收敛到「当前内容」而非「全部历史」。
- **单提交 tree 含两文件**：
  - ``state.json`` — 紧凑任务状态表，**O(任务数)** 有界：每任务
    ``status / review_phase / claimed_by / claimed_session / review_claimed_at /
    attempt_count / updated_event_id``，由本地合并后全量事件 replay 派生；
  - ``delta.jsonl`` — 自上次 ``sync`` 以来未归档事件尾，有界于同步间隔
    （``--compact`` 归并入 ``state.json`` 后清空）。
- **pull-first + event_id 去重合并**：pull/compact 先 fetch 远端 ref，把远端
  ``delta.jsonl`` 中本地缺失事件合并进本地账本（按 ``(timestamp, event_id)``
  确定性排序），再重建 checkpoint；重复 pull/sync 幂等。
- 本地 ``_ledger.jsonl`` 始终保留完整审计（跨设备事件按确定性全局序归并）。

git 承载的文件不支持动态建路径；ref 名与文件名均为固定常量。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store

# 专用账本 ref（本地 + 远端同名）
ORCHD_LEDGER_REF = "refs/heads/orchd/ledger"

# ref tree 内文件名
_REF_STATE = "state.json"
_REF_DELTA = "delta.jsonl"

# 本地同步 marker（账本根下，运行时状态不入 git）
_SYNC_MARKER_NAME = ".ledger_sync_marker.json"

# 远端默认名
_DEFAULT_REMOTE = "origin"

# ------------------------------------------------------------------
# 本地同步 marker
# ------------------------------------------------------------------


def _marker_path(store: Store) -> Path:
    """同步 marker 文件路径（账本根，与 ledger/checkpoint 同级，不入 git）。"""
    return store.backend.orchd_dir / _SYNC_MARKER_NAME


def _load_marker(store: Store) -> dict[str, Any]:
    path = _marker_path(store)
    if not path.exists():
        return {"last_pushed_event_id": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_pushed_event_id": None}


def _save_marker(store: Store, last_pushed_event_id: str | None) -> None:
    path = _marker_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "last_pushed_event_id": last_pushed_event_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=1,
        ) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------
# git 子进程（沿用现有 subprocess 风格，UTF-8 解码）
# ------------------------------------------------------------------


def _run_git(
    project_root: Path,
    args: list[str],
    input_text: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """运行 git 子进程并返回 UTF-8 解码结果。

    输入以 bytes 传递（避免 Windows 文本模式把 \\n 翻译成 \\r\\n，导致
    mktree 文件名带上 \\r 污染 tree 结构）；输出手动 UTF-8 解码
    （不启用 subprocess 文本模式，防止与 bytes 输入冲突挂起——
    2026-09-10 实踩：encoding= 与 bytes input 同用时 mktree 读 stdin 阻塞）。
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        input=input_text.encode("utf-8") if input_text is not None else None,
        capture_output=True,
        timeout=timeout,
    )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _git_fail(project_root: Path, args: list[str],
              proc: subprocess.CompletedProcess[str]) -> OrchdError:
    """git 子进程失败 → 结构化 OrchdError（E007 语义扩展：git 操作失败）。"""
    detail = (proc.stderr or proc.stdout or "").strip()[:400]
    return OrchdError(
        ErrorCode.E007,
        f"git_sync_failed: git {' '.join(args[:3])} → exit {proc.returncode}",
        [{
            "command": "git " + " ".join(args),
            "detail": detail,
            "project_root": str(project_root)
        }],
    )


def _fetch_head_sha(project_root: Path) -> str | None:
    """返回 FETCH_HEAD 的 commit sha；不可解析返回 None。"""
    r = _run_git(project_root,
                 ["rev-parse", "--verify", "--quiet", "FETCH_HEAD"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _local_ref_sha(project_root: Path) -> str | None:
    """返回本地 orchd/ledger ref 的 sha；不存在返回 None。"""
    r = _run_git(project_root,
                 ["rev-parse", "--verify", "--quiet", ORCHD_LEDGER_REF])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _check_git_repo(project_root: Path) -> None:
    """前置校验：project_root 必须是 git 工作树（非仓库抛 E007）。"""
    r = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise OrchdError(
            ErrorCode.E007,
            "git_sync_not_a_repo: orchd sync 需要 git 工作树",
            [{
                "project_root": str(project_root),
                "hint": "在项目根（含 .git）目录下执行 sync"
            }],
        )


def _fetch_ref(project_root: Path, remote: str) -> bool:
    """fetch 远端 orchd/ledger ref 到 FETCH_HEAD。

    Returns:
        True：远端存在该 ref（FETCH_HEAD 已就位）；False：远端尚无该 ref。
    """
    check = _run_git(project_root, ["remote", "get-url", remote])
    if check.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            f"git_sync_no_remote: 仓库没有远端 '{remote}'",
            [{
                "remote": remote,
                "hint": "git remote add origin <url> 后重试"
            }],
        )
    r = _run_git(project_root,
                 ["fetch", remote, ORCHD_LEDGER_REF, "--no-tags"])
    return r.returncode == 0


def _read_ref_file(project_root: Path, ref_spec: str,
                   filename: str) -> str | None:
    """读取 ref 中指定文件内容；文件不存在返回 None。"""
    r = _run_git(project_root, ["show", f"{ref_spec}:{filename}"])
    if r.returncode != 0:
        return None
    return r.stdout


def _write_ledger_ref(
    project_root: Path,
    state: dict[str, Any],
    delta: list[dict[str, Any]],
    parent_sha: str | None,
) -> str:
    """在临时目录构造新 tree（state.json + delta.jsonl）→ commit-tree 单提交。

    返回新 commit 的 sha。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with tempfile.TemporaryDirectory() as td:
        state_path = Path(td) / _REF_STATE
        delta_path = Path(td) / _REF_DELTA
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) +
            "\n",
            encoding="utf-8",
        )
        delta_lines = "".join(
            json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
            for ev in delta)
        delta_path.write_text(delta_lines, encoding="utf-8")

        blob_state = _run_git(
            project_root,
            ["hash-object", "-w", str(state_path)])
        if blob_state.returncode != 0:
            raise _git_fail(project_root, ["hash-object"], blob_state)
        blob_delta = _run_git(
            project_root,
            ["hash-object", "-w", str(delta_path)])
        if blob_delta.returncode != 0:
            raise _git_fail(project_root, ["hash-object"], blob_delta)

        tree_input = (
            f"100644 blob {blob_state.stdout.strip()}\t{_REF_STATE}\n"
            f"100644 blob {blob_delta.stdout.strip()}\t{_REF_DELTA}\n")
        mktree = _run_git(project_root, ["mktree"], input_text=tree_input)
        if mktree.returncode != 0:
            raise _git_fail(project_root, ["mktree"], mktree)
        tree_sha = mktree.stdout.strip()

        commit_args = [
            "commit-tree", tree_sha, "-m", f"orchd sync ledger at {now}"
        ]
        if parent_sha:
            commit_args += ["-p", parent_sha]
        commit = _run_git(project_root, commit_args)
        if commit.returncode != 0:
            raise _git_fail(project_root, ["commit-tree"], commit)
        return commit.stdout.strip()


def _update_local_ref(project_root: Path, commit_sha: str) -> None:
    """把新 commit 指向本地 orchd/ledger ref（不 checkout，无分支切换）。"""
    r = _run_git(project_root, ["update-ref", ORCHD_LEDGER_REF, commit_sha])
    if r.returncode != 0:
        raise _git_fail(project_root, ["update-ref"], r)


def _push_ref(project_root: Path, remote: str,
              expected_sha: str | None) -> None:
    """push 本地 orchd/ledger ref 到远端同名 ref。

    - 远端已有该 ref（``expected_sha`` 非空）→ ``--force-with-lease=<ref>:<expected>``
      CAS 语义：远端被并行更新则拒绝，由 pull-first 重试兜底；
    - 远端无该 ref（首次）→ 普通 push 创建。
    """
    args = ["push", remote, f"{ORCHD_LEDGER_REF}:{ORCHD_LEDGER_REF}"]
    if expected_sha:
        args.append(f"--force-with-lease={ORCHD_LEDGER_REF}:{expected_sha}")
    r = _run_git(project_root, args)
    if r.returncode != 0:
        raise _git_fail(project_root, ["push"], r)


# ------------------------------------------------------------------
# 状态派生 / 事件合并（纯逻辑）
# ------------------------------------------------------------------


def build_state(store: Store) -> dict[str, Any]:
    """从本地合并后账本派生 state.json 紧凑任务状态表（O(任务数)）。

    每任务字段：status / attempt_count / updated_event_id + 非空可选字段
    （review_phase / claimed_by / claimed_session / review_claimed_at）。
    ``updated_event_id`` 为该任务最近一次事件 id（正序扫描后者覆盖）。
    """
    state = store.replay()
    last_event: dict[str, str] = {}
    if store.ledger_exists():
        for ev in store.backend.read_events():
            tid = ev.get("task_id")
            eid = ev.get("event_id")
            if tid and eid:
                last_event[tid] = eid
    tasks: dict[str, dict[str, Any]] = {}
    for tid, ts in sorted(state.items()):
        entry: dict[str, Any] = {
            "status": ts.status,
            "attempt_count": ts.attempt_count,
            "updated_event_id": last_event.get(tid),
        }
        for key in ("review_phase", "claimed_by", "claimed_session",
                    "review_claimed_at"):
            val = getattr(ts, key, None)
            if val:
                entry[key] = val
        tasks[tid] = entry
    return {
        "schema_version": 1,
        "generated_at":
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": tasks,
    }


def _parse_delta_events(text: str | None) -> list[dict[str, Any]]:
    """解析 delta.jsonl 文本为事件列表（空/None → 空列表，坏行跳过）。"""
    if not text or not text.strip():
        return []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _merge_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 timestamp 稳定归并 + event_id 去重合并。

    两设备合并后的事件集相同、全局序相同 → replay 结果收敛一致。

    排序语义（2026-09-10 修复）：**仅以 timestamp 为主键的稳定排序**——
    同一设备内事件的相对顺序（如 CLAIMED→DONE→REVIEW_READY）由输入顺序
    天然保持（Python sorted 稳定）；同秒事件不再按 event_id 二次排序，
    否则同一秒生成的事件（timestamp 相同）会按随机 event_id 打乱语义顺序，
    导致 REVIEW_READY 排在 CLAIMED 之前、replay 状态错乱（实踩）。
    输入约定：``local_events + new_remote``——本地在前、远端增量在后，
    稳定归并后本地事件整体优先（确定性：同一对输入恒同序）。

    无 event_id 的事件保守保留（不参与去重，防审计丢失）。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in sorted(
            events,
            key=lambda e: e.get("timestamp", ""),
    ):
        eid = ev.get("event_id", "")
        if eid:
            if eid in seen:
                continue
            seen.add(eid)
        out.append(ev)
    return out


def _events_after(events: list[dict[str, Any]],
                  last_pushed_event_id: str | None) -> list[dict[str, Any]]:
    """取 marker（已推送事件 id）之后的事件；marker 缺失/找不到 → 全量（保守）。"""
    if not last_pushed_event_id:
        return list(events)
    for i, ev in enumerate(events):
        if ev.get("event_id") == last_pushed_event_id:
            return events[i + 1:]
    return list(events)


def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """从远端 state.json 派生进度摘要（供 pull 响应展示）。"""
    tasks = state.get("tasks") or {}
    counts: dict[str, int] = {}
    for ts in tasks.values():
        status = ts.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {"task_total": len(tasks), "by_status": counts}


# ------------------------------------------------------------------
# 订阅命令（push / pull / compact）
# ------------------------------------------------------------------


def _rebuild_local_ledger(store: Store, events: list[dict[str, Any]]) -> int:
    """把合并后（去重排序）事件原子重写本地 ledger 并重建 checkpoint。

    ledger 为事件审计唯一权威：重写保留全部事件、仅规范化确定性全局序，
    随后 replay_full → update_checkpoint 重建快照（O(任务数) checkpoint）。
    返回本地事件总数。
    """
    store.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if events:
        lines = "".join(
            json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
            for ev in events)
    else:
        lines = ""
    tmp = store.ledger_path.with_suffix(".sync.tmp")
    tmp.write_text(lines, encoding="utf-8")
    os.replace(str(tmp), str(store.ledger_path))
    store._line_count = len(events)
    state = store.replay_full()
    store.update_checkpoint(state)
    return len(events)


def _sync_lock_and_call(store: Store, fn):
    """sync 写路径统一持账本锁（并发写防撕裂）。"""
    store.acquire_lock()
    try:
        return fn()
    finally:
        store.release_lock()


def pull(
    project_root: Path,
    store: Store,
    remote: str = _DEFAULT_REMOTE,
) -> dict[str, Any]:
    """fetch 远端 ref → 远端 delta 合并进本地账本（event_id 去重）→ 重建 checkpoint。

    幂等：远端 delta 中本地已有的事件被跳过，重复 pull 无副作用。
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
                "note":
                f"远端 '{remote}' 尚无 {ORCHD_LEDGER_REF} ref",
            }
        delta_text = _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA)
        state_text = _read_ref_file(project_root, "FETCH_HEAD", _REF_STATE)
        remote_delta = _parse_delta_events(delta_text)
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
        merged = _merge_events(local_events + new_remote)
        if len(merged) != len(local_events):
            _rebuild_local_ledger(store, merged)
        remote_state = json.loads(state_text) if state_text else {}
        return {
            "performed": True,
            "pulled": len(new_remote),
            "local_events": len(merged),
            "remote_ref": ORCHD_LEDGER_REF,
            "remote": _state_summary(remote_state),
        }

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
    2. 本地新事件 = marker 之后的本地事件；新 delta = 远端存量 delta ∪ 本地新事件
       （去重），确保不丢他端尚未 compact 的事件尾；
    3. 从合并后本地账本重算 state.json（O(任务数)）；
    4. 单提交 tree（state.json + delta.jsonl），``--force-with-lease`` 推送。
    push 成功后更新本地 marker 为本地最后事件 id。
    """

    def _do() -> dict[str, Any]:
        local_events = store.backend.read_events() if store.ledger_exists(
        ) else []
        has_remote = _fetch_ref(project_root, remote)
        remote_delta: list[dict[str, Any]] = []
        expected_sha: str | None = None
        if has_remote:
            remote_delta = _parse_delta_events(
                _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA))
            expected_sha = _fetch_head_sha(project_root)

        # pull-first：远端 delta 中本地缺失事件并入本地（确定性全局序重建 checkpoint）
        existing = {
            e.get("event_id")
            for e in local_events if e.get("event_id")
        }
        new_remote = [
            e for e in remote_delta
            if e.get("event_id") and e["event_id"] not in existing
        ]
        merged = _merge_events(local_events + new_remote)
        if len(merged) != len(local_events):
            _rebuild_local_ledger(store, merged)

        # 本地新事件（基于本地存量 + marker，不含本次拉入的远端事件）
        # + 远端存量 delta → 新 delta（有界于同步间隔，event_id 去重）
        mark = _load_marker(store)
        local_after = _events_after(local_events,
                                    mark.get("last_pushed_event_id"))
        delta = _merge_events(local_after + remote_delta)

        state = build_state(store)
        parent_sha = expected_sha or _local_ref_sha(project_root)
        commit_sha = _write_ledger_ref(project_root, state, delta, parent_sha)
        _update_local_ref(project_root, commit_sha)
        _push_ref(project_root, remote, expected_sha)

        last_event_id = merged[-1].get("event_id") if merged else None
        _save_marker(store, last_event_id)
        pushed = len(local_after)
        return {
            "performed": True,
            "pushed_delta": pushed,
            "remote_delta_merged": len(new_remote),
            "local_events": len(merged),
            "commit": commit_sha,
            "delta_events": len(delta),
            "state_tasks": len(state.get("tasks", {})),
        }

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
        local_events = store.backend.read_events() if store.ledger_exists(
        ) else []
        has_remote = _fetch_ref(project_root, remote)
        remote_delta: list[dict[str, Any]] = []
        expected_sha: str | None = None
        if has_remote:
            remote_delta = _parse_delta_events(
                _read_ref_file(project_root, "FETCH_HEAD", _REF_DELTA))
            expected_sha = _fetch_head_sha(project_root)

        existing = {
            e.get("event_id")
            for e in local_events if e.get("event_id")
        }
        new_remote = [
            e for e in remote_delta
            if e.get("event_id") and e["event_id"] not in existing
        ]
        merged = _merge_events(local_events + new_remote)
        if len(merged) != len(local_events):
            _rebuild_local_ledger(store, merged)

        state = build_state(store)
        parent_sha = expected_sha or _local_ref_sha(project_root)
        commit_sha = _write_ledger_ref(project_root, state, [], parent_sha)
        _update_local_ref(project_root, commit_sha)
        _push_ref(project_root, remote, expected_sha)

        last_event_id = merged[-1].get("event_id") if merged else None
        _save_marker(store, last_event_id)
        return {
            "performed": True,
            "compacted_delta": len(remote_delta) + 0,
            "merged_from_remote": len(new_remote),
            "local_events": len(merged),
            "commit": commit_sha,
            "state_tasks": len(state.get("tasks", {})),
        }

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
        delta = _parse_delta_events(delta_text)
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
