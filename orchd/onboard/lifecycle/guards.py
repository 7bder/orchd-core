"""Orchd 任务生命周期管理 - lifecycle/guards 域。

迁移自 orchd/onboard.py（task-split-onboard-lifecycle-guards）：
  - _guard_cross_worktree_dirty: 跨 worktree 脏写检测（S-A2 阶段 2，fail-closed）
  - _guard_declared_diff: 声明文件必须进入任务分支 diff（红线 #13 硬门禁）
  - _guard_zero_residual: 提交零残留门禁（task-engine-done-integrity-gate）
  - _guard_out_of_scope: 越界改动检测（红线 #3 引擎兜底，task-concurrency-hardening）

每个门禁含嵌套 guard 函数（_dirty_overlap_guard / _declared_diff_guard /
_residual_guard / _out_of_scope_guard），随外层一并迁移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops import (
    GUARD_FAIL_CLOSED,
    branch_exists,
    check_workspace_state,
    get_default_branch as _get_default_branch,
    is_task_worktree,
    list_tracked_changes,
    run_guard,
)


def _guard_cross_worktree_dirty(
    project_root: Path | None,
    files_to_edit: list[str],
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> None:
    """S-A2 阶段 2（fail-closed 门禁）：跨 worktree 脏写检测。

    task-engine-done-integrity-gate：active 任务的声明文件不应出现在主工作树未提交
    改动中（防止测试/实现写在 main 而任务分支漏提交）。跑不起来时放行 = 主工作树
    脏写漏检；done 可安全重试，故选择阻断并留痕。
    """
    if not project_root:
        return

    def _dirty_overlap_guard() -> list[str]:
        if not is_task_worktree(project_root):
            raise NotApplicableError(
                "非独立任务 worktree（flat 单工作树 / 容器降级模式）："
                "跨 worktree 脏写检测不适用（无独立主工作树可比对）"
            )
        from orchd.worktree import main_worktree_dirty_overlap

        return main_worktree_dirty_overlap(project_root, files_to_edit)

    overlap = run_guard(
        _dirty_overlap_guard,
        guard_name="main_worktree_dirty_overlap",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={"task_id": task_id, "command": "done"},
        hint="检测未生效，已 fail-closed 阻断 done；确认 git 环境后重试 done",
        degraded=degraded_guards,
    ) or []
    if overlap:
        raise OrchdError(
            ErrorCode.E017,
            "dirty_workspace: 主工作树存在与当前任务 files_to_edit 重叠的未提交改动",
            [{
                "task_id": task_id,
                "overlap_files": overlap,
                "files_to_edit": files_to_edit,
                "hint": (
                    "这些文件应在任务 worktree 内修改并由 done 提交；"
                    "请先提交/还原主工作树改动后重试"
                ),
            }],
        )


def _guard_declared_diff(
    project_root: Path | None,
    task_id: str,
    files_to_edit: list[str],
    degraded_guards: list[dict[str, Any]],
) -> None:
    """声明文件必须进入任务分支 diff（红线 #13 硬门禁）。

    门禁自身出故障 → fail-closed 抛 E030 留痕；声明文件缺失 → 抛 E010。
    """
    if not (project_root and files_to_edit):
        return

    def _declared_diff_guard() -> list[dict[str, str]]:
        if not is_task_worktree(project_root):
            raise NotApplicableError(
                "非独立任务 worktree（flat / 容器降级模式）：声明文件分支 diff "
                "门禁不适用（本次未生效，声明完整性仅由 review 期诊断兜底）"
            )
        from orchd.worktree import diagnose_missing_branch_files

        return diagnose_missing_branch_files(project_root, task_id, files_to_edit)

    diagnosed = run_guard(
        _declared_diff_guard,
        guard_name="diagnose_missing_branch_files",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={"task_id": task_id, "command": "done"},
        hint="声明文件 diff 校验未生效，已 fail-closed 阻断 done；确认 git 环境后重试",
        degraded=degraded_guards,
    ) or []
    if not diagnosed:
        return

    missing_names = [d["file"] for d in diagnosed]
    reason_summary = {d["file"]: d["reason"] for d in diagnosed}
    reasons = {d["reason"] for d in diagnosed}
    hints = []
    if "path_not_found" in reasons:
        hints.append(
            "path_not_found: 文件在磁盘不存在，请修正 files_to_edit "
            "路径或从声明中移除"
        )
    if "gitignored" in reasons:
        ignored = [
            f"{d['file']} ({d['detail']})"
            for d in diagnosed if d["reason"] == "gitignored"
        ]
        hints.append(
            f"gitignored: 文件被 .gitignore 忽略 — {'; '.join(ignored)}。"
            "请调整 ignore 规则或从 files_to_edit 移除"
        )
    if "not_committed" in reasons:
        hints.append(
            "not_committed: 文件已修改但未提交到任务分支，"
            "请 git add + commit"
        )
    raise OrchdError(
        ErrorCode.E010,
        "file_conflict: 声明文件未进入任务分支 diff",
        [{
            "task_id": task_id,
            "missing_declared_files": missing_names,
            "reasons": reason_summary,
            "files_to_edit": files_to_edit,
            "hint": " | ".join(hints),
        }],
    )


def _guard_zero_residual(
    project_root: Path | None,
    task_id: str,
    files_to_edit: list[str],
    degraded_guards: list[dict[str, Any]],
) -> None:
    """提交零残留门禁（task-engine-done-integrity-gate）。

    git 探测故障 ≠ git 不可用，一律按三态区分：故障 → fail-closed E030；
    不适用（非 git）→ 降级留痕；残留存在 → 抛 E017。
    """
    if not (project_root and files_to_edit):
        return

    def _residual_guard() -> list[str]:
        st = check_workspace_state(project_root)
        if st.get("state") == "error":
            raise RuntimeError(
                f"git 探测故障: {st.get('error') or st.get('reason')}"
            )
        if not st.get("available"):
            raise NotApplicableError(
                f"git {st.get('reason') or 'unavailable'}：提交零残留校验不适用"
            )
        tracked = list_tracked_changes(project_root)
        if tracked is None:
            raise RuntimeError(
                "git status 探测失败（list_tracked_changes 返回 None）"
            )
        return [f for f in tracked if f in files_to_edit]

    residual = run_guard(
        _residual_guard,
        guard_name="commit_zero_residual",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={"task_id": task_id, "command": "done"},
        hint="零残留校验未生效，已 fail-closed 阻断 done；确认 git 环境后重试",
        degraded=degraded_guards,
    ) or []
    if not residual:
        return
    raise OrchdError(
        ErrorCode.E017,
        "dirty_workspace: files_to_edit 范围内仍有未提交跟踪改动",
        [{
            "task_id": task_id,
            "residual_files": residual,
            "hint": "引擎自动提交未覆盖这些文件，请先提交后重试 done",
        }],
    )


def _guard_out_of_scope(
    project_root: Path | None,
    task_def: dict[str, Any],
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> None:
    """越界改动检测（红线 #3 引擎兜底，task-concurrency-hardening）。

    对任务分支相对 main 的「实际改动文件」与 files_to_edit ∪ exempt_files 显式
    比照；detected 越界 → 抛 E010。仅"环境不适用"允许降级且必须留痕，校验故障
    （git 超时 / 解析失败）fail-closed 阻断。
    """
    if not project_root:
        return
    allowed: set[str] = set(task_def.get("files_to_edit", []))
    allowed |= set(task_def.get("exempt_files", []))
    # 固定资产豁免（对齐 L3 hook 的 amend 自动提交豁免）
    allowed |= {".orchd/_master.json", ".orchd/IDEAS.md"}

    def _out_of_scope_guard() -> list[str]:
        state = check_workspace_state(project_root)
        state_name = state.get("state")
        if state_name == "unavailable":
            raise NotApplicableError(
                f"git {state.get('reason')}：越界改动检测不适用"
            )
        if state_name == "error":
            raise RuntimeError(
                f"git 探测故障（{state.get('reason')}）：越界改动检测无法执行"
            )
        default = _get_default_branch(project_root)
        if not default:
            raise NotApplicableError(
                "无默认分支（main/master）引用：越界改动检测不适用"
            )
        exists = branch_exists(project_root, f"task/{task_id}")
        if exists is None:
            raise RuntimeError(
                f"git 探测故障：无法确认任务分支 task/{task_id} 是否存在"
            )
        if not exists:
            raise NotApplicableError(
                f"任务分支 task/{task_id} 不存在：越界改动检测不适用"
            )
        from orchd.worktree import _git_diff_names

        actual_modified = _git_diff_names(project_root, task_id)
        return [f for f in actual_modified if f not in allowed]

    out_of_scope = run_guard(
        _out_of_scope_guard,
        guard_name="out_of_scope_changes",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={"task_id": task_id, "command": "done"},
        hint="越界改动检测未生效，已 fail-closed 阻断 done；确认 git 环境后重试",
        degraded=degraded_guards,
    ) or []
    if not out_of_scope:
        return
    raise OrchdError(
        ErrorCode.E010,
        "file_conflict: 实现改动超出任务 files_to_edit∪exempt_files 声明范围",
        [{
            "task_id": task_id,
            "out_of_scope_files": sorted(out_of_scope),
            "declared_files": sorted(allowed),
            "hint": (
                "实现只允许改动 files_to_edit/exempt_files 声明内的文件。"
                "若确有必要连带修改，请先用 amend 把该文件纳入声明后重试。"
            ),
        }],
    )
