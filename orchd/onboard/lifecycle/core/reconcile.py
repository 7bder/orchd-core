"""lifecycle core reconcile 域 — done 前置两闸口（基线新鲜度 + 对账）。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

两闸口同源（复用 ``gitops_ops.reconcile_with_main``）但语义不同：
``_check_base_freshness`` 只拦真冲突（apply=False 纯只读），
``_reconcile_before_verify`` 冲突时落地保留合并现场（apply=True）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError


# 受管出口单一生成源（task-guidance-hint-managed-git-form AC2）：任务分支同步 main
# 的唯一合法命令串（rules/git.md 红线 #1 豁免二的精确形态）。与 amend_patch_cmd
# 同构的单源纪律——hint/文案引用本常量，禁止在他处再贴字面量拼装；存量字面量
# （guide.py E015 词条 / proxy.py / review.py / gitops_ops.py 的既有提示）属各自
# 任务的既有交付，本常量自本任务起作为新增文案的唯一起点。
MANAGED_MERGE_MAIN_CMD = "orchd git merge main"


def _check_base_freshness(
    project_root: Path | None,
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> None:
    """done 前置基线新鲜度闸口（task-done-base-freshness）。

    堵当前缺口：main 已持续推进（本仓库 main 已 297+ completed）时，任务分支基线与
    main 漂移。既有 :func:`_reconcile_before_verify`（挂载点 ① 的 apply=True 落地对账）
    只在「main 自 merge-base 推进且触及本任务改动文件」时才落到 merge-tree 探测冲突，
    且 apply=True 语义是在任务 worktree **落地合并并保留现场**供实现者解决——对一个
    「先要阻断」的闸口而言太重。

    本闸口与 reconcile 同源判定（复用 :func:`reconcile_with_main`，apply=False 纯只读
    探测，不落地、不动工作区、不建 MERGE_HEAD），但语义收紧为**只拦真正冲突**：

    - main 未推进 / 任务分支不存在 / 无文件交集 / 可干净合并 → **放行**（由 code
      APPROVED 的 merge 兜底；可干净快进的任务分支不阻断，是为避免 deadlock——
      实现者在 done 前不得手动 merge main（红线 #1；唯一合法同步出口是任务分支上的
      受管命令，见 :data:`MANAGED_MERGE_MAIN_CMD`），落后但无冲突就应允许 done）；
    - main 推进且与 task 实际改动**同文件冲突** → 阻断，返回含「请先经受管出口
      同步 main 并重跑 verify」hint 的 E015，早发现避免 review 期 E015。

    git 故障等异常 → best-effort 降级为 degraded_guard，不阻断 done（零回归）。
    仅只读探测，不产生任何 git 写操作；与 reconcile 互补——本闸口聚焦「冲突即拦」，
    apply=True 对账聚焦「已阻断后落地解决现场」。
    """
    if project_root is None:
        return
    from orchd.gitops_ops import reconcile_with_main as _reconcile_with_main

    # 任务分支不存在（flat 降级 / 未建独立分支）→ 无对账对象，放行
    # （reconcile_with_main 内部对 no_branch 返回 checked=False，先快速跳过省开销）
    probe = _probe_task_branch(str(project_root), task_id)
    if not probe:
        return
    try:
        rec = _reconcile_with_main(project_root=project_root, task_id=task_id, apply=False)
    except Exception as exc:  # reconcile 本不抛，防御性收纳
        degraded_guards.append({
            "guard": "base_freshness",
            "task_id": task_id,
            "error": str(exc),
            "hint": (
                "基线新鲜度判定异常降级（best-effort），done 继续；"
                "如反复出现请检查仓库 git 状态"
            ),
        })
        return
    # 未探测(checked=False)或未冲突(clean=True) → 放行
    if not rec.get("checked") or rec.get("clean", True):
        return
    # 真正冲突：main 推进且与本任务文件同路径冲突 → 阻断 early
    from orchd.line_ctx import resolve_task_branch_for

    raise OrchdError(
        ErrorCode.E015,
        "stale_base: 任务分支落后 trunk 且与 trunk 存在文件冲突",
        [{
            "task_id": task_id,
            "branch": resolve_task_branch_for(project_root, task_id),
            "files": rec.get("files") or [],
            "hint": (
                "任务分支落后 main 且同文件冲突：在任务分支执行受管出口 "
                f"{MANAGED_MERGE_MAIN_CMD} 解决冲突并 commit，随后重跑 verify 并重试 "
                "done，避免带陈旧基线提交后续 review 期暴露 drift"
            ),
        }],
    )


def _probe_task_branch(workdir: str, task_id: str) -> bool:
    """任务分支存在性（best-effort）：不存在返回 False（由调用方放行）。"""
    import subprocess

    from orchd.line_ctx import resolve_task_branch_for

    branch = resolve_task_branch_for(workdir, task_id)
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return True  # 探不明一律当作存在，交由 reconcile 判定


def _reconcile_before_verify(
    project_root: Path | None,
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """done 前置对账（task-done-reconcile-main 挂载点 ①）。

    与 main 对账把冲突左移到实现者手里：main 自本任务 merge-base 以来推进且触及
    本任务改动文件时，冲突在 done 阶段即被检出（原本要等 code APPROVED 的 merge
    才爆发，代价是两轮审查全部作废——实测 task-test-cli-split-domains 4h15m / 1 文件）。

    与 reviewer 认领挂载点**共用同一函数与同一触发条件**
    （:func:`orchd.gitops_ops.reconcile_with_main`，防双写漂移），区别仅在
    ``apply=True``：冲突时在任务 worktree 内保留合并现场（``MERGE_HEAD``）供实现者
    直接解决，并以权威真源（``unmerged_paths``）校正冲突清单。

    冲突 → 抛 E015（附真实冲突清单 + 可执行指引）；git 不可用等异常 → best-effort
    降级为 degraded_guard，不阻断 done（零回归）。
    """
    if project_root is None:
        return None
    try:
        from orchd.gitops_ops import reconcile_with_main

        rec = reconcile_with_main(project_root, task_id, apply=True)
    except Exception as exc:
        degraded_guards.append({
            "guard": "reconcile_with_main",
            "task_id": task_id,
            "error": str(exc),
            "hint": "done 前置对账异常降级（best-effort），done 继续；"
                    "如反复出现请检查仓库 git 状态",
        })
        return None
    if rec.get("reason") != "conflict":
        return rec
    raise OrchdError(
        ErrorCode.E015,
        "merge_conflict: 任务分支与 main 对账冲突（done 前置）",
        [{
            "task_id": task_id,
            "conflict_files": rec.get("files", []),
            "worktree": rec.get("worktree"),
            "reason": rec.get("reason"),
            "hint": rec.get("action"),
        }],
    )
