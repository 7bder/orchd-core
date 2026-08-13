"""Orchd 任务生命周期管理模块。

覆盖任务从创建到终结的完整流程：
  - bootstrap: 输出分解套件（schema + prompt + guide），供 LLM 生成任务清单。
  - request: 只读查询候选任务（implementer / reviewer），无锁。
  - claim: 认领任务（锁内 check-then-act），并 best-effort 创建 git 分支。
  - done: 报告完成（锁外执行 verify_command → 锁内二次校验 + 写事件）。
  - review_submit: 提交审查结论（APPROVED / CHANGES_REQUESTED），锁内写事件。
  - retract: 撤回事件及其级联影响，锁内操作。
  - force_status: 强制设置任务状态（受"允许从"矩阵约束），锁内操作。

Git 辅助操作（_try_git_branch / _try_git_merge）均为 best-effort：
成功时反映到返回字段，失败时静默降级、不影响任务状态机。

依赖方向：onboard.py → ledger.py / pool.py / spec.py（不导入 cli / report）。
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    check_workspace_state,
    ensure_committed,
    get_default_branch,
    get_head_commit,
    hook_install,
    hook_uninstall,
    session_lock_acquire,
    session_lock_check,
    session_lock_release,
)
from orchd.ledger import Store, TaskDerived, TaskState, generate_event_id, generate_session_fingerprint
from orchd.pool import (
    _build_claimed_files,
    build_pool,
    detect_file_conflict,
    effective_importance,
    get_dependency_closure,
    sort_candidates,
)

# force-status 合法目标：force_status() 只允许将任务强制设置到这四种状态，
# 其余状态（如 in_review、done）由正常事件流驱动，不可被强制跳转。
_FORCE_TARGETS = {"pending", "claimed", "completed", "cancelled"}

# M-3 逃生口（2026-08-12 全面审计）：默认跳转矩阵 _ALLOWED_FROM 外的两条
# "合理但不鼓励"的跳转，仅当调用方显式 force=True（CLI --force 二次确认）时放行。
# 格式为 (target_status, current_status)，与 _ALLOWED_FROM 的 key 语义一致。
# - ("completed", "claimed")：claimed → completed（实现者弃坑但功能已完成，善后）
# - ("pending", "cancelled")：cancelled → pending（误取消后复活）
# 其余默认非法跳转（如跳过审查的 pending→completed、终态互转 completed→cancelled）
# 不在此白名单内，--force 对它们无效，防止逃生口被用来绕过正常审查/终态保护。
_FORCE_ESCAPE_HATCHES = frozenset({
    ("completed", "claimed"),
    ("pending", "cancelled"),
})

# verify_command 超时（秒）：120s 是在"给测试套件足够执行时间"与
# "避免无限期阻塞 done 流程"之间的折中值；大部分单测 / lint 命令在 2 分钟内可完成。
_VERIFY_TIMEOUT = 120


def _guard_write_command(
    project_root: Path | None,
    *,
    allowed_branches: set[str] | None,
    require_clean: bool,
    command: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> None:
    """L1 分支守卫 + L2 session 锁：写命令前校验当前分支、工作区干净度与 session 锁（best-effort）。

    - ``project_root`` 为 None（单元测试）或 git 不可用/非 git 仓库
      （``check_workspace_state`` 返回 available=False）→ 静默跳过，
      保持 best-effort 契约（不阻塞无 git 环境的降级路径）。
    - 分支不在 ``allowed_branches`` → E018 wrong_branch
      （含当前分支/期望分支/处置指引）。
    - ``require_clean`` 且工作区有已跟踪改动 → E017 dirty_workspace
      （untracked 工具/配置文件不视为脏，与既有"干净"判定一致）。
    - git 可用且 ``orchd_dir`` 和 ``agent_id`` 均提供时，检查 session lock：
      被其他 agent 持有且未超时 → E019 workspace_busy。
    """
    branch = None
    git_available = False
    if project_root is not None:
        state = check_workspace_state(project_root)
        if state.get("available"):
            git_available = True
            branch = state.get("branch")
            if allowed_branches is not None and branch not in allowed_branches:
                expected = sorted(allowed_branches)
                raise OrchdError(
                    ErrorCode.E018,
                    f"wrong_branch: {command} 须在 {expected} 分支执行，当前在 '{branch}'",
                    [{
                        "command": command,
                        "current_branch": branch,
                        "expected_branches": expected,
                        "hint": f"请先切换到 {' 或 '.join(expected)} 分支再执行 {command}",
                    }],
                )
            if require_clean and not state.get("clean"):
                raise OrchdError(
                    ErrorCode.E017,
                    f"dirty_workspace: {command} 要求工作区干净（无已跟踪文件改动）",
                    [{
                        "command": command,
                        "hint": "请先提交或还原已跟踪文件改动（untracked 工具/配置文件不阻塞）",
                    }],
                )

    # L2 session 锁（best-effort：仅在有 git 环境时检查，orchd_dir/agent_id 缺失时跳过）
    if git_available and orchd_dir is not None and agent_id is not None:
        _ensure_session_lock(orchd_dir, agent_id, branch)


def _decode_subprocess_output(raw: bytes) -> str:
    """稳健解码子进程输出：UTF-8 优先，GBK 回退（Windows 默认代码页），最后有损 UTF-8。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _verify_output_summary(stdout: bytes, stderr: bytes, limit: int = 400) -> str:
    """从 verify_command 子进程输出提取可读摘要（stdout 尾部 + stderr 头部）。

    P2（2026-08-08）：verify 结果随 DONE 事件入库，供 reviewer claim 时注入引用。
    stdout 尾部通常含 pytest 汇总（passed/failed），stderr 头部含错误定位；
    均截断防 ledger 膨胀。
    """
    out = _decode_subprocess_output(stdout)
    err = _decode_subprocess_output(stderr)
    parts = []
    if out.strip():
        out_s = out.rstrip()
        parts.append(out_s[-limit:] + ("…" if len(out_s) > limit else ""))
    if err.strip():
        err_s = err.strip()
        parts.append(f"stderr: {err_s[:200]}" + ("…" if len(err_s) > 200 else ""))
    return " | ".join(parts)

# 默认分解指南：当项目 docs/decomposition-guide.md 不存在时，
# bootstrap() 使用此内置文本作为 guide 字段，确保 LLM 始终有基本的分解原则可参考。
_FALLBACK_GUIDE = (
    "分解指南文件缺失。基本原则：任务粒度 0.5-6h，每个任务 1-4 个文件，"
    "2-5 条可量化验收标准，依赖链 ≤ 4 层，测试文件列入 files_to_edit。"
)


def bootstrap(project_root: Path | None = None) -> dict[str, Any]:
    """输出分解套件 JSON：schema + prompt + guide + next_step。

    不调用 LLM、不访问网络、不写文件。
    注意：此函数仅读取项目静态文件（schema / templates / docs），
    不要求 .orchd/ 目录已存在——它可在项目初始化（orchd init）之前安全调用。

    Raises:
        OrchdError E001: schema 或 architect.md 缺失。
    """
    if project_root is None:
        project_root = _find_project_root()

    schema_path = project_root / "schema" / "_master.schema.json"
    prompt_path = project_root / "templates" / "architect.md"
    guide_path = project_root / "docs" / "decomposition-guide.md"

    if not schema_path.exists():
        raise OrchdError(
            ErrorCode.E001,
            f"file not found: {schema_path}",
            [{"path": str(schema_path), "message": "schema 文件缺失"}],
        )
    if not prompt_path.exists():
        raise OrchdError(
            ErrorCode.E001,
            f"file not found: {prompt_path}",
            [{"path": str(prompt_path), "message": "architect prompt 模板缺失"}],
        )

    schema_text = schema_path.read_text(encoding="utf-8")
    prompt_text = prompt_path.read_text(encoding="utf-8")

    if guide_path.exists():
        guide_text = guide_path.read_text(encoding="utf-8")
    else:
        guide_text = _FALLBACK_GUIDE

    return {
        "schema": schema_text,
        "prompt": prompt_text,
        "guide": guide_text,
        "next_step": "请根据以上 schema、prompt 和 guide 对项目进行任务分解，输出符合 _master.schema.json 的 JSON。",
    }


def _find_project_root() -> Path:
    """从 cwd 向上逐层搜索包含 schema/ 子目录的项目根目录。

    搜索策略：依次检查 cwd → cwd.parent → cwd.parent.parent → …，
    返回第一个含有 schema/ 目录的祖先路径；若所有祖先均不匹配则回退到 cwd 本身。
    这使得在子目录中运行 CLI 也能正确定位到项目根。
    """
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / "schema").is_dir():
            return parent
    return cwd

# ------------------------------------------------------------------
# L2 session 工作区锁辅助（2026-08-06 task-l2-session-lock）
# ------------------------------------------------------------------


