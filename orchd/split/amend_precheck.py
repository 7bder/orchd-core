from __future__ import annotations

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_current_branch, get_default_branch, list_tracked_changes
from orchd.intake import _INTAKE_PRODUCT_FILES
from orchd.spec import Master
from pathlib import Path
from typing import Any

def _amend_precheck(
    orchd_dir: Path, master: Master
) -> tuple[
    Path,
    list[dict[str, Any]],
    list[dict[str, Any]],
    Path,
    str,
    list[dict[str, Any]],
    list[str],
    list[dict[str, Any]],
    list[str],
    list[str],
    list[str],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
]:
    """amend 前置段：参数/分支/脏区解析与变更累加器准备（原 amend 体 L407-474）。

    含红线 7 分支守卫（E007 invalid_branch）与摄入产物外的脏工作区守卫（E017）；
    返回后续段所需的声明对象与全部变更累加器（顺序与本函数 return 一致）。
    """
    orchd_dir = Path(orchd_dir)
    tasks = master.tasks
    modules = master.modules

    # 红线 7：amend 只在 default（main）分支（= canonical 主工作树）执行。
    # task-master-single-copy：废除 task 分支 amend 例外——container 下任务
    # worktree 不再保留 .orchd/_master.json（sparse-checkout 抑制），唯一权威 =
    # 主工作树；任务分支上补声明改由"认领后、动手前在主工作树补 files_to_edit"
    # 流程承接。任务分支调用 amend 一律拒绝注册（E007，含 task/xxx），而非降级
    # 为"仅不提交"，避免污染 master 注册来源。分支判定仅在 amend 阶段强制：
    # init / validate / status 等只读或冷启动命令不受限。
    project_root = orchd_dir.parent
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_branch: amend 仅在 default（{default_branch}）分支执行（主工作树），"
            f"当前分支 {current_branch} 拒绝注册（红线 7）",
            [{
                "reason": "invalid_branch",
                "branch": current_branch,
                "default": default_branch,
                "hint": (
                    f"任务分支不再允许 amend；请在主工作树 {project_root} 上补充/注册 "
                    "files_to_edit 等声明后再执行"
                ),
            }],
        )

    # intake-commit-enforcement（2026-08-14）：amend 前置"非摄入产物干净"守卫。
    # 摄入产物（IDEAS.md / ROADMAP.md / _master.json，两种布局）允许未提交态进入
    # （摄入 → amend 的正当链路，引擎随后强制提交）；摄入产物之外的任何已跟踪
    # 改动 → E017 阻断注册——避免脏工作区被 checkout -b 带进任务分支，对齐
    # claim/done/review 的 require_clean 语义（lint：amend 是四条写命令中此前
    # 唯一无干净度守卫的）。
    dirty_files = list_tracked_changes(project_root)
    if dirty_files is not None:
        non_intake = [f for f in dirty_files if f not in _INTAKE_PRODUCT_FILES]
        if non_intake:
            raise OrchdError(
                ErrorCode.E017,
                "dirty_workspace: amend 要求除摄入产物（IDEAS.md / ROADMAP.md / "
                "_master.json）外工作区干净",
                [{
                    "command": "amend",
                    "dirty_files": non_intake,
                    "hint": (
                        "请先提交或还原摄入产物之外的文件改动"
                        "（untracked 工具/配置文件不阻塞）"
                    ),
                }],
            )

    errors: list[dict[str, Any]] = []
    updated_tasks: list[str] = []
    whitelisted_updates: list[dict[str, Any]] = []
    new_tasks: list[str] = []
    unchanged_tasks: list[str] = []
    sources_missing: list[str] = []
    sources_invalid: list[str] = []
    source_errors: list[dict[str, Any]] = []
    attachable_sync: list[dict[str, Any]] = []
    terminal_decl_sync: list[dict[str, Any]] = []
    # task-terminal-spec-revision-channel：本次终态规格文本修订明细（写审计事件 + 回响应）
    terminal_text_revisions: list[dict[str, Any]] = []
    # task-amend-orphan-prune：本次清除的孤儿 module 快照目录（module_id 列表，留痕）
    pruned_modules: list[str] = []
    return (
        orchd_dir,
        tasks,
        modules,
        project_root,
        default_branch,
        errors,
        updated_tasks,
        whitelisted_updates,
        new_tasks,
        unchanged_tasks,
        sources_missing,
        sources_invalid,
        source_errors,
        attachable_sync,
        terminal_decl_sync,
        terminal_text_revisions,
        pruned_modules,
    )
