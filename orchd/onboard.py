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
import sys
import time
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    ensure_committed,
    get_default_branch,
    hook_install,
    hook_uninstall,
    session_lock_check,
    session_lock_release,
)
from orchd.ledger import Store, TaskDerived, TaskState
from orchd.pool import (
    _build_claimed_files,
    build_pool,
    detect_file_conflict,
    effective_importance,
    get_dependency_closure,
    sort_candidates,
)

# 子域外置（task-refactor-onboard-domain-split）：
# - 共享辅助（guard/event/decoder/session-lock）→ orchd.gitops_ops
# - review 子域 → orchd.review
# - git 写子域 → orchd.gitops_ops
# 此处 re-export 保持「from orchd.onboard import X」与 monkeypatch 旧路径兼容。
from orchd.gitops import get_head_commit  # noqa: E402  re-export for monkeypatch
from orchd.gitops_ops import (  # noqa: E402  re-exports (backward compat)
    decode_subprocess_output as _decode_subprocess_output,
    ensure_session_lock as _ensure_session_lock,
    guard_write_command as _guard_write_command,
    make_event as _make_event,
    now_iso as _now_iso,
    sync_master_with_main as _sync_master_with_main,
    try_auto_resolve_conflict as _try_auto_resolve_conflict,
    try_delete_task_branch as _try_delete_task_branch,
    try_git_branch as _try_git_branch,
    try_git_merge as _try_git_merge,
    verify_output_summary as _verify_output_summary,
)
from orchd.review import (  # noqa: E402  re-exports (backward compat)
    extract_last_done as _extract_last_done,
    extract_review_baseline as _extract_review_baseline,
    extract_review_comments as _extract_review_comments,
    find_last_done_event as _find_last_done_event,
    request_reviewer as _request_reviewer,
    review_submit as review_submit,
)

# force-status 合法目标：force_status() 只允许将任务强制设置到这四种状态，
# 其余状态（如 in_review、done）由正常事件流驱动，不可被强制跳转。
_FORCE_TARGETS = {"pending", "claimed", "completed", "cancelled"}


def _is_fingerprint_agent_id(agent_id: str) -> bool:
    """判断 agent_id 是否为指纹形态身份（12 位 hex，task-fp-identity-engine）。

    稳定身份指纹（``orchd.ledger.resolve_agent_id``）为 12 位 SHA-256 短哈希。
    指纹身份由引擎自动识别，无法预写入静态 reviewers 名单，
    故在 E007 名单校验中豁免（审查独立性仍由 E016 防自审 + E011 忙度兜底）。
    """
    if not agent_id or len(agent_id) != 12:
        return False
    try:
        int(agent_id, 16)
        return True
    except ValueError:
        return False

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
# 方案 C（task-full-regression-gate）：全量 pytest 冒烟超时上限（秒）。
# 全量套件不含慢用例时约 35s，预留足够余量；失败仅告警不阻断 done。
_FULL_REGRESSION_TIMEOUT = 300


# 默认分解指南：当项目 docs/decomposition-guide.md 不存在时，
# bootstrap() 使用此内置文本作为 guide 字段，确保 LLM 始终有基本的分解原则可参考。
_FALLBACK_GUIDE = (
    "分解指南文件缺失。基本原则：任务粒度 0.5-6h，每个任务 1-4 个文件，"
    "2-5 条可量化验收标准，依赖链 ≤ 4 层，测试文件列入 files_to_edit。"
)


def _resolve_resource_root(project_root: Path) -> Path:
    """解析引擎资源根（schema / templates / docs 所在目录，task-12-engine-path-abstraction）。

    双态兼容：
    - 根布局（开发态）：资源在项目根（``project_root/schema``）→ 返回 ``project_root``。
    - 发布态布局（自包含 ``.orchd/``）：资源归置 ``.orchd/``（``.orchd/schema``）→
      返回 ``project_root / ".orchd"``。
    判定以 ``schema/`` 是否存在为准；两者都无 → 回退 ``project_root``
    （保持既有 E001「缺失报错」语义，缺失时由调用方抛错）。
    """
    project_root = Path(project_root)
    if (project_root / "schema").is_dir():
        return project_root
    orchd_dir = project_root / ".orchd"
    if (orchd_dir / "schema").is_dir():
        return orchd_dir
    return project_root


