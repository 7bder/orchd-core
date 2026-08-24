"""Orchd 就绪池构建、候选排序与文件冲突检测模块。

本模块提供以下核心功能：
- build_pool：就绪池构建，按状态→依赖→能力→排除的管线过滤出可执行候选任务。
- sort_candidates：候选任务按重要性（importance）及其他维度进行排序。
- detect_file_conflict：文件级写冲突检测，识别目标任务与活跃 claimed 任务之间的文件重叠。
- compute_downstream_blocked：统计每个任务被多少 pending 任务依赖，用于自动推导 importance。

依赖方向：pool.py → ledger.py / spec.py（不导入 onboard / cli）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orchd.ledger import Store, TaskState

# importance 权重映射
_IMPORTANCE_WEIGHT = {"critical": 4, "high": 3, "normal": 2, "low": 1}

# 共享核心文件集合（task-concurrency-hardening）：
# 多 agent 并行改动这些引擎/测试基础设施文件时冲突最密集（onboard/gitops/cli/
# errors/spec/pool/review/worktree/ledger + tests/*），且纯声明级 files_to_edit
# 重叠未必能反映（常因越界改动未声明而同文件）。摄入/注册 tip 将其冲突从
# warning 升级为强约束：非依赖的并行任务命中共享核心文件重叠即硬串行化
# （靠 request 依赖感知过滤 + claim E010 兜底），不允许静默并行。
SHARED_CORE_FILES = frozenset({
    "orchd/onboard.py", "orchd/gitops.py", "orchd/gitops_ops.py",
    "orchd/cli.py", "orchd/errors.py", "orchd/spec.py", "orchd/pool.py",
    "orchd/review.py", "orchd/worktree.py", "orchd/ledger.py",
    "orchd/split.py", "orchd/intake.py", "orchd/report.py",
})


def derive_importance(
    blocked_downstream_count: int, thresholds: dict[str, Any] | None = None
) -> str:
    """按下游阻塞数自动推导 importance。

    阈值可配置（_master.json ``config.importance``），默认：
    >=5 → critical；3-4 → high；1-2 → normal；0 → low。
    自定义阈值 dict 提供 critical/high/normal 三个下界（如
    {"critical": 8, "high": 5, "normal": 2}），缺省键回退默认值。
    """
    t = thresholds or {}
    critical_at = int(t.get("critical", 5))
    high_at = int(t.get("high", 3))
    normal_at = int(t.get("normal", 1))
    if blocked_downstream_count >= critical_at:
        return "critical"
    if blocked_downstream_count >= high_at:
        return "high"
    if blocked_downstream_count >= normal_at:
        return "normal"
    return "low"


def effective_importance(
    task: dict[str, Any],
    blocked_downstream_count: int,
    thresholds: dict[str, Any] | None = None,
) -> str:
    """任务生效的 importance：显式声明优先，缺省时按下游阻塞数推导。"""
    explicit = task.get("importance")
    if explicit:
        return explicit
    return derive_importance(blocked_downstream_count, thresholds)


@dataclass
class Candidate:
    """就绪池中的一条候选任务记录。

    每个 Candidate 包含 _master.json 中的完整任务定义（task）以及
    计算得到的下游阻塞任务数（blocked_downstream_count），供后续
    排序和冲突检测使用。

    Attributes:
        task: _master.json 中的完整任务定义字典。
        blocked_downstream_count: 被多少 pending 状态的下游任务依赖。
        rework: 是否为返工任务（pending 且 attempt_count > 0，即曾被审查打回）。
    """

    task: dict[str, Any]  # _master.json 中的完整任务定义
    blocked_downstream_count: int = 0
    rework: bool = False


@dataclass
class Conflict:
    """任务之间的文件级写冲突记录。

    当目标任务的 files_to_edit 与某个活跃 claimed 任务的同名文件
    存在交集时，即产生一条 Conflict 记录，用于阻止并发写入同一文件。

    Attributes:
        task_id: 产生冲突的被 claimed 任务 ID。
        files: 两个任务重叠的文件路径列表（已排序）。
        claimed_by: 持有该任务 claim 的 agent 标识。
    """

    task_id: str
    files: list[str]
    claimed_by: str
    # task-concurrency-hardening：重叠文件是否包含共享核心文件。摄入期据此把
    # 「共享核心文件并行」从软 warning 升级为强串行约束（非依赖即硬排除）。
    is_shared_core: bool = False


def build_pool(
    tasks: list[dict[str, Any]],
    state: dict[str, TaskState],
    capabilities: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[Candidate]:
    """构建就绪池。

    过滤管线按以下顺序依次执行，任一环节不通过则跳过该任务：
    1. 状态过滤：status == "pending"（缺失状态视为 pending）。
    2. 依赖放行：所有 depends_on 中任务的 status ∈ {completed, cancelled}。
    3. 能力过滤：若 agent 声明 capabilities，则 task.requires ⊆ capabilities。
    4. 排除列表：task_id 不在 exclude 列表中。

    Args:
        tasks: _master.json 中的 tasks[] 定义列表。
        state: Store.replay() 返回的任务状态字典。
        capabilities: agent 声明的能力列表；None 表示不过滤。
        exclude: 要排除的 task_id 列表。
    """
    exclude_set = set(exclude or [])

    # 预计算 blocked_downstream_count
    blocked_counts = compute_downstream_blocked(tasks, state)

    candidates: list[Candidate] = []
    for task in tasks:
        tid = task.get("id", "")

        # 排除
        if tid in exclude_set:
            continue

        # 状态过滤：缺失 = pending
        ts = state.get(tid)
        status = ts.status if ts else "pending"
        if status != "pending":
            continue

        # 依赖放行：所有 depends_on 的任务必须 completed 或 cancelled
        deps = task.get("depends_on", [])
        deps_satisfied = True
        for dep_id in deps:
            dep_ts = state.get(dep_id)
            dep_status = dep_ts.status if dep_ts else "pending"
            if dep_status not in ("completed", "cancelled"):
                deps_satisfied = False
                break
        if not deps_satisfied:
            continue

        # 能力过滤
        if capabilities is not None:
            requires = set(task.get("requires", []))
            if not requires.issubset(set(capabilities)):
                continue

        candidates.append(
            Candidate(
                task=task,
                blocked_downstream_count=blocked_counts.get(tid, 0),
                rework=bool(ts and ts.attempt_count > 0),
            )
        )

    return candidates


def sort_candidates(
    candidates: list[Candidate],
    sort_key: str | None = None,
    importance_thresholds: dict[str, Any] | None = None,
) -> list[Candidate]:
    """排序候选列表。

    sort_key:
        None / "default" → importance desc → blocked_downstream_count desc → estimated_hours asc
        "importance" → importance desc
        "downstream" → blocked_downstream_count desc
        "hours" → estimated_hours asc

    importance_thresholds: _master.json ``config.importance`` 自定义阈值
    （derive_importance 覆盖），None 用默认阈值。
    """
    if sort_key == "importance":
        return sorted(
            candidates, key=lambda c: _importance_key(c, importance_thresholds),
            reverse=True,
        )
    elif sort_key == "downstream":
        return sorted(candidates, key=lambda c: c.blocked_downstream_count, reverse=True)
    elif sort_key == "hours":
        return sorted(candidates, key=lambda c: c.task.get("estimated_hours", 0))
    else:
        # 默认复合排序：importance desc → rework（返工优先）→ blocked_downstream desc
        # → estimated_hours asc。rework 仅作同级内 tie-break，不跨 importance 层。
        return sorted(
            candidates,
            key=lambda c: (
                _importance_key(c, importance_thresholds),
                c.rework,
                c.blocked_downstream_count,
                -c.task.get("estimated_hours", 0),
            ),
            reverse=True,
        )


def detect_file_conflict(
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    target_task: dict[str, Any],
    claimed_files: dict[str, tuple[list[str], str]] | None = None,
    include_pending: bool = False,
) -> list[Conflict]:
    """检测目标任务的 files_to_edit 与活跃任务的冲突。

    include_pending=False（默认）：只比对活跃 claimed 任务（claim 时 E010 语义）。
    include_pending=True：额外比对池内 pending 任务的 files_to_edit（request 预检
    与摄入冲突规划用），pending 冲突的 claimed_by 标记为 "pending"，表示未认领。

    Args:
        state: 当前任务状态。
        tasks: 所有任务定义。
        target_task: 要检测的目标任务。
        claimed_files: 可选预计算的 {task_id: (files, claimed_by)} 映射。
        include_pending: 是否纳入 pending 任务比对。

    Returns:
        冲突列表。
    """
    target_files = set(target_task.get("files_to_edit", []))
    if not target_files:
        return []

    if claimed_files is None:
        claimed_files = _build_claimed_files(state, tasks, include_pending=include_pending)

    conflicts: list[Conflict] = []
    target_id = target_task.get("id", "")
    for tid, (files, claimed_by) in claimed_files.items():
        if tid == target_id:
            continue
        overlap = target_files & set(files)
        if overlap:
            conflicts.append(
                Conflict(
                    task_id=tid,
                    files=sorted(overlap),
                    claimed_by=claimed_by,
                    is_shared_core=bool(overlap & SHARED_CORE_FILES),
                )
            )
    return conflicts


def get_dependency_closure(
    task_id: str, tasks: list[dict[str, Any]]
) -> set[str]:
    """返回目标任务的全图依赖传递闭包（祖先 + 子孙）。

    依赖相关的任务对按依赖顺序执行（build_pool / claim 的 E008 依赖放行保证
    不会并行领取），files_to_edit 共享不构成并发写冲突。request 依赖感知强制
    过滤用它判定"与 pending 依赖任务冲突"是否应放行。

    Args:
        task_id: 目标任务 ID。
        tasks: 所有任务定义（_master.json 的 tasks[]）。

    Returns:
        与 task_id 存在直接或传递依赖关系的全部 task_id 集合（不含自身）。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    result: set[str] = set()
    # 向上（祖先）遍历
    seen: set[str] = set()
    stack = list(task_map.get(task_id, {}).get("depends_on", []))
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in task_map:
            continue
        seen.add(cur)
        stack.extend(task_map[cur].get("depends_on", []))
    result |= seen
    # 向下（子孙）遍历：谁直接或间接依赖 task_id
    seen_desc: set[str] = set()
    stack = [tid for tid, t in task_map.items() if task_id in t.get("depends_on", [])]
    while stack:
        cur = stack.pop()
        if cur in seen_desc or cur not in task_map:
            continue
        seen_desc.add(cur)
        stack.extend(
            tid for tid, t in task_map.items() if cur in t.get("depends_on", [])
        )
    result |= seen_desc
    return result


