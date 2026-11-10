"""worktree recycle cleanup 域（W6-C5 纯搬移）。

来源：``orchd/worktree/recycle.py``（1023 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
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



# task-cont-dirty-worktree-ladder：脏 worktree 五级降级回收。
# L1 干净/仅 ignored → 普通 remove；L2 脏 → 救援提交 + orchd/rescue ref；
# L3 救援失败 → 整目录隔离到 .orchd/.doctor-backup/worktrees/；L4 移动失败 →
# bundle+patch 备份；L5 仍失败 → 原地保留 + manual notice。仅
# discard_uncommitted + discard_confirmed 双旗标（CLI --discard-uncommitted
# --confirm）才允许 force 丢弃；普通 --force-recycle 只删未合并分支。
_RESCUE_REF_PREFIX = "refs/heads/orchd/rescue"
_DIRTY_REFUSAL_MARKER = "modified or untracked"
_DIRTY_FILE_SNAPSHOT_CAP = 50
# task-cont-dirty-ladder-zero-loss（SC1）：ignored 文件分两类——
# 可重建缓存（删了能再生成，不触发脏梯）与用户文件（默认按用户文件处理，
# 触发 L2 救援复制）。判定不了的路径一律按用户文件处理。
_REBUILDABLE_CACHE_DIRS = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".tox",
    ".venv",
    "dist",
    "build",
})
_REBUILDABLE_CACHE_SUFFIXES = (".pyc",)
# L4 镜像文件数上限：超限视为镜像失败（调用方继续 L5，不 rmtree）。
_L4_MIRROR_FILE_CAP = 5000


def _is_rebuildable_cache_path(rel: str) -> bool:
    """ignored 路径是否属于可重建缓存（目录段命中或 ``*.pyc`` 后缀）。"""
    norm = rel.replace("\\", "/").strip()
    if not norm:
        return False
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    if any(p in _REBUILDABLE_CACHE_DIRS for p in parts):
        return True
    return parts[-1].endswith(_REBUILDABLE_CACHE_SUFFIXES)


def _porcelain_path(raw: str) -> str | None:
    """porcelain 行路径解码：去掉引号转义，保留 git 的 ``/`` 分隔、去尾斜杠。"""
    text = raw.strip()
    if not text:
        return None
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            text = json.loads(text)
        except (ValueError, TypeError):
            # 非 JSON 可解析的引号形态：无法判定 → 去掉引号按用户文件处理。
            text = text[1:-1]
    return text.rstrip("/") or None


def _wt_dirty_snapshot(wt_path: Path) -> dict[str, Any] | None:
    """worktree 脏快照（porcelain v1，含 untracked 与 ignored）。

    清单来源：
    ``git status --porcelain=v1 --ignored=matching --untracked-files=all``。
    输出区分 tracked 改动、untracked、ignored 缓存与 ignored 用户文件；
    无法判定属于哪类的 ignored 路径一律归入用户文件。

    Returns:
        结构化快照；``None`` = status 不可得（git 失败/超时）。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain=v1",
             "--ignored=matching", "--untracked-files=all"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    tracked: list[str] = []
    untracked: list[str] = []
    ignored: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        code = line[:2]
        rel = _porcelain_path(line[3:])
        if rel is None:
            continue
        if code == "??":
            untracked.append(rel)
        elif code.strip() == "!!":
            ignored.append(rel)
        else:
            tracked.append(rel)
    cache_ignored = [p for p in ignored if _is_rebuildable_cache_path(p)]
    user_ignored = [p for p in ignored if not _is_rebuildable_cache_path(p)]
    files = (tracked + untracked + ignored)[:_DIRTY_FILE_SNAPSHOT_CAP]
    return {
        "ok": True,
        "tracked": tracked,
        "untracked": untracked,
        "ignored": ignored,
        "cache_ignored": cache_ignored,
        "user_ignored": user_ignored,
        "files": files,
        # 仅缓存 ignored 不触梯；tracked / untracked / ignored 用户文件均判脏。
        "dirty": bool(tracked) or bool(untracked) or bool(user_ignored),
    }


def _wt_dirty_files(wt_path: Path) -> list[str] | None:
    """worktree 脏文件快照（兼容入口，含 ignored；截断到 50 条用于留痕）。

    Returns:
        非空列表 = 有未提交改动或 ignored 文件（含缓存）；空列表 = 干净；
        ``None`` = status 不可得（git 失败/超时），调用方回退 stderr 归因。
    """
    snapshot = _wt_dirty_snapshot(wt_path)
    if snapshot is None:
        return None
    return snapshot["files"]


