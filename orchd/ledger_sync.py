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

import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock
from orchd.ledger import (
    _CHECKPOINT_SCHEMA_VERSION,
    _atomic_replace,
    _fsync_dir,
    FilesystemBackend,
    Store,
    event_sort_key,
)

# 专用账本 ref（本地 + 远端同名）
ORCHD_LEDGER_REF = "refs/heads/orchd/ledger"

# ref tree 内文件名
_REF_STATE = "state.json"
_REF_DELTA = "delta.jsonl"

# 本地同步 marker（账本根下，运行时状态不入 git）
_SYNC_MARKER_NAME = ".ledger_sync_marker.json"

# 远端默认名
_DEFAULT_REMOTE = "origin"

# sync 专用锁（L-4，账本根下）：Git 网络段（fetch/push 最坏约 210s）不再占账本锁，
# 并发 sync 之间由本锁串行；账本锁只在「本地快照 + 重建落盘」临界区短暂持有。
_SYNC_LOCK_NAME = ".sync.lock"
# 获取 .sync.lock 的阻塞等待上限（秒）：需覆盖最坏网络段，故显著大于账本锁 10s。
# ORCHD_SYNC_LOCK_WAIT_SECS 可覆盖（须为正数）。
_SYNC_LOCK_WAIT_SECS = 300

# ------------------------------------------------------------------
# 本地同步 marker
# ------------------------------------------------------------------


def _store_runtime_root(store: Store) -> Path:
    """账本根路径（marker/lock 与 ledger/checkpoint 同级目录）。

    ``StorageBackend`` 接口不含 ``orchd_dir``（仅 :class:`FilesystemBackend`
    在构造时持有），这里用 ``isinstance`` 收窄类型以获得该属性
    （mypy 收紧路线：orchd.ledger_sync 首个移除 ignore_errors）。
    """
    backend = store.backend
    if not isinstance(backend, FilesystemBackend):
        raise RuntimeError(f"不支持的非文件系统存储后端: {type(backend).__name__}")
    return backend.orchd_dir


def _marker_path(store: Store) -> Path:
    """同步 marker 文件路径（账本根，与 ledger/checkpoint 同级，不入 git）。"""
    return _store_runtime_root(store) / _SYNC_MARKER_NAME


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

    超时（L-4）：``subprocess.run`` 的 ``TimeoutExpired`` 转换为结构化 E007
    （含命令 / cwd / 超时秒数与处置提示），不再以未捕获异常逃逸成 E999。
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            input=input_text.encode("utf-8")
            if input_text is not None else None,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"git_timeout: git {' '.join(args[:3])} 超过 {timeout}s 未返回",
            [{
                "command": "git " + " ".join(args),
                "project_root": str(project_root),
                "timeout_s": timeout,
                "hint": "远端不可达 / 网络慢 / 仓库过大：稍后重试，或调大 _run_git 的 timeout",
            }],
        ) from exc
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


def _remote_ref_exists(project_root: Path, remote: str) -> bool | None:
    """远端 ref 存在性独立判据（不依赖 fetch 的 returncode）。

    用 ``git ls-remote <remote> <ref>`` 判定：
    - returncode == 0 且输出含 ref 名 → True（远端确实有该 ref）；
    - returncode == 0 且输出为空 → False（远端可达但尚无该 ref，首次同步路径）；
    - returncode != 0 → None（传输失败：网络不可达 / 认证失败 / 代理异常等，
      存在性不可判定，不得按“远端无 ref”静默处理）。

    Returns:
        True / False / None（三态，见上）。
    """
    r = _run_git(project_root, ["ls-remote", remote, ORCHD_LEDGER_REF])
    if r.returncode != 0:
        return None
    return ORCHD_LEDGER_REF in r.stdout


