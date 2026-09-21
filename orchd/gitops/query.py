"""gitops query 域：只读查询（纯 git，不依赖 worktree）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.gitops._const import _GIT_TIMEOUT
import orchd.gitops as _gitops_pkg


# ---------------------------------------------------------------------------
# 只读探测按次缓存（task-gitops-probe-cache）
# ---------------------------------------------------------------------------
# 背景：Windows 上单次 git spawn 约 40–75ms，而 get_current_branch /
# main_worktree_root / _git_toplevel 等只读探测在单条命令内被反复调用
# （实测单 CLI 用例 33 subprocess 中 ~28 次为此类探测）。
#
# 新鲜度（零 spawn 信号）：缓存键 =（归一化绝对路径 + .git 形态快照）。
# 任何可能改变答案的变更都会改变键 → 自动 miss，绝不 stale：
#   - 切分支/建分支 → HEAD 内容或 mtime 变化；
#   - git init → 父目录 mtime 变化（.git 由无到有）；
#   - worktree 增删别处 → 本 cwd 信号不变 → 答案本就不变，命中正确。
# 信号不可读（目录被删、权限异常）→ 不缓存（fail-safe：宁可多 spawn）。
# 跨命令无需手动失效：键变化即失效；长生命进程可调 clear_git_probe_cache()。
_PROBE_CACHE: dict[tuple, Any] = {}


def clear_git_probe_cache() -> None:
    """清空只读探测缓存（测试隔离 / 长生命进程手动失效）。"""
    _PROBE_CACHE.clear()


def _probe_cache_key(project_root: Path) -> tuple | None:
    """构造零 spawn 新鲜度键；不可判定返回 None（调用方直通 git）。"""
    import os as _os

    try:
        p = Path(project_root).resolve()
    except OSError:
        return None
    base = _os.path.normcase(str(p))
    dot = p / ".git"
    try:
        if not dot.exists() and not dot.is_symlink():
            try:
                return (base, "nage", p.stat().st_mtime_ns)
            except OSError:
                return None
        if dot.is_file():
            try:
                target = dot.read_text(
                    encoding="utf-8", errors="replace").partition(
                        "gitdir:")[2].strip()
            except OSError:
                return None
            if not target:
                return (base, "odd", None)
            head = Path(target) / "HEAD"
            try:
                return (base, "file", target,
                        head.read_text(encoding="utf-8", errors="replace"),
                        head.stat().st_mtime_ns)
            except OSError:
                return (base, "file", target, None, None)
        if dot.is_dir():
            head = dot / "HEAD"
            try:
                return (base, "dir",
                        head.read_text(encoding="utf-8", errors="replace"),
                        head.stat().st_mtime_ns)
            except OSError:
                return (base, "dir", None, None)
        return (base, "odd", None)
    except OSError:
        return None


def get_current_branch(project_root: Path) -> str | None:
    """获取当前 git 分支名。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。

    按次缓存：同键（同目录 + 同 HEAD 快照）重复调用零 spawn。
    确定性失败（git 正常返回非零退出码，如非仓库）同样缓存——同键下答案
    不变；抛异常路径（超时/IO，瞬时故障）永不缓存，保证恢复力。
    """
    key = _probe_cache_key(project_root)
    ckey = ("branch", key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        result = _gitops_pkg._run_git(project_root, ["branch", "--show-current"])
        value = result.stdout.strip() or None if result.returncode == 0 else None
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
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
        # 2. 本地常见默认分支名单次调用判定（task-gitops-probe-cache 压扁）：
        #    `git branch --list main master` 一次返回存在性，替代逐个
        #    `rev-parse --verify`（省 1 spawn；语义与文档"本地存在 main/master
        #    分支"逐字一致——且比 --verify 更严格：后者连同名 tag 也命中）。
        names = _gitops_pkg._run_git(
            project_root, ["branch", "--list", "main", "master", "--format=%(refname:short)"])
        if names.returncode == 0:
            have = set((names.stdout or "").split())
            if "main" in have:
                return "main"
            if "master" in have:
                return "master"
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


def _has_linked_worktrees(project_root: Path) -> bool:
    """判定仓库是否处于多 worktree 并行场景（存在 linked worktrees）。

    ``git worktree list --porcelain`` 每个 worktree 块以 ``worktree <path>`` 行开头；
    计数 >1 即存在 linked worktree（主 worktree + 至少一个 linked）。

    W-16（2026-09-15）：改按**行前缀**匹配，不再用子串 ``stdout.count("worktree ")``
    ——``locked <reason>`` / ``prunable <reason>`` 行的 reason 文本、或登记路径本身
    含 ``worktree `` 子串时，旧实现会把块数算多，从而在单 worktree 场景误判为多
    worktree（进而走 merge-wt 相关分支）。

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
        blocks = [
            ln for ln in (proc.stdout or "").splitlines()
            if ln.startswith("worktree ")
        ]
        return len(blocks) > 1
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def main_worktree_root(project_root: Path) -> Path:
    """从任意 worktree 定位主工作树根（task-14-merge-main-tree，AC1）。

    ``git rev-parse --git-common-dir`` 返回公共 git 目录：主 worktree 返回
    ``<根>/.git``，linked worktree（任务 worktree）返回同一主 ``.git``。
    取其父目录即主工作树根（merge 在主工作树内执行、永不切任务 worktree 的 main）。
    非 git / 解析失败回退 ``project_root``（best-effort，flat 单会话零回归）。

    按次缓存：同键重复调用零 spawn（common-dir 由 .git 指针派生，指针不变则
    答案不变；指针变化 → 键变化 → 自动 miss）。确定性失败（非零退出/空输出）
    同样缓存；抛异常路径永不缓存。
    """
    key = _probe_cache_key(project_root)
    ckey = ("main_root", key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
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
            value = project_root
        else:
            git_dir = proc.stdout.strip()
            if not git_dir:
                value = project_root
            else:
                p = Path(git_dir)
                if not p.is_absolute():
                    p = (project_root / p).resolve()
                else:
                    p = p.resolve()
                value = p.parent if p.name == ".git" else project_root
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
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