# ------------------------------------------------------------------
# 内部辅助
# ------------------------------------------------------------------


def _importance_key(
    c: Candidate, thresholds: dict[str, Any] | None = None
) -> int:
    """将候选任务映射为重要性整数权重，用于排序比较。

    内部调用 effective_importance() 获取任务的生效重要性标签，
    再通过 _IMPORTANCE_WEIGHT 映射为整数（critical=4, high=3, normal=2, low=1）。
    未识别的标签默认返回 2（normal）。
    """
    return _IMPORTANCE_WEIGHT.get(
        effective_importance(c.task, c.blocked_downstream_count, thresholds), 2
    )


def compute_downstream_blocked(
    tasks: list[dict[str, Any]], state: dict[str, TaskState]
) -> dict[str, int]:
    """统计每个任务被多少 pending 任务的 depends_on 引用。"""
    counts: dict[str, int] = {}
    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        status = ts.status if ts else "pending"
        if status != "pending":
            continue
        for dep_id in task.get("depends_on", []):
            counts[dep_id] = counts.get(dep_id, 0) + 1
    return counts


def _build_claimed_files(
    state: dict[str, TaskState],
    tasks: list[dict[str, Any]],
    include_pending: bool = False,
) -> dict[str, tuple[list[str], str]]:
    """从当前状态和任务定义推导活跃任务的文件映射。

    默认包含状态为 "claimed" 且持有者（claimed_by）非空的任务；
    include_pending=True 时额外纳入 pending 任务（claimed_by 标记为
    "pending"），供 request 预检 / 摄入冲突规划使用。

    Args:
        state: Store.replay() 返回的任务状态字典。
        tasks: _master.json 中的 tasks[] 定义列表。
        include_pending: 是否纳入 pending 任务。

    Returns:
        {task_id: (files_to_edit 列表, claimed_by agent 标识)} 的映射字典。
        其中 pending 任务的 claimed_by 为 "pending"。
    """
    task_map = {t.get("id", ""): t for t in tasks}
    result: dict[str, tuple[list[str], str]] = {}
    # claimed 任务（有 ledger 状态且持有者非空）
    for tid, ts in state.items():
        if ts.status == "claimed" and ts.claimed_by:
            task_def = task_map.get(tid, {})
            files = task_def.get("files_to_edit", [])
            result[tid] = (files, ts.claimed_by)
    # pending 任务（state 无记录或 status=pending）——pending 无 ledger 事件，
    # 必须遍历 tasks 定义补齐，否则空 ledger 下收集不到
    if include_pending:
        for task_def in tasks:
            tid = task_def.get("id", "")
            if tid in result:
                continue
            ts = state.get(tid)
            s = ts.status if ts else "pending"
            if s == "pending":
                result[tid] = (task_def.get("files_to_edit", []), "pending")
    return result
