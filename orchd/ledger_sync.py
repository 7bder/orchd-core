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
- 本地账本始终保留完整审计（文件后端载体为 ``_ledger.jsonl``；跨设备事件按
  确定性全局序归并）。

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

# 专用账本 ref（本地 + 远端同名）
ORCHD_LEDGER_REF = "refs/heads/orchd/ledger"

# ref tree 内文件名
_REF_STATE = "state.json"
_REF_DELTA = "delta.jsonl"

# 本地同步 marker（账本根下，运行时状态不入 git）
_SYNC_MARKER_NAME = ".ledger_sync_marker.json"

# ref tree 内 state.json 的快照格式版本（build_state 写入；新设备 bootstrap 校验，
# task-sync-compact-bootstrap-from-state）：加法式演进——补字段不提升版本。
_STATE_SCHEMA_VERSION = 1

# 入站整类拒收的事件类型（task-sync-inbound-force-gate，P0-3 修法②）：
# FORCE_STATUS 是**本地人工动作**——要人显式二次确认，语义绑定本机上下文
# （分支 / worktree / 锁），跨机复制等于把一端的人工善后强加给另一端；它在
# TRANSITION_TABLE 里 target=None，旧实现因此完全绕过跃迁校验（伪造终态静默落盘的
# 入口）。拒收走既有 rejected 明细 + E030 告警通道，不静默丢弃。
# task-sync-inbound-retract-authz：RETRACT 移出整类拒收，改窄口径放行（见
# ``_inbound_retract_verdict``）——入站只认本地可验证的时间事实（目标认领已
# 超时），不认入站署名与 admin（口径单一真源见
# ``docs/system-design-online.md`` §3.6）。
_INBOUND_LOCAL_ONLY_TYPES = frozenset({"FORCE_STATUS"})

# 入站拒收的判定基准二分（task-sync-push-kept-events 返工轮）：
#   absolute —— **同一引擎版本下**与设备无关地非法（非 dict / 契约违约 / 本地专用
#     类型），因此可以从共享 ref 的 delta 载体里剔除，不必让每台 push 过的设备反复
#     携带同一枚垃圾事件；
#   relative —— 只对**本机**派生态或**本机**账本非法（跃迁白矩阵、RETRACT 窄口径），
#     对状态落后的另一端可能完全合法，故只能本机不入账，不得据本机判定删掉他端
#     需要的转录项（否则一台设备获得了删除另一台设备历史事件的权力）。
# 「同一引擎版本」这个限定不是客套：``contract:`` 里含 ``unknown event type``
# （``orchd/contracts/events.py``），而 ``docs/compat-policy.md`` 许可新增事件类型——
# 升级窗口内旧引擎会把新类型判成 absolute 并把它从共享转录里删掉。跨版本收紧另立
# idea 讨论，本模块的口径以此处为准。
# 分类真源就是 ``_validate_inbound_events`` 里各条拒收点自带的 basis 键；下游只读该
# 键，禁止用 reason 字符串前缀嗅探重造一套分类。
_INBOUND_REJECT_ABSOLUTE = "absolute"
_INBOUND_REJECT_RELATIVE = "relative"

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
    """账本根路径（marker/lock 与 ledger 载体同级目录，后端无关）。

    marker（``.ledger_sync_marker.json``）与 sync 锁（``.sync.lock``）是账本根下
    的普通文件，不经 StorageBackend 端口——经 :func:`resolve_store_dir` 由
    ``store.orchd_dir`` 解析，与各后端构造时传入的目录同一来源
    （task-sync-sqlite-backend-support，B-max 阶段 3：此前按
    ``isinstance FilesystemBackend`` 硬拒非文件后端，现 marker/lock 本就与
    载体形态无关，判定改为与后端无关的目录解析）。
    """
    return resolve_store_dir(store.orchd_dir)


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
    _write_text_atomic(
        path,
        json.dumps(
            {
                "last_pushed_event_id": last_pushed_event_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=1,
        ) + "\n",
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
    review_self_review / review_claimed_by / review_claimed_session /
    merge_warning）。后三项由 task-sync-compact-bootstrap-from-state 补齐：
    新设备 bootstrap 以本快照为**唯一**派生态来源（远端 delta 已被 compact
    置空），缺项即静默丢状态，故字段集须与 ``TaskState`` 对齐。
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
                    "review_claimed_at", "review_self_review",
                    "review_claimed_by", "review_claimed_session",
                    "merge_warning"):
            val = getattr(ts, key, None)
            if val:
                entry[key] = val
        tasks[tid] = entry
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "generated_at":
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": tasks,
    }


