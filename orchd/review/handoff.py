"""done 后的审查交接（review_handoff）：mint 审查会话 + 渲染 spawn prompt。

背景（task-review-handoff-engine，2026-11-04）：此前「审查者自己取号」是宿主可见
契约的一部分——实现方 LLM 必须记住并拼一条取号命令，忘了就撞 lineage 硬门 E016。
本模块把该动作收进引擎：done 成功后由引擎 mint 一个 ``kind=reviewer``、
``parent=该任务最后一次 DONE 的 session_id`` 的会话，并渲染一段可直接交给
subagent 的 spawn prompt。

依赖方向：``handoff`` 只依赖 ``orchd.contracts`` / ``orchd.ledger``（会话与 DONE
事件）与本包叶子域 ``history``；``orchd.guide``（命令前缀 / 模板路由）与
``orchd.worktree``（任务 worktree 解析）按需延迟导入，避免与聚合入口成环。
消费方：``orchd.onboard.claim.review_face``（files_to_review 同源）、
``orchd.cli.commands.workflow``（done 挂载点）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orchd.contracts.task_schema import FILES_TO_EDIT_FIELD
from orchd.ledger import Store

from .history import find_last_done_event

# 审查阶段取值域（与 claim/review 的 ``--type`` 一致；None = unified 单阶段）。
PHASE_SPEC = "spec"
PHASE_CODE = "code"
REVIEW_PHASES = (PHASE_SPEC, PHASE_CODE)

# 交接失败的结构化错误码（响应字段，不是异常；done 语义优先，调用方 fail-closed）。
HANDOFF_ERROR_CODE = "review_handoff_failed"


def files_to_review(
    task_def: dict[str, Any],
    shared: dict[str, Any] | None = None,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """审查清单单一真源（claim 与 handoff 共用，禁止各写一份 phase 映射）。

    映射口径（与 ``review_face`` 历史行为逐字一致）：

    - ``files_to_edit`` 全部 ``must_read``；
    - ``spec`` 阶段附 ``shared/architecture.md``（reference）；
    - ``code`` 阶段附 ``shared/conventions.md``（must_read）；
    - unified（``phase`` 为 None）两者都附。

    Args:
        task_def: 任务定义（取 ``files_to_edit``）。
        shared: 宿主 shared 上下文（``architecture`` / ``conventions`` 键）。
        phase: 审查阶段 ``spec`` / ``code`` / None（unified）。

    Returns:
        ``[{path, priority, hint?}]`` 列表；无 shared 时只含 must_read 项。
    """
    files: list[dict[str, Any]] = [
        {"path": path, "priority": "must_read"}
        for path in task_def.get(FILES_TO_EDIT_FIELD, [])
    ]
    if not shared:
        return files
    if phase == PHASE_SPEC:
        arch = shared.get("architecture")
        if arch:
            files.append({"path": arch, "priority": "reference", "hint": "arch"})
        return files
    if phase == PHASE_CODE:
        conv = shared.get("conventions")
        if conv:
            files.append({"path": conv, "priority": "must_read", "hint": "conv"})
        return files
    arch = shared.get("architecture")
    if arch:
        files.append({"path": arch, "priority": "reference", "hint": "arch"})
    conv = shared.get("conventions")
    if conv:
        files.append({"path": conv, "priority": "must_read", "hint": "conv"})
    return files


def review_phase_of(review_created: dict[str, Any] | None) -> str | None:
    """由 done 的 ``review_created`` 推导审查阶段（None = unified 单阶段）。

    ``mode=unified`` 时**省略** ``--type``；``two_phase`` 与
    ``two_phase_single_stage_doc`` 取 ``type``（后者为 ``code``）。
    """
    if not review_created:
        return None
    if review_created.get("mode") == "unified":
        return None
    review_type = review_created.get("type")
    return review_type if review_type in REVIEW_PHASES else None


def template_for_phase(phase: str | None) -> str:
    """审查模板路径（单一真源：``orchd.guide.template_for`` 的模板路由）。

    ``spec`` / ``code`` 取对应阶段的模板；unified 取单阶段模板。路由表为空或路由
    抛异常时返回空串——调用方（``build_review_handoff``）据此降级为
    ``handoff_render_failed``，不再静默退回「见 rules/review.md」。
    """
    from orchd.guide import template_for

    mode = "unified" if phase is None else "two_phase"
    try:
        candidates = template_for("reviewer", mode)
    except Exception:  # 路由缓存不可用不阻断交接
        return ""
    if phase is not None:
        for rel in candidates:
            if f"{phase}-reviewer" in rel:
                return rel
    return candidates[0] if candidates else ""


def _type_arg(phase: str | None) -> str:
    """claim/review 的 ``--type`` 片段（unified 省略，与 guide 口径一致）。"""
    return f" --type {phase}" if phase in REVIEW_PHASES else ""


def _phase_label(phase: str | None) -> str:
    if phase == PHASE_SPEC:
        return "spec（规格审查）"
    if phase == PHASE_CODE:
        return "code（代码审查）"
    return "unified（单阶段审查）"


def _bullets(files: list[dict[str, Any]], priority: str) -> list[str]:
    """按 priority 分组的清单项（must_read 之外统一归 reference 段）。"""
    if priority == "must_read":
        picked = [item for item in files if item.get("priority") == "must_read"]
    else:
        picked = [item for item in files if item.get("priority") != "must_read"]
    return [f"- {item.get('path')}" for item in picked if item.get("path")]


def _same_path(left: str, right: str) -> bool:
    """路径相等判定（不触盘；Windows 大小写与分隔符归一）。"""
    if not left or not right:
        return False
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def render_handoff_prompt(
    *,
    task_id: str,
    phase: str | None,
    worktree: str,
    session_token: str,
    reviewer_session_id: str,
    parent_session_id: str,
    implementer_fingerprint: str,
    reviewer_fingerprint: str,
    files: list[dict[str, Any]],
    template: str,
    entry_cmd: str,
    project_root: str = "",
) -> str:
    """渲染可直接交给 subagent 的 spawn prompt（纯函数，无 IO / 无副作用）。

    prompt 覆盖：任务 / 阶段 / worktree（+ canonical 项目根）、PowerShell 与 bash
    两种 token 注入、claim 与 review 命令、审查模板、must_read / reference 清单、
    comments 首句（实现者与审查者 **12 位指纹**）、只读与不 push / 不切分支 /
    不 merge / 不手改账本约束、JSON 输出契约。**不含任何取号命令**——身份由引擎
    mint 并在此交接，subagent 不应再申请第二个会话身份。

    优化约定：不复制 acceptance_criteria，只声明 AC 来自 claim 响应并要求逐条
    三态取证；意见写到仓库外临时文件；token 只进环境变量；空清单段省略；
    project_root 与 worktree 相同时省略该行。

    Args:
        task_id: 任务 id。
        phase: ``spec`` / ``code`` / None（unified）。
        worktree: 审查执行目录（任务 worktree，flat 布局为主工作树）。
        session_token: 引擎 mint 的审查会话 token。
        reviewer_session_id: 审查会话 session_id。
        parent_session_id: 该任务最后一次 DONE 的 session_id（lineage 父）。
        implementer_fingerprint: 实现者 12 位指纹（DONE 事件 ``agent_id``）。
        reviewer_fingerprint: 审查者 12 位指纹（mint 返回的 ``fingerprint``）。
        files: ``files_to_review`` 输出。
        template: 审查模板相对路径；空串表示路由不可用（调用方应先降级）。
        entry_cmd: 引擎入口命令前缀（``python .orchd/__main__.py``）。
        project_root: canonical 项目根（仅展示，缺省省略该行）。

    Returns:
        多行 prompt 文本（``\\n`` 分隔）。
    """
    claim_cmd = f"{entry_cmd} claim --task {task_id}{_type_arg(phase)} --confirm"
    review_cmd = (
        f"{entry_cmd} review --task {task_id}{_type_arg(phase)} "
        "--verdict APPROVED|CHANGES_REQUESTED --comments-file <第 4 步意见文件>"
    )
    template_line = template or "见 .orchd/rules/review.md（模板路由不可用）"
    optional_sections: list[str] = []
    must_read_lines = _bullets(files, "must_read")
    if must_read_lines:
        optional_sections.extend(["", "## must_read", *must_read_lines])
    reference_lines = _bullets(files, "reference")
    if reference_lines:
        optional_sections.extend(["", "## reference", *reference_lines])
    lines = [
        "你是一次独立审查的 subagent。以下内容由引擎在 done 成功时生成，按原文执行。",
        "",
        "## 交接信息",
        f"- 任务：{task_id}",
        f"- 阶段：{_phase_label(phase)}",
        f"- 工作树：{worktree}（所有命令都在此目录执行）",
        *(
            [f"- 项目根：{project_root}"]
            if project_root and not _same_path(project_root, worktree)
            else []
        ),
        f"- 审查会话：{reviewer_session_id}",
        f"- 父会话（执行 done 的实现会话）：{parent_session_id}",
        "",
        "## 身份（已由引擎交接，勿再取号）",
        "kind=reviewer，父会话即上列实现会话；token 仅用于设置 ORCHD_SESSION_ID，"
        "不要写入意见文件、JSON、命令行或日志。",
        f'PowerShell：$env:ORCHD_SESSION_ID="{session_token}" ｜ '
        f'bash：export ORCHD_SESSION_ID="{session_token}"',
        "",
        "## 步骤",
        f'1. cd "{worktree}"',
        f"2. {claim_cmd}",
        "   - claim 响应的 acceptance_criteria 是唯一验收标准："
        "逐条给出“满足/不满足/需人工 + 证据”，不要复制 AC 原文。",
        "   - claim 响应自带 verify 摘要与 rerun_command；默认不重跑，"
        "仅在 head 漂移 / degraded / 需要复现时重跑。",
        f"3. 读模板 {template_line}，并按下面 must_read / reference 取证。",
        "4. 把意见写到仓库外临时文件（UTF-8）："
        f"PowerShell `$env:TEMP\\orchd-review-{task_id}.md`；"
        f"bash `${{TMPDIR:-/tmp}}/orchd-review-{task_id}.md`。"
        "这是唯一允许写的文件。",
        f"5. {review_cmd}",
        "6. 最终回复只输出 JSON；不要把 JSON 写进意见文件。",
        "",
        "## 约束（硬性）",
        "- 只读审查：不改仓库文件、不 git commit、不 git push、不切换分支、不 merge",
        "- 不手改账本 / _master / checkpoint；不跑全量回归（只评审本任务范围）",
        "- comments 首句固定为：",
        f"  本审查由实现会话 {implementer_fingerprint} spawn 的独立 subagent 执行，"
        f"审查指纹 {reviewer_fingerprint}",
        "- claim 若因 E016/E033 失败：不要自行 session start、不换 token、"
        "不改用其它身份；停止并把错误原文回报给实现会话",
        *optional_sections,
        "",
        "## 输出契约（JSON）",
        '{"verdict": "APPROVED|CHANGES_REQUESTED",'
        ' "comments": "<首句 + 逐条证据>", "task_id": "' + task_id + '"}',
    ]
    return "\n".join(lines)


def _handoff_error(reason: str, message: str, hint: str) -> dict[str, Any]:
    """结构化交接错误（响应字段；调用方 fail-closed，done 语义不受影响）。"""
    return {
        "error": {
            "code": HANDOFF_ERROR_CODE,
            "reason": reason,
            "message": message,
            "hint": hint,
        }
    }


def build_review_handoff(
    store: Store,
    orchd_dir: Path,
    task_def: dict[str, Any],
    task_id: str,
    project_root: Path | None = None,
    review_created: dict[str, Any] | None = None,
    shared: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """done 成功后生成审查交接：mint reviewer 会话 + 渲染 prompt。

    失败**不抛异常**：DONE 已落账，交接属增量能力，任何环节失败都降级为
    ``{"error": {...}}``，由调用方（CLI）原样挂到 done 响应上并 fail-closed。

    Args:
        store: 账本 Store（读最后一次 DONE 事件取 lineage 父会话）。
        orchd_dir: ``<项目>/.orchd``（会话 runtime 落盘根）。
        task_def: 任务定义（取 ``files_to_edit`` 组清单）。
        task_id: 任务 id。
        project_root: 项目根（解析任务 worktree；None 时退回 ``orchd_dir.parent``）。
        review_created: done 的 ``review_created``（推导阶段与 ``--type``）。
        shared: 宿主 shared 上下文（architecture / conventions）。

    Returns:
        成功：``{prompt, session_token, reviewer_session_id, parent_session_id,
        implementer_fingerprint, reviewer_fingerprint, worktree, hint, phase,
        template, files_to_review}``；
        失败：``{error: {code, reason, message, hint}}``，``reason`` ∈
        ``done_session_id_missing`` / ``reviewer_session_start_failed`` /
        ``handoff_render_failed``（与 design §4 枚举一致）。
    """
    phase = review_phase_of(review_created)
    done_event = find_last_done_event(store, task_id)
    if not done_event:
        return _handoff_error(
            "done_session_id_missing",
            f"任务 {task_id} 无 DONE 事件，无法推导审查 lineage 父会话",
            "确认 done 已落账后重试；或人工取号并按 DONE session_id 声明 parent",
        )
    parent_session_id = str(done_event.get("session_id") or "").strip()
    if not parent_session_id:
        return _handoff_error(
            "done_session_id_missing",
            f"任务 {task_id} 的 DONE 事件缺 session_id（旧账本）",
            "lineage 硬门对缺 session_id 的 DONE fail-closed；请人工取号后"
            "停在本响应，不要伪造 parent",
        )
    # 实现者指纹 = DONE 的 agent_id（= claimed_by，12 位 hex）；缺失则首句无从固定，
    # 按渲染失败降级（禁止退回 64 位 session_id 或占位符）。
    implementer_fingerprint = str(done_event.get("agent_id") or "").strip()
    root = Path(project_root) if project_root is not None else Path(orchd_dir).parent

    # strict=True：container 布局下解析不到任务 worktree 即降级为结构化 error，
    # 不回退主工作树（否则 prompt 会把 subagent 指到错误目录，且在错误目录 claim）。
    # 延迟 import 一并纳入 try：DONE 已落账，任何准备步骤失败都只能是
    # handoff_render_failed，不得冒到 CLI 兜底变成未登记的第 4 种 reason。
    try:
        from orchd.gitops_ops.reconcile import resolve_task_worktree

        resolved = resolve_task_worktree(root, task_id, strict=True)
    except Exception as exc:
        return _handoff_error(
            "handoff_render_failed",
            f"任务 worktree 解析失败：{type(exc).__name__}: {exc}",
            "done 已成功；请人工确认任务 worktree 存在后再取号领审",
        )
    if resolved is None:
        return _handoff_error(
            "handoff_render_failed",
            "任务 worktree 解析为空（strict 解析未返回路径）",
            "done 已成功；请人工确认任务 worktree 存在后再取号领审",
        )
    worktree = str(resolved)
    try:
        from orchd.guide import _ENTRY_CMD
        from orchd.ledger.session import session_start

        try:
            minted = session_start(
                orchd_dir,
                agent_name=f"reviewer-{task_id}",
                kind="reviewer",
                parent_session_id=parent_session_id,
            )
        except Exception as exc:  # mint 失败（runtime 不可写 / 会话层拒绝）
            return _handoff_error(
                "reviewer_session_start_failed",
                f"审查会话 mint 失败：{type(exc).__name__}: {exc}",
                "done 已成功；请人工取号（kind=reviewer、parent 取上列 DONE 会话）后再领审",
            )
        reviewer_fingerprint = str(minted.get("fingerprint") or "").strip()
        if not implementer_fingerprint or not reviewer_fingerprint:
            return _handoff_error(
                "handoff_render_failed",
                "comments 首句所需指纹缺失"
                f"（implementer={implementer_fingerprint!r}, reviewer={reviewer_fingerprint!r}）",
                "首句必须含实现者与审查者 12 位指纹；请人工核对 DONE 事件与会话 runtime",
            )
        template = template_for_phase(phase)
        if not template:
            return _handoff_error(
                "handoff_render_failed",
                "审查模板路由不可达（orchd.guide.template_for 未返回候选）",
                "done 已成功；请检查 templates/ 与模板路由后人工取号领审",
            )
        files = files_to_review(task_def, shared, phase)
        prompt = render_handoff_prompt(
            task_id=task_id,
            phase=phase,
            worktree=worktree,
            session_token=minted["session_token"],
            reviewer_session_id=minted["session_id"],
            parent_session_id=parent_session_id,
            implementer_fingerprint=implementer_fingerprint,
            reviewer_fingerprint=reviewer_fingerprint,
            files=files,
            template=template,
            entry_cmd=_ENTRY_CMD,
            project_root=str(root),
        )
    except Exception as exc:  # 准备/渲染阶段任何异常都归 handoff_render_failed
        return _handoff_error(
            "handoff_render_failed",
            f"交接准备或渲染失败：{type(exc).__name__}: {exc}",
            "done 已成功；请人工取号领审并反馈该缺陷",
        )
    return {
        "prompt": prompt,
        "session_token": minted["session_token"],
        "reviewer_session_id": minted["session_id"],
        "parent_session_id": parent_session_id,
        "implementer_fingerprint": implementer_fingerprint,
        "reviewer_fingerprint": reviewer_fingerprint,
        "worktree": worktree,
        "hint": (
            "把 prompt 原样交给当前会话 spawn 的独立 subagent（token 已含在 prompt 内）；"
            "实现会话不得自己领审，无 subagent 能力请 retract 后停止并上报"
        ),
        "phase": phase,
        "template": template,
        "files_to_review": files,
    }


__all__ = (
    "HANDOFF_ERROR_CODE",
    "PHASE_CODE",
    "PHASE_SPEC",
    "REVIEW_PHASES",
    "build_review_handoff",
    "files_to_review",
    "render_handoff_prompt",
    "review_phase_of",
    "template_for_phase",
)