def _ensure_session_lock(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
) -> None:
    """确保当前 session 可写入：检查 session lock，若被其他 agent 持有则 E019。

    流程：
    1. 检查 .orchd/.session.lock
    2. 若被其他 agent 持有且未超时 → 抛出 E019 workspace_busy
    3. 若未被持有 / 已超时 / 损坏 → 获取锁（覆盖写入）
    4. 若由当前 agent 持有 → 续期（更新时间戳）

    Args:
        orchd_dir: .orchd 目录路径。
        agent_id: 当前 session 的 agent ID。
        branch: 当前 git 分支名（可选）。

    Raises:
        OrchdError E019: 工作区被其他 session 占用。

    Note:
        best-effort 语义：锁获取失败（IO 错误）不抛异常，静默降级。
    """
    check = session_lock_check(orchd_dir)
    if check.get("locked"):
        holder = check.get("agent_id", "unknown")
        if holder != agent_id:
            raise OrchdError(
                ErrorCode.E019,
                f"workspace_busy: 工作区被 '{holder}' 占用（分支 {check.get('branch', 'N/A')}，"
                f"已锁定 {check.get('age_min', 0):.1f} 分钟）",
                [{
                    "agent_id": agent_id,
                    "holder": holder,
                    "holder_branch": check.get("branch"),
                    "holder_timestamp": check.get("timestamp"),
                    "age_min": check.get("age_min"),
                    "hint": "等待该 session 结束，或使用 watchdog --timeout 0 强制释放僵死锁",
                }],
            )
        # 当前 agent 持有锁：续期
    # 锁未被持有 / 已超时 / 损坏 / 当前 agent 持有：获取或续期
    session_lock_acquire(orchd_dir, agent_id, branch)


def _now_iso() -> str:
    """返回当前时间的本地时区 ISO 8601 字符串（精确到秒）。

    先获取 UTC 当前时间，再转换为系统本地时区，避免跨时区机器产生时间混乱。
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _make_event(task_id: str, agent_id: str, etype: str, **extra) -> dict[str, Any]:
    """构造标准事件字典，用于追加到 ledger。

    事件 schema 字段：
        v          - 事件版本号（当前固定为 1），便于未来 schema 演进时做兼容判断。
        event_id   - 全局唯一事件 ID，由 generate_event_id() 生成。
        timestamp  - 事件发生的本地时区 ISO 8601 时间戳（精确到秒）。
        task_id    - 关联的任务 ID。
        agent_id   - 触发此事件的 agent 标识。
        type       - 事件类型（如 CLAIMED / DONE / REVIEW_SUBMITTED / FORCE_STATUS 等）。

    **extra 中的键值对会直接合并到事件字典，用于各类型事件的差异化字段
    （如 changes_description、verdict、target_status 等）。
    """
    ev = {
        "v": 1,
        "event_id": generate_event_id(),
        "timestamp": _now_iso(),
        "task_id": task_id,
        "agent_id": agent_id,
        "type": etype,
        "session_fingerprint": generate_session_fingerprint(),
    }
    ev.update(extra)
    return ev


# ------------------------------------------------------------------
# request（只读，无锁）
# ------------------------------------------------------------------


def _find_review_priority_tasks(
    store: Store, state: dict[str, TaskState], tasks: list[dict[str, Any]],
    agent_id: str, derived: TaskDerived | None = None,
) -> list[dict[str, Any]]:
    """查找该 agent 可审查的 in_review 任务（用于 implementer request 时的优先调度提示）。

    过滤条件：
    1. 任务状态为 in_review
    2. 审查未被其他人认领（review_claimed_by is None）
    3. agent 在任务的 reviewers 名单内
    4. agent 不是该任务的实现者（排除 self-review）

    H2（2026-08-13）：``derived`` 为 request 单次扫描的派生缓存，
    循环内查询实现者改为 O(1)（原实现对每个候选任务全扫一次 ledger）。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    review_tasks: list[dict[str, Any]] = []
    for tid, ts in state.items():
        if ts.status != "in_review" or ts.review_claimed_by is not None:
            continue
        task_def = task_map.get(tid, {})
        # 检查 agent 是否在 reviewers 名单内
        if agent_id not in task_def.get("reviewers", []):
            continue
        # 排除 self-review：检查 DONE 事件的 agent_id
        done_author, _ = _extract_last_done(store, tid, derived)
        if done_author and done_author == agent_id:
            continue
        review_tasks.append({
            "task_id": tid,
            "review_phase": ts.review_phase or "spec",
            "name": task_def.get("name", ""),
        })
    # spec 阶段优先
    review_tasks.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    return review_tasks


