"""worktree layout migration 域（W6-C5 纯搬移）。

来源：``orchd/worktree/layout.py``（1079 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from .marker import (
    _CONTAINER_MAIN_DIR,
    _RUNTIME_DIR,
    _TRASH_DIR,
    marker_path,
    write_layout,
)

from .gitinfo import (
    _GIT_TIMEOUT,
)



# 保守版无效文件清单（task-14-layout-migrate-junk-clean）：可再生的 OS 杂项 /
# 缓存 / 覆盖产物 / 临时 / 日志。迁移时隔离回收至 trash/ 而非带进 main/，
# 避免目录污染；绝不触碰 .git / .orchd / 工具目录 / venv / 被跟踪文件。
_JUNK_NAMES = frozenset({
    # OS 杂项
    ".DS_Store", "Thumbs.db",
    # 缓存
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    # 覆盖产物
    ".coverage", "htmlcov",
    # 临时
    ".pytest-tmp", ".tmp-pytest",
    # 引擎自动同步日志（可再生）
    ".orchd-core-sync.log",
})

_JUNK_PREFIXES = ("_tmp_", "pytest_tmp_")


# P2-8：迁移时绝不搬入 main/ 的 venv / IDE 目录（环境专属，非项目源码内容）。
# 注：.trae/.workbuddy 等工具配置目录照常移入 main/（test_migrate_keeps_tool_and_tracked 断言）。
_TOOL_DIR_NAMES = frozenset({".venv", ".idea", ".vscode"})



def _is_junk_entry(name: str) -> bool:
    """顶层条目是否为保守版无效文件（可再生，迁移时隔离回收而非移入 main/）。"""
    if name in _JUNK_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _JUNK_PREFIXES)



# 运行时账本文件（task-14-layout-migrate-ledger-move）：容器布局默认账本根为
# <容器>/.orchd-runtime/，迁移时须从 main/.orchd/ 搬入，否则历史对引擎不可见。
# master 目录文件（_master.json/shared/rules/IDEAS/ROADMAP/SKILL/templates/
# proposals/merge-acks.json/.layout.json）保留在 main/.orchd/，不在此列。
# task-migrate-sqlite-carrier：sqlite 载体一并搬运（库 + WAL 旁瓣；同目录
# 搬移、无 Store 占用，失败走既有完整回滚；-shm 为划痕可重建，随库同搬无害）。
_LEDGER_RUNTIME_FILES = (
    "_ledger.jsonl", "_checkpoint.json", ".lock", ".session.lock",
    "session-worktrees.json",
    "_ledger.sqlite3", "_ledger.sqlite3-wal", "_ledger.sqlite3-shm",
)



def _move_ledger_runtime_files(
    orchd_dir: Path, runtime_root: Path
) -> tuple[list[str], list[dict[str, Any]]]:
    """把运行时账本文件从 ``<main>/.orchd/`` 搬到 ``<runtime>/``（best-effort）。

    迁移 flat→container 后，``resolve_store_dir`` 默认读 ``<容器>/.orchd-runtime/``，
    若历史账本仍留在 ``main/.orchd/``，引擎将看不到既有任务状态。此函数把
    ledger / checkpoint / 锁 / session-worktrees / mod-* 搬到 runtime 根，
    保证迁移一次成功后任务历史立即可见。缺失的条目跳过，搬移失败记入
    ``errors`` 交由调用方回滚/上报（W-11 起不再静默跳过）。

    Returns:
        ``(moved, errors)``：已搬移的文件/目录名清单（相对名），以及搬移失败项
        （``{"name", "error"}``）——W-11 起失败不再静默吞掉，由调用方决定回滚/上报。
    """
    moved: list[str] = []
    errors: list[dict[str, Any]] = []
    _intake_lock_name = _intake_lock_filename()
    for name in _LEDGER_RUNTIME_FILES:
        if name == _intake_lock_name:
            # W-11：迁移全程持 .intake.lock（串行化准入写）——**不搬移持锁文件本身**
            # （Windows 上搬移被本进程句柄占用的文件会失败；且搬走后旧 fd 与新路径
            # 语义分裂）。下次准入写会在新 canonical 路径自动重建该锁。
            continue
        src = orchd_dir / name
        if src.exists():
            try:
                shutil.move(str(src), str(runtime_root / name))
                moved.append(name)
            except OSError as exc:
                # W-11：搬移失败不再静默——记入 errors 供调用方回滚与上报
                errors.append({
                    "name": name,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    for p in sorted(orchd_dir.glob("mod-*")):
        try:
            shutil.move(str(p), str(runtime_root / p.name))
            moved.append(p.name)
        except OSError as exc:
            errors.append({
                "name": p.name,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return moved, errors



def _rollback_layout_migration(
    project_root: Path,
    main_dir: Path,
    runtime: Path,
    moved: list[str],
    ledger_moved: list[str] | None = None,
) -> list[dict[str, Any]]:
    """回滚半迁移状态（W-11）：账本文件移回 → 删标记 → ``.orchd`` 逐条移回 →
    其余条目移回 → 清空目录。

    ``.orchd`` 必须**逐条**移回（不能目录级 move：目标 ``project_root/.orchd``
    已存在且仍持有 .intake.lock，Windows 下目录级 move 会失败/冲突）。

    返回**回滚失败项** ``[{"item", "error"}]``：迁移/回滚过程中的 OSError 一律上报
    调用方（旧实现 ``except OSError: pass`` 静默吞掉，留下无人知晓的半迁移残留）。
    """
    errors: list[dict[str, Any]] = []
    new_orchd = main_dir / ".orchd"
    orig_orchd = project_root / ".orchd"
    for name in list(ledger_moved or []):
        src = runtime / name
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(new_orchd / name))
        except OSError as exc:
            errors.append({"item": f"ledger:{name}",
                           "error": f"{type(exc).__name__}: {exc}"})
    try:
        marker = marker_path(new_orchd)
        if marker.exists():
            marker.unlink()
    except OSError as exc:
        errors.append({"item": "marker", "error": f"{type(exc).__name__}: {exc}"})
    if new_orchd.is_dir():
        orig_orchd.mkdir(parents=True, exist_ok=True)
        for sub in sorted(new_orchd.iterdir()):
            dst = orig_orchd / sub.name
            if dst.exists():
                continue  # 原位置已有同名条目（.intake.lock 等）→ 保留原物
            try:
                shutil.move(str(sub), str(dst))
            except OSError as exc:
                errors.append({"item": f".orchd/{sub.name}",
                               "error": f"{type(exc).__name__}: {exc}"})
    for name in list(moved):
        if name == ".orchd":
            continue
        src = main_dir / name
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(project_root / name))
        except OSError as exc:
            errors.append({"item": name, "error": f"{type(exc).__name__}: {exc}"})
    for d in (new_orchd, main_dir, runtime):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError as exc:
            errors.append({"item": f"rmdir:{d.name}",
                           "error": f"{type(exc).__name__}: {exc}"})
    return errors



def _intake_lock_filename() -> str:
    """准入锁文件名（单一事实源：``orchd.ledger._INTAKE_LOCK_FILENAME``）。"""
    try:
        from orchd.ledger import _INTAKE_LOCK_FILENAME

        return _INTAKE_LOCK_FILENAME
    except Exception:
        return ".intake.lock"



def layout_migrate(project_root: Path) -> dict[str, Any]:
    """flat → container 迁移**入口**（W-11：全程持 ``.intake.lock`` 串行化）。

    与并发 amend / intake 写（同一把 ``.intake.lock``）互斥：迁移会搬移
    ``.orchd`` 运行时文件并写布局标记，无锁并发会留下「锁根与账本根分裂」的
    半迁移态。取锁失败即拒绝迁移（**不做无锁迁移**），返回结构化原因。

    Args:
        project_root: flat 主工作树根。

    Returns:
        迁移实现 :func:`_layout_migrate_impl` 的结果；取锁失败时为
        ``{"migrated": False, "reason": "intake_lock_unavailable", ...}``。
    """
    from orchd.ledger import intake_lock_acquire, intake_lock_release

    project_root = Path(project_root).resolve()
    try:
        lock = intake_lock_acquire(project_root / ".orchd", "layout-migrate")
    except Exception as exc:
        return {
            "migrated": False,
            "reason": "intake_lock_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "准入锁不可得（并发准入写进行中或锁损坏），稍后重试",
        }
    try:
        return _layout_migrate_impl(project_root)
    finally:
        try:
            intake_lock_release(lock)
        except Exception:
            pass



def _layout_migrate_impl(project_root: Path) -> dict[str, Any]:
    """迁移实现（调用方须已持 ``.intake.lock``，见 :func:`layout_migrate`）。

    flat → container 迁移：内容整体移入 ``main/``，保留 git 历史、可回滚（AC4）。

    前置校验（AC4）：工作区干净（无已跟踪改动）、无活跃 linked worktree 冲突。
    迁移过程 best-effort：任一步骤失败则尝试把已移动条目移回（可回滚，不破坏
    原仓库）；成功输出受影响路径清单。

    无效文件自动清理（task-14-layout-migrate-junk-clean）：迁移前把保守版
    无效文件清单（OS 杂项/缓存/临时/日志，见 ``_JUNK_NAMES``/``_is_junk_entry``）
    隔离回收至 ``<runtime>/trash/``（可还原），而非带进 ``main/`` 造成目录污染；
    清理条目记入 ``cleaned``。绝不清理 ``.git`` / ``.orchd`` / 工具目录 / venv /
    被跟踪文件。

    运行时账本搬运（task-14-layout-migrate-ledger-move）：容器布局默认账本根为
    ``<runtime>/``，迁移后把历史账本（ledger/checkpoint/锁/session-worktrees/mod-*）
    从 ``main/.orchd/`` 搬到 ``<runtime>/``（见 ``_move_ledger_runtime_files``），
    保证迁移一次成功后引擎对既有任务状态立即可见；master 目录文件保留在
    ``main/.orchd/``。搬移条目记入 ``ledger_moved``。

    Args:
        project_root: flat 主工作树根（git 根，含 ``.git`` / ``.orchd``）。

    Returns:
        ``{"migrated": True, "main_worktree": <Path>, "moved": [<str>],
        "cleaned": [<str>], "ledger_moved": [<str>], "marker": <Path>,
        "runtime_root": <Path>, "trash_root": <Path>（有清理时）}``
        或 ``{"migrated": False, "reason": <str>, ...}``（前置校验失败）。
    """
    from orchd.gitops import check_workspace_state

    project_root = Path(project_root).resolve()
    main_dir = project_root / _CONTAINER_MAIN_DIR

    # 前置校验：工作区干净
    state = check_workspace_state(project_root)
    if state.get("available") and not state.get("clean"):
        return {
            "migrated": False,
            "reason": "dirty_workspace",
            "hint": "迁移要求工作区干净（无已跟踪改动），请先提交或还原后再试",
        }

    # 前置校验：无活跃 linked worktree（并发 worktree 冲突）
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        # W-16：按**行前缀**计数（``worktree <path>`` 块头），不再用子串
        # ``count("worktree ")``——``locked`` / ``prunable`` 行或路径里含该子串
        # 都会被误计，导致有 linked worktree 时判「无」或反之。
        if proc.returncode == 0 and len([
            ln for ln in (proc.stdout or "").splitlines()
            if ln.startswith("worktree ")
        ]) > 1:
            return {
                "migrated": False,
                "reason": "active_worktrees",
                "hint": "存在活跃 worktree，先清理（orchd doctor --prune-worktrees 或 git worktree prune）再迁移",
            }
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    main_dir.mkdir(parents=True, exist_ok=True)
    runtime = project_root / _RUNTIME_DIR
    runtime.mkdir(parents=True, exist_ok=True)
    trash_dir = runtime / _TRASH_DIR
    trash_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    cleaned: list[str] = []

    # 迁移：先把保守版无效文件（OS 杂项/缓存/临时/日志）隔离回收至 trash/
    # （不进入 main/，可还原），其余条目移入 main/。
    entries = sorted(
        p for p in project_root.iterdir()
        if p.name not in (_CONTAINER_MAIN_DIR, _RUNTIME_DIR)
    )
    try:
        for entry in entries:
            if _is_junk_entry(entry.name):
                shutil.move(str(entry), str(trash_dir))
                cleaned.append(str(entry.name))
                continue
            if entry.name in _TOOL_DIR_NAMES:
                # P2-8：工具目录/venv 留在原地，不搬入 main/（环境专属，非项目源码）
                continue
            if entry.name == ".orchd":
                # W-11：迁移全程持有 .orchd/.intake.lock，Windows 下**目录级 move 会
                # 因锁住的文件失败（WinError 33）** → 逐条搬移并跳过持锁文件本身
                # （锁在新 canonical 路径由下次准入写重建；旧文件留待 doctor 收敛）。
                dst_orchd = main_dir / ".orchd"
                dst_orchd.mkdir(parents=True, exist_ok=True)
                for sub in sorted(entry.iterdir()):
                    if sub.name == _intake_lock_filename():
                        continue
                    shutil.move(str(sub), str(dst_orchd / sub.name))
                moved.append(entry.name)
                continue
            dst = main_dir / entry.name
            shutil.move(str(entry), str(dst))
            moved.append(str(entry.name))
    except Exception as exc:
        # 可回滚：把已移动条目移回（best-effort，不破坏原仓库）。
        # W-11：回滚过程中的 OSError **不再静默**（记入 rollback_errors 上报）。
        rollback_errors = _rollback_layout_migration(
            project_root, main_dir, runtime, moved
        )
        return {
            "migrated": False,
            "reason": "move_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rollback_errors": rollback_errors,
            "hint": "迁移中断，已尝试回滚；请检查残留后重试",
        }

    # 共享账本 runtime 根 + 布局标记
    orchd_dir = main_dir / ".orchd"
    # W-11：搬移已成功后的写操作（写布局标记 / 移账本）任一失败 → **完整回滚**，
    # 避免「main/ 已就位但锁根/账本根分裂」的半迁移态（旧实现不回滚且吞 OSError）。
    try:
        write_layout(orchd_dir, "container", main_dir)
        ledger_moved, ledger_errors = _move_ledger_runtime_files(orchd_dir, runtime)
    except Exception as exc:
        rollback_errors = _rollback_layout_migration(
            project_root, main_dir, runtime, moved
        )
        return {
            "migrated": False,
            "reason": "post_move_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rolled_back": list(moved),
            "rollback_errors": rollback_errors,
            "hint": "搬移完成后写标记/移账本失败，已回滚到 flat 布局；请检查残留后重试",
        }
    if ledger_errors:
        # 账本搬移部分失败 = 账本根分裂（部分在 main/.orchd、部分在 runtime）→
        # 同样回滚（含已写布局标记），并把失败项结构化上报（不静默）。
        rollback_errors = _rollback_layout_migration(
            project_root, main_dir, runtime, moved, ledger_moved
        )
        return {
            "migrated": False,
            "reason": "ledger_move_failed",
            "errors": ledger_errors,
            "rolled_back": list(moved),
            "rollback_errors": rollback_errors,
            "hint": "运行时账本搬移不完整，已回滚以保持锁根与账本根一致；请修复后重试",
        }

    result = {
        "migrated": True,
        "main_worktree": str(main_dir),
        "moved": moved,
        "cleaned": cleaned,
        "ledger_moved": ledger_moved,
        "marker": str(marker_path(orchd_dir)),
        "runtime_root": str(runtime),
    }
    if cleaned:
        result["trash_root"] = str(trash_dir)
    return result
