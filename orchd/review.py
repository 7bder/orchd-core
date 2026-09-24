"""Orchd review 子域：审查申请、审查意见提取与审查提交。

将与审查（review）相关的辅助函数与主流程从 onboard.py 外置，
保持 onboard.py 只保留生命周期主干（bootstrap / request / claim /
done / retract / force_status）。

依赖方向：本模块不导入 onboard.py，避免循环依赖。共享辅助
（make_event / guard_write_command）自 orchd.gitops_ops 导入，
低级钩子（get_head_commit / session_lock_*）自 orchd.gitops 导入。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
# task-14-git-policy-layer：判定类逻辑（checkout_default_strict / guard_review_write）
# 已收敛到 orchd.gitops（专用 git 判定模块）；共享辅助 make_event 仍自
# orchd.gitops_ops 导入。
from orchd.gitops import (
    checkout_default_strict,
    get_head_commit,
    guard_review_write,
    hook_uninstall,
    main_worktree_root,
    release_session_lock_if_owned,
    session_lock_check,
    session_lock_release,
)
from orchd.gitops_ops import (
    make_event,
    try_auto_resolve_conflict,
    try_delete_task_branch,
    try_git_merge,
)
from orchd.guide import NEXT_ACTION_EXIT, read_for
from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for
from orchd.ledger import (
    Store,
    TaskDerived,
    TaskState,
    # task-fp-identity-single-source：指纹判定单一事实源（本子域不导入 onboard，
    # 统一从 ledger 导入，消除私有副本的同步漂移风险）
    is_fingerprint_agent_id as _is_fingerprint_agent_id,
    resolve_review_mode,
    resolve_store_dir,
)


def is_self_review_author(
    done_event: dict[str, Any] | None,
    agent_id: str,
    session_id: str | None,
) -> bool:
    """判定「本次审查/认领是否为自审」：实现者（DONE 作者）与审查者是否同一身份。

    判定口径为全仓单一事实源（v4，2026-09-15 停服升级时，由 claim 与
    select_review_candidate 的历史私有副本收敛于此，三段判定完全等价）：

    - DONE 事件与当前 ``session_id`` 均已知 → 要求 session 与 agent 同时匹配；
    - 否则退化为仅比较 ``agent_id``（旧数据 / 无 session 环境）。

    自审事实的落账由调用方完成：REVIEW_CLAIMED / REVIEW_SUBMITTED 事件写入
    ``is_self_review`` 字段（派生为 TaskState.review_self_review），使事后审计
    直接读账本即可认定，不再依赖 ``DONE.agent_id == REVIEW_CLAIMED.agent_id``
    的启发式推导（该推导在 session 维度不可靠）。
    """
    if not done_event:
        return False
    done_author = done_event.get("agent_id")
    done_session = done_event.get("session_id")
    if not done_author:
        return False
    if done_session and session_id:
        return done_session == session_id and done_author == agent_id
    return done_author == agent_id


def _recent_transitions(
    store: Store, task_id: str, limit: int = 5, derived: TaskDerived | None = None,
) -> list[dict[str, Any]]:
    """提取任务最近 N 条状态变迁事件（P0-9 E007 信息增强）。

    从 ledger 中提取该任务最近的 type/agent_id/timestamp，用于 E007 报错时
    展示状态迁移轨迹，帮助用户定位"为什么任务不在预期状态"。
    """
    events = store._read_ledger_lines(from_line=1)
    transitions = [
        {
            "type": ev.get("type"),
            "agent_id": ev.get("agent_id"),
            "timestamp": ev.get("timestamp"),
        }
        for ev in events
        if ev.get("task_id") == task_id and ev.get("type")
    ]
    return transitions[-limit:]


def _task_branch_tip(project_root: Path, task_id: str) -> str | None:
    """best-effort 取 ``task/{task_id}`` 分支 tip SHA（task-merge-warning-resolve-sha）。

    供 merge_warning 事件附加 ``resolve_sha``：audit-merge 以此判定 main 是否
    已含实现（手工补 merge 落地后自动销账）。git 不可用 / 分支不存在 / 异常
    → None（省略字段，行为向后兼容）。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", resolve_task_branch_for(project_root, task_id)],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def _wt_dir_name(task_id: str) -> str:
    """任务 worktree 目录名（单一来源：``orchd.worktree.worktree_hint``）。

    AC4（task-review-diagnostics-hardening）：与 ``orchd/gitops/guard.py`` 的
    E018 hint 同源，均经 ``worktree_hint(task_id)``（内部使用 ``_task_wt_name``）
    输出真实目录名，杜绝各处自行拼 ``task-{task_id}`` 产生 ``task-task-<id>``
    双前缀。引擎不可用时回退等价实现（best-effort）。
    """
    try:
        from orchd.worktree import worktree_hint

        return worktree_hint(task_id)
    except Exception:
        short = task_id[5:] if task_id.startswith("task-") else task_id
        return f"task-{short}"


def _merge_diagnostic_action(task_id: str, diag: dict[str, Any]) -> str:
    """merge 非冲突失败的可执行指引（task-merge-failure-diagnostic）。

    ``diag`` 为 :func:`orchd.gitops_ops.try_git_merge` 的 ``merge_diagnostic``
    （stage/stderr 摘录/kind/files/hint）。保持任务 in_review 不合并；结尾附
    通用放弃通道（与 merge_env_error / merge_conflict 同形）。
    """
    kind = (diag or {}).get("kind", "unknown")
    hint = (diag or {}).get("hint", "")
    excerpt = (diag or {}).get("stderr_excerpt", "")
    head = (
        "git merge 未执行（非冲突失败，诊断已透出）：任务保持 in_review，"
        "未标记完成、未回收任务 worktree。"
    )
    body = f"【诊断】{hint}" if hint else ""
    if excerpt and kind == "unknown":
        body += f"\n【git 原文】{excerpt[:300]}"
    return (
        f"{head}\n{body}\n"
        "【修复步骤】\n"
        "  1. 按上方诊断处置\n"
        "  2. 由同一 reviewer 重试 code APPROVED\n"
        "【放弃本次审查】\n"
        f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason 'merge失败放弃'\n"
        f"  orchd force-status --task {task_id} --status pending --reason 'merge失败回退'"
    )