def _parse_delta_events(text: str | None) -> tuple[list[dict[str, Any]], int]:
    """解析 delta.jsonl 文本为事件列表（空/None → 空列表）。

    非 dict 行（坏 JSON / 数字 / 字符串 / 数组 / null）显式跳过并计数，
    不再让 AttributeError 从 ``_merge_events_detailed`` 的 ``ev.get``
    逃逸导致整批同步中止。

    Returns:
        ``(events, malformed_skipped)``：事件列表与跳过的脏行数。
    """
    if not text or not text.strip():
        return [], 0
    events: list[dict[str, Any]] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        events.append(obj)
    return events, skipped


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
        payload: dict[str, Any] = {
            "ledger_line": 0,
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "tasks": {},
            "sentinel": "ledger_rewrite_in_progress",
        }
        # 快照基随哨兵原样存续（task-sync-compact-bootstrap-from-state）：重建尾部的
        # update_checkpoint 以「现存 checkpoint」判定是否保留它，哨兵若抹掉它，
        # 基就在重建过程中丢失、下一次重建退回空基 replay_full。
        prev_ckpt = store.backend.load_checkpoint() or {}
        if prev_ckpt.get("bootstrap_tasks") is not None:
            payload["bootstrap_tasks"] = prev_ckpt["bootstrap_tasks"]
        store.backend.save_checkpoint(payload)
    except (OSError, OrchdError):
        pass


