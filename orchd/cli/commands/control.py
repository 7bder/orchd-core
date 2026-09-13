"""Orchd CLI 路由：control 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-workflow-control）：
  - _cmd_amend: amend 命令（snapshot 增量更新）
  - _cmd_retract: retract 命令（事件撤回）
  - _cmd_force_status: force-status 命令（强制状态转换）
  - _cmd_merge_ack: merge-ack 命令（merge_warning 人工销账）

3a 阶段说明：本模块是 control 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)

from orchd.ledger import structured_error

from orchd.cli.identity import _identity_warning, _require_agent_id
from orchd.cli.skeleton import _cli_skeleton


@_cli_skeleton
def _cmd_amend(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """增量更新 snapshot，依据状态约束矩阵过滤变更。

    CLI 参数: args.master — master 文件路径（默认 .orchd/_master.json）。
    返回: 变更摘要字典（new_tasks / updated_tasks / unchanged_tasks / removed_tasks），
    成功后附加 commit 字段（best-effort 自动提交 master 与 IDEAS.md，不阻塞）；
    新增/变更任务若声明 verify_command，附加 verify_dry_run 字段（试跑结果仅提示、不阻断注册）。
    """
    import json as _json
    import subprocess

    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.ledger import Store, resolve_store_dir, resolve_workspace_root
    from orchd.onboard import _decode_subprocess_output
    from orchd.spec import load_master
    from orchd.split import (
        amend,
        is_text_only_spec_revision,
        validate_terminal_revision,
    )

    # amend-only-canonical（task-master-single-copy）：分支守卫前移——在 canonical
    # 化**之前**检查调用方 cwd 所在分支。此前守卫位于 split.amend 内且基于
    # orchd_dir.parent 判定，而本函数先 canonical 到主工作树再调 amend，守卫查的
    # 永远是 main 分支 → 生产路径下守卫是死代码（review AC3 实锤：任务分支 amend
    # exit=0/amended:true）。前移后 container 任务 worktree 内执行 amend：cwd 分支
    # task/xxx ≠ default → 直接 E007，不触碰主工作树副本。split.amend 内守卫保留
    # 为函数级防御（直调者兜底）。
    from orchd.worktree import resolve_canonical_project_root

    caller_root = Path.cwd()
    caller_branch = get_current_branch(caller_root)
    default_branch = get_default_branch(caller_root) or "main"
    if caller_branch is not None and caller_branch != default_branch:
        try:
            canonical_root = resolve_canonical_project_root(caller_root)
        except Exception:
            canonical_root = caller_root
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_branch: amend 仅在 default（{default_branch}）分支执行（主工作树），"
            f"当前分支 {caller_branch} 拒绝注册（红线 7）",
            [{
                "branch": caller_branch,
                "default": default_branch,
                "main_worktree": str(canonical_root),
                "hint": (
                    f"任务分支不再允许 amend；请在主工作树 {canonical_root} 上补充/注册 "
                    "files_to_edit 等声明后再执行。"
                    "定位方法：git worktree list 中带 (main) 标记的路径即主工作树；"
                    "或从任务 worktree 路径上溯到项目根下的 main/ 目录。"
                    f'可执行命令：cd "{canonical_root}"; python .orchd/__main__.py amend '
                    "--task <id> --files-to-edit <file>"
                ),
            }],
        )

    default_master = getattr(args, "master", None)
    if default_master in (None, "", ".orchd/_master.json"):
        canonical_root = resolve_canonical_project_root(Path.cwd())
        master_path = canonical_root / ".orchd" / "_master.json"
    else:
        master_path = Path(default_master)
    master = load_master(master_path)
    orchd_dir = master_path.parent
    store = Store(orchd_dir)
    project_root = orchd_dir.parent

    # task-terminal-spec-revision-channel：终态规格文本修订通道（--revise-terminal）。
    # --reason 非空硬校验前移到此处（任何写入 / dry-run 之前 fail-fast）；终态性校验
    # 留在 split.amend 内（那里有 status 权威来源），两处共用 validate_terminal_revision
    # 单一事实源。
    revise_terminal = getattr(args, "revise_terminal", None)
    if revise_terminal is not None:
        validate_terminal_revision(getattr(args, "reason", None))

    # task-amend-decl-patch-channel：声明域 CLI 补登（--task + --files-to-edit /
    # --exempt-files / --verify-command）。列表类为并集追加语义（只增：CLI 表达
    # 不了删除，删改天然落 E007 矩阵）；verify_command 为覆写。补丁先落到内存
    # master，再走统一 amend 流程（白名单矩阵 + dry-run + 提交全复用）。
    patch_task = getattr(args, "task", None)
    patch_files = list(getattr(args, "files_to_edit", None) or [])
    patch_exempt = list(getattr(args, "exempt_files", None) or [])
    patch_verify = getattr(args, "verify_command", None)
    if patch_task is not None:
        if not (patch_files or patch_exempt or patch_verify is not None):
            raise OrchdError(
                ErrorCode.E007,
                "amend --task 需至少携带一个补丁字段",
                [{
                    "task_id": patch_task,
                    "hint": "补登示例：--files-to-edit <file> / --exempt-files <file> / "
                            "--verify-command \"<cmd>\"（列表类为并集追加，只增不删）",
                }],
            )
        target = next(
            (t for t in master.tasks if t.get("id") == patch_task), None,
        )
        if target is None:
            raise OrchdError(
                ErrorCode.E005,
                f"task '{patch_task}' not found in master",
                [{"task_id": patch_task,
                  "hint": f"任务 {patch_task} 在 _master.json 中不存在，检查 id 拼写或注册"}],
            )
        if patch_files:
            target["files_to_edit"] = sorted(
                set(target.get("files_to_edit", [])) | set(patch_files)
            )
        if patch_exempt:
            target["exempt_files"] = sorted(
                set(target.get("exempt_files", []) or []) | set(patch_exempt)
            )
        if patch_verify is not None:
            target["verify_command"] = patch_verify
        # 补丁落盘：后续 dry-run 预计算与 amend() 均以文件为准对齐；
        # amend 成功后的自动提交负责入库（与 intake 链路一致）。
        master_path.write_text(
            _json.dumps(master.raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # task-e028-dryrun-exit4-priority：dry-run 前置到写入/提交之前。
    # 原实现先 amend()（写 snapshot + amend 自动 commit）再 dry-run，E028 阻断时
    # snapshot/commit 已实际完成，错误文案「注册已阻断」与真实副作用不符（实测
    # ae2f9a2 已提交入库）。现改为：预计算将变更任务 → 先 dry-run 校验 → 阻断则
    # raise（此时未写入）；通过后再 amend() 写入 + 自动提交。
    from orchd.split import classify_dry_run_failure
    from orchd.spec import validate_quality, verify_command_dangerous_reasons
    from orchd.subproc import run_shell

    # 预计算将变更任务（与 split.amend 内部 existing_tasks 加载同源）：
    # store_root/mod-*/spec.json 的存量定义 vs master，值差异即视为将变更。
    store_root = Path(resolve_store_dir(orchd_dir))
    existing_tasks: dict[str, dict[str, Any]] = {}
    for spec_path in sorted(store_root.glob("mod-*/spec.json")):
        snapshot = _json.loads(spec_path.read_text(encoding="utf-8"))
        for t in snapshot.get("tasks", []):
            existing_tasks[t.get("id", "")] = t
    changed = [
        t.get("id", "")
        for t in master.tasks
        if t.get("id", "") not in existing_tasks
        or existing_tasks.get(t.get("id", "")) != t
    ]

    # task-terminal-spec-revision-channel：纯文本修订不改 verify_command → 重跑 dry-run
    # 无信息量；且目标任务的**存量** E024/E027（历史定义缺 --basetemp / 含不安全段）
    # 会把这次文本修订整体阻断，使通道对历史终态任务不可用。故把"仅文本修订"的目标
    # 任务同时剔出 dry-run 与存量告警阻断集合（只影响开关指向的那一个任务）。
    dry_run_skipped: list[str] = []
    if revise_terminal is not None:
        old_def = existing_tasks.get(revise_terminal)
        new_def = next(
            (t for t in master.tasks if t.get("id") == revise_terminal), None,
        )
        if (old_def is not None and new_def is not None
                and is_text_only_spec_revision(old_def, new_def)):
            dry_run_skipped.append(revise_terminal)
    dry_run_changed = [tid for tid in changed if tid not in dry_run_skipped]

    # dry-run 试跑将变更任务的 verify_command（与 done 相同 shell 执行、同 cwd、
    # 限时 30s；2026-08-08 升级：assertion_mismatch 类失败阻断注册（E028），
    # E024/E027（缺 basetemp / 不安全段）阻断注册；expected_pending 仅提示）
    task_map = {t.get("id", ""): t for t in master.tasks}
    dry_run_results: list[dict[str, Any]] = []
    blocking_errors: list[dict[str, Any]] = []
    for tid in dry_run_changed:
        verify_cmd = task_map.get(tid, {}).get("verify_command")
        if not verify_cmd:
            continue
        _dangerous = verify_command_dangerous_reasons(verify_cmd)
        if _dangerous:
            blocking_errors.append({
                "code": ErrorCode.E027.name,
                "task_id": tid,
                "verify_command": verify_cmd,
                "reasons": _dangerous,
                "message": (
                    "verify_command 含 shell 注入风险，dry-run 拒绝执行（E027）"
                ),
            })
            continue
        try:
            proc = run_shell(verify_cmd, str(project_root), 30)
            failure_class = None
            if proc.returncode != 0:
                # E028 误判修复（task-fix-e028-dryrun-created-file）：缺失路径若属于
                # 本任务 files_to_edit（即将创建）→ expected_pending 仅提示不阻断
                _to_be_created = set(
                    task_map.get(tid, {}).get("files_to_edit", []) or []
                )
                failure_class = classify_dry_run_failure(
                    verify_cmd, proc.returncode,
                    _decode_subprocess_output(proc.stderr)[:500],
                    _decode_subprocess_output(proc.stdout)[:300],
                    _to_be_created,
                )
                if failure_class == "assertion_mismatch":
                    _msg28 = "dry-run 断言不匹配（assertion_mismatch）：verify_command 引用现有文件但断言失败/语法错误，注册已阻断（E028）"
                    _details28 = [{"task_id": tid, "verify_command": verify_cmd, "exit_code": proc.returncode, "stderr": _decode_subprocess_output(proc.stderr)[:500]}]
                    _resp28 = structured_error("E028", _msg28, _details28, project_root)
                    _err28 = _resp28.get("error", {})
                    _guid28 = _resp28.get("guidance")
                    blocking_errors.append({
                        "code": _err28.get("code", "E028"),
                        "task_id": tid,
                        "verify_command": verify_cmd,
                        "exit_code": proc.returncode,
                        "stderr": _decode_subprocess_output(proc.stderr)[:500],
                        "message": _err28.get("message", _msg28),
                        "details": _err28.get("details", _details28),
                        "guidance": _guid28,
                        "severity": _err28.get("severity", "error"),
                    })
            dry_run_results.append({
                "task_id": tid,
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "failure_class": failure_class,
                "stderr": _decode_subprocess_output(proc.stderr)[:500],
                "hint": (
                    "dry-run 仅提示不阻断注册：实现未完成时失败属预期（可忽略）；"
                    "若断言应匹配现有文件而失败，则 verify_command 定义可能有误，建议核对"
                ),
            })
        except (subprocess.SubprocessError, OSError):
            # 运行环境异常：静默跳过，不影响 amend 主流程
            continue

    # E024/E027 阻断：amend 注册点对新增/变更任务的 verify_command 质量校验
    # （E024 缺 basetemp / E027 不安全段 → 阻断注册，不再等 done 期 E014）
    # AC4 grandfather：仅收集 changed 任务（或新注册任务）的 E024/E027，
    # 存量任务命中仅 warning 不阻断——避免存量 E027/E024 阻塞任何后续 amend。
    qerrors = validate_quality(master)
    changed_set = set(dry_run_changed)
    for qe in qerrors:
        if qe.code in (ErrorCode.E024, ErrorCode.E027):
            # 从 path（$.tasks[i].verify_command）解析任务 index → 定位 task id
            import re as _re
            m_idx = _re.search(r"\$\.tasks\[(\d+)\]", qe.path)
            tid = None
            if m_idx:
                idx = int(m_idx.group(1))
                if 0 <= idx < len(master.tasks):
                    tid = master.tasks[idx].get("id")
            # grandfather：仅 changed 任务阻断，存量（未变更）命中跳过
            if tid is not None and tid in changed_set:
                blocking_errors.append({
                    "code": qe.code.name,
                    "path": qe.path,
                    "message": qe.message,
                })

    if blocking_errors:
        # dry-run 前置：此时 amend 尚未执行，snapshot/commit 均未写入，
        # 「阻断注册」与真实副作用一致（task-e028-dryrun-exit4-priority）。
        raise OrchdError(
            ErrorCode.E028 if any(e.get("code") == "E028" for e in blocking_errors)
            else ErrorCode.E027,
            "amend_blocked: verify_command 校验未通过（注册已阻断，未写入 snapshot）",
            blocking_errors,
        )

    # 校验通过后执行 amend（写入 snapshot + checkpoint；透传终态文本修订通道）
    result = amend(
        orchd_dir, master, store,
        revise_terminal=revise_terminal,
        reason=getattr(args, "reason", None),
    )

    # 成功后 best-effort 自动提交（锁外、不阻塞状态机，语义对齐 merged:false）
    summary = ", ".join(changed) if changed else "snapshot refresh"

    # 分支校验：intake/amend 约定只在 main 执行，非 main 时降级为不提交，
    # 避免 master+IDEAS.md 被误提交进任务分支（污染待 merge 内容）
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        result["commit"] = {
            "performed": False,
            "reason": "not_on_main",
            "branch": current_branch,
        }
    else:
        # AC3（task-12-engine-path-abstraction）：IDEAS.md 走统一工作区根 helper
        # （默认 .orchd/，兼容旧根路径）；commit 路径与 ensure_committed 期望一致。
        ws_root = resolve_workspace_root(project_root)
        # intake-commit-enforcement（2026-08-14）：提交范围含 ROADMAP.md——
        # roadmap 摄入改的 ROADMAP.md 此前不在范围，必然残留未提交改动
        commit = ensure_committed(
            project_root,
            [str(master_path), str(ws_root / "IDEAS.md"), str(ws_root / "ROADMAP.md")],
            f"chore(intake): orchd amend — {summary}",
        )
        result["commit"] = commit
        # intake-commit-enforcement（2026-08-14）：commit 降级可审计化（对齐
        # merge_warning 先例）——注册成功后 commit 未执行（非 no_changes）不再
        # 静默：写入 commit_warning 供 status --audit-intake 巡检。git 环境不可用
        # （not_a_git_repo / git_unavailable）保留 best-effort 降级（判据 3）；
        # git 可用但提交失败（commit_failed）同样告警——"注册成功但改动未入库"
        # 违背"强制提交"语义，须人工核对。
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"amend 注册成功但 commit 未执行（{commit.get('reason')}）："
                    "摄入产物改动可能未入库"
                ),
                "hint": (
                    "若为 git 环境异常，可运行 'orchd status --audit-intake' "
                    "巡检未提交摄入产物，或运行 'orchd intake' 手动提交"
                ),
            }

    if dry_run_results:
        result["verify_dry_run"] = dry_run_results
    if dry_run_skipped:
        # 透明化：本次跳过 dry-run 的任务（纯文本修订，verify_command 未变）
        result["verify_dry_run_skipped"] = dry_run_skipped
    return result


def _cmd_retract(args) -> dict:
    from orchd.cli import _load_tasks

    """撤回已提交的事件。

    CLI 参数: args.event（可选，事件 ID 精确撤回）或 args.task + args.event_type
    （可选，按任务+类型自动定位最近匹配事件）、args.reason（必需）。
    返回: 撤回事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import retract

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)

    event_id = getattr(args, "event", None)
    task_id = getattr(args, "task", None)
    event_type = getattr(args, "event_type", None)

    if not event_id and not (task_id and event_type):
        return {"error": "retract 需要 --event <事件ID> 或 --task <任务ID> --type <事件类型>"}

    return retract(
        store, agent_id=agent_id, target_event_id=event_id,
        reason=args.reason, project_root=orchd_dir.parent,
        task_id=task_id, event_type=event_type,
    )


