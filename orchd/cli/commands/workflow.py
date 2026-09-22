"""Orchd CLI 路由：workflow 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-workflow-control）：
  - _cmd_request: request 命令（候选任务请求 + auto-claim）
  - claim_preview: claim 确认闸门预览（只读，不写事件不建分支）
  - _cmd_claim: claim 命令（实现/审查认领）
  - _cmd_done: done 命令（任务完成报告）
  - _cmd_review: review 命令（审查结果提交）

3a 阶段说明：本模块是 workflow 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.guide import (
    NEXT_ACTION_EXIT,
    NEXT_ACTION_REVIEW_TAKEOVER,
    NEXT_ACTION_WAIT,
)
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)

from orchd.ledger import structured_error

from orchd.cli.identity import (
    _detect_claim_role,
    _identity_warning,
    _require_agent_id,
    _session_collision_warning,
)
from orchd.cli.skeleton import _cli_skeleton


@_cli_skeleton
def _cmd_request(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """获取下一个可认领的候选任务。

    CLI 参数: args.capabilities、args.exclude、
    args.sort（importance/downstream/hours）、args.auto_claim（--auto-claim，
    候选返回后自动 claim）。agent 身份由引擎按宿主注入的 ORCHD_SESSION_ID
    （session-id-fingerprint）。
    不再有 --agent/--role。
    返回: 匹配的任务信息或空结果。--auto-claim 时附加 claim 结果（或错误）。
    """
    from orchd.ledger import stale_review_claims
    from orchd.onboard import claim, request
    enforce_self_review_block = bool(
        master.config.get("enforce_self_review_block") if hasattr(master, "config") else False
    )
    result = request(
        store, tasks, agent_id=agent_id,
        capabilities=_flatten_nargs(args.capabilities),
        exclude=_flatten_nargs(args.exclude),
        sort_key=args.sort,
        max_active=getattr(args, "max_active", None),
        importance_thresholds=(
            (master.config.get("importance") if hasattr(master, "config") else None)
            or None
        ),
        enforce_self_review_block=enforce_self_review_block,
        conflict_policy=(
            (master.config.get("conflict_policy") if hasattr(master, "config") else None)
            or None
        ),
    )

    # task-review-comments-gate-and-stale-timeout（D）：request 候选只透出
    # review_comments_count（条数），不摆意见正文，避免逐候选 token 浪费；
    # 完整意见仅在 claim --confirm 时由 review_comments 字段给出。
    if result.get("candidate"):
        _rc = result["candidate"].pop("review_comments", None)
        result["candidate"]["review_comments_count"] = (
            len(_rc) if isinstance(_rc, list) else 0
        )

    # W-2 僵尸审查认领巡检：request 响应恒附 stale_reviews，无候选时把
    # next_action 抬为 review_takeover 并给接管命令，避免 agent 卡在死锁里
    # （判定复用 ledger 派生，与 status/doctor 一致）。
    stale_reviews: dict[str, dict[str, object]] = {}
    try:
        stale_reviews = stale_review_claims(store.replay())
    except Exception:
        pass
    if stale_reviews:
        ordered = sorted(stale_reviews.items(), key=lambda kv: kv[1]["age_s"], reverse=True)
        result["stale_reviews"] = [
            {"task_id": tid, **v} for tid, v in ordered
        ]
        takeover_msg = (
            f"僵尸审查认领 {len(stale_reviews)} 个（认领超时未提交）："
            + "、".join(f"{tid}({v['review_phase']},{v['claimed_by']},{v['age_s']}s)" for tid, v in ordered)
            + "。可接管：先 python .orchd/__main__.py retract --task <id> --type "
            "REVIEW_CLAIMED（引擎对超时认领放行跨 agent 回收），再重新认领审查。"
        )
        if result.get("message"):
            result["message"] = f"{result['message']} {takeover_msg}"
        else:
            result["message"] = takeover_msg
        if result.get("candidate") is None:
            result["next_action"] = NEXT_ACTION_REVIEW_TAKEOVER

    # 无候选（candidate=None / next_action=exit|wait）：以引擎分配为准，
    # 附加 stop_wait 引导，明确"停止等待用户指令"，防止 agent 自行 claim/重试。
    # _attach_guidance 幂等（已有 guidance 不覆盖），此处预置即生效。
    if result.get("candidate") is None and result.get("next_action") in (
        NEXT_ACTION_EXIT, NEXT_ACTION_WAIT
    ):
        from orchd.guide import stop_wait_guidance

        result["guidance"] = stop_wait_guidance()

    # --auto-claim：候选非空时自动 claim（绕过人工确认）。
    # 默认禁用：仅当 _master.json 顶层 config.allow_auto_claim 显式为 true 时，
    # agent 才可调用 --auto-claim（用户明确授权）；否则结构化拒绝，防止无人值守
    # agent 绕过 claim 人工确认闸门连续领任务。
    if getattr(args, "auto_claim", False):
        allow_auto_claim = bool(
            (master.config.get("allow_auto_claim") if hasattr(master, "config") else False)
        )
        if not allow_auto_claim:
            result["auto_claim_disabled"] = True
            _details32 = [
                {
                    "message": "agent 不得擅自使用 --auto-claim 连续领任务。仅在用户明确于 _master.json 顶层 config.allow_auto_claim 设为 true 后才允许。",
                    "hint": "请使用 python .orchd/__main__.py claim --task <id> --confirm 手动认领，或在 _master.json 顶层 config.allow_auto_claim 设为 true 后重试",
                    "command": "python .orchd/__main__.py claim --task <id> --confirm",
                }
            ]
            _resp32 = structured_error("E032", "自动认领（--auto-claim）默认禁用", _details32, orchd_dir)
            result["error"] = _resp32.get("error")
            # Channel C: attach guidance at top-level as well (structured_error已含guidance)
            if "guidance" in _resp32:
                result["guidance"] = _resp32["guidance"]
            return result
    if getattr(args, "auto_claim", False) and result.get("candidate"):
        candidate_id = result["candidate"]["task_id"]
        shared = master.shared if hasattr(master, "shared") else None
        claim_result = claim(
            store, tasks, agent_id=agent_id, task_id=candidate_id,
            project_root=orchd_dir.parent, shared=shared,
            with_context=getattr(args, "with_context", False),
            enforce_self_review_block=enforce_self_review_block,
        )
        result["auto_claimed"] = True
        result["claimed"] = claim_result
    return result


def claim_preview(
    store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str = "implementer",
    project_root: Path | None = None,
    review_type: str | None = None,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    """claim 前确认闸门预览（task-claim-confirm-gate，只读，不写事件不建分支）。

    显式 ``orchd claim``（无 ``--confirm``）先展示预览：claim_type
    （implementer/reviewer）、任务基本信息、当前状态、git 状况（分支 + 工作区
    干净度）、将执行动作，以及 reviewer 角色的 ``review_phase``（spec/code）、
    ``reviewers`` 名单与 self-review 预期校验（E016）。用户确认无误后再以
    ``--confirm`` 真正执行 claim（走 ``onboard.claim`` 的锁内 check-then-act）。

    与 ``onboard.claim`` 的区别：本函数只读（replay + 派生缓存 + git 状态探测），
    不写任何事件、不建分支；所有"校验预期"仅作透明展示，不抛错阻断。
    预览逻辑归属 CLI 层：任务 files_to_edit 仅声明 orchd/cli.py（mod-core）。

    Args:
        store: ``orchd.ledger.Store`` 实例。
        review_type: reviewer 认领时显式指定的审查阶段（spec/code）。
    """
    from orchd.gitops import check_workspace_state
    from orchd.onboard import _extract_last_done
    from orchd.onboard.claim import build_scope_warning
    from orchd.report import task_revive_markers

    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E005, f"task '{task_id}' not found in master",
                         [{"task_id": task_id, "hint": f"任务 {task_id} 在 _master.json 中不存在，检查 id 拼写或注册"}])

    state = store.replay()
    derived = store.scan_task_derived()
    ts = state.get(task_id)
    status = ts.status if ts else "pending"
    # review-unify-r2：unified 模式下审查阶段显示为 unified（单阶段），
    # 不再回落 spec；two_phase 模式保持 spec/code 展示。
    from orchd.ledger import resolve_review_mode
    if resolve_review_mode(store.orchd_dir) == "unified":
        current_phase = "unified"
    else:
        current_phase = (ts.review_phase if ts else None) or "spec"

    preview: dict[str, Any] = {
        "claim_type": role,
        "task_id": task_id,
        "name": task_def.get("name", ""),
        "brief": task_def.get("brief", ""),
        "module": task_def.get("module", ""),
        "depends_on": task_def.get("depends_on", []),
        "reviewers": task_def.get("reviewers", []),
        "current_status": status,
        "review_phase": current_phase,
        # task-claim-preview-and-scope-triage：声明范围与校验命令前置展示
        "files_to_edit": task_def.get("files_to_edit", []),
        "exempt_files": task_def.get("exempt_files", []),
        "verify_command": task_def.get("verify_command", ""),
        # cwd 预期：实现 claim 须在 main，审查 claim 须在 task 分支（两者规则相反）
        "cwd_expected": "main（主工作树）" if role == "implementer" else f"task/{task_id}（任务 worktree）",
    }
    # task-review-comments-gate-and-stale-timeout（D）：预览阶段只透出意见条数，
    # 不摆正文（token 克制）；完整意见仅在 claim --confirm 返回体的 review_comments
    # 字段一次给出。初次实现任务无意见 → 0；返工任务 → 实际条数。
    from orchd.review import extract_review_comments as _ecr
    preview["review_comments_count"] = len(_ecr(store, task_id, derived))

    # git 状况（best-effort，非 git 环境降级为 available:false）
    if project_root is not None:
        ws = check_workspace_state(project_root)
        preview["git"] = {
            "available": ws.get("available", False),
            "branch": ws.get("branch"),
            "clean": ws.get("clean"),
        }
    else:
        preview["git"] = {"available": False}

    # 将执行动作（claim() 会做的事）
    preview["actions"] = [
        f"写 {'REVIEW_CLAIMED' if role == 'reviewer' else 'CLAIMED'} 事件到 ledger",
        f"创建/切换 task/{task_id} 分支",
    ]

    # 校验预期（与 claim() 锁内校验一致，透明展示；失败仅提示不阻断预览）
    if role == "reviewer":
        done_author, _ = _extract_last_done(store, task_id, derived)
        preview["done_by"] = done_author
        is_self = bool(done_author and done_author == agent_id)
        preview["expected_checks"] = [
            {"check": "任务处于 in_review（可认领审查）",
             "expected_pass": status == "in_review"},
            {"check": "agent 在任务 reviewers 名单内",
             "expected_pass": agent_id in task_def.get("reviewers", []),
             "note": "指纹身份（12位hex）默认豁免此名单校验（ledger.is_fingerprint_agent_id），expected_pass=false 不阻断认领；具名 agent 身份才需在 reviewers 名单内"},
            {"check": "审查阶段与当前 review_phase 匹配",
             "expected_pass": (not review_type) or review_type == current_phase},
        ]
        # 自审（E016）降级为提示项：默认不阻断，仅标注；enable 时才作为检查生效
        if is_self:
            preview["expected_checks"].append({
                "check": "自审提示（E016：实现者 = 审查者）",
                "expected_pass": not enforce_self_review_block,
                "note": "默认仅提示不阻断；线上版 config.enforce_self_review_block=true 时该检查才生效并阻断",
            })
    else:
        preview["expected_checks"] = [
            {"check": "任务处于 pending（可认领）",
             "expected_pass": status == "pending"},
            {"check": "依赖全部满足",
             "expected_pass": all(
                 (state.get(d).status if state.get(d) else "pending")
                 in ("completed", "cancelled") for d in task_def.get("depends_on", [])
             )},
            {"check": "未被其他 agent 认领",
             "expected_pass": not (ts and ts.claimed_by and ts.claimed_by != agent_id)},
        ]
    # 复活标记（task-force-status-revive-audit）：该任务曾有 completed→pending 复活
    # 历史时透明展示（reason+evidence_sha+时间），正常任务不展示（零误伤）。
    markers = task_revive_markers(store)
    if task_id in markers:
        preview["revive_marker"] = markers[task_id]
    # task-amend-scope-add：claim 预览连带文件预警
    # task-fix-claim-scope-warning：与 onboard.claim 共用 build_scope_warning 单一来源
    if role == "implementer":
        scope_warning = build_scope_warning(task_def, project_root=project_root)
        if scope_warning is not None:
            preview["scope_warning"] = scope_warning
    return preview


@_cli_skeleton
def _cmd_claim(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """认领指定任务。

    CLI 参数: args.task（必需）、args.confirm（--confirm，确认执行认领）。
    agent 身份由引擎按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint）；claim 按任务
    当前状态自动分流：in_review → 审查认领（REVIEW_CLAIMED），pending →
    实现认领（CLAIMED），不再有 --agent/--role。
    返回: 认领事件信息；无 --confirm 时仅返回确认闸门预览
    （confirm_required:true + preview，不写事件、不建分支）。
    """
    from orchd.onboard import claim
    from orchd.worktree import resolve_canonical_project_root

    shared = master.shared if hasattr(master, "shared") else None
    # canonical-project-root（2026-08-28 修复）：claim 的 project_root 与
    # pool/status/done 等读一致统一走 canonical。否则在容器根（其下残留
    # .orchd 时 _find_orchd_dir 会命中容器根）执行 claim，ensure_task_wt 的
    # _propagate_container_marker 会把任务 worktree 布局标记的 main_worktree
    # 写成容器根，导致任务 worktree 账本解析错位（done 报 not in claimed）。
    project_root = resolve_canonical_project_root(orchd_dir.parent)
    agent_id = _require_agent_id(orchd_dir)
    role = _detect_claim_role(store, tasks, args.task)
    enforce_self_review_block = bool(
        master.config.get("enforce_self_review_block") if hasattr(master, "config") else False
    )

    # 确认闸门：无 --confirm 仅输出预览（只读，不写事件、不建分支）
    if not getattr(args, "confirm", False):
        preview = claim_preview(
            store, tasks, agent_id=agent_id, task_id=args.task,
            role=role, project_root=project_root,
            review_type=getattr(args, "review_type", None),
            enforce_self_review_block=enforce_self_review_block,
        )
        result = {
            "confirm_required": True,
            "claim_type": preview["claim_type"],
            "hint": "预览模式：确认无误后请加 --confirm 真正执行认领（写事件 + 建分支）",
            "preview": preview,
        }
        warning = _identity_warning(agent_id, orchd_dir)
        if warning:
            result["warning"] = warning
        collision = _session_collision_warning(
            agent_id, store, exclude_task_id=args.task,
            review_task_id=args.task if role == "reviewer" else None,
        )
        if collision:
            result["session_collision_warning"] = collision
        return result

    result = claim(
        store, tasks, agent_id=agent_id, task_id=args.task,
        project_root=project_root, shared=shared,
        review_type=getattr(args, "review_type", None),
        with_context=getattr(args, "with_context", False),
        enforce_self_review_block=enforce_self_review_block,
        force=getattr(args, "force", False),
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    collision = _session_collision_warning(
        agent_id, store, exclude_task_id=args.task,
        review_task_id=args.task if role == "reviewer" else None,
    )
    if collision:
        result["session_collision_warning"] = collision
    return result


@_cli_skeleton
def _cmd_done(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """报告任务完成，提交变更描述与可选的关切事项。

    CLI 参数: args.task（必需）、args.changes / args.changes_file（二选一，
    变更描述）、args.concerns（可选关切事项）。agent 身份由引擎自动按宿主
    注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 完成事件信息。
    """
    from orchd.onboard import done

    agent_id = _require_agent_id(orchd_dir)
    changes = _resolve_text_arg(args.changes, args.changes_file, "--changes", "--changes-file")
    # 红线 #3 硬化：done 前提前校验范围外文件（复用 L3 同一判定 _guard_out_of_scope，不出现两套标准）
    # 提前在 verify 之前失败，verify 未执行
    # task-flat-decl-authority：early guard 的 task_def 经 resolve_declaration_source
    # 解析（flat 下从 main blob 读权威声明；否则陈旧声明会在引擎之前先误拦 E010）。
    from orchd.worktree import resolve_declaration_source as _resolve_decl_early
    _tasks_early = _resolve_decl_early(orchd_dir.parent, tasks, None)[0]
    _task_def_early = {t.get("id", ""): t for t in _tasks_early}.get(args.task)
    if _task_def_early is not None:
        from orchd.onboard import _guard_out_of_scope as _early_scope_guard
        _early_scope_guard(orchd_dir.parent, _task_def_early, args.task, [])
    result = done(
        store, tasks, agent_id=agent_id, task_id=args.task,
        changes_description=changes, concerns=args.concerns,
        project_root=orchd_dir.parent,
        skip_lesson_review=getattr(args, "skip_lesson_review", False),
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    return result


def _cmd_review_show(args) -> dict:
    """只读回看任务全部历史审查意见（task-review-comments-readback）。

    与 --verdict 互斥（同时提供直接拒绝）；不写任何事件、不改任务状态、
    不要求会话身份；completed（含归档）与无意见任务均可调用（无意见返回空列表）。
    --type 在回看模式下不作过滤（回看全部历史）。
    """
    from orchd.ledger import Store
    from orchd.cli import _load_tasks
    from orchd.review import extract_review_history

    if getattr(args, "verdict", None) is not None:
        raise OrchdError(
            ErrorCode.E007,
            "--show 与 --verdict 只能二选一",
            [{"arguments": ["--show", "--verdict"]}],
        )
    tasks, orchd_dir, _ = _load_tasks()
    task_map = {t.get("id", ""): t for t in tasks}
    if task_map.get(args.task) is None:
        raise OrchdError(
            ErrorCode.E005,
            f"task '{args.task}' not found in master",
            [{"task_id": args.task, "hint": f"任务 {args.task} 在 _master.json 中不存在，检查 id 拼写或注册"}],
        )
    store = Store(orchd_dir)
    history = extract_review_history(store, args.task)
    return {"task_id": args.task, "comments": history, "count": len(history)}


def _cmd_review(args) -> dict:
    from orchd.cli import _load_tasks
    from orchd.cli import _maybe_archive_ideas
    from orchd.cli._util import _preimport_archive_deps

    """提交审查结果（spec review 或 code review）。

    CLI 参数: args.task（必需）、args.type（spec/code）、
    args.verdict（APPROVED/CHANGES_REQUESTED）、
    args.comments / args.comments_file（可选，二选一）。agent 身份由引擎自动按
    宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 审查事件信息。
    """
    from orchd.ledger import Store
    from orchd.review import review_submit

    if getattr(args, "show", False):
        return _cmd_review_show(args)
    if getattr(args, "verdict", None) is None:
        raise OrchdError(
            ErrorCode.E007,
            "必须提供 --verdict（提交模式），或改用 --show 只读回看",
            [{"arguments": ["--verdict", "--show"]}],
        )
    comments = _resolve_text_arg(
        args.comments, args.comments_file, "--comments", "--comments-file",
        required=False,
    )
    tasks, orchd_dir, _ = _load_tasks()
    task_map = {t.get("id", ""): t for t in tasks}
    if task_map.get(args.task) is None:
        raise OrchdError(
            ErrorCode.E005,
            f"task '{args.task}' not found in master",
            [{"task_id": args.task, "hint": f"任务 {args.task} 在 _master.json 中不存在，检查 id 拼写或注册"}],
        )
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    # 终态归档依赖预导入（task-review-archive-selfdelete-fix AC1）：review_submit
    # 成功（code APPROVED）会终态回收任务 worktree，连带删除本进程 orchd 源码
    # 目录；归档唯一懒加载点 orchd.ideas 必须在源码尚存时绑定进 sys.modules，
    # 否则回收后再导入即 ModuleNotFoundError（归档静默失效）。预导入失败不阻断
    # ——_maybe_archive_ideas 的子进程兜底会接管。
    _preimport_archive_deps()
    result = review_submit(
        store, tasks, agent_id=agent_id, task_id=args.task,
        review_type=args.type, verdict=args.verdict, comments=comments,
        project_root=orchd_dir.parent,
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("task_status") == "completed":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def register(sub) -> None:
    """注册 workflow 模块的子命令。"""
    # request
    p = sub.add_parser("request", help="获取下一个候选任务")
    p.add_argument("--capabilities", nargs="*")
    p.add_argument("--exclude", nargs="*")
    p.add_argument("--sort", choices=["importance", "downstream", "hours"])
    p.add_argument("--auto-claim", action="store_true",
                   help="request 成功返回候选后自动执行 claim（绕过人工确认，适合无人值守场景）")
    p.add_argument("--with-context", action="store_true",
                   help="--auto-claim 时附加全部共享上下文（默认按需）")
    p.add_argument("--max-active", type=int, default=None,
                   help="全局活跃（claimed）任务数达到该值时拒绝候选（容量控制）")
    p.set_defaults(func=_cmd_request)

    # claim
    p = sub.add_parser("claim", help="认领任务")
    p.add_argument("--task", required=True)
    p.add_argument("--type", dest="review_type", choices=["spec", "code"],
                   help="reviewer 认领时指定审查阶段（默认锁任务当前阶段）")
    p.add_argument("--confirm", action="store_true",
                   help="确认执行认领（无 --confirm 时仅输出预览，不写事件、不建分支）")
    p.add_argument("--with-context", action="store_true",
                   help="显式附加全部共享上下文（architecture + conventions），默认按需")
    p.add_argument("--force", action="store_true",
                   help="绕过 retract 认领冷却期（task-retract-bind-cooloff）")
    p.set_defaults(func=_cmd_claim)

    # done
    p = sub.add_parser("done", help="报告任务完成")
    p.add_argument("--task", required=True)
    p.add_argument("--changes")
    p.add_argument("--changes-file", help="从文件读取变更描述（UTF-8），与 --changes 二选一")
    p.add_argument("--concerns")
    p.add_argument("--skip-lesson-review", dest="skip_lesson_review",
                   action="store_true",
                   help="跳过 lesson 收尾 hook（CI/CD/自动化场景，§8.6 bypass）")
    p.set_defaults(func=_cmd_done)

    # review
    p = sub.add_parser("review", help="提交审查结果")
    p.add_argument("--task", required=True)
    # review-unify-r2：unified 单阶段模式下无需 --type（一次 APPROVED 即 merge）；
    # two_phase 模式仍须传 spec/code。
    p.add_argument("--type", required=False, choices=["spec", "code"],
                   help="审查阶段（spec/code）；unified 单阶段模式下可省略")
    p.add_argument("--verdict", required=False, choices=["APPROVED", "CHANGES_REQUESTED"],
                   help="审查结论（提交模式必填；--show 回看模式下不得提供）")
    p.add_argument("--comments")
    p.add_argument("--comments-file", help="从文件读取审查意见（UTF-8），与 --comments 二选一")
    p.add_argument("--show", action="store_true",
                   help="只读回看该任务全部历史审查意见（与 --verdict 互斥，不写事件）")
    p.set_defaults(func=_cmd_review)