def _ladder_timestamp() -> str:
    """UTC 时间戳（ref/目录名安全形态），同一秒内多次调用也保持单调可读。"""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _rescue_ref_name(task_id: str) -> str:
    """L2 救援 ref 名：refs/heads/orchd/rescue/<task_id>-<UTC>。"""
    return f"{_RESCUE_REF_PREFIX}/{task_id}-{_ladder_timestamp()}"


def _backup_root(stable_wt: Path) -> Path:
    """隔离/备份桶根：<主工作树>/.orchd/.doctor-backup/worktrees/。"""
    return Path(stable_wt) / ".orchd" / ".doctor-backup" / "worktrees"


def _l2_rescue_commit(
    stable_wt: Path,
    wt_path: Path,
    task_id: str,
    recycle_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """L2：脏工作区救援提交（纯 plumbing，不动 HEAD / 任务分支 / 工作区文件）。

    临时索引（``GIT_INDEX_FILE`` 指向系统临时目录，不触碰 worktree 自身 index）
    ``read-tree HEAD`` 播种 → ``add -A`` 收编改动与 untracked（ignored 除外）→
    ``write-tree`` → ``commit-tree -p HEAD`` → ``update-ref`` 写入
    ``refs/heads/orchd/rescue/<task_id>-<UTC>``。此后 ``--force`` 移除不再丢数据。
    任一步失败即整体失败（返回 ``ok=False``），调用方降级 L3。
    """
    fd, tmp_index = tempfile.mkstemp(prefix="orchd-rescue-", suffix=".index")
    os.close(fd)
    ref = _rescue_ref_name(task_id)
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = tmp_index

    def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv, cwd=str(cwd), capture_output=True, encoding="utf-8",
            errors="replace", timeout=30, env=env,
        )

    def _err(proc: subprocess.CompletedProcess) -> str:
        return ((proc.stderr or "") or (proc.stdout or "")).strip()[:300]

    try:
        head = _run(["git", "rev-parse", "--verify", "HEAD"], wt_path)
        if head.returncode != 0:
            return {"ok": False, "error": _err(head), "ref": ref}
        read_tree = _run(["git", "read-tree", "HEAD"], wt_path)
        if read_tree.returncode != 0:
            return {"ok": False, "error": _err(read_tree), "ref": ref}
        add = _run(["git", "add", "-A", "--", "."], wt_path)
        if add.returncode != 0:
            return {"ok": False, "error": _err(add), "ref": ref}
        wtree = _run(["git", "write-tree"], wt_path)
        if wtree.returncode != 0:
            return {"ok": False, "error": _err(wtree), "ref": ref}
        tree = (wtree.stdout or "").strip()
        msg = (
            f"orchd rescue: preserve uncommitted changes of {task_id} "
            f"before worktree recycle"
        )
        cmt = _run(["git", "commit-tree", tree, "-p", "HEAD", "-m", msg], wt_path)
        if cmt.returncode != 0:
            return {"ok": False, "error": _err(cmt), "ref": ref}
        commit = (cmt.stdout or "").strip()
        upd = _run(["git", "update-ref", ref, commit], stable_wt)
        if upd.returncode != 0:
            return {"ok": False, "error": _err(upd), "ref": ref}
        return {"ok": True, "ref": ref, "commit": commit}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "ref": ref}
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass


def _l2_backup_root(backup_root: Path, task_id: str) -> Path:
    """L2 用户 ignored 文件救援目录：``<backup_root>/<task>-<UTC>-l2-files/``。"""
    target = backup_root / f"{task_id}-{_ladder_timestamp()}-l2-files"
    while target.exists():  # 同秒碰撞：后缀化，绝不嵌套进已有桶
        target = target.with_name(f"{target.name}-{os.getpid() % 10000:04d}")
    return target


