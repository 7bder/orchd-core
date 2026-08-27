"""orchd/guide.py — 引擎侧无感用户流程引导（task-guide-seamless-guidance）。

引擎侧增加无感用户流程引导，防止用户不知道下一步做什么。核心思想是**无感**：
不新增需要主动运行的独立命令，而是在所有命令的 JSON 响应中自动附加一个
``guidance`` 字段，并在人类可读输出（``status --text`` / ``--help``）中嵌入提示。

设计约束（与 cli.py 注入点一起满足存取标准）：
- **零 orchd 内部依赖**：本模块只依赖纯 Python 标准库和 ``orchd.ledger.TaskState``
  的字段语义（作为数据输入，不 import 其他 orchd 模块），可独立测试。
- **纯函数**：``next_guidance`` / ``status_guidance_text`` / ``first_time_guide``
  均为接受已派生的状态数据、返回引导文本的纯函数，无副作用、无 I/O。
- **加法式**：只产生新的 ``guidance`` 键，不修改任何既有字段；已有 guidance
  时不覆盖（幂等，由 cli.py 注入点保证）。
- **best-effort**：任何异常应由调用方（cli.py ``_attach_guidance``）静默跳过，
  不阻塞主流程。

引导语言：中文。
"""

from __future__ import annotations

import os
from typing import Any

# 引导语义：返回 {step, read, template, command, hint} 结构，供 agent/用户据以行动。
# step    —— 建议执行的下一步动作标识（first_time / request_review / request_impl /
#             done / wait_review / request_new_idea / optional_cancel / done_all）
# read    —— 知识路由：该读的规则文件路径数组（可空数组 [] 表示无建议）
# template—— 方法路由：该用的模板路径数组（可空数组 [] 表示无建议）
# command —— 建议执行的 orchd 命令（含参数占位，用户替换占位符）
# hint    —— 一句话说明为什么这么做
#
# 路由表（状态机 × 角色 → 知识+方法）为引擎侧单一事实源，顶层平铺、加法式：
# 只新增 read/template 两键，与既有 {step, command, hint} 平铺兼容，后期扩展零迁移。
# 边界：引擎不做内容域解析，read/template 是字符串常量路径，不读取 rules/ 文件内容。

# orchd 入口命令前缀（零根入口 .orchd/__main__.py，容器布局下从项目根运行）。
# 集中为模块级常量：入口变更时只改此处，避免 20+ 处命令/hint 字符串漂移。
_ENTRY_CMD = "python .orchd/__main__.py"


def _resolve_paths(
    paths: list[str],
    base_dir: str | os.PathLike[str] | None,
) -> list[str]:
    """将 read/template 路径过滤为实际存在的文件路径（知识路由闭环）。

    路由闭环（task-guide-routing-loop）：guidance 的 read/template 字段由静态
    字符串路径升级为**可执行闭环**——agent 收到 guidance 后按 read 数组读规则
    文件、按 template 数组加载模板。为此引擎侧须保证字段指向实际存在的文件，
    否则 agent 按路径读取会落空。

    规则：
    - ``paths`` 中的每条解析候选位置：``base_dir`` 下及其**父目录**下（规则在
      ``.orchd/rules/``、模板在项目根 ``templates/``，故双候选兜底）；任一命中
      实际文件即保留，都不存在则静默跳过（best-effort 降级，不抛异常）。
    - 绝对路径直接判定，不做拼接。
    - ``base_dir`` 为 None 或空 → 不做存在性校验，原样返回（纯函数 / 单测场景）。
    - 空数组 → 返回空数组（无害，向下兼容）。

    Args:
        paths: read 或 template 路径数组。
        base_dir: 规则/模板所在根目录（.orchd/）；None 时跳过校验。

    Returns:
        过滤后只含实际存在文件的路径数组（顺序保持）。
    """
    if not paths or base_dir is None:
        return list(paths)
    root = os.path.abspath(os.fspath(base_dir))
    parent = os.path.dirname(root)
    kept: list[str] = []
    for p in paths:
        if os.path.isabs(p):
            if os.path.isfile(p):
                kept.append(p)
            continue
        candidates = (os.path.join(root, p), os.path.join(parent, p))
        if any(os.path.isfile(c) for c in candidates):
            kept.append(p)
    return kept


