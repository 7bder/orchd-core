"""review merge 执行段（_review_submit_merge_run），依赖 branch+resolve_markers。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import subprocess

from orchd.gitops import main_worktree_root
from orchd.line_ctx import resolve_task_branch_for
from orchd.line_ctx import resolve_trunk_for
from orchd.gitops_ops import try_auto_resolve_conflict
from orchd.gitops_ops import try_git_merge
from .branch import _wt_dir_name
from .resolve_markers import _resolve_review_resolve_markers


def _review_submit_merge_run(
    *,
    project_root: Path | None,
    task_id: str,
    result: dict[str, Any],
    degraded: list[dict[str, Any]],
    authorize_reviewer_resolve: bool,
) -> tuple[dict[str, Any] | None, bool, bool]:
    """merge 执行段（原嵌套 try 体 L955-1049，6 条语句）。

    git 可用性探测 → try_git_merge → 冲突处理（自动化解 / 审查者授权解冲突兜底）→
    merge_env_error 与 merge_diagnostic 分支；冲突失败路径写 result 的 error/guidance/
    action 并保持 in_review。返回 (merge_result, auto_resolved, _nogit_single_dir)。
    """
    merge_result = None
    _nogit_single_dir = False
    if project_root:
        from orchd.nogit import git_available as _git_avail

        if not _git_avail(Path(project_root)):
            # 单目录无 git（task-nogit-single-dir-pivot）：无合并动作
            # （工作已在主目录），直接走完成路径（与 merge 成功同后续）。
            _nogit_single_dir = True
        else:
            merge_result = try_git_merge(project_root, task_id)

    auto_resolved = False
    conflict_files: list[str] = []
    if merge_result is not None and merge_result.get("conflict"):
        # task-degraded-trace-visibility：union 取 verify_command 失败经
        # degraded 透到响应；2 参测试替身向后兼容（TypeError 回退 2 参）。
        if project_root:
            try:
                auto = try_auto_resolve_conflict(project_root, task_id, degraded)
            except TypeError as _te:
                if "positional" in str(_te) or "takes" in str(_te):
                    auto = try_auto_resolve_conflict(project_root, task_id)
                else:
                    raise
        else:
            auto = None
        if auto and auto.get("resolved"):
            auto_resolved = True
        else:
            conflict_files = (auto or {}).get("conflict_files") or merge_result.get("files", [])
            # task-merge-tests-union-and-reviewer-fallback [fallback]：
            # 审查者显式授权解冲突兜底。仅在 authorize_reviewer_resolve=True
            # 时尝试解 REVIEW-RESOLVE 标记段，保留双方内容；只提交冲突文件。
            # 无授权或解冲突失败 → 回退 E015（默认仍要求实现者解）。
            reviewer_resolved = False
            if authorize_reviewer_resolve and project_root and conflict_files:
                _rr_workdir = main_worktree_root(project_root)
                _rr_trunk = resolve_trunk_for(project_root)
                _rr_branch = resolve_task_branch_for(project_root, task_id)
                # 重新触发 merge 以获得冲突工作树状态（try_git_merge 已 abort）
                subprocess.run(["git", "-C", str(_rr_workdir), "merge", "--abort"],
                               capture_output=True, timeout=10)
                subprocess.run(["git", "-C", str(_rr_workdir), "checkout", _rr_trunk],
                               capture_output=True, timeout=10)
                _rr_merge = subprocess.run(
                    ["git", "-C", str(_rr_workdir), "merge", _rr_branch],
                    capture_output=True, timeout=30,
                )
                if _rr_merge.returncode != 0:
                    reviewer_resolved = _resolve_review_resolve_markers(
                        _rr_workdir, conflict_files
                    )
            if reviewer_resolved:
                auto_resolved = True
                result["reviewer_resolve"] = "Both sides preserved"
            else:
                result["merged"] = False
                result["reason"] = "merge_conflict"
                result["conflict_files"] = conflict_files
                result["task_status"] = "in_review"
            # 通道 D：E015 merge_conflict 指引挂载（structured_error 收敛）
            # 加法式：保留原 reason/conflict_files/action，新增 error 字段
            # 使 guide.py E015 静态表指引(recovery/command/exit_type=git-diagnose)可达
            try:
                from orchd.ledger import structured_error
                _cf_text = ", ".join(conflict_files) if conflict_files else "未知"
                _e015_resp = structured_error(
                    "E015",
                    f"merge 冲突：{_cf_text}（main 已恢复，需人工裁决）",
                    [{
                        "conflict_files": conflict_files,
                        "hint": (
                            "进入任务 worktree 执行 orchd git merge main，解决冲突后 commit，"
                            "再由同一 reviewer 重试 code APPROVED"
                        ),
                    }],
                    project_root,
                )
                result["error"] = _e015_resp.get("error")
                if "guidance" in _e015_resp:
                    result["guidance"] = _e015_resp["guidance"]
            except Exception:
                pass  # best-effort：指引挂接失败不阻断冲突上报
            # P0-18：增强冲突指引——worktree 位置、文件清单、重试路径、回退命令
            _auto_action = (auto or {}).get("action")
            if _auto_action:
                result["action"] = _auto_action
            else:
                _cf_list = ", ".join(conflict_files) if conflict_files else "未知"
                result["action"] = (
                    f"merge 冲突（main 已恢复）。冲突文件：{_cf_list}。\n"
                    f"【解决步骤】\n"
                    f"  1. 进入任务 worktree 目录：cd ../{_wt_dir_name(task_id)}/\n"
                    f"     （container 布局下主工作树内无法 checkout task/{task_id} 分支）\n"
                    f"  2. 执行 orchd git merge main（受管通道，任务分支放行），"
                    f"解决冲突后 git commit\n"
                    f"  3. 由同一 reviewer 重试 code APPROVED\n"
                    f"【放弃本次审查】\n"
                    f"  orchd retract --task {task_id} --type REVIEW_CLAIMED "
                    f"--reason 'merge冲突放弃'\n"
                    f"  orchd force-status --task {task_id} --status pending "
                    f"--reason 'merge冲突回退'"
                )

    return merge_result, auto_resolved, _nogit_single_dir
