"""orchd/guide/flow.py — 转换上下文/分支/合并与入口域（W5-G2 拆分，纯搬移）。
来源：``__init__.py`` L526-813（transition 族 + 分支/上下文/slim）、L827-934（hint 合并与 mode）、
L937-948（预算常量块，G3 起迁 rules）、L995-1062（入口两部），见 §4.6.2/§4.6.3。域内两簇互相
调用故同文件；截断原语与预算常量取 rules（顶层导入）；line / line_ctx 为函数级依赖；
任务状态名取 ``contracts.events`` 常量。
"""

from __future__ import annotations

from typing import Any

from orchd.contracts.events import (
    STATUS_CANCELLED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
)
from .digest import _ENTRY_CMD
from .paths import read_for, template_for
from .stage import _classify, _derive
from .rules import (
    _DEGRADED_PREFIX,
    _HINT_MAX,
    _REDLINE_PREFIX,
    _truncate,
    _truncate_redline_semantic,
)


def transition_guidance(
    ts: Any,
    task_id: str,
    agent_id: str | None,
) -> dict[str, Any] | None:
    """聚焦任务的「状态由来」转换块（纯函数，加法式，task-guide-transition-aware）。

    由 TaskState（status/attempt_count/claimed_by/review_claimed_by）派生——当前
    状态即最近一次状态机转换的结果，永不陈旧、永不与真实状态矛盾。未知/无聚焦
    任务返回 None。分发器：空值守卫后按状态类别委托三个子函数。
    """
    if ts is None or not getattr(ts, "status", None):
        return None
    s = ts.status
    g = _active_transition(s, ts, task_id, agent_id)
    if g is not None:
        return g
    g = _terminal_transition(s, task_id)
    if g is not None:
        return g
    return _rework_transition(s, ts, task_id)


def _active_transition(
    s: str,
    ts: Any,
    task_id: str,
    agent_id: str | None,
) -> dict[str, Any] | None:
    """活跃状态转换块：claimed（实现中）/ done|in_review（已提交待审）。

    不匹配返回 None。
    """
    if s == STATUS_CLAIMED:
        mine = bool(agent_id and ts.claimed_by == agent_id)
        holder = ts.claimed_by or "其他 agent"
        return {
            "type": "claimed_impl",
            "task_id": task_id,
            "mine": mine,
            "hint": (
                f"任务 {task_id} 已认领{'给你' if mine else f'（{holder}）'}（实现中）："
                f"在 task/{task_id} 分支实现并提交（container 布局下在任务 worktree 内执行，"
                f"勿在主工作树改任务文件），完成后用 {_ENTRY_CMD} done 提交并自动切回主分支。"
            ),
            "read": read_for("claimed_impl"),
        }
    if s in (STATUS_DONE, STATUS_IN_REVIEW):
        reviewing = "、正在审查中" if s == STATUS_IN_REVIEW and ts.review_claimed_by else ""
        return {
            "type": "done_submitted",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已提交待审查{reviewing}：不要在任务分支继续改动，"
                "审查通过后引擎自动合并并回收任务环境。"
                f"审查者：code 阶段 APPROVED 后须回主工作树运行 {_ENTRY_CMD} status "
                "--audit-merge 确认零告警。"
            ),
            "read": read_for("done_submitted"),
        }
    return None


def _terminal_transition(
    s: str,
    task_id: str,
) -> dict[str, Any] | None:
    """终态转换块：completed（已合并）/ cancelled（已取消）。不匹配返回 None。"""
    if s == STATUS_COMPLETED:
        return {
            "type": "approved_completed",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已审查通过并合并（completed）：任务完成，"
                "其分支/环境已回收，可进入下一步。"
                f"审查者请运行 {_ENTRY_CMD} status --audit-merge 确认 "
                "merge_audit.warnings 为空（rules/review.md 硬要求）。"
            ),
            "read": read_for("approved_completed"),
        }
    if s == STATUS_CANCELLED:
        return {
            "type": "cancelled",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已取消：可忽略，或用 {_ENTRY_CMD} force-status "
                "改为 pending 重新评估。"
            ),
            "read": read_for("cancelled"),
        }
    return None


def _rework_transition(
    s: str,
    ts: Any,
    task_id: str,
) -> dict[str, Any] | None:
    """返工转换块：pending 且 attempt_count > 0（被审查打回）。不匹配返回 None。"""
    if s == STATUS_PENDING and ts.attempt_count > 0:
        return {
            "type": "changes_requested",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 被审查打回复工（第 {ts.attempt_count} 次尝试）："
                f"先读 review_comments 中的前次意见再修复，完成后重新 {_ENTRY_CMD} done。"
            ),
            "read": read_for("changes_requested"),
        }
    return None