def resolve_read_paths(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """将 guidance 的 read/template 字段解析为指向实际存在文件的路径（纯函数）。

    知识路由闭环的引擎侧接口（task-guide-routing-loop）：调用方（cli.py
    ``_attach_guidance`` 注入点）在附加 guidance 后透传 ``base_dir``，本函数
    返回**新增 ``read``/``template`` 键**的 guidance 副本，指向实际存在的规则/
    模板文件，不存在时静默跳过。空数组 / base_dir None 时原样返回（无害降级）。

    Args:
        guidance: 原始 guidance 字典（含 read/template 数组）。
        base_dir: 规则/模板根目录（.orchd/）；None 时跳过校验。

    Returns:
        过滤后的 guidance 字典（加法式，仅调整 read/template 两键，不碰
        step/command/hint 结构；含 agent_view/project_view 时递归过滤，
        保证双视角与顶层 read/template 一致）。
    """
    out = dict(guidance)
    out["read"] = _resolve_paths(guidance.get("read") or [], base_dir)
    out["template"] = _resolve_paths(guidance.get("template") or [], base_dir)
    # 双视角（task-guidance-dual-view-engine）：递归过滤子视角的 read/template，
    # 避免顶层已过滤而 agent_view/project_view 仍指向不存在路径的不一致。
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = resolve_read_paths(sub, base_dir)
    return out


def _summarize(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
) -> dict[str, int]:
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
    counts = {"pending": 0, "claimed": 0, "done": 0, "in_review": 0,
              "completed": 0, "cancelled": 0}
    my_claimed: list[str] = []
    rework = 0
    first_rework_tid: str | None = None
    first_unclaimed_review: str | None = None
    first_unclaimed_review_phase: str | None = None
    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        s = ts.status if ts else "pending"
        counts[s] = counts.get(s, 0) + 1
        if s == "pending" and ts and ts.attempt_count > 0:
            rework += 1
            if first_rework_tid is None:
                first_rework_tid = tid
        if s == "claimed" and agent_id and ts and ts.claimed_by == agent_id:
            my_claimed.append(tid)
        if s == "in_review":
            if agent_id and ts and ts.review_claimed_by == agent_id:
                counts["my_in_review"] = counts.get("my_in_review", 0) + 1
            if ts and ts.review_claimed_by is None and first_unclaimed_review is None:
                first_unclaimed_review = tid
                first_unclaimed_review_phase = ts.review_phase or "unified"
    counts["total"] = len(tasks)
    counts["my_claimed"] = my_claimed
    counts["rework"] = rework
    counts["first_rework_tid"] = first_rework_tid
    counts["first_unclaimed_review"] = first_unclaimed_review
    counts["first_unclaimed_review_phase"] = first_unclaimed_review_phase
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
    if c.get("in_review", 0) > 0:
        return {
            "step": "claim_review" if c.get("first_unclaimed_review") else "request_review",
            "focus_tid": c.get("first_unclaimed_review"),
        }
    if c.get("rework", 0) > 0 and c["claimed"] == 0:
        return {"step": "rework_first", "focus_tid": c.get("first_rework_tid")}
    if c["pending"] > 0 and c["claimed"] == 0:
        return {"step": "request_impl", "focus_tid": None}
    if c.get("my_claimed"):
        return {"step": "done", "focus_tid": c["my_claimed"][0]}
    if c["claimed"] > 0 or c["done"] > 0:
        return {"step": "wait_review", "focus_tid": None}
    if c["cancelled"] > 0:
        return {"step": "optional_cancel", "focus_tid": None}
    if c["completed"] > 0 and c["completed"] == c["total"]:
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
            "read": [],
            "template": [],
            "command": f"{_ENTRY_CMD} idea propose --title '<灵感>' --feasibility '<论证>'",
            "hint": "项目已初始化但还没有任务：可提交新 idea 供拆解，或直接规划下一阶段。",
        }
    return {
        "step": "first_time",
        "read": [],
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
        "read": [],
        "template": [],
        "command": "",
        "hint": "引擎未分配任务：停止，不得自行 claim 或重试 request，等待用户下一条指令。",
    }


