"""orchd/guide/stage.py — 引导状态摘要与 step 构造域（W5-G2 拆分，纯搬移）。

来源：``orchd/guide/__init__.py``（起点 e3f21d0b 原 L625-1038 一带）的 L110-523：状态摘要
``_summarize`` / 分类 ``_classify`` / 首引与停止引导 / ``_derive`` 与三档 step 构造
（review / impl / terminal）。拆分记录见 design/file-split-program-20261015.md §4.6.2。

依赖方向：``stage`` → ``digest.py``（``_ENTRY_CMD``）+ ``paths.py``（``read_for`` /
``template_for``）+ ``contracts.events``（任务状态名真源）；不导入聚合入口（否则成加载期环）。
"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import (
    STATUS_CLAIMED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
)
from .digest import _ENTRY_CMD
from .paths import read_for, template_for


def _summarize(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
) -> dict[str, Any]:
    """从任务状态派生计数摘要（纯函数，不读文件）。

    以 master 任务定义（``tasks``）为准逐任务统计：任务在 ``state`` 中无记录
    视为 pending（尚未有任何事件）。避免"未认领的 pending 任务不在 state 中"
    而漏计。

    Args:
        state: ``Store.replay()`` 的产物（task_id -> TaskState）。
        tasks: master 任务定义列表（用于判定空项目与逐任务状态）。
        agent_id: 当前 agent 身份（可选；用于"claimed 属于当前 agent"的判定）。

    Returns:
        六状态计数 + 关键派生态（含 rework 返工任务数）。
    """
    counts: dict[str, Any] = {
        STATUS_PENDING: 0, STATUS_CLAIMED: 0, STATUS_DONE: 0,
        STATUS_IN_REVIEW: 0, STATUS_COMPLETED: 0, STATUS_CANCELLED: 0,
    }
    my_claimed: list[str] = []
    rework = 0
    first_rework_tid: str | None = None
    first_unclaimed_review: str | None = None
    first_unclaimed_review_phase: str | None = None
    # task-review-independence-enforce：焦点审查任务是否要求独立审查（claim_review
    # hint 显式指引换会话；step 词表不新增，hint 文案与 rules/review.md 同语义）。
    first_unclaimed_review_requires_independence = False
    # 本 agent 已领未提交的审查（task-guide-step-vocab-and-routing-fix）：原实现只
    # 计数 my_in_review 却不产出出口，导致 in_review 全被领走时引导指回 request。
    my_review_tid: str | None = None
    my_review_phase: str | None = None
    _task_map = {t.get("id", ""): t for t in tasks}
    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        s = ts.status if ts else STATUS_PENDING
        counts[s] = counts.get(s, 0) + 1
        if s == STATUS_PENDING and ts and ts.attempt_count > 0:
            rework += 1
            if first_rework_tid is None:
                first_rework_tid = tid
        if s == STATUS_CLAIMED and agent_id and ts and ts.claimed_by == agent_id:
            my_claimed.append(tid)
        if s == STATUS_IN_REVIEW:
            if agent_id and ts and ts.review_claimed_by == agent_id:
                counts["my_in_review"] = counts.get("my_in_review", 0) + 1
                if my_review_tid is None:
                    my_review_tid = tid
                    my_review_phase = ts.review_phase or "unified"
            if ts and ts.review_claimed_by is None and first_unclaimed_review is None:
                first_unclaimed_review = tid
                first_unclaimed_review_phase = ts.review_phase or "unified"
                if _task_map.get(tid, {}).get("require_independent_review"):
                    first_unclaimed_review_requires_independence = True
    counts["total"] = len(tasks)
    counts["my_claimed"] = my_claimed
    counts["rework"] = rework
    counts["first_rework_tid"] = first_rework_tid
    counts["first_unclaimed_review"] = first_unclaimed_review
    counts["first_unclaimed_review_phase"] = first_unclaimed_review_phase
    counts["first_unclaimed_review_requires_independence"] = (
        first_unclaimed_review_requires_independence)
    counts["my_review_tid"] = my_review_tid
    counts["my_review_phase"] = my_review_phase
    return counts


def _classify(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None,
    has_master: bool,
    review_mode: str = "two_phase",
) -> dict[str, Any]:
    """按状态机+角色推导「步骤 + 聚焦任务」（纯函数）。

    与 ``_derive`` 共用同一决策源（task-guide-transition-aware）：状态引导
    （下一步做什么）与转换感知引导（刚发生什么/现在在哪）都从这里取聚焦任务，
    保证两处永不漂移。返回 ``{"step": ..., "focus_tid": str | None}``。
    """
    c = _summarize(state, tasks, agent_id)
    if c["total"] == 0:
        return {
            "step": "empty_project" if has_master else "first_time",
            "focus_tid": None,
        }
    if c.get(STATUS_IN_REVIEW, 0) > 0:
        # 优先级（task-guidance-review-priority-fix）：**先收尾本 agent 已领未提交的审查，
        # 再谈领新审查**。原先 first_unclaimed_review 判定在前，导致只要池中存在任何未认领
        # 审查，submit_review 出口就永不可达——会话被引去领他任务的审查，而该 claim 会被
        # orchd/onboard/claim.py 的 E011 review busy 拒绝，手上已领的审查就此悬空未提交
        # （2026-09-13 实测 evt-cd8fa533，任务冻结在 in_review/code）。
        if c.get("my_review_tid"):
            # 本 agent 已领审查未提交 → 先给出可推进出口
            return {"step": "submit_review", "focus_tid": c["my_review_tid"]}
        if c.get("first_unclaimed_review"):
            return {"step": "claim_review", "focus_tid": c["first_unclaimed_review"]}
        # 审查已被他人领取：本 agent 无可做之事 → 等待（不再产出 request_review 死路）
        return {"step": "wait_review", "focus_tid": None}
    if c.get("rework", 0) > 0 and not c.get("my_claimed"):
        return {"step": "rework_first", "focus_tid": c.get("first_rework_tid")}
    if c[STATUS_PENDING] > 0 and not c.get("my_claimed"):
        # 忙度只看**本 agent** 的 claimed（原实现用全局 claimed，导致其他会话一旦有
        # 任务在实现，本 agent 永远拿不到 request_impl，与 request 的候选结论打架）
        return {"step": "request_impl", "focus_tid": None}
    if c.get("my_claimed"):
        return {"step": "done", "focus_tid": c["my_claimed"][0]}
    if c[STATUS_CLAIMED] > 0 or c[STATUS_DONE] > 0:
        return {"step": "wait_review", "focus_tid": None}
    if c[STATUS_CANCELLED] > 0:
        return {"step": "optional_cancel", "focus_tid": None}
    if c[STATUS_COMPLETED] > 0 and c[STATUS_COMPLETED] == c["total"]:
        return {"step": "done_all", "focus_tid": None}
    return {"step": "check_status", "focus_tid": None}


def first_time_guide(has_master: bool = False) -> dict[str, Any]:
    """首次引导：区分「未初始化」与「空项目」（task-guidance-dual-view-engine）。

    Args:
        has_master: False → 未初始化项目（无 ``_master.json``）：step=first_time，
            附 ``card`` 结构化字段（供接入层渲染 SVG 全貌卡片）；
            True → 空项目（有 master 但 0 任务）：step=empty_project。

    Returns:
        引导结构 {step, read, template, command, hint}；first_time 另附 card。
    """
    if has_master:
        return {
            "step": "empty_project",
            "read": read_for("empty_project"),
            "template": [],
            "command": f"{_ENTRY_CMD} idea propose --title '<灵感>' --feasibility '<论证>'",
            "hint": "项目已初始化但还没有任务：先 status 确认状态（validate 校验），"
                    "可提交新 idea 供拆解，或直接规划下一阶段。",
        }
    return {
        "step": "first_time",
        "read": read_for("first_time"),
        "template": [],
        "command": f"{_ENTRY_CMD} bootstrap",
        "hint": f"项目尚未初始化：先运行 {_ENTRY_CMD} bootstrap 获取任务分解套件，"
                f"再 {_ENTRY_CMD} init 初始化快照，之后即可 request/claim 领取任务。",
        "card": {
            "title": "Orchd 项目初始化引导",
            "phase": "first_time",
            "steps": ["bootstrap", "init", "request"],
            "current": 0,
            "next": "bootstrap",
        },
    }


def stop_wait_guidance() -> dict[str, Any]:
    """request 无候选时的停止引导（task-request-no-task-stop）。

    引擎未分配任务（candidate=None / next_action=exit|wait）时返回：agent 应
    停止，不得自行 claim / 重试 request / --auto-claim，等待用户下一条指令。
    command 为空（无可执行命令），hint 明确停止语义。
    """
    return {
        "step": "stop_wait",
        "read": read_for("stop_wait"),
        "template": [],
        "command": "",
        "hint": "引擎未分配任务：停止，不得自行 claim 或重试 request，等待用户下一条指令。",
    }


def lesson_review_guidance(task_id: str | None = None) -> dict[str, Any]:
    """done 收尾挂起（``next_action=await_review``）的引导（纯函数）。

    ``orchd/onboard/lifecycle/core.py`` 在 lessons ``require_review`` 时把 done 结果
    置 ``next_action="await_review"``，但该取值既不在 guidance 词表内、也无引导分支
    —— 本函数补上出口（经 :data:`NEXT_ACTION_TO_STEP` 映射），并顺带补上
    ``skill-lesson.md`` 的知识路由（此前零路由）。
    """
    tid = task_id or "<task_id>"
    return {
        "step": "lesson_review",
        "read": read_for("lesson_review"),
        "template": [],
        "command": f"{_ENTRY_CMD} lesson review --task {tid}",
        "hint": (
            f"任务 {tid} 已提交 done，但其 lessons 暂存建议待人工审核："
            f"先审暂存条目（可 --approve-all / --reject）再完成收尾。"
        ),
    }


def _derive(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None,
    has_master: bool,
    review_mode: str = "two_phase",
) -> dict[str, Any]:
    """按状态机+角色推导单视角引导（内部函数，返回纯 {step, read, template, command, hint}）。

    分发器：分类 + 空项目守卫后，按 step 类别委托三个子函数（review/impl/terminal）。
    子函数不匹配时返回 None，分发器依次尝试，最终兜底 check_status。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（None 表示未知/项目视角）。
        has_master: 是否已存在 ``_master.json``（区分未初始化/空项目）。
        review_mode: 审查模式（review-unify-r2）：``"unified"`` 单阶段
            （in_review 模板用 reviewer.md），``"two_phase"`` 双阶段
            （spec-reviewer.md + code-reviewer.md）。缺省 two_phase 向后兼容。

    Returns:
        纯 5 键引导结构；无任务时返回 first_time/empty_project 引导。
    """
    cls = _classify(state, tasks, agent_id, has_master, review_mode)
    step = cls["step"]
    c = _summarize(state, tasks, agent_id)

    # 空项目 / 无任务 → 首次引导（has_master 区分未初始化 vs 空项目）
    if step in ("first_time", "empty_project"):
        return first_time_guide(has_master=has_master)

    # 按 step 类别分发：review → impl → terminal（兜底）
    g = _review_step_guidance(step, c, review_mode)
    if g is not None:
        return g
    g = _impl_step_guidance(step, c, cls)
    if g is not None:
        return g
    return _terminal_step_guidance(step, c)


def _review_step_guidance(
    step: str,
    c: dict[str, Any],
    review_mode: str,
) -> dict[str, Any] | None:
    """审查类 step（claim_review / request_review）的引导构造。

    review-unify-r2：按 review_mode 分流模板——unified 单阶段 reviewer.md，
    two_phase 双阶段 spec-reviewer.md + code-reviewer.md。
    不匹配返回 None。
    """
    # 模板按 review_mode 分流（review-unify-r2）：unified 单阶段 reviewer.md，
    # two_phase 双阶段 spec-reviewer.md + code-reviewer.md。经模板路由反向生成
    # （task-guide-routing-meta），与旧手写分支逐字同输出（见单测 parity 断言）。
    # 注意：此处只看 review_mode（phase 缺失回退只影响 --type 参数与 phase_label，
    # 不影响模板选择——与旧语义一致）。
    _eff_mode = "unified" if review_mode == "unified" else "two_phase"
    review_templates = template_for("reviewer", _eff_mode)
    if step == "claim_review":
        unclaimed_tid = c.get("first_unclaimed_review")
        phase = c.get("first_unclaimed_review_phase")
        # unified 阶段：**省略 --type**（claim 默认锁任务当前阶段）。历史上本分支产出
        # `claim --type review`，而 claim 的 --type choices 仅 [spec, code] → 非法命令。
        if review_mode == "unified" or not phase or phase == "unified":
            cmd = f"{_ENTRY_CMD} claim --task {unclaimed_tid} --confirm"
            phase_label = "unified"
        else:
            cmd = f"{_ENTRY_CMD} claim --task {unclaimed_tid} --type {phase} --confirm"
            phase_label = phase
        return {
            "step": "claim_review",
            "read": read_for("claim_review"),
            "template": review_templates,
            "command": cmd,
            "hint": (
                f"有 {c[STATUS_IN_REVIEW]} 个任务待审查：领取审查任务（{phase_label} 阶段）——"
                f"认领须在任务 worktree 内执行且带 --confirm；审查结论也须在任务 worktree 内提交"
                f"（E018 守卫：目标目录必须等于任务 worktree，防错目录审查）；提交成功后引擎回收"
                f"该 worktree，回收前请先切出任务目录（Windows 句柄占用会导致回收失败）；"
                f"代码审查通过任务才算完成。"
            ) + (
                " 注意：该任务 require_independent_review=true，要求异指纹审查——"
                "实现会话不得认领，请换独立会话（不同 ORCHD_SESSION_ID）执行 claim"
                "（同指纹提交的审查结论视为无效）。"
                if c.get("first_unclaimed_review_requires_independence") else ""
            ),
        }
    if step == "submit_review":
        tid = c.get("my_review_tid")
        phase = c.get("my_review_phase")
        unified = review_mode == "unified" or not phase or phase == "unified"
        # review 的 --type choices 仅 [spec, code]；unified 单阶段模式须省略该参数。
        type_arg = "" if unified else f" --type {phase}"
        phase_label = "unified" if unified else (phase or "")
        return {
            "step": "submit_review",
            "read": read_for("submit_review"),
            "template": review_templates,
            "command": (
                f"{_ENTRY_CMD} review --task {tid}{type_arg} "
                f"--verdict APPROVED|CHANGES_REQUESTED"
            ),
            "hint": (
                f"任务 {tid} 的审查（{phase_label} 阶段）已由你领取但尚未提交："
                f"按 files_to_review 核对后提交 review 结论；code 阶段 APPROVED 后"
                f"须回主工作树运行 {_ENTRY_CMD} status --audit-merge 确认零告警。"
            ),
        }
    return None


def _impl_step_guidance(
    step: str,
    c: dict[str, Any],
    cls: dict[str, Any],
) -> dict[str, Any] | None:
    """实现类 step（rework_first / request_impl / done）的引导构造。

    rework_first：有返工任务且无活跃认领 → 优先认领返工（claimed==0 守卫
    与 request_impl 一致：本 agent 持有 claimed 时走 done，避免误导领新任务
    触发 E011 busy）。不匹配返回 None。
    """
    if step == "rework_first":
        return {
            "step": "rework_first",
            "read": read_for("rework_first"),
            "template": template_for("implementer", None),
            "command": f"{_ENTRY_CMD} request",
            "hint": (
                f"有 {c['rework']} 个返工任务待认领（已被审查打回）：优先处理避免积压，"
                "认领后请先读 review_comments 中的前次审查意见。"
            ),
        }
    if step == "request_impl":
        return {
            "step": "request_impl",
            # rules/safety.md（引擎改动触碰 §9.1 停服边界）路由到实现入口等 per-site
            # 理由现收归各 rules 文件头 front-matter（task-guide-routing-meta），
            # 本处只保留"verify.md 必须保留"类的输出形状约束注释。
            "read": read_for("request_impl"),
            "template": template_for("implementer", None),
            "command": f"{_ENTRY_CMD} request",
            # hint 保持紧凑（task-guidance-block-budget-root-fix：预算由 _BLOCK_MAX
            # 求和不等式统一管理，不再有 200 字符硬编码上限）；
            # 测试纪律经 read 路由送达，不挤占 hint 额度。
            "hint": f"有 {c[STATUS_PENDING]} 个待认领任务：现在没有活跃实现，可领取一个新任务。",
        }
    if step == "done":
        tid = cls["focus_tid"]
        return {
            "step": "done",
            # read 优先级（slim 截断先丢次要项）收归 front-matter 的 order
            # （task-guide-routing-meta），本处保留形状约束意图。
            "read": read_for("done"),
            "template": template_for("implementer", None),
            "command": f"{_ENTRY_CMD} done --task {tid} --changes '<描述>'",
            "hint": (
                f"任务 {tid} 已认领给当前 agent：在**任务 worktree**（container 布局）内"
                f"用 {_ENTRY_CMD} done 提交，verify 通过后进入审查、引擎自动切回主分支。"
            ),
        }
    return None


def _terminal_step_guidance(
    step: str,
    c: dict[str, Any],
) -> dict[str, Any]:
    """等待/收尾类 step（wait_review / optional_cancel / done_all）+ 兜底 check_status。

    始终返回非 None（含兜底），是 _derive 分发链的最后一环。
    """
    if step == "wait_review":
        return {
            "step": "wait_review",
            "read": read_for("wait_review"),
            "template": [],
            "command": f"{_ENTRY_CMD} status",
            "hint": "有任务正在实现或已提交待审查：等待实现者 done 或审查者 review，"
                    f"用 {_ENTRY_CMD} status --text 查看最新进展。",
        }
    if step == "optional_cancel":
        return {
            "step": "optional_cancel",
            "read": read_for("optional_cancel"),
            "template": [],
            "command": f"{_ENTRY_CMD} status",
            "hint": f"存在已取消任务：可忽略，或用 {_ENTRY_CMD} force-status 改为 pending 重新评估。",
        }
    if step == "done_all":
        return {
            "step": "done_all",
            "read": read_for("done_all"),
            "template": [],
            "command": f"{_ENTRY_CMD} status",
            "hint": "所有任务已完成：可提交新 idea 供拆解，或进入下一阶段规划。",
        }
    if step == "audit_merge":
        # review code APPROVED + merge 成功后的完成态引导（task-review-completion-guidance）。
        # 实际构造由 orchd/review.py _build_completion_guidance 完成（含 worktree
        # 回收残留处置），此处保留词表构造分支以满足 GUIDANCE_STEPS 单一来源约束。
        return {
            "step": "audit_merge",
            "read": read_for("audit_merge"),
            "template": [],
            "command": f"{_ENTRY_CMD} status --audit-merge",
            "hint": "任务已审查通过并合并：请在主工作树执行 status --audit-merge 确认无警告。",
        }
    # 兜底：无可匹配分支（pending 被阻塞 / 未知状态组合）——留痕，避免兜底静默
    return {
        "step": "check_status",
        "read": read_for("check_status"),
        "template": [],
        "command": f"{_ENTRY_CMD} status",
        "hint": (
            "查看当前任务池状态，确认下一步可执行的命令"
            "（当前无可匹配的引导分支，已降级兜底）。"
        ),
        "degraded": {"kind": "no_branch_matched", "fallback_step": "check_status"},
    }