def request(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    capabilities: list[str] | None = None,
    exclude: list[str] | None = None,
    role: str = "implementer",
    sort_key: str | None = None,
    max_active: int | None = None,
    importance_thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """返回排序后第一个候选任务（或空池 JSON）。只读不加锁。

    当候选池非空时，返回排序最优的候选任务摘要（task_id / name / brief 等），
    供调用方确认后进入 claim 阶段。

    Args:
        max_active: 全局容量上限（--max-active N）。当前处于 claimed 状态的任务数
            达到该值时返回空候选（reason="max_active_reached"），拒绝再领取，
            用于无人值守场景控制并发活跃任务数。None 表示不限制。

    当候选池为空时，返回空池响应格式：
        candidate=None, message=提示信息, next_action="exit",
        pool_size=0, blocked_count=被依赖阻塞的任务数,
        reason="capability_mismatch"|"none_ready"（区分能力不匹配与真无就绪），
        mismatched=能力不匹配时的就绪任务列表（[{task_id, requires}]）。
    调用方应据此提示用户退出、等待依赖完成或调整能力声明。
    """
    state = store.replay()

    # H2（2026-08-13 性能审核）：单次扫描构建 per-task 派生缓存，
    # 供 _find_review_priority_tasks / _request_reviewer 的多次查询复用，
    # 消除「每个候选任务全扫一次 ledger」的重复 O(L) 扫描。
    derived = store.scan_task_derived()

    if role == "reviewer":
        return _request_reviewer(store, state, tasks, agent_id, derived)

    # Review 优先调度：implementer 请求时，若有可认领的 review 任务，优先提示审查
    review_priority = _find_review_priority_tasks(store, state, tasks, agent_id, derived)
    if review_priority:
        best_review = review_priority[0]
        return {
            "candidate": None,
            "review_priority": {
                "task_id": best_review["task_id"],
                "review_phase": best_review["review_phase"],
                "name": best_review["name"],
                "total_available": len(review_priority),
            },
            "message": (
                f"有 {len(review_priority)} 个待审查任务可领取，建议先完成审查再领取实现任务。"
                f"使用 'orchd request --agent {agent_id} --role reviewer' 领取审查任务。"
            ),
            "next_action": "review_first",
            "pool_size": 0,
        }

    # --max-active N 容量控制：全局活跃（claimed）任务数达到上限即拒绝新领取
    if max_active is not None:
        active = sum(1 for ts in state.values() if ts.status == "claimed")
        if active >= max_active:
            return {
                "candidate": None,
                "message": (
                    f"max_active_reached: 全局活跃任务数 {active} 已达上限"
                    f" {max_active}，拒绝再领取。等待已完成任务进入审查后再试。"
                ),
                "next_action": "wait",
                "reason": "max_active_reached",
                "pool_size": 0,
                "active_count": active,
                "max_active": max_active,
            }

    candidates = build_pool(tasks, state, capabilities=capabilities, exclude=exclude)
    candidates = sort_candidates(candidates, sort_key=sort_key,
                                 importance_thresholds=importance_thresholds)

    # L1 pending 级文件冲突预检 → 依赖感知强制过滤（2026-08-08 重构）：
    # 与池内 claimed+pending 任务 files_to_edit 冲突的候选分类处理：
    # - 与 claimed 活跃任务冲突 → 硬排除（领取必撞 claim E010，提前拦截）
    # - 与 pending 非依赖任务冲突 → 硬排除（同批次并行会撞，强制串行化）
    # - 与 pending 依赖任务冲突（依赖闭包内）→ 放行（依赖链串行正确）
    # 被排除项保留在响应 excluded_conflicts 中供可见性；无 --force 绕行通道
    # （claim E010 仍是不可绕过的最终边界）。
    excluded_conflicts: list[dict[str, Any]] = []
    candidate_conflicts: dict[str, list[dict[str, Any]]] = {}
    kept: list[Any] = []
    # H3（2026-08-13 性能审核）：claimed_files 映射循环外预计算一次
    # （O(T)），候选循环内 detect_file_conflict 传参复用——原实现每个候选
    # 都内部重建映射，O(候选数 × T)。
    claimed_files = _build_claimed_files(state, tasks, include_pending=True)
    for cand in candidates:
        conflicts = detect_file_conflict(
            state, tasks, cand.task, include_pending=True,
            claimed_files=claimed_files,
        )
        if not conflicts:
            kept.append(cand)
            continue
        dep_closure = get_dependency_closure(cand.task.get("id", ""), tasks)
        excluded: list[dict[str, Any]] = []
        allowed: list[dict[str, Any]] = []
        for c in conflicts:
            if c.claimed_by == "pending" and c.task_id in dep_closure:
                allowed.append({
                    "task_id": c.task_id,
                    "files": c.files,
                    "claimed_by": c.claimed_by,
                })  # 依赖链上的 pending 冲突：串行正确，放行
            else:
                excluded.append({
                    "task_id": c.task_id,
                    "files": c.files,
                    "claimed_by": c.claimed_by,
                })
        if excluded:
            excluded_conflicts.append({
                "task_id": cand.task.get("id", ""),
                "conflicts": excluded,
            })
        if not excluded:
            # 无硬冲突（或仅依赖链冲突）：保留候选；有任一硬冲突即整体排除
            kept.append(cand)
            if allowed:
                candidate_conflicts[cand.task.get("id", "")] = allowed
    candidates = kept  # 硬冲突候选已被排除

    if not candidates:
        # 区分空池原因：能力不匹配（存在就绪候选但 requires 不满足）vs 真无就绪
        reason = "none_ready"
        mismatched: list[dict[str, Any]] = []
        if capabilities:
            unfiltered = build_pool(tasks, state, capabilities=None, exclude=exclude)
            mismatched = [
                {
                    "task_id": c.task.get("id", ""),
                    "requires": list(c.task.get("requires", [])),
                }
                for c in unfiltered
                if not set(c.task.get("requires", [])).issubset(set(capabilities))
            ]
            if mismatched:
                reason = "capability_mismatch"
        # 计算 blocked_count：pending 但依赖未满足的任务数
        blocked_count = 0
        for task in tasks:
            tid = task.get("id", "")
            ts = state.get(tid)
            s = ts.status if ts else "pending"
            if s == "pending":
                for dep_id in task.get("depends_on", []):
                    dep_ts = state.get(dep_id)
                    dep_s = dep_ts.status if dep_ts else "pending"
                    if dep_s not in ("completed", "cancelled"):
                        blocked_count += 1
                        break
        if reason == "capability_mismatch":
            message = (
                f"当前无匹配能力的就绪任务（{len(mismatched)} 个就绪任务因能力不匹配被过滤；"
                "可用 --capabilities 空格分隔声明能力，如 --capabilities python git docs）"
            )
        elif excluded_conflicts:
            reason = "conflict_excluded"
            message = (
                f"当前无就绪任务（{len(excluded_conflicts)} 个候选因文件冲突被依赖感知"
                "强制过滤；完成冲突任务后重试，或调整 files_to_edit）"
            )
        else:
            message = "当前无就绪任务（所有任务已完成或被阻塞）"
        return {
            "candidate": None,
            "message": message,
            "next_action": "exit",
            "pool_size": 0,
            "blocked_count": blocked_count,
            "reason": reason,
            "mismatched": mismatched,
            "excluded_conflicts": excluded_conflicts,
        }

    best = candidates[0]
    task_id = best.task.get("id", "")
    # downstream_blocked: 被当前任务阻塞的 pending 任务 ID 列表
    downstream_blocked = []
    for task in tasks:
        ts = state.get(task.get("id", ""))
        s = ts.status if ts else "pending"
        if s == "pending" and task_id in task.get("depends_on", []):
            downstream_blocked.append(task.get("id", ""))

    review_comments = _extract_review_comments(store, task_id, derived)
    warnings: list[str] = []
    ts = state.get(task_id)
    if ts and ts.attempt_count > 0:
        max_attempts = best.task.get("max_attempts", 3)
        if ts.attempt_count >= max_attempts:
            warnings.append("exceeded_max_attempts")

    # 候选摘要：
    # 全量任务定义（files_to_read / acceptance_criteria / deliverables /
    # verify_command 等大字段）推迟到 claim 时返回，避免同 session 重复序列化。
    candidate: dict[str, Any] = {
        "task_id": task_id,
        "name": best.task.get("name", ""),
        "brief": best.task.get("brief", ""),
        "module": best.task.get("module", ""),
        "importance": effective_importance(best.task, best.blocked_downstream_count),
        "depends_on": list(best.task.get("depends_on", [])),
        "downstream_blocked": downstream_blocked,
        "review_comments": review_comments,
        "source": best.task.get("source"),
    }
    if task_id in candidate_conflicts:
        candidate["conflict_with"] = candidate_conflicts[task_id]
        warnings.append(
            f"file_conflict_pending: 与依赖链任务文件冲突 {len(candidate_conflicts[task_id])} 处"
            f"（{candidate_conflicts[task_id][0]['task_id']} 等），依赖串行放行"
        )
    for optional in ("difficulty", "estimated_hours"):
        if optional in best.task:
            candidate[optional] = best.task[optional]

    return {
        "candidate": candidate,
        "pool_size": len(candidates),
        "prompt": f"确认将此任务分配给 {agent_id}？(执行 / 跳过 / 重新声明能力)",
        "warnings": warnings,
        "excluded_conflicts": excluded_conflicts,
    }


def _request_reviewer(
    store: Store, state: dict[str, TaskState], tasks: list[dict[str, Any]],
    agent_id: str, derived: TaskDerived | None = None,
) -> dict[str, Any]:
    """查找处于 in_review 且未被审查者 claim 的任务。

    排序规则：spec 阶段的审查优先于 code 阶段（spec 通过后才进入 code review，
    因此 spec 优先可以减少等待时间）。同等阶段内按 ledger 遍历顺序排列。

    H2（2026-08-13）：``derived`` 为 request 单次扫描的派生缓存。
    """
    review_candidates = []
    not_in_list: list[dict[str, Any]] = []
    task_map = {t.get("id", ""): t for t in tasks}
    for tid, ts in state.items():
        if ts.status == "in_review" and ts.review_claimed_by is None:
            task_def = task_map.get(tid, {})
            # 预过滤：请求方必须在任务 reviewers 名单内（否则 claim 必然 E007）
            if agent_id not in task_def.get("reviewers", []):
                not_in_list.append({
                    "task_id": tid,
                    "review_phase": ts.review_phase or "spec",
                    "reviewers": task_def.get("reviewers", []),
                })
                continue
            # P3.1：排除 self-review（实现者不得审查自己实现的任务，
            # 对齐 _find_review_priority_tasks 与 claim 的 E016）
            done_author, _ = _extract_last_done(store, tid, derived)
            if done_author and done_author == agent_id:
                continue
            review_candidates.append({
                "task_id": tid,
                "task": task_def,
                "review_phase": ts.review_phase,
            })
    # spec 优先
    review_candidates.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    if not review_candidates:
        if not_in_list:
            # §7.4：agent 不在任何待审查任务的 reviewers 名单内时，返回
            # reason 与 tasks（含各任务 reviewers 名单），供人判断是否 amend。
            return {
                "candidate": None,
                "message": f"有 {len(not_in_list)} 个待审查任务但你不在名单内",
                "next_action": "exit",
                "pool_size": 0,
                "reason": "not_in_reviewer_list",
                "tasks": not_in_list,
            }
        return {
            "candidate": None,
            "message": "当前无待审查任务",
            "next_action": "exit",
            "pool_size": 0,
        }
    best = review_candidates[0]
    task_def = best["task"]
    task_id = best["task_id"]
    author, changes_description = _extract_last_done(store, task_id, derived)

    # 审查候选摘要：审查所需上下文
    # （files_to_review / acceptance_criteria / changes_description）在此给出，
    # 不再全量返回任务定义。
    candidate: dict[str, Any] = {
        "task_id": task_id,
        "review_type": best["review_phase"],
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

    return {
        "candidate": candidate,
        "pool_size": len(review_candidates),
        "prompt": f"确认将此审查分配给 {agent_id}？(执行 / 跳过 / 重新声明能力)",
        "warnings": [],
    }


def _extract_review_comments(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> list[str]:
    """从 ledger 中提取该任务的所有审查意见（REVIEW_SUBMITTED 的 comments）。

    注意：此函数需要从头扫描整个 ledger（from_line=1），因为审查意见
    可能散布在任务生命周期的任意时刻（如多轮审查打回场景），无法通过
    checkpoint 或索引快速定位。

    H2（2026-08-13 性能审核）：调用方若已用 :meth:`Store.scan_task_derived`
    单次扫描，可传入 ``derived`` 直接查询缓存（O(1)），避免重复全扫。
    """
    if derived is not None:
        return derived.review_comments.get(task_id, [])
    comments: list[str] = []
    if not store.ledger_exists():
        return comments
    events = store._read_ledger_lines(from_line=1)
    for ev in events:
        if (ev.get("task_id") == task_id
                and ev.get("type") == "REVIEW_SUBMITTED"
                and ev.get("comments")):
            comments.append(ev["comments"])
    return comments


def _extract_last_done(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> tuple[str | None, str | None]:
    """从 ledger 中提取该任务最近一次 DONE 事件的 (agent_id, changes_description)。

    遍历全部事件，取最后一个 type=="DONE" 的记录；多轮返工时返回最近一轮的作者与变更描述。
    若 ledger 不存在或无 DONE 事件，返回 (None, None)。

    H2（2026-08-13 性能审核）：传入 ``derived`` 时直接查缓存（O(1)）。
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


def _find_last_done_event(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> dict[str, Any] | None:
    """从 ledger 中查找该任务最近一次 DONE 事件（含 timestamp/agent_id）。

    用于 done 的"假失败消除"：verify 失败但 ledger 已写 DONE 时，
    说明 DONE 已实际落地（如超时异常后重试），应返回成功语义而非 E014。
    返回最近一条 DONE 事件 dict；无则返回 None。

    H2（2026-08-13 性能审核）：传入 ``derived`` 时直接查缓存（O(1)）。
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


def _is_high_risk(task_def: dict[str, Any]) -> bool:
    """高风险领域判定：任务触碰引擎核心（状态机分支 / CLI 契约 / 锁协议）。

    用于「共享上下文按需」：这类任务的实现默认自动附 conventions.md
    （编码规范 + 自检约定），降低越界/违规概率；architecture.md 不自动附，
    仅任务自身 files_to_read 显式引用或 --with-context 显式开启时提供。
    """
    module = task_def.get("module", "")
    if module == "mod-core":
        return True
    for f in task_def.get("files_to_edit", []):
        if f.startswith("orchd/") or f == ".orchd/_master.json":
            return True
    return False


def _extract_previous_changes(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> str | None:
    """从 ledger 中提取该任务最近一次 DONE 的 changes_description。"""
    return _extract_last_done(store, task_id, derived)[1]


# ------------------------------------------------------------------
# claim（写操作，锁内）
# ------------------------------------------------------------------


def claim(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str = "implementer",
    project_root: Path | None = None,
    shared: dict[str, Any] | None = None,
    review_type: str | None = None,
    with_context: bool = False,
) -> dict[str, Any]:
    """认领任务。锁内 check-then-act。

    校验逻辑（锁内）：状态合法性、依赖满足、文件冲突、agent 繁忙等。
    事件写入（锁内）：CLAIMED 或 REVIEW_CLAIMED 事件追加到 ledger。
    Git 分支（锁外 best-effort）：implementer 认领后自动创建/切换 task/{id} 分支，
    失败时静默降级，不影响认领状态。

    Args:
        with_context: 是否显式附加全部共享上下文（architecture + conventions）。
            默认 False——共享上下文按需：仅高风险领域任务（引擎核心模块/状态机
            分支/CLI 契约/锁协议）自动附 conventions.md；architecture.md 仅任务
            自身 files_to_read 显式引用时提供（ROADMAP 1.1「共享上下文按需」）。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E008, f"task '{task_id}' not found in master",
                         [{"task_id": task_id}])

    # L1 分支守卫 + L2 session 锁（锁外，best-effort）：
    # - implementer：须在默认分支（main/master）且工作区干净（引擎要从
    #   当前 HEAD 建任务分支，脏工作区会导致 checkout -b 后分支被污染）；
    # - reviewer：须在对应 task 分支且工作区干净（审查的是已提交 diff）。
    if role == "reviewer":
        _guard_write_command(
            project_root,
            allowed_branches={f"task/{task_id}"},
            require_clean=True,
            command="review claim",
            orchd_dir=store.orchd_dir,
            agent_id=agent_id,
        )
    else:
        default_branch = get_default_branch(project_root) or "main"
        _guard_write_command(
            project_root,
            allowed_branches={default_branch},
            require_clean=True,
            command="claim",
            orchd_dir=store.orchd_dir,
            agent_id=agent_id,
        )

    store.acquire_lock()
    try:
        state = store.replay()
        # H2（2026-08-13）：单次扫描派生缓存，供 E016 校验与返回段
        # review_comments / done_event / previous_changes 复用（锁外同样
        # 有效——derived 是纯内存数据，无锁生命周期绑定）。
        derived = store.scan_task_derived()
        ts = state.get(task_id)
        status = ts.status if ts else "pending"

        if role == "reviewer":
            # Reviewer claim：任务必须 in_review 且未被审查者认领
            if status != "in_review":
                raise OrchdError(
                    ErrorCode.E008,
                    f"task_not_ready: '{task_id}' status is '{status}', expected 'in_review'",
                    [{"task_id": task_id, "current_status": status}],
                )
            # 校验：审查阶段必须与任务当前 review_phase 匹配（spec/code 两阶段串行）
            current_phase = (ts.review_phase if ts else None) or "spec"
            if review_type and review_type != current_phase:
                raise OrchdError(
                    ErrorCode.E007,
                    f"phase_mismatch: task '{task_id}' 当前是 {current_phase} 审查阶段，"
                    f"不能认领 {review_type} 审查",
                    [{"task_id": task_id, "current_phase": current_phase,
                      "requested_phase": review_type,
                      "hint": "spec 审查 APPROVED 后任务自动进入 code 审查，届时再认领 code"}],
                )
            # 校验：只有任务指定的 reviewers 名单内的 agent 可认领审查
            designated = task_def.get("reviewers", [])
            if agent_id not in designated:
                raise OrchdError(
                    ErrorCode.E007,
                    f"not_designated_reviewer: '{agent_id}' 不在任务 '{task_id}' 的 reviewers 名单中",
                    [{"task_id": task_id, "agent": agent_id, "reviewers": designated,
                      "hint": "请使用名单内的 agent ID，或先 orchd amend 修改 reviewers"}],
                )
            if ts and ts.review_claimed_by:
                raise OrchdError(
                    ErrorCode.E009,
                    f"already_claimed: review claimed by '{ts.review_claimed_by}'",
                    [{"task_id": task_id, "claimed_by": ts.review_claimed_by,
                      "hint": "如该审查已中断（不会继续提交），可 orchd retract 该 REVIEW_CLAIMED 事件释放认领"}],
                )
            # E016: self-review 阻断——实现者不得审查自己实现的任务
            done_author, _ = _extract_last_done(store, task_id, derived)
            if done_author and done_author == agent_id:
                raise OrchdError(
                    ErrorCode.E016,
                    f"self_review_blocked: '{agent_id}' 是任务 '{task_id}' 的实现者，不能审查自己的实现",
                    [{"task_id": task_id, "agent_id": agent_id, "done_by": done_author,
                      "hint": "请使用其他 agent ID（如 reviewer-1）领取此审查任务，确保审查独立性"}],
                )
        else:
            # Implementer claim：必须 pending + 依赖满足
            if status != "pending":
                raise OrchdError(
                    ErrorCode.E008,
                    f"task_not_ready: '{task_id}' status is '{status}', expected 'pending'",
                    [{"task_id": task_id, "current_status": status}],
                )
            for dep_id in task_def.get("depends_on", []):
                dep_ts = state.get(dep_id)
                dep_status = dep_ts.status if dep_ts else "pending"
                if dep_status not in ("completed", "cancelled"):
                    raise OrchdError(
                        ErrorCode.E008,
                        f"task_not_ready: dependency '{dep_id}' not satisfied (status={dep_status})",
                        [{"task_id": task_id, "blocked_by": dep_id}],
                    )
            # E009: 未被其他 agent claim
            if ts and ts.claimed_by and ts.claimed_by != agent_id:
                raise OrchdError(
                    ErrorCode.E009,
                    f"already_claimed: '{task_id}' claimed by '{ts.claimed_by}'",
                    [{"task_id": task_id, "claimed_by": ts.claimed_by}],
                )
            # E010: 文件冲突
            conflicts = detect_file_conflict(state, tasks, task_def)
            if conflicts:
                raise OrchdError(
                    ErrorCode.E010,
                    f"file_conflict: files overlap with active tasks",
                    [{"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by}
                     for c in conflicts],
                )

        # E011: agent busy（implementer 与 reviewer 角色互斥：同一 agent 一次只能
        # 持有一个实现任务或一个审查任务，杜绝「实现 A + 审查 B」并行——
        # 2026-08-13 全面审核 §4.3 修复）
        for tid, t_state in state.items():
            if t_state.status == "claimed" and t_state.claimed_by == agent_id:
                raise OrchdError(
                    ErrorCode.E011,
                    f"agent_busy: '{agent_id}' already holds task '{tid}'",
                    [{"agent_id": agent_id, "blocking_task": tid}],
                )
            if t_state.status == "in_review" and t_state.review_claimed_by == agent_id:
                # 找该任务最近的 REVIEW_CLAIMED 事件 id，供中断的审查者 retract 自救
                review_claim_event_id = ""
                for ev in store._read_ledger_lines(from_line=1):
                    if (ev.get("task_id") == tid
                            and ev.get("type") == "REVIEW_CLAIMED"
                            and ev.get("agent_id") == agent_id):
                        review_claim_event_id = ev.get("event_id", "")
                raise OrchdError(
                    ErrorCode.E011,
                    f"agent_busy: '{agent_id}' already reviewing task '{tid}'",
                    [{
                        "agent_id": agent_id,
                        "blocking_task": tid,
                        "review_claim_event_id": review_claim_event_id,
                        "hint": (
                            "如该审查已中断（不会继续提交），可执行 "
                            f"orchd retract --agent {agent_id} --event {review_claim_event_id} "
                            "--reason 'abandoned review' 释放认领后重新领取审查"
                            if review_claim_event_id else
                            "该任务无你的 REVIEW_CLAIMED 事件，请人工核对状态"
                        ),
                    }],
                )

        # 写事件
        files_claimed = task_def.get("files_to_edit", [])
        if role == "reviewer":
            review_phase = ts.review_phase if ts else "spec"
            # R1: 记录 baseline_sha（认领时的 HEAD commit），用于审查期间漂移检测
            baseline_sha = get_head_commit(project_root) if project_root else None
            event = _make_event(
                task_id, agent_id, "REVIEW_CLAIMED",
                review_type=review_phase,
                baseline_sha=baseline_sha,
            )
        else:
            event = _make_event(
                task_id, agent_id, "CLAIMED",
                role=role,
                files_claimed=files_claimed,
            )
        store.append_event(event)

        # 更新 checkpoint
        new_state = store.replay()
        store.update_checkpoint(new_state)

    finally:
        store.release_lock()

    # git 分支（best-effort，锁外）
    if role == "implementer" and project_root:
        _try_git_branch(project_root, task_id)
        # L3 pre-commit hook 安装（best-effort，锁外）
        files_to_edit = task_def.get("files_to_edit", [])
        if files_to_edit:
            hook_install(
                project_root, task_id, files_to_edit,
                exempt_files=task_def.get("exempt_files"),
            )

    review_comments = _extract_review_comments(store, task_id, derived)

    # 构造返回
    if role == "reviewer":
        # 审查者专用契约：
        # files_to_review + acceptance_criteria + changes_description，
        # 并按 review 阶段差异化附加 shared 上下文。
        files_to_review = [
            {"path": p, "priority": "must_read"}
            for p in task_def.get("files_to_edit", [])
        ]
        if shared:
            if review_phase == "spec":
                arch = shared.get("architecture")
                if arch:
                    files_to_review.append({
                        "path": arch, "priority": "reference",
                        "hint": "架构上下文（spec review 自动附加）",
                    })
            else:
                conv = shared.get("conventions")
                if conv:
                    files_to_review.append({
                        "path": conv, "priority": "must_read",
                        "hint": "编码规范（code review 自动附加）",
                    })
        done_event = _find_last_done_event(store, task_id, derived)
        changes_description = done_event.get("changes_description") if done_event else None
        review_claim_result = {
            "claimed": True,
            "task_id": task_id,
            "review_type": review_phase,
            "files_to_review": files_to_review,
            "acceptance_criteria": task_def.get("acceptance_criteria", []),
            "changes_description": changes_description,
            "review_comments": review_comments,
            "event_id": event["event_id"],
        }
        # P2（2026-08-08）：注入最近 DONE 事件的 verify 结果摘要
        # （ok / exit_code / elapsed_seconds / output_summary），reviewer 默认引用
        # 该结果而非重跑测试（证据分层）；旧事件无 verify 字段则省略（兼容）。
        if done_event and done_event.get("verify"):
            review_claim_result["verify"] = done_event["verify"]
        return review_claim_result

    files_to_read = list(task_def.get("files_to_read", []))
    if shared:
        if with_context:
            # --with-context 显式开启：附加全部共享上下文
            for key in ("architecture", "conventions"):
                path = shared.get(key)
                if path:
                    files_to_read.append({
                        "path": path, "priority": "reference",
                        "hint": "共享上下文（--with-context 显式开启）",
                    })
        elif _is_high_risk(task_def):
            # 共享上下文按需：高风险领域（引擎核心/状态机/CLI 契约/锁协议）
            # 默认自动附 conventions.md；architecture.md 仅显式引用时提供
            conv = shared.get("conventions")
            if conv:
                files_to_read.append({
                    "path": conv, "priority": "reference",
                    "hint": "共享上下文（高风险领域自动附加）",
                })

    previous_changes = _extract_previous_changes(store, task_id, derived)

    # L1 pending 级冲突预警（不阻止 claim，提示后续 merge 冲突风险）
    pending_conflicts = [
        {"task_id": c.task_id, "files": c.files, "claimed_by": c.claimed_by}
        for c in detect_file_conflict(state, tasks, task_def, include_pending=True)
        if c.claimed_by == "pending"
    ]

    return {
        "claimed": True,
        "task": task_def,
        "files_to_read": files_to_read,
        "files_to_edit": files_claimed,
        "review_comments": review_comments,
        "previous_changes": previous_changes,
        "branch": f"task/{task_id}",
        "pending_conflicts": pending_conflicts,
        "event_id": event["event_id"],
    }


# ------------------------------------------------------------------
# done（锁外 verify + 锁内写事件）
# ------------------------------------------------------------------


def _load_config_blocked(store: Store) -> set[str] | None:
    """从 master config 读取 doc_single_stage_blocked（C5 配置化）。

    读 ``.orchd/_master.json`` 顶层 config.doc_single_stage_blocked（string[]）；
    config 缺失、空对象或无该键时返回 None（调用方回退硬编码默认集合）。
    best-effort：master 缺失/解析失败返回 None（不抛异常）。
    """
    try:
        master_path = store.orchd_dir / "_master.json"
        if not master_path.exists():
            return None
        import json as _json
        master = _json.loads(master_path.read_text(encoding="utf-8"))
        blocked = (master.get("config") or {}).get("doc_single_stage_blocked")
        if not isinstance(blocked, list) or not blocked:
            return None
        return {str(b) for b in blocked if isinstance(b, str)}
    except (OSError, ValueError):
        return None


# 文档白名单：仅这些明确的文档类型判为「文档单阶段」；schema / 构建配置 / CI /
# 任何代码文件（含非 Python：.js / .go / .toml / .yml 等）一律走双阶段审查
# （P2 §4.1 收紧——此前「非 .py / 非 orchd/ / 非 tests/ 即文档」过宽）。
_DOC_SINGLE_STAGE_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt")


def _is_doc_single_stage(
    files_to_edit: list[str],
    blocked: set[str] | None = None,
) -> bool:
    """Q2 review 分级判定：纯文档且不碰约定/状态文件 → 单阶段（跳过 spec）。

    满足以下条件返回 True（文档类单阶段 code 终审）：
    - files_to_edit 全部为「真文档」（白名单后缀 .md/.mdx/.markdown/.rst/.txt，
      含 docs/、doc/ 目录下的文档文件——目录不再无条件放行，见下）
    - 不包含约定/状态文件：SKILL.md、.orchd/shared/conventions.md、.orchd/_master.json
      （这些属于"约定改变"，须保持双阶段审查）

    白名单语义（2026-08-13 全面审核 §4.1 收紧）：schema JSON、pyproject.toml、
    CI yml、任何代码文件（含非 Python）都是高风险变更，一律双阶段终审。

    目录 + 后缀双重判断（2026-08-13 remaining-issues 遗留项 1）：docs/、doc/
    目录下的**代码/数据文件**（如 docs/example.py、docs/schema.json、doc/x.js）
    同样必须命中后缀白名单，否则双阶段——「目录放行」不得绕过后缀白名单，
    防止示例代码 / JSON 契约 / 脚本被静默单阶段终审。

    C5（ROADMAP 1.1.1）：blocked 集合可配置——从 master config 的
    ``doc_single_stage_blocked`` 读取，缺省回退硬编码集合；新增约定文件
    加入 config 后，含该文件的任务不再被判定单阶段。

    Args:
        files_to_edit: 任务声明的 files_to_edit 列表。
        blocked: blocked 文件集合（可选，缺省用硬编码默认集合）。

    Returns:
        True 表示单阶段（跳过 spec）；False 表示常规双阶段。
    """
    if not files_to_edit:
        return False
    if blocked is None:
        blocked = {"SKILL.md", ".orchd/shared/conventions.md", ".orchd/_master.json"}
    for f in files_to_edit:
        if f in blocked:
            return False
        lower = f.lower()
        # 只认后缀白名单：docs/ 目录下文件也须命中后缀（目录 + 后缀双重判断），
        # 避免 docs/example.py 等代码/数据文件被误判为文档单阶段。
        if not lower.endswith(_DOC_SINGLE_STAGE_SUFFIXES):
            return False
    return True


def done(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    changes_description: str,
    concerns: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """报告任务完成。verify_command 锁外执行，锁内二次校验 + 写事件。

    采用两阶段模式以避免长时间持锁：
      1. 锁外阶段：预校验任务状态 → 执行 verify_command（可能耗时较长）。
      2. 锁内阶段：TOCTOU 二次校验状态是否仍为 claimed → 写 DONE 事件 →
         自动追加 REVIEW_READY(spec) 事件，触发 spec 审查流程。
    这种模式确保 verify_command 的长耗时不会阻塞其他 agent 的并发操作。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E007, f"task '{task_id}' not found",
                         [{"task_id": task_id}])

    # 预校验（无锁，快速失败）
    state = store.replay()
    ts = state.get(task_id)
    if not ts or ts.status != "claimed" or ts.claimed_by != agent_id:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not claimed by '{agent_id}'",
            [{"task_id": task_id, "expected": "claimed", "actual": ts.status if ts else "pending"}],
        )

    # L1 分支守卫 + L2 session 锁（锁外，best-effort）：done 须在目标 task 分支或 main 上执行。
    # 注意：此处只校验分支，不校验干净度——files_to_edit 范围内的未提交
    # 改动是正常状态（由引擎 ensure_committed 兜底提交）；干净校验放在
    # 自动提交之后（提交后仍有已跟踪改动 = 范围外改动，见下方 E017 检查）。
    default_branch = get_default_branch(project_root) or "main"
    _guard_write_command(
        project_root,
        allowed_branches={f"task/{task_id}", default_branch},
        require_clean=False,
        command="done",
        orchd_dir=store.orchd_dir,
        agent_id=agent_id,
    )

    # verify_command 锁外执行
    verify_cmd = task_def.get("verify_command")
    # B3（ROADMAP 1.1.1）：verify 超时可配置——任务级 verify_timeout_seconds 可选字段，
    # 缺失回退引擎默认 _VERIFY_TIMEOUT=120（全量 pytest 慢机器/沙箱拦截实测 210s 超时场景可调大）
    verify_timeout = task_def.get("verify_timeout_seconds") or _VERIFY_TIMEOUT
    # P2：verify 结果摘要随 DONE 事件入库（verify_record），供 review claim 注入引用。
    verify_record: dict[str, Any] | None = None
    if verify_cmd and project_root:
        import time as _time
        started = _time.monotonic()
        try:
            result = subprocess.run(
                verify_cmd, shell=True, cwd=str(project_root),
                capture_output=True, timeout=verify_timeout,
            )
            elapsed = round(_time.monotonic() - started, 1)
            verify_record = {
                "ok": result.returncode == 0,
                "exit_code": result.returncode,
                "elapsed_seconds": elapsed,
                "output_summary": _verify_output_summary(result.stdout, result.stderr),
            }
            if result.returncode != 0:
                # 假失败消除必须限定"本次 attempt 的 DONE 已实际落地"：仅当任务
                # 状态已离开 claimed（本次 done 已写完 DONE 并推进到 done/in_review，
                # 如超时异常后重试、并发写入）才复用该 DONE 返回成功语义而非 E014。
                # 若任务仍处于 claimed，即便 ledger 存在上一次 attempt 的旧 DONE
                # （rework 场景），本次 verify 失败也是真实的 → 必须 E014，避免
                # 误复用上一次 DONE 掩盖真实失败（2026-08-12 实踩：rework 误触发）。
                fresh_state = store.replay()
                cur_ts = fresh_state.get(task_id)
                if cur_ts is not None and cur_ts.status != "claimed":
                    # 注意：此处不传 derived——必须实时扫描 ledger。verify 期间
                    # 并发落地的 DONE 不在 done 开头的缓存内（H2 缓存仅覆盖
                    # claim/request/review_submit 等静态查询场景）。
                    prior_done = _find_last_done_event(store, task_id)
                else:
                    prior_done = None
                if prior_done is not None:
                    return {
                        "done": True,
                        "task_id": task_id,
                        "status": "done",
                        "attempt_count": prior_done.get("attempt_count", ts.attempt_count + 1),
                        "note": "verify_failed_but_done_already_written",
                        "ledger_timestamp": prior_done.get("timestamp"),
                        "event_id": prior_done.get("event_id"),
                        "verify": {
                            "returncode": result.returncode,
                            "elapsed_seconds": elapsed,
                        },
                    }
                raise OrchdError(
                    ErrorCode.E014,
                    f"verify_command_failed: exit code {result.returncode} after {elapsed}s",
                    [{
                        "command": verify_cmd,
                        "returncode": result.returncode,
                        "elapsed_seconds": elapsed,
                        "stderr": _decode_subprocess_output(result.stderr)[:500],
                        "stdout": _decode_subprocess_output(result.stdout)[:300],
                        "hint": (
                            "verify 失败可能因断言不匹配或环境问题；若为 pytest 超长执行，"
                            "按 SKILL.md 自检约定改用模块定向 verify_command"
                        ),
                    }],
                )
        except subprocess.TimeoutExpired as exc:
            elapsed = round(_time.monotonic() - started, 1)
            partial_out = _decode_subprocess_output(
                (exc.stdout or b"")[:300] if hasattr(exc, "stdout") else b""
            )
            # 超时同理：仅当本次 attempt 的 DONE 已实际落地（任务离开 claimed）
            # 才复用旧 DONE 做假失败消除；rework 场景（仍 claimed + 旧 DONE 存在）
            # 是真实超时失败 → E014（2026-08-12 rework 误触发修复）。
            fresh_state = store.replay()
            cur_ts = fresh_state.get(task_id)
            if cur_ts is not None and cur_ts.status != "claimed":
                # 实时扫描（不传 derived）：超时期间并发落地的 DONE 必须可见
                prior_done = _find_last_done_event(store, task_id)
            else:
                prior_done = None
            if prior_done is not None:
                return {
                    "done": True,
                    "task_id": task_id,
                    "status": "done",
                    "attempt_count": prior_done.get("attempt_count", ts.attempt_count + 1),
                    "note": "timeout_but_done_already_written",
                    "ledger_timestamp": prior_done.get("timestamp"),
                    "event_id": prior_done.get("event_id"),
                    "verify": {
                        "timeout": verify_timeout,
                        "elapsed_seconds": elapsed,
                        "partial_stdout": partial_out,
                    },
                }
            raise OrchdError(
                ErrorCode.E014,
                f"verify_command_failed: timeout after {verify_timeout}s"
                f"（实际执行 {elapsed}s，未完成）",
                [{
                    "command": verify_cmd,
                    "timeout": verify_timeout,
                    "elapsed_seconds": elapsed,
                    "partial_stdout": partial_out,
                    "hint": (
                        "verify 超时通常是全量 pytest 累计耗时超上限：按 SKILL.md 自检约定"
                        "改用模块定向 verify_command（python -m pytest tests/test_<模块>.py "
                        "--basetemp=...），并确认测试不触发沙箱拦截"
                    ),
                }],
            )

    # 锁外 best-effort 自动提交（verify 通过后、写 DONE 事件前）：
    # 提交范围限定任务声明的 files_to_edit，失败/跳过不影响状态机。
    commit_result: dict[str, Any] | None = None
    if project_root:
        files_to_edit = task_def.get("files_to_edit", [])
        if files_to_edit:
            commit_message = changes_description or task_def.get(
                "name", f"orchd: done {task_id}"
            )
            commit_result = ensure_committed(project_root, files_to_edit, commit_message)

    # L1 干净校验（自动提交后）：files_to_edit 范围内改动已被引擎提交，
    # 此时若仍有已跟踪改动 = 实现者改了范围外文件（或提交失败残留），
    # 拒绝写完成事件，避免把范围外改动/脏状态带入 DONE。
    if project_root:
        _guard_write_command(
            project_root,
            allowed_branches=None,
            require_clean=True,
            command="done",
            orchd_dir=store.orchd_dir,
            agent_id=agent_id,
        )

    # 锁内二次校验 + 写事件
    store.acquire_lock()
    try:
        state = store.replay()
        ts = state.get(task_id)
        if not ts or ts.status != "claimed" or ts.claimed_by != agent_id:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: TOCTOU - task state changed during verify",
                [{"task_id": task_id}],
            )

        attempt_count = ts.attempt_count + 1
        done_event = _make_event(
            task_id, agent_id, "DONE",
            changes_description=changes_description,
            attempt_count=attempt_count,
        )
        if concerns:
            done_event["concerns"] = concerns
        if verify_record is not None:
            done_event["verify"] = verify_record
        store.append_event(done_event)

        # 自动 REVIEW_READY：文档类单阶段（跳过 spec 直接 code 终审，Q2 分级），
        # 常规任务双阶段（spec → code）。C5：blocked 集合从 master config 读取
        # （doc_single_stage_blocked），缺省回退硬编码集合。
        blocked_config = _load_config_blocked(store)
        review_type = "code" if _is_doc_single_stage(
            task_def.get("files_to_edit", []), blocked=blocked_config
        ) else "spec"
        review_event = _make_event(task_id, agent_id, "REVIEW_READY", review_type=review_type)
        store.append_event(review_event)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    result: dict[str, Any] = {
        "done": True,
        "task_id": task_id,
        "status": "done",
        "attempt_count": attempt_count,
        "review_created": {"type": review_type},
        "event_id": done_event["event_id"],
    }
    if commit_result is not None:
        result["commit"] = commit_result

    # L3 pre-commit hook 卸载（best-effort，锁外）
    if project_root:
        hook_uninstall(project_root)

    # L258：done 成功后释放 session lock（校验锁 agent_id == done agent，
    # 防误释放他人锁；best-effort 不阻断 done 结果）
    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    return result


# ------------------------------------------------------------------
# review_submit（写操作，锁内）
# ------------------------------------------------------------------


def _extract_review_baseline(
    store: Store, task_id: str, agent_id: str, derived: TaskDerived | None = None
) -> str | None:
    """从最近的 REVIEW_CLAIMED 事件提取 baseline_sha（用于漂移检测）。

    Args:
        store: 事件存储。
        task_id: 任务 ID。
        agent_id: 审查者 ID。
        derived: H2 缓存（2026-08-13 性能审核）——传入时直接查缓存 O(1)。

    Returns:
        baseline_sha（str）或 None（事件不存在 / 缺少字段 / 非 git 环境）。
    """
    if derived is not None:
        return derived.review_baselines.get((task_id, agent_id))
    try:
        events = store._read_ledger_lines(from_line=1)
    except Exception:
        return None

    # 反向遍历找最近的 REVIEW_CLAIMED（匹配 task_id + agent_id）
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
    review_type: str,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """提交审查结果（APPROVED 或 CHANGES_REQUESTED）。

    锁内校验：任务必须处于 in_review 状态，审查阶段（spec/code）须匹配，
    且审查者须为当前 agent。

    spec 审查 APPROVED 时，自动追加 REVIEW_READY(code) 事件，
    将任务无缝推进到 code review 阶段，无需人工再次触发。
    code 审查 APPROVED 时，先执行锁外 git merge：merge 成功才写
    REVIEW_SUBMITTED 完成事件（任务 completed）；merge 冲突则不写
    完成事件（任务停留 in_review，审查记录不落地），解决冲突后由
    同一 reviewer 重试提交。环境不支持 merge（非 git 仓库/异常）
    按 best-effort 降级标记完成。CHANGES_REQUESTED 时任务回退 pending。
    """
    # L2 session 锁（best-effort）
    _guard_write_command(
        project_root,
        allowed_branches=None,
        require_clean=False,
        command="review",
        orchd_dir=store.orchd_dir,
        agent_id=agent_id,
    )

    store.acquire_lock()
    try:
        state = store.replay()
        # H2（2026-08-13）：单次扫描派生缓存，供漂移检测 baseline 查询复用
        derived = store.scan_task_derived()
        ts = state.get(task_id)

        # E007: 必须 in_review 且审查类型匹配
        if not ts or ts.status != "in_review":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: task '{task_id}' not in_review",
                [{"task_id": task_id, "actual": ts.status if ts else "pending"}],
            )
        if ts.review_phase != review_type:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review phase mismatch (expected '{ts.review_phase}', got '{review_type}')",
                [{"task_id": task_id, "expected_phase": ts.review_phase}],
            )
        if ts.review_claimed_by != agent_id:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: review not claimed by '{agent_id}' (claimed by '{ts.review_claimed_by}')",
                [{"task_id": task_id, "claimed_by": ts.review_claimed_by, "agent": agent_id}],
            )

        # R1: baseline 漂移检测（非阻塞 warning）
        baseline_sha = _extract_review_baseline(store, task_id, agent_id, derived)
        current_sha = get_head_commit(project_root) if project_root else None
        baseline_drift = False
        if baseline_sha and current_sha and baseline_sha != current_sha:
            baseline_drift = True

        event = _make_event(
            task_id, agent_id, "REVIEW_SUBMITTED",
            review_type=review_type,
            verdict=verdict,
        )
        if comments:
            event["comments"] = comments

        result: dict[str, Any] = {
            "submitted": True,
            "task_id": task_id,
            "review_type": review_type,
            "verdict": verdict,
        }

        if baseline_drift:
            result["baseline_warning"] = (
                f"baseline drift detected: task branch HEAD changed during review "
                f"(claimed at {baseline_sha[:7]}, now {current_sha[:7]}). "
                f"Review may be based on outdated code."
            )

        # code APPROVED 的完成事件延迟到锁外 merge 成功后再写
        pending_code_event: dict[str, Any] | None = None

        if verdict == "APPROVED" and review_type == "spec":
            # 自动进入 code review
            store.append_event(event)
            code_ready = _make_event(task_id, agent_id, "REVIEW_READY", review_type="code")
            store.append_event(code_ready)
            result["task_status"] = "in_review"
            result["next_review"] = "code"
            new_state = store.replay()
            store.update_checkpoint(new_state)

        elif verdict == "APPROVED" and review_type == "code":
            # merge 前置：merge 成功才写完成事件；冲突则停留 in_review 不写
            result["task_status"] = "in_review"
            pending_code_event = event

        elif verdict == "CHANGES_REQUESTED":
            store.append_event(event)
            result["task_status"] = "pending"
            result["back_to_pool"] = True
            new_state = store.replay()
            store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    # git merge（锁外 best-effort）：code APPROVED 时 merge 成功才写完成事件
    if pending_code_event is not None:
        merge_result = None
        if project_root:
            merge_result = _try_git_merge(project_root, task_id)

        auto_resolved = False
        conflict_files: list[str] = []
        if merge_result is not None and merge_result.get("conflict"):
            # L3 自动化解：abort 恢复 main → 分支 merge main 预演 → 自动合并或返回清单
            auto = _try_auto_resolve_conflict(project_root, task_id) if project_root else None
            if auto and auto.get("resolved"):
                auto_resolved = True
            else:
                conflict_files = (auto or {}).get("conflict_files") or merge_result.get("files", [])
                result["merged"] = False
                result["reason"] = "merge_conflict"
                result["conflict_files"] = conflict_files
                result["task_status"] = "in_review"
                result["action"] = (auto or {}).get("action") or (
                    f"merge 冲突已发生（main 已恢复）：请在 task 分支 task/{task_id} "
                    f"上执行 git merge main 解决冲突并提交，然后由同一 reviewer "
                    f"重试 code APPROVED"
                )

        if not (merge_result is not None and merge_result.get("conflict")) or auto_resolved:
            # merge 成功 / 自动化解成功 / 环境不支持（None，best-effort 降级不卡状态机）
            store.acquire_lock()
            try:
                # 锁外 merge 窗口（最长 40s）内任务状态可能被 retract/force-status
                # 等改写：写完成事件前二次校验（对齐 done 的 verify 二次校验模式）
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
                store.release_lock()
            if result.get("reason") != "state_changed_during_merge":
                if merge_result is None:
                    result["merged"] = None
                    result["merge_warning"] = (
                        "git merge 未执行（非 git 仓库或 git 不可用），"
                        "按 best-effort 语义标记完成"
                    )
                else:
                    result["merged"] = True
                    if auto_resolved:
                        result["auto_resolved"] = True
                        result["action"] = (
                            f"merge 冲突已自动化解（abort + 分支预演合并），"
                            f"任务已 completed"
                        )

    # L258 对称修复：review_submit 完成后释放 session lock
    # （校验锁 agent_id == review agent，防误释放他人锁；best-effort 不阻断审查结果）
    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    return result