def _derive(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None,
    has_master: bool,
    review_mode: str = "two_phase",
) -> dict[str, Any]:
    """按状态机+角色推导单视角引导（内部函数，返回纯 {step, read, template, command, hint}）。

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

    # 有 in_review 未审 → 优先引导领审查（知识：review 规则；方法：审查模板）
    # review-unify-r2：按 review_mode 分流模板——unified 单阶段 reviewer.md，
    # two_phase 双阶段 spec-reviewer.md + code-reviewer.md。
    if step == "claim_review":
        review_templates = (
            ["templates/reviewer.md"]
            if review_mode == "unified"
            else ["templates/spec-reviewer.md", "templates/code-reviewer.md"]
        )
        unclaimed_tid = c.get("first_unclaimed_review")
        phase = c.get("first_unclaimed_review_phase")
        if review_mode == "unified" or not phase or phase == "unified":
            cmd = f"{_ENTRY_CMD} claim --task {unclaimed_tid} --type review"
            phase_label = "unified"
        else:
            cmd = f"{_ENTRY_CMD} claim --task {unclaimed_tid} --type {phase}"
            phase_label = phase
        return {
            "step": "claim_review",
            "read": ["rules/review.md"],
            "template": review_templates,
            "command": cmd,
            "hint": (
                f"有 {c['in_review']} 个任务待审查：先领取审查任务（{phase_label} 阶段），"
                f"代码审查通过后任务才算完成。"
            ),
        }
    if step == "request_review":
        review_templates = (
            ["templates/reviewer.md"]
            if review_mode == "unified"
            else ["templates/spec-reviewer.md", "templates/code-reviewer.md"]
        )
        return {
            "step": "request_review",
            "read": ["rules/review.md"],
            "template": review_templates,
            "command": f"{_ENTRY_CMD} request",
            "hint": f"有 {c['in_review']} 个任务待审查：先领取审查任务，代码审查通过后任务才算完成。",
        }

    # 有返工任务且无活跃认领 → 引导优先认领返工，避免积压
    # （claimed==0 守卫与 request_impl 一致：本 agent 持有 claimed 时引导 done，
    #  而非误导其认领新任务触发 E011 busy）
    if step == "rework_first":
        return {
            "step": "rework_first",
            "read": ["rules/review.md", "rules/session.md"],
            "template": ["templates/implementer.md"],
            "command": f"{_ENTRY_CMD} request",
            "hint": (
                f"有 {c['rework']} 个返工任务待认领（已被审查打回）：优先处理避免积压，"
                "认领后请先读 review_comments 中的前次审查意见。"
            ),
        }

    # 有 pending 且无 claimed → 引导领实现任务（知识：摄入/会话规则；方法：实现模板）
    if step == "request_impl":
        return {
            "step": "request_impl",
            "read": ["rules/intake.md", "rules/session.md"],
            "template": ["templates/implementer.md"],
            "command": f"{_ENTRY_CMD} request",
            "hint": f"有 {c['pending']} 个待认领任务：现在没有活跃实现，可领取一个新任务。",
        }

    # 有当前 agent 的 claimed 任务 → 引导 done（知识：会话/验证/git 规则；方法：实现模板）
    if step == "done":
        tid = cls["focus_tid"]
        return {
            "step": "done",
            "read": ["rules/session.md", "rules/verify.md", "rules/git.md"],
            "template": ["templates/implementer.md"],
            "command": f"{_ENTRY_CMD} done --task {tid} --changes '<描述>'",
            "hint": f"任务 {tid} 已认领给当前 agent：实现完成后用 {_ENTRY_CMD} done 提交（verify 通过后进入审查）。",
        }

    # 有 claimed（其他 agent）或 done 无审 → 等待/触发审查
    if step == "wait_review":
        return {
            "step": "wait_review",
            "read": ["rules/review.md"],
            "template": [],
            "command": f"{_ENTRY_CMD} status --text",
            "hint": "有任务正在实现或已提交待审查：等待实现者 done 或审查者 review，"
                    f"用 {_ENTRY_CMD} status --text 查看最新进展。",
        }

    # 有 cancelled → 可忽略或重新认领
    if step == "optional_cancel":
        return {
            "step": "optional_cancel",
            "read": [],
            "template": [],
            "command": f"{_ENTRY_CMD} status --text",
            "hint": f"存在已取消任务：可忽略，或用 {_ENTRY_CMD} force-status 改为 pending 重新评估。",
        }

    # 全部 completed → 全部完成 / 写新 idea
    if step == "done_all":
        return {
            "step": "done_all",
            "read": [],
            "template": [],
            "command": f"{_ENTRY_CMD} status --text",
            "hint": "所有任务已完成：可提交新 idea 供拆解，或进入下一阶段规划。",
        }

    # 兜底：pending 有但被阻塞等
    return {
        "step": "check_status",
        "read": [],
        "template": [],
        "command": f"{_ENTRY_CMD} status --text",
        "hint": "查看当前任务池状态，确认下一步可执行的命令。",
    }


def transition_guidance(
    ts: Any,
    task_id: str,
    agent_id: str | None,
) -> dict[str, Any] | None:
    """聚焦任务的「状态由来」转换块（纯函数，加法式，task-guide-transition-aware）。

    由 TaskState（status/attempt_count/claimed_by/review_claimed_by）派生——当前
    状态即最近一次状态机转换的结果，永不陈旧、永不与真实状态矛盾。未知/无聚焦
    任务返回 None。
    """
    if ts is None or not getattr(ts, "status", None):
        return None
    s = ts.status
    if s == "claimed":
        mine = bool(agent_id and ts.claimed_by == agent_id)
        holder = ts.claimed_by or "其他 agent"
        return {
            "type": "claimed_impl",
            "task_id": task_id,
            "mine": mine,
            "hint": (
                f"任务 {task_id} 已认领{'给你' if mine else f'（{holder}）'}（实现中）："
                f"在 task/{task_id} 分支实现并提交，完成后用 {_ENTRY_CMD} done 提交并自动切回主分支。"
            ),
            "read": ["rules/session.md", "rules/git.md", "rules/verify.md"],
        }
    if s in ("done", "in_review"):
        reviewing = "、正在审查中" if (s == "in_review" and ts.review_claimed_by) else ""
        return {
            "type": "done_submitted",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已提交待审查{reviewing}：不要在任务分支继续改动，"
                "审查通过后引擎自动合并并回收任务环境。"
            ),
            "read": ["rules/review.md", "rules/git.md"],
        }
    if s == "completed":
        return {
            "type": "approved_completed",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已审查通过并合并（completed）：任务完成，"
                "其分支/环境已回收，可进入下一步。"
            ),
            "read": [],
        }
    if s == "cancelled":
        return {
            "type": "cancelled",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已取消：可忽略，或用 {_ENTRY_CMD} force-status "
                "改为 pending 重新评估。"
            ),
            "read": [],
        }
    if s == "pending" and ts.attempt_count > 0:
        return {
            "type": "changes_requested",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 被审查打回复工（第 {ts.attempt_count} 次尝试）："
                f"先读 review_comments 中的前次意见再修复，完成后重新 {_ENTRY_CMD} done。"
            ),
            "read": ["rules/review.md", "rules/session.md"],
        }
    return None


def branch_context(
    branch: str | None,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """当前分支 → 分支纪律上下文（纯函数，task-guide-transition-aware）。

    ``task/{id}`` → 任务分支纪律；``main``/``master`` → 主分支纪律；其他分支 →
    警示。检测不到分支（None）返回 None（静默跳过，best-effort）。
    """
    if not branch:
        return None
    if branch.startswith("task/"):
        tid = branch[len("task/"):]
        return {
            "branch": branch,
            "role": "task",
            "task_id": tid,
            "hint": (
                f"当前在任务分支 {branch}：实现/提交只在本分支，勿在主分支改动任务文件；"
                f"完成后 {_ENTRY_CMD} done 会自动切回主分支。"
            ),
        }
    if branch in ("main", "master"):
        return {
            "branch": branch,
            "role": "main",
            "hint": (
                f"当前在主分支 {branch}：只允许 claim 前的读操作与引擎自动 merge；"
                "任务改动必须在 task 分支完成，不要在主分支直接提交任务文件。"
            ),
        }
    return {
        "branch": branch,
        "role": "other",
        "hint": f"当前分支 {branch} 不是 task 分支：请确认是否误切分支，必要时切回 task 分支继续。",
    }


def context_guidance(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
    review_mode: str = "two_phase",
    branch: str | None = None,
) -> dict[str, Any]:
    """转换感知上下文（纯函数，加法式，task-guide-transition-aware）。

    返回 ``{transition?, branch_ctx?}``：branch_ctx 恒有（只要检测到分支）；
    transition 仅在存在聚焦任务时给出——聚焦与 next_guidance 共用 ``_classify``，
    保证与状态引导指向同一任务、永不漂移、永不陈旧。
    """
    out: dict[str, Any] = {}
    bc = branch_context(branch, state)
    if bc:
        out["branch_ctx"] = bc
    cls = _classify(state, tasks, agent_id, has_master=bool(tasks), review_mode=review_mode)
    focus_tid = cls.get("focus_tid")
    if focus_tid:
        tr = transition_guidance(state.get(focus_tid), focus_tid, agent_id)
        if tr:
            out["transition"] = tr
    return out


def next_guidance(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
    has_master: bool = False,
    review_mode: str = "two_phase",
) -> dict[str, Any]:
    """按状态机+角色推导下一步引导（纯函数，双视角，task-guidance-dual-view-engine）。

    双视角契约：
    - 顶层 {step, read, template, command, hint} 5 键语义不变 = agent_view 推导结果
      （向后兼容，既有消费方无感）。
    - 新增 ``agent_view``（以传入 agent_id 推导）与 ``project_view``（以
      agent_id=None 推导）两键（加法式，不破坏既有字段）。
    - ``agent_id`` 未知（None/空串）时两视角相等（退化一致，接入层可据此
      只呈现项目视角）。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（可选；None/空 → agent_view 退化为项目视角）。
        has_master: 是否已存在 ``_master.json``（区分未初始化/空项目）。
        review_mode: 审查模式（review-unify-r2）：``"unified"`` 单阶段模板，
            ``"two_phase"`` 双阶段模板。缺省 two_phase 向后兼容。

    Returns:
        引导结构：顶层 5 键 + agent_view + project_view。
    """
    agent_view = _derive(state, tasks, agent_id, has_master, review_mode)
    project_view = _derive(state, tasks, None, has_master, review_mode)
    # 顶层严格保持 5 键（= agent_view 的 5 键，向后兼容），双视角挂 agent_view/project_view
    out = {
        "step": agent_view["step"],
        "read": agent_view["read"],
        "template": agent_view["template"],
        "command": agent_view["command"],
        "hint": agent_view["hint"],
        "agent_view": agent_view,
        "project_view": project_view,
    }
    return out


def status_guidance_text(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
    has_master: bool = False,
    review_mode: str = "two_phase",
) -> str:
    """为 ``python .orchd/__main__.py status --text`` 生成表格末尾的引导文字（纯函数）。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（可选）。
        has_master: 是否已存在 ``_master.json``（status 命令前置必有，传 True）。
        review_mode: 审查模式（review-unify-r2），透传 next_guidance。

    Returns:
        一行引导文字（含换行前缀），供追加到表格末尾。
    """
    g = next_guidance(state, tasks, agent_id, has_master, review_mode)
    return f"\n下一步：{g['hint']}（{g['command']}）\n"


def _extract_tldr(path: str) -> str | None:
    """读取 rule 文件，提取 TL;DR 段文本（I/O 透传，不做内容域解析）。

    匹配以 ``> TL;DR:`` 开头的行，返回 ``:`` 后的内容（strip）；无该行或
    内容为空返回 None。文件读取异常（OSError）返回 None（best-effort 降级）。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("> TL;DR:"):
                    text = line[len("> TL;DR:"):].strip()
                    return text or None
    except OSError:
        return None
    return None