def _own_merge_residual(
    audit_root: Path | None,
    task_id: str,
    tip_sha: str | None,
    worktree_recycled: dict[str, Any] | None,
    branch_deleted: bool | None,
) -> list[dict[str, Any]]:
    """窄口径自家残留检查（task-merge-audit-inline，AC2）。

    只查**本任务** merge 成功后应已清理的三项；无关告警（幽灵分支 / 他任务
    残留 / cancelled 遗留）由 :func:`orchd.report.merge_audit` 全量巡检注解，
    不进入本函数（AC3：注解不拦）。

    检查项（均为"本应已清理却仍存在"= 真残留才命中）：
      1. ``worktree_not_recycled``：``worktree_recycled.removed is False``
         （回收函数已区分"无可回收"→removed=True，故 False 即真残留）；
      2. ``branch_not_cleaned``：分支删除未报告成功 **且** 分支实际仍存在
         （``git show-ref`` 验证；删除报 False 但分支已无 = 幂等口径差异，
         属良性，不拦）；
      3. ``branch_not_merged_into_main``：分支 tip 已知 **且** main 不包含它
         （``git merge-base --is-ancestor``；tip 未知时无法判定，不拦）。

    只读 git，永不抛异常（探针失败返回已确认项，不阻断完成路径）。
    """
    residual: list[dict[str, Any]] = []
    wr = worktree_recycled or {}
    if wr.get("removed") is False:
        res = wr.get("residual") or {}
        residual.append({
            "task_id": task_id,
            "reason": "worktree_not_recycled",
            "detail": res.get("reason") or wr.get("reason") or "worktree 回收未成功",
        })
    if audit_root is not None:
        try:
            root = str(audit_root)
            branch = resolve_task_branch_for(audit_root, task_id)
            trunk = resolve_trunk_for(audit_root)
            if branch_deleted is not True:
                show = subprocess.run(
                    ["git", "-C", root, "show-ref", "--verify",
                     f"refs/heads/{branch}"],
                    capture_output=True, timeout=10,
                )
                if show.returncode == 0:
                    residual.append({
                        "task_id": task_id,
                        "reason": "branch_not_cleaned",
                        "detail": f"{branch} 分支仍存在（分支删除未成功）",
                    })
            if tip_sha:
                anc = subprocess.run(
                    ["git", "-C", root, "merge-base", "--is-ancestor",
                     tip_sha, trunk],
                    capture_output=True, timeout=10,
                )
                if anc.returncode != 0:
                    residual.append({
                        "task_id": task_id,
                        "reason": "branch_not_merged_into_main",
                        "detail": f"{branch} tip {tip_sha[:7]} 未被 {trunk} 包含",
                    })
        except Exception:
            pass
    return residual


def _own_residual_action(
    task_id: str, own: list[dict[str, Any]], audit_root: Path | None
) -> str:
    """自家残留的可执行指引（task-merge-audit-inline，AC2）。

    与 merge_conflict / merge_env_error 指引同形：修复步骤 + 同一 reviewer
    重试 + 通用放弃通道。
    """
    lines = [
        "merge 已落地但本任务残留未清理：任务已退回 in_review，未标记完成。",
        "【残留】",
    ]
    for entry in own:
        reason = entry.get("reason", "unknown")
        detail = entry.get("detail", "")
        if reason == "worktree_not_recycled":
            fix = (
                "先切出任务 worktree 目录后重试回收，或运行 doctor --fix "
                "清理残留；不要在已失效的 worktree 目录内执行命令"
            )
        elif reason == "branch_not_cleaned":
            fix = (
                f"在主工作树执行 git branch -d task/{task_id}（root={audit_root}），"
                "失败则运行 doctor --fix"
            )
        elif reason == "branch_not_merged_into_main":
            fix = (
                "核对 main 是否包含本次实现（git log main 查找任务提交）；"
                "缺失则进入任务 worktree 执行 orchd git merge main 确认后重试"
            )
        else:
            fix = "按残留详情人工处置"
        lines.append(f"  - {reason}：{detail}。{fix}")
    lines += [
        "【修复步骤】",
        "  1. 按上方残留逐项处置",
        "  2. 由同一 reviewer 重试 code APPROVED",
        "【放弃本次审查】",
        f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason '自家残留放弃'\n"
        f"  orchd force-status --task {task_id} --status pending --reason '自家残留回退'",
    ]
    return "\n".join(lines)


