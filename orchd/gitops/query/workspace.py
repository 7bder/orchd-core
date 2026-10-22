"""工作区状态与已跟踪改动（check_workspace_state / list_tracked_changes 及两个内部探针）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import shutil
import subprocess

import orchd.gitops as _gitops_pkg
from .refs import get_current_branch


def check_workspace_state(project_root: Path) -> dict[str, Any]:
    """检查当前 git 工作区状态：分支名 + 干净度（best-effort，**三态**）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"available": True, "state": "available", "branch": <str|None>,
              "clean": <bool>}``
              branch 为当前分支名（detached HEAD 时为 None）；clean 表示无已
              跟踪文件改动（untracked 文件不视为脏；内容差异口径，porcelain
              幻影脏不计，见 task-cleanliness-content-diff）。
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
        # task-gitops-probe-cache 合并：`status -b` 首行即分支（`## main...`），
        # 一次调用同时得分支 + 脏度；解析失败回退 get_current_branch（缓存命中，
        # 不新增 spawn）。原分开调用 = is-inside + branch + status（3 spawn）。
        status = _gitops_pkg._run_git(
            project_root, ["status", "--porcelain=v1", "-b", "--untracked-files=no"])
        branch: str | None = None
        first = (status.stdout.splitlines() or [""])[0] if status.returncode == 0 else ""
        if first.startswith("## "):
            head = first[3:].split("...")[0].strip()
            if head.startswith("No commits yet on "):
                head = head[len("No commits yet on "):].strip()
            if head and head != "HEAD (no branch)" and " " not in head:
                branch = head
        if branch is None:
            branch = get_current_branch(project_root)
        # 已跟踪文件改动（不含 untracked）：porcelain 快道无输出即净（零新增 spawn）；
        # 有输出再以内容差异确认（task-cleanliness-content-diff）——幻影脏
        # （racy stat、杀软触碰 mtime 等内容零差异）不判脏，无法判定则偏脏。
        # 注意：`-b` 使 stdout 恒含头行，不可沿用旧 `not stdout.strip()` 判据
        # （否则恒判脏）；仅首行且以 `## ` 开头才剔除（路径行原样保留）。
        body_lines = status.stdout.splitlines()
        if body_lines and body_lines[0].startswith("## "):
            body_lines = body_lines[1:]
        if status.returncode != 0:
            clean = False
        elif not "".join(body_lines).strip():
            clean = True
        else:
            confirmed = _has_content_diff(project_root)
            clean = confirmed is False
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


def _has_content_diff(project_root: Path) -> bool | None:
    """内容差异确认（task-cleanliness-content-diff，porcelain 报脏后的确认档）。

    ``git diff --quiet``（工作区 vs 暂存区）与 ``git diff --cached --quiet``
    （暂存区 vs HEAD）双确认：任一返回码 1（有内容/模式差异）即真脏；双双
    0 即幻影脏（racy stat、杀软触碰 mtime 等内容零差异）；返回码非 0/1
    即无法判定 → None（调用方按脏处理，fail-closed，不静默放行）。
    SubprocessError/OSError 向上传递（调用方既有异常通道处理）。

    Returns:
        True 真脏 / False 幻影净 / None 无法判定。
    """
    for args in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
        proc = _gitops_pkg._run_git(project_root, args)
        if proc.returncode == 1:
            return True
        if proc.returncode != 0:
            return None
    return False


def _content_changed_paths(project_root: Path) -> list[str] | None:
    """有内容/模式差异的已跟踪路径（task-cleanliness-content-diff）。

    ``git diff --name-only -z`` 双取（工作区 + 暂存区）并集、排序去重；
    untracked 文件天然不在 diff 输出内（与既有排除语义一致）。双空即幻影净；
    任一调用失败（返回码非零）→ None；异常向上传递。

    Returns:
        排序路径列表；None 表示无法判定。
    """
    out: set[str] = set()
    for args in (["diff", "--name-only", "-z"], ["diff", "--cached", "--name-only", "-z"]):
        proc = _gitops_pkg._run_git(project_root, args)
        if proc.returncode != 0:
            return None
        out.update(
            entry for entry in (proc.stdout or "").split("\0") if entry.strip()
        )
    return sorted(out)


def list_tracked_changes(project_root: Path) -> list[str] | None:
    """返回已跟踪文件的未提交改动路径列表（best-effort）。

    用于 amend / intake 的"非摄入产物干净"守卫：区分「摄入产物（IDEAS.md /
    ROADMAP.md / _master.json）允许未提交」与「其余已跟踪改动必须提交」。

    Returns:
        - ``list[str]``：已跟踪文件的改动路径（相对 project_root）。
        - ``None``：非 git 仓库 / git 不可用 / 异常（调用方降级为不阻断）。

    Note:
        仅已跟踪文件（diff 天然不含 untracked），与"工作区干净 = 无已跟踪
        改动"的语义一致；untracked 工具/配置文件不列入。porcelain 仅作快道：
        无输出直接返回空（零新增 spawn）；有输出再以内容差异确认，幻影脏
        （racy stat 等内容零差异）不列入（task-cleanliness-content-diff）。
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
        if not "".join(status.stdout.splitlines()).strip():
            return []
        confirmed = _content_changed_paths(project_root)
        if confirmed is None:
            # 无法判定：回退 porcelain 解析（偏脏，fail-closed 方向）
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
        return confirmed
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
