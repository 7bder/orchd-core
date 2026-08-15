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


def first_time_guide() -> dict[str, Any]:
    """未初始化项目 / 全新工作区的引导（bootstrap 后使用）。

    Returns:
        引导结构：指引先 bootstrap 获取分解套件，再 init 初始化快照。
    """
    return {
        "step": "first_time",
        "read": [],
        "template": [],
        "command": "orchd bootstrap",
        "hint": "项目尚未初始化：先运行 orchd bootstrap 获取任务分解套件，"
                "再 orchd init 初始化快照，之后即可 request/claim 领取任务。",
    }


def next_guidance(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
) -> dict[str, Any]:
    """按状态机+角色推导下一步引导（纯函数）。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（可选）。

    Returns:
        引导结构 {step, read, template, command, hint}；无任务时返回 first_time 引导。
    """
    c = _summarize(state, tasks, agent_id)

    # 空项目 / 无任务 → 首次引导
    if c["total"] == 0:
        return first_time_guide()

    # 有 in_review 未审 → 优先引导领审查（知识：review 规则；方法：审查模板）
    if c.get("in_review", 0) > 0:
        return {
            "step": "request_review",
            "read": ["rules/review.md"],
            "template": ["templates/spec-reviewer.md", "templates/code-reviewer.md"],
            "command": f"orchd request --agent {agent_id or '<你>'} --role reviewer",
            "hint": f"有 {c['in_review']} 个任务待审查：先领取审查任务，代码审查通过后任务才算完成。",
        }

    # 有 pending 且无 claimed → 引导领实现任务（知识：摄入/会话规则；方法：实现模板）
    if c["pending"] > 0 and c["claimed"] == 0:
        return {
            "step": "request_impl",
            "read": ["rules/intake.md", "rules/session.md"],
            "template": ["templates/implementer.md"],
            "command": f"orchd request --agent {agent_id or '<你>'}",
            "hint": f"有 {c['pending']} 个待认领任务：现在没有活跃实现，可领取一个新任务。",
        }

    # 有当前 agent 的 claimed 任务 → 引导 done（知识：会话/验证/git 规则；方法：实现模板）
    if c.get("my_claimed"):
        tid = c["my_claimed"][0]
        return {
            "step": "done",
            "read": ["rules/session.md", "rules/verify.md", "rules/git.md"],
            "template": ["templates/implementer.md"],
            "command": f"orchd done --task {tid} --agent {agent_id or '<你>'} --changes '<描述>'",
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


def status_guidance_text(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
) -> str:
    """为 ``orchd status --text`` 生成表格末尾的引导文字（纯函数）。

    Args:
        state: ``Store.replay()`` 产物。
        tasks: master 任务定义列表。
        agent_id: 当前 agent 身份（可选）。

    Returns:
        一行引导文字（含换行前缀），供追加到表格末尾。
    """
    g = next_guidance(state, tasks, agent_id)
    return f"\n下一步：{g['hint']}（{g['command']}）"