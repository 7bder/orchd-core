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
        六状态计数 + 关键派生态。
    """
    counts = {"pending": 0, "claimed": 0, "done": 0, "in_review": 0,
              "completed": 0, "cancelled": 0}
    my_claimed: list[str] = []
    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        s = ts.status if ts else "pending"
        counts[s] = counts.get(s, 0) + 1
        if s == "claimed" and agent_id and ts and ts.claimed_by == agent_id:
            my_claimed.append(tid)
        if s == "in_review" and agent_id and ts and ts.review_claimed_by == agent_id:
            counts["my_in_review"] = counts.get("my_in_review", 0) + 1
    counts["total"] = len(tasks)
    counts["my_claimed"] = my_claimed
    return counts


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
            "command": "orchd idea propose --title '<灵感>' --feasibility '<论证>'",
            "hint": "项目已初始化但还没有任务：可提交新 idea 供拆解，或直接规划下一阶段。",
        }
    return {
        "step": "first_time",
        "read": [],
        "template": [],
        "command": "orchd bootstrap",
        "hint": "项目尚未初始化：先运行 orchd bootstrap 获取任务分解套件，"
                "再 orchd init 初始化快照，之后即可 request/claim 领取任务。",
        "card": {
            "title": "Orchd 项目初始化引导",
            "phase": "first_time",
            "steps": ["bootstrap", "init", "request"],
            "current": 0,
            "next": "bootstrap",
        },
    }


def _derive(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None,
    has_master: bool,
) -> dict[str, Any]:
    """按状态机+角色推导单视角引导（内部函数，返回纯 {step, read, template, command, hint}）。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（None 表示未知/项目视角）。
        has_master: 是否已存在 ``_master.json``（区分未初始化/空项目）。

    Returns:
        纯 5 键引导结构；无任务时返回 first_time/empty_project 引导。
    """
    c = _summarize(state, tasks, agent_id)

    # 空项目 / 无任务 → 首次引导（has_master 区分未初始化 vs 空项目）
    if c["total"] == 0:
        return first_time_guide(has_master=has_master)

    # 有 in_review 未审 → 优先引导领审查（知识：review 规则；方法：审查模板）
    if c.get("in_review", 0) > 0:
        return {
            "step": "request_review",
            "read": ["rules/review.md"],
            "template": ["templates/spec-reviewer.md", "templates/code-reviewer.md"],
            "command": "orchd request",
            "hint": f"有 {c['in_review']} 个任务待审查：先领取审查任务，代码审查通过后任务才算完成。",
        }

    # 有 pending 且无 claimed → 引导领实现任务（知识：摄入/会话规则；方法：实现模板）
    if c["pending"] > 0 and c["claimed"] == 0:
        return {
            "step": "request_impl",
            "read": ["rules/intake.md", "rules/session.md"],
            "template": ["templates/implementer.md"],
            "command": "orchd request",
            "hint": f"有 {c['pending']} 个待认领任务：现在没有活跃实现，可领取一个新任务。",
        }

    # 有当前 agent 的 claimed 任务 → 引导 done（知识：会话/验证/git 规则；方法：实现模板）
    if c.get("my_claimed"):
        tid = c["my_claimed"][0]
        return {
            "step": "done",
            "read": ["rules/session.md", "rules/verify.md", "rules/git.md"],
            "template": ["templates/implementer.md"],
            "command": f"orchd done --task {tid} --changes '<描述>'",
            "hint": f"任务 {tid} 已认领给当前 agent：实现完成后用 orchd done 提交（verify 通过后进入审查）。",
        }

    # 有 claimed（其他 agent）或 done 无审 → 等待/触发审查
    if c["claimed"] > 0 or c["done"] > 0:
        return {
            "step": "wait_review",
            "read": ["rules/review.md"],
            "template": [],
            "command": "orchd status --text",
            "hint": "有任务正在实现或已提交待审查：等待实现者 done 或审查者 review，"
                    "用 orchd status --text 查看最新进展。",
        }

    # 有 cancelled → 可忽略或重新认领
    if c["cancelled"] > 0:
        return {
            "step": "optional_cancel",
            "read": [],
            "template": [],
            "command": "orchd status --text",
            "hint": "存在已取消任务：可忽略，或用 orchd force-status 改为 pending 重新评估。",
        }

    # 全部 completed → 全部完成 / 写新 idea
    if c["completed"] > 0 and c["completed"] == c["total"]:
        return {
            "step": "done_all",
            "read": [],
            "template": [],
            "command": "orchd status --text",
            "hint": "所有任务已完成：可提交新 idea 供拆解，或进入下一阶段规划。",
        }

    # 兜底：pending 有但被阻塞等
    return {
        "step": "check_status",
        "read": [],
        "template": [],
        "command": "orchd status --text",
        "hint": "查看当前任务池状态，确认下一步可执行的命令。",
    }


def next_guidance(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
    has_master: bool = False,
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

    Returns:
        引导结构：顶层 5 键 + agent_view + project_view。
    """
    agent_view = _derive(state, tasks, agent_id, has_master)
    project_view = _derive(state, tasks, None, has_master)
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
) -> str:
    """为 ``orchd status --text`` 生成表格末尾的引导文字（纯函数）。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（可选）。
        has_master: 是否已存在 ``_master.json``（status 命令前置必有，传 True）。

    Returns:
        一行引导文字（含换行前缀），供追加到表格末尾。
    """
    g = next_guidance(state, tasks, agent_id, has_master)
    return f"\n下一步：{g['hint']}（{g['command']}）\n"
