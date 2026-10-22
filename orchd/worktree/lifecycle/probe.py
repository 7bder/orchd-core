"""lifecycle.probe —— 自 ``orchd/worktree/lifecycle.py`` 拆出。

worktree 注册状态探测、有效性判定与孤儿清理。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.worktree.layout import _GIT_TIMEOUT

def _worktree_registered_paths(project_root: Path) -> set[str] | None:
    """`git worktree list --porcelain` 登记路径集合（normcase 归一化）。

    返回 None 表示 git 不可用/解析失败（调用方保守降级：视为有效、不删）。
    按行前缀解析（W-16 口径），路径经 resolve + normcase 归一化。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    paths: set[str] = set()
    for ln in (proc.stdout or "").splitlines():
        if not ln.startswith("worktree "):
            continue
        p = ln[len("worktree "):].strip()
        if p:
            try:
                paths.add(os.path.normcase(str(Path(p).resolve())))
            except OSError:
                paths.add(os.path.normcase(p))
    return paths


def _is_effective_worktree(project_root: Path, wt_path: Path) -> bool:
    """判定是否为有效 worktree（doctor/worktree 单一真源）。

    有效 ⇔ git 登记含该路径 **且** `git rev-parse --verify HEAD` 可解析。
    任一 git 探测失败 → 保守返回 True（视为有效，绝不误删）。
    无 `.git` 标记 → 返回 False（由调用方按无标记残留路径处理）。
    """
    if not (wt_path / ".git").exists():
        return False
    registered = _worktree_registered_paths(project_root)
    if registered is None:
        return True
    try:
        key = os.path.normcase(str(wt_path.resolve()))
    except OSError:
        return True
    if key not in registered:
        return False
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True
    return head.returncode == 0 and bool((head.stdout or "").strip())


def _cleanup_stale_task_wt(project_root: Path, wt_path: Path) -> None:
    """best-effort 清理 worktree add 失败残留的半成品元数据（孤儿 worktree）。

    - ``git worktree prune``：移除指向已消失目录的失效 git 注册（安全）。
    - 残留目录仅在其**非有效 worktree**时删除：无 ``.git`` 标记的沿旧路径；
      有 ``.git`` 标记的按 :func:`_is_effective_worktree` 判定——登记失效或
      HEAD 不可解析的半检出可回收，有效 worktree 绝不误删（与 doctor 残留
      判定同源）。
    任何失败静默跳过，不阻断降级主流程。
    """
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    if not wt_path.exists():
        return
    if (wt_path / ".git").exists() and _is_effective_worktree(project_root, wt_path):
        return
    try:
        wt_path.rmdir()
    except OSError:
        try:
            shutil.rmtree(str(wt_path), ignore_errors=True)
        except Exception:
            pass


def _verify_task_wt(wt_path: Path, expected_branch: str) -> dict[str, Any]:
    """创建期不变量校验（W-4）：任务 worktree 检出分支恰为 task/<id> 且 HEAD 可解析。

    Returns:
        ``{"ok": True}`` 或 ``{"ok": False, "reason": str}``（best-effort，绝不抛异常）。
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return {"ok": False, "reason": f"任务 worktree HEAD 无法解析（{wt_path}）"}
        cur = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        actual = cur.stdout.strip() if cur.returncode == 0 else ""
        if actual != expected_branch:
            return {"ok": False,
                    "reason": f"任务 worktree 检出分支 {actual or '<null>'}，期望 {expected_branch}"}
        return {"ok": True}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"ok": False, "reason": f"worktree 校验异常: {exc}"}