# ---------------------------------------------------------------------------
# 写命令的 branch_ctx 差异化提示（task-audit-guidance-branch-ctx-rollout）
# ---------------------------------------------------------------------------
# 同一分支纪律下，关键写命令的 hint 按命令语义差异化（加法式追加，默认不变）：
# - claim   —— 将自动建 task/{id} 分支（弱 LLM 据此知道 claim 后会自动切换分支）
# - done    —— 须在任务分支执行（弱 LLM 据此知道 done 不能在 main 提交）
# - review  —— 须在任务分支且工作区干净（弱 LLM 据此知道审查前要确认工作区）
# 仅对 claim/done/review 三个写命令生效；其余命令（command=None）保持原 hint
# 向后兼容。单点化：差异化仍由 branch_context 一处生成，不新增第二套实现。
_CMD_BRANCH_CTX_TIPS: dict[str, str] = {
    "claim": "；claim 将自动建 task/{id} 分支",
    "done": "；done 须在任务分支执行",
    "review": "；review 须在任务分支且工作区干净",
}


def _current_line_trunk() -> str | None:
    """当前线 trunk（best-effort；供 branch_context 识别非默认线主干，失败 → None）。

    task-line-audit-guide-wiring：引导层保持纯函数为默认，此处按 cwd 惰性解析；
    解析失败（非仓库 / 无 master）回退 ``None``，角色判定退回 main/master 历史口径。
    """
    try:
        from pathlib import Path

        from orchd.line_ctx import resolve_trunk_for

        return resolve_trunk_for(Path.cwd())
    except Exception:  # noqa: BLE001 - 引导层 best-effort，不得影响主流程
        return None


def branch_context(
    branch: str | None,
    state: dict[str, Any],
    command: str | None = None,
) -> dict[str, Any] | None:
    """当前分支 → 分支纪律上下文（纯函数，task-guide-transition-aware）。

    ``task/{id}`` → 任务分支纪律；``main``/``master`` → 主分支纪律；其他分支 →
    警示。检测不到分支（None）返回 None（静默跳过，best-effort）。

    ``command`` 为 claim/done/review 时，hint 按命令语义差异化（追加一句，
    见 ``_CMD_BRANCH_CTX_TIPS``）；其余命令/缺省保持通用分支纪律（向后兼容）。
    """
    if not branch:
        return None
    tip = _CMD_BRANCH_CTX_TIPS.get(command) if command else None
    # 单根命名空间反解（pass8 F1-A）：task/{id} 或 task/{line}/{id}；
    # 旧 {line}/task/{id} 不再识别（fail-closed）。
    from orchd.line import parse_task_branch

    parsed = parse_task_branch(branch)
    if parsed is not None:
        tid = parsed[1]
        hint = (
            f"当前在任务分支 {branch}：实现/提交只在本分支，勿在主分支改动任务文件；"
            f"完成后 {_ENTRY_CMD} done 会自动切回主分支。"
        )
        if tip:
            hint = f"{hint}{tip}"
        return {
            "branch": branch,
            "role": "task",
            "task_id": tid,
            "hint": hint,
        }
    if branch in ("main", "master") or branch == _current_line_trunk():
        hint = (
            f"当前在主分支 {branch}：只允许 claim 前的读操作与引擎自动 merge；"
            "任务改动必须在 task 分支完成，不要在主分支直接提交任务文件。"
        )
        if tip:
            hint = f"{hint}{tip}"
        return {
            "branch": branch,
            "role": "main",
            "hint": hint,
        }
    hint = f"当前分支 {branch} 不是 task 分支：请确认是否误切分支，必要时切回 task 分支继续。"
    if tip:
        hint = f"{hint}{tip}"
    return {
        "branch": branch,
        "role": "other",
        "hint": hint,
    }


def context_guidance(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    agent_id: str | None = None,
    review_mode: str = "two_phase",
    branch: str | None = None,
    command: str | None = None,
) -> dict[str, Any]:
    """转换感知上下文（纯函数，加法式，task-guide-transition-aware）。

    返回 ``{transition?, branch_ctx?}``：branch_ctx 恒有（只要检测到分支）；
    transition 仅在存在聚焦任务时给出——聚焦与 next_guidance 共用 ``_classify``，
    保证与状态引导指向同一任务、永不漂移、永不陈旧。

    ``command`` 为 claim/done/review 时透传 branch_context，使 branch_ctx.hint
    按命令语义差异化（task-audit-guidance-branch-ctx-rollout）。
    """
    out: dict[str, Any] = {}
    bc = branch_context(branch, state, command)
    if bc:
        out["branch_ctx"] = bc
    cls = _classify(state, tasks, agent_id, has_master=bool(tasks), review_mode=review_mode)
    focus_tid = cls.get("focus_tid")
    if focus_tid:
        tr = transition_guidance(state.get(focus_tid), focus_tid, agent_id)
        if tr:
            out["transition"] = tr
    return out


