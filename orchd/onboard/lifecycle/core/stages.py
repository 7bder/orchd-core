"""lifecycle core stages 域 — S-A2 前置预校验与自动提交+完整性门禁。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

``_done_precheck``（无锁快速失败）、``_commit_and_verify_integrity``（提交 + 四门禁
编排；``commit_files`` 表达式是 git.md 口径的对拍真源）、``_done_auto_commit``。
"""

from __future__ import annotations

import subprocess
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
    SURFACE_REFRESH_FIELD,
)

from orchd.onboard.lifecycle.guards import (
    _guard_declared_diff,
    _guard_docs_declarations,
    _guard_landing_precheck,
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

    ``degraded_guards`` 是 done 期「未阻断但须留痕」的**唯一**挂载面：本函数初始化它，
    后续阶段（entry_guard 的共享入口覆盖与改动集镜像闭环 / 完整性门禁 / 会话锁）一律追加到
    同一列表，由 ``_assemble_done_result`` 统一挂进响应——B1（task-guard-drift-done-closure）
    的 warn 档缺失镜像清单走的就是这条通道，故本函数的返回位保持不变。

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
        # 落地台账预检（task-engine-defect-fix-unified-u9）：先于 verify 与自动提交，
        # 判据与 merge 后审计同源（_changelog_landing_gaps 单一 helper）。
        _guard_landing_precheck(project_root, task_id, degraded_guards)

    return task_def, files_to_edit, degraded_guards


def _integrity_preflight(
    task_def: dict[str, Any],
    task_id: str,
    project_root: Path | None,
    files_to_edit: list[str],
    commit_files: list[str],
    degraded_guards: list[dict[str, Any]],
) -> None:
    """S-A2 preflight：提交前在「分支 diff ∪ 待提交改动」等价视图上跑三个内容门禁。

    task-engine-defect-fix-unified-u4：把副作用（自动提交）与校验解耦——内容门禁
    全部在提交之前判定，任一失败时任务分支不留下新提交（先判后提交）。判定所需
    信息不足（非 git / 不可判定）时保持各门禁既有降级/留痕语义，不扩大阻断面。
    """
    if not project_root:
        return
    # 生成产物声明门禁（task-docsgen-gate）：真源改动 ⇒ 临时重生成比对，产物变化
    # 未声明即阻断（hard）/ 留痕（warn）；先于声明 diff 与自动提交，避免落到
    # E010/E017 才发现「改了真源没带上产物」。
    _guard_docs_declarations(
        project_root,
        task_def,
        task_id,
        files_to_edit,
        degraded_guards,
    )
    from orchd.worktree.diagnosis import pending_change_paths

    pending = pending_change_paths(project_root)
    tracked_changed = set(pending.get("tracked") or ())
    untracked = set(pending.get("untracked") or ())
    from orchd.pool import _is_path_covered

    declared_untracked = {
        f for f in untracked if any(_is_path_covered(d, f) for d in files_to_edit)
    }
    # 声明完整性：未提交 / 未跟踪的声明文件按「已变更」等价补齐，避免 AC2 的
    # 「未跟踪新文件」在提交前被误判 not_committed；未跟踪文件只并入「已声明」
    # 路径（运行时/临时产物如 .orchd 快照与 --changes-file 在 git 模式下提交前
    # 本就不可见，保持既有不可见口径，不扩大阻断面）。
    _guard_declared_diff(
        project_root,
        task_id,
        files_to_edit,
        degraded_guards,
        extra_changed=tracked_changed | declared_untracked,
    )
    # 零残留：本次自动提交范围内的待提交改动即将入库，不算残留。
    _guard_zero_residual(
        project_root,
        task_id,
        files_to_edit,
        degraded_guards,
        pending_commit=commit_files,
    )
    # 越界：未跟踪文件只并入「已声明」路径（非声明 untracked 多为运行辅助产物，
    # git 模式下提交前本就不可见，保持既有不可见口径）。
    _guard_out_of_scope(
        project_root,
        task_def,
        task_id,
        degraded_guards,
        extra_changed=tracked_changed | declared_untracked,
    )


def _head_commit_sha(project_root: Path | None) -> str | None:
    """直读当前 HEAD 的提交 SHA（best-effort，非 git / 失败返回 None）。

    task-engine-defect-fix-unified-u4：不复用 gitops 的探测缓存——postflight 紧接
    自动提交之后执行，缓存键（目录 + HEAD 快照）在秒级粒度下可能仍是提交前的值。
    """
    if not project_root:
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _postflight_failure_details(
    exc: OrchdError,
    project_root: Path | None,
    commit_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """postflight 失败时把「提交是否已落盘」并入 details（响应与分支事实一致）。"""
    base = dict(exc.details[0]) if exc.details else {}
    performed = bool(commit_result and commit_result.get("performed"))
    base["commit_landed"] = performed
    base["commit_sha"] = _head_commit_sha(project_root) if performed else None
    return [base]


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
    # task-surface-refresh-channel：commit 范围再追加 generated 面受控刷新路径
    # （surface_refresh[].path，path/generator 已由注册表解析）；refresh 路径不进
    # declared_diff/zero_residual/out_of_scope 的声明域判定，只在 out_of_scope 的
    # 允许集合里展开（见 guards._guard_out_of_scope）。
    commit_files = list(dict.fromkeys([
        *files_to_edit,
        *task_def.get(EXEMPT_FILES_FIELD, []),
        *[
            str(item.get("path"))
            for item in (task_def.get(SURFACE_REFRESH_FIELD) or [])
            if isinstance(item, dict) and item.get("path")
        ],
    ]))
    # task-engine-defect-fix-unified-u4：先判后提交。内容门禁（声明完整性 / 零残留 /
    # 越界）在提交前于「分支 diff ∪ 待提交改动」等价视图上跑一遍，失败时任务分支不
    # 新增提交；提交后再跑权威 postflight（含 L1 干净校验）。
    _integrity_preflight(
        task_def,
        task_id,
        project_root,
        files_to_edit,
        commit_files,
        degraded_guards,
    )
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
            base_detail = dict(exc.details[0]) if exc.details else {}
            base_detail.update({
                "auto_commit_reason": commit_result.get("reason"),
                "auto_commit_message": commit_result.get("message"),
                "auto_commit_timeout_seconds": commit_result.get("timeout_seconds"),
                "commit_landed": bool(commit_result.get("performed")),
                "commit_sha": None,
                "hint": (
                    "自动提交失败（"
                    + str(commit_result.get("reason"))
                    + "），工作区改动未落盘；请检查 git 提交错误后重试 done"
                ),
            })
            raise OrchdError(ErrorCode.E017, exc.message, [base_detail])
        # task-engine-defect-fix-unified-u4 AC3：postflight 失败时显式披露提交事实，
        # 响应与账本（DONE 事件未写）一致。
        raise OrchdError(
            exc.code,
            exc.message,
            _postflight_failure_details(exc, project_root, commit_result),
        )

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
