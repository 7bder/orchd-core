"""gitops commit 域：兜底提交 + HEAD 推进检测（叶子模块，零同包依赖）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.gitops._const import _GIT_COMMIT_TIMEOUT, _GIT_TIMEOUT
from orchd.gitops._run import _run_git
from orchd.gitops.query import _probe_git_repo_ready


def _cleanup_index_locks(project_root: Path) -> None:
    """清理被强杀的 git commit 遗留的索引锁（index.lock / next-index-*.lock）。

    仅在 commit 超时（TimeoutExpired）后调用：超时意味着 git 进程被强杀，
    可能留下 ``.git/index.lock`` / ``.git/next-index-*.lock``（含 linked
    worktree 的独立 git dir）。best-effort：解析 git dir 失败或删除失败
    均静默跳过，不阻断提交结果。``commit_failed``（rc!=0）分支**不得**调用
    本函数——那是 git 自身可恢复的失败，删锁可能破坏并发中的其他提交。
    """
    try:
        proc = _run_git(project_root, ["rev-parse", "--git-dir"], timeout=_GIT_TIMEOUT)
        if proc.returncode != 0:
            return
        git_dir = Path((proc.stdout or "").strip())
        if not git_dir.is_absolute():
            git_dir = project_root / git_dir
        for lock in [git_dir / "index.lock", *git_dir.glob("next-index-*.lock")]:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass


def _filter_committable_paths(project_root: Path, paths: list[str]) -> list[str]:
    """过滤不存在 + 被 .gitignore 忽略的路径，返回可提交的路径列表。"""
    # intake-commit-enforcement：过滤不存在的路径，避免 git add 遇错误
    # pathspec 整体 fatal，连带阻断其余存在路径的提交。
    existing_paths: list[str] = []
    for p in paths:
        ap = Path(p) if Path(p).is_absolute() else project_root / p
        if ap.exists():
            existing_paths.append(p)
    # roadmap-untracked：过滤被 .gitignore 忽略的路径（如 .orchd/ 内的运行时 /
    # 历史 ROADMAP 残留副本），git add 遇被忽略路径会 fatal 中止；被忽略路径直接
    # 剔除，未跟踪但未被忽略的新文件仍保留，维持引擎兜底提交语义。
    non_ignored_paths: list[str] = []
    for p in existing_paths:
        try:
            proc = _run_git(project_root, ["check-ignore", "-q", "--", p])
            if proc.returncode != 0:
                non_ignored_paths.append(p)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    return non_ignored_paths


def _commit_filtered_paths(
    project_root: Path,
    paths: list[str],
    message: str,
) -> dict[str, Any]:
    """执行 git add + diff + commit（均限定 paths），返回结构化结果。"""
    # 只 add 声明范围。add 失败不阻断：是否真的"无改动"由 diff 精确判断。
    try:
        _run_git(project_root, ["add", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    # 范围内是否有 staged 改动：0=无，1=有，其余为 git 错误
    try:
        diff = _run_git(project_root, ["diff", "--cached", "--quiet", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git diff failed"}
    if diff.returncode == 0:
        return {"performed": False, "reason": "no_changes", "message": message}
    if diff.returncode != 1:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (diff.stderr or diff.stdout).strip()[:300] or "git diff --cached failed",
        }
    # commit 同样限定 paths：不提交声明范围外的 staged 内容，不 push。
    # 写操作使用独立超时预算（_GIT_COMMIT_TIMEOUT）：pre-commit hook 等写路径
    # 单次可越过读操作 10s 上限，超时不再吞成 commit_failed（AC1/AC2）。
    try:
        commit = _run_git(
            project_root,
            ["commit", "-m", message, "--", *paths],
            timeout=_GIT_COMMIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup_index_locks(project_root)
        return {
            "performed": False,
            "reason": "commit_timeout",
            "message": "git commit timed out",
            "timeout_seconds": _GIT_COMMIT_TIMEOUT,
            "detail": str(exc) if str(exc) else "subprocess.TimeoutExpired",
        }
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git commit failed"}
    if commit.returncode != 0:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (commit.stderr or commit.stdout).strip()[:300] or "git commit failed",
        }
    return {"performed": True, "reason": "committed", "message": message}


def ensure_committed(
    project_root: Path,
    paths: list[str],
    message: str,
) -> dict[str, Any]:
    """best-effort 将 ``paths`` 范围内的未提交改动提交到当前分支。

    Args:
        project_root: 仓库根目录（git 命令的 cwd）。
        paths: 允许提交的路径列表（相对 project_root 或绝对路径）。
            调用方须传入任务声明的 ``files_to_edit`` 或固定资产路径。
        message: 提交消息。

    Returns:
        结构化结果，永不抛异常：
            - ``{"performed": True, "reason": "committed", "message": message}``
              引擎实际创建了一个提交。
            - ``{"performed": False, "reason": "no_changes", "message": message}``
              范围内无未提交改动（实现者已自行提交、路径不存在或本就无改动），
              无需提交。
            - ``{"performed": False, "reason": "not_a_git_repo"}``
              当前目录不是 git 工作树。
            - ``{"performed": False, "reason": "git_unavailable"}``
              git 可执行文件不在 PATH。
            - ``{"performed": False, "reason": "commit_failed", "message": <stderr>}``
              git diff / commit 失败（如 user 未配置、模拟异常）。

    Raises:
        永不抛异常；所有 git 失败均降级为结构化结果。
    """
    if not paths:
        return {"performed": False, "reason": "no_paths"}
    probe = _probe_git_repo_ready(project_root)
    if probe is not None:
        return probe
    filtered = _filter_committable_paths(project_root, paths)
    if not filtered:
        return {"performed": False, "reason": "no_changes", "message": message}
    return _commit_filtered_paths(project_root, filtered, message)


def head_drift_check(
    project_root: Path,
    ref: str = "HEAD",
    base_ref: str = "main",
) -> dict[str, Any]:
    """提交前 HEAD 推进检测（task-intake-file-lock AC3，git 层 TOCTOU 防护）。

    准入（intake/amend）在工作区写入全局文件（_master.json / IDEAS.md /
    ROADMAP.md）前，比对 "本地 ref 相对 base_ref 是否已被并行推进"：
    记录本次会话读取 ref 时的 commit，与提交时/写入时的当前 commit 比较——
    若期间 base_ref（如 main）被其他 agent 推进，则本地工作区基于的 base 已过期，
    此时提交会基于过期 base、与并行改动冲突。检测到推进则返回 drift=True，
    caller 应拒绝并提示先更新/重拉。

    best-effort：非 git / ref 解析失败降级 False（不误伤）。

    Args:
        project_root: 仓库根目录（git 命令 cwd）。
        ref: 待检测的 ref（默认 HEAD）。
        base_ref: 比对基线 ref（默认 main）。

    Returns:
        ``{"drift": False}`` 无推进（base 未变化）或不可解析。
        ``{"drift": True, "base_sha": <str>, "head_sha": <str>,
          "ref": <str>, "base_ref": <str>}`` base 与本地已分叉，应重拉。
    """
    if shutil.which("git") is None:
        return {"drift": False}
    try:
        head = _run_git(project_root, ["rev-parse", "--verify", ref])
        if head.returncode != 0:
            head_sha = ""
        else:
            head_sha = (head.stdout or "").strip()
        base = _run_git(project_root, ["rev-parse", "--verify", base_ref])
        if base.returncode != 0:
            return {"drift": False}
        base_sha = (base.stdout or "").strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"drift": False}
    if not head_sha or not base_sha:
        return {"drift": False}
    # base 是 head 的祖先 → 本地未落后（head 包含 base）无漂移；
    # merge-base 非 base_sha → 已分叉/被并行推进，检测漂移。
    try:
        merge_base = _run_git(project_root, ["merge-base", ref, base_ref])
        mb = (merge_base.stdout or "").strip() if merge_base.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        mb = ""
    if mb == base_sha:
        return {"drift": False}
    return {"drift": True, "base_sha": base_sha, "head_sha": head_sha,
            "ref": ref, "base_ref": base_ref}