def _summarize_rules(
    read_paths: list[str],
    base_dir: str | os.PathLike[str] | None,
) -> list[str]:
    """把 read 路径数组映射为 ``<文件名>: <TL;DR>`` 摘要数组（纯函数）。

    - 相对路径按 base_dir 拼接定位；绝对路径直接读。
    - 无 TL;DR 段的文件跳过（不产生元素）。
    - base_dir 为 None 或 read 为空 → 返回空数组（无害降级）。
    """
    if not read_paths or base_dir is None:
        return []
    root = os.path.abspath(os.fspath(base_dir))
    rules: list[str] = []
    for p in read_paths:
        path = p if os.path.isabs(p) else os.path.join(root, p)
        tldr = _extract_tldr(path)
        if tldr:
            rules.append(f"{os.path.splitext(os.path.basename(p))[0]}: {tldr}")
    return rules


def attach_rule_summaries(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """对 guidance 的 read 数组生成 rules 键（加法式，纯函数）。

    知识路由闭环的摘要层（task-guidance-rule-summary）：调用方（cli.py
    ``_attach_guidance`` 注入点）在 ``resolve_read_paths`` 之后透传 base_dir，
    本函数返回**新增 ``rules`` 键**的 guidance 副本，内容为 read 数组对应
    规则文件的 TL;DR 摘要（``<文件名>: <TL;DR>``），无 TL;DR 段跳过。

    - 顶层新增 rules 键；agent_view / project_view 递归处理（与
      resolve_read_paths 同构），保证双视角与顶层 rules 一致。
    - 不碰 step/read/template/command/hint 既有字段（加法式）。
    - read 为空 / base_dir None 时返回 rules=[]（无害降级）。
    """
    out = dict(guidance)
    out["rules"] = _summarize_rules(guidance.get("read") or [], base_dir)
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = attach_rule_summaries(sub, base_dir)
    return out


# 错误恢复指引映射（error recovery guidance）：错误码 → 恢复指引。
# 只提示不代行——不自动执行任何修复命令，修复决策权始终在人/agent。
ERROR_GUIDANCE: dict[str, dict[str, Any]] = {
    "E007": {
        "recovery": "任务状态不合法：用 status 查看当前状态，确认任务处于预期阶段",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E008": {
        "recovery": "任务未就绪：确认任务处于 pending 状态再 claim",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E009": {
        "recovery": "任务已被认领：等待其完成，或确认是否由你认领",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E010": {
        "recovery": "文件冲突：与在握任务 files_to_edit 重叠，需串行化或合并任务",
        "read": ["rules/intake.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E011": {
        "recovery": "agent 忙：已持有其他任务，先完成或 retract 再领新任务",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E014": {
        "recovery": "verify 失败：查看 verify 输出，修复实现后重试 done",
        "read": ["rules/verify.md"],
        "command": "python -m pytest <定向测试>",
    },
    "E016": {
        "recovery": "自审被阻断：换会话/身份审查，或由他人审查",
        "read": ["rules/review.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E017": {
        "recovery": "工作区脏：先提交/清理未提交改动，再重试",
        "read": ["rules/git.md"],
        "command": "git status",
    },
    "E018": {
        "recovery": "分支错误：确认处于正确分支",
        "read": ["rules/git.md"],
        "command": "git branch --show-current",
    },
    "E020": {
        "recovery": "范围外提交：只改 files_to_edit 声明文件",
        "read": ["rules/git.md"],
        "command": "git status",
    },
    "E025": {
        "recovery": "source 引用缺失：任务需关联 IDEAS.md/ROADMAP.md 条目",
        "read": ["rules/intake.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
    "E032": {
        "recovery": "auto-claim 被禁：需人工确认 claim 或 config.allow_auto_claim",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} claim --task <id> --confirm",
    },
    "E033": {
        "recovery": "会话身份缺失：先 session start 注入 ORCHD_SESSION_ID",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} session start",
    },
    "E034": {
        "recovery": "撤认归属守卫：仅事件作者或 admin 可撤回",
        "read": ["rules/session.md"],
        "command": f"{_ENTRY_CMD} status --text",
    },
}

# 未命中映射时的通用兜底指引（保证任何错误码都有恢复指引）。
# 设计 §3 补丁1：本兜底「立即停止并报告」**不算"明确指引"**——归入"无 guidance"
# 分支，允许 agent 分析自愈（否则所有未映射错误码都有 fallback 指引，自愈永不触发）。
_FALLBACK_ERROR_GUIDANCE: dict[str, Any] = {
    "recovery": "遇到错误：立即停止并报告，不自行猜测处置",
    "read": ["rules/session.md"],
    "command": f"{_ENTRY_CMD} status --text",
}

# warning 级错误独立指引（设计 §5）：warning 不阻断操作，agent 应继续而非停止，
# 与 _FALLBACK_ERROR_GUIDANCE 的"立即停止"语义不匹配。命中 warning 码（且未映射
# 到具体 ERROR_GUIDANCE）时走本指引，而非 fallback。
_WARNING_ERROR_GUIDANCE: dict[str, Any] = {
    "recovery": (
        "警告不阻断：可继续当前流程；若你判定该警告是更深问题的征兆，"
        "可 lesson report 上报（设计 §5.1 三重信号判定）"
    ),
    "read": ["rules/session.md"],
    "command": f"{_ENTRY_CMD} status --text",
}


def error_guidance(code: str) -> dict[str, Any]:
    """按错误码返回恢复指引（纯函数）。

    Args:
        code: 错误码名（ErrorCode.name，如 ``"E017"``）。

    Returns:
        ``{step: "recover", recovery, read, command}``；未命中映射时返回
        通用兜底指引，保证任何错误码都有恢复指引。warning 码（且未映射具体指引）
        走独立 warning 指引（设计 §5）。suggest_report 码额外标注，供 agent 决策
        是否打点 lesson（§5.1 信号 A）。
    """
    from orchd.errors import is_suggest_report_code, is_warning_code

    g = ERROR_GUIDANCE.get(code)
    if g is None and is_warning_code(code):
        g = _WARNING_ERROR_GUIDANCE
    if g is None:
        g = _FALLBACK_ERROR_GUIDANCE
    out = {"step": "recover", **g}
    if is_suggest_report_code(code):
        out["suggest_report"] = True
    return out


def attach_error_guidance(
    resp: dict[str, Any],
    code: str,
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """错误响应附加 guidance（加法式，纯函数）。

    调用方（cli.py ``except OrchdError`` 分支）在 ``to_json_response`` 之后
    透传错误码与 base_dir，本函数返回**新增 ``guidance`` 键**的错误响应副本：
    ``{step: "recover", recovery, read, rules, command}``——read 走
    ``resolve_read_paths`` + ``attach_rule_summaries``，错误指引同样带红线摘要。

    - 幂等：已有 guidance 不覆盖。
    - best-effort：任何异常由调用方静默跳过。
    """
    out = dict(resp)
    if "guidance" in out:
        return out
    g = error_guidance(code)
    guidance = attach_rule_summaries(resolve_read_paths(g, base_dir), base_dir)
    # 经验回灌触发注入（设计 §8.4）：错误码命中的 lesson cases 挂到 guidance.cases。
    # best-effort：任何异常静默跳过，绝不阻塞错误响应主流程。
    try:
        from orchd import __version__
        from orchd.lessons import is_lessons_enabled, lookup_lessons

        if is_lessons_enabled(base_dir):
            cases = lookup_lessons(base_dir, code, __version__)
            if cases:
                guidance["cases"] = cases
    except Exception:
        pass
    out["guidance"] = guidance
    return out