def _try_auto_resolve_conflict(
    project_root: Path, task_id: str
) -> dict[str, Any] | None:
    """L3：merge 冲突自动化解——恢复 main → 分支 merge main 预演 → 自动合并或返回清单。

    流程：
    1. git merge --abort（恢复 main，清除 _try_git_merge 留下的 MERGE_HEAD 中间态）
    2. git checkout task/{id} + git merge main（分支预演；无冲突自动生成 merge commit）
    3. git checkout main + git merge task/{id}（main 侧合并，此时应干净/fast-forward）

    Returns:
        {"resolved": True}：自动化解成功（main 已含任务分支实现）。
        {"resolved": False, "conflict_files": [...], "action": "..."}：仍需人工解决。
        None：git 环境异常（调用方按 best-effort 降级处理）。
    """
    def run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    def parse_conflicts(output: str) -> list[str]:
        files = []
        for line in output.split("\n"):
            if "CONFLICT" in line:
                parts = line.split()
                if parts:
                    files.append(parts[-1])
        return files

    try:
        # 1. 恢复 main（若处于 merge 中间态）
        run("merge", "--abort")
        # 2. 切到任务分支，预演 merge main
        co = run("checkout", f"task/{task_id}")
        if co is None or co.returncode != 0:
            return None
        pre = run("merge", "main")
        if pre is None:
            return None
        if pre.returncode != 0:
            # 分支上冲突：abort 恢复分支干净，返回冲突清单交实现者
            run("merge", "--abort")
            files = parse_conflicts(pre.stdout or "")
            return {
                "resolved": False,
                "conflict_files": files,
                "action": (
                    f"分支 task/{task_id} 与 main 合并冲突：请在 task 分支上执行 "
                    f"git merge main 解决冲突并提交（{len(files) or '若干'} 个文件），"
                    f"然后由同一 reviewer 重试 code APPROVED"
                ),
            }
        # 3. 分支已含 main（merge 成功自动提交）→ 切回 main 合并任务分支
        co2 = run("checkout", "main")
        if co2 is None or co2.returncode != 0:
            return None
        final = run("merge", f"task/{task_id}")
        if final is None:
            return None
        if final.returncode != 0:
            files = parse_conflicts(final.stdout or "")
            return {
                "resolved": False,
                "conflict_files": files,
                "action": (
                    f"main 与任务分支合并仍冲突（{len(files) or '若干'} 个文件），"
                    f"请人工处理"
                ),
            }
        return {"resolved": True}
    except Exception:
        return None


