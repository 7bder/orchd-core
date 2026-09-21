"""orchd/guide.py — 引擎侧无感用户流程引导（task-guide-seamless-guidance）。
# E008/E009 claim语义归位 - guide table already correct per N1

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

import hashlib
import os
from pathlib import Path
from typing import Any

# 引导语义：返回 {step, read, template, command, hint} 结构，供 agent/用户据以行动。
# step    —— 建议执行的下一步动作标识；**全部取值见模块级常量 GUIDANCE_STEPS**
#             （单一事实源，此处不复述清单——历史教训：复述清单漏了 5 个实际取值，
#             并留了一个全仓无人返回的死词汇 request_new_idea）
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


# ---------------------------------------------------------------------------
# guidance step 词表与旁路导航映射（单一事实源，task-guide-step-vocab-and-routing-fix）
# ---------------------------------------------------------------------------
# 本集合登记 **guidance 的 step 全部可能取值**：各分支、模块文档与完备性断言
# （tests/test_guidance_sync.py）均以此为准，禁止散落裸字面量。
#
# ⚠ 扫描范围提示：orchd/gitops_ops.py 里也有 "step" 键（create / checkout），
# 那是**分支状态字段**、与 guidance 语义无关；做全包 AST 扫描时必须排除该处，
# 不得把它并入本词表。
GUIDANCE_STEPS: frozenset[str] = frozenset({
    "first_time",       # 未初始化项目（无 _master.json）
    "empty_project",    # 已初始化但 0 任务
    "claim_review",     # 有可领审查任务
    "submit_review",    # 本 agent 已领审查、待提交结论（原实现缺此出口 → 死循环）
    "wait_review",      # 审查已被他人领取 / 有任务在实现中
    "rework_first",     # 有返工任务（曾被审查打回）待认领
    "request_impl",     # 有待认领实现任务
    "done",             # 本 agent 已认领实现任务，待 done
    "optional_cancel",  # 存在已取消任务（可选处置）
    "done_all",         # 全部任务已完成
    "check_status",     # 兜底：无可匹配分支
    "stop_wait",        # request 无候选：停止等待用户指令
    "lesson_review",    # done 收尾挂起：lessons 暂存待人工审核
    "recover",          # 错误恢复指引（error_guidance）
    "audit_merge",      # review code APPROVED 后附 audit_merge guidance（task-review-completion-guidance）
})

# 旁路导航（next_action）词表 + → guidance step 的**单一映射（单一决策源）**。
# next_action 由 orchd/onboard/request.py、orchd/review.py 与
# orchd/onboard/lifecycle/core.py 产出；历史上它与 guidance.step 是两套互不相识的
# 词表（无一致性约束），且各调用点各自裸写字面量。
# task-guidance-completeness-gate 完成**调用点合流**：词表只在 guide 声明，
# 调用点一律引用下列常量，不得再写裸字面量——tests/test_guidance_sync.py 断言
# 「orchd/ 内除本模块外无任何 next_action 字符串字面量」。
NEXT_ACTION_EXIT: str = "exit"                  # request/review 无候选：停止等指令
NEXT_ACTION_WAIT: str = "wait"                  # request 触达 max_active：等待
NEXT_ACTION_REVIEW_FIRST: str = "review_first"  # request 有可领审查：优先领取
NEXT_ACTION_REVIEW_TAKEOVER: str = "review_takeover"  # 僵尸审查认领：可接管
NEXT_ACTION_AWAIT_REVIEW: str = "await_review"  # done 收尾 lessons 待审核
# 本会话已领审查未提交（review_claimed_by == 自己）：先提交手上的结论，不谈领新任务。
# task-guidance-review-priority-fix 补登记：此前 submit_review 只有 step 词表条目而无
# 对应 next_action 取值 → request 在「已持有审查认领」时只能产 review_first，把会话引向
# 他任务的审查（必被 claim 的 E011 review busy 拒绝），手上的认领就此悬空未提交。
NEXT_ACTION_SUBMIT_REVIEW: str = "submit_review"

NEXT_ACTION_TO_STEP: dict[str, str] = {
    NEXT_ACTION_EXIT: "stop_wait",
    NEXT_ACTION_WAIT: "wait_review",
    NEXT_ACTION_REVIEW_FIRST: "claim_review",
    # review_takeover（request 巡检到僵尸审查认领，candidate=None 时抬升）：动作语义
    # 是「接管并重新领取审查」（retract REVIEW_CLAIMED → re-claim），归入 claim_review。
    NEXT_ACTION_REVIEW_TAKEOVER: "claim_review",
    NEXT_ACTION_AWAIT_REVIEW: "lesson_review",
    NEXT_ACTION_SUBMIT_REVIEW: "submit_review",
}


def step_for_next_action(next_action: str | None) -> str:
    """next_action → guidance step（单一映射；未知取值兜底 check_status）。

    未登记取值返回 ``check_status`` 是**降级**语义：映射覆盖率由测试锁定
    （新增 next_action 取值而未登记 → 测试失败），不在运行期静默猜测。
    """
    if not next_action:
        return "check_status"
    return NEXT_ACTION_TO_STEP.get(next_action, "check_status")


def amend_patch_cmd(task, files=None, exempt=None, verify=None, entry=_ENTRY_CMD):
    """amend 声明域补登命令的单一事实源（task-amend-decl-patch-channel）。

    guide.py 逃生文案、hook.py 逃生 echo、测试断言皆由此派生，禁止手写
    ``amend --task/--files-to-edit/--exempt-files/--verify-command`` 字符串。
    占位符（``<id>``/``{task_id}``/``<cmd>``）按原文透传，调用方负责替换。
    """
    parts = [entry, "amend", "--task", str(task)]
    for f in files or []:
        parts += ["--files-to-edit", str(f)]
    for f in exempt or []:
        parts += ["--exempt-files", str(f)]
    if verify is not None:
        parts += ["--verify-command", str(verify)]
    return " ".join(parts)


def amend_mainwt_command(task, main_wt, files=None, exempt=None, verify=None,
                         entry=_ENTRY_CMD):
    """带主工作树绝对路径的 amend 补登命令（task-amend-guidance-mainwt）。

    在 amend_patch_cmd 基础上前缀 cd "<main_wt>";，使持任务 agent
    可直接复制执行——任务 worktree 内无 _master.json，amend 必须回主工作树。
    跨平台用分号分隔（PowerShell / bash / cmd 均兼容）；main_wt 含空格时
    双引号包裹。占位符按原文透传，调用方负责替换。
    """
    cmd = amend_patch_cmd(task, files=files, exempt=exempt, verify=verify, entry=entry)
    return f'cd "{main_wt}"; {cmd}'

# ---------------------------------------------------------------------------
# W-1 guidance 分级：T0/T1/T2（task-guide-tiering）
# ---------------------------------------------------------------------------
# 信息瘦身口径（owner 已确认）：单视角 + 5 键 / 红线上限 1 条 / hint 单行。
# 全命令分三级注入，避免"每条命令灌全量、三层重复"的信息超载。
_T0_CMDS = frozenset({"status", "doctor", "help", "version", "session-status",
                      "session-current", "session", "pool", "validate",
                      "context-digest"})
_T1_CMDS = frozenset({"retract", "force-status"})


def guidance_tier(command: str) -> int:
    """命令 → 引导分级（纯函数，W-1）。

    T0  读/健康命令：guidance 置空（仅保留退出态一行）。
    T1  轻写/恢复命令：只给 step + command + hint + 1 条红线 + branch_ctx。
    T2  决策写命令：完整但精简（step/command/hint/read/branch_ctx 5 键）。
    未列出的命令（含子命令首 token，如 "session"）默认 T2。
    """
    if not command:
        return 2
    base = command.strip()
    if base in _T0_CMDS:
        return 0
    if base in _T1_CMDS:
        return 1
    return 2


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
      实际文件即保留，都不存在则剔除（best-effort，不抛异常）——剔除项由
      :func:`resolve_read_paths` 写 ``degraded`` 留痕，不再静默。
    - 绝对路径直接判定，不做拼接。
    - ``base_dir`` 为 None 或空 → 不做存在性校验，原样返回（纯函数 / 单测场景）。
    - 空数组 → 返回空数组（无害，向下兼容）。

    Args:
        paths: read 或 template 路径数组。
        base_dir: 规则/模板所在根目录（.orchd/）；None 时跳过校验。

    Returns:
        过滤后只含实际存在文件的路径数组（顺序保持）。
    """
    kept, _dropped = _split_paths(paths, base_dir)
    return kept