def _rebuild_local_ledger(store: Store, events: list[dict[str, Any]]) -> int:
    """把合并后（去重排序）事件原子重写本地 ledger 并重建 checkpoint。

    ledger 为事件审计唯一权威：重写保留全部事件、仅规范化确定性全局序，
    随后 replay_full → update_checkpoint 重建快照（O(任务数) checkpoint）。

    写路径走 StorageBackend 端口（task-sync-sqlite-backend-support，B-max
    阶段 3）：``replace_active_events`` 全量替换活跃事件（文件后端原子重写
    活跃文件，sqlite 后端单事务重建事件表并同步计数器）——旧实现在此处做 raw
    文件手术（拼 jsonl → tmp → ``_atomic_replace`` 到 ``ledger_path``），在
    sqlite 下会把合并事件写进读侧不可见的遗留 jsonl（静默丢数据），故此前整
    个 sync 对非文件后端 E007 硬拒。端口化后双后端同一语义。

    落盘序列（L-1）：（哨兵 checkpoint）→ 端口替换 → 父目录 fsync
    → ``update_checkpoint``。要点：
    - replace 前写哨兵 → 中途崩溃只会触发全量 replay，不会「旧行号 + 新账本」错配；
    - 端口替换内部保证原子（文件 tmp+replace / sqlite 单事务），父目录 fsync
      保住目录项持久化（与旧序列同耐久）；
    - 崩溃后 checkpoint 停在哨兵（0 行）→ replay 必从第 1 行全量重放。

    归档前缀不得入活跃段（task-sync-rebuild-active-suffix）：入参 ``events`` 是
    **逻辑**事件流（读侧 = 归档前缀 + 活跃后缀，见 ``StorageBackend.read_events``
    的 ``filesystem`` 实现），而 ``replace_active_events`` 的契约只接**活跃后缀**
    （口径真源 ``orchd/storage/__init__.py``），两后端均按「归档解析数 + 本次重插
    数」记逻辑总数。喂全量会把归档段再写进活跃载体，于是后端计数器把归档事件
    重复计入（HEAD 实测 archive=10 / merged=12 时 ``event_count()`` 由 12 变 22），
    而读侧按 event_id 去重仍只见 12 条——计数器与 ``read_events()`` 背离、
    ``read_events(from, to)`` 窗口起点错位（HEAD 实测窗口只返回归档段尾部，其后的
    活跃后缀整段不见）。事件本身不丢（全量 ``replay_full()`` 恒可见）；增量
    ``replay()`` 的回退触发只以 ``orchd/ledger.py::_load_checkpoint``（:1909）
    所列两支为准（checkpoint 解析失败回退支、compact journal 在位支，
    :1926-:1948），本文件不断言第三支。故此处按归档 event_id
    **集合**切出活跃后缀（不按位置切片：合并序下归档与活跃事件可交错），并把 H4
    计数器落到「归档解析数 + 活跃后缀数」，与 ``Store.compact_archive`` 的同名赋值
    同口径（``_line_count = len(moved) + len(suffix)``，见 ``orchd/ledger.py``）——
    ``_line_count`` 语义是**逻辑总数**，不是活跃行数。

    bootstrap 快照接力（task-sync-compact-bootstrap-from-state）：本地事件流可能
    只是「compact 之后的新事件」（新设备 bootstrap 后本地账本为空，之后 pull 到
    的事件不带 pre-compact 前缀）。此时从空基 ``replay_full()`` 重建会冲掉快照
    派生态，故先取现存 checkpoint 的 ``bootstrap_tasks``（快照基键，普通
    checkpoint 无此键）：命中则以该 pre-compact 原始快照为
    ``_rebuild_after_retract`` 的基叠加本地事件；否则维持原 ``replay_full()``
    语义。

    Returns:
        本地逻辑事件总数（归档解析数 + 活跃后缀数）。
    """
    root = _store_runtime_root(store)
    root.mkdir(parents=True, exist_ok=True)
    # 基必须在写哨兵**之前**取——哨兵会把 checkpoint 覆盖成空快照。判据是
    # checkpoint 的 `bootstrap_tasks`（快照基），**不是** ledger_line==0：接力重建
    # 自身会把 ledger_line 抬到物理行数，用行号判定只能护住首次承载内容的 pull。
    # 另一关键：基恒取**原始快照** `bootstrap_tasks`，而不是会被 update_checkpoint
    # 改写的合成态 `tasks`——否则「base + 重放全部物理事件」会把已折入 base 的事件
    # 二次应用，正确性就静默依赖「事件类型幂等」这条不变量。
    prev_ckpt = store.backend.load_checkpoint() or {}
    carry: dict[str, TaskState] = {}
    raw_base = prev_ckpt.get("bootstrap_tasks")
    if isinstance(raw_base, dict):
        carry = {tid: TaskState.from_dict(ts)
                 for tid, ts in raw_base.items() if isinstance(ts, dict)}
    _write_sentinel_checkpoint(store)
    archived, _ = read_archive_events(root)
    archived_ids = {ev.get("event_id") for ev in archived if ev.get("event_id")}
    active_suffix = [ev for ev in events if ev.get("event_id") not in archived_ids]
    store.backend.replace_active_events(active_suffix)
    _fsync_dir(root)
    store._line_count = len(archived) + len(active_suffix)
    if carry:
        # 快照基 + 本地事件：走 _rebuild_after_retract 的「两遍扫描 + 以 base 为基」
        # 语义（而非 _apply_events：它遇 RETRACT 会递归重建并从空基重放，快照仍会
        # 被冲）。此时 replace 已写完，物理账本即合并后的本地事件。
        state = carry
        store._replay_violations = []
        store._rebuild_after_retract(state, set(), base=carry)
    else:
        state = store.replay_full()
    store.update_checkpoint(state)
    return store._line_count


