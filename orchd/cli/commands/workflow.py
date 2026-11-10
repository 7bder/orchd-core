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

import json
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_REVIEW_CLAIMED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
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
from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD,
    BRIEF_FIELD,
    DEPENDS_ON_FIELD,
    EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
    IMPORTANCE_FIELD,
    MODULE_FIELD,
    NAME_FIELD,
    REVIEWERS_FIELD,
    SOURCE_FIELD,
    VERIFY_COMMAND_FIELD,
)

from orchd.ledger import structured_error
from orchd.line import TASK_BRANCH_ROOT

from orchd.cli.identity import (
    _detect_claim_role,
    _identity_warning,
    _require_agent_id,
    _session_collision_warning,
)
from orchd.cli.skeleton import _cli_skeleton
from orchd.contracts.responses import (
    BRANCH_KEY,
    CLAIMED_KEY,
    COMMAND_KEY,
    COMMENTS_KEY,
    COUNT_KEY,
    DONE_KEY,
    ERROR_KEY,
    GUIDANCE_KEY,
    HINT_KEY,
    MESSAGE_KEY,
    NEXT_ACTION_KEY,
    SUMMARY_KEY,
    TASK_ID_KEY,
    TASK_STATUS_KEY,
)


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
    from orchd.onboard import request
    from orchd.onboard.claim import claim
    # P0-2（task-review-hardblock-e2-request-gate）：自审候选在 request 侧
    # 恒被排除（无 config / 任务级开关），不会产出「派了必被 E016 拒」的候选。
    result = request(
        store, tasks, agent_id=agent_id,
        capabilities=_flatten_nargs(args.capabilities),
        exclude=_flatten_nargs(args.exclude),
        sort_key=args.sort,
        max_active=getattr(args, "max_active", None),
        importance_thresholds=(
            (master.config.get(IMPORTANCE_FIELD) if hasattr(master, "config") else None)
            or None
        ),
        conflict_policy=(
            (master.config.get("conflict_policy") if hasattr(master, "config") else None)
            or None
        ),
        phase=getattr(args, "phase", None),
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
    stale_reviews: dict[str, dict[str, Any]] = {}
    try:
        stale_reviews = stale_review_claims(store.replay())
    except Exception:
        pass
    if stale_reviews:
        ordered = sorted(stale_reviews.items(), key=lambda kv: kv[1]["age_s"], reverse=True)
        result["stale_reviews"] = [
            {TASK_ID_KEY: tid, **v} for tid, v in ordered
        ]
        takeover_msg = (
            f"僵尸审查认领 {len(stale_reviews)} 个（认领超时未提交）："
            + "、".join(f"{tid}({v['review_phase']},{v['claimed_by']},{v['age_s']}s)" for tid, v in ordered)
            + "。可接管：先 python .orchd/__main__.py retract --task <id> --type "
            "REVIEW_CLAIMED（引擎对超时认领放行跨 agent 回收），再重新认领审查。"
        )
        if result.get("message"):
            result[MESSAGE_KEY] = f"{result['message']} {takeover_msg}"
        else:
            result[MESSAGE_KEY] = takeover_msg
        if result.get("candidate") is None:
            result[NEXT_ACTION_KEY] = NEXT_ACTION_REVIEW_TAKEOVER

    # 无候选（candidate=None / next_action=exit|wait）：以引擎分配为准，
    # 附加 stop_wait 引导，明确"停止等待用户指令"，防止 agent 自行 claim/重试。
    # _attach_guidance 幂等（已有 guidance 不覆盖），此处预置即生效。
    if result.get("candidate") is None and result.get("next_action") in (
        NEXT_ACTION_EXIT, NEXT_ACTION_WAIT
    ):
        from orchd.guide import stop_wait_guidance

        result[GUIDANCE_KEY] = stop_wait_guidance()

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
                    MESSAGE_KEY: "agent 不得擅自使用 --auto-claim 连续领任务。仅在用户明确于 _master.json 顶层 config.allow_auto_claim 设为 true 后才允许。",
                    HINT_KEY: "请使用 python .orchd/__main__.py claim --task <id> --confirm 手动认领，或在 _master.json 顶层 config.allow_auto_claim 设为 true 后重试",
                    COMMAND_KEY: "python .orchd/__main__.py claim --task <id> --confirm",
                }
            ]
            _resp32 = structured_error("E032", "自动认领（--auto-claim）默认禁用", _details32, orchd_dir)
            result[ERROR_KEY] = _resp32.get("error")
            # Channel C: attach guidance at top-level as well (structured_error已含guidance)
            if "guidance" in _resp32:
                result[GUIDANCE_KEY] = _resp32[GUIDANCE_KEY]
            return result
    if getattr(args, "auto_claim", False) and result.get("candidate"):
        candidate_id = result["candidate"]["task_id"]
        shared = master.shared if hasattr(master, "shared") else None
        claim_result = claim(
            store, tasks, agent_id=agent_id, task_id=candidate_id,
            project_root=orchd_dir.parent, shared=shared,
            with_context=getattr(args, "with_context", False),
        )
        result["auto_claimed"] = True
        result[CLAIMED_KEY] = claim_result
    return result


