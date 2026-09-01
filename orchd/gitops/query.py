"""gitops query 域：只读查询（纯 git，不依赖 worktree）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.gitops._const import _GIT_TIMEOUT
import orchd.gitops as _gitops_pkg


def get_current_branch(project_root: Path) -> str | None:
    """获取当前 git 分支名。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。
    """
    try:
        result = _gitops_pkg._run_git(project_root, ["branch", "--show-current"])
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_head_commit(project_root: Path) -> str | None:
    """获取当前 HEAD 的 commit SHA（用于 review baseline 追踪）。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。
    """
    try:
        result = _gitops_pkg._run_git(project_root, ["rev-parse", "HEAD"])
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_default_branch(project_root: Path) -> str | None:
    """检测**本仓库**的默认分支名（best-effort）。

    优先级：
    1. ``git config init.defaultBranch``（用户显式配置）
    2. 本地存在 ``main`` 分支
    3. 本地存在 ``master`` 分支
    4. 都没有返回 None

    非 git 仓库、git 不可用或任何异常返回 None。

    Note:
        必须先判定 ``project_root`` 是否为 git 工作树，再读 ``init.defaultBranch``。
        ``git config --get`` 会一路回溯到**系统 / 全局**配置（Git for Windows 的
        ``etc/gitconfig`` 预设 ``init.defaultbranch=main``），在**任意目录**——
        包括非 git 目录——都返回非 None。若据此认定"存在默认分支"，下游门禁
        会把"非 git 环境（不适用）"误判成"git 探测故障"而错误 fail-closed
        （2026-08-29 实踩：引擎经 Git Bash 执行 verify 时命中该组合）。
    """
    try:
        # 0. 非 git 工作树 → 无"本仓库默认分支"概念，直接返回 None
        inside = _gitops_pkg._run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        # 1. 显式配置优先
        cfg = _gitops_pkg._run_git(project_root, ["config", "--get", "init.defaultBranch"])
        if cfg.returncode == 0:
            name = cfg.stdout.strip()
            if name:
                return name
        # 2. 探测本地常见默认分支名
        for candidate in ("main", "master"):
            check = _gitops_pkg._run_git(project_root, ["rev-parse", "--verify", "--quiet", candidate])
            if check.returncode == 0:
                return candidate
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def branch_exists(project_root: Path, branch: str) -> bool | None:
    """判定分支是否存在（三态，供门禁的"环境不适用"判定使用）。

    Returns:
        - ``True`` 分支存在；
        - ``False`` 分支不存在（前提不成立 → 门禁不适用，允许降级 + 留痕）；
        - ``None`` git 探测故障（超时 / IO）→ 调用方必须 fail-closed，
          不得当作"不存在"降级放行。

    Note:
        与 :func:`check_workspace_state` 同构：把「不适用」与「故障」分开，
        避免用 ``returncode != 0`` 一次性吞掉两种语义。
    """
    try:
        proc = _gitops_pkg._run_git(project_root, ["rev-parse", "--verify", "--quiet", branch])
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        return None
    except (subprocess.SubprocessError, OSError):
        return None


def check_workspace_state(project_root: Path) -> dict[str, Any]:
    """检查当前 git 工作区状态：分支名 + 干净度（best-effort，**三态**）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"available": True, "state": "available", "branch": <str|None>,
              "clean": <bool>}``
              branch 为当前分支名（detached HEAD 时为 None）；clean 表示无已
              跟踪文件改动（untracked 文件不视为脏）。
            - ``{"available": False, "state": "unavailable", "reason":
              "git_unavailable" | "not_a_git_repo"}``
              **环境不适用**（无 git 可执行文件 / 非 git 工作树）→ 调用方可降级。
            - ``{"available": False, "state": "error", "reason": "git_error",
              "error": <str>}``
              **git 探测故障**（``_GIT_TIMEOUT`` 超时 / IO / 其他 OSError）→
              调用方必须 fail-closed（见 :func:`guard_write_command`）。

    Note:
        ``subprocess.TimeoutExpired`` 是 ``SubprocessError`` 的子类：改造前它与
        「非 git 仓库」一并被降级为 ``available=False``，导致大仓库 / 慢盘 /
        杀毒软件实时扫描下 git 超 ``_GIT_TIMEOUT`` 秒时，L1 分支守卫与 L2 session
        锁整体静默失效（守卫以为"不适用"）。三态把故障单独归为 ``error``。
        ``available`` 字段语义保持原样（仅 fully available 时为 True），
        旧调用方（``checkout_default_strict`` 等）零回归。
    """
    if shutil.which("git") is None:
        return {
            "available": False,
            "state": "unavailable",
            "reason": "git_unavailable",
        }
    try:
        check = _gitops_pkg._run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {
                "available": False,
                "state": "unavailable",
                "reason": "not_a_git_repo",
            }
        branch = get_current_branch(project_root)
        # 已跟踪文件改动（不含 untracked）：--porcelain 输出非空即脏
        status = _gitops_pkg._run_git(project_root, ["status", "--porcelain", "--untracked-files=no"])
        clean = status.returncode == 0 and not status.stdout.strip()
        return {
            "available": True,
            "state": "available",
            "branch": branch,
            "clean": clean,
        }
    except FileNotFoundError:
        # git 在探测期间从 PATH 消失（环境变更）：仍属"不适用"而非探测故障
        return {
            "available": False,
            "state": "unavailable",
            "reason": "git_unavailable",
        }
    except (subprocess.SubprocessError, OSError) as exc:
        # 超时 / IO：门禁没能跑起来，与"不适用"严格区分（fail-closed 治理）
        return {
            "available": False,
            "state": "error",
            "reason": "git_error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def list_tracked_changes(project_root: Path) -> list[str] | None:
    """返回已跟踪文件的未提交改动路径列表（best-effort）。

    用于 amend / intake 的"非摄入产物干净"守卫：区分「摄入产物（IDEAS.md /
    ROADMAP.md / _master.json）允许未提交」与「其余已跟踪改动必须提交」。

    Returns:
        - ``list[str]``：已跟踪文件的改动路径（相对 project_root）。
        - ``None``：非 git 仓库 / git 不可用 / 异常（调用方降级为不阻断）。

    Note:
        仅已跟踪文件（``--untracked-files=no``），与"工作区干净 = 无已跟踪
        改动"的语义一致；untracked 工具/配置文件不列入。
    """
    if shutil.which("git") is None:
        return None
    try:
        check = _gitops_pkg._run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return None
        status = _gitops_pkg._run_git(project_root, ["status", "--porcelain", "--untracked-files=no"])
        if status.returncode != 0:
            return None
        files: list[str] = []
        for line in status.stdout.splitlines():
            if len(line) < 4:
                continue
            # porcelain v1：`XY path`（XY 各 1 字符 + 空格）；rename 为
            # `R  old -> new`（取箭头后路径，保守处理，避免把 rename 目标误列）
            code, _, path = line[:2], line[2], line[3:]
            if code == "R " or code.startswith("R"):
                arrow = path.find(" -> ")
                if arrow != -1:
                    path = path[arrow + 4:]
            files.append(path.strip())
        return files
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _has_linked_worktrees(project_root: Path) -> bool:
    """判定仓库是否处于多 worktree 并行场景（存在 linked worktrees）。

    ``git worktree list --porcelain`` 每个 worktree 块以 ``worktree `` 开头；
    >1 即存在 linked worktree（主 worktree + 至少一个 linked）。
    单 worktree（默认）场景返回 False——不创建 merge-wt，保持既有行为零回归
    （checkout_default_strict 仍可在 agent worktree 内切回 main）。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            return False
        return proc.stdout.count("worktree ") > 1
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def main_worktree_root(project_root: Path) -> Path:
    """从任意 worktree 定位主工作树根（task-14-merge-main-tree，AC1）。

    ``git rev-parse --git-common-dir`` 返回公共 git 目录：主 worktree 返回
    ``<根>/.git``，linked worktree（任务 worktree）返回同一主 ``.git``。
    取其父目录即主工作树根（merge 在主工作树内执行、永不切任务 worktree 的 main）。
    非 git / 解析失败回退 ``project_root``（best-effort，flat 单会话零回归）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return project_root
        git_dir = proc.stdout.strip()
        if not git_dir:
            return project_root
        p = Path(git_dir)
        if not p.is_absolute():
            p = (project_root / p).resolve()
        else:
            p = p.resolve()
        if p.name == ".git":
            return p.parent
        return project_root
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return project_root


def is_task_worktree(project_root: Path) -> bool:
    """判定当前目录是否为 linked worktree（任务 worktree，task-14-merge-main-tree AC3）。

    ``git rev-parse --git-dir``：linked worktree 返回 ``<common>/.git/worktrees/<name>``
    （含 ``worktrees/`` 段），主 worktree 返回 ``<根>/.git``。含 ``worktrees/`` → True。
    非 git / 解析失败返回 False（best-effort）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return False
        return "worktrees/" in (proc.stdout.strip() or "")
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _probe_git_repo_ready(project_root: Path) -> dict[str, Any] | None:
    """探测 git 可用 + 仓库状态；返回 None 表示通过，否则返回结构化失败结果。"""
    if shutil.which("git") is None:
        return {"performed": False, "reason": "git_unavailable"}
    try:
        check = _gitops_pkg._run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {"performed": False, "reason": "not_a_git_repo"}
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "not_a_git_repo"}
    return None

