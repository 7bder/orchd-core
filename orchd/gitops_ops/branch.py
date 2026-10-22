"""gitops_ops branch 域 — 任务分支切换、merge / ff / 删分支与失败分类。

来源：``orchd/gitops_ops.py（1220 行）``（W6-C6 纯搬移）。

任务生命周期特定的 git 写动作；全部 best-effort（环境异常返回 None/Falsy，
真实仓库内 git 命令失败则显式上报状态字典）。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.gitops import main_worktree_root, unmerged_paths
from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for


def _git_error_summary(proc: "subprocess.CompletedProcess[str]") -> str:
    """从 git checkout 子进程提取可读错误摘要：stderr 优先，回退 stdout，截断 200 字符。"""
    text = (proc.stderr or proc.stdout or "").strip()
    return text[:200] + ("…" if len(text) > 200 else "")


def try_git_branch(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """best-effort 切换到任务分支 task/{task_id}，并显式上报 git 命令失败。

    返工场景分支已存在则 checkout 复用（task-master-single-copy：不再同步分支
    worktree 的 .orchd/_master.json 与 main——container 副本已由 sparse-checkout
    抑制、flat 布局单副本无需同步）。
    首次 claim 才 checkout -b 新建——**显式从默认分支(main/master) fork**，
    避免游离 HEAD / 孤儿分支（2026-08-28 仓库损坏根因修复）。

    返回值契约（task-git-branch-fail-report，替代原先的静默吞错）：
      - 成功创建/切换：``{"state": "ok"}``；
      - git 命令失败（真实仓库内 checkout / checkout -b 返回非零）：
        ``{"state": "failed", "step": "create" | "checkout", "error": <stderr 摘要>}``；
      - 环境异常（非 git 仓库 / git 不可用 / 子进程超时）：``None``
        ——静默降级、不抛异常（既有契约 test_non_git_dir_no_error 锁定）。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        # 环境探测：区分「非 git 仓库等环境异常」（返回 None）与
        # 「真实仓库内 git 命令失败」（返回 failed 状态字典）。
        env = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if env.returncode != 0:
            return None
        check = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if check.returncode == 0:
            checkout = subprocess.run(
                ["git", "checkout", branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if checkout.returncode == 0:
                return {"state": "ok"}
            return {
                "state": "failed",
                "step": "checkout",
                "error": _git_error_summary(checkout),
            }
        default = resolve_trunk_for(project_root)
        create = subprocess.run(
            ["git", "checkout", "-b", branch, default],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if create.returncode == 0:
            return {"state": "ok"}
        return {
            "state": "failed",
            "step": "create",
            "error": _git_error_summary(create),
        }
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _clean_stale_index_lock(workdir: Path) -> bool:
    """清理主工作树 .git/index.lock 残留锁文件。

    当 git 进程异常中断时（如 shell 被强制关闭、超时被杀），index.lock 会残留，
    阻塞后续所有 git 操作（checkout/merge/commit 等均报 ``Unable to create
    '.../index.lock': File exists``）。通过文件年龄判断锁是否已过期：
    - 存在时间 > 5 分钟 → 视为 stale，删除后返回 True。
    - 不存在或 < 5 分钟 → 不动，返回 False（可能是活跃 git 操作，不应干预）。

    best-effort：任何异常（路径不存在、权限不足等）静默降级，不阻断调用方。
    """
    try:
        lock = workdir / ".git" / "index.lock"
        if not lock.is_file():
            return False
        age = datetime.now(timezone.utc).timestamp() - lock.stat().st_mtime
        if age > 300:  # 5 分钟
            lock.unlink()
            return True
    except Exception:
        pass
    return False


def _classify_merge_failure(stderr: str) -> dict[str, Any]:
    """merge 非冲突失败分类（task-merge-failure-diagnostic）。

    ``stderr`` 为 ``git merge`` 的原始错误文本。返回
    ``{"kind", "files", "hint"}``：
    - ``untracked_collision``：untracked 同路径碰撞（附文件清单；处置为文件系统
      移动/删除，**非 git 写操作**，不违反红线；切勿 ``git add`` 它）；
    - ``local_changes``：主工作树已跟踪脏写阻断（提交/还原归属，主分支无手动提交
      豁免，须报告归属）；
    - ``unknown``：其余（含空 stderr），附原文摘录供人工判定。
    """
    text = stderr or ""
    m = re.search(
        r"untracked working tree files would be overwritten by merge:\s*\n"
        r"((?:[ \t]+.*\n?)+)",
        text,
    )
    if m:
        files = [
            ln.strip().strip('"').strip("'")
            for ln in m.group(1).splitlines()
            if ln.strip()
        ]
        files = [f for f in files if f]
        listing = "、".join(files) if files else "（未能解析）"
        return {
            "kind": "untracked_collision",
            "files": files,
            "hint": (
                f"主工作树有未跟踪文件阻挡合并：{listing}。"
                "用文件系统移走或删除它们（Remove-Item / 移出工作树，非 git 写操作，"
                "不违反红线；切勿 git add）后，由同一 reviewer 重试 code APPROVED。"
            ),
        }
    if "would be overwritten by merge" in text:
        return {
            "kind": "local_changes",
            "files": [],
            "hint": (
                "主工作树已跟踪改动阻挡合并：请先确认改动归属并提交/还原（主分支无"
                "手动提交豁免，归属不明请报告），后由同一 reviewer 重试 code APPROVED。"
            ),
        }
    excerpt = text.strip()[:300]
    return {
        "kind": "unknown",
        "files": [],
        "hint": (
            "merge 失败但无冲突、无可分类原因"
            + (f"（git 原文：{excerpt}）" if excerpt else "（git 无输出）")
            + "：确认 git 可用且主工作树干净后，由同一 reviewer 重试 code APPROVED。"
        ),
    }


def try_git_merge(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """best-effort 将任务分支合并到主工作树的 main（task-14-merge-main-tree）。

    始终在**主工作树**内执行（``git rev-parse --git-common-dir`` 定位；flat 单会话
    即 project_root，零回归），任务 worktree 永不 checkout main。merge-wt 已废弃。

    - 成功：``{"conflict": False}``
    - 内容冲突：``{"conflict": True, "files": [...]}``（files 取权威真源
      :func:`orchd.gitops.unmerged_paths`，不再用末词启发式）
    - 非冲突失败：``{"conflict": False, "merge_diagnostic": {...}}``
      （task-merge-failure-diagnostic：stage/stderr 摘录/分类，不再裸 ``None``——
      裸 ``None`` 使 review 只能报无诊断的 ``merge_env_error`` 黑洞）
    - 环境异常（checkout 失败 / git 不可用 / 抛异常）：``None``（调用方按
      best-effort 降级，行为不变）。
    """
    trunk = resolve_trunk_for(project_root)
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        workdir = main_worktree_root(project_root)
        _clean_stale_index_lock(workdir)
        checkout = subprocess.run(
            ["git", "-C", str(workdir), "checkout", trunk],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if checkout.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "-C", str(workdir), "merge", branch],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            # 权威真源：索引中的未合并路径。必须在 merge --abort **之前**取——abort 会
            # 清掉索引未合并态，之后再查必然为空（task-conflict-true-source-fix）。
            paths = unmerged_paths(workdir)
            # 三态：非空 → 冲突；空 → 非冲突失败（透出诊断，见上）；None（测不到）→
            # 保守按冲突上报且不崩。
            if paths or paths is None:
                # P2-7：冲突后立即 abort 清理 MERGE_HEAD，避免残留中间态阻塞后续 git 操作。
                # try_auto_resolve_conflict 开头会再次 abort（幂等），此处先清理无副作用。
                _abort_merge(workdir)
                return {"conflict": True, "files": paths or []}
            err = (result.stderr or "").strip()
            return {
                "conflict": False,
                "merge_diagnostic": {
                    "stage": "merge",
                    "stderr_excerpt": err[:500],
                    **_classify_merge_failure(err),
                },
            }
        return {"conflict": False}
    except subprocess.TimeoutExpired:
        # B2（task-gate-cleanup-batch）：merge 超时同样残留 MERGE_HEAD（与冲突
        # 同源）——先 abort 再返回可诊断的 None（调用方 review 报 merge_env_error
        # 附超时诊断，原裸 None 与真环境异常不可区分）。
        _abort_merge(workdir)
        return None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _abort_merge(workdir: Path) -> None:
    """merge 中止清理（B2，task-gate-cleanup-batch）：冲突/超时共用。

    best-effort：失败静默（调用方已有降级路径），10s 有界。
    """
    try:
        subprocess.run(
            ["git", "-C", str(workdir), "merge", "--abort"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass


def try_ff_merge_to_main(
    project_root: Path, task_id: str
) -> dict[str, Any] | None:
    """W-5：仅当任务分支领先 main 时以 ``--ff-only`` 快速并入 main。

    逃生舱（force-status completed）"完成 = 状态终态 + 代码落 main" 的落码环节。
    与 ``try_git_merge``（普通 merge，可产生 merge commit/自动化解）不同：**仅接受
    快进**——任务与 main 分叉时拒绝自动合并，返回 ``diverged`` 交人工处理，防止
    静默并入错误代码。

    Returns:
        - ``{"state": "merged"}``：ff 合并成功，main 前进到任务分支。
        - ``{"state": "already_in_main"}``：任务分支不领先 main（已并入 / 无独立分支）。
        - ``{"state": "diverged", "branch": <task_branch>}``：与 main 分叉，拒绝自动合并。
        - ``None``：环境异常，调用方按 best-effort 降级。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    trunk = resolve_trunk_for(project_root)

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        workdir = main_worktree_root(project_root)
        return subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )

    try:
        _ = _git("rev-parse", "--git-dir")  # 非 git 仓库探测（返回 None 降级）
        # 任务分支不存在 → 无待落码（无独立分支即视为已并入/无实现）
        if _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return {"state": "already_in_main"}
        trunk_is_ancestor = _git("merge-base", "--is-ancestor", trunk, branch).returncode == 0
        task_is_ancestor = _git("merge-base", "--is-ancestor", branch, trunk).returncode == 0
        if trunk_is_ancestor and task_is_ancestor:
            # trunk 与任务分支同 commit → 无待落码（已并入）
            return {"state": "already_in_main"}
        if trunk_is_ancestor:
            # 任务领先 trunk → 可快进。先确认主工作树落到 trunk（flat 布局下主工作树
            # 可能正 checkout 任务分支），再 --ff-only 快进。
            if _git("checkout", trunk).returncode != 0:
                return {"state": "diverged", "branch": branch}
            if _git("merge", "--ff-only", branch).returncode == 0:
                return {"state": "merged"}
            # 快进被拒（并发/脏工作区）→ 交人工，防止静默完成却不落码
            return {"state": "diverged", "branch": branch}
        if task_is_ancestor:
            return {"state": "already_in_main"}
        # 两者互为祖先均不成立 → 分叉，拒绝自动合并（仅接受快进，防止静默并入错误代码）
        return {"state": "diverged", "branch": branch}
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def try_delete_task_branch(project_root: Path, task_id: str) -> bool:
    """best-effort 删除任务分支 task/{task_id}（merge 成功后调用）。

    分支删除以**主工作树**为稳定 cwd（git -C）：容器布局下 project_root 是任务
    worktree，task/{id} 曾由该 worktree checkout，git 拒绝删除被占用分支；worktree
    已回收后（remove_task_wt 或分支已删）分支不再被占用，-d 成功或报分支不存在。
    flat 布局 main_worktree_root 回退 project_root，cwd 即主工作树，零回归。

    Returns:
        True：删除成功，或分支已不存在（幂等视为成功，best-effort 不抛异常）。
        False：删除失败或环境不支持。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        workdir = main_worktree_root(project_root)
        result = subprocess.run(
            ["git", "-C", str(workdir), "branch", "-d", branch],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return True
        # 分支已不存在（如 remove_task_wt 的 -D 已删）→ 幂等视为成功
        err = (result.stderr or "").lower()
        if (
            "not found" in err
            or "doesn't exist" in err
            or "does not exist" in err
        ):
            return True
        return False
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