def claim_preview(
    store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str = "implementer",
    project_root: Path | None = None,
    review_type: str | None = None,
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

    task_map = {t.get(ID_FIELD, ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E005, f"task '{task_id}' not found in master",
                         [{TASK_ID_KEY: task_id, HINT_KEY: f"任务 {task_id} 在 _master.json 中不存在，检查 id 拼写或注册"}])

    state = store.replay()
    derived = store.scan_task_derived()
    ts = state.get(task_id)
    status = ts.status if ts else STATUS_PENDING
    # review-unify-r2：unified 模式下审查阶段显示为 unified（单阶段），
    # 不再回落 spec；two_phase 模式保持 spec/code 展示。
    from orchd.ledger import resolve_review_mode
    if resolve_review_mode(store.orchd_dir) == "unified":
        current_phase = "unified"
    else:
        current_phase = (ts.review_phase if ts else None) or "spec"

    preview: dict[str, Any] = {
        "claim_type": role,
        TASK_ID_KEY: task_id,
        NAME_FIELD: task_def.get(NAME_FIELD, ""),
        BRIEF_FIELD: task_def.get(BRIEF_FIELD, ""),
        MODULE_FIELD: task_def.get(MODULE_FIELD, ""),
        DEPENDS_ON_FIELD: task_def.get(DEPENDS_ON_FIELD, []),
        REVIEWERS_FIELD: task_def.get(REVIEWERS_FIELD, []),
        "current_status": status,
        "review_phase": current_phase,
        # task-claim-preview-and-scope-triage：声明范围与校验命令前置展示
        FILES_TO_EDIT_FIELD: task_def.get(FILES_TO_EDIT_FIELD, []),
        EXEMPT_FILES_FIELD: task_def.get(EXEMPT_FILES_FIELD, []),
        ACCEPTANCE_CRITERIA_FIELD: task_def.get(ACCEPTANCE_CRITERIA_FIELD, []),
        VERIFY_COMMAND_FIELD: task_def.get(VERIFY_COMMAND_FIELD, ""),
        # cwd 预期：实现 claim 须在 main，审查 claim 须在 task 分支（两者规则相反）
        "cwd_expected": (
            "main（主工作树）" if role == "implementer"
            else f"{TASK_BRANCH_ROOT}{task_id} 或 "
                 f"{TASK_BRANCH_ROOT}{{line}}/{task_id}（任务 worktree）"
        ),
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
            BRANCH_KEY: ws.get("branch"),
            "clean": ws.get("clean"),
        }
    else:
        preview["git"] = {"available": False}

    # 将执行动作（claim() 会做的事）
    preview["actions"] = [
        f"写 {EVENT_REVIEW_CLAIMED if role == 'reviewer' else EVENT_CLAIMED} 事件到 ledger",
        f"创建/切换 {TASK_BRANCH_ROOT}{task_id} 分支",
    ]

    # 校验预期（与 claim() 锁内校验一致，透明展示；失败仅提示不阻断预览）
    # task-debug-claim-register-ux：E011 的两类在手判定对两个角色都生效，故在分支前
    # 统一计算，并复用 execute 的 _owns_task / _owns_review（session 优先，禁止另写副本）。
    from orchd.ledger import resolve_session_identity as _resolve_session
    from orchd.onboard.claim.execute import _owns_review, _owns_task

    session_id = _resolve_session(store.orchd_dir)["session_id"]
    busy_tasks = [
        tid for tid, t_state in state.items()
        if tid != task_id
        and t_state.status in (STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW)
        and _owns_task(t_state, agent_id, session_id)
    ]
    busy_reviews = [
        tid for tid, t_state in state.items()
        if tid != task_id
        and t_state.status == STATUS_IN_REVIEW
        and _owns_review(t_state, agent_id, session_id)
    ]
    if role == "reviewer":
        done_author, _ = _extract_last_done(store, task_id, derived)
        preview["done_by"] = done_author
        is_self = bool(done_author and done_author == agent_id)
        preview["expected_checks"] = [
            {"check": "任务处于 in_review（可认领审查）",
             "expected_pass": status == STATUS_IN_REVIEW},
            {"check": "agent 在任务 reviewers 名单内",
             "expected_pass": agent_id in task_def.get(REVIEWERS_FIELD, []),
             "note": "指纹身份（12位hex）默认豁免此名单校验（ledger.is_fingerprint_agent_id），expected_pass=false 不阻断认领；具名 agent 身份才需在 reviewers 名单内"},
            {"check": "审查阶段与当前 review_phase 匹配",
             "expected_pass": (not review_type) or review_type == current_phase},
        ]
        # 自审（E016）：无条件硬阻断（P0-1，task-review-hardblock-e1-claim-gate）。
        # 无 config 开关、无任务级覆盖、无降级提示项——审查必须由独立 subagent 承担。
        if is_self:
            preview["expected_checks"].append({
                "check": "自审阻断（E016：实现者 = 审查者，无条件）",
                "expected_pass": False,
                "note": "自审恒阻断：审查须由实现会话 spawn 的独立 subagent 执行，无降级档",
            })
        # E011 对审查认领同样生效（confirm 侧循环不分角色）——此前预览只在实现分支给条目。
        preview["expected_checks"].append({
            "check": "本 agent 无其它在手任务（E011）",
            "expected_pass": not busy_tasks,
            "note": ("在手任务：" + "、".join(busy_tasks)) if busy_tasks else "",
        })
        # review-busy 同样对两角色生效：reviewer 手里若已持另一张卡的审查认领，
        # confirm 侧会以 E011 review busy 拒绝（execute 的循环在 role 分支之外）。
        preview["expected_checks"].append({
            "check": "本 agent 无其它在手审查（E011 review-busy）",
            "expected_pass": not busy_reviews,
            "note": ("在手审查：" + "、".join(busy_reviews)) if busy_reviews else "",
        })
    else:
        preview["expected_checks"] = [
            {"check": "任务处于 pending（可认领）",
             "expected_pass": status == STATUS_PENDING},
            {"check": "依赖全部满足",
             "expected_pass": all(
                 (state.get(d).status if state.get(d) else STATUS_PENDING)
                 in TERMINAL_STATUSES for d in task_def.get(DEPENDS_ON_FIELD, [])
             )},
            {"check": "未被其他 agent 认领",
             "expected_pass": not (ts and ts.claimed_by and ts.claimed_by != agent_id)},
        ]
        # task-debug-claim-register-ux：预览与 --confirm 判据对齐（E010 / E041 / 冷却 /
        # E011），复用同一批只读谓词；E011 的两类在手判定已在分支前统一算好。
        from orchd.onboard.claim.precheck import _check_retract_cooldown
        from orchd.onboard.serial_source import serial_busy_siblings
        from orchd.pool import detect_file_conflict

        conflicts = detect_file_conflict(state, tasks, task_def)
        preview["expected_checks"].append({
            "check": "与在途任务声明文件不冲突（E010）",
            "expected_pass": not conflicts,
            "note": ("冲突任务：" + "、".join(c.task_id for c in conflicts)) if conflicts else "",
        })
        siblings = serial_busy_siblings(task_def, tasks, state)
        preview["expected_checks"].append({
            "check": "同一 source 无在途任务（E041）",
            "expected_pass": not siblings,
            "note": ("在途同源卡：" + "、".join(siblings)) if siblings else "",
        })
        cooldown = _check_retract_cooldown(store, task_id)
        preview["expected_checks"].append({
            "check": "不在 retract 认领冷却期",
            "expected_pass": cooldown is None,
            "note": ((f"{cooldown['seconds_ago']:.0f}s 前被撤回（冷却 300s；"
                      "--confirm 带 --force 可跳过）") if cooldown else ""),
        })
        preview["expected_checks"].append({
            "check": "本 agent 无其它在手任务（E011）",
            "expected_pass": not busy_tasks,
            "note": ("在手任务：" + "、".join(busy_tasks)) if busy_tasks else "",
        })
        preview["expected_checks"].append({
            "check": "本 agent 无其它在手审查（E011 review-busy）",
            "expected_pass": not busy_reviews,
            "note": ("在手审查：" + "、".join(busy_reviews)) if busy_reviews else "",
        })
        if project_root is not None:
            from orchd.worktree import actual_changes_conflict

            actual = actual_changes_conflict(project_root, state, tasks, task_def)
            preview["expected_checks"].append({
                "check": "任务分支实际改动不与他人在途冲突（E010）",
                "expected_pass": not actual,
                "note": ("冲突文件：" + "、".join(str(a) for a in actual)) if actual else "",
            })
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


def _claim_e033_with_task_context(
    exc: OrchdError, store, task_id: str,
) -> OrchdError:
    """claim 的 E033 补齐任务状态上下文（task_status/claimed_by/review_claimed_by）。

    E033 的抛出点在 ``require_session_runtime``，那里不知道 claim 目标；claim 命令
    捕获后补上任务当前状态，便于定位「会话失配时任务被谁占着 / 卡在哪个阶段」。
    """
    ts = store.replay().get(task_id)
    details = [dict(d) for d in (exc.details or [])]
    head = details[0] if details else {}
    head.update({
        TASK_ID_KEY: task_id,
        TASK_STATUS_KEY: getattr(ts, "status", None) or STATUS_PENDING,
        "claimed_by": getattr(ts, "claimed_by", None),
        "review_claimed_by": getattr(ts, "review_claimed_by", None),
    })
    hint = head.get(HINT_KEY)
    context = (
        f"当前任务 {task_id} 状态为 {head[TASK_STATUS_KEY]}"
        f"（claimed_by={head['claimed_by']}, review_claimed_by={head['review_claimed_by']}）"
    )
    head[HINT_KEY] = f"{hint}；{context}" if hint else context
    return OrchdError(exc.code, exc.message, [head, *details[1:]])


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
    from orchd.onboard.claim import claim
    from orchd.worktree import resolve_canonical_project_root

    shared = master.shared if hasattr(master, "shared") else None
    # canonical-project-root（2026-08-28 修复）：claim 的 project_root 与
    # pool/status/done 等读一致统一走 canonical。否则在容器根（其下残留
    # .orchd 时 _find_orchd_dir 会命中容器根）执行 claim，ensure_task_wt 的
    # _propagate_container_marker 会把任务 worktree 布局标记的 main_worktree
    # 写成容器根，导致任务 worktree 账本解析错位（done 报 not in claimed）。
    project_root = resolve_canonical_project_root(orchd_dir.parent)
    role = _detect_claim_role(store, tasks, args.task)
    try:
        agent_id = _require_agent_id(orchd_dir)
    except OrchdError as exc:
        if exc.code is ErrorCode.E033:
            raise _claim_e033_with_task_context(exc, store, args.task) from exc
        raise

    # 确认闸门：无 --confirm 仅输出预览（只读，不写事件、不建分支）
    if not getattr(args, "confirm", False):
        preview = claim_preview(
            store, tasks, agent_id=agent_id, task_id=args.task,
            role=role, project_root=project_root,
            review_type=getattr(args, "review_type", None),
        )
        result = {
            "confirm_required": True,
            "claim_type": preview["claim_type"],
            HINT_KEY: "预览模式：确认无误后请加 --confirm 真正执行认领（写事件 + 建分支）",
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
        force=getattr(args, "force", False),
    )
    # engine-discipline-hardening 轻 guidance（2026-10-23 停服施工）：同 source 串行包
    # 实现前复核提示（tier=suggest / exit_type=continue，不阻断、不新增错误码）。
    # 顶层 serial_hint 键——不预置 guidance dict，避免挡掉 skeleton 的标准 guidance。
    _src_of_task = next(
        (t.get(SOURCE_FIELD) for t in tasks if t.get(ID_FIELD) == args.task), None)
    if role != "reviewer" and _src_of_task:
        result["serial_hint"] = {
            HINT_KEY: "同 source 串行包：实现前请 amend 复核定义以当前现场为准（files_to_edit/AC 可能随前序兄弟卡落地而漂移）",
            "tier": "suggest",
            "exit_type": "continue",
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


def _done_deps_gate(
    project_root: Path, task_id: str, task_def: dict | None,
) -> None:
    """done 期依赖边复检（task-deps-hard-gate）：实际 diff 的未登记边阻断 done。

    口径与注册期同源（orchd.deps.undeclared_edges_for_files）；diff 取
    ``orchd.nogit.changed_paths``（git 优先、快照兜底），并叠加``pending_change_paths``
    中**已声明但未提交**的改动（done 的 auto-commit 在本门禁之后，只读已提交 diff
    会留下未提交窗口绕过路径）。

    仅引擎仓在本函数硬阻断（E042）；宿主仓的 deps 判定统一由宿主 guard
    （``orchd/guards/deps.py``，按 ``guards.json`` 的 ``warn``/``hard`` 档位与
    ``root``/``package`` 配置分流，``off``/无图跳过）在 done 链路接管，避免两套口径。
    引擎仓坏图（坏 JSON / 坏 schema）不再 fail-open：统一判定走
    ``decide_gate``，status=hard 时以 E007 + reason ``deps_graph_unavailable`` 阻断。
    """
    from orchd import deps as _deps
    from orchd.guards.deps import (
        GATE_HARD,
        GATE_OFF,
        GATE_SKIPPED,
        REASON_GRAPH_UNAVAILABLE,
        decide_gate,
    )
    from orchd.nogit import changed_paths
    from orchd.repo_role import is_engine_repo

    if not is_engine_repo(Path(project_root)):
        return

    decision = decide_gate(project_root)
    if decision.status in (GATE_SKIPPED, GATE_OFF):
        return
    if decision.status == GATE_HARD:
        raise OrchdError(
            ErrorCode.E007,
            f"deps_graph_unavailable: {decision.message}",
            [{
                "code": ErrorCode.E007.name,
                TASK_ID_KEY: task_id,
                "reason": REASON_GRAPH_UNAVAILABLE,
                "path": decision.policy.deps_rel,
                MESSAGE_KEY: decision.message,
                HINT_KEY: (
                    "修复 .orchd/shared/deps.json 后重试；引擎仓坏图必须 "
                    "fail-closed，不允许以告警放行"
                ),
            }],
        )
    graph = decision.graph
    if graph is None:  # GATE_OK 保证图非空；其余状态已在上面分流
        return
    declared = set((task_def or {}).get(FILES_TO_EDIT_FIELD) or []) | set(
        (task_def or {}).get(EXEMPT_FILES_FIELD) or [],
    )
    pending: set[str] = set()
    try:
        from orchd.worktree.diagnosis import pending_change_paths

        view = pending_change_paths(Path(project_root))
        pending = {
            p for p in view.get("tracked", set()) | view.get("untracked", set())
            if p in declared
        }
    except Exception:
        pending = set()
    changed = sorted(
        {
            p for p in changed_paths(Path(project_root), task_id)
            if p.endswith(".py")
        }
        | {p for p in pending if p.endswith(".py")}
    )
    missing = _deps.undeclared_edges_for_files(
        changed, project_root=Path(project_root), graph=graph,
        exclude=(task_def or {}).get(EXEMPT_FILES_FIELD) or (),
    )
    if not missing:
        return
    details = [
        {
            "code": ErrorCode.E042.name,
            TASK_ID_KEY: task_id,
            MODULE_FIELD: owner,
            COUNT_KEY: len(edges),
            "edges": [f"{owner} -> {edge}" for edge in edges],
            MESSAGE_KEY: f"模块 {owner} 有 {len(edges)} 条依赖边未登记",
        }
        for owner, edges in sorted(missing.items())
    ]
    raise OrchdError(
        ErrorCode.E042,
        "deps_edge_undeclared: done 期检测到本任务 diff 含未登记依赖边"
        "（done 已阻断，未提交）",
        details,
    )


def _attach_review_handoff(
    result: dict[str, Any],
    store: Any,
    orchd_dir: Path,
    task_def: dict[str, Any] | None,
    task_id: str,
    master: Any,
) -> None:
    """done 成功后挂 ``review_handoff``（交接失败只降级，不改 done 语义）。

    task-review-handoff-engine AC1 / AC6：done 既有字段与成功语义不变；审查会话
    mint、prompt 渲染的任何失败都收敛为结构化 ``error``，调用方（宿主）据
    ``error`` 字段 fail-closed，而不是让 done 变成失败。

    Args:
        result: done 响应（原地并入 ``review_handoff``）。
        store: 账本 Store（取最后一次 DONE 的 session_id 作 lineage 父）。
        orchd_dir: ``<项目>/.orchd``（mint 审查会话的 runtime 根）。
        task_def: 任务定义（取 files_to_edit 组审查清单）；None 时按空定义降级。
        task_id: 任务 id。
        master: 已加载的 master（取 shared 上下文；缺失按 None 处理）。

    Returns:
        None（副作用落在 ``result`` 上）。
    """
    if not result.get(DONE_KEY):
        return
    try:
        # import 也必须在 try 内：DONE 已落账，模块导入失败同样只能降级，
        # 不得把 done 翻成失败（AC6）。
        from orchd.review.handoff import build_review_handoff

        handoff = build_review_handoff(
            store,
            orchd_dir,
            task_def or {},
            task_id,
            project_root=orchd_dir.parent,
            review_created=result.get("review_created"),
            shared=getattr(master, "shared", None),
        )
    except Exception as exc:  # noqa: BLE001 —— done 已落账，交接异常一律降级
        handoff = {
            ERROR_KEY: {
                "code": "review_handoff_failed",
                "reason": "unexpected_exception",
                MESSAGE_KEY: f"{type(exc).__name__}: {exc}",
                HINT_KEY: "done 已成功；交接生成失败，请人工取号后再领审并核对 lineage",
            }
        }
    result["review_handoff"] = handoff


def _surface_summary_fallback(changes: str | None, task_def: dict[str, Any] | None) -> str:
    """``--changelog-summary`` 缺省摘要：取 ``--changes`` 首个非空行，其次任务名。"""
    for line in (changes or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return str((task_def or {}).get(NAME_FIELD, "") or "").strip()


def _resolve_surface_entries(
    args: Any,
    task_def: dict[str, Any] | None,
    changes: str | None,
    project_root: Path | None = None,
) -> list[dict[str, Any]] | None:
    """解析 done 期落地面片段入参（``--surface-entry`` JSON + ``--changelog-*`` 糖）。"""
    entries: list[dict[str, Any]] = []
    for raw in getattr(args, "surface_entry", None) or []:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OrchdError(
                ErrorCode.E007,
                f"--surface-entry 不是合法 JSON：{raw!r}",
                [{"detail": str(exc)}],
            ) from exc
        if not isinstance(payload, dict):
            raise OrchdError(
                ErrorCode.E007,
                "--surface-entry 必须是 JSON 对象",
                [{"detail": raw}],
            )
        entries.append(payload)
    category = getattr(args, "changelog_category", None)
    summary = getattr(args, "changelog_summary", None)
    if category is not None or summary is not None:
        if summary is None:
            # AC4「默认 summary 可安全截断」：只有派生摘要做截断，显式摘要不截断
            # （超长由 surfaces.prepare_entries → validate_fragment 报 E007）。
            summary = _surface_summary_fallback(changes, task_def)
            try:
                from orchd import surfaces as _surface_mod

                specs = _surface_mod.load_surfaces(project_root or Path("."))
                changelog_spec = specs.get("changelog")
                task_id = str(getattr(args, "task", "") or "")
                if changelog_spec is not None and task_id:
                    summary = _surface_mod.truncate_summary(
                        changelog_spec, task_id, summary
                    )
            except Exception:  # noqa: BLE001 - 截断失败不得阻断 done 参数解析
                pass
        entries.append(
            {
                "surface": "changelog",
                "category": category or "changed",
                SUMMARY_KEY: summary,
            }
        )
    return entries or None


def _allow_existing_surface_fragments(
    project_root: Path,
    task_def: dict[str, Any],
    task_id: str,
) -> None:
    """done 重试放行：把该任务已存在的片段路径并入 early 范围守卫的 exempt_files。"""
    try:
        from orchd import surfaces

        from orchd.gitops_ops import resolve_task_worktree

        root = resolve_task_worktree(project_root, task_id, strict=False) or project_root
        existing = surfaces.existing_fragment_rel_paths(root, task_id)
    except Exception:  # noqa: BLE001 - 片段域异常不得阻断 done 主流程
        return
    if not existing:
        return
    merged = list(dict.fromkeys([*(task_def.get(EXEMPT_FILES_FIELD, []) or []), *existing]))
    task_def[EXEMPT_FILES_FIELD] = merged


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
    _task_def_early = {t.get(ID_FIELD, ""): t for t in _tasks_early}.get(args.task)
    surface_entries = _resolve_surface_entries(
        args, _task_def_early, changes, orchd_dir.parent
    )
    if _task_def_early is not None:
        _allow_existing_surface_fragments(orchd_dir.parent, _task_def_early, args.task)
        from orchd.onboard import _guard_out_of_scope as _early_scope_guard
        _early_scope_guard(orchd_dir.parent, _task_def_early, args.task, [])
    # done 期 deps 复检（task-deps-hard-gate）：实际 diff 未登记边 → E042 阻断。
    _done_deps_gate(orchd_dir.parent, args.task, _task_def_early)
    result = done(
        store, tasks, agent_id=agent_id, task_id=args.task,
        changes_description=changes or "", concerns=args.concerns,
        project_root=orchd_dir.parent,
        skip_lesson_review=getattr(args, "skip_lesson_review", False),
        surface_entries=surface_entries,
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    # task-review-handoff-engine：done 成功即交接审查身份与 spawn prompt
    # （引擎 mint reviewer session，实现方无需再拼取号命令）。
    _attach_review_handoff(result, store, orchd_dir, _task_def_early, args.task, master)
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
    task_map = {t.get(ID_FIELD, ""): t for t in tasks}
    if task_map.get(args.task) is None:
        raise OrchdError(
            ErrorCode.E005,
            f"task '{args.task}' not found in master",
            [{TASK_ID_KEY: args.task, HINT_KEY: f"任务 {args.task} 在 _master.json 中不存在，检查 id 拼写或注册"}],
        )
    store = Store(orchd_dir)
    history = extract_review_history(store, args.task)
    return {TASK_ID_KEY: args.task, COMMENTS_KEY: history, COUNT_KEY: len(history)}


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
    task_map = {t.get(ID_FIELD, ""): t for t in tasks}
    if task_map.get(args.task) is None:
        raise OrchdError(
            ErrorCode.E005,
            f"task '{args.task}' not found in master",
            [{TASK_ID_KEY: args.task, HINT_KEY: f"任务 {args.task} 在 _master.json 中不存在，检查 id 拼写或注册"}],
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
        rework_scope=getattr(args, "rework_scope", None),
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("task_status") == STATUS_COMPLETED:
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
    p.add_argument("--phase", default=None,
                   help="指定阶段名（覆盖自动推进）；不在 tasks[] 的 phase 取值集合时返回结构化提示而非静默空候选")
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
    p.add_argument("--surface-entry", dest="surface_entry", action="append",
                   help="done 期写落地面片段（JSON：{surface,category,summary}；可重复）")
    p.add_argument("--changelog-category", dest="changelog_category",
                   help="CHANGELOG 片段分类（added/changed/deprecated/removed/fixed/security）")
    p.add_argument("--changelog-summary", dest="changelog_summary",
                   help="CHANGELOG 片段摘要（缺省取 --changes 首行，其次任务名）")
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
    p.add_argument("--rework-scope", choices=["spec", "code"], default=None,
                   help="打回范围分类（仅 code 阶段 CHANGES_REQUESTED 可用：code=仅实现问题，返工直达 code；缺省 spec=全退重走）")
    p.add_argument("--show", action="store_true",
                   help="只读回看该任务全部历史审查意见（与 --verdict 互斥，不写事件）")
    p.set_defaults(func=_cmd_review)