def bootstrap(project_root: Path | None = None) -> dict[str, Any]:
    """输出分解套件 JSON：schema + prompt + guide + next_step。

    不调用 LLM、不访问网络、不写文件。
    注意：此函数仅读取项目静态文件（schema / templates / docs），
    不要求 .orchd/ 目录已存在——它可在项目初始化（orchd init）之前安全调用。

    资源根双态兼容（task-12-engine-path-abstraction，AC1）：
    开发态在项目根（``schema/`` 等），发布态在 ``.orchd/``（引擎资源归置）。

    Raises:
        OrchdError E001: schema 或 architect.md 缺失。
    """
    if project_root is None:
        project_root = _find_project_root()
    resource_root = _resolve_resource_root(project_root)

    schema_path = resource_root / "schema" / "_master.schema.json"
    prompt_path = resource_root / "templates" / "architect.md"
    guide_path = resource_root / "docs" / "decomposition-guide.md"

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
    """从 cwd 向上逐层搜索项目根目录。

    搜索策略：依次检查 cwd → cwd.parent → cwd.parent.parent → …，
    返回第一个含有 ``.orchd/`` 目录的祖先路径（task-12-engine-path-abstraction，
    AC2：按 .orchd/ 定位项目根，不再依赖根含 schema/——发布态自包含 .orchd/
    工作空间同样命中）；若所有祖先均不匹配则回退到 cwd 本身。
    这使得在子目录中运行 CLI 也能正确定位到项目根。
    """
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".orchd").is_dir():
            return parent
    return cwd


# ------------------------------------------------------------------
# request（只读，无锁）
# ------------------------------------------------------------------


