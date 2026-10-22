"""git 分支/在途文件探测（_git_lines/_default_branch/_inflight_files）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import subprocess

from orchd.gitops.repo import GitBackend


def _git_lines(project_root: Path, *args: str) -> list[str] | None:
    """执行 git 子命令并返回非空输出行（best-effort）。

    异常 / 非零退出 / 超时返回 None（「测不到」），调用方据此降级，
    不把测不到伪装成空集。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _default_branch(project_root: Path) -> str:
    """当前线 trunk（best-effort；取不到回退 "main"，与 worktree._git_diff_names 一致）。"""
    try:
        from orchd.line_ctx import resolve_trunk_for

        return resolve_trunk_for(project_root)
    except Exception:
        return "main"


def _inflight_files(
    project_root: Path | None, tasks: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """以 git 事实取「在途」任务 → 实际改动文件（task-inflight-conflict-visibility）。

    「在途」= 分支 ``task/<id>`` 存在且有**未落默认分支的提交**（即不在
    ``git for-each-ref --merged=<default>`` 结果内）。覆盖 done / in_review /
    force-status 悬空态 —— 判定只看「改动是否已落 main」，与任务状态字段无关，
    这正是修掉 _build_claimed_files 只看 claimed 之状态盲区的方式。

    性能：批量 2 次 git 调用（列全部 task/* 分支 + 列已并入默认分支的分支），
    与候选数 N 无关；随后仅对在途任务逐个取实际改动文件（O(M)，M = 在途任务数），
    避免 O(N×M)。任一步失败即返回已收集部分（best-effort，与
    ``worktree.actual_changes_conflict`` 的降级语义一致）。

    Args:
        project_root: 主工作树根；None / 非 git → 空结果。
        tasks: 仅登记 _master.json 已知任务的分支，避免野生分支误报。

    Returns:
        {task_id: [实际改动文件]}；仅含「有未落 main 提交且改动非空」的任务。
    """
    if project_root is None:
        return {}
    known = {t.get("id", "") for t in tasks}
    from orchd.line_ctx import resolve_task_branch_for

    prefix = resolve_task_branch_for(project_root, "")
    refspec = f"refs/heads/{prefix}"
    branches = _git_lines(
        project_root, "for-each-ref", "--format=%(refname:short)", refspec
    )
    if not branches:
        return {}
    merged = set(
        _git_lines(
            project_root,
            "for-each-ref",
            f"--merged={_default_branch(project_root)}",
            "--format=%(refname:short)",
            refspec,
        )
        or []
    )
    inflight: dict[str, list[str]] = {}
    for branch in branches:
        if branch in merged or not branch.startswith(prefix):
            continue
        tid = branch[len(prefix):]
        if tid not in known:
            continue
        # 在途=分支级事实（仅 git 有分支概念）：经端口 GitBackend 直连（与原
        # _git_diff_names 同函数；无 git 时分支循环本就为空，此处不做快照语义外溢）。
        files = GitBackend(project_root).changed_paths(tid) or []
        if files:
            inflight[tid] = files
    return inflight
