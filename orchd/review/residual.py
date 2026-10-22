"""merge 残留处置（_merge_diagnostic_action/_own_merge_residual/_own_residual_action），叶域。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import subprocess

from orchd.line_ctx import resolve_task_branch_for
from orchd.line_ctx import resolve_trunk_for


def _merge_diagnostic_action(task_id: str, diag: dict[str, Any]) -> str:
    """merge 非冲突失败的可执行指引（task-merge-failure-diagnostic）。

    ``diag`` 为 :func:`orchd.gitops_ops.try_git_merge` 的 ``merge_diagnostic``
    （stage/stderr 摘录/kind/files/hint）。保持任务 in_review 不合并；结尾附
    通用放弃通道（与 merge_env_error / merge_conflict 同形）。
    """
    kind = (diag or {}).get("kind", "unknown")
    hint = (diag or {}).get("hint", "")
    excerpt = (diag or {}).get("stderr_excerpt", "")
    head = (
        "git merge 未执行（非冲突失败，诊断已透出）：任务保持 in_review，"
        "未标记完成、未回收任务 worktree。"
    )
    body = f"【诊断】{hint}" if hint else ""
    if excerpt and kind == "unknown":
        body += f"\n【git 原文】{excerpt[:300]}"
    return (
        f"{head}\n{body}\n"
        "【修复步骤】\n"
        "  1. 按上方诊断处置\n"
        "  2. 由同一 reviewer 重试 code APPROVED\n"
        "【放弃本次审查】\n"
        f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason 'merge失败放弃'\n"
        f"  orchd force-status --task {task_id} --status pending --reason 'merge失败回退'"
    )


def _own_merge_residual(
    audit_root: Path | None,
    task_id: str,
    tip_sha: str | None,
    worktree_recycled: dict[str, Any] | None,
    branch_deleted: bool | None,
) -> list[dict[str, Any]]:
    """窄口径自家残留检查（task-merge-audit-inline，AC2）。

    只查**本任务** merge 成功后应已清理的三项；无关告警（幽灵分支 / 他任务
    残留 / cancelled 遗留）由 :func:`orchd.report.merge_audit` 全量巡检注解，
    不进入本函数（AC3：注解不拦）。

    检查项（均为"本应已清理却仍存在"= 真残留才命中）：
      1. ``worktree_not_recycled``：``worktree_recycled.removed is False``
         （回收函数已区分"无可回收"→removed=True，故 False 即真残留）；
      2. ``branch_not_cleaned``：分支删除未报告成功 **且** 分支实际仍存在
         （``git show-ref`` 验证；删除报 False 但分支已无 = 幂等口径差异，
         属良性，不拦）；
      3. ``branch_not_merged_into_main``：分支 tip 已知 **且** main 不包含它
         （``git merge-base --is-ancestor``；tip 未知时无法判定，不拦）。

    只读 git，永不抛异常（探针失败返回已确认项，不阻断完成路径）。
    """
    residual: list[dict[str, Any]] = []
    wr = worktree_recycled or {}
    if wr.get("removed") is False:
        res = wr.get("residual") or {}
        residual.append({
            "task_id": task_id,
            "reason": "worktree_not_recycled",
            "detail": res.get("reason") or wr.get("reason") or "worktree 回收未成功",
        })
    if audit_root is not None:
        try:
            root = str(audit_root)
            branch = resolve_task_branch_for(audit_root, task_id)
            trunk = resolve_trunk_for(audit_root)
            if branch_deleted is not True:
                show = subprocess.run(
                    ["git", "-C", root, "show-ref", "--verify",
                     f"refs/heads/{branch}"],
                    capture_output=True, timeout=10,
                )
                if show.returncode == 0:
                    residual.append({
                        "task_id": task_id,
                        "reason": "branch_not_cleaned",
                        "detail": f"{branch} 分支仍存在（分支删除未成功）",
                    })
            if tip_sha:
                anc = subprocess.run(
                    ["git", "-C", root, "merge-base", "--is-ancestor",
                     tip_sha, trunk],
                    capture_output=True, timeout=10,
                )
                if anc.returncode != 0:
                    residual.append({
                        "task_id": task_id,
                        "reason": "branch_not_merged_into_main",
                        "detail": f"{branch} tip {tip_sha[:7]} 未被 {trunk} 包含",
                    })
        except Exception:
            pass
    return residual


def _own_residual_action(
    task_id: str, own: list[dict[str, Any]], audit_root: Path | None
) -> str:
    """自家残留的可执行指引（task-merge-audit-inline，AC2）。

    与 merge_conflict / merge_env_error 指引同形：修复步骤 + 同一 reviewer
    重试 + 通用放弃通道。
    """
    lines = [
        "merge 已落地但本任务残留未清理：任务已退回 in_review，未标记完成。",
        "【残留】",
    ]
    for entry in own:
        reason = entry.get("reason", "unknown")
        detail = entry.get("detail", "")
        if reason == "worktree_not_recycled":
            fix = (
                "先切出任务 worktree 目录后重试回收，或运行 doctor --fix "
                "清理残留；不要在已失效的 worktree 目录内执行命令"
            )
        elif reason == "branch_not_cleaned":
            fix = (
                f"引擎自动删分支（task-merge-auto-delete-branch）失败的兜底属仓库"
                f"管理员处置：在主工作树人工执行 git branch -d task/{task_id}"
                f"（root={audit_root}），agent 不得自行执行该写操作（红线 #1）；"
                "失败则运行 doctor --fix"
            )
        elif reason == "branch_not_merged_into_main":
            fix = (
                "核对 main 是否包含本次实现（git log main 查找任务提交）；"
                "缺失则进入任务 worktree 执行 orchd git merge main 确认后重试"
            )
        else:
            fix = "按残留详情人工处置"
        lines.append(f"  - {reason}：{detail}。{fix}")
    lines += [
        "【修复步骤】",
        "  1. 按上方残留逐项处置",
        "  2. 由同一 reviewer 重试 code APPROVED",
        "【放弃本次审查】",
        f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason '自家残留放弃'\n"
        f"  orchd force-status --task {task_id} --status pending --reason '自家残留回退'",
    ]
    return "\n".join(lines)