def slim_guidance(
    guidance: dict[str, Any],
    ctx: dict[str, Any] | None,
    tier: int,
    max_read: int = 2,
) -> dict[str, Any]:
    """把全量 guidance + 转换上下文收敛为分级精简结构（纯函数，W-1）。

    W-1 信息瘦身口径（owner 已确认）：单视角 + 5 键 / 红线上限 1 条 /
    hint 单行。本函数在命令响应层做最终收敛，纯函数层（next_guidance 等）
    保持可测且不推倒重写。

    Args:
        guidance: ``next_guidance`` 产出的全量引导（含 agent_view/project_view、
            template、rules 等将被裁剪的字段）。
        ctx: ``context_guidance`` 产出的转换上下文（branch_ctx / transition）。
        tier: ``guidance_tier(cmd)`` 的分级值。
        max_read: read[] 条数上限（默认 2，指向文件按需读）。

    Returns:
        - tier=0 → ``{}``（读/健康命令：guidance 置空，由调用方省略键）。
        - tier=1 → ``{step, command?, hint?, branch_ctx?}``（轻写/恢复：不带 read）。
        - tier=2 → ``{step, command?, hint?, read[], branch_ctx?}``（决策写）。

    收敛规则：
        去 agent_view / project_view / template（template 并入 read 前缀）；
        transition 语义与红线 ≤1 条一并并入 hint（一行）；
        rules 不再作为顶层独立键（防信息超载），read≤max_read 兜底。
        契约（task-audit-guidance-contract-unify）：command 无可执行命令、hint
        为空时省略对应键，不给空串（""）；read 为空数组时保留空列表。
    """
    if tier == 0:
        return {}
    out: dict[str, Any] = {
        "step": guidance.get("step", "check_status"),
    }
    # 契约（task-audit-guidance-contract-unify）：无内容的键省略而非给空串——
    # command 无可执行命令时省略该键（弱 LLM 解析不歧义）。
    cmd = guidance.get("command")
    if cmd:
        out["command"] = cmd
    # 顶层键收敛（W-1）：transition 语义与红线 ≤1 条并入 hint（一行）
    hint = _merge_hint(guidance, ctx)
    # 契约（task-audit-guidance-contract-unify）：hint 为空时省略该键，不给 ""。
    if hint:
        out["hint"] = hint
    bc = (ctx or {}).get("branch_ctx")
    if isinstance(bc, dict) and bc.get("hint") and tier >= 1:
        out["branch_ctx"] = bc
    if tier >= 2:
        out["read"] = _merge_read(guidance, max_read)
        # task-guidance-read-versions：read_versions 与 read 同步保留（加法式），
        # 仅保留 read 中实际存在路径的版本信息，避免与截断后的 read 不一致。
        _rv = guidance.get("read_versions") or {}
        if _rv:
            out["read_versions"] = {k: v for k, v in _rv.items() if k in out["read"]}
    return out