# ------------------------------------------------------------------
# retract（写操作，锁内）
# ------------------------------------------------------------------


def retract(
    store: Store,
    agent_id: str,
    target_event_id: str,
    reason: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """撤回事件（级联）。

    找到目标事件后，自动撤回该事件及其后同 task_id 的所有后续事件（级联撤回）。
    注意：FORCE_STATUS 事件不可撤回——它是管理员强制操作，具有不可逆语义，
    若需修正应再次调用 force_status 而非 retract。
    """
    store.acquire_lock()
    try:
        # 读取全部事件找 target
        all_events = store._read_ledger_lines(from_line=1)
        target_event = None
        for ev in all_events:
            if ev.get("event_id") == target_event_id:
                target_event = ev
                break

        if target_event is None:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: event '{target_event_id}' not found",
                [{"event_id": target_event_id}],
            )
        if target_event.get("type") == "FORCE_STATUS":
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: cannot retract FORCE_STATUS event",
                [{"event_id": target_event_id, "type": "FORCE_STATUS"}],
            )

        # 找级联事件（同 task_id，在 target 之后的事件）
        task_id = target_event.get("task_id", "")
        target_idx = next(i for i, ev in enumerate(all_events)
                         if ev.get("event_id") == target_event_id)
        cascade_ids = [target_event_id]
        for ev in all_events[target_idx + 1:]:
            if ev.get("task_id") == task_id and ev.get("type") != "RETRACT":
                cascade_ids.append(ev.get("event_id"))

        # 逐条写 RETRACT
        retracted_events = []
        for eid in cascade_ids:
            retract_ev = _make_event(
                task_id, agent_id, "RETRACT",
                target_event_id=eid,
                reason=reason,
            )
            store.append_event(retract_ev)
            retracted_events.append(eid)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    # L3 pre-commit hook 卸载（best-effort，锁外）
    if project_root:
        hook_uninstall(project_root)

    return {
        "retracted": True,
        "retracted_events": retracted_events,
        "task_id": task_id,
        "new_status": new_state.get(task_id, TaskState()).status,
    }