def _find_review_priority_tasks(
    store: Store, state: dict[str, TaskState], tasks: list[dict[str, Any]],
    agent_id: str, derived: TaskDerived | None = None,
    enforce_self_review_block: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """查找该 agent 可审查的 in_review 任务（用于 implementer request 时的优先调度提示）。

    返回 ``(review_tasks, excluded_self_review)`` 二元组：
    - ``review_tasks``：可分配的审查任务（按 spec 阶段优先排序）。
    - ``excluded_self_review``：自审任务列表。默认（enforce=False）仅标注、照常
      进入 review_tasks；线上版 enforce=True 时改为排除（仅标注、不分配）。

    过滤条件（task-fp-request-filter，指纹身份模型）：
    1. 任务状态 in_review 且审查未被他人认领（review_claimed_by is None）。
    2. reviewers 名单门禁仅作**向后兼容**：该字段存在且非空时，不在名单内
       直接排除（不计入自审）；字段缺失/为空（生产 _master.json 已无
       reviewers）则跳过名单门禁，仅按实现指纹去重。
    3. self-review：DONE 实现指纹 == 当前 request 指纹时，默认仅标注
       is_self_review（照常分配）；enforce_self_review_block=True 时归入
       excluded_self_review（不分配，AC1）。

    H2（2026-08-13）：``derived`` 为 request 单次扫描的派生缓存，
    循环内查询实现者改为 O(1)（原实现对每个候选任务全扫一次 ledger）。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    review_tasks: list[dict[str, Any]] = []
    excluded_self_review: list[dict[str, Any]] = []
    for tid, ts in state.items():
        if ts.status != "in_review" or ts.review_claimed_by is not None:
            continue
        task_def = task_map.get(tid, {})
        # 向后兼容：reviewers 字段存在且非空时仍按其门禁（旧契约/测试）。
        # 字段缺失或为空（指纹身份模型）则跳过名单门禁，仅按实现指纹去重。
        designated = task_def.get("reviewers")
        if designated and agent_id not in designated:
            continue
        # self-review：DONE 实现指纹 == 当前 request 指纹。
        # 默认仅标注 is_self_review 并照常分配；enforce=True 时归入
        # excluded_self_review（不分配，保 AC1 可见性）。
        done_author, _ = _extract_last_done(store, tid, derived)
        is_self = bool(done_author and done_author == agent_id)
        if is_self:
            excluded_self_review.append({
                "task_id": tid,
                "review_phase": ts.review_phase or "spec",
                "name": task_def.get("name", ""),
                "done_author": done_author,
                "is_self_review": True,
            })
        if is_self and enforce_self_review_block:
            continue
        entry = {
            "task_id": tid,
            "review_phase": ts.review_phase or "spec",
            "name": task_def.get("name", ""),
        }
        if is_self:
            entry["is_self_review"] = True
        review_tasks.append(entry)
    # spec 阶段优先
    review_tasks.sort(key=lambda c: (0 if c["review_phase"] == "spec" else 1))
    return review_tasks, excluded_self_review


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
    enforce_self_review_block: bool = False,
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
        return _request_reviewer(
            store, state, tasks, agent_id, derived,
            enforce_self_review_block=enforce_self_review_block,
        )

    # Review 优先调度：implementer 请求时，若有可认领的 review 任务，优先提示审查
    review_priority, excluded_self_review = _find_review_priority_tasks(
        store, state, tasks, agent_id, derived,
        enforce_self_review_block=enforce_self_review_block,
    )
    if review_priority:
        best_review = review_priority[0]
        rp_entry = {
            "task_id": best_review["task_id"],
            "review_phase": best_review["review_phase"],
            "name": best_review["name"],
            "total_available": len(review_priority),
        }
        if best_review.get("is_self_review"):
            rp_entry["is_self_review"] = True
        resp: dict[str, Any] = {
            "candidate": None,
            "review_priority": rp_entry,
            "message": (
                f"有 {len(review_priority)} 个待审查任务可领取，建议先完成审查再领取实现任务。"
                f"使用 'orchd request' 领取审查任务。"
            ),
            "next_action": "review_first",
            "pool_size": 0,
        }
        if excluded_self_review:
            resp["excluded_self_review"] = excluded_self_review
        return resp
    # 审查优先级为空：若全部 in_review 任务都是自审（被指纹比对排除），
    # 不返回 review_first，落回下方执行任务分配；excluded_self_review 仍标注（AC1）。

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
        result: dict[str, Any] = {
            "candidate": None,
            "message": message,
            "next_action": "exit",
            "pool_size": 0,
            "blocked_count": blocked_count,
            "reason": reason,
            "mismatched": mismatched,
            "excluded_conflicts": excluded_conflicts,
        }
        if excluded_self_review:
            result["excluded_self_review"] = excluded_self_review
        return result

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

    candidate_result: dict[str, Any] = {
        "candidate": candidate,
        "pool_size": len(candidates),
        "prompt": f"确认将此任务分配给 {agent_id}？(执行 / 跳过 / 重新声明能力)",
        "warnings": warnings,
        "excluded_conflicts": excluded_conflicts,
    }
    if excluded_self_review:
        candidate_result["excluded_self_review"] = excluded_self_review
    return candidate_result


def _is_high_risk(task_def: dict[str, Any]) -> bool:
    """高风险领域判定：任务触碰引擎核心（状态机分支 / CLI 契约 / 锁协议）。

    用于「共享上下文按需」：这类任务的实现默认自动附 conventions.md
    （编码规范 + 自检约定），降低越界/违规概率；architecture.md 不自动附，
    仅任务自身 files_to_read 显式引用或 --with-context 显式开启时提供。

    双态兼容（task-12-engine-path-abstraction，AC4）：files_to_edit 命中
    ``orchd/``（开发态根布局）或 ``.orchd/orchd/``（发布态自包含 .orchd 布局）
    均判高风险；``.orchd/_master.json`` 固定资产同样高风险。
    """
    module = task_def.get("module", "")
    if module == "mod-core":
        return True
    for f in task_def.get("files_to_edit", []):
        if (
            f.startswith("orchd/")
            or f.startswith(".orchd/orchd/")
            or f == ".orchd/_master.json"
        ):
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
    role: str | None = None,
    project_root: Path | None = None,
    shared: dict[str, Any] | None = None,
    review_type: str | None = None,
    with_context: bool = False,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    """认领任务。锁内 check-then-act。

    角色按任务当前状态自动分流（task-fp-identity-engine，CLI 不再传 --role）：
    in_review → reviewer（REVIEW_CLAIMED）；其他（pending）→ implementer（CLAIMED）。
    ``role`` 参数保留为可选（向后兼容既有调用方）；None 时按状态自动判定。

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

    # 角色分流：显式 role 优先；None 时按任务当前状态自动判定
    if role is None:
        pre_state = store.replay()
        pre_ts = pre_state.get(task_id)
        role = "reviewer" if (pre_ts and pre_ts.status == "in_review") else "implementer"

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
        # 红线 8（R3）：写命令前置校验运行时文件完整性（只读告警，不阻断）
        integrity_warnings = store.check_integrity()
        state = store.replay()
        # H2（2026-08-13）：单次扫描派生缓存，供 E016 校验与返回段
        # review_comments / done_event / previous_changes 复用（锁外同样
        # 有效——derived 是纯内存数据，无锁生命周期绑定）。
        derived = store.scan_task_derived()
        ts = state.get(task_id)
        status = ts.status if ts else "pending"
        # E016 降级标记：实现者审查自己实现时默认仅提示（见 reviewer 分支）
        is_self_review = False

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
            # 校验：只有任务指定的 reviewers 名单内的 agent 可认领审查。
            # E007 豁免（task-fp-identity-engine）：会话指纹形态 agent_id（12 位 hex）
            # 无法预写入静态 reviewers 名单，身份由引擎自动识别，故豁免名单校验
            # （审查独立性仍由 E016 防自审 + E011 忙度校验兜底）。
            designated = task_def.get("reviewers", [])
            if agent_id not in designated and not _is_fingerprint_agent_id(agent_id):
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
            # E016: self-review——实现者审查自己实现的任务。
            # 默认仅提示（enforce_self_review_block=False，单机模型）；线上版
            # 设 `_master.json config.enforce_self_review_block=true` 时恢复阻断。
            done_author, _ = _extract_last_done(store, task_id, derived)
            if done_author and done_author == agent_id:
                if enforce_self_review_block:
                    raise OrchdError(
                        ErrorCode.E016,
                        f"self_review_blocked: '{agent_id}' 是任务 '{task_id}' 的实现者，不能审查自己的实现",
                        [{"task_id": task_id, "agent_id": agent_id, "done_by": done_author,
                          "hint": "请使用其他 agent ID（如 reviewer-1）领取此审查任务，确保审查独立性"}],
                    )
                # 降级路径：仅提示、不阻断（self_review_notice 附到审查认领结果）
                is_self_review = True
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
        # 2026-08-15 e011-busy-lifecycle（worker-1 连领事故）：实现者 busy 判定从
        # claimed 扩展为 claimed/done/in_review——claimed_by 在 done/in_review 状态
        # 仍保留（DONE/REVIEW_READY 不清 claimed_by），任务未终态（completed/cancelled）
        # 或打回（CHANGES_REQUESTED→pending）前，实现者不得再领取新实现任务，
        # 否则会在上一任务审查/返工期间并发持有多个任务（无人值守 --auto-claim 连领）。
        for tid, t_state in state.items():
            # E011 实现者持有检查：默认（enforce=False）下仅"自审"（认领审查的恰是自己
            # 实现的那个任务）放行——允许单机模型下实现者自审；持有任务审查 *其它* 任务
            # 仍阻断；enforce=True 时自审也恢复阻断（与 E016 一致）。
            if (t_state.status in ("claimed", "done", "in_review")
                    and t_state.claimed_by == agent_id
                    and (tid != task_id or enforce_self_review_block)):
                raise OrchdError(
                    ErrorCode.E011,
                    f"agent_busy: '{agent_id}' already holds task '{tid}'"
                    f" (status={t_state.status})",
                    [{"agent_id": agent_id, "blocking_task": tid,
                      "blocking_status": t_state.status,
                      "hint": "任务完成审查（completed/cancelled）或被打回（pending）后才可领取新任务"}],
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
                            f"orchd retract --event {review_claim_event_id} "
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
        # E016 降级（默认提示）：实现者审查自己实现时附加提示，不阻断认领
        if is_self_review:
            review_claim_result["self_review_notice"] = {
                "message": "当前以实现者指纹认领审查自己实现的任务（E016 自审）",
                "done_by": done_author,
                "hint": "默认仅提示；线上版可设 _master.json config.enforce_self_review_block=true 恢复强制阻断",
                "enforce_self_review_block": enforce_self_review_block,
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

    result = {
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
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result


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
    - 不包含约定/状态文件：SKILL.md、.orchd/SKILL.md、
      .orchd/shared/conventions.md、.orchd/_master.json
      （这些属于"约定改变"，须保持双阶段审查；.orchd/SKILL.md 为发布态布局）

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
        blocked = {
            "SKILL.md",
            ".orchd/SKILL.md",
            ".orchd/shared/conventions.md",
            ".orchd/_master.json",
        }
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

    # 预校验（无锁，快速失败）：仅要求任务处于 claimed 状态，不比对 caller 指纹与
    # claimed_by——宿主会话身份漂移场景下，done 按认领者记账（见锁内 author_mismatch）。
    state = store.replay()
    ts = state.get(task_id)
    if not ts or ts.status != "claimed":
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not in claimed state",
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

    # 方案 C（task-full-regression-gate）：引擎改动 merge 前全量回归闸门。
    # files_to_edit 含 orchd/*.py（核心引擎）时，done verify 通过、自动提交后，
    # 锁外附加一次全量 pytest 冒烟，防止契约漂移在合并时静默通过。失败仅生成本
    # 次 DONE 的 full_regression 警告，不阻断 done、不改任务状态（对齐验收标准 2）。
    # 锁外执行（约 35s），避免长时间持锁；锁内二次校验仍兜底 TOCTOU。
    full_regression: dict[str, Any] | None = None
    files_to_edit = task_def.get("files_to_edit", [])
    if project_root and any(
        (f.startswith("orchd/") or f.startswith(".orchd/orchd/")) and f.endswith(".py")
        for f in files_to_edit
    ):
        reg_started = time.monotonic()
        try:
            # P2-1（2026-08-19 审查）：回归子进程继承当前解释器（sys.executable），
            # 避免裸 `python` 命中 PATH 上未装依赖的解释器导致误报。
            reg_cmd = (
                f'"{sys.executable}" -m pytest tests/ -q '
                f'--basetemp="${{TMPDIR:-/tmp}}/orchd-vf-$$"'
            )
            reg_result = subprocess.run(
                reg_cmd,
                shell=True, cwd=str(project_root),
                capture_output=True, timeout=_FULL_REGRESSION_TIMEOUT,
            )
            reg_elapsed = round(time.monotonic() - reg_started, 1)
            if reg_result.returncode == 0:
                full_regression = {
                    "ok": True,
                    "elapsed_seconds": reg_elapsed,
                    "output_summary": _verify_output_summary(reg_result.stdout, reg_result.stderr),
                }
            else:
                full_regression = {
                    "ok": False,
                    "code": "full_regression",
                    "severity": "warning",
                    "message": (
                        f"full_regression_failed: exit code {reg_result.returncode} "
                        f"after {reg_elapsed}s"
                    ),
                    "details": {
                        "command": f'"{sys.executable}" -m pytest tests/ -q',
                        "returncode": reg_result.returncode,
                        "elapsed_seconds": reg_elapsed,
                        "output_summary": _verify_output_summary(reg_result.stdout, reg_result.stderr),
                    },
                }
        except subprocess.TimeoutExpired as exc:
            reg_elapsed = round(time.monotonic() - reg_started, 1)
            partial_out = _decode_subprocess_output(
                (exc.stdout or b"")[:300] if hasattr(exc, "stdout") else b""
            )
            full_regression = {
                "ok": False,
                "code": "full_regression",
                "severity": "warning",
                "message": (
                    f"full_regression_timeout: after {reg_elapsed}s "
                    f"(timeout={_FULL_REGRESSION_TIMEOUT}s)"
                ),
                "details": {
                    "command": f'"{sys.executable}" -m pytest tests/ -q',
                    "timeout": _FULL_REGRESSION_TIMEOUT,
                    "elapsed_seconds": reg_elapsed,
                    "partial_stdout": partial_out,
                },
            }

    # 锁内二次校验 + 写事件
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        ts = state.get(task_id)
        if not ts or ts.status != "claimed":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: TOCTOU - task state changed during verify",
                [{"task_id": task_id}],
            )

        # 记账一律以任务认领者 claimed_by 为准，忽略 caller 当前指纹：
        # 宿主会话身份漂移时，caller 指纹与认领者不一致，仍正常放行并按认领者记账。
        claimed_by = ts.claimed_by
        author_mismatch = claimed_by != agent_id
        attempt_count = ts.attempt_count + 1
        done_event = _make_event(
            task_id, claimed_by, "DONE",
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
        review_event = _make_event(task_id, claimed_by, "REVIEW_READY", review_type=review_type)
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
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if author_mismatch:
        result["author_mismatch"] = {
            "claimed_by": claimed_by,
            "caller_fingerprint": agent_id,
            "message": (
                "done 已按任务认领者 claimed_by 记账；caller 指纹与认领者不一致"
                "（宿主会话身份可能漂移），仅提示、不阻断"
            ),
        }
    if full_regression is not None:
        result["full_regression"] = full_regression
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
        integrity_warnings = store.check_integrity()
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

    result = {
        "retracted": True,
        "retracted_events": retracted_events,
        "task_id": task_id,
        "new_status": new_state.get(task_id, TaskState()).status,
    }
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result


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
        integrity_warnings = store.check_integrity()
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

    result = {
        "forced": True,
        "task_id": task_id,
        "previous_status": current,
        "new_status": target_status,
        "reason": reason,
    }
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result