def _l2_backup_ignored_files(
    wt_path: Path,
    backup_root: Path,
    task_id: str,
    rel_paths: list[str],
) -> dict[str, Any]:
    """L2：在 ``git worktree remove --force`` 前复制 ignored 用户文件。

    保留相对路径（目录条目整棵 copytree，文件条目 copy2）；复制失败即
    ``ok=False``——调用方不得 force remove，必须降级 L3 整目录隔离。
    """
    target = _l2_backup_root(backup_root, task_id)
    copied: list[str] = []
    failed: list[str] = []
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "dir": str(target),
            "copied": copied,
            "failed": failed,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }
    for rel in rel_paths:
        src = wt_path / rel
        dst = target / rel
        try:
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src), str(dst))
            else:
                # 符号链接 / 已消失 / 非普通文件：无法判定 → 按失败处理。
                failed.append(f"{rel}: not_regular_file")
                continue
        except (OSError, shutil.Error) as exc:
            failed.append(f"{rel}: {type(exc).__name__}: {exc}"[:300])
            continue
        copied.append(rel)
    return {
        "ok": not failed,
        "dir": str(target),
        "copied": copied,
        "failed": failed,
        "error": (f"copy_failed: {failed[0]}"[:300] if failed else None),
    }


def _l3_isolate_dir(wt_path: Path, backup_root: Path, task_id: str) -> dict[str, Any]:
    """L3：整目录隔离——把 worktree 目录原样搬进备份桶（同卷 rename 优先）。

    搬走后对 recorded path 而言目录已消失，``git worktree prune``（调用方执行）
    即可收敛陈旧登记；目录内容逐字节保留、可人工回捞。
    """
    target = backup_root / f"{task_id}-{_ladder_timestamp()}"
    while target.exists():  # 同秒碰撞：后缀化，绝不嵌套进已有桶
        target = target.with_name(f"{target.name}-{os.getpid() % 10000:04d}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(wt_path), str(target))
        return {"ok": True, "target": target}
    except (OSError, shutil.Error) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                "target": target}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _l4_mirror_files(
    wt_path: Path,
    backup_dir: Path,
    rel_paths: list[str],
) -> dict[str, Any]:
    """L4：把 untracked + ignored 用户文件镜像到 ``<backup>/files/``。

    同步产出 ``manifest.json``（相对路径 / 大小 / sha256）。镜像失败或文件数
    超过 ``_L4_MIRROR_FILE_CAP`` 即 ``ok=False``——调用方不得 rmtree，继续 L5。
    """
    files_root = backup_dir / "files"
    manifest: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        files_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False,
            "files_dir": None,
            "manifest": None,
            "entries": [],
            "errors": [f"{type(exc).__name__}: {exc}"[:300]],
        }

    cap_exceeded = False

    def _mirror_one(rel: str) -> None:
        nonlocal cap_exceeded
        if cap_exceeded:
            return
        if len(manifest) >= _L4_MIRROR_FILE_CAP:
            cap_exceeded = True
            errors.append(
                f"mirror_file_cap_exceeded(>{_L4_MIRROR_FILE_CAP}): {rel}"[:300])
            return
        src = wt_path / rel
        dst = files_root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            manifest.append({
                "path": rel.replace("\\", "/"),
                "size": dst.stat().st_size,
                "sha256": _sha256_file(dst),
            })
        except (OSError, shutil.Error) as exc:
            errors.append(f"{rel}: {type(exc).__name__}: {exc}"[:300])

    for rel in rel_paths:
        src = wt_path / rel
        if src.is_symlink():
            # 设计 §6.5：非普通文件、符号链接不碰；跳过并留痕，不阻断镜像。
            continue
        if src.is_dir():
            for dirpath, _dirnames, filenames in os.walk(str(src)):
                for name in filenames:
                    child = Path(dirpath) / name
                    if child.is_symlink():
                        continue
                    _mirror_one(child.relative_to(wt_path).as_posix())
        elif src.is_file():
            _mirror_one(rel.replace("\\", "/"))
        else:
            # 快照里有、落盘已消失：无法判定 → 镜像失败（保守，不删）。
            errors.append(f"{rel}: missing_at_mirror_time")

    manifest_path = backup_dir / "manifest.json"
    try:
        manifest_path.write_text(
            json.dumps(
                {"file_count": len(manifest), "files": manifest},
                ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        errors.append(f"manifest.json: {type(exc).__name__}: {exc}"[:300])
        manifest_path = None  # type: ignore[assignment]

    return {
        "ok": not errors,
        "files_dir": str(files_root),
        "manifest": str(manifest_path) if manifest_path else None,
        "entries": manifest,
        "errors": errors,
    }
