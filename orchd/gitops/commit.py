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


def _rel_path(project_root: Path, path: str) -> str:
    """把路径规范成相对 ``project_root`` 的 posix 串（失败时原样返回）。"""
    try:
        p = Path(path)
        if p.is_absolute():
            p = p.relative_to(project_root)
        return p.as_posix()
    except (ValueError, OSError):
        return path.replace("\\", "/")


def merge_in_progress(project_root: Path) -> bool:
    """当前工作树是否处于 merge 冲突现场（``MERGE_HEAD`` 存在）。

    merge 中的 ``git commit`` **不允许带 pathspec**（``fatal: cannot do a
    partial commit``），且提交内容天然包含被合入分支带入的文件——提交层必须
    先识别这台状态机，才谈得上受管收口（task-engine-defect-fix-unified-u5）。
    """
    try:
        proc = _run_git(
            project_root, ["rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return proc.returncode == 0


def _all_staged_paths(project_root: Path) -> set[str] | None:
    """索引中全部已暂存路径（merge 收口时做「暂存集受控」校验用）。

    失败返回 ``None`` 而不是空集：空集会让调用方把"快照失败"误当成"没有既有
    暂存"，随后 rollback 可能 reset 掉调用前已有的 staged 内容（errata EE1）。
    """
    try:
        proc = _run_git(project_root, ["diff", "--cached", "--name-only", "-z"])
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return {p for p in (proc.stdout or "").split("\0") if p}


def _merge_parent_side_paths(project_root: Path) -> set[str]:
    """merge 中「被合入分支（父侧）带入」的路径集合。

    ``MERGE_HEAD`` 相对 merge-base 的改动即 trunk 侧带入的内容：这些文件属于
    merge 语义而非本任务越界改动，暂存集校验必须放行（E020 的 merge 父侧
    判定同源，见 u6）。
    """
    try:
        base = _run_git(project_root, ["merge-base", "HEAD", "MERGE_HEAD"])
        if base.returncode != 0:
            return set()
        base_sha = (base.stdout or "").strip()
        if not base_sha:
            return set()
        proc = _run_git(
            project_root,
            ["diff", "--name-only", "-z", base_sha, "MERGE_HEAD"],
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return set()
    if proc.returncode != 0:
        return set()
    return {p for p in (proc.stdout or "").split("\0") if p}


def _head_sha(project_root: Path) -> str | None:
    """当前 HEAD 的 commit sha（best-effort；失败 → None）。"""
    try:
        proc = _run_git(project_root, ["rev-parse", "HEAD"])
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _staged_paths(project_root: Path, paths: list[str]) -> set[str]:
    """返回 ``paths`` 中「索引里已有暂存改动」的子集（add / modify / delete）。

    done 自动提交在失败（``commit_failed``）时不会回滚索引，于是可能留下
    ``D  src/gone.py`` 这种已暂存删除：此时 ``git ls-files`` 已不含该路径，
    :func:`path_state` 会判为 ghost 并被过滤掉 ⇒ 重试 done 永远 ``no_changes``。
    本函数只看「索引是否已有本范围改动」，与磁盘存在性无关，作为可提交性的
    正交补充（不构成第二套存在性判定）。
    """
    if not paths:
        return set()
    try:
        proc = _run_git(
            project_root,
            ["diff", "--cached", "--name-only", "-z", "--", *paths],
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return set()
    if proc.returncode != 0:
        return set()
    return {p for p in (proc.stdout or "").split("\0") if p}


def _filter_committable_paths(project_root: Path, paths: list[str]) -> list[str]:
    """按声明路径状态过滤可提交路径（task-engine-defect-fix-unified-u2）。

    可提交性判定与声明校验（``_guard_declared_diff`` / E026）共用
    :func:`orchd.spec.declarations.path_state` 单一分类，不再各用一套磁盘
    存在性判定：

    - exists / untracked-new：磁盘存在 → 保留（与原 ``Path.exists`` 探针口径一致）；
    - tracked-deleted：磁盘缺失但 git 索引仍跟踪 → **保留**，让 ``git add`` 把
      删除态纳入暂存（否则 done 自动提交拿到空列表 ``no_changes``，声明内删除
      无法收口，E010 hint 又无引擎可执行路径）；
    - ghost：磁盘缺失且未跟踪 → 剔除（避免 git add 遇错误 pathspec 整体 fatal，
      连带阻断其余存在路径的提交；与旧行为等价）。

    「索引里已有暂存改动」的路径无条件保留（:func:`_staged_paths`）：暂存删除后
    该路径已不在 ``git ls-files`` 中、会被判成 ghost，但索引里记录着引擎（或
    agent）已完成的暂存——丢弃它会让 done 重试恒 ``no_changes``（commit_failed
    留下 staged 删除的确定性死循环，u2 审查 F1）。该判据与存在性分类正交。

    跟踪信息不可用（非 git / git 探测失败）时 ``path_state`` 降级为 exists/ghost，
    fail-closed 语义与旧实现一致（未知一律按不存在剔除）。
    """
    from orchd.spec.declarations import (
        PATH_STATE_GHOST,
        git_tracked_files,
        path_state,
    )

    tracked = git_tracked_files(project_root)
    staged = _staged_paths(project_root, paths)
    existing_paths: list[str] = [
        p
        for p in paths
        if (_rel_path(project_root, p) in staged
            or path_state(p, project_root, tracked_files=tracked) != PATH_STATE_GHOST)
    ]
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


def _split_subject_body(message: str) -> tuple[str, str]:
    """commit message 拆分为 subject + body（task-review-rework-scope）。

    首段做 subject（单行超 100 字符截断注记），余下做 body——`git log
    --oneline` 不再出现 KB 级标题行；完整原文仍存账本（DONE changes_description）
    与响应 message，可审计。
    """
    text = (message or "").strip()
    if "\n\n" in text:
        subject, _, body = text.partition("\n\n")
    else:
        subject, _, body = text.partition("\n")
    subject = " ".join(subject.split())
    if len(subject) > 100:
        subject = subject[:97] + "..."
    return subject, body.strip()


def _rollback_new_staged(project_root: Path, paths: set[str]) -> tuple[bool, str]:
    """只撤销本次 ``git add`` 新增的暂存路径，调用前已有的暂存保持不变。

    done / 归档自动提交失败时，此前已 ``git add`` 的内容会留在索引里，挡住下一轮
    完整性门禁。这里按「add 前后暂存集差」精确回滚本次新增项，绝不 ``reset``
    调用方原有的暂存（task-cont-archive-commit-rollback AC1）。

    Returns:
        ``(rolled_back, rollback_error)``——成功撤销返回 ``(True, "")``；没有新增项
        返回 ``(False, "")``；git 失败返回 ``(False, <原因>)``。
    """
    if not paths:
        return False, ""
    try:
        proc = _run_git(project_root, ["reset", "-q", "--", *sorted(paths)])
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]
    if proc.returncode != 0:
        return False, ((proc.stderr or proc.stdout).strip()[:300] or "git reset failed")
    return True, ""


def _commit_failure_result(
    project_root: Path,
    newly_staged: set[str],
    reason: str,
    message: str,
    *,
    staged_snapshot_unavailable: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """构造提交失败结果，并回滚本次新增 staged 路径。

    ``staged_snapshot_unavailable=True`` 表示 add 前/后的暂存集快照失败，无法
    可靠算出"本次新增"——此时**跳过** rollback（宁可留下 staged 交人工，也不能
    reset 掉调用前已有的 staged 内容），并显式记录该降级原因。
    """
    if staged_snapshot_unavailable:
        rolled_back, rollback_error = False, ""
    else:
        rolled_back, rollback_error = _rollback_new_staged(project_root, newly_staged)
    result: dict[str, Any] = {
        "performed": False,
        "reason": reason,
        "message": message,
        "rolled_back": rolled_back,
        **extra,
    }
    if staged_snapshot_unavailable:
        result["staged_snapshot_unavailable"] = True
    if rolled_back:
        result["rollback_reason"] = reason
    if rollback_error:
        result["rollback_error"] = rollback_error
    return result


def _commit_filtered_paths(
    project_root: Path,
    paths: list[str],
    message: str,
) -> dict[str, Any]:
    """执行 git add + diff + commit，返回结构化结果。

    merge 冲突现场（``MERGE_HEAD``）委托 :func:`close_merge_in_progress`：
    与 ``orchd git merge --continue`` 共用同一实现（暂存声明路径 → 校验暂存集
    ⊆ 声明集 ∪ trunk 父侧带入集 → 不带 pathspec 提交），越界即
    ``staged_out_of_scope`` 拒绝（task-engine-defect-fix-unified-u5 AC2）。
    单一出口避免 done 与代理各写一套越界门禁（u5 审查 B4）。
    """
    if merge_in_progress(project_root):
        # merge 冲突现场：唯一收口实现在 :func:`close_merge_in_progress`
        # （``orchd git merge --continue`` 与 done 自动提交共用，不另写一套
        # 越界门禁/无 pathspec 提交；u5 审查 B4「单一出口」）。
        result = close_merge_in_progress(project_root, paths, message)
        if result.get("performed"):
            return {
                "performed": True,
                "reason": "committed",
                "message": message,
                "commit_sha": result.get("commit_sha"),
            }
        return result
    # 记录 add 前的暂存集：失败时只撤销本次新增项（task-cont-archive-commit-rollback AC1）。
    # errata EE1：快照失败返回 None——此时无法算"本次新增"，必须跳过 rollback 而不是
    # 把 None/空集当作"调用前没有 staged"（否则会 reset 掉用户/前序任务已有暂存）。
    before_staged = _all_staged_paths(project_root)
    snapshot_unavailable = before_staged is None
    # 只 add 声明范围。add 异常/非零必须显式失败，不得吞掉后让 diff 误判 no_changes。
    try:
        add = _run_git(project_root, ["add", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return _commit_failure_result(
            project_root, set(), "commit_failed",
            f"git add failed: {exc}",
            staged_snapshot_unavailable=snapshot_unavailable)
    if add.returncode != 0:
        return _commit_failure_result(
            project_root, set(), "commit_failed",
            (add.stderr or add.stdout).strip()[:300] or "git add failed",
            staged_snapshot_unavailable=snapshot_unavailable)
    after_staged = _all_staged_paths(project_root)
    if before_staged is None or after_staged is None:
        snapshot_unavailable = True
        newly_staged: set[str] = set()
    else:
        newly_staged = after_staged - before_staged
    # 范围内是否有 staged 改动：0=无，1=有，其余为 git 错误
    try:
        diff = _run_git(
            project_root, ["diff", "--cached", "--quiet", "--", *paths],
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return _commit_failure_result(
            project_root, newly_staged, "commit_failed", "git diff failed",
            staged_snapshot_unavailable=snapshot_unavailable)
    if diff.returncode == 0:
        return {"performed": False, "reason": "no_changes", "message": message}
    if diff.returncode != 1:
        return _commit_failure_result(
            project_root,
            newly_staged,
            "commit_failed",
            (diff.stderr or diff.stdout).strip()[:300] or "git diff --cached failed",
            staged_snapshot_unavailable=snapshot_unavailable,
        )
    # commit 同样限定 paths：不提交声明范围外的 staged 内容，不 push。
    # 写操作使用独立超时预算（_GIT_COMMIT_TIMEOUT）：pre-commit hook 等写路径
    # 单次可越过读操作 10s 上限，超时不再吞成 commit_failed（AC1/AC2）。
    # task-review-rework-scope：首段做 subject、正文放 body（git -m ×2），
    # 响应 message 仍回传完整原文（可审计）。
    subject, body = _split_subject_body(message)
    commit_argv = ["commit", "-m", subject]
    if body:
        commit_argv += ["-m", body]
    commit_argv += ["--", *paths]
    try:
        commit = _run_git(
            project_root,
            commit_argv,
            timeout=_GIT_COMMIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup_index_locks(project_root)
        return _commit_failure_result(
            project_root,
            newly_staged,
            "commit_timeout",
            "git commit timed out",
            timeout_seconds=_GIT_COMMIT_TIMEOUT,
            detail=str(exc) if str(exc) else "subprocess.TimeoutExpired",
            staged_snapshot_unavailable=snapshot_unavailable,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return _commit_failure_result(
            project_root, newly_staged, "commit_failed", "git commit failed",
            staged_snapshot_unavailable=snapshot_unavailable)
    if commit.returncode != 0:
        return _commit_failure_result(
            project_root,
            newly_staged,
            "commit_failed",
            (commit.stderr or commit.stdout).strip()[:300] or "git commit failed",
            staged_snapshot_unavailable=snapshot_unavailable,
        )
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


def close_merge_in_progress(
    project_root: Path,
    paths: list[str],
    message: str | None = None,
) -> dict[str, Any]:
    """受管收口 merge 冲突现场：暂存声明路径 → 校验暂存集 → 不带 pathspec 提交。

    这是 ``orchd git merge --continue``（红线 #1 豁免二的收口形态）的执行体，
    也是 done 自动提交在 merge 状态下的同一实现——**唯一**的 merge 收口出口，
    不对 agent 暴露 ``git add`` / 带 pathspec 的 ``commit``。

    Args:
        project_root: 仓库根目录（任务 worktree）。
        paths: 任务声明路径（files_to_edit ∪ exempt_files）。
        message: 提交消息；``None`` 时用 ``git commit --no-edit`` 沿用 MERGE_MSG。

    Returns:
        结构化结果（永不抛异常）：
            - ``{"performed": True, "reason": "merged", "commit_sha": <sha>}``；
            - ``{"performed": False, "reason": "merge_not_in_progress"}``；
            - ``{"performed": False, "reason": "no_changes"}`` 已无待收口改动；
            - ``{"performed": False, "reason": "staged_out_of_scope",
              "staged_out_of_scope": [<paths>]}`` 暂存集超出声明 ∪ 父侧带入集；
            - ``commit_timeout`` / ``commit_failed``（同普通提交）。
    """
    if not merge_in_progress(project_root):
        return {"performed": False, "reason": "merge_not_in_progress"}
    try:
        add = _run_git(project_root, ["add", "-A", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return {"performed": False, "reason": "commit_failed",
                "message": f"git add failed: {exc}"}
    if add.returncode != 0:
        return {"performed": False, "reason": "commit_failed",
                "message": (add.stderr or add.stdout).strip()[:300] or "git add failed"}
    declared = {_rel_path(project_root, p) for p in paths}
    staged_paths = _all_staged_paths(project_root)
    if staged_paths is None:
        return {"performed": False, "reason": "staged_snapshot_unavailable",
                "message": "merge 收口前暂存集快照失败，跳过本次收口以避免误判越界"}
    extras = sorted(
        staged_paths - declared - _merge_parent_side_paths(project_root)
    )
    if extras:
        return {
            "performed": False,
            "reason": "staged_out_of_scope",
            "message": "merge 中暂存改动超出任务声明范围",
            "staged_out_of_scope": extras,
        }
    # 无待收口暂存改动 → no_changes（与普通提交路径语义一致，done 重试幂等）。
    try:
        staged_diff = _run_git(
            project_root, ["diff", "--cached", "--quiet"],
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git diff failed"}
    if staged_diff.returncode == 0:
        return {
            "performed": False,
            "reason": "no_changes",
            "message": message or "merge: 无待收口改动",
        }
    if staged_diff.returncode != 1:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (
                (staged_diff.stderr or staged_diff.stdout).strip()[:300]
                or "git diff --cached failed"
            ),
        }
    commit_argv = ["commit"]
    if message is None:
        commit_argv += ["--no-edit"]
    else:
        subject, body = _split_subject_body(message)
        commit_argv += ["-m", subject]
        if body:
            commit_argv += ["-m", body]
    try:
        commit = _run_git(project_root, commit_argv, timeout=_GIT_COMMIT_TIMEOUT)
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
    return {
        "performed": True,
        "reason": "merged",
        "message": message or "merge: 收口已解决的冲突",
        "commit_sha": _head_sha(project_root),
    }


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