def _merge_hint(
    guidance: dict[str, Any],
    ctx: dict[str, Any] | None,
) -> str:
    """合并 base_hint + 红线摘要 + transition hint 为单行（去重保序，带预算）。

    W-1 收敛：transition 语义与红线 ≤1 条一并并入 hint，rules 不再作为
    顶层独立键（防信息超载）。无内容时返回空串（调用方据此省略 hint 键）。

    task-guidance-completeness-gate 两级预算（AC4）：
    - 红线段（``红线：`` + rules[0] 的 TL;DR 全文，原文可达 306 字符）截断到
      :data:`_REDLINE_MAX`——保留「红线：」前缀 + 首条要点 + 省略号；
    - 合并结果再收敛到 :data:`_HINT_MAX` 以内（红线排在 transition 之前，
      超限时优先牺牲可裁剪的 transition 上下文，红线要点保持完整可见）。
    """
    hint_bits: list[str] = []
    # 顺序契约（task-output-order-determinism AC4）：hint 恒定段恒在易变段之前。
    # 稳定段 = base_hint（step 说明/首次引导）+ 红线段（静态纪律摘要）；
    # 易变段 = transition hint（含 task_id/实测命令）+ degraded（运行期路径细节）。
    # 该顺序保证：宿主按前缀缓存时，同一输入的前缀块命中不受易变尾部扰动；
    # 预算超限时 _truncate 自末尾截断，首先牺牲易变段，稳定段要点保持完整。
    base_hint = guidance.get("hint")
    if base_hint:
        hint_bits.append(base_hint)
    rules = guidance.get("rules") or []
    if rules:
        _read_for_redline = guidance.get("read") or []
        hint_bits.append(_truncate_redline_semantic(
            f"{_REDLINE_PREFIX}{rules[0]}",
            budget=_HINT_MAX,
            read_paths=_read_for_redline,
            rule_count=len(rules),
        ))
    ctx = ctx or {}
    transition = ctx.get("transition")
    if isinstance(transition, dict) and transition.get("hint"):
        hint_bits.append(transition["hint"])
    degraded = guidance.get("degraded")
    if isinstance(degraded, dict) and degraded.get("kind"):
        # 降级留痕可见化：渲染层不再硬编码 200 字符裁剪，hint 可容纳完整细节。
        _detail = degraded.get("detail") or degraded.get("dropped") or ""
        _detail_str = f"（{_detail}）" if _detail else ""
        hint_bits.append(f"{_DEGRADED_PREFIX}{degraded['kind']}{_detail_str}")
    merged = "；".join(dict.fromkeys(hint_bits))  # 去重保序，单行
    return _truncate(merged, _HINT_MAX)


def _merge_read(
    guidance: dict[str, Any],
    max_read: int,
) -> list[str]:
    """合并 read[] + template[]（template 并入 read 前缀），截断到 max_read。

    W-1 收敛：去 agent_view / project_view / template（template 并入 read）。
    read 为空数组时保留空列表（契约：不给 None）。
    """
    read = list(guidance.get("read") or [])
    for t in guidance.get("template") or []:
        if t not in read:
            read.append(t)  # template 并入 read 前缀
    return read[:max_read]


def apply_guidance_mode(
    guidance: dict[str, Any],
    ctx: dict[str, Any] | None,
    tier: int,
    mode: str = "slim",
) -> dict[str, Any]:
    """按用户选择的 guidance 模式输出（task-audit-guidance-tier-switch AC1/2/3）。

    运行时开关 ``--guidance=slim|full``（缺省 slim），在命令层控制 guidance
    字段丰俭，与静态命令分级 ``guidance_tier`` 正交：tier 决定"该不该给引导"，
    mode 决定"给多少字段"。

    Args:
        guidance: ``next_guidance`` 产出的全量引导。
        ctx: ``context_guidance`` 产出的转换上下文（branch_ctx / transition）。
        tier: ``guidance_tier(cmd)`` 的分级值（0 = 读/健康命令省略 guidance）。
        mode: ``"slim"``（默认）或 ``"full"``。

    Returns:
        - tier=0 → ``{}``（读/健康命令：guidance 置空，由调用方省略键）。
        - ``slim`` → ``{step, command?, hint?}`` 核心三字段（省 token，默认）。
        - ``full`` → ``{step, command?, hint?, read[], rules?, branch_ctx?}``
          全量（调试 / 弱 LLM 场景，等同当前 tier=2 行为并保留 rules）。
    """
    if tier == 0:
        return {}
    if mode == "full":
        # full：复用 slim_guidance 的 tier=2 收敛（含 read / branch_ctx），
        # 并额外保留 rules 字段（slim 模式下 rules 被并入 hint，full 独立呈现）。
        out = slim_guidance(guidance, ctx, tier=2)
        rules = guidance.get("rules") or []
        if rules:
            out["rules"] = rules
        return out
    # slim：仅 step / command / hint 核心三字段，去掉 read / rules / branch_ctx。
    slim_out: dict[str, Any] = {"step": guidance.get("step", "check_status")}
    cmd = guidance.get("command")
    if cmd:
        slim_out["command"] = cmd
    # hint 构造与 slim_guidance 共用 _merge_hint 单一实现（含红线摘要与降级留痕），
    # 避免两处各写一份导致口语/留痕不一致。
    hint = _merge_hint(guidance, ctx)
    if hint:
        slim_out["hint"] = hint
    return slim_out


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
        引导结构：顶层 5 键 + agent_view + project_view；发生降级时另附
        ``degraded`` 标记（加法式，非降级场景键集不变）。
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
    # 降级留痕（AC6，加法式）：仅在该视角发生降级时出现，非降级场景键集不变。
    if isinstance(agent_view.get("degraded"), dict):
        out["degraded"] = agent_view["degraded"]
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
