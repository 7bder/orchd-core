"""worktree layout gitinfo 域（W6-C5 纯搬移）。

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



_GIT_TIMEOUT = 10



def _git_toplevel(project_root: Path) -> Path | None:
    """best-effort 定位 git 主工作树根（git rev-parse --show-toplevel）。

    非 git 仓库 / git 不可用 / 异常返回 None（调用方降级）。

    按次缓存（task-gitops-probe-cache）：缓存键与
    ``orchd.gitops.query`` 同源（目录形态 + .git/HEAD 快照），toplevel 只与
    仓库归属有关、键不变则答案不变。函数内惰性导入（worktree 不在顶层依赖
    gitops，避免循环）。确定性失败（非零退出/空输出）同样缓存；
    抛异常路径永不缓存（task-probe-cache-negative-results）。
    """
    try:
        from orchd.gitops.query import _PROBE_CACHE, _probe_cache_key
    except Exception:
        _PROBE_CACHE = None  # type: ignore[assignment]
        _probe_cache_key = None  # type: ignore[assignment]
    key = _probe_cache_key(project_root) if _probe_cache_key is not None else None
    ckey = ("toplevel", key) if key is not None else None
    if _PROBE_CACHE is not None and ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        value: Path | None = None
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out:
                value = Path(out)
        if _PROBE_CACHE is not None and ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None



def _git_common_dir(project_root: Path) -> Path | None:
    """定位仓库共享 git 目录（**就地取证优先**，git 命令兜底）。

    review W-1 / R-16：残留目录判定需要「曾由 git 登记为本仓库 worktree」的证据，
    证据落点即 ``<common-dir>/worktrees/<name>``。linked worktree 与主工作树返回
    同一 common dir；非 git / git 不可用 / 解析失败 → None（调用方保守降级）。

    task-worktree-common-dir-fallback（CI/Linux 稳健）：原实现只走
    ``git rev-parse --git-common-dir``，该命令失败即**静默**放弃取证——CI 实测该
    用例决策退成 ``no_git_registry_evidence``（本机绿、CI 红）。故改为就地取证优先：
    ``<root>/.git`` 为目录即 common dir；为文件（linked worktree）时解析其
    ``gitdir: <path>``（``<common>/worktrees/<name>`` 形态取 ``<common>``）。两条就地
    路径都不成立才回退 git 命令；三条全失败时写 stderr 留痕（不静默降级）。
    """
    root = Path(project_root)
    dot_git = root / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = (root / target).resolve()
                # linked worktree：<common>/worktrees/<name> → 取 <common>
                if target.parent.name == "worktrees":
                    return target.parent.parent
                return target
    except OSError:
        pass
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        print(
            f"WARN: git common dir 取证失败（{type(exc).__name__}: {exc}），"
            "残留判定将退化为「无登记证据」",
            file=sys.stderr,
        )
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or "").strip().splitlines()
        print(
            f"WARN: git common dir 取证失败（rc={proc.returncode}）"
            f"{detail[0][:120] if detail else ''}，残留判定将退化为「无登记证据」",
            file=sys.stderr,
        )
        return None
    git_dir = Path(proc.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (Path(project_root) / git_dir).resolve()
    return git_dir



def _is_git_tracked_path(path: Path) -> bool:
    """路径是否被其所属 git 仓库跟踪（junk 清理前的安全校验，review W-15）。

    判定序：以路径本身（目录）或父目录（文件）为 cwd 定位最近 git 仓库根
    （``--show-toplevel``），再以 ``git ls-files --error-unmatch -- <相对路径>``
    判定是否被跟踪。非 git / 路径不在仓库内 → False（不构成「被跟踪」，允许清理）。

    探测异常（git 不可用 / 超时）→ True：保守判为「可能被跟踪」而跳过删除。
    这样 ``_cleanup_container_root_junk`` 的 docstring 承诺「绝不触碰被跟踪文件」
    才有实现支撑（此前只有承诺、无校验）。
    """
    try:
        path = Path(path)
        probe = path if path.is_dir() else path.parent
        root = _git_toplevel(probe)
        if root is None:
            return False
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(rel).replace(os.sep, "/")],
            cwd=str(root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True
    return proc.returncode == 0



def nearest_git_root(start: Path) -> Path | None:
    """起点所属的**最近 git 仓库根**（``git rev-parse --show-toplevel``，绝对路径）。

    与 :func:`main_worktree_root` 的分工：后者用 ``--git-common-dir`` 区分
    「linked worktree → 共同主根」与「独立仓库 → 自身」服务 canonical 根解析；
    本函数只回答「起点当前站在哪个 git 仓库里」，用作 ``.orchd`` 向上查找时
    **不可逾越的边界**。非 git 目录 / git 不可用 / 异常 → None（调用方降级）。
    """
    root = _git_toplevel(Path(start))
    return root.resolve() if root is not None else None



def find_orchd_dir_within_git_boundary(start: Path | None = None) -> Path:
    """从 ``start``（默认 cwd）向上定位第一个 ``.orchd/``，但不越过最近 git 仓库根。

    边界规则（task-canonical-root-boundary-guard，AC1）：

    - 起点位于某 git 仓库 R 内：仅在闭区间 ``[start .. R 根]`` 内查找 ``.orchd``。
      区间内（含 R 根）有 ``.orchd`` → 返回它；一路到 R 根都没有 → 返回
      ``start/.orchd``（按 flat/自身处理），**绝不**继续爬到 R 的祖先。这堵住
      「pytest tmp_path 落在宿主仓库内、其层级目录又被 ``git init`` 成独立仓库」
      时解析越过内层仓库顶、误把宿主真实 ``.orchd`` 当工作区的事故。
    - linked worktree：R 根即该任务 worktree 根，其下有引擎传播的 ``.orchd``，
      在区间内即命中（master 读取再由 resolve_canonical_project_root 归主，零回归）。
    - 起点不在任何 git 仓库（``nearest_git_root`` 为 None）：维持历史行为，逐级
      向上直到命中（发布态自包含 / 非 git 降级路径，零回归）。
    """
    start = Path(start or Path.cwd()).resolve()
    git_root = nearest_git_root(start)
    for parent in [start, *start.parents]:
        candidate = parent / ".orchd"
        if candidate.is_dir():
            return candidate
        if git_root is not None and parent == git_root:
            # 已查至最近 git 仓库根仍无 .orchd：停止，禁止越界继续上爬。
            break
    return start / ".orchd"