# ------------------------------------------------------------------
# force_status（写操作，锁内）
# ------------------------------------------------------------------


def force_status(
    store: Store,
    agent_id: str,
    task_id: str,
    target_status: str,
    reason: str,
    assignee: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """强制设置任务状态。

    受两层约束：
      1. 目标状态必须在 _FORCE_TARGETS 集合内（pending / claimed / completed / cancelled）。
      2. "允许从"矩阵：每个目标状态仅允许从特定源状态跳转，
         例如 claimed 只能从 pending 强制，completed 只能从 in_review 强制，
         cancelled 可从 pending / claimed / done / in_review 强制。
    这避免了不合逻辑的状态跳转（如从 pending 直接强制到 completed 跳过审查）。

    逃生口（M-3，2026-08-12）：矩阵外的两条"合理但不鼓励"跳转——
    claimed→completed（弃坑但功能已完成）与 cancelled→pending（误取消复活）——
    仅当 ``force=True``（CLI ``--force`` 显式二次确认）时放行；否则仍抛 E007。
    ``force`` 对白名单之外的非法跳转不生效，防止绕过正常审查/终态保护。
    """
    if target_status not in _FORCE_TARGETS:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: cannot force to '{target_status}' (legal: {sorted(_FORCE_TARGETS)})",
            [{"target": target_status, "legal_targets": sorted(_FORCE_TARGETS)}],
        )
    if target_status == "claimed" and not assignee:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_state: force to 'claimed' requires --assignee",
            [{"target": "claimed", "missing": "assignee"}],
        )

    store.acquire_lock()
    try:
        state = store.replay()
        ts = state.get(task_id)
        current = ts.status if ts else "pending"

        # "允许从" 校验矩阵
        _ALLOWED_FROM = {
            "pending": {"claimed", "done", "in_review", "completed"},
            "claimed": {"pending"},
            "completed": {"in_review"},
            "cancelled": {"pending", "claimed", "done", "in_review"},
        }
        allowed = _ALLOWED_FROM.get(target_status, set())
        if current not in allowed:
            in_hatch = (target_status, current) in _FORCE_ESCAPE_HATCHES
            if force and in_hatch:
                pass  # 显式逃生口：放行
            else:
                hint = (
                    "；如需走逃生口（claimed→completed / cancelled→pending）请加 --force"
                    if in_hatch
                    else ""
                )
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: cannot force '{task_id}' from '{current}' to '{target_status}'"
                    f" (allowed from: {sorted(allowed)}){hint}",
                    [{"task_id": task_id, "current": current, "target": target_status,
                      "allowed_from": sorted(allowed)}],
                )

        event = _make_event(
            task_id, agent_id, "FORCE_STATUS",
            target_status=target_status,
            reason=reason,
        )
        if assignee:
            event["assignee"] = assignee
        store.append_event(event)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    return {
        "forced": True,
        "task_id": task_id,
        "previous_status": current,
        "new_status": target_status,
        "reason": reason,
    }