def request_reviewer(
    store: Store,
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    agent_id: str,
    derived: TaskDerived | None = None,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    """查找处于 in_review 且未被审查者 claim 的任务。

    排序规则：spec 阶段的审查优先于 code 阶段。同等阶段内按 ledger 遍历顺序排列。
    调用方（onboard.request）在 reviewer 角色下转发到此函数。

    self-review（实现者 == 审查者）：默认仅标注 ``is_self_review`` 并照常进入
    候选；``enforce_self_review_block=True``（线上版）时排除（AC1）。
    """
    review_candidates: list[dict[str, Any]] = []
    not_in_list: list[dict[str, Any]] = []
    task_map = {t.get("id", ""): t for t in tasks}
    for tid, ts in state.items():
        if ts.status == "in_review" and ts.review_claimed_by is None:
            task_def = task_map.get(tid, {})
            # 向后兼容：reviewers 名单存在且非空时不再名单内 → not_in_list；
            # 字段缺失/为空（指纹身份模型）则跳过名单门禁，仅按实现指纹去重。
            # 指纹豁免（task-fp-review-priority-exempt，对齐 claim 侧 E007）：
            # 指纹形态 agent_id（12 位 hex）不在名单内也正常进入候选（不落入
            # not_in_list）；具名 agent（名单外）仍记 not_in_list（向后兼容）。
            designated = task_def.get("reviewers")
            if designated and agent_id not in designated \
                    and not _is_fingerprint_agent_id(agent_id):
                not_in_list.append({
                    "task_id": tid,
                    "review_phase": ts.review_phase or "spec",
                    "reviewers": task_def.get("reviewers", []),
                })
                continue
            # self-review：DONE 实现指纹 == 当前 reviewer 指纹。
            # 默认仅标注照常分配；enforce=True 时排除（AC1）。
            # v4（2026-09-15 停服升级）：判定收敛到 is_self_review_author（单一事实源），
            # 与 claim / review_submit 的落账口径严格一致，消除私有副本漂移。
            from orchd.ledger import resolve_session_identity
            current_session = resolve_session_identity(store.orchd_dir)["session_id"]
            is_self = is_self_review_author(
                find_last_done_event(store, tid, derived), agent_id, current_session
            )
            if is_self and enforce_self_review_block:
                continue
            entry = {
                "task_id": tid,
                "task": task_def,
                "review_phase": ts.review_phase,
            }
            if is_self:
                entry["is_self_review"] = True
            review_candidates.append(entry)
    review_candidates.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    if not review_candidates:
        if not_in_list:
            return {
                "candidate": None,
                "message": f"有 {len(not_in_list)} 个待审查任务但你不在名单内",
                "next_action": NEXT_ACTION_EXIT,
                "pool_size": 0,
                "reason": "not_in_reviewer_list",
                "tasks": not_in_list,
            }
        return {
            "candidate": None,
            "message": "当前无待审查任务",
            "next_action": NEXT_ACTION_EXIT,
            "pool_size": 0,
        }
    best = review_candidates[0]
    task_def = best["task"]
    task_id = best["task_id"]
    author, changes_description = extract_last_done(store, task_id, derived)

    candidate: dict[str, Any] = {
        "task_id": task_id,
        # review-unify-r2：unified 单阶段（review_phase 为 None）展示为 unified。
        "review_type": best["review_phase"] or "unified",
        "module": task_def.get("module", ""),
        "brief": task_def.get("brief", ""),
        "importance": task_def.get("importance", "normal"),
        "files_to_review": [
            {"path": p, "priority": "must_read"}
            for p in task_def.get("files_to_edit", [])
        ],
        "acceptance_criteria": task_def.get("acceptance_criteria", []),
        "changes_description": changes_description,
    }
    if author:
        candidate["author"] = author
    if best.get("is_self_review"):
        candidate["is_self_review"] = True

    return {
        "candidate": candidate,
        "pool_size": len(review_candidates),
        "prompt": f"确认将此审查分配给 {agent_id}？(执行 / 跳过 / 重新声明能力)",
        "warnings": [],
    }


def extract_review_comments(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> list[str]:
    """从 ledger 中提取该任务的所有审查意见（REVIEW_SUBMITTED 的 comments）。

    传入 ``derived`` 时直接从缓存读取（O(1)），否则全扫描 ledger。
    """
    if derived is not None:
        return derived.review_comments.get(task_id, [])
    comments: list[str] = []
    if not store.ledger_exists():
        return comments
    events = store._read_ledger_lines(from_line=1)
    for ev in events:
        if ev.get("task_id") != task_id or ev.get("type") != "REVIEW_SUBMITTED":
            continue
        if ev.get("comments"):
            comments.append(ev["comments"])
        # task-review-comments-gate-and-stale-timeout（B）：历史空打回事件
        # （CHANGES_REQUESTED 但无 comments）注入占位，避免返工时 review_comments=[]
        # 导致实现者看不到任何意见。A 的强制门已阻止新空打回，此处仅兜底历史数据。
        elif ev.get("verdict") == "CHANGES_REQUESTED":
            comments.append(
                "[该次打回未附审查意见（历史数据），请联系审查者补充；"
                "当前版本已强制要求 CHANGES_REQUESTED 必须附意见]"
            )
    return comments


def extract_review_history(store: Store, task_id: str) -> list[dict[str, Any]]:
    """该任务全部 REVIEW_SUBMITTED 的结构化意见（只读回看，task-review-comments-readback）。

    每项 {"review_type", "verdict", "timestamp", "comments"}，与 claim 侧
    extract_review_comments 同源扫描（空打回历史占位口径一致）。
    只读：不写事件、不改任务状态；completed（含归档）任务同样可读。
    """
    history: list[dict[str, Any]] = []
    if not store.ledger_exists():
        return history
    events = store._read_ledger_lines(from_line=1)
    for ev in events:
        if ev.get("task_id") != task_id or ev.get("type") != "REVIEW_SUBMITTED":
            continue
        if ev.get("comments"):
            body = ev["comments"]
        # 空打回历史占位口径与 extract_review_comments 一致（此处只读呈现）。
        elif ev.get("verdict") == "CHANGES_REQUESTED":
            body = (
                "[该次打回未附审查意见（历史数据），请联系审查者补充；"
                "当前版本已强制要求 CHANGES_REQUESTED 必须附意见]"
            )
        else:
            continue
        history.append({
            "review_type": ev.get("review_type"),
            "verdict": ev.get("verdict"),
            "timestamp": ev.get("timestamp"),
            "comments": body,
        })
    return history


def extract_last_done(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> tuple[str | None, str | None]:
    """从 ledger 中提取该任务最近一次 DONE 事件的 (agent_id, changes_description)。

    多轮返工时返回最近一轮的作者与变更描述。
    """
    if derived is not None:
        ev = derived.last_done.get(task_id)
        if ev:
            return ev.get("agent_id"), ev.get("changes_description")
        return None, None
    if not store.ledger_exists():
        return None, None
    events = store._read_ledger_lines(from_line=1)
    author: str | None = None
    changes: str | None = None
    for ev in events:
        if ev.get("task_id") == task_id and ev.get("type") == "DONE":
            author = ev.get("agent_id")
            changes = ev.get("changes_description")
    return author, changes


def find_last_done_event(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> dict[str, Any] | None:
    """从 ledger 中查找该任务最近一次 DONE 事件（含 timestamp/agent_id）。

    用于 done 的"假失败消除"：verify 失败但 ledger 已写 DONE 时，
    说明 DONE 已实际落地，应返回成功语义而非 E014。
    """
    if derived is not None:
        return derived.last_done.get(task_id)
    if not store.ledger_exists():
        return None
    events = store._read_ledger_lines(from_line=1)
    last: dict[str, Any] | None = None
    for ev in events:
        if ev.get("task_id") == task_id and ev.get("type") == "DONE":
            last = ev
    return last


def extract_review_baseline(
    store: Store, task_id: str, agent_id: str, derived: TaskDerived | None = None
) -> str | None:
    """从最近的 REVIEW_CLAIMED 事件提取 baseline_sha（用于漂移检测）。"""
    if derived is not None:
        return derived.review_baselines.get((task_id, agent_id))
    try:
        events = store._read_ledger_lines(from_line=1)
    except Exception:
        return None
    for event in reversed(events):
        if (
            event.get("type") == "REVIEW_CLAIMED"
            and event.get("task_id") == task_id
            and event.get("agent_id") == agent_id
        ):
            return event.get("baseline_sha")
    return None


def review_submit(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
    authorize_reviewer_resolve: bool = False,
    rework_scope: str | None = None,
) -> dict[str, Any]:
    """提交审查结果（task-session-lock-lifecycle：异常路径也保证释放会话锁）。

    review_type 为 None 表示 unified 单阶段审查（review-unify-r2）。

    ``_review_submit_impl`` 的包装：``finally`` 中经 :func:`release_session_lock_if_owned`
    条件释放本 agent 的 session 锁（仅持有者==本 agent 才释放，幂等）。正常路径
    由 ``_review_submit_impl`` 尾部释放并写 ``session_lock_released``；异常/提前
    返回路径由本包装器的 finally 兜底，杜绝漏放锁（此前需 60min 超时 + watchdog 兜底）。
    """
    # task-guide-routing-meta：防御性重入路由（库调用无 main() 启动初始化时）。
    # .orchd 存在但 rules/ 缺失 → 跳过（保留既有缓存）；其余失败静默（响应路径
    # 不因子虚乌有而崩，缺失路由由 read_for 的 fail-closed 在使用点报错）。
    try:
        from orchd.guide import init_routing

        _rr_orchd = Path(project_root) / ".orchd" if project_root else None
        if _rr_orchd is not None and _rr_orchd.is_dir():
            init_routing(_rr_orchd, project_root)
    except Exception:
        pass
    try:
        return _review_submit_impl(
            store, tasks, agent_id, task_id, review_type, verdict, comments,
            project_root, authorize_reviewer_resolve=authorize_reviewer_resolve,
            rework_scope=rework_scope,
        )
    finally:
        if project_root:
            release_session_lock_if_owned(project_root / ".orchd", agent_id)


def _resolve_review_resolve_markers(workdir: Path, conflict_files: list[str]) -> bool:
    """[fallback] 审查者授权解冲突：只解 REVIEW-RESOLVE 标记段，保留双方内容。

    task-merge-tests-union-and-reviewer-fallback：仅在显式
    authorize_reviewer_resolve=True 时由调用方触发。逐文件扫描
    ``<<<<<<< REVIEW-RESOLVE`` 标记段，去掉标记符、保留双方内容（Both sides
    preserved），不改业务逻辑。只 git add 冲突文件并提交，不触碰其他文件。

    Returns:
        True 表示全部标记段已解并提交；False 表示失败（调用方回退 E015）。
    """
    import re as _re

    marker_start = _re.compile(r"^<<<<<<< REVIEW-RESOLVE\s*$", _re.MULTILINE)
    marker_sep = _re.compile(r"^=======\s*$", _re.MULTILINE)
    marker_end = _re.compile(r"^>>>>>>>.*$", _re.MULTILINE)

    for rel in conflict_files:
        target = workdir / rel
        if not target.exists():
            return False
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        # 逐段替换 REVIEW-RESOLVE 标记
        pos = 0
        result_parts: list[str] = []
        while True:
            m_start = marker_start.search(text, pos)
            if not m_start:
                result_parts.append(text[pos:])
                break
            result_parts.append(text[pos:m_start.start()])
            m_sep = marker_sep.search(text, m_start.end())
            if not m_sep:
                return False  # 标记不完整，拒绝
            m_end = marker_end.search(text, m_sep.end())
            if not m_end:
                return False
            # 保留双方内容（去掉标记符），中间加空行分隔
            left = text[m_start.end():m_sep.start()].strip("\n")
            right = text[m_sep.end():m_end.start()].strip("\n")
            result_parts.append(f"{left}\n\n{right}\n")
            pos = m_end.end()
        merged = "".join(result_parts)
        # 确认无残留冲突标记
        if "<<<<<<<" in merged or ">>>>>>>" in merged:
            return False
        target.write_text(merged, encoding="utf-8")
        add = subprocess.run(
            ["git", "-C", str(workdir), "add", rel],
            capture_output=True,
        )
        if add.returncode != 0:
            return False
    # 只提交冲突文件
    commit = subprocess.run(
        ["git", "-C", str(workdir), "commit", "-q",
         "-m", "chore(merge): reviewer-authorized resolve — Both sides preserved"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return commit.returncode == 0


def _review_submit_impl(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
    *,
    authorize_reviewer_resolve: bool = False,
    rework_scope: str | None = None,
) -> dict[str, Any]:
    """提交审查结果（APPROVED 或 CHANGES_REQUESTED）。

    review_type 为 None 表示 unified 单阶段审查（review-unify-r2）。

    锁内校验：任务必须处于 in_review 状态，审查阶段（spec/code）须匹配，
    且审查者须为当前 agent。

    spec APPROVED 自动推进到 code review；code APPROVED 先锁外 git merge，
    成功才写完成事件（任务 completed），merge 冲突则停留 in_review。
    CHANGES_REQUESTED 时任务回退 pending。
    """
    # 意图化守卫（task-14-git-policy-layer）：任意分支、不要求干净。
    # AC4（task-session-lock-degrade-observability）：此前该调用点是四个意图化封装
    # （claim / done / clean / review）中唯一**未透传 degraded** 的——会话锁降级条目
    # 只生成、不入响应，review 路径的「本次未经会话锁保护」不可见。此处补齐接线。
    degraded: list[dict[str, Any]] = []
    guard_review_write(
        project_root,
        orchd_dir=store.orchd_dir,
        agent_id=agent_id,
        degraded=degraded,
    )
    # task-14-worktree-lifecycle（AC2）：目标 root == 任务 worktree（不一致 E018，
    # 防错目录审查）。flat 单会话绑定=主工作树 → 恒通过；无绑定 → best-effort 跳过。
    if project_root:
        from orchd.worktree import guard_task_root

        guard_task_root(project_root, resolve_store_dir(store.orchd_dir), task_id, "review")

    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        derived = store.scan_task_derived()
        ts = state.get(task_id)

        # P0-9：预计算最近状态变迁（仅 E007 路径使用，happy path 不触发开销）
        def _err_transitions() -> list[dict[str, Any]]:
            return _recent_transitions(store, task_id, derived=derived)

        if not ts or ts.status != "in_review":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: task '{task_id}' not in_review",
                [{"task_id": task_id, "actual": ts.status if ts else "pending",
                  "recent_transitions": _err_transitions(),
                  "hint": f"任务当前状态为 {ts.status if ts else 'pending'}（非 in_review），"
                          f"可能尚未 done 或已被审查打回 pending"}],
            )
        if ts.review_phase != review_type:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review phase mismatch (expected '{ts.review_phase}', got '{review_type}')",
                [{"task_id": task_id, "expected_phase": ts.review_phase,
                  "got_phase": review_type,
                  "recent_transitions": _err_transitions(),
                  "hint": f"审查阶段不匹配：任务处于 {ts.review_phase} 阶段，"
                          f"你提交的是 {review_type}，请确认审查类型"}],
            )
        from orchd.ledger import resolve_session_identity
        current_session = resolve_session_identity(store.orchd_dir)["session_id"]
        if ts.review_claimed_session and current_session:
            claimed_other = not (
                ts.review_claimed_session == current_session
                and ts.review_claimed_by == agent_id
            )
        else:
            claimed_other = ts.review_claimed_by != agent_id
        if claimed_other:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review not claimed by this session "
                f"(claimed_by='{ts.review_claimed_by}', claimed_session='{ts.review_claimed_session}')",
                [{"task_id": task_id, "claimed_by": ts.review_claimed_by,
                  "claimed_session": ts.review_claimed_session, "agent": agent_id,
                  "current_session": current_session,
                  "recent_transitions": _err_transitions(),
                  "hint": "审查已被其他 session 认领或当前 session 不匹配；"
                          "可用 orchd retract --task <id> --type REVIEW_CLAIMED 释放后重新认领"}],
            )

        baseline_sha = extract_review_baseline(store, task_id, agent_id, derived)
        # AC1（task-review-baseline-and-worktree-recycle-fix）：current_sha 与
        # REVIEW_CLAIMED 的 baseline_sha 必须同源。原取 get_head_commit(project_root)
        # （主工作树 HEAD），与基线（任务分支 tip）比对必然不等 → 恒误报漂移。
        current_sha = (
            _task_branch_tip(project_root, task_id) or get_head_commit(project_root)
        ) if project_root else None
        baseline_drift = bool(baseline_sha and current_sha and baseline_sha != current_sha)

        # task-review-comments-gate-and-stale-timeout（A）：CHANGES_REQUESTED
        # 必须附意见，否则返工任务取到 review_comments=[]，实现者看不到打回原因。
        # APPROVED / REVIEW_READY 不受影响（通过无需意见）。空白字符串视同空。
        if verdict == "CHANGES_REQUESTED" and not (comments or "").strip():
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: CHANGES_REQUESTED requires non-empty review comments",
                [{"task_id": task_id, "verdict": verdict,
                  "hint": "打回审查必须附上具体修改意见（comments 不能为空或仅空白），"
                          "否则实现者返工后无法获知需要修复什么"}],
            )

        # v4（2026-09-15 停服升级）：自审事实落账——提交审查时把「实现者 == 审查者」
        # 写入事件（与 REVIEW_CLAIMED 同口径、同一判定函数），TaskState.review_self_review
        # 随之派生；事后审计直接读账本，不必再用 DONE.agent_id == REVIEW_CLAIMED.agent_id
        # 的启发式推导。
        is_self_review = is_self_review_author(
            derived.last_done.get(task_id), agent_id, current_session
        )
        # task-review-rework-scope：打回范围分类。rework_scope 仅伴随
        # CHANGES_REQUESTED，且仅 code 阶段打回可声明 code（spec 打回重走
        # spec 是唯一语义）；APPROVED / 非 code 阶段携带即 E007，防误用。
        if rework_scope is not None:
            if verdict != "CHANGES_REQUESTED" or review_type != "code":
                raise OrchdError(
                    ErrorCode.E007,
                    "invalid_rework_scope: rework_scope 仅用于 code 阶段的 CHANGES_REQUESTED",
                    [{"task_id": task_id, "verdict": verdict,
                      "review_type": review_type,
                      "hint": "仅 code 审查打回实现问题时可附 --rework-scope code"
                              "（返工后直达 code）；其余情形不得携带"}],
                )
            if rework_scope not in ("spec", "code"):
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_rework_scope: {rework_scope!r} 非法",
                    [{"task_id": task_id,
                      "hint": "rework_scope 仅支持 spec（默认，全退重走）/ code"
                              "（仅实现问题，返工直达 code）"}],
                )
        event = make_event(
            task_id, agent_id, "REVIEW_SUBMITTED",
            verdict=verdict,
            is_self_review=is_self_review,
        )
        if rework_scope is not None:
            event["rework_scope"] = rework_scope
        # review-unify-r2：unified 单阶段（review_type 为 None）不写 review_type
        # 字段（R2-b：新事件无 review_type）；two_phase 保留 spec/code 供 replay
        # 按两阶段语义解释，与老事件兼容。
        if review_type is not None:
            event["review_type"] = review_type
        if comments:
            event["comments"] = comments

        result: dict[str, Any] = {
            "submitted": True,
            "task_id": task_id,
            "review_type": review_type,
            "verdict": verdict,
        }
        if is_self_review:
            # 与 claim 的 self_review_notice 呼应：非阻断提示，便于调用方即时知情
            result["is_self_review"] = True
        if integrity_warnings:
            result["integrity_warnings"] = integrity_warnings

        if baseline_drift:
            # AC1（task-review-diagnostics-hardening）：baseline_warning 结构化，
            # 以稳定契约对象承载漂移信息（code / baseline_sha / current_sha /
            # severity / message），字段级可断言，避免 container 布局下告警长期
            # 被当作裸字符串噪音忽略。无漂移时不产出该字段。
            result["baseline_warning"] = {
                "code": "baseline_drift",
                "baseline_sha": baseline_sha,
                "current_sha": current_sha,
                "severity": "warning",
                "message": (
                    f"task branch HEAD changed during review "
                    f"(claimed at {baseline_sha[:7]}, now {current_sha[:7]}). "
                    f"Review may be based on outdated code."
                ),
            }

        pending_code_event: dict[str, Any] | None = None

        if verdict == "APPROVED" and review_type == "spec":
            store.append_event(event)
            code_ready = make_event(task_id, agent_id, "REVIEW_READY", review_type="code")
            store.append_event(code_ready)
            result["task_status"] = "in_review"
            result["next_review"] = "code"
            new_state = store.replay()
            store.update_checkpoint(new_state)

        elif verdict == "APPROVED" and (review_type == "code" or review_type is None):
            # review-unify-r2：unified 单阶段（review_type 为 None）与 code 终审
            # 一样走 merge → completed；two_phase 的 spec APPROVED 走上一分支。
            result["task_status"] = "in_review"
            pending_code_event = event

        elif verdict == "CHANGES_REQUESTED":
            # 打回前强约束切回默认分支(main/master)，避免工作区滞留 task/{id} 分支、
            # 后接 agent 认领时报 E018。切换失败抛 E018/E017 → 不写事件、任务仍
            # in_review、审查 claim 保留，reviewer 处理后重试即可（与 done 强约束一致）。
            if project_root:
                result["checked_out_main"] = checkout_default_strict(
                    project_root, command="review"
                )
            store.append_event(event)
            result["task_status"] = "pending"
            result["back_to_pool"] = True
            new_state = store.replay()
            store.update_checkpoint(new_state)

        else:
            # P2-5：未知 verdict / 不支持 review_type 组合不得静默无操作，
            # 显式报错，避免调用方拿到无 task_status 的「成功」结果。
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_verdict: unsupported verdict={verdict!r} review_type={review_type!r}",
                [{"task_id": task_id, "verdict": verdict, "review_type": review_type,
                  "hint": "verdict 仅支持 APPROVED / CHANGES_REQUESTED"}],
            )
    finally:
        store.release_lock()

    if pending_code_event is not None:
        # task-engine-review-merge-diff-gate：code APPROVED 前校验声明文件已全部
        # 进入任务分支 diff；缺失则拒绝 merge，避免“实现/测试未进分支”被终审放行。
        # task-master-single-copy：与 done 门禁同源改用 diagnose_missing_branch_files
        # （区分“漏提交”与“声明但未改动”），声明未改动的冗余文件不再误拦 merge。
        if project_root:
            try:
                # task-flat-decl-authority：声明经 resolve_declaration_source 解析
                # （flat 下从 main blob 读权威声明，防任务分支本地副本陈旧）。
                from orchd.worktree import resolve_declaration_source
                _tasks = resolve_declaration_source(project_root, tasks, degraded)[0]
                task_map = {t.get("id", ""): t for t in _tasks}
                task_def = task_map.get(task_id) or {}
                from orchd.worktree import diagnose_missing_branch_files

                missing = [
                    d["file"] for d in diagnose_missing_branch_files(
                        project_root, task_id, task_def.get("files_to_edit", [])
                    )
                ]
                if missing:
                    raise OrchdError(
                        ErrorCode.E010,
                        "file_conflict: 声明文件未进入任务分支 diff，拒绝 merge",
                        [{
                            "task_id": task_id,
                            "missing_declared_files": missing,
                            "hint": (
                                "请回任务分支确认这些文件已提交；若无需修改，"
                                "请从 files_to_edit 移除或补充说明"
                            ),
                        }],
                    )
            except OrchdError:
                raise
            except Exception:
                pass

        # 并发 merge 串行（task-14-merge-main-tree AC4）：以主工作树锁互斥，
        # 多个 code APPROVED 同时进主工作树 merge 时排队，互不干扰。
        # flat（任务 worktree == 主工作树 == 本 store）不加锁——review_submit
        # 已在开头持有并释放同一把 store 锁，此处复用会重复 acquire 死锁（零回归）。
        # task-audit-hint-show：内联审计的 report 导入必须在 merge/回收之前完成——
        # container 下终态回收会删掉任务 worktree 连带其 orchd/ 源码副本，之后再
        # 延迟导入即 ModuleNotFoundError → merge_audit 恒 skipped(no_project_root)
        # （生产 11/11 code APPROVED 实证；与 _cmd_review 的 _preimport_archive_deps
        # 同模式）。此处提前绑定，后续只用不再导入。
        try:
            from orchd.report import merge_audit as _merge_audit_fn
        except Exception:
            _merge_audit_fn = None
        merge_lock: Any | None = None
        # task-audit-hint-show：main_wt 显式初始化为 None——原仅在
        # `if project_root:` 内赋值，无 project 上下文时后文引用即 NameError；
        # 初始化后无上下文安全降级（审计跳过 + 指引不附主工作树字段）。
        main_wt = None
        if project_root:
            main_wt = main_worktree_root(project_root)
            if main_wt != Path(project_root).resolve():
                merge_lock = Store(main_wt / ".orchd")
                merge_lock.acquire_lock()
        try:
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
                auto = try_auto_resolve_conflict(project_root, task_id) if project_root else None
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
                            [{"conflict_files": conflict_files, "hint": "进入任务 worktree 执行 orchd git merge main，解决冲突后 commit，再由同一 reviewer 重试 code APPROVED"}],
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
                            f"  2. 执行 orchd git merge main（受管通道，任务分支放行），解决冲突后 git commit\n"
                            f"  3. 由同一 reviewer 重试 code APPROVED\n"
                            f"【放弃本次审查】\n"
                            f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason 'merge冲突放弃'\n"
                            f"  orchd force-status --task {task_id} --status pending --reason 'merge冲突回退'"
                        )

            if merge_result is None and project_root is not None and not _nogit_single_dir:
                # P0-18：rename merge_not_executed → merge_env_error（语义更精确）
                result["merged"] = False
                result["reason"] = "merge_env_error"
                result["task_status"] = "in_review"
                result["action"] = (
                    "git merge 未执行（git 不可用 / 非 git 仓库 / 环境异常）：任务保持 "
                    "in_review，未标记完成、未回收任务 worktree。\n"
                    "【修复步骤】\n"
                    "  1. 确认 git 可用且当前目录是有效 git 仓库\n"
                    "  2. 运行 orchd doctor 检查 git 完整性\n"
                    "  3. 修复后由同一 reviewer 重试 code APPROVED\n"
                    "【放弃本次审查】\n"
                    f"  orchd retract --task {task_id} --type REVIEW_CLAIMED --reason 'git环境异常'\n"
                    f"  orchd force-status --task {task_id} --status pending --reason 'git环境异常回退'"
                )
            elif (merge_result is not None and merge_result.get("merge_diagnostic")
                    and project_root is not None):
                # task-merge-failure-diagnostic：非冲突失败带诊断——保持 in_review
                # 不合并；reason 沿用 merge_env_error（下游与既有测试稳定），action
                # 按分类给可执行指引（untracked 碰撞为文件系统处置，非 git 写）。
                _diag = merge_result["merge_diagnostic"]
                result["merged"] = False
                result["reason"] = "merge_env_error"
                result["task_status"] = "in_review"
                result["merge_diagnostic"] = _diag
                result["action"] = _merge_diagnostic_action(task_id, _diag)
            elif merge_result is None or not merge_result.get("conflict") or auto_resolved:
                # project_root 为 None（非 git / 无 worktree）时不进入 merge 分支，
                # 不计算 store_root（此时 merge 必然未执行，remove_task_wt 不会命中）。
                store_root = (
                    resolve_store_dir(project_root / ".orchd") if project_root else None
                )
                # 内联审计根（task-merge-audit-inline）：回收会删掉任务 worktree，
                # 审计 git 探针必须以主工作树为稳定 cwd（与删分支的 delete_root 同口径）。
                # main_wt 仅 project_root 非空时绑定，嵌套取值防 NameError。
                _audit_root = (
                    (main_wt if main_wt is not None else project_root)
                    if project_root else None
                )
                # 回收/删分支前捕获任务分支 tip：之后分支可能已删无法再取；
                # best-effort，取不到则跳过 tip 落 main 项（不拦）。
                _tip_sha: str | None = None
                if _audit_root is not None:
                    try:
                        _tip_sha = _task_branch_tip(_audit_root, task_id)
                    except Exception:
                        _tip_sha = None
                # container（merge_lock 与 store_root/sl 落在同一共享账本根 .lock）下，
                # merge_lock 已持那把 .lock 排他锁：完成事件写入 + 终态回收（unbind）
                # 复用它而非再次 flock，避免 E012 同进程双 fd 死锁（task-14-review
                # -double-lock）。flat（merge_lock=None）与原路径一致：按需自加锁。
                merge_lock_path = (
                    merge_lock.lock_path.resolve() if merge_lock is not None else None
                )
                # 写完成事件所用锁：与 store 同根才复用；否则按需 self 加锁。
                reuse_write = bool(
                    project_root
                    and merge_lock_path is not None
                    and merge_lock_path == store.lock_path.resolve()
                )
                # 终态回收（unbind）所用锁：与 store_root 同根才复用——即使 store 与
                # project 不同根（如测试注入 detached store），只要 merge_lock 已持
                # store_root 那把 .lock，解绑就不得再次 flock（防 E012）。
                reuse_recycle = bool(
                    project_root
                    and store_root is not None
                    and merge_lock_path is not None
                    and merge_lock_path == (store_root / ".lock").resolve()
                )
                if not reuse_write:
                    store.acquire_lock()
                try:
                    state = store.replay()
                    ts = state.get(task_id)
                    if (
                        not ts
                        or ts.status != "in_review"
                        or ts.review_claimed_by != agent_id
                        or ts.review_phase != review_type
                    ):
                        result["task_status"] = ts.status if ts else "unknown"
                        result["merged"] = False
                        result["reason"] = "state_changed_during_merge"
                        result["action"] = (
                            f"git merge 已执行，但任务状态在 merge 期间被改变"
                            f"（当前 {result['task_status']}），完成事件未写入，"
                            f"请人工核对状态与 main 分支后处理"
                        )
                    else:
                        store.append_event(pending_code_event)
                        new_state = store.replay()
                        store.update_checkpoint(new_state)
                        result["task_status"] = "completed"
                finally:
                    if not reuse_write:
                        store.release_lock()
                if result.get("reason") != "state_changed_during_merge":
                    if merge_result is None:
                        # 无 git 上下文（project_root=None，单元测试/无仓库）best-effort：
                        # 无实际合并，不回收 worktree、不删分支。审计恒跳过但仍附
                        # 响应（task-merge-audit-inline AC1/AC4：nogit 单目录 /
                        # 无上下文如实标记 skipped，不阻断完成）。
                        result["merged"] = None
                        if _nogit_single_dir:
                            result["merge_audit"] = {
                                "skipped": True, "reason": "nogit_single_dir",
                            }
                        elif project_root is None:
                            result["merge_audit"] = {
                                "skipped": True, "reason": "no_project_root",
                            }
                        else:
                            result["merge_audit"] = {
                                "skipped": True, "reason": "merge_not_executed",
                            }
                    else:
                        result["merged"] = True
                        if auto_resolved:
                            result["auto_resolved"] = True
                            result["action"] = (
                                f"merge 冲突已自动化解（abort + 分支预演合并），"
                                f"任务已 completed"
                            )
                        # task-14-review-branch-cleanup（AC1/AC2）：先回收任务 worktree，
                        # 释放 task/{task_id} 分支占用，再删分支。修复前顺序颠倒——
                        # 先 try_delete 时任务 worktree 仍 checkout 该分支，git 拒绝删除
                        # 被占用分支（即使 cwd 已改为主工作树 git -C），branch_deleted 恒为
                        # False；worktree 移除后分支不再被占用，-d 方能成功。
                        # task-14-worktree-lifecycle（AC3）：回收任务 worktree =
                        # git worktree remove + 删分支 + 解绑（best-effort）。
                        # ExclusiveFileLock 原语自动判定同进程持锁（depth 计数），
                        # 无需 lock_held 透传，不会触发 E012 死锁。
                        from orchd.worktree import remove_task_wt

                        result["worktree_recycled"] = remove_task_wt(
                            project_root, task_id, store_root, lock_held=reuse_recycle
                        )
                        # 分支删除以**主工作树**为稳定 cwd：容器布局下 remove_task_wt 已
                        # 回收并删除任务 worktree（project_root 目录不再存在），再以
                        # project_root 调用会因 cwd 失效失败。main_wt 在回收前解析，
                        # 回收后仍稳定存在（container=main/，flat=project_root）。
                        delete_root = main_wt if main_wt is not None else project_root
                        result["branch_deleted"] = try_delete_task_branch(
                            delete_root, task_id
                        )
                        # 内联 merge 审计（task-merge-audit-inline，AC1/AC2/AC3）：
                        # code APPROVED 在 merge 成功后自动跑 merge_audit 并附响应。
                        # 只读 best-effort：探针失败 / 跳过（非 git / 无 main /
                        # 无 project_root）不阻断完成。_merge_audit_fn 已在
                        # merge/回收前提前绑定（task-audit-hint-show），此处不再
                        # 延迟导入——回收后任务 worktree 源码副本已删，现导入必败。
                        if _merge_audit_fn is not None and _audit_root is not None:
                            try:
                                _audit = _merge_audit_fn(store, tasks, _audit_root)
                            except Exception:
                                _audit = {"skipped": True, "reason": "audit_failed"}
                        else:
                            _audit = {"skipped": True, "reason": "no_project_root"}
                        result["merge_audit"] = _audit
                        # 窄口径自家残留（AC2）：仅本任务残留退回 in_review +
                        # 可执行指引；无关告警只注解不拦（AC3：completed 维持）。
                        _own: list[dict[str, Any]] = []
                        try:
                            _own = _own_merge_residual(
                                _audit_root, task_id, _tip_sha,
                                result.get("worktree_recycled"),
                                result.get("branch_deleted"),
                            )
                        except Exception:
                            _own = []
                        if _own:
                            if not _audit.get("skipped", False):
                                _audit.setdefault("warnings", []).extend(_own)
                            else:
                                _audit["own_residual"] = _own
                            # task-recycle-noblock：残留分两档处置——
                            # ① 分支类（未合入 / 未清理）：代码未落地，不得标记完成，
                            #    仍打回 in_review（下不变）；
                            # ② 目录残留（worktree_not_recycled，Windows 下调用方
                            #    shell 句柄占用常见）：改警告注解 + completed 维持，
                            #    不再空转第二轮审查；回收 journal 保留（仅全成功才删），
                            #    引擎下次回收该任务时按 journal 重放收敛（延迟回收）。
                            # 未知 reason 按分支档处理（fail-closed 方向）。
                            _bounce = [w for w in _own
                                       if w.get("reason") != "worktree_not_recycled"]
                            _deferred = [w for w in _own
                                         if w.get("reason") == "worktree_not_recycled"]
                            if _bounce:
                                # 退回 in_review：FORCE_STATUS 通道（replay 层 ungated，
                                # target=in_review 保留审查认领字段，同一 reviewer 可重试；
                                # revive 巡检只认 completed→pending，本通道零审计噪音）。
                                _reopen = make_event(
                                    task_id, agent_id, "FORCE_STATUS",
                                    target_status="in_review",
                                    reason="own_merge_residual",
                                    residual=_bounce,
                                )
                                if not reuse_write:
                                    store.acquire_lock()
                                try:
                                    store.append_event(_reopen)
                                    _reopened_state = store.replay()
                                    store.update_checkpoint(_reopened_state)
                                    result["task_status"] = "in_review"
                                finally:
                                    if not reuse_write:
                                        store.release_lock()
                                result["reason"] = "own_merge_residual"
                                result["action"] = _own_residual_action(
                                    task_id, _bounce, _audit_root)
                            elif _deferred:
                                # 目录残留：completed 维持，仅注解（merge_audit
                                # warnings 已含该项；延迟回收指引见完成态 guidance）。
                                result["worktree_residual_deferred"] = {
                                    "task_id": task_id,
                                    "residual": _deferred,
                                    "hint": (
                                        "任务已 completed；目录残留改警告注解，不再打回 "
                                        "in_review。回收 journal 已保留，引擎下次回收该任务"
                                        "时按 journal 重放收敛；目录残留可运行 doctor --fix "
                                        "清理（doctor_check=worktree_residual）"
                                    ),
                                }
        finally:
            if merge_lock is not None:
                merge_lock.release_lock()

    # L3 pre-commit hook 卸载（best-effort）：code APPROVED 终态后不再需要任务级
    # hook，防止残留（与 done/retract 一致；覆盖 merge 降级/成功全部完成路径）。
    if project_root and result.get("task_status") == "completed":
        hook_uninstall(project_root)

    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    # task-review-completion-guidance（AC1/AC2）：code APPROVED 且 merge 成功
    # （任务 completed）的响应附 guidance，明确下一步在主工作树执行
    # status --audit-merge；回收未成功时额外给出处置指引。
    if result.get("task_status") == "completed" and result.get("merged") is True:
        result["guidance"] = _build_completion_guidance(
            task_id, result.get("worktree_recycled"),
            main_worktree=str(main_wt) if main_wt is not None else None,
        )

    # AC4：守卫降级不静默——会话锁未持有时非空，并入响应（沿用「有降级才补字段」
    # 约定，无降级则维持既有字段集合不变）。
    if degraded:
        result["degraded_guards"] = degraded

    return result


