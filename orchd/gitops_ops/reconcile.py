"""gitops_ops reconcile 域 — 任务 worktree 解析与任务分支↔main 对账。

来源：``orchd/gitops_ops.py（1220 行）``（W6-C6 纯搬移）。

``reconcile_with_main`` 是 done 前置与 reviewer 认领前置**共用的单一来源**
（防双写漂移），本域只依赖 ``gitver`` 一个兄弟域。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import is_task_worktree, unmerged_paths
from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

from .gitver import _split_nonempty_lines, _split_nul, merge_tree_supported


def resolve_task_worktree(
    project_root: Path | None,
    task_id: str,
    *,
    strict: bool = False,
) -> Path | None:
    """解析对账 / 诊断 / done 所用任务 worktree（单一来源）。

    container 布局下 ``project_root`` 若是主工作树，需解析到
    ``<task_wt_root>/task-<id>``（分支实际 checkout 处）；本身即任务 worktree
    则原样返回；flat / 解析不到回退 ``project_root``（维持既有降级语义）。
    原先该逻辑内联于 ``orchd.onboard.claim._resolve_review_worktree``，此处收敛为
    单一实现，避免对账与诊断两处漂移（task-done-reconcile-main）。

    task-done-root-resolution AC2：新增 ``strict`` 参数。当 ``strict=True`` 且
    container 布局下解析不到任务 worktree（目录缺失 / 非 git）时，抛
    ``OrchdError`` 阻断并给出可执行指引，不回退主工作树、不静默写 DONE。
    flat 布局与本身即任务 worktree 时不受 strict 影响（原样返回）。
    """
    if project_root is None:
        return None
    try:
        if is_task_worktree(project_root):
            return project_root
        from orchd.worktree import _task_wt_name, detect_layout

        layout = detect_layout(Path(project_root))
        if layout.get("layout") == "container":
            cand = Path(layout["task_wt_root"]) / _task_wt_name(task_id)
            # A0b（单目录收敛后）：无 git 即无任务 worktree 概念，仅 git 登记生效。
            if (cand / ".git").exists():
                return cand
            # AC2: strict 模式下解析不到任务 worktree 时抛错阻断
            if strict:
                raise OrchdError(
                    ErrorCode.E018,
                    f"done 执行位置解析失败：container 布局下任务 worktree 不存在 "
                    f"({cand})，无法从主工作树静默 done",
                    [{
                        "task_id": task_id,
                        "expected_worktree": str(cand),
                        "hint": (
                            f"请进入任务 worktree 执行 done：cd '{cand}'; "
                            f"python .orchd/__main__.py done --task {task_id} --changes '<描述>'；"
                            f"或先重建 worktree：git worktree add {cand} "
                            f"{resolve_task_branch_for(project_root, task_id)}"
                        ),
                    }],
                )
    except Exception as e:
        # strict 模式下的 OrchdError 直接抛出，不捕获
        if strict and isinstance(e, OrchdError):
            raise
        return Path(project_root)
    return Path(project_root)


def reconcile_with_main(
    project_root: Path | None,
    task_id: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """任务分支 ↔ main 对账（**单一来源**，done 前置 + reviewer 认领前置共用）。

    根因（2026-09-11~12 并发冲突复盘）：容器布局下任务分支基线在 claim 时冻结，
    main 在任务生命周期内持续推进，直到 code APPROVED 的 merge 才首次对账 →
    冲突在「两轮审查之后」爆发，恢复路径粗暴（retract + force-status + 重实现 +
    全量重审）。实测 task-test-cli-split-domains：基线 3539ea8(13:48) → 对方
    3cfcc16 于 14:00:52 落 main（晚于 done 84 秒）→ 17:48:33 撞 modify/delete →
    22:03:16 才 completed（4h15m / 1 个文件）。

    两个挂载点**共用本函数与同一触发条件**（防双写漂移）：

    - ① ``done`` 前置（``apply=True``）：覆盖「实现期间 main 推进」；
    - ② reviewer 认领前置（``apply=False``）：覆盖「done 之后、merge 之前」残余
      窗口——实测事故正落在该窗口。

    触发条件（精确，避免分支刷满 merge commit）：**仅当** main 自 merge-base
    以来的改动与本任务分支的实际改动**有交集**时才探测；main 未推进或无交集时
    零成本跳过（不产生任何 git 写操作）。

    探测优先用零副作用的 ``git merge-tree --write-tree --name-only --no-messages``
    （本机 git 2.55.0 实测：不切分支、不动工作区、无 ``MERGE_HEAD``；输出首行为
    tree OID、其余行即真实冲突路径；配 ``-c core.quotePath=false`` 避免非 ASCII
    路径被八进制转义）；git < 2.38 降级为 ``merge --no-commit --no-ff`` 预演。

    Args:
        project_root: 主工作树根（container 下经 :func:`resolve_task_worktree`
            解析到任务 worktree）。``None`` → 不探测。
        task_id: 任务 id（分支名 ``task/{task_id}``）。
        apply: ``True``（done 挂载点）时冲突**不 abort**——在任务 worktree 内保留
            合并现场（``MERGE_HEAD``）供实现者直接解决；``False``（reviewer 认领
            挂载点）时纯探测、不落地，保证审查期工作区不被触碰（不触碰 E017）。

    Returns:
        ``{"checked", "clean", "files", "reason", "worktree", "action"}``：
        ``checked`` 是否真正探测；``clean`` 探测结果（未探测 ``None``，未触发
        ``True``）；``files`` 冲突文件（真实路径，权威）；``reason`` ∈
        ``no_branch`` / ``no_main_advance`` / ``no_overlap`` / ``clean`` /
        ``conflict`` / ``git_unavailable``；``action`` 冲突时可执行指引。

    本函数**不抛异常**（best-effort）：git 异常一律降级为
    ``reason="git_unavailable"``，调用方据 ``checked`` 决定是否采信。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    trunk = resolve_trunk_for(project_root)
    workdir = resolve_task_worktree(project_root, task_id)
    result: dict[str, Any] = {
        "checked": False,
        "clean": None,
        "files": [],
        "reason": "git_unavailable",
        "worktree": str(workdir) if workdir else None,
        "action": None,
    }
    if workdir is None:
        return result

    def _run(
        args: list[str], timeout: int = 30
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return None

    # 0) 任务分支存在性（不存在 → 无对账对象，静默降级）
    probe = _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    if probe is None:
        return result
    if probe.returncode != 0:
        result["reason"] = "no_branch"
        return result

    # 1) merge-base：任务分支冻结基线与 trunk 的分叉点
    mb = _run(["merge-base", trunk, branch])
    if mb is None or mb.returncode != 0 or not (mb.stdout or "").strip():
        return result
    base = mb.stdout.strip()

    # 2) trunk 自 merge-base 以来的改动（空 → trunk 未推进 → 零成本跳过）
    trunk_diff = _run(["diff", "--name-only", "-z", base, trunk])
    if trunk_diff is None or trunk_diff.returncode != 0:
        return result
    trunk_files = _split_nul(trunk_diff.stdout)
    if not trunk_files:
        result.update(reason="no_main_advance", clean=True)
        return result

    # 3) 本任务分支的实际改动；与 trunk 推进文件无交集 → 零成本跳过
    task_diff = _run(["diff", "--name-only", "-z", base, branch])
    if task_diff is None or task_diff.returncode != 0:
        return result
    if not (set(trunk_files) & set(_split_nul(task_diff.stdout))):
        result.update(reason="no_overlap", clean=True)
        return result

    # 4) 触发：探测可合并性
    result["checked"] = True
    clean: bool | None = None
    files: list[str] | None = None
    staged_merge = False  # 降级路径下是否已处于（保留现场的）冲突合并态

    if merge_tree_supported():
        mt = _run(
            [
                "-c", "core.quotePath=false",
                "merge-tree", "--write-tree", "--name-only", "--no-messages",
                trunk, branch,
            ]
        )
        if mt is not None and mt.returncode in (0, 1):
            if mt.returncode == 0:
                clean = True
            else:
                clean = False
                # 首行为 tree OID，其余行为冲突路径（--no-messages 已去掉信息段）
                files = _split_nonempty_lines(mt.stdout)[1:]

    if clean is None:
        # 降级（git < 2.38 或 merge-tree 执行异常）：真合并预演
        merged = _run(["merge", "--no-commit", "--no-ff", trunk])
        if merged is None:
            result["checked"] = False
            return result
        if merged.returncode == 0:
            clean = True
            _run(["merge", "--abort"])  # 撤销预演痕迹，保持分支零残留
        else:
            paths = unmerged_paths(workdir)
            if not paths:
                # merge 失败但索引无未合并条目（脏工作区等）或测不到 → 非对账结论，
                # 降级放行（不得把非冲突误报为冲突，零回归）
                result["checked"] = False
                return result
            clean = False
            files = paths
            if apply:
                staged_merge = True  # 现场已保留，供实现者解决
            else:
                _run(["merge", "--abort"])  # 纯探测：不留现场

    if clean:
        result.update(reason="clean", clean=True)
        return result

    # 冲突：apply=True 时在任务 worktree 内落地合并、保留现场并取权威清单
    conflict_files = list(files or [])
    if apply and not staged_merge:
        _run(["merge", trunk])
        authoritative = unmerged_paths(workdir)
        if authoritative:  # 非空才采信（空 = 未落地成功，保留探测清单）
            conflict_files = authoritative
    result.update(
        reason="conflict",
        clean=False,
        files=conflict_files,
        action=(
            f"任务分支 {branch} 与 {trunk} 对账发现冲突"
            f"（{len(conflict_files) or '若干'} 个文件）："
            + ("合并现场已保留在任务 worktree（MERGE_HEAD 存在），" if apply else "")
            + "请解决冲突 → git add → git commit，然后重新 done"
        ),
    )
    return result