# ------------------------------------------------------------------
# Git 辅助（best-effort）
# ------------------------------------------------------------------


def _try_git_branch(project_root: Path, task_id: str) -> None:
    """best-effort 切换到任务分支。

    返工场景（审查打回后重新 claim）分支已存在，直接 checkout 复用；
    首次 claim 才 checkout -b 新建。复用分支后，若分支工作区的
    .orchd/_master.json 落后 main（main 上 amend 修正的任务定义不进入
    已存在分支），自动同步为 main 版本并 best-effort 提交，避免 done
    读旧定义（task-conventions-sync 实踩 E014）。非 git 仓库或任何异常
    静默降级。
    """
    branch = f"task/{task_id}"
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if check.returncode == 0:
            checkout = subprocess.run(
                ["git", "checkout", branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if checkout.returncode == 0:
                _sync_master_with_main(project_root, branch)
        else:
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


def _sync_master_with_main(project_root: Path, branch: str) -> None:
    """分支工作区 .orchd/_master.json 落后 main 时同步为 main 版本（best-effort）。

    复用分支场景：main 上 amend 修正的任务定义不会自动进入已存在分支，
    done 会读到旧定义（旧 verify_command）导致 E014。检测分支工作区 master
    与 main 的差异，落后则 ``git checkout main --`` 同步（仅限该一个路径，
    不触碰实现者其他改动）并 best-effort 提交，失败/无差异静默跳过。
    """
    master_rel = str(Path(".orchd") / "_master.json")
    # 无 main 分支（如独立仓库/测试环境）时跳过，避免 git 报错
    has_main = subprocess.run(
        ["git", "rev-parse", "--verify", "main"],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    ).returncode == 0
    if not has_main:
        return
    diff = subprocess.run(
        ["git", "diff", "--quiet", "main", "--", master_rel],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if diff.returncode == 0:
        return  # 与 main 一致，零操作
    subprocess.run(
        ["git", "checkout", "main", "--", master_rel],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    ensure_committed(
        project_root,
        [master_rel],
        f"chore(claim): sync {branch} master with main",
    )


def _try_git_merge(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """best-effort 将任务分支合并到 main。

    流程：checkout main → merge task/{id}。
    合并成功返回 {"conflict": False}；
    合并冲突时解析 git stdout 中含 "CONFLICT" 的行，提取冲突文件路径，
    返回 {"conflict": True, "files": [...]}；
    checkout main 失败（脏工作区/非 git 仓库/无 main 分支）、merge 失败但
    stdout 无 "CONFLICT" 标记（环境问题而非内容冲突）、或任何异常返回 None
    （静默降级）。

    注意：merge 结果由调用方决定提示方式。merge 前置化后，conflict 表示
    任务尚未完成（停留 in_review，可解决后重试）；None 表示环境不支持，
    调用方按 best-effort 降级标记完成。
    """
    try:
        checkout = subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if checkout.returncode != 0:
            # 无法切到 main（脏工作区/非 git 仓库/无 main 分支）：环境问题，非冲突
            return None
        result = subprocess.run(
            ["git", "merge", f"task/{task_id}"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            # 解析冲突文件：仅 stdout 含 "CONFLICT" 才算内容冲突；
            # 否则（git fatal、非 git 仓库等）视为环境失败，静默降级
            conflict_files = []
            for line in result.stdout.split("\n"):
                if "CONFLICT" in line:
                    parts = line.split()
                    if parts:
                        conflict_files.append(parts[-1])
            if conflict_files:
                return {"conflict": True, "files": conflict_files}
            return None
        return {"conflict": False}
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
