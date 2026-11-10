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

from orchd.contracts.events import (
    EVENT_AMEND,
)
from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops import (
    GUARD_FAIL_CLOSED,
    branch_exists,
    check_workspace_state,
    get_default_branch as _get_default_branch,
    is_task_worktree,
    list_tracked_changes,
    main_worktree_root,
    run_guard,
)
from orchd.contracts.responses import (
    COMMAND_KEY,
    HINT_KEY,
    TASK_ID_KEY,
)
from orchd.contracts.task_schema import (
    EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD,
    SURFACE_REFRESH_FIELD,
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
        context={TASK_ID_KEY: task_id, COMMAND_KEY: "done"},
        hint="检测未生效，已 fail-closed 阻断 done；确认 git 环境后重试 done",
        degraded=degraded_guards,
    ) or []
    if overlap:
        raise OrchdError(
            ErrorCode.E017,
            "dirty_workspace: 主工作树存在与当前任务 files_to_edit 重叠的未提交改动",
            [{
                TASK_ID_KEY: task_id,
                "overlap_files": overlap,
                FILES_TO_EDIT_FIELD: files_to_edit,
                HINT_KEY: (
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
    extra_changed: set[str] | list[str] | None = None,
) -> None:
    """声明文件必须进入任务分支 diff（红线 #13 硬门禁）。

    门禁自身出故障 → fail-closed 抛 E030 留痕；声明文件缺失 → 抛 E010。
    """
    if not (project_root and files_to_edit):
        return

    def _declared_diff_guard() -> list[dict[str, str]]:
        from orchd.nogit import git_available as _git_avail

        if not _git_avail(Path(project_root)):
            # 单目录无 git（task-nogit-single-dir-pivot）：done 期不做声明完整性
            # 校验，与 flat / 非任务 worktree 同口径（NotApplicable 降级留痕）。
            # 理由：单目录没有隔离工作区，done 期无法区分“尚未实现”与辅助流程
            # （如 --changes-file / --comments-file 这类声明文件未落盘的合法调用，
            # AC4 要求按原写法通过）；声明完整性仅由 review 期诊断兜底。done 期
            # 的无 git 有效门禁是 out-of-scope 与 residual（主目录快照口径，AC3）。
            raise NotApplicableError(
                "单目录无 git：声明文件分支 diff 门禁不适用（本次未生效，声明完整性"
                "仅由 review 期诊断兜底，与 flat 口径一致）"
            )
        # task-flat-guard-parity：独立任务 worktree 走既有口径；flat / 降级下
        # 当前即任务分支时走等价口径（与独立任务 worktree 同语义，见
        # worktree.is_current_task_branch）；其余（main 上等）保持跳过留痕。
        # 判定顺序保留 is_task_worktree 在先：既有测试打该缝线，且容器语义不变
        # （is_task_worktree 为本模块顶层导入，与 diagnose 内同源）。
        from orchd.worktree import (
            diagnose_missing_branch_files,
            is_current_task_branch,
        )

        if not is_task_worktree(Path(project_root)):
            if not is_current_task_branch(Path(project_root), task_id):
                raise NotApplicableError(
                    "非任务分支工作区（main 上等）：声明文件分支 diff 门禁不适用"
                    "（本次未生效，声明完整性仅由 review 期诊断兜底）"
                )
        # task-engine-defect-fix-unified-u4：extra_changed 是「待提交改动」等价
        # 视图（preflight 用），空则不传，保持既有调用缝线零回归。
        if extra_changed:
            return diagnose_missing_branch_files(
                project_root, task_id, files_to_edit, extra_changed=extra_changed
            )
        return diagnose_missing_branch_files(project_root, task_id, files_to_edit)

    diagnosed = run_guard(
        _declared_diff_guard,
        guard_name="diagnose_missing_branch_files",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={TASK_ID_KEY: task_id, COMMAND_KEY: "done"},
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
        # task-engine-defect-fix-unified-u2：删除态与幽灵路径的分流不再依赖
        # diagnosis 的 git status 探针标记，改由声明层 path_state 单一分类判定
        # （与 commit 层可提交性判定同源，杜绝两套磁盘存在性判定）：
        # - tracked-deleted（已被删除且仍跟踪）→ 引导「重跑 done 由引擎自动提交
        #   删除态并保持声明」——与 _guard_out_of_scope 的 D 感知口径对齐；不再
        #   引导「从声明中移除」（那会令删除变声明外改动，反向触发 out_of_scope
        #   E010，双向夹击），也不再指引被代理拒的 `git add -A`。
        # - ghost（磁盘不存在且未跟踪）→ 引导修正路径或 claimed 态撤回 ghost。
        from orchd.spec.declarations import (
            PATH_STATE_GHOST,
            PATH_STATE_TRACKED_DELETED,
            git_tracked_files,
            path_state,
        )

        tracked = git_tracked_files(project_root)
        not_found_files = sorted(
            d["file"] for d in diagnosed if d.get("reason") == "path_not_found"
        )
        deleted_files = [
            fp for fp in not_found_files
            if path_state(fp, project_root, tracked_files=tracked)
            == PATH_STATE_TRACKED_DELETED
        ]
        ghost_files = [
            fp for fp in not_found_files
            if path_state(fp, project_root, tracked_files=tracked) == PATH_STATE_GHOST
        ]
        if deleted_files:
            hints.append(
                "path_not_found(删除态): 声明文件已从磁盘删除但删除态未进入任务"
                "分支——重跑 done 由引擎自动提交删除态并保持声明（可提交性判定"
                "已把 tracked-deleted 纳入暂存）；勿从声明中移除该文件（否则删除"
                "将变声明外改动，触发 out_of_scope"
                f" E010）：{'; '.join(deleted_files)}"
            )
        if ghost_files:
            hints.append(
                "path_not_found(ghost): 文件在磁盘不存在且未被 git 跟踪——"
                "请修正 files_to_edit 路径，或在主工作树执行 amend "
                "--remove-files-to-edit 撤回该声明（claimed 态允许移除 ghost）："
                f"{'; '.join(ghost_files)}"
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
            "not_committed: 文件已修改但未进入任务分支 diff——请在任务 worktree "
            "内保存改动后重跑 done（引擎自动提交声明范围，无需手工 git add）"
        )
    raise OrchdError(
        ErrorCode.E010,
        "file_conflict: 声明文件未进入任务分支 diff",
        [{
            TASK_ID_KEY: task_id,
            "missing_declared_files": missing_names,
            "reasons": reason_summary,
            FILES_TO_EDIT_FIELD: files_to_edit,
            HINT_KEY: " | ".join(hints),
        }],
    )


def _guard_zero_residual(
    project_root: Path | None,
    task_id: str,
    files_to_edit: list[str],
    degraded_guards: list[dict[str, Any]],
    pending_commit: set[str] | list[str] | None = None,
) -> None:
    """提交零残留门禁（task-engine-done-integrity-gate）。

    git 探测故障 ≠ git 不可用，一律按三态区分：故障 → fail-closed E030；
    不适用（非 git）→ 降级留痕；残留存在 → 抛 E017。
    """
    if not (project_root and files_to_edit):
        return

    def _residual_guard() -> list[str]:
        from orchd.nogit import git_available as _git_avail, maindir_residual_paths

        if not _git_avail(Path(project_root)):
            # 单目录无 git：committed 快照之后又发生的改动必须被检出。
            from orchd.pool import _is_path_covered

            residual = maindir_residual_paths(project_root, task_id)
            return [f for f in residual if any(_is_path_covered(d, f) for d in files_to_edit)]
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
        from orchd.pool import _is_path_covered
        return [f for f in tracked if any(_is_path_covered(d, f) for d in files_to_edit)]

    residual = run_guard(
        _residual_guard,
        guard_name="commit_zero_residual",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={TASK_ID_KEY: task_id, COMMAND_KEY: "done"},
        hint="零残留校验未生效，已 fail-closed 阻断 done；确认 git 环境后重试",
        degraded=degraded_guards,
    ) or []
    if pending_commit:
        # task-engine-defect-fix-unified-u4：preflight 判定时，本次自动提交范围内的
        # 待提交改动即将入库，不算残留（提交后 postflight 用空参获得权威结论）。
        from orchd.pool import _is_path_covered

        pending = {str(p).replace("\\", "/") for p in pending_commit if p}
        residual = [
            f for f in residual
            if not any(_is_path_covered(p, f) for p in pending)
        ]
    if not residual:
        return
    raise OrchdError(
        ErrorCode.E017,
        "dirty_workspace: files_to_edit 范围内仍有未提交跟踪改动",
        [{
            TASK_ID_KEY: task_id,
            "residual_files": residual,
            HINT_KEY: (
                "引擎自动提交未覆盖这些文件——请重跑 done（引擎会按 "
                "files_to_edit ∪ exempt_files 重新自动提交，无需手工 git add）；"
                "若文件确属本任务但未声明，先在主工作树 amend 补声明；"
                "若属误改，用 orchd restore --path 还原"
            ),
        }],
    )


def _guard_out_of_scope(
    project_root: Path | None,
    task_def: dict[str, Any],
    task_id: str,
    degraded_guards: list[dict[str, Any]],
    extra_changed: set[str] | list[str] | None = None,
) -> None:
    """越界改动检测（红线 #3 引擎兜底，task-concurrency-hardening）。

    对任务分支相对 main 的「实际改动文件」与 files_to_edit ∪ exempt_files 显式
    比照；detected 越界 → 抛 E010。仅"环境不适用"允许降级且必须留痕，校验故障
    （git 超时 / 解析失败）fail-closed 阻断。

    task-decl-concession-autoregister：引入**越界分诊**——连带类（同名测试
    ``tests/test_<stem>.py``、``docs/*.md``，经 spec.is_concession_file 单一
    来源白名单）自动登记（写 AMEND 审计事件，复用既有事件类型与 amend 语义，
    不新增事件类型、不改 _apply_event 语义）、不阻断 done；高风险类（引擎核心
    ``orchd/`` 既有文件、约定文件、他人声明或在途文件）仍 E010 拒绝。
    """
    if not project_root:
        return
    allowed: set[str] = set(task_def.get(FILES_TO_EDIT_FIELD, []))
    allowed |= set(task_def.get(EXEMPT_FILES_FIELD, []))
    # task-surface-refresh-channel：generated 面受控刷新路径不属于写集声明，
    # 但 done 会代任务提交它，故越界守卫须允许（申报矩阵已在 amend 期校验）。
    allowed |= {
        str(item.get("path"))
        for item in (task_def.get(SURFACE_REFRESH_FIELD) or [])
        if isinstance(item, dict) and item.get("path")
    }
    # 固定资产豁免（对齐 L3 hook 的 amend 自动提交豁免）
    allowed |= {".orchd/_master.json", ".orchd/IDEAS.md"}
    # task-feedback-session-runtime-gate：引擎自己的 session runtime 不是任务
    # 内容。flat / no-git 布局下它落在 <store>/sessions/，若项目的 .gitignore
    # 未覆盖 .orchd/*（测试临时工程常见），会被越界守卫误判；按 store 相对
    # 路径加入豁免。container 布局 runtime 在工作树外，relative_to 失败即跳过。
    try:
        from orchd.ledger import session_runtime_dir
        _rt = session_runtime_dir(Path(project_root) / ".orchd")
        _rel = _rt.resolve().relative_to(Path(project_root).resolve()).as_posix()
        allowed.add(_rel.rstrip("/") + "/")
    except (ValueError, OSError, ImportError):
        # 工作树外 / store 不可解析：不阻断越界守卫，保持原判定。
        pass

    def _out_of_scope_guard() -> list[str]:
        state = check_workspace_state(project_root)
        state_name = state.get("state")
        if state_name == "error":
            raise RuntimeError(
                f"git 探测故障（{state.get('reason')}）：越界改动检测无法执行"
            )
        # 变更检测**单一真源**（task-nogit-changedet-core / A0a + task-repo-backend-port / b1）：
        # 经 RepositoryBackend 端口分发（git→GitBackend≡_git_diff_names，
        # 无 git→SnapshotBackend≡快照差分），D/R 口径一致（kernel-contract INV-1）；
        # 无 git 模式不再整体降级为 NotApplicable（红线 #3 不因环境退化，INV-2）。
        # 基线缺失判定与单目录可见性策略保留在调用方（端口只算差分，不管策略）：
        # 前者是降级留痕依据，后者是 git“未提交不可见”的等价口径。
        if state_name == "unavailable":
            from orchd.gitops.repo import SnapshotBackend
            from orchd.nogit import read_manifest, snapshot_dir

            base = read_manifest(snapshot_dir(Path(project_root), task_id, "base"))
            if base is None:
                # 无基线快照 → 无法判定（A0b 起由 claim 建立基线，届时恒非空）；
                # 显式降级留痕，不做静默放行以外的任何假设。
                raise NotApplicableError(
                    f"git {state.get('reason')} 且无基线快照：越界改动检测不适用"
                    "（基线由 claim 建立，见 A0b）"
                )
            # 单目录无 git（task-nogit-single-dir-pivot）：只比照基线快照中已存在
            # 的路径。基线中不存在的新增文件（如 --changes-file 这类运行辅助文件）
            # 在 git 模式下同属不可见（未提交即不在分支 diff 内），此处同样跳过；
            # 已知边界：改名落入声明外新路径的极端情形可能漏检（窄口径，需刻意跨
            # 范围改名；单目录下 done 期声明门禁与 flat 同口径降级）。
            changed = SnapshotBackend(project_root).changed_paths(task_id)
            actual_modified = [p for p in changed if p in base]
        else:
            from orchd.line_ctx import resolve_task_branch_for

            default = _get_default_branch(project_root)
            if not default:
                raise NotApplicableError(
                    "无默认分支（main/master）引用：越界改动检测不适用"
                )
            branch = resolve_task_branch_for(project_root, task_id)
            exists = branch_exists(project_root, branch)
            if exists is None:
                raise RuntimeError(
                    f"git 探测故障：无法确认任务分支 {branch} 是否存在"
                )
            if not exists:
                raise NotApplicableError(
                    f"任务分支 {branch} 不存在：越界改动检测不适用"
                )
            from orchd.gitops.repo import for_project

            # task-rename-declaration-deadlock-fix：重命名展开口径（删+增），
            # 与 delete-parity/E020 hook 同口径，不死锁
            actual_modified = for_project(project_root).changed_paths(
                task_id, no_renames=True)
        from orchd.pool import _is_path_covered
        allowed_list = list(allowed)
        # task-engine-defect-fix-unified-u4：preflight 的「待提交改动」并入实际改动
        # 视图（提交前分支 diff 不含它们），空则保持既有口径零回归。
        if extra_changed:
            extra = {str(p).replace("\\", "/") for p in extra_changed if p}
            actual_modified = sorted(set(actual_modified) | extra)
        return [f for f in actual_modified if not any(_is_path_covered(a, f) for a in allowed_list)]

    out_of_scope = run_guard(
        _out_of_scope_guard,
        guard_name="out_of_scope_changes",
        on_error=GUARD_FAIL_CLOSED,
        fallback=[],
        context={TASK_ID_KEY: task_id, COMMAND_KEY: "done"},
        hint="越界改动检测未生效，已 fail-closed 阻断 done；确认 git 环境后重试",
        degraded=degraded_guards,
    ) or []
    if not out_of_scope:
        return

    # task-decl-concession-autoregister：越界分诊——连带类自动登记放行，
    # 高风险类仍 E010 拒绝（护栏强度不下降）。
    from orchd.spec import is_concession_file

    files_to_edit = [f for f in (task_def.get(FILES_TO_EDIT_FIELD) or []) if isinstance(f, str)]
    concession: list[str] = []
    high_risk: list[str] = []
    for f in out_of_scope:
        if is_concession_file(f, files_to_edit):
            concession.append(f)
        else:
            high_risk.append(f)

    if concession:
        _auto_register_concession(project_root, task_id, concession)
    if not high_risk:
        return

    # done 前置优先级（task-v163-claim-budget-and-errors）：越界文件中若仍有
    # **未提交的已跟踪改动**，同一现场同时命中「脏工作区」(E017) 与「越界」(E010)。
    # 先报 E017——否则 agent 会被引导去 amend 补声明，而真正的阻塞是未提交/未还原
    # 的脏改动（amend 也消不掉它）。已提交的越界改动仍走下方 E010 主路径。
    dirty_high: list[str] = []
    try:
        from orchd.pool import _is_path_covered

        dirty = list_tracked_changes(project_root)
        if dirty is not None:
            dirty_high = sorted(
                f for f in high_risk
                if any(_is_path_covered(d, f) for d in dirty))
    except Exception:  # noqa: BLE001 - 探测失败不改变既有 E010 主路径
        dirty_high = []
    if dirty_high:
        raise OrchdError(
            ErrorCode.E017,
            "dirty_workspace: done 检测到越界文件仍是未提交的已跟踪改动",
            [{
                TASK_ID_KEY: task_id,
                "out_of_scope_files": dirty_high,
                "declared_files": sorted(allowed),
                HINT_KEY: (
                    "先提交或还原这些已跟踪改动（orchd restore --path <文件> / "
                    "git 提交）后重跑 done；越界声明问题在脏工作区清除后再按 E010 "
                    "引导处理"
                ),
            }],
        )

    # task-amend-guidance-mainwt：定位主工作树，生成带绝对路径的可执行 amend 命令
    try:
        main_wt = str(main_worktree_root(project_root))
        from orchd.guide import amend_mainwt_command
        patch_cmd = amend_mainwt_command(
            task_id, main_wt, files=high_risk[:3])
    except Exception:
        main_wt = "<主工作树路径>"
        patch_cmd = f'cd "{main_wt}"; python .orchd/__main__.py amend --task {task_id} --files-to-edit <file>'
    raise OrchdError(
        ErrorCode.E010,
        "file_conflict: 实现改动超出任务 files_to_edit∪exempt_files 声明范围",
        [{
            TASK_ID_KEY: task_id,
            "out_of_scope_files": sorted(high_risk),
            "auto_registered_files": sorted(concession),
            "declared_files": sorted(allowed),
            "main_worktree": main_wt,
            HINT_KEY: (
                "实现只允许改动 files_to_edit/exempt_files 声明内的文件。"
                "若确有必要连带修改，请回到主工作树补 files_to_edit 声明并执行 amend"
                "（任务 worktree 不保留 _master.json，唯一权威 = 主工作树；"
                "claimed 状态任务允许 files_to_edit 只增不删，需记录原因，不重置 attempt_count）。"
                f"可执行命令：{patch_cmd}"
            ),
        }],
    )


def _auto_register_concession(
    project_root: Path | None,
    task_id: str,
    files: list[str],
) -> None:
    """连带类自动登记：写 AMEND 审计事件（task-decl-concession-autoregister）。

    复用既有 AMEND 事件类型与 amend 语义（**不新增事件类型、不改
    ``ledger._apply_event`` 语义**）：AMEND 为纯审计事件（``_event_target_status``
    返回 None → 跳过状态机校验，``_apply_event`` 无 AMEND 分支 → 不影响任务状态），
    审计明细随事件落账，用既有 ledger/status 入口即可回查。

    **幂等（code review R1 修复）**：``_guard_out_of_scope`` 在真实 CLI 路径会被
    调用两次（``cli/commands/workflow.py`` 的 done 早检 + ``lifecycle/core.py``
    的 done 完整性门禁），若各自登记将产生重复 AMEND 事件（且随 done 重试累积）。
    故写事件前先扫描 ledger 已存在的 ``reason=auto_concession_registration`` 事件，
    按其 ``files`` 集合去重——同批连带文件只会落账一条审计。

    best-effort：store 不可用或写事件失败不阻断 done（自动登记为增益而非护栏，
    护栏 = 高风险类仍 E010，与登记动作解耦）。身份经会话环境解析（与 claim/done
    同一会话级指纹），无需调用方透传——保持 ``_guard_out_of_scope`` 签名稳定。
    """
    if not project_root or not files or not task_id:
        return
    try:
        from orchd.ledger import Store, resolve_agent_id

        orchd_dir = project_root / ".orchd"
        store = Store(orchd_dir)
        agent_id = resolve_agent_id(orchd_dir)
        from orchd.gitops_ops import make_event

        # 幂等：已登记的同 reason 连带文件不再重复写事件（防 CLI 早检 + done 双调用双写）
        already_registered: set[str] = set()
        try:
            for ev in store._read_ledger_lines(from_line=1):
                if (
                    ev.get("task_id") == task_id
                    and ev.get("type") == EVENT_AMEND
                    and ev.get("reason") == "auto_concession_registration"
                ):
                    already_registered.update(ev.get("files") or [])
        except Exception:
            already_registered = set()
        pending = [f for f in files if f not in already_registered]
        if not pending:
            return

        store.acquire_lock()
        try:
            for f in pending:
                ev = make_event(
                    task_id, agent_id, EVENT_AMEND,
                    reason="auto_concession_registration",
                    files=[f],
                    hint=(
                        "done 越界分诊：连带类自动登记（同名测试 / docs/*.md 白名单），"
                        "原 files_to_edit 无需人工 amend 补声明"
                    ),
                )
                store.append_event(ev)
            new_state = store.replay()
            store.update_checkpoint(new_state)
        finally:
            store.release_lock()
    except Exception:
        # best-effort：登记失败不影响 done（护栏已由高风险 E010 独立保证）
        return


def _surface_landing_hint(
    project_root: Path,
    task_id: str,
) -> tuple[str, str]:
    """按 surfaces 注册表生成 E039 落地提示（task-shared-surface-guard-hint）。

    默认 changelog 面为 fragments 模式：提示引擎写片段，禁止再教用户手改
    CHANGELOG.md；宿主自定义 serial/generated 模式时回退为对应路径提示。
    """
    from orchd import surfaces

    fallback_path = "CHANGELOG.md"
    try:
        specs = surfaces.load_surfaces(project_root)
    except (OrchdError, OSError):
        # 注册表非法/不可读：无非是配置问题，回退旧提示即可（不吞真实编程错误）。
        specs = {}
    spec = specs.get("changelog")
    if spec is None:
        hint = (
            f"在本任务分支的 {fallback_path} `[Unreleased]` 分区补一行含 `{task_id}` "
            "的语义化条目后重跑 done（本预检先于 验证与自动提交，未产生任何提交）。"
        )
        return hint, fallback_path

    path = spec.path or fallback_path
    if spec.mode == "generated":
        command = surfaces.generator_command(spec.generator) or "注册表声明的生成器"
        hint = (
            f"落地面 `{path}` 为 generated 模式（由 `{command}` 生成，禁止手改）："
            f"先运行生成器并提交生成结果（使 `{task_id}` 在册）后重跑 done"
            "（本预检先于 验证与自动提交，未产生任何提交）。"
        )
        return hint, path
    if spec.mode != "fragments":
        # amend 必须回主工作树（worktree 内无 _master.json），走单一源命令生成器；
        # 生成失败只降级文案，不影响预检判据。
        try:
            main_wt = str(main_worktree_root(project_root))
            from orchd.guide import amend_mainwt_command

            amend_cmd = amend_mainwt_command(task_id, main_wt, files=[path])
        except Exception:  # noqa: BLE001 - 指引降级：给出占位路径即可
            amend_cmd = (
                f'cd "<主工作树路径>"; python .orchd/__main__.py amend --task {task_id} '
                f"--files-to-edit {path}"
            )
        hint = (
            f"落地面 `{path}` 为 {spec.mode} 模式：在本任务分支更新 `{path}` "
            f"使 `{task_id}` 在册后重跑 done；该共享面未在 files_to_edit 声明时，先在主工作树执行 "
            f"`{amend_cmd}` 再补内容（本预检先于 验证与自动提交，未产生任何提交）。"
        )
        return hint, path

    fragment_dir = spec.dir or "changelog.d"
    filename = spec.filename or "{task_id}.{category}.md"
    rendered_name = filename.replace("{task_id}", task_id).replace(
        "{category}", "<category>")
    fragment_path = f"{fragment_dir}/{rendered_name}"
    entry_template = spec.entry or "- {task_id}：{summary}"
    entry_line = entry_template.replace("{task_id}", task_id).replace("{summary}", "<摘要>")
    categories = "、".join(spec.categories or ())
    payload = (
        '{"surface":"changelog","category":"<category>","summary":"<摘要>"}'
    )
    entry_example = (
        f"python .orchd/__main__.py done --task {task_id} --surface-entry '{payload}'"
    )
    hint = (
        f"落地面 `{path}` 为 fragments 模式（片段目录 `{fragment_dir}`，每次 done 写一条片段）："
        f"在本任务分支写片段 `{fragment_path}`，内容为单行 `{entry_line}`"
        f"（分类：{categories}）；或直接 `python .orchd/__main__.py done --task {task_id} "
        '--changelog-category <category> --changelog-summary "<摘要>"`（等价 '
        f"`{entry_example}`）让引擎写片段。不要手工编辑 {path}，汇总由 "
        "`python .orchd/__main__.py surfaces assemble` 完成；本预检先于 验证与自动提交，"
        "未产生任何提交。"
    )
    return hint, path


def _guard_landing_precheck(
    project_root: Path | None,
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> None:
    """落地台账预检（task-engine-defect-fix-unified-u9，done 前置，E039 通道）。

    判据与 merge 后审计**同源**：一律经 ``orchd.report.audit._changelog_landing_gaps``
    这唯一 helper（禁止本处复制 CHANGELOG / git 遍历判据）。区别只在时点与投影：
    本预检在 验证与自动提交**之前**跑，把「合入后 CHANGELOG 缺行」提前到 done
    前暴露（u4 事务语义：预检失败不产生任何提交）。

    只判「本任务」缺口（``incoming_missing``）；历史落地缺口不在此阻断——存量欠账
    若也拦，会让全部任务被历史锁死，历史面由 merge 审计注解 + 收口卡清算。

    走 E039 通道 + ``details.rule="landing_precheck"`` 分流（drift_closure /
    subpackage_entry 的既有先例：不新增错误码）。
    """
    if project_root is None:
        return
    from orchd.report.audit import _changelog_landing_gaps

    gaps = _changelog_landing_gaps(project_root, incoming_task_id=task_id)
    pending = [g for g in gaps if g.get("incoming_missing")]
    if not pending:
        return
    gap = pending[0]
    hint, surface_path = _surface_landing_hint(project_root, task_id)
    raise OrchdError(
        ErrorCode.E039,
        f"landing_precheck: 落地任务 '{task_id}' 将不在 {surface_path} 在册",
        [{
            TASK_ID_KEY: task_id,
            "rule": "landing_precheck",
            "path": gap.get("path"),
            "task_ids": gap.get("task_ids", []),
            HINT_KEY: hint,
        }],
    )


def _git_baseline_measurable(project_root: Path, task_id: str) -> bool:
    """git 后端改动集前提：两 ref 可解析且 diff 基线可计算（三态探测，fail-closed）。

    ``orchd.worktree._git_diff_names`` 对「任务分支缺失 / diff 失败」一律返回空列表，
    单看返回值无法区分「确实零改动」与「环境不可测」。这里分两层预检：
    ``branch_exists`` 三态确认 trunk 与任务分支存在，再用 ``git merge-base`` 确认两
    ref 有共同祖先——orphan 历史 / 浅克隆截断时 ``git diff A...B`` 会以
    ``fatal: no merge base`` 失败（rc=128）而被吞成空列表。任一前提不成立（含探测
    异常/超时）都按不可测处理，交由调用方回退声明并留痕。
    """
    from orchd.gitops import _run_git, branch_exists
    from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

    try:
        trunk = resolve_trunk_for(project_root)
        task_branch = resolve_task_branch_for(project_root, task_id)
    except Exception:  # noqa: BLE001 - 基线名不可解析 ⇒ 不可测
        return False
    if branch_exists(project_root, trunk) is not True:
        return False
    if branch_exists(project_root, task_branch) is not True:
        return False
    try:
        # 两 ref 都存在也可能没有共同祖先（no merge base）；此时三点 diff 不可算。
        merge_base = _run_git(project_root, ["merge-base", trunk, task_branch])
    except Exception:  # noqa: BLE001 - 探测故障/超时 ⇒ 不可测
        return False
    return merge_base.returncode == 0


def _docs_changed_paths(project_root: Path, task_id: str) -> tuple[list[str], bool]:
    """本任务改动集（分支 diff ∪ 工作树未提交/暂存改动），返回 ``(paths, measurable)``。

    ``measurable=False`` 表示后端无法给出可信改动集（无 git 且快照基线缺失 / git 基线
    分支缺失或 diff 失败 / 后端异常），调用方回退声明的 ``files_to_edit`` 并留痕——
    不得静默按空集放行。
    """
    from orchd.gitops.repo import for_project

    backend = for_project(project_root)
    changed: list[str] = []
    try:
        changed += list(backend.changed_paths(task_id))
    except Exception:  # noqa: BLE001 - 后端不可用 ⇒ 调用方回退声明
        return [], False
    if backend.backend_name == "git" and not _git_baseline_measurable(project_root, task_id):
        # diff 失败会被 _git_diff_names 吞成空列表；基线不可解析必须显式降级，
        # 否则「无任务分支」场景会以 changed=[]/measurable=True 静默放行。
        return [], False
    measurable = not (backend.backend_name == "snapshot" and not changed)
    if backend.backend_name != "snapshot":
        try:
            from orchd.worktree.diagnosis import pending_change_paths

            pending = pending_change_paths(project_root)
            changed += list(pending.get("tracked") or ())
            changed += list(pending.get("untracked") or ())
        except Exception:  # noqa: BLE001 - 工作树不可读 ⇒ 已取到的分支 diff 仍有效
            pass
    normalized = [str(path).replace("\\", "/").strip().removeprefix("./") for path in changed]
    return list(dict.fromkeys(path for path in normalized if path)), measurable


def _guard_docs_declarations(
    project_root: Path | None,
    task_def: dict[str, Any],
    task_id: str,
    files_to_edit: list[str],
    degraded_guards: list[dict[str, Any]],
    changed: list[str] | None = None,
) -> None:
    """生成产物声明门禁（task-docsgen-gate，done 前置，E039 通道）。

    判据（只对 ``sync=done`` 的产物；``merge`` 产物由合并入口单写，done 不抢写）：

    * 真源命中改动集 ⇒ 临时重生成并与磁盘比对（``block`` 只比生成区段）；
    * 内容变化且产物未在 ``files_to_edit`` 声明 ⇒ ``gate=hard`` 阻断（E039
      ``rule="docs_declaration"``），``gate=warn`` 只记 ``degraded_guards``；
    * 内容未变但产物已声明 ⇒ 记 warning「建议移除声明」（防幽灵声明）；
    * 生成器/产物读取异常 ⇒ 降级留痕（E030），不阻断 done（fail-open 同族纪律）。

    只消费 ``changed``（分支 diff ∪ 工作树改动），不写盘、不起子进程。
    """
    if project_root is None:
        return
    from orchd.repo_role import is_engine_repo

    # 引擎仓门禁（同 drift / generated 家族口径）：宿主/测试临时仓没有生成器真源
    # 目录结构，硬套会把普通任务误判为「产物未声明」；宿主文档走只读快照 check。
    if not is_engine_repo(Path(project_root)):
        return
    try:
        from orchd import docs_manifest
        from orchd.docsgen import blocks

        entries, problems = docs_manifest.resolve_manifest()
    except Exception as exc:  # noqa: BLE001 - 清单不可读 ⇒ 降级留痕不阻断
        from orchd.gitops import GUARD_STATUS_FAILED, record_degraded_guard

        record_degraded_guard(
            degraded_guards,
            guard_name="docs_declaration",
            status=GUARD_STATUS_FAILED,
            reason="文档产物清单不可读，本次未做声明判定",
            error=f"{type(exc).__name__}: {exc}",
            context={TASK_ID_KEY: task_id},
            hint="确认 orchd.docs_manifest 可导入后重试 done",
        )
        return
    from orchd.gitops import (
        GUARD_STATUS_FAILED,
        GUARD_STATUS_NOT_APPLICABLE,
        record_degraded_guard,
    )
    from orchd.pool import _is_path_covered

    if problems:
        record_degraded_guard(
            degraded_guards,
            guard_name="docs_declaration",
            status=GUARD_STATUS_FAILED,
            reason="文档产物清单配置非法，本次未做声明判定",
            context={TASK_ID_KEY: task_id, "problems": problems},
            hint="修正 orchd/docs_manifest.py 后重试 done",
        )
        return
    if changed is None:
        changed, measurable = _docs_changed_paths(Path(project_root), task_id)
        if not measurable:
            # 不可测（无 git 且快照基线缺失 / 后端异常）：回退声明并留痕，禁止静默放行。
            changed = list(files_to_edit or ())
            record_degraded_guard(
                degraded_guards,
                guard_name="docs_declaration",
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason="改动集不可测（无 git/快照基线），回退声明的 files_to_edit 判定",
                context={TASK_ID_KEY: task_id, "fallback": "files_to_edit"},
                hint="确认任务基线（claim 快照或 git 分支）可解析后重试 done",
            )
    declared = {
        str(path).replace("\\", "/").strip().removeprefix("./")
        for path in (files_to_edit or ())
    }
    root = Path(project_root)
    for entry in entries:
        if docs_manifest.sync_point(entry) != "done":
            continue
        if not docs_manifest.matches_sources(entry, changed):
            continue
        is_declared = any(_is_path_covered(path, entry.artifact) for path in declared)
        try:
            expected = docs_manifest.generator_for(entry.generator)
            expected_text = expected() if callable(expected) else None
            if expected_text is None:
                raise RuntimeError(f"生成器不可用：{entry.generator}")
            path = root / entry.artifact
            disk = path.read_text(encoding="utf-8") if path.is_file() else None
            if entry.kind == "block" and disk is not None:
                try:
                    actual = blocks.extract_block(disk, entry.id)
                except blocks.BlockError:
                    # 标记非法 = 产物不可用 ⇒ 按「与生成结果不同」处理（走声明/档位判定）。
                    actual = None
            else:
                actual = disk if entry.kind == "file" else None
        except Exception as exc:  # noqa: BLE001 - 单产物求值/读取异常降级留痕
            record_degraded_guard(
                degraded_guards,
                guard_name="docs_declaration",
                status=GUARD_STATUS_FAILED,
                reason=f"产物 {entry.artifact} 重生成比对失败，本次未判定",
                error=f"{type(exc).__name__}: {exc}",
                context={TASK_ID_KEY: task_id, "artifact_id": entry.id},
                hint="确认生成器可导入且产物可读后重试 done",
            )
            continue
        if actual != expected_text:
            if is_declared:
                continue
            detail = {
                TASK_ID_KEY: task_id,
                "rule": "docs_declaration",
                "artifact_id": entry.id,
                "artifact": entry.artifact,
                "gate": entry.gate,
                HINT_KEY: (
                    f"真源已变更且 {entry.artifact} 重生成结果不同；先运行 "
                    f"`python .orchd/__main__.py docs write --changed <真源路径>` 写回，"
                    "再在主工作树用 amend 把该产物补进 files_to_edit 后重试 done"
                ),
            }
            if entry.gate == "hard":
                raise OrchdError(
                    ErrorCode.E039,
                    "docs_declaration_gap: 真源已变更但生成产物未声明",
                    [detail],
                )
            record_degraded_guard(
                degraded_guards,
                guard_name="docs_declaration",
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason=f"G1 产物 {entry.artifact} 变化未声明（warn 档：仅提示）",
                context=detail,
                hint=detail[HINT_KEY],
            )
            continue
        if is_declared:
            record_degraded_guard(
                degraded_guards,
                guard_name="docs_declaration",
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason=f"声明了 {entry.artifact} 但重生成结果与磁盘一致，建议移除该声明",
                context={
                    TASK_ID_KEY: task_id,
                    "artifact_id": entry.id,
                    "artifact": entry.artifact,
                },
                hint="该产物无内容变化；移除 files_to_edit 中的幽灵声明可减少无谓 diff",
            )
