"""lifecycle core stages 域 — S-A2 前置预校验与自动提交+完整性门禁。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

``_done_precheck``（无锁快速失败）、``_commit_and_verify_integrity``（提交 + 四门禁
编排；``commit_files`` 表达式是 git.md 口径的对拍真源）、``_done_auto_commit``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    ensure_committed,
    guard_clean_workspace as _guard_clean_workspace,
    guard_done_branch as _guard_done_branch,
)
from orchd.ledger import Store
from orchd.contracts.task_schema import (
    EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
    NAME_FIELD,
)

from orchd.onboard.lifecycle.guards import (
    _guard_declared_diff,
    _guard_out_of_scope,
    _guard_zero_residual,
)


def _done_precheck(
    store: Store,
    tasks: list[dict[str, Any]],
    task_id: str,
    agent_id: str,
    project_root: Path | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """S-A2 阶段 1：无锁预校验（快速失败）。

    仅要求任务处于 claimed 状态，不比对 caller 指纹与 claimed_by——宿主会话身份漂移
    场景下，done 按认领者记账（见锁内 author_mismatch）。返回 task_def /
    files_to_edit / degraded_guards（门禁降级登记表，随阶段透传、收尾统一挂响应）。

    task-flat-decl-authority：task_def 经 :func:`orchd.worktree.resolve_declaration_source`
    解析（flat 下从 main blob 读权威声明，防任务分支本地副本陈旧）；返回的 task_def
    随阶段下传复用，本 done 调用内 blob 只读一次。
    """
    degraded_guards: list[dict[str, Any]] = []
    from orchd.worktree import resolve_declaration_source
    tasks = resolve_declaration_source(project_root, tasks, degraded_guards)[0]
    task_def = None
    for t in tasks:
        if t.get(ID_FIELD) == task_id:
            task_def = t
            break
    if task_def is None:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not found in task list",
            [{"task_id": task_id}],
        )
    files_to_edit: list[str] = list(task_def.get(FILES_TO_EDIT_FIELD, []))

    state = store.replay()
    ts = state.get(task_id)
    # B1（task-flat-decl-authority）：done 重复调用语义——首次 done 后任务离 claimed
    # 态，再次 done 在此闸口 E007 拒绝，不写第二个 DONE 事件（幂等安全）。
    if not ts or ts.status != "claimed":
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not in claimed state",
            [{"task_id": task_id, "expected": "claimed", "actual": ts.status if ts else "pending"}],
        )

    if project_root:
        _guard_done_branch(
                project_root,
                task_id=task_id,
                orchd_dir=store.orchd_dir,
                agent_id=agent_id,
                degraded=degraded_guards,
            )

    return task_def, files_to_edit, degraded_guards


def _commit_and_verify_integrity(
    store: Store,
    task_def: dict[str, Any],
    task_id: str,
    agent_id: str,
    project_root: Path | None,
    files_to_edit: list[str],
    changes_description: str,
    degraded_guards: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """S-A2 阶段 4：锁外自动提交 + 4 个完整性门禁（均 fail-closed）编排器。

    自动提交范围 = 合并后的文件清单 files_to_edit ∪ exempt_files（本函数的 commit_files），
    失败/跳过不影响状态机；扩展仅限提交范围，declared_diff / zero_residual / out_of_scope
    仍以声明的 files_to_edit 为准。随后按序执行三个独立门禁函数（声明 diff / 零残留 /
    越界）+ L1 干净校验。每个门禁跑不起来时 fail-closed 阻断并留痕。对外仅返回
    commit_result（与重构前一致）。
    """
    commit_message = changes_description or task_def.get(
        NAME_FIELD, f"orchd: done {task_id}"
    )
    # task-done-exempt-files-commit：自动提交范围扩展为 files_to_edit ∪ exempt_files，
    # 使 exempt_files 改动（如配套测试）随 done 自动提交进入任务分支 diff，不再被
    # _guard_clean_workspace 以 E017 阻断。扩展仅限提交范围；declared_diff / zero_residual
    # / out_of_scope 门禁仍以 files_to_edit 为准，与既有语义零回归。
    commit_files = list(dict.fromkeys([*files_to_edit, *task_def.get(EXEMPT_FILES_FIELD, [])]))
    commit_result = _done_auto_commit(project_root, commit_files, commit_message, task_id)

    # task-commit-timeout-failure-surface AC4：自动提交硬失败（commit_timeout /
    # commit_failed）时，后续干净守卫抛的 E017 附引擎提交诊断（reason/message），
    # hint 明示「自动提交失败」，不再仅呈现裸 dirty_workspace 语义。
    try:
        _guard_declared_diff(project_root, task_id, files_to_edit, degraded_guards)
        _guard_zero_residual(project_root, task_id, files_to_edit, degraded_guards)
        _guard_out_of_scope(project_root, task_def, task_id, degraded_guards)

        if project_root:
            _guard_clean_workspace(
                project_root,
                command="done",
                orchd_dir=store.orchd_dir,
                agent_id=agent_id,
                degraded=degraded_guards,
            )
    except OrchdError as exc:
        hard_failed = bool(
            commit_result
            and commit_result.get("reason") in ("commit_timeout", "commit_failed")
        )
        if hard_failed and exc.code is ErrorCode.E017:
            assert commit_result is not None
            base_detail = exc.details[0] if exc.details else {}
            raise OrchdError(
                ErrorCode.E017,
                exc.message,
                [{
                    **base_detail,
                    "auto_commit_reason": commit_result.get("reason"),
                    "auto_commit_message": commit_result.get("message"),
                    "auto_commit_timeout_seconds": commit_result.get("timeout_seconds"),
                    "hint": (
                        "自动提交失败（"
                        + str(commit_result.get("reason"))
                        + "），工作区改动未落盘；请检查 git 提交错误后重试 done"
                    ),
                }],
            )
        raise

    return commit_result


def _done_auto_commit(
    project_root: Path | None,
    files_to_edit: list[str],
    commit_message: str,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """锁外 best-effort 自动提交（verify 通过后、写 DONE 事件前）。

    提交范围 = 调用方传入的文件清单：done 侧由 :func:`_commit_and_verify_integrity`
    合并 files_to_edit ∪ exempt_files 后传入（本形参名沿用 files_to_edit，但不等于任务
    声明集合）。失败/跳过不影响状态机；扩展仅限提交范围，declared_diff / zero_residual /
    out_of_scope 仍以 files_to_edit 为准（越界 E020）。

    A0b / 单目录无 git（task-nogit-single-dir-pivot）：「提交」= 推进主目录
    committed 快照（:func:`orchd.nogit.take_snapshot`），不经 git commit；
    返回结构与 ``ensure_committed`` 兼容。判据为主目录基线快照存在（claim 已建立）。
    """
    if not project_root:
        return None
    if task_id:
        from orchd.nogit import git_available as _git_avail, take_snapshot

        if not _git_avail(Path(project_root)):
            info = take_snapshot(Path(project_root), task_id, "committed")
            return {
                "performed": True,
                "reason": "committed",
                "message": "nogit: committed snapshot advanced",
                "files": info["files"],
            }
    if not files_to_edit:
        return None
    return ensure_committed(project_root, files_to_edit, commit_message)