def _split_paths(
    paths: list[str],
    base_dir: str | os.PathLike[str] | None,
) -> tuple[list[str], list[str]]:
    """把 read/template 路径拆成 ``(存在的, 不存在的)``（存在性判定单一实现）。

    task-guide-step-vocab-and-routing-fix：原先「不存在即静默剔除」让路由漂移
    不可见（缺哪个规则文件无人知道）。拆出 dropped 供 :func:`resolve_read_paths`
    写 ``degraded`` 降级标记留痕（对齐 conflict_policy 的 degraded_guards 约定），
    同时保持 :func:`_resolve_paths` 的既有签名与语义不变。
    """
    if not paths or base_dir is None:
        return list(paths), []
    root = os.path.abspath(os.fspath(base_dir))
    parent = os.path.dirname(root)
    kept: list[str] = []
    dropped: list[str] = []
    for p in paths:
        if os.path.isabs(p):
            (kept if os.path.isfile(p) else dropped).append(p)
            continue
        candidates = (os.path.join(root, p), os.path.join(parent, p))
        (kept if any(os.path.isfile(c) for c in candidates) else dropped).append(p)
    return kept, dropped


def resolve_read_paths(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """将 guidance 的 read/template 字段解析为指向实际存在文件的路径（纯函数）。

    知识路由闭环的引擎侧接口（task-guide-routing-loop）：调用方（cli.py
    ``_attach_guidance`` 注入点）在附加 guidance 后透传 ``base_dir``，本函数
    返回**新增 ``read``/``template`` 键**的 guidance 副本，指向实际存在的规则/
    模板文件，不存在时剔除并写 ``degraded`` 留痕（不再静默）。
    空数组 / base_dir None 时原样返回（无害降级）。

    Args:
        guidance: 原始 guidance 字典（含 read/template 数组）。
        base_dir: 规则/模板根目录（.orchd/）；None 时跳过校验。

    Returns:
        过滤后的 guidance 字典（加法式，仅调整 read/template 两键，并在发生
        剔除时附加 ``degraded`` 标记；不碰 step/command/hint 结构；含
        agent_view/project_view 时递归过滤，保证双视角与顶层 read/template 一致）。
    """
    out = dict(guidance)
    read_kept, read_dropped = _split_paths(guidance.get("read") or [], base_dir)
    tpl_kept, tpl_dropped = _split_paths(guidance.get("template") or [], base_dir)
    out["read"] = read_kept
    out["template"] = tpl_kept
    dropped = sorted(set(read_dropped) | set(tpl_dropped))
    if dropped:
        # AC6 降级留痕：路径缺失不再静默——写 degraded 标记，由 slim 层并入 hint。
        out["degraded"] = {"kind": "read_paths_missing", "dropped": dropped}
    # 双视角（task-guidance-dual-view-engine）：递归过滤子视角的 read/template，
    # 避免顶层已过滤而 agent_view/project_view 仍指向不存在路径的不一致。
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = resolve_read_paths(sub, base_dir)
    return out


def attach_read_versions(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """为 guidance 的 read[] 每条路径附加 {mtime, size} 版本标注（纯函数，加法式）。

    task-guidance-read-versions：配合读取纪律，agent 依据版本标注判断规则/模板
    文件是否变化，未变则不重读。本函数**只读 stat，不读文件内容**。

    - best-effort：文件缺失 / stat 异常跳过该条（不抛异常，不阻塞）。
    - 加法式：新增顶层键 ``read_versions``（dict[path, {mtime, size}]），
      ``read[]`` 保持字符串数组契约不变。
    - 双视角递归：agent_view / project_view 若为 dict 则同样附加。
    - 空 read / base_dir None 时 read_versions={}（无害降级）。

    Args:
        guidance: 已通过 resolve_read_paths 过滤的 guidance 字典。
        base_dir: 规则/模板根目录（.orchd/）；None 时跳过 stat。

    Returns:
        新增 read_versions 键的 guidance 副本（不修改入参）。
    """
    out = dict(guidance)
    versions: dict[str, dict[str, float | int]] = {}
    if base_dir is not None:
        base = Path(base_dir)
        for rel in guidance.get("read") or []:
            if not isinstance(rel, str):
                continue
            try:
                st = (base / rel).stat()
                versions[rel] = {"mtime": st.st_mtime, "size": st.st_size}
            except (OSError, ValueError):
                continue  # best-effort：缺失/异常跳过该条
    out["read_versions"] = versions
    # 双视角递归
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = attach_read_versions(sub, base_dir)
    return out


# 必读面内容哈希（task-context-digest-command）：session-doc-digest 的引擎侧最小闭环。
# 覆盖面单一真源：显式文件清单 + rules/*.md 目录枚举（sorted）。只读输出，
# 无时间戳 / pid / mtime 等易变字段，同一输入字节稳定。
CONTEXT_DIGEST_FILES = (
    ".orchd/SKILL.md",
    "AGENTS.md",
    ".orchd/shared/conventions.md",
    ".orchd/shared/architecture.md",
)


def _digest_one(path: Path, rel: str) -> dict[str, Any]:
    """单个必读面文件的内容哈希记录（best-effort：缺失/异常标 exists=false，不抛异常）。"""
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        return {"path": rel, "sha256": "", "bytes": 0, "exists": False}
    return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "exists": True}


def context_digest(project_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """必读面文件的内容哈希与字节数（纯函数，只读，task-context-digest-command）。

    与 :func:`attach_read_versions` 同层（best-effort 容错风格），但判据是内容
    sha256 而非 mtime/size（checkout/clone 改 mtime 而内容相同、size 相同内容
    不同时 mtime/size 均误判）。

    Returns:
        ``{"files": [...], "digest": <hex>, "root": <str>}``：
        files 每项 ``{"path", "sha256", "bytes", "exists"}``（path 为相对项目根
        的正斜杠路径，按 path 升序；缺失文件 exists=false、sha256 为空串）；
        digest 为已存在文件 ``path:sha256`` 行拼接的 sha256（聚合指纹）；
        root 为解析用项目根（调用方传入原值，跨调用稳定）。
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    entries: list[dict[str, Any]] = [_digest_one(root / rel, rel) for rel in CONTEXT_DIGEST_FILES]
    try:
        rules_dir = root / ".orchd" / "rules"
        entries.extend(
            _digest_one(rules_dir / p.name, ".orchd/rules/" + p.name)
            for p in sorted(rules_dir.glob("*.md"))
            if p.is_file()
        )
    except OSError:
        pass
    entries.sort(key=lambda e: e["path"])
    agg = hashlib.sha256(
        "\n".join(f"{e['path']}:{e['sha256']}" for e in entries if e["exists"]).encode("utf-8")
    ).hexdigest()
    return {"files": entries, "digest": agg, "root": str(root)}


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
    # 本 agent 已领未提交的审查（task-guide-step-vocab-and-routing-fix）：原实现只
    # 计数 my_in_review 却不产出出口，导致 in_review 全被领走时引导指回 request。
    my_review_tid: str | None = None
    my_review_phase: str | None = None
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
                if my_review_tid is None:
                    my_review_tid = tid
                    my_review_phase = ts.review_phase or "unified"
            if ts and ts.review_claimed_by is None and first_unclaimed_review is None:
                first_unclaimed_review = tid
                first_unclaimed_review_phase = ts.review_phase or "unified"
    counts["total"] = len(tasks)
    counts["my_claimed"] = my_claimed
    counts["rework"] = rework
    counts["first_rework_tid"] = first_rework_tid
    counts["first_unclaimed_review"] = first_unclaimed_review
    counts["first_unclaimed_review_phase"] = first_unclaimed_review_phase
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
    if c.get("in_review", 0) > 0:
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
    if c["pending"] > 0 and not c.get("my_claimed"):
        # 忙度只看**本 agent** 的 claimed（原实现用全局 claimed，导致其他会话一旦有
        # 任务在实现，本 agent 永远拿不到 request_impl，与 request 的候选结论打架）
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
        "read": ["skill-lesson.md", "rules/session.md"],
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
    review_templates = (
        ["templates/reviewer.md"]
        if review_mode == "unified"
        else ["templates/spec-reviewer.md", "templates/code-reviewer.md"]
    )
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
            "read": ["rules/review.md", "rules/testing.md"],
            "template": review_templates,
            "command": cmd,
            "hint": (
                f"有 {c['in_review']} 个任务待审查：领取审查任务（{phase_label} 阶段）——"
                f"认领须在任务 worktree 内执行且带 --confirm；审查结论也须在任务 worktree 内提交"
                f"（E018 守卫：目标目录必须等于任务 worktree，防错目录审查）；提交成功后引擎回收"
                f"该 worktree，回收前请先切出任务目录（Windows 句柄占用会导致回收失败）；"
                f"代码审查通过任务才算完成。"
            ),
        }
    if step == "submit_review":
        tid = c.get("my_review_tid")
        phase = c.get("my_review_phase")
        unified = review_mode == "unified" or not phase or phase == "unified"
        # review 的 --type choices 仅 [spec, code]；unified 单阶段模式须省略该参数。
        type_arg = "" if unified else f" --type {phase}"
        phase_label = "unified" if unified else phase
        return {
            "step": "submit_review",
            "read": ["rules/review.md", "shared/conventions.md"],
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
            "read": ["rules/review.md", "rules/session.md", "rules/testing.md"],
            "template": ["templates/implementer.md"],
            "command": f"{_ENTRY_CMD} request",
            "hint": (
                f"有 {c['rework']} 个返工任务待认领（已被审查打回）：优先处理避免积压，"
                "认领后请先读 review_comments 中的前次审查意见。"
            ),
        }
    if step == "request_impl":
        return {
            "step": "request_impl",
            # rules/safety.md（引擎改动触碰 §9.1 停服边界）路由到实现入口：领实现任务时
            # 先确认改动是否触碰停服边界（task-guidance-completeness-gate 路由补全）。
            "read": ["rules/session.md", "rules/intake.md", "rules/testing.md",
                     "rules/safety.md"],
            "template": ["templates/implementer.md"],
            "command": f"{_ENTRY_CMD} request",
            # hint 保持紧凑（task-guidance-block-budget-root-fix：预算由 _BLOCK_MAX
            # 求和不等式统一管理，不再有 200 字符硬编码上限）；
            # 测试纪律经 read 路由送达，不挤占 hint 额度。
            "hint": f"有 {c['pending']} 个待认领任务：现在没有活跃实现，可领取一个新任务。",
        }
    if step == "done":
        tid = cls["focus_tid"]
        return {
            "step": "done",
            # read 按**本 step 的优先级**排序：slim 层截断（max_read）时先丢次要项，
            # verify.md 必须保留（done 的第一动作就是确认 verify_command 与预算）。
            # rules/windows.md（Windows 下 shell/管道编码陷阱）随 done 路由：
            # verify_command 由引擎在本机执行，Windows 环境约束与该步直接相关
            # （task-guidance-completeness-gate 路由补全）。
            "read": ["rules/verify.md", "rules/testing.md", "rules/session.md",
                     "rules/git.md", "rules/windows.md"],
            "template": ["templates/implementer.md"],
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
            "read": ["rules/review.md"],
            "template": [],
            "command": f"{_ENTRY_CMD} status --text",
            "hint": "有任务正在实现或已提交待审查：等待实现者 done 或审查者 review，"
                    f"用 {_ENTRY_CMD} status --text 查看最新进展。",
        }
    if step == "optional_cancel":
        return {
            "step": "optional_cancel",
            "read": [],
            "template": [],
            "command": f"{_ENTRY_CMD} status --text",
            "hint": f"存在已取消任务：可忽略，或用 {_ENTRY_CMD} force-status 改为 pending 重新评估。",
        }
    if step == "done_all":
        return {
            "step": "done_all",
            "read": [],
            "template": [],
            "command": f"{_ENTRY_CMD} status --text",
            "hint": "所有任务已完成：可提交新 idea 供拆解，或进入下一阶段规划。",
        }
    if step == "audit_merge":
        # review code APPROVED + merge 成功后的完成态引导（task-review-completion-guidance）。
        # 实际构造由 orchd/review.py _build_completion_guidance 完成（含 worktree
        # 回收残留处置），此处保留词表构造分支以满足 GUIDANCE_STEPS 单一来源约束。
        return {
            "step": "audit_merge",
            "read": ["rules/review.md"],
            "template": [],
            "command": f"{_ENTRY_CMD} status --audit-merge",
            "hint": "任务已审查通过并合并：请在主工作树执行 status --audit-merge 确认无警告。",
        }
    # 兜底：无可匹配分支（pending 被阻塞 / 未知状态组合）——留痕，避免兜底静默
    return {
        "step": "check_status",
        "read": ["rules/session.md"],
        "template": [],
        "command": f"{_ENTRY_CMD} status --text",
        "hint": (
            "查看当前任务池状态，确认下一步可执行的命令"
            "（当前无可匹配的引导分支，已降级兜底）。"
        ),
        "degraded": {"kind": "no_branch_matched", "fallback_step": "check_status"},
    }


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
    if s == "claimed":
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
            "read": ["rules/session.md", "rules/git.md", "rules/verify.md", "rules/testing.md"],
        }
    if s in ("done", "in_review"):
        reviewing = "、正在审查中" if (s == "in_review" and ts.review_claimed_by) else ""
        return {
            "type": "done_submitted",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已提交待审查{reviewing}：不要在任务分支继续改动，"
                "审查通过后引擎自动合并并回收任务环境。"
                f"审查者：code 阶段 APPROVED 后须回主工作树运行 {_ENTRY_CMD} status "
                "--audit-merge 确认零告警。"
            ),
            "read": ["rules/review.md", "rules/git.md"],
        }
    return None


def _terminal_transition(
    s: str,
    task_id: str,
) -> dict[str, Any] | None:
    """终态转换块：completed（已合并）/ cancelled（已取消）。不匹配返回 None。"""
    if s == "completed":
        return {
            "type": "approved_completed",
            "task_id": task_id,
            "hint": (
                f"任务 {task_id} 已审查通过并合并（completed）：任务完成，"
                "其分支/环境已回收，可进入下一步。"
                f"审查者请运行 {_ENTRY_CMD} status --audit-merge 确认 "
                "merge_audit.warnings 为空（rules/review.md 硬要求）。"
            ),
            "read": ["rules/review.md"],
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
    return None


def _rework_transition(
    s: str,
    ts: Any,
    task_id: str,
) -> dict[str, Any] | None:
    """返工转换块：pending 且 attempt_count > 0（被审查打回）。不匹配返回 None。"""
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
    if branch.startswith("task/"):
        tid = branch[len("task/"):]
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
    if branch in ("main", "master"):
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


def _truncate(text: str, limit: int) -> str:
    """超限时保留前缀 + 省略号（省略号单字符，结果不超 limit）。

    task-guidance-completeness-gate：hint 预算的统一截断原语（红线段与总量共用），
    避免两处各写一份截断逻辑导致口径漂移。
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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
    out: dict[str, Any] = {"step": guidance.get("step", "check_status")}
    cmd = guidance.get("command")
    if cmd:
        out["command"] = cmd
    # hint 构造与 slim_guidance 共用 _merge_hint 单一实现（含红线摘要与降级留痕），
    # 避免两处各写一份导致口语/留痕不一致。
    hint = _merge_hint(guidance, ctx)
    if hint:
        out["hint"] = hint
    return out


# 红线并入 hint 的前缀（task-guide-tiering：只留 ≤1 条最强红线）
_REDLINE_PREFIX: str = "红线："
# 降级留痕并入 hint 的前缀（AC6：路径缺失 / 兜底分支不再静默）
_DEGRADED_PREFIX: str = "引导降级："
# hint 预算（task-guidance-completeness-gate AC4）：合并结果总量上限。
_HINT_MAX: int = 300

# task-guidance-block-budget-root-fix：stderr 提示块预算单一真源。
_BLOCK_MAX: int = 1600
_MAX_COMMAND_LEN: int = 200
_MAX_READ_LEN: int = 500
_MAX_CASES_LEN: int = 400


def _block_layout_overhead() -> int:
    """渲染布局固定开销（sep*2 + 前缀 + 换行 + 空行）。"""
    sep = "─" * 40
    return (
        1 + len(sep) + 1 +
        len("orchd ▸ ") + 1 +
        len("建议执行：") + 1 +
        len(sep) + 1 + 1
    )


def _assert_budget_consistency() -> None:
    """常量自洽断言：布局开销 + 各内容预算 <= _BLOCK_MAX。"""
    total = _block_layout_overhead() + _HINT_MAX + _MAX_COMMAND_LEN + _MAX_READ_LEN + _MAX_CASES_LEN
    assert total <= _BLOCK_MAX, (
        f"guidance block budget overflow: {total} > {_BLOCK_MAX}"
    )


_assert_budget_consistency()


def _truncate_redline_semantic(text, budget, read_paths, rule_count=1):
    """红线语义截断：按点分割只装整点，装不下退化为红线 N 条（见路径）。"""
    if len(text) <= budget:
        return text
    import re
    points = re.split(r'(?<=[；。;])', text)
    fitted = []
    current = ""
    for p in points:
        if not p:
            continue
        if len(current) + len(p) <= budget - 1:
            current += p
            fitted.append(p)
        else:
            break
    if fitted:
        return current + "…"
    read_hint = read_paths[0] if read_paths else "rules/review.md"
    return f"{_REDLINE_PREFIX}{rule_count} 条（见 {read_hint}）"


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
    max_rules: int = 1,
) -> dict[str, Any]:
    """对 guidance 的 read 数组生成 rules 键（加法式，纯函数）。

    知识路由闭环的摘要层（task-guidance-rule-summary）：调用方（cli.py
    ``_attach_guidance`` 注入点）在 ``resolve_read_paths`` 之后透传 base_dir，
    本函数返回**新增 ``rules`` 键**的 guidance 副本，内容为 read 数组对应
    规则文件的 TL;DR 摘要（``<文件名>: <TL;DR>``），无 TL;DR 段跳过。

    W-1 红线上限（task-guide-tiering）：仅保留前缀 ``max_rules`` 条摘要
    （默认 1 条），其余规则交由 ``read[]`` 指向文件按需读——避免"红线列表"
    信息超载同时保留关键约束提示。

    - 顶层新增 rules 键；agent_view / project_view 递归处理（与
      resolve_read_paths 同构），保证双视角与顶层 rules 一致。
    - 不碰 step/read/template/command/hint 既有字段（加法式）。
    - read 为空 / base_dir None 时返回 rules=[]（无害降级）。
    """
    out = dict(guidance)
    out["rules"] = _summarize_rules(guidance.get("read") or [], base_dir)[:max_rules]
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = attach_rule_summaries(sub, base_dir, max_rules=max_rules)
    return out


# 错误恢复指引映射（error recovery guidance）：错误码 → 恢复指引。
# 只提示不代行——不自动执行任何修复命令，修复决策权始终在人/agent。
# (task-audit-error-guidance-coverage) 数据化：14 个结构相同的 dict 收敛为
# 表格常量 + 循环构建，覆盖 ErrorCode 全部 36 个枚举成员——新增错误码未补指引时，
# 测试 set(ERROR_GUIDANCE) ⊇ {e.name for e in ErrorCode} 直接失败。
# (task-errexit-type-contract) 静态表 4 元组扩为 5 元组，新增 exit_type
# {exec-command, git-diagnose, manual-action, await-external, continue}，
# warning 码统一 continue，锁/被占类统一 await-external 且 recovery 含“不要重试原命令”。
_ERROR_GUIDANCE_TABLE: tuple[tuple[str, str, tuple[str, ...], str, str, str], ...] = (
    # (错误码, recovery, read 规则文件, command, tier, exit_type)
    ("E001", "源码/任务文件缺失：补齐或修正 details.path 指向的文件路径后重试，运行 python .orchd/__main__.py validate 定位缺失文件", ("rules/intake.md",), f"{_ENTRY_CMD} validate", "suggest", "manual-action"),
    ("E002", "JSON 解析失败：用 doctor 检查并修复损坏的 intake/schema 文件", ("rules/intake.md",), f"{_ENTRY_CMD} doctor", "suggest", "exec-command"),
    ("E003", "schema 校验失败：运行 validate 定位并修复不符项后重试", ("rules/intake.md",), f"{_ENTRY_CMD} validate", "suggest", "exec-command"),
    ("E004", "检测到 DAG 依赖环：检查 depends_on 引用，消除循环依赖", ("rules/intake.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E005", "引用缺失：补全被引用的任务/源条目（IDEAS/ROADMAP/source）", ("rules/intake.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E006", "ID 重复：确认任务标识唯一，合并或重命名冲突项", ("rules/intake.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E007", "任务状态不合法：当前 {current} 不允许 {event_type}→{target}，允许集合 {allowed}，请用 python .orchd/__main__.py status 查看当前状态并修正", ("rules/session.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E008", "任务未就绪：确认任务处于 pending 状态再 claim", ("rules/session.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E009", "任务已被认领：已被 {holder} 认领，不要重试原命令，等待其完成或由其 retract", ("rules/session.md",), f"{_ENTRY_CMD} status --text", "suggest", "await-external"),
    ("E010", "文件冲突：与 {claimed_by} 任务 files_to_edit 重叠（{files}），需串行化或合并任务，等待其 done 后重试", ("rules/intake.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E011", "agent 忙：已持有其他任务，不要重试原命令，先完成或 retract 再领新任务", ("rules/session.md",), f"{_ENTRY_CMD} status --text", "suggest", "await-external"),
    ("E012", "锁持有超时：用 watchdog 查看锁持有者，不要重试原命令，失效则清理并重试", ("rules/recovery.md",), f"{_ENTRY_CMD} watchdog", "suggest", "await-external"),
    ("E013", "引擎未初始化：先运行 init 初始化工作区再操作", ("rules/install.md",), f"{_ENTRY_CMD} init", "auto", "exec-command"),
    ("E014", "verify 失败：查看 verify 输出，修复实现后重试 done", ("rules/verify.md",), "python -m pytest <定向测试>", "suggest", "exec-command"),
    ("E015", "合并冲突：停止操作并报告冲突详情，人工裁决（勿自行强推）", ("rules/git.md",), "git status", "manual", "git-diagnose"),
    # E016 命令相位无关（task-guidance-completeness-gate AC6）：claim 的 --type choices
    # 仅 [spec, code]，unified 单阶段须省略该参数——历史上此处写 `--type review` 属非法取值
    # （与 _review_step_guidance 的 unified 分支口径一致）。
    ("E016", "自审被阻断：当前会话已实现 {task_id}，需换会话/身份重新 claim --task {task_id} --confirm（相位无关），或由他人审查", ("rules/review.md",), f"{_ENTRY_CMD} claim --task <id> --confirm", "suggest", "manual-action"),
    ("E017", "工作区脏：先提交/清理未提交改动，再重试", ("rules/git.md",), "git status", "suggest", "git-diagnose"),
    ("E018", "分支错误：确认处于正确分支", ("rules/git.md",), "git branch --show-current", "suggest", "git-diagnose"),
    ("E019", "工作区忙：检查持有会话锁的 agent，不要重试原命令，等待其释放或按需接管", ("rules/session.md",), f"{_ENTRY_CMD} watchdog", "suggest", "await-external"),
    ("E020", "范围外提交：只改 files_to_edit 声明文件", ("rules/git.md",), "git status", "suggest", "git-diagnose"),
    ("E021", "身份不匹配：确认 ORCHD_SESSION_ID 与认领者一致，必要时重连", ("rules/session.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E022", f"缺少 verify_command：任务 {{task_id}} 缺少 verify_command，请运行 {amend_patch_cmd('{task_id}', verify='<cmd --basetemp=...>')} 补充后重试", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<cmd>"), "suggest", "exec-command"),
    ("E023", "验收标准模糊（警告不阻断）：建议用可量化标准，可 amend 修订", ("rules/intake.md",), f"{_ENTRY_CMD} amend", "suggest", "continue"),
    ("E024", "verify_command 缺 --basetemp：用 amend 补充跨平台 basetemp", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<cmd --basetemp=...>"), "suggest", "exec-command"),
    ("E025", "source 引用缺失：任务 {task_id} 需关联 IDEAS.md/ROADMAP.md 条目 {source}，请补齐 source 后重试", ("rules/intake.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E026", "测试连带未声明（警告不阻断）：用 amend 声明测试或加入 exempt_files", ("rules/verify.md",), f"{_ENTRY_CMD} amend", "suggest", "continue"),
    ("E027", "verify_command 含不安全/不兼容段：用 amend 改为跨平台安全命令", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<安全命令>"), "suggest", "exec-command"),
    ("E028", f"dry-run 断言不匹配：任务 {{task_id}} 的 verify_command 断言失败，请核对断言或运行 {amend_patch_cmd('{task_id}', verify='<修正命令>')} 调整", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<修正命令>"), "suggest", "continue"),
    ("E029", "任务拆解粒度越界：任务 {task_id} 按 R4 文件≤5/行≤60/小时≤8 拆分为更小任务，参考 docs/decomposition-guide.md", ("rules/intake.md",), f"{_ENTRY_CMD} status --text", "suggest", "continue"),
    ("E030", "运行时文件完整性校验失败（警告不阻断）：用 doctor 诊断并修复引擎文件", ("rules/recovery.md",), f"{_ENTRY_CMD} doctor", "manual", "continue"),
    ("E031", "ROADMAP 规划章节未落地 IDEAS：章节 {chapter} 需先运行 python .orchd/__main__.py roadmap-land <版本> 落地为 IDEAS pending 后再 intake", ("rules/intake.md",), f"{_ENTRY_CMD} roadmap-land <版本>", "suggest", "continue"),
    ("E032", "auto-claim 被禁：需人工确认 claim 或 config.allow_auto_claim", ("rules/session.md",), f"{_ENTRY_CMD} claim --task <id> --confirm", "suggest", "exec-command"),
    ("E033", "会话身份缺失：先 session start 注入 ORCHD_SESSION_ID", ("rules/session.md",), f"{_ENTRY_CMD} session start", "suggest", "exec-command"),
    ("E034", "撤认归属守卫：仅事件作者 {owner} 或 admin 可撤回 {task_id}，当前 {caller} 无权；跨 agent 撤认仅限超时（僵尸）认领（CLAIMED 600s / REVIEW_CLAIMED 300s，env 可覆盖），未超时请停止", ("rules/session.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E035", "会话冲突告警（警告不阻断）：同一工作区多会话碰撞，确认各会话职责避免写入竞争", ("rules/session.md",), f"{_ENTRY_CMD} watchdog", "suggest", "continue"),
    ("E036", "容器根执行被拒：切换到主工作树（details.main_worktree）下执行，或设 ORCHD_ALLOW_CONTAINER_ROOT=1 豁免", ("rules/git.md",), f"{_ENTRY_CMD} status --text", "suggest", "manual-action"),
    ("E037", "verify_command 引用路径未声明且不存在：将路径加入 files_to_edit/exempt_files，或改指向实际存在的文件（声明口径一致性）", ("rules/intake.md",), f"{_ENTRY_CMD} amend --task <id> --files-to-edit <path>", "suggest", "exec-command"),
    ("E038", "brief 声明的 files_to_edit 数量口径与实际声明不一致：对齐 brief 文案与声明集合（非拆分建议，warning 不阻断）", ("rules/intake.md",), f"{_ENTRY_CMD} amend --task <id> --brief <对齐后文案>", "suggest", "continue"),
    ("E039", f"共享入口改动未覆盖登记的既有测试：{{missing}} 必须出现在 verify_command 的 pytest 目标里（否则既有断言静默变红），用 {amend_patch_cmd('{task_id}', verify='<补入登记测试后的命令>')} 补登后重试", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<补入登记测试后的命令>"), "suggest", "exec-command"),
)
# 显式豁免集：允许裸弱出口的码（默认空集，加入需逐一说理注释）
ALLOWED_WEAK: frozenset[str] = frozenset()
# E007 内部防御白名单：正常协作流不可达的位点，无需补 hint（按文件显式登记）
E007_INTERNAL_DEFENSE: frozenset[str] = frozenset({
    "orchd/lessons.py",  # 8 处字段校验，内部防御
    "orchd/split.py",  # 4 处结构校验
    "orchd/cli/commands/lessons.py:lesson",  # lesson 字段校验（cli 拆包后路径）
})

# ── 码→通道登记表（设计 §2.2 / §8.5）───────────────────────────────────────
# 每个 ErrorCode 至少登记一条可达通道，防止 E015 死映射与 B/C/D 漏接线。
# 通道含义：A=异常(raise OrchdError), B=批量校验(ValidationError), C=手工dict(return), D=Shell hook
ERROR_CODE_CHANNELS: dict[str, frozenset[str]] = {
    # ── 通道 A（异常通道）──
    "E001": frozenset({"A"}),       # file_not_found: raise OrchdError ×4
    "E002": frozenset({"A"}),       # invalid_json: raise OrchdError ×1
    "E003": frozenset({"A", "B"}),  # schema_validation_failed: raise ×2 + spec ValidationError
    "E005": frozenset({"A", "B"}),  # reference_not_found: raise ×1 + spec ValidationError
    "E007": frozenset({"A"}),       # invalid_state: raise ×48
    "E008": frozenset({"A"}),       # task_not_ready: raise ×5
    "E009": frozenset({"A"}),       # already_claimed: raise ×2
    "E010": frozenset({"A"}),       # file_conflict: raise ×5
    "E011": frozenset({"A"}),       # agent_busy: raise ×3
    "E012": frozenset({"A"}),       # lock_timeout: raise ×4
    "E013": frozenset({"A"}),       # not_initialized: raise ×1
    "E014": frozenset({"A"}),       # verify_command_failed: raise via builder ×2
    "E016": frozenset({"A"}),       # self_review_blocked: raise ×1
    "E017": frozenset({"A"}),       # dirty_workspace: raise ×5
    "E018": frozenset({"A"}),       # wrong_branch: raise ×5
    "E019": frozenset({"A"}),       # workspace_busy: raise ×2
    "E022": frozenset({"A", "B"}),  # missing_verify_command: raise ×1 + spec ValidationError
    "E025": frozenset({"A"}),       # source_reference_not_found: raise ×1
    "E027": frozenset({"A"}),       # verify_command_unsafe: raise ×1
    "E033": frozenset({"A"}),       # session_identity_missing: raise ×6
    "E034": frozenset({"A"}),       # retract_not_authorized: raise ×1
    "E036": frozenset({"A"}),       # container_root_cwd: raise ×1
    # ── 通道 B（批量校验）──
    "E004": frozenset({"B"}),       # dag_cycle: spec ValidationError
    "E006": frozenset({"B"}),       # duplicate_id: spec ValidationError
    "E023": frozenset({"B"}),       # vague_acceptance_criteria: spec ValidationError (warning)
    "E024": frozenset({"B"}),       # verify_command_missing_basetemp: spec ValidationError
    "E026": frozenset({"B"}),       # unexempted_test_coupling: spec ValidationError (warning)
    "E028": frozenset({"B", "C"}),  # dry_run_assertion_mismatch: spec ValidationError + cli手工dict
    "E029": frozenset({"B"}),       # granularity_overflow: spec ValidationError (warning)
    "E037": frozenset({"B"}),       # verify_reference_drift: spec ValidationError (blocking)
    "E038": frozenset({"B"}),       # brief_decl_count_mismatch: spec ValidationError (warning)
    # ── 通道 A 补登记（task-shared-entry-verify-gate）──
    "E039": frozenset({"A"}),       # shared_entry_verify_gap: done 前置挂载点 raise ×1
                                    # （onboard/lifecycle/core.py _guard_shared_entry_coverage）
    # ── 通道 C（手工dict）──
    "E021": frozenset({"C"}),       # identity_mismatch: cli手工dict (warning)
    "E030": frozenset({"C"}),       # runtime_file_integrity: gitops/ledger warn dict (warning)
    "E031": frozenset({"C", "B"}),  # roadmap_landing_warning: spec手工dict + validate批量
    "E032": frozenset({"C"}),       # auto_claim_disabled: cli手工dict
    "E035": frozenset({"C"}),       # session_collision_warning: cli手工dict (warning)
    # ── 通道 D（Shell hook）──
    "E020": frozenset({"D"}),       # out_of_scope_commit: git hook echo
    # ── 通道 A 补登记（task-done-reconcile-main）──
    "E015": frozenset({"A"}),       # merge_conflict: done 前置对账 raise OrchdError ×1
                                    # （onboard/lifecycle/core.py 挂载点①）；review.py 仍手工 dict 挂 result reason
}

# 通道有效值（用于断言新增码必须登记有效通道）
_VALID_CHANNELS = frozenset({"A", "B", "C", "D"})

ERROR_GUIDANCE: dict[str, dict[str, Any]] = {
    code: {"recovery": recovery, "read": list(reads), "command": command, "tier": tier, "exit_type": exit_type}
    for code, recovery, reads, command, tier, exit_type in _ERROR_GUIDANCE_TABLE
}

# 未命中映射时的通用兜底指引（保证任何错误码都有恢复指引）。
# 设计 §3 补丁1：本兜底「立即停止并报告」**不算"明确指引"**——归入"无 guidance"
# 分支，允许 agent 分析自愈（否则所有未映射错误码都有 fallback 指引，自愈永不触发）。
# (task-audit-error-guidance-coverage) fallback 的 read 不再硬编码 rules/session.md，
# 而是按错误码所属域推导：verify→rules/verify.md、git→rules/git.md、
# intake→rules/intake.md，其余默认 rules/session.md，使兜底指引贴近错误发生场景。
_FALLBACK_RECOVERY = "遇到错误：立即停止并报告，不自行猜测处置"
_FALLBACK_COMMAND = f"{_ENTRY_CMD} status --text"

_VERIFY_DOMAIN = frozenset({"E014", "E022", "E023", "E024", "E026", "E027", "E028",
                            "E037", "E039"})
_GIT_DOMAIN = frozenset({"E010", "E015", "E017", "E018", "E019", "E020"})
_INTAKE_DOMAIN = frozenset({"E001", "E002", "E003", "E004", "E005", "E006", "E025", "E029"})
_FALLBACK_READ_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "verify": ("rules/verify.md",),
    "git": ("rules/git.md",),
    "intake": ("rules/intake.md",),
    "session": ("rules/session.md",),
}


def _fallback_read(code: str) -> tuple[str, ...]:
    """按错误码所属域返回兜底指引的 read 规则文件（未知码默认 session）。"""
    if code in _VERIFY_DOMAIN:
        return _FALLBACK_READ_BY_DOMAIN["verify"]
    if code in _GIT_DOMAIN:
        return _FALLBACK_READ_BY_DOMAIN["git"]
    if code in _INTAKE_DOMAIN:
        return _FALLBACK_READ_BY_DOMAIN["intake"]
    return _FALLBACK_READ_BY_DOMAIN["session"]


def _fallback_error_guidance(code: str) -> dict[str, Any]:
    """未映射错误码的兜底指引（read 按错误码域推导）。"""
    return {
        "recovery": _FALLBACK_RECOVERY,
        "read": list(_fallback_read(code)),
        "command": _FALLBACK_COMMAND,
        "tier": "manual",
        "exit_type": "manual-action",
    }

# warning 级错误独立指引（设计 §5）：warning 不阻断操作，agent 应继续而非停止，
# 与 _fallback_error_guidance 的"立即停止"语义不匹配。命中 warning 码（且未映射
# 到具体 ERROR_GUIDANCE）时走本指引，而非 fallback。本任务映射覆盖全部枚举码后，
# 此分支主要防御未来新增的 warning 码未补映射的情形。
_WARNING_ERROR_GUIDANCE: dict[str, Any] = {
    "recovery": (
        "警告不阻断：可继续当前流程；若你判定该警告是更深问题的征兆，"
        "可 lesson report 上报（设计 §5.1 三重信号判定）"
    ),
    "read": ["rules/session.md"],
    "command": f"{_ENTRY_CMD} status --text",
    "tier": "manual",
    "exit_type": "continue",
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
        g = _fallback_error_guidance(code)
    out = {"step": "recover", **g}
    if is_suggest_report_code(code):
        out["suggest_report"] = True
    return out


def annotate_validation_items(
    items: list[dict[str, Any]],
    base_dir: str | os.PathLike[str] | None,
) -> list[dict[str, Any]]:
    """批量校验结果逐条附加 error_guidance（通道 B：validate/split 批量输出）。

    纯函数：输入 ``[{code, path, message}, ...]`` 列表，输出每条附加
    ``guidance: {recovery, command, exit_type}``，并将 message 中已定位的
    对象（cycle_nodes / 重复 id / 缺失引用 id）透传进 ``guidance.hint``。

    Args:
        items: 校验错误/告警列表，每条含 ``code``（错误码名，如 ``"E004"``）、
              ``path``（JSON Path）、``message``（人类可读描述）。
        base_dir: 项目 .orchd/ 目录，用于 resolve_read_paths；None 时跳过路径校验。

    Returns:
        新列表（不修改原列表），每条附加 ``guidance`` 键。
    """
    import re as _re

    out: list[dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        code = item.get("code", "")
        g = error_guidance(code)
        # 场景化 hint：从 message 中提取已定位的对象
        hint = _extract_validation_hint(code, item.get("message", ""))
        if hint:
            g["hint"] = hint
            g["recovery"] = hint
        entry["guidance"] = g
        out.append(entry)
    return out


def _extract_validation_hint(code: str, message: str) -> str:
    """从校验错误 message 中提取场景化 hint（纯函数）。

    - E004 (DAG cycle): 提取 cycle 节点列表
    - E006 (duplicate id): 提取重复的 id
    - E005 (reference not found): 提取缺失的引用 id
    """
    import re as _re

    if code == "E004":
        m = _re.search(r"dependency cycle detected involving: (.+)", message)
        if m:
            nodes = m.group(1).strip()
            return f"依赖环涉及任务: {nodes}，请检查 depends_on 消除循环依赖"
    elif code == "E006":
        m = _re.search(r"duplicate (task_id|module_id): '([^']+)'", message)
        if m:
            id_type = m.group(1)
            dup_id = m.group(2)
            return f"重复的 {id_type}: '{dup_id}'，请合并或重命名冲突项"
    elif code == "E005":
        m = _re.search(r"module '([^']+)' not found", message)
        if m:
            mod = m.group(1)
            return f"模块 '{mod}' 未在 modules[] 中定义，请检查 module 字段"
        m = _re.search(r"shared file not found: '([^']+)'", message)
        if m:
            ref = m.group(1)
            return f"共享文件 '{ref}' 不存在，请检查 shared 配置"
        m = _re.search(r"task '([^']+)' not found in master", message)
        if m:
            tid = m.group(1)
            return f"任务 '{tid}' 在 _master.json 中不存在，检查 id 是否拼写正确或是否已注册"
    return ""


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
    # 场景化 hint 优先（2026-08-28 bug1 修复）：错误详情含 hint（如 done 越界 →
    # amend 补声明、锁冲突 → watchdog）时，用它覆盖通用 recovery，使恢复指引贴近
    # 具体原因而非泛化描述。取首个含 hint 的 detail，缺失则保持通用 recovery。
    error_details = (resp.get("error") or {}).get("details") or []
    for detail in error_details:
        if isinstance(detail, dict) and detail.get("hint"):
            guidance["hint"] = detail["hint"]
            guidance["recovery"] = detail["hint"]
            break
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
