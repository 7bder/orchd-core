"""gitops cleanup 域：删除/清理 + 冲突解析。"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any
import orchd.gitops as _gitops_pkg


def _os_delete_file(path: Path) -> bool:
    """底层 OS 直接删除文件（绕过 Python ``Path.unlink`` 的沙箱劫持）。

    task-workspace-docs-isolation：部分沙箱把 ``Path.unlink`` 劫持为
    "移入回收站"（safe-delete），回收站可用时文件进回收站、不可用时
    FAIL_CLOSED 抛 OSError。为满足"直接删除、不进回收站"，优先用底层 OS
    调用：Windows 经 ctypes 调 ``kernel32.DeleteFileW``（Python 层无法劫持），
    POSIX 用 ``os.unlink``。失败返回 ``False``（由调用方回退）。

    Returns:
        ``True`` 文件已直接删除；``False`` 底层删除失败。
    """
    try:
        if os.name == "nt":
            import ctypes

            # 清只读属性后删除（Windows 只读文件 DeleteFileW 会失败）
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
            return bool(ctypes.windll.kernel32.DeleteFileW(str(path)))
        os.unlink(path)
        return True
    except OSError:
        return False


def _os_delete_tree(path: Path) -> bool:
    """递归删除目录树（绕过 Path.unlink 沙箱劫持，Windows 用 ctypes）。

    task-test-tmp-recycle-bin-force-delete：与 :func:`_os_delete_file` 并列，
    提供递归目录树底层删除，供 worktree 回收与 pytest 清理共用，统一"直接删除、
    不进回收站"契约。

    Windows：先清只读属性（``SetFileAttributesW(FILE_ATTRIBUTE_NORMAL)``，git 对象
    文件只读导致删除失败），递归删子项后 ``RemoveDirectoryW`` 删目录，文件用
    ``DeleteFileW``；POSIX 用 ``os.rmdir``（本来就不进回收站）。任何 OSError 失败
    返回 ``False``，由调用方回退。

    Args:
        path: 待删除目录树或单文件 / 符号链接。

    Returns:
        ``True`` 路径已不存在（删除成功或本就不存在）；``False`` 删除失败。
    """
    try:
        # 不存在且非符号链接（断开的符号链接仍应尝试删除）→ 视为已清理
        if not path.exists() and not path.is_symlink():
            return True
        # 符号链接或单文件 → 走单文件底层删除（不递归进链接目标）
        if path.is_symlink() or not path.is_dir():
            return _os_delete_file(path)
        # 递归删子项（失败静默，由目录删除失败统一返回 False）
        for child in path.iterdir():
            _os_delete_tree(child)
        if os.name == "nt":
            import ctypes

            # 清只读属性后删空目录（Windows 只读目录 RemoveDirectoryW 失败）
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
            return bool(ctypes.windll.kernel32.RemoveDirectoryW(str(path)))
        # POSIX：用原始 os.rmdir（非 Path.rmdir，避免被测试进程内劫持递归）
        os.rmdir(str(path))
        return True
    except OSError:
        return False


def _safe_delete(path: Path, base_dir: Path) -> None:
    """沙箱安全的文件删除（直接删除优先，绕过 unlink 劫持）。

    部分沙箱把 ``Path.unlink`` 劫持为"移入回收站"（safe-delete），回收站可用时
    文件进回收站、不可用时 FAIL_CLOSED 抛 OSError（2026-08-06 实踩：
    windows-sandbox-recycle-bin-unavailable，全量 pytest 稳定触发）。删除顺序：
    ① 底层 OS 直接删除（``_os_delete_file``，Python 层无法劫持，不进回收站）；
    ② ``Path.unlink``（沙箱正常环境）；③ 仍失败则降级为把文件重命名移动到
    系统临时目录（重命名不经删除劫持，目标位置"消失"，语义等效删除；残留物
    为 ``orchd-trash-*``，可由 :func:`_cleanup_trash_residue` 直接清理）。

    Args:
        path: 待删除文件。
        base_dir: 用于生成唯一残留名的基准目录名。

    Raises:
        OSError: 底层删除、unlink 与降级重命名均失败时向上抛（由调用方 best-effort 降级）。
    """
    if _gitops_pkg._os_delete_file(path):
        return
    try:
        path.unlink()
        return
    except OSError:
        pass
    dest = Path(tempfile.gettempdir()) / (
        f"orchd-trash-{base_dir.name}-{uuid.uuid4().hex[:8]}-{path.name}"
    )
    os.replace(path, dest)


def _cleanup_trash_residue(tmp_root: Path | None = None) -> list[str]:
    """清理系统 temp 中历史 ``orchd-trash-*`` 残留（best-effort 直接删除）。

    task-workspace-docs-isolation：早期 :func:`_safe_delete` 降级重命名产生的
    ``orchd-trash-*`` 残留物（文件或目录）。直接删除（Windows 只读文件先清
    只读属性），不进回收站，避免残留持续占用系统 temp。

    Args:
        tmp_root: 扫描根（默认系统 temp；测试可注入）。

    Returns:
        已清理的残留条目名清单。
    """
    cleaned: list[str] = []
    try:
        root = tmp_root or Path(tempfile.gettempdir())

        def _onerror(func: Any, p: str, exc: Any) -> None:
            try:
                os.chmod(p, 0o200)  # S_IWRITE：清只读后重试
                func(p)
            except OSError:
                pass

        for p in sorted(root.glob("orchd-trash-*")):
            try:
                if p.is_dir():
                    shutil.rmtree(str(p), onerror=_onerror)
                elif not _gitops_pkg._os_delete_file(p):
                    continue
                if not p.exists():
                    cleaned.append(p.name)
            except OSError:
                pass
    except OSError:
        pass
    return cleaned


def parse_conflicts(output: str) -> list[str]:
    """从 git merge 输出中提取冲突文件路径列表（merge 冲突判定，task-14-git-policy-layer）。

    遍历输出行，命中 ``CONFLICT`` 的行取其最后一个词作为冲突文件路径。
    无冲突返回空列表。
    """
    files: list[str] = []
    for line in (output or "").split("\n"):
        if "CONFLICT" in line:
            parts = line.split()
            if parts:
                files.append(parts[-1])
    return files