def _fetch_ref(project_root: Path, remote: str) -> bool:
    """fetch 远端 orchd/ledger ref 到 FETCH_HEAD。

    ref 存在性与 fetch 传输结果分离（task-ledger-sync-fetch-failure-semantics）：
    先以 :func:`_remote_ref_exists` 独立判定存在性，再 fetch；传输类失败以
    结构化 E007 上报，不再回退为“远端无 ref”伪成功。首次同步（远端确实无 ref）
    行为不变：返回 False，调用方仍走 remote_ref=None + note 路径。

    Returns:
        True：远端存在该 ref（FETCH_HEAD 已就位）；False：远端尚无该 ref。

    Raises:
        OrchdError(E007)：远端不存在（remote get-url 失败）、传输失败
            （ls-remote 不可判定 / fetch 在 ref 存在时失败）。
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
    exists = _remote_ref_exists(project_root, remote)
    if exists is None:
        raise OrchdError(
            ErrorCode.E007,
            f"git_sync_fetch_transport_failed: fetch 远端 '{remote}' 传输失败，"
            f"无法判定 {ORCHD_LEDGER_REF} 是否存在",
            [{
                "remote": remote,
                "ref": ORCHD_LEDGER_REF,
                "hint": "检查网络连通 / 认证 / 代理后重试；不要按“远端无 ref”处理",
            }],
        )
    if not exists:
        return False
    r = _run_git(project_root,
                 ["fetch", remote, ORCHD_LEDGER_REF, "--no-tags"])
    if r.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            f"git_sync_fetch_failed: 远端 '{remote}' 存在 {ORCHD_LEDGER_REF} "
            "但 fetch 传输失败",
            [{
                "remote": remote,
                "ref": ORCHD_LEDGER_REF,
                "detail": (r.stderr or r.stdout or "").strip()[:400],
                "hint": "检查网络连通 / 认证 / 代理后重试",
            }],
        )
    return True


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
    （review_phase / claimed_by / claimed_session / review_claimed_at /
    review_self_review）。
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
                    "review_claimed_at", "review_self_review"):
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


def _event_fingerprint(ev: dict[str, Any]) -> str:
    """事件内容确定性指纹（排除 event_id 本身）。

    用于同 event_id 异内容且排序键全等时的确定性择一（NEW-L4）：
    指纹较小者保留，不依赖各机输入序（local 在前 / remote 在前）。
    """
    payload = {k: v for k, v in ev.items() if k != "event_id"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _inject_deterministic_ids(events: list[dict[str, Any]]) -> int:
    """为缺失 event_id 的事件注入确定性 id（NEW-L3），返回注入条数。

    无 id 的远端事件此前被 new_remote 过滤静默丢弃，compact 清 delta 后
    从远端永久消失。改为基于内容哈希生成 ``evt-sync-<hash12>`` 注入，
    使其能参与常规去重合并路径；同一无 id 事件在各机生成相同 id，
    跨设备幂等。
    """
    count = 0
    for ev in events:
        if not ev.get("event_id"):
            ev["event_id"] = f"evt-sync-{_event_fingerprint(ev)[:12]}"
            count += 1
    return count


def _merge_events_detailed(
    events: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按 timestamp 稳定归并 + event_id 去重合并，并检出 id 碰撞（L-9）。

    两设备合并后的事件集相同、全局序相同 → replay 结果收敛一致。

    排序语义（L-2，用户裁决 A）：排序键为 ``ledger.event_sort_key``，即
    ``(aware 绝对时刻, 写入方标识, event_id)``：

    ① 旧实现直接对 timestamp **字符串**排序——带本地 offset 的字符串按字典序
    比较会跨时区因果倒置（``-08:00`` 机与 ``+09:00`` 机收敛到不同状态），
    「幂等收敛」契约不成立；
    ② 现按 aware 绝对时刻比较（``parse_event_time``），跨时区天然可比；旧本地
    时区/秒精度事件按带偏移绝对时刻参与比较，**不重写历史事件**；
    ③ 第二元为写入方标识（``session_id`` 回退 ``agent_id``），保证跨设备同瞬间
    事件的序一致；第三元 ``event_id`` **仅对亚秒精度事件生效**：秒精度旧事件
    保留输入顺序（稳定排序），以免按随机 id 打乱同写入方事件的语义顺序
    （CLAIMED→DONE→REVIEW_READY，2026-09-10 实踩防线；实测 48% 历史事件与
    同写入方事件撞秒，其中 382 组会被 event_id 序倒置）。
    输入约定：``local_events + new_remote``——本地在前、远端增量在后；排序键为
    事件的纯函数 + 稳定排序，故同一事件集合在任意设备上归并结果逐元素相同。

    id 碰撞检出（L-9）：event_id 相同但 ``task_id`` / ``type`` 不同 → 判为
    碰撞（旧 32 bit id 生日碰撞的典型表现），产出 E030 告警条目，**不再
    静默丢弃**（去重仍保留先到者，语义不变）。同 id 同 task/type 的重复
    行按既有语义静默去重（幂等重放，非碰撞）。

    无 event_id 的事件保守保留（不参与去重，防审计丢失）。

    Returns:
        ``(merged_events, collisions)``；collisions 为 E030 结构告警列表。
    """
    seen: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []
    for ev in sorted(
            events,
            key=event_sort_key,
    ):
        eid = ev.get("event_id", "")
        if eid:
            prev = seen.get(eid)
            if prev is not None:
                is_collision = (prev.get("task_id"),
                                prev.get("type")) != (ev.get("task_id"),
                                                      ev.get("type"))
                # NEW-L4：仅当排序键全等时才用指纹确定性择一（不依赖输入序）；
                # 排序键不同时先到者由排序决定（确定性），保留原语义。
                if event_sort_key(ev) == event_sort_key(prev):
                    if _event_fingerprint(ev) < _event_fingerprint(prev):
                        idx = out.index(prev)
                        out[idx] = ev
                        seen[eid] = ev
                        kept, dropped = ev, prev
                    else:
                        kept, dropped = prev, ev
                    tiebreak_msg = "已按内容指纹确定性保留"
                else:
                    kept, dropped = prev, ev
                    tiebreak_msg = "已保留先到事件"
                if is_collision:
                    collisions.append({
                        "code":
                        ErrorCode.E030.name,
                        "severity":
                        "warning",
                        "message": (f"event_id 碰撞：{eid} 同时对应不同 task/type，"
                                    f"疑似 32 bit id 生日碰撞；{tiebreak_msg}，请人工核对"),
                        "event_id":
                        eid,
                        "kept": {
                            "task_id": kept.get("task_id"),
                            "type": kept.get("type"),
                        },
                        "dropped": {
                            "task_id": dropped.get("task_id"),
                            "type": dropped.get("type"),
                        },
                    })
                continue
            seen[eid] = ev
        out.append(ev)
    return out, collisions