def _cmd_force_status(args) -> dict:
    from orchd.cli import _load_tasks
    from orchd.cli import _maybe_archive_ideas

    """强制设置任务状态（用于恢复僵死任务或手动干预）。

    CLI 参数: args.task（必需）、args.status（必需，目标状态）、
    args.reason（必需）、args.assignee（可选，指定认领人）、
    args.force（可选，逃生口二次确认——claimed→completed / cancelled→pending）、
    args.evidence_sha（可选，completed→pending 复活所需的 git 证据 commit SHA）。
    agent 身份由引擎自动按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 强制状态变更事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import force_status

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    result = force_status(
        store, agent_id=agent_id, task_id=args.task,
        target_status=args.status, reason=args.reason, assignee=args.assignee,
        force=args.force, project_root=orchd_dir.parent,
        evidence_sha=args.evidence_sha,
    )
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("new_status") == "cancelled":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _cmd_merge_ack(args) -> dict:
    from orchd.cli import _load_tasks

    """merge_warning 人工销账：登记 .orchd/merge-acks.json（task-merge-warning-ack）。

    CLI 参数: args.task（必需，已人工确认的 task_id）、args.reason（必需，确认原因）。
    与 resolve_sha 自动销账互补：人工路径兜底旧事件 / 无 sha 场景，登记后
    audit-merge 不再报 merge_warning_unresolved。
    返回: 登记结果 {acked, task_id, acked_at, reason}。
    """
    from orchd.report import merge_ack

    _, orchd_dir, _ = _load_tasks()
    return merge_ack(orchd_dir.parent, args.task, args.reason)


def register(sub) -> None:
    """注册 control 模块的子命令。"""
    # amend
    p = sub.add_parser("amend", help="增量更新 snapshot")
    p.add_argument("--master", default=".orchd/_master.json")
    p.add_argument("--task", default=None,
                   help="声明域补登：目标任务 id（需至少再带一个补丁字段）")
    p.add_argument("--files-to-edit", nargs="*", action="extend", default=None,
                   help="补登 files_to_edit（并集追加，只增不删；支持重复标志累加）")
    p.add_argument("--exempt-files", nargs="*", action="extend", default=None,
                   help="补登 exempt_files（并集追加，只增不删；支持重复标志累加）")
    p.add_argument("--verify-command", default=None,
                   help="覆写 verify_command")
    p.add_argument("--revise-terminal", default=None, dest="revise_terminal",
                   help="终态任务规格文本修订：目标 task_id（须为 completed/cancelled，"
                        "需配 --reason；仅放行 acceptance_criteria / brief / name / "
                        "deliverables，写 AMEND 审计事件并同步快照）")
    p.add_argument("--reason", default=None,
                   help="修订理由（--revise-terminal 必填、非空；写入 AMEND 审计事件）")
    p.set_defaults(func=_cmd_amend)

    # retract
    p = sub.add_parser("retract", help="撤回事件")
    p.add_argument("--event", required=False, default=None,
                   help="事件 ID（精确撤回）；与 --task + --type 二选一")
    p.add_argument("--task", required=False, default=None,
                   help="任务 ID（配合 --type 自动定位最近匹配事件）")
    p.add_argument("--type", required=False, default=None, dest="event_type",
                   choices=["CLAIMED", "DONE", "REVIEW_CLAIMED", "REVIEW_SUBMITTED",
                            "REVIEW_READY", "AMEND", "MERGE_WARNING"],
                   help="事件类型（配合 --task 自动定位最近匹配事件）")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_retract)

    # force-status
    p = sub.add_parser("force-status", help="强制设置任务状态")
    p.add_argument("--task", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--assignee")
    p.add_argument("--force", action="store_true",
                   help="显式确认走逃生口（claimed→completed / cancelled→pending）")
    p.add_argument("--evidence-sha", default=None,
                   help="复活已完成任务的 git 证据 commit SHA（仅 completed→pending 时需要）")
    p.set_defaults(func=_cmd_force_status)

    # merge-ack（task-merge-warning-ack）
    p = sub.add_parser("merge-ack", help="merge_warning 人工销账（merge-acks 确认清单）")
    p.add_argument("--task", required=True, help="已人工确认的 task_id")
    p.add_argument("--reason", required=True, help="确认原因（必填）")
    p.set_defaults(func=_cmd_merge_ack)