def _build_completion_guidance(
    task_id: str, worktree_recycled: dict[str, Any] | None,
    main_worktree: str | None = None,
) -> dict[str, Any]:
    """构造 code APPROVED + merge 成功后的完成态 guidance（AC1/AC2）。

    AC1：明确下一步在主工作树执行 status --audit-merge（rules/review.md 硬要求）。
    AC2：回收未成功（worktree_recycled.removed=false）时额外给出处置指引，
    文案与 residual 实际结局一致、不承诺已清理。
    task-merge-audit-inline：内联审计已在 code APPROVED 内自动执行（响应
    merge_audit 字段），此处复核命令保留作人工二次确认。
    task-recycle-noblock：目录残留不再打回审查时，此处附延迟回收指引
    （journal 保留 + doctor --fix）。
    """
    from orchd.guide import _ENTRY_CMD

    hint = (
        f"任务 {task_id} 已审查通过并合并（completed）。内联 merge 审计已执行"
        f"（见响应 merge_audit 字段）；请在**主工作树**执行 "
        f"{_ENTRY_CMD} status --audit-merge 复核确认 merge_audit.warnings 为空"
        "（rules/review.md 硬要求）。"
    )
    wr = worktree_recycled or {}
    if wr.get("removed") is False:
        residual = wr.get("residual") or {}
        reason = residual.get("reason") or "worktree 回收未成功"
        hint += (
            f" 注意：任务 worktree 回收未成功（{reason}），"
            "请先切出该目录后重试回收，或运行 doctor --fix 清理残留；"
            "不要在已失效的 worktree 目录内执行命令。"
            " 回收 journal 已保留（延迟回收）：引擎下次回收该任务时按 journal "
            "重放收敛，无需重审。"
        )
    return {
        "step": "audit_merge",
        "command": f"{_ENTRY_CMD} status --audit-merge",
        "hint": hint,
        "read": read_for("audit_merge"),
        **({"main_worktree": main_worktree} if main_worktree else {}),
    }


def build_reviewer_rerun_command(task_def: dict[str, Any]) -> str | None:
    """从任务 verify_command 提取 reviewer 可粘贴的定向重跑命令（AC3，单一事实源）。

    解析 verify_command（``ruff && pytest && validate`` 链式），提取含 ``pytest``
    的命令段作为 reviewer 定向重跑模板——reviewer 只需重跑测试，不需重跑 ruff
    或 validate。跨平台 --basetemp 形式直接继承 verify_command 中的写法
    （``${TMPDIR:-/tmp}/orchd-vf-$$``），不手写字符串。verify_command 无
    pytest 段时返回 None（不编造命令）。
    """
    verify_cmd = task_def.get("verify_command")
    if not isinstance(verify_cmd, str) or not verify_cmd.strip():
        return None
    for segment in verify_cmd.split("&&"):
        seg = segment.strip()
        if "pytest" in seg:
            return seg
    return None