def _merge_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``_merge_events_detailed`` 的兼容包装（仅返回合并结果）。"""
    return _merge_events_detailed(events)[0]


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


def _write_sentinel_checkpoint(store: Store) -> None:
    """replace 前写入哨兵 checkpoint：声明「0 行 + 空快照」（L-1）。

    ``_rebuild_local_ledger`` 在 replace 之后才调 ``update_checkpoint``；若进程
    在两者之间崩溃/掉电，旧 checkpoint 声明的 ``ledger_line`` 会与新账本错配
    （快照不一致告警、甚至状态回退）。先把 checkpoint 降为哨兵（0 行 + 空任务），
    崩溃后 replay 必从第 1 行**全量重放**，绝不丢事件、也不会错配。

    哨兵本身不是错误状态：它只是「重建进行中」的标记，任何一次成功重建或后续
    写路径都会立刻以真实快照覆盖它。写入失败静默降级（best-effort：宁可不写
    哨兵，也不能阻断重建）。

    Args:
        store: 目标 Store。
    """
    try:
        store.backend.save_checkpoint({
            "ledger_line": 0,
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "tasks": {},
            "sentinel": "ledger_rewrite_in_progress",
        })
    except (OSError, OrchdError):
        pass


def _rebuild_local_ledger(store: Store, events: list[dict[str, Any]]) -> int:
    """把合并后（去重排序）事件原子重写本地 ledger 并重建 checkpoint。

    ledger 为事件审计唯一权威：重写保留全部事件、仅规范化确定性全局序，
    随后 replay_full → update_checkpoint 重建快照（O(任务数) checkpoint）。

    落盘序列（L-1，与 ``save_checkpoint`` 同款）：
    ``write → flush → fsync →（哨兵 checkpoint）→ _atomic_replace → 父目录 fsync``
    → ``update_checkpoint``。要点：
    - 无 flush/fsync 时掉电可致 0 字节账本（丢全部历史），故 tmp 写完即 fsync；
    - replace 前写哨兵 → 中途崩溃只会触发全量 replay，不会「旧行号 + 新账本」错配；
    - replace 走 ``_atomic_replace``（L-8：Windows 句柄争用有界重试，耗尽抛 E007）。

    Returns:
        本地事件总数。
    """
    store.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if events:
        lines = "".join(
            json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
            for ev in events)
    else:
        lines = ""
    tmp = store.ledger_path.with_suffix(".sync.tmp")
    # 保持既有文本模式写入语义（编码/换行不变），仅补 flush + fsync
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(lines)
        f.flush()
        os.fsync(f.fileno())
    _write_sentinel_checkpoint(store)
    _atomic_replace(tmp, store.ledger_path)
    _fsync_dir(store.ledger_path.parent)
    store._line_count = len(events)
    state = store.replay_full()
    store.update_checkpoint(state)
    return len(events)


def _reconcile_local(
    store: Store,
    local_events: list[dict[str, Any]],
    new_remote: list[dict[str, Any]],
    *,
    source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """合并远端增量并按【内容比较】决定是否重建本地账本（L-6）。

    旧判据是事件**条数**（``len(merged) != len(local_events)``）：当「去重丢弃」
    与「新增」条数相抵时被判为「无变化」→ 远端事件永久不落本地、两机静默发散，
    且无任何告警。改为内容比较（``merged != local_events``）后，相抵场景同样
    触发重建，并产出结构化 E030 告警（不再静默）。

    Args:
        store: 本地 Store。
        local_events: 合并前的本地事件（原序）。
        new_remote: 本次要并入的远端增量（已按 event_id 过滤本地存量）。
        source: 触发来源（pull / push / compact），写入告警上下文。

    Returns:
        ``(merged_events, warnings)``；warnings 含 id 碰撞告警与相抵重建告警。
    """
    merged, collisions = _merge_events_detailed(local_events + new_remote)
    warnings: list[dict[str, Any]] = list(collisions)
    if merged == local_events:
        # 未触发重建也要刷新软校验观测（L-3）：合并结果与本地一致时仍可能存在
        # 序列级违规（例如远端冲突事件被 event_id 去重丢弃，但冲突本身值得暴露）
        store.replay()
    else:
        if len(merged) == len(local_events):
            warnings.append({
                "code":
                ErrorCode.E030.name,
                "severity":
                "warning",
                "message": ("sync 重建判据命中原「条数相抵」盲区：合并后事件数与本地相同"
                            "但内容已变（去重丢弃与新增相抵），已按内容比较触发重建"),
                "context": {
                    "source": source,
                    "local_events": len(local_events)
                },
            })
        # 内含 replay_full → 顺带采集序列级软校验违规
        _rebuild_local_ledger(store, merged)

    # L-3（task-ledger-replay-visibility）：replay 序列级软校验违规汇总——只告警、
    # 不阻断、不改派生状态，以结构化字段（violations）挂在 warning 上，随 sync
    # 合并响应返回（pull / push / compact 的 warnings 与 replay_violations 字段）。
    violations = store.replay_violations
    if violations:
        warnings.append({
            "code":
            ErrorCode.E030.name,
            "severity":
            "warning",
            "message": (f"replay 软校验发现 {len(violations)} 条序列级不变量违规"
                        "（如跨设备双认领）——事件已照常应用，此处仅告警"),
            "source":
            source,
            "violations":
            violations,
        })
    return merged, warnings


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
        missing_id_count = _inject_deterministic_ids(remote_delta)
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
        remote_state = json.loads(state_text) if state_text else {}
        result: dict[str, Any] = {
            "performed": True,
            "pulled": len(new_remote),
            "remote_events_without_id": missing_id_count,
            "local_events": len(merged),
            "remote_ref": ORCHD_LEDGER_REF,
            "remote": _state_summary(remote_state),
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
    2. 本地新事件 = marker 之后的本地事件；新 delta = 远端存量 delta ∪ 本地新事件
       （去重），确保不丢他端尚未 compact 的事件尾；
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
        if has_remote:
            remote_delta = _parse_delta_events(
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
            delta, delta_collisions = _merge_events_detailed(local_after +
                                                             remote_delta)
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
        if has_remote:
            remote_delta = _parse_delta_events(
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
