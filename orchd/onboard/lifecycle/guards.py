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
    main_worktree_root,
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
        from orchd.pool import _is_path_covered
        return [f for f in tracked if any(_is_path_covered(d, f) for d in files_to_edit)]

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

    task-decl-concession-autoregister：引入**越界分诊**——连带类（同名测试
    ``tests/test_<stem>.py``、``docs/*.md``，经 spec.is_concession_file 单一
    来源白名单）自动登记（写 AMEND 审计事件，复用既有事件类型与 amend 语义，
    不新增事件类型、不改 _apply_event 语义）、不阻断 done；高风险类（引擎核心
    ``orchd/`` 既有文件、约定文件、他人声明或在途文件）仍 E010 拒绝。
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
        from orchd.pool import _is_path_covered
        allowed_list = list(allowed)
        return [f for f in actual_modified if not any(_is_path_covered(a, f) for a in allowed_list)]

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

    # task-decl-concession-autoregister：越界分诊——连带类自动登记放行，
    # 高风险类仍 E010 拒绝（护栏强度不下降）。
    from orchd.spec import is_concession_file

    files_to_edit = [f for f in (task_def.get("files_to_edit") or []) if isinstance(f, str)]
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
            "task_id": task_id,
            "out_of_scope_files": sorted(high_risk),
            "auto_registered_files": sorted(concession),
            "declared_files": sorted(allowed),
            "main_worktree": main_wt,
            "hint": (
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
                    and ev.get("type") == "AMEND"
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
                    task_id, agent_id, "AMEND",
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
