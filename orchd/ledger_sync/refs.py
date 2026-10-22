"""ledger sync — git ref 底层与同步标记域（W6-C2 拆分，纯搬移）。

来源：``orchd/ledger_sync.py``（1622 行，W6-C2 纯搬移）。

``_run_git`` 族 / ledger ref 读写推拉 / 同步 marker 读写。

**叶子域**：零包内相对导入（不反向依赖任何兄弟子模块，否则成加载期环）。
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


# 远端默认名
_DEFAULT_REMOTE = "origin"


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