def _bootstrap_local_from_state(
    store: Store,
    remote_state: dict[str, Any],
    local_events: list[dict[str, Any]],
    remote_delta: list[dict[str, Any]],
    state_error: str | None,
) -> dict[str, Any]:
    """新设备 bootstrap：由远端 ``state.json`` 构造本地 checkpoint（口径①）。

    task-sync-compact-bootstrap-from-state。触发形态：另一端 push 后执行
    ``compact``（远端 ``delta.jsonl`` 被置空），此后加入的新设备 ``pull`` 只能
    拿到空 delta——本地账本 0 条事件、派生态全空，而 ``state.json`` 此前只被
    用于响应回显。本函数把该快照落到本地 checkpoint，使 ``replay()`` 由快照
    起步、后续增量事件照常参与 replay：

    - **不伪造事件**：只写 checkpoint（``ledger_line=0`` 表示本地尚无事件），
      事件转录保持纯净；后续事件既参与纯 ``replay()``，也在合并重建路径
      （``_rebuild_local_ledger``，见其「bootstrap 快照接力」段）里以本快照为基
      叠加——否则首次承载内容的增量 pull 会把快照派生态冲掉；
    - **fail-closed**：仅在「远端 delta 为空 + 本地无事件 + state 可解析且
      ``schema_version`` 受支持 + tasks 形状合法」时写入；任一不满足即返回
      ``applied=False`` + 结构化 reason，**不落半态**；
    - **幂等**：重复 pull 重写同一快照，派生态不变。

    已知边界（如实声明）：``state.json`` 是派生快照，不含 RETRACT 集合与事件级
    审计（本函数不写 ``retracted``），新设备的撤回集合由后续增量事件补齐；远端
    快照自身的正确性由 compact 端负责（本函数不做跨端交叉校验）。

    Args:
        store: 本地 Store（调用方须已持账本锁）。
        remote_state: 远端 ``state.json`` 解析结果（非 dict 时传空 dict）。
        local_events: 本地现存逻辑事件（判「本地是否已有账本内容」）。
        remote_delta: 本轮远端 delta（非空即走增量路径，不进 bootstrap）。
        state_error: ``state.json`` 解析失败的分类（None 表示可解析）。

    Returns:
        ``{"applied": bool, "tasks": int, "reason": str | None}``。
    """
    if state_error is not None:
        return {"applied": False, "tasks": 0, "reason": state_error}
    if remote_delta:
        return {"applied": False, "tasks": 0, "reason": "delta_present"}
    if local_events:
        return {"applied": False, "tasks": 0, "reason": "local_events_present"}
    if not remote_state:
        return {"applied": False, "tasks": 0, "reason": "no_remote_state"}
    version = remote_state.get("schema_version")
    if version != _STATE_SCHEMA_VERSION:
        return {"applied": False, "tasks": 0,
                "reason": "state_schema_unsupported",
                "schema_version": version}
    raw_tasks = remote_state.get("tasks")
    if not isinstance(raw_tasks, dict):
        return {"applied": False, "tasks": 0, "reason": "state_tasks_malformed"}
    snapshot: dict[str, dict[str, Any]] = {}
    try:
        for tid, ts in raw_tasks.items():
            if isinstance(tid, str) and isinstance(ts, dict):
                snapshot[tid] = TaskState.from_dict(ts).to_dict()
    except (TypeError, ValueError, KeyError):
        return {"applied": False, "tasks": 0,
                "reason": "state_task_fields_malformed"}
    if not snapshot:
        return {"applied": False, "tasks": 0, "reason": "state_tasks_empty"}
    store.backend.save_checkpoint({
        "ledger_line": 0,
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "tasks": snapshot,
        # 快照基（task-sync-compact-bootstrap-from-state）：携带 **pre-compact
        # 原始快照**，供重建路径以它为基叠加物理事件、完整性校验豁免前缀比对。
        # 它必须恒为「原始」——update_checkpoint / 哨兵只保留、绝不更新其值，
        # 否则 base 会掺进本地事件的派生态，与「重放全部物理事件」二次应用。
        "bootstrap_tasks": snapshot,
    })
    return {"applied": True, "tasks": len(snapshot), "reason": None}


def _claim_stale_timeout_s() -> float:
    """CLAIMED 超时阈值（复用本地 E034 判据，避免二套阈值）。

    延迟导入 orchd.onboard.control 规避模块加载期依赖环（control 处于更高层）。
    """
    from orchd.onboard.control import claim_stale_timeout_s

    return claim_stale_timeout_s()


def _trunc(value: Any, limit: int = 200) -> Any:
    """拒收明细回显截断（_parse_delta_events 无体积上限，防超长字段灌响应）。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _inbound_retract_verdict(
    ev: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> tuple[bool, str]:
    """入站 RETRACT 窄口径判定（task-sync-inbound-retract → task-sync-inbound-retract-authz）。

    口径 A（2026-10-08 用户裁决，来源 idea:sync-inbound-trust-hygiene / pass16 P2-6）：
    **入站授权只取本地可验证的时间事实**——目标为 ``CLAIMED`` / ``REVIEW_CLAIMED``
    且已超阈值才放行，其余一律拒收。原「作者本人或 ``admin``」两条分支已删：

    - ``ev.agent_id`` 取自**入站事件自身**，``target.agent_id`` 虽取自本地账本，
      但共享 ref 的 ``delta.jsonl`` 原样携带 CLAIMED 事件的 ``agent_id``
      （``_write_ledger_ref`` 写的就是事件流），署名对任何能读该 ref 的对端都是
      公开的 ⇒ 两条分支用的都是可自证、可预知的凭据，不是授权；
    - 本地那条 ``E034`` 归属守卫（``orchd/onboard/control.py::retract``）比的是
      **会话身份**（由 session 机制约束），与入站不同源，此前 docstring 称
      「镜像本地守卫」并不成立；本地仍认 ``admin`` 属既有留账（control.py 注释
      记为 P2-11，根治需 Registry 认证机制），本卡只收窄入站侧，不对称的理由与
      代价写进 ``docs/system-design-online.md`` §3.6。

    代价与自愈路径（与边界声明同口径，勿写成「自动解」）：对端**未超时**的自撤认
    会被拒收，本端该认领停在 ``claimed``，直至超阈值后经僵尸接管通道
    （``.orchd/rules/session.md`` 接管 SOP 第 4 步，即本函数的 ``stale_claim``
    分支）或人工 ``force-status`` 处置；接管与人工动作都可被 report 的 stale-claim
    检测观测。``admin`` 的跨机纠正实际走 ``force_status`` → ``FORCE_STATUS``，
    而后者入站**整类拒收**（见 ``_INBOUND_LOCAL_ONLY_TYPES``），故删去 ``admin``
    分支不削减任何当前可达功能。

    Returns:
        ``(allowed, reason)``。
    """
    target_eid = ev.get("target_event_id")
    target = by_id.get(target_eid) if isinstance(target_eid, str) else None
    if target is None:
        return False, f"target_not_found: {target_eid!r}"
    if target.get("type") == "FORCE_STATUS":
        return False, "cannot_retract_force_status"
    ttype = target.get("type")
    age = review_claim_age_s(target.get("timestamp"))
    if ttype == "REVIEW_CLAIMED" and age is not None and age >= review_stale_timeout_s():
        return True, "stale_review_claim"
    if ttype == "CLAIMED" and age is not None and age >= _claim_stale_timeout_s():
        return True, "stale_claim"
    return False, (
        f"not_authorized: target '{target_eid}' owned by "
        f"'{target.get('agent_id')}', retractor '{ev.get('agent_id')}' "
        "（入站授权只认目标已超时）"
    )


def _validate_inbound_events(
    local_events: list[dict[str, Any]],
    new_remote: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """入站校验：远端增量逐事件过契约 + 跃迁白矩阵（fail-closed 逐条丢弃）。

    与写路径（``Store.append_event``）同判据，但语义为「丢弃 + 明细」而非抛错：
    - ``validate_event`` 直接作用于原始事件（不做 ``normalize_event``——缺 ``v``
      必须拒收，而不是被补缺省放行）；
    - ``_event_target_status`` + ``validate_transition`` 相对合并序下的运行状态
      校验（运行状态由本地事件按序派生、全 pending 起点；通过的远端事件即时
      应用状态，后续远端事件基于最新状态校验——伪造 DONE 被丢弃后不再污染
      合法 CLAIMED 的判定）；
    - ``FORCE_STATUS``（见 ``_INBOUND_LOCAL_ONLY_TYPES``）**整类拒收**：本地人工
      动作（要人二次确认），不跨机复制；且它在转移表里 ``target=None``，放行等于
      跳过跃迁校验（历史缺陷：伪造 ``completed`` 静默落盘后，此后合法 CLAIMED 被
      E007 永久拒绝）。本地事件流仍由 ``_track`` 按其 ``target_status`` 派生运行状态，
      拒收只作用于远端增量；
    - ``RETRACT`` **窄口径放行**（task-sync-inbound-retract-authz，见
      ``_inbound_retract_verdict`` 与 ``docs/system-design-online.md`` §3.6）：
      只认目标认领已超时的时间事实（``CLAIMED`` / ``REVIEW_CLAIMED`` 分走
      ``claim_stale_timeout_s`` / ``review_stale_timeout_s`` 阈值），不认入站
      署名与 ``admin``；其余拒收。
      放行的 RETRACT 经 ``_track`` 应用到派生态（目标事件从 applied 移除并重算），
      使本端回到「无活跃认领」；
    - 非 dict 输入（正常经 ``_parse_delta_events`` 已过滤，此处纵深兜底）拒收。

    非法事件逐条丢弃 + 明细返回，不抛错、不中止整库合并（可用性语义不变）。

    Returns:
        ``(kept, rejected)``：保留事件与拒收明细
        （``event_id`` / ``type`` / ``task_id`` / ``reason`` / ``basis``）。``basis``
        取 ``absolute``（同一引擎版本下与设备无关地非法，可自共享转录剔除）或
        ``relative``（只对本机非法，必须继续携带），是 push 组 delta 排除面的唯一真源。
    """
    status: dict[str, str] = {}
    # task-sync-inbound-retract：放行的 RETRACT 要应用到派生态，否则后续合法
    # CLAIMED 被跃迁校验误拒（目标认领撤销后本端应回 pending）。按 task 维护
    # 已应用（未被撤回）事件序，RETRACT 时移除目标事件并重算该任务状态。
    applied: dict[str, list[dict[str, Any]]] = {}

    def _recompute(tid: str) -> None:
        st = "pending"
        for e in applied.get(tid, []):
            if e.get("type") == "FORCE_STATUS":
                ts = e.get("target_status", "pending")
                st = ts if isinstance(ts, str) else "pending"
            else:
                tgt = _event_target_status(e)
                if tgt is not None:
                    st = tgt
        status[tid] = st

    def _track(ev: dict[str, Any]) -> None:
        tid = ev.get("task_id")
        if not tid or not isinstance(tid, str):
            return
        if ev.get("type") == "RETRACT":
            target_eid = ev.get("target_event_id")
            applied[tid] = [
                e for e in applied.get(tid, [])
                if e.get("event_id") != target_eid
            ]
            _recompute(tid)
            return
        applied.setdefault(tid, []).append(ev)
        if ev.get("type") == "FORCE_STATUS":
            target_status = ev.get("target_status", "pending")
            status[tid] = (target_status if isinstance(target_status, str)
                           else "pending")
            return
        target = _event_target_status(ev)
        if target is not None:
            status[tid] = target

    # 事件按 event_id 索引：供入站 RETRACT 解析被撤目标（本地 + 已保留远端）。
    by_id: dict[str, dict[str, Any]] = {}
    for local_ev in local_events:
        if isinstance(local_ev, dict):
            # task-ledger-sync-mypy-index-fix：取出并 isinstance 收窄为 str——
            # `.get()` 静态类型为 Any|None，`if local_ev.get(...)` 只证真值不收窄，
            # 直接索引 dict[str, ...] 触发 mypy [index] 红（event_id 契约恒为 str）。
            _ev_id = local_ev.get("event_id")
            if isinstance(_ev_id, str) and _ev_id:
                by_id[_ev_id] = local_ev
            _track(local_ev)

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    dict_remote: list[dict[str, Any]] = []
    for remote_ev in new_remote:
        if not isinstance(remote_ev, dict):
            rejected.append({
                "event_id": None,
                "type": None,
                "task_id": None,
                "reason": "not_a_dict: 远端事件不是 JSON 对象",
                "basis": _INBOUND_REJECT_ABSOLUTE,
            })
            continue
        dict_remote.append(remote_ev)
    for ev in sorted(dict_remote, key=event_sort_key):
        eid = ev.get("event_id")
        etype = ev.get("type")
        tid = ev.get("task_id")
        violations = validate_event(ev)
        if violations:
            rejected.append({
                "event_id": eid,
                "type": etype,
                "task_id": tid,
                "reason": "contract: " + "; ".join(violations),
                "basis": _INBOUND_REJECT_ABSOLUTE,
            })
            continue
        if etype in _INBOUND_LOCAL_ONLY_TYPES:
            # 整类拒收（本地人工动作不跨机复制）：不入 kept、不 _track，
            # 明细挂 E030 的 rejected_events，避免伪造终态静默污染后续判定。
            # task-sync-inbound-retract：仅 FORCE_STATUS 仍整类拒收；RETRACT 走
            # 下方窄口径（_inbound_retract_verdict）。
            rejected.append({
                "event_id": eid,
                "type": etype,
                "task_id": tid,
                "reason": (
                    "local_only: 本地人工动作不跨机复制，整类拒收"
                    f"（target_status={_trunc(ev.get('target_status'))!r}）"
                ),
                "basis": _INBOUND_REJECT_ABSOLUTE,
                "target_status": _trunc(ev.get("target_status")),
            })
            continue
        if etype == "RETRACT":
            allowed, verdict_reason = _inbound_retract_verdict(ev, by_id)
            if not allowed:
                rejected.append({
                    "event_id": eid,
                    "type": etype,
                    "task_id": tid,
                    "reason": f"retract_narrow: {verdict_reason}",
                    "basis": _INBOUND_REJECT_RELATIVE,
                    "target_event_id": _trunc(ev.get("target_event_id")),
                })
                continue
            if eid:
                by_id[eid] = ev
            _track(ev)
            kept.append(ev)
            continue
        target = _event_target_status(ev)
        if tid and target:
            try:
                validate_transition(
                    ev.get("type", ""),
                    status.get(tid, "pending") if isinstance(tid, str)
                    else "pending",
                    target,
                )
            except OrchdError as exc:
                rejected.append({
                    "event_id": eid,
                    "type": etype,
                    "task_id": tid,
                    "reason": f"transition: {exc.message}",
                    "basis": _INBOUND_REJECT_RELATIVE,
                })
                continue
        if eid:
            by_id[eid] = ev
        _track(ev)
        kept.append(ev)
    return kept, rejected


def _count_inbound_rejected(warnings: list[dict[str, Any]]) -> int:
    """从合并告警中汇总入站校验拒收数（调用方结果字段用）。"""
    total = 0
    for warning in warnings:
        context = warning.get("inbound_validation")
        if isinstance(context, dict):
            total += int(context.get("rejected", 0) or 0)
    return total


def _inbound_rejected_ids(warnings: list[dict[str, Any]]) -> set[str]:
    """从合并告警中取**绝对非法**拒收事件的 event_id 集合（push 组 delta 排除用）。

    与 :func:`_count_inbound_rejected` 同一明细源（E030 的
    ``inbound_validation.rejected_events``，构造点见 ``_reconcile_local``）——计数与
    点名共用一份真源，不另设第二套判定。

    只点名 ``basis == "absolute"`` 那批（同一引擎版本下与设备无关地非法：非 dict /
    契约违约 / 本地专用类型）。``relative``（跃迁白矩阵、RETRACT 窄口径）的判定基准是本机派生态
    与本机账本，同一事件对状态落后的另一端完全合法——按它删共享转录，等于给一台
    设备删除另一台设备历史事件的权力。条目缺 ``basis`` 键（旧告警、手构明细）一律
    按 relative 处理：不确定就继续携带，转录完整性优先于净化。
    """
    ids: set[str] = set()
    for warning in warnings:
        context = warning.get("inbound_validation")
        if not isinstance(context, dict):
            continue
        for entry in context.get("rejected_events") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("basis") != _INBOUND_REJECT_ABSOLUTE:
                continue
            if entry.get("event_id"):
                ids.add(str(entry["event_id"]))
    return ids


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
        ``(merged_events, warnings)``；warnings 含 id 碰撞告警、相抵重建告警、
        入站校验拒收告警（E030，拒收明细挂 ``inbound_validation``）。
    """
    kept_remote, inbound_rejected = _validate_inbound_events(
        local_events, new_remote)
    merged, collisions = _merge_events_detailed(local_events + kept_remote)
    warnings: list[dict[str, Any]] = list(collisions)
    # 接触即纠接入（task-counter-cache-reconcile D2）：既存计数漂移进入本
    # 汇聚点即归位并留 info 级痕——幂等分支（merged == local）原先对漂移完全
    # 静默（V3 实锤形态），此后每次 sync 都是纠偏触点。与下方 counter_drift
    # 重建告警正交：那条比「缓存 vs 去重逻辑流」（承载重复探测），本条比
    # 「缓存 vs 实体物理数」（缓存归位），两层哨兵各司其职。
    rec = store.reconcile_counters()
    if rec.get("drift"):
        warnings.append({
            "code": "counter_recalibrated",
            "severity": "info",
            "source": source,
            "message": (
                "sync 接触即纠：计数缓存漂移 "
                f"{rec.get('from')} → 实体真值 {rec.get('to')} 已归位"
                if rec.get("recalibrated") else
                "计数缓存漂移 "
                f"{rec.get('from')} ≠ 实体 {rec.get('to')}——"
                "当前为只读通道未归位，可写进程接触即自愈"
            ),
            "counter_recalibrate": {
                "from": rec.get("from"),
                "to": rec.get("to"),
                "repaired": bool(rec.get("recalibrated")),
            },
        })
    if inbound_rejected:
        warnings.append({
            "code":
            ErrorCode.E030.name,
            "severity":
            "warning",
            "message": (f"sync 入站校验拒收 {len(inbound_rejected)} 条非法远端事件"
                        "（逐条丢弃，整库合并继续）"),
            "source":
            source,
            "inbound_validation": {
                "rejected": len(inbound_rejected),
                "kept": len(kept_remote),
                "rejected_events": inbound_rejected,
            },
        })
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

        # 计数不变量（task-sync-rebuild-active-suffix）：后端持久计数器与读侧逻辑流
        # 必须同数——活跃段重复承载归档前缀时，event_count 把归档事件二次计入，而
        # read_events 按 event_id 去重掩盖了它，两者当场背离。重建后立刻核对，不等
        # 下一个进程（或另一台设备）去发现。只在重建分支查：幂等重复合并不落盘，
        # 无需为此多付一遍读侧成本。
        _counted = store.backend.event_count()
        _read_len = len(store.backend.read_events())
        if _counted != _read_len:
            _archive_n = len(read_archive_events(_store_runtime_root(store))[0])
            warnings.append({
                "code":
                ErrorCode.E030.name,
                "severity":
                "warning",
                "message": (
                    f"sync 重建后计数背离：后端 event_count={_counted} 与 "
                    f"len(read_events())={_read_len} 不符"
                    "（活跃段疑似重复承载归档前缀，读侧靠 event_id 去重掩盖）"
                ),
                "source":
                source,
                "counter_drift": {
                    "archive_events": _archive_n,
                    "active_events": _counted - _archive_n,
                    "logical_total": _read_len,
                    "backend_event_count": _counted,
                    "read_events_len": _read_len,
                },
            })

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
