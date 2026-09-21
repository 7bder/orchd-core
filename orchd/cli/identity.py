"""Orchd CLI - identity 域。

迁移自 orchd/cli.py（task-split-cli-identity）：
  - _auto_inject_session_id: 宿主会话身份自动注入（ORCHD_SESSION_ID 兜底）
  - _resolve_agent_id: 解析当前会话身份（由 ORCHD_SESSION_ID 派生指纹）
  - _require_agent_id: 解析身份，为空则 E033 拒绝（写命令用）
  - _detect_claim_role: 按任务当前状态自动判定认领角色
  - _identity_warning: 比对 git config user.name 与 agent_id，不一致返回 E021 warning
  - _is_fingerprint_agent_id: 判断 agent_id 是否为指纹形态身份（12 位 hex）
  - _current_task_from_branch: best-effort 从当前 git 分支名推导归属任务
  - _session_collision_warning: 只读检测当前会话指纹的「并行会话碰撞」
  - _session_collision_warn_dict: 构造 session_collision_warning 告警 dict（E035）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import structured_error

# 宿主会话身份环境变量（TRAE 会话码兜底）
_TRAE_SESSION_ID_ENV = "ICUBE_CODEMAIN_SESSION"


def _auto_inject_session_id() -> None:
    """宿主会话身份自动注入：若 ORCHD_SESSION_ID 未设置，则用 TRAE 会话码兜底。"""
    if os.environ.get("ORCHD_SESSION_ID"):
        return
    trae_sid = os.environ.get(_TRAE_SESSION_ID_ENV)
    if trae_sid:
        os.environ["ORCHD_SESSION_ID"] = trae_sid


def _resolve_agent_id(orchd_dir: Path | None = None) -> str:
    """解析当前会话身份：由宿主注入的 ``ORCHD_SESSION_ID`` 派生（session-id-fingerprint）。

    引擎统一从 ``orchd.ledger.resolve_agent_id`` 取 agent 身份：
    - 有 ``ORCHD_SESSION_ID`` → 确定性派生 12 位 hex 指纹（同一对话内稳定，
      切换对话换指纹）；
    - 无该变量 → 返回空字符串（引擎不生成、不借用、不落盘任何身份）。
    宿主（TRAE / codex / opencode / workbuddy）在启动 orchd 前统一把各自
    会话唯一码注入 ``ORCHD_SESSION_ID``。写命令在身份为空时由调用方拒绝。
    """
    from orchd.ledger import resolve_agent_id

    return resolve_agent_id(orchd_dir)


def _require_agent_id(orchd_dir: Path | None = None) -> str:
    """解析当前会话身份；为空（宿主未注入 ORCHD_SESSION_ID）则 E033 拒绝。

    供写命令（claim / done / review / retract / force-status）调用：这些命令
    需要把身份写进事件，身份缺失时不可静默降级，须明确报错提示宿主注入会话 ID。
    """
    agent_id = _resolve_agent_id(orchd_dir)
    if not agent_id:
        raise OrchdError(
            ErrorCode.E033,
            "session_identity_missing: 宿主未注入 ORCHD_SESSION_ID，无法识别当前会话身份",
            [{
                "agent_id": agent_id,
                "hint": (
                    "本命令需要会话身份。请宿主在启动 orchd 前把当前会话唯一码注入 "
                    "ORCHD_SESSION_ID（TRAE 会话自动注入；codex/opencode/workbuddy "
                    "由各自接入层注入），再重试"
                ),
            }],
        )
    return agent_id


def _detect_claim_role(store, tasks: list[dict[str, Any]], task_id: str) -> str:
    """按任务当前状态自动判定认领角色（task-fp-identity-engine）。

    - in_review → reviewer（审查认领，REVIEW_CLAIMED）
    - 其他（pending / claimed 等）→ implementer（实现认领，CLAIMED）
    引擎据此在 claim 时省略 --role，实现按状态自动分流。
    """
    state = store.replay()
    ts = state.get(task_id)
    return "reviewer" if (ts and ts.status == "in_review") else "implementer"


def _identity_warning(agent_id: str, orchd_dir: Path) -> dict[str, Any] | None:
    """比对 git config user.name 与 agent_id，不一致返回 E021 warning（不阻断）。

    git 不可用 / user.name 未配置 / 与 agent_id 一致 → 返回 None（无 warning）。
    指纹形态身份 agent_id（12 位 hex）豁免
    E021——指纹为自动化 agent 身份锚定，不与人名 git user.name 硬比对。
    用于写命令（claim/done/review）前的身份审计（ROADMAP 1.1 L5）：
    仅提示，不阻断状态机。
    """
    import subprocess

    # 指纹形态身份 agent_id 豁免 E021（12 位 hex）
    if _is_fingerprint_agent_id(agent_id):
        return None

    try:
        proc = subprocess.run(
            ["git", "config", "user.name"], cwd=str(orchd_dir.parent),
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    git_name = proc.stdout.strip()
    if not git_name or git_name == agent_id:
        return None
    # Channel C: via structured_error (to_json_response + attach_error_guidance), details恒为list, 恒带guidance
    _msg = "identity_mismatch: git config user.name 与 agent_id 不一致"
    _details = [{"git_user_name": git_name, "agent_id": agent_id, "warning": "identity_mismatch", "hint": "git config user.name 与 agent_id 不一致，请核对身份（SKILL.md 命名规范：{provider}-{序号}）"}]
    _resp = structured_error("E021", _msg, _details, orchd_dir)
    _err = _resp.get("error", {})
    _guidance = _resp.get("guidance")
    return {
        "code": _err.get("code", "E021"),
        "warning": "identity_mismatch",
        "git_user_name": git_name,
        "agent_id": agent_id,
        "hint": "git config user.name 与 agent_id 不一致，请核对身份（SKILL.md 命名规范：{provider}-{序号}）",
        "details": _err.get("details", _details),
        "guidance": _guidance,
        "severity": _err.get("severity", "warning"),
    }


def _is_fingerprint_agent_id(agent_id: str) -> bool:
    """判断 agent_id 是否为指纹形态身份（12 位 hex）。

    task-fp-identity-single-source（2026-08-22）：单一事实源为
    ``orchd.ledger.is_fingerprint_agent_id``，此处惰性导入转发（保持调用点
    不变，避免模块级循环依赖），消除本地副本的同步漂移风险。
    """
    from orchd.ledger import is_fingerprint_agent_id

    return is_fingerprint_agent_id(agent_id)


def _current_task_from_branch(project_root: Path) -> str | None:
    """best-effort 从当前 git 分支名推导本流程归属任务（``task/<id>`` 前缀）。

    容器布局（1.4）下 agent 在专属 task worktree 中工作，分支即 ``task/<id>``，
    据此可将「本流程发起的任务」从并行活跃任务中排除，避免单任务流程误报。
    无 git / 非 task 分支 / 调用失败 → 返回 None（调用方退化为更保守的阈值）。
    """
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if branch.startswith("task/"):
        return branch[len("task/"):]
    return None


def _session_collision_warning(
    agent_id: str,
    store,
    exclude_task_id: str | None = None,
    review_task_id: str | None = None,
) -> dict[str, Any] | None:
    """只读检测当前会话指纹的「并行会话碰撞」，返回 ``session_collision_warning`` 或 None。

    两种成因按 reason 区分归因（task-e035-hint-split）：自审（reviewer session ==
    实现者 session）首要成因是同一会话既实现又审查，按自审处置（comments 披露
    或换会话）；并行活跃任务首要成因才是 host 项目级会话码注入（全项目同一
    ``ORCHD_SESSION_ID`` 使并行对话派生同一指纹）。本函数仅只读 replay
    ledger，**不修改身份机制（resolve_agent_id 不变）、不落状态（不写 ledger/checkpoint）、
    不阻断命令**，只在命中时附加只读告警，提示 host 注入粒度违约。

    触发条件（任一即告警）：

    1. 并行活跃任务：当前指纹名下已存在非本流程发起的并行活跃任务
       （``claimed``/``in_review``，owner 指纹 == 当前指纹，且 ``task_id != exclude_task_id``）；
    2. 自审实现：``review_task_id`` 的实现者指纹 == 当前指纹（审查自己实现）。

    非指纹身份（如 ``provider-{n}``）豁免——其粒度本就为 agent 级，碰撞语义不同，
    不在此告警范围。

    Args:
        agent_id: 当前会话指纹（由 ``resolve_agent_id`` 派生）。
        store: ledger Store（用于 replay 当前任务状态）。
        exclude_task_id: 本流程正在操作/归属的任务（claim 传被认领任务，
          status 传当前分支或显式 task_id）；命中后从并行集合排除，避免自指。
        review_task_id: 若本流程为审查认领，传被审查任务，用于自审检测。

    Returns:
        告警 dict（含 ``code``/``warning``/``reason``/``colliding_tasks``/``hint``），
        或 None（无碰撞、零误报）。
    """
    if not agent_id or not _is_fingerprint_agent_id(agent_id):
        return None

    from orchd.ledger import resolve_session_identity

    current_session_id = resolve_session_identity(getattr(store, "orchd_dir", None))["session_id"]

    def _same_owner(claimed_by: str | None, claimed_session: str | None) -> bool:
        if claimed_session and current_session_id:
            return claimed_session == current_session_id and claimed_by == agent_id
        return bool(claimed_by and claimed_by == agent_id)

    states = store.replay()

    # 条件 2：审查自己实现（reviewer session == 实现者 session）
    if review_task_id:
        ts = states.get(review_task_id)
        if ts is not None and _same_owner(ts.claimed_by, ts.claimed_session):
            return _session_collision_warn_dict(
                reason="self_implementation_review",
                colliding_tasks=[review_task_id],
                hint=(
                    "当前会话与任务实现者 session 相同，疑似审查自己实现：请先按自审"
                    "处置——在 review comments 首句披露自审（写明实现者 = 审查者），"
                    "或换一个独立会话（新对话 / 重新 session start 注入唯一会话码）"
                    "担任 reviewer；仅当各对话确已使用独立会话码但仍派生同指纹时，"
                    "再按 host 会话码项目级注入排查。"
                ),
            )

    # 条件 1：当前 session 名下存在其他并行活跃任务
    colliding: list[str] = []
    for tid, ts in states.items():
        if tid == exclude_task_id:
            continue
        if ts.status in ("claimed", "in_review"):
            if _same_owner(ts.claimed_by, ts.claimed_session) or _same_owner(
                ts.review_claimed_by, ts.review_claimed_session
            ):
                colliding.append(tid)

    if colliding:
        # 本流程有明确归属任务 → 其余活跃任务必为并行（误报概率低，直接告警）；
        # 无明确归属任务 → 仅当指纹名下已并行多任务（>=2）才告警，避免单任务正常流误报。
        if exclude_task_id is not None or len(colliding) >= 2:
            return _session_collision_warn_dict(
                reason="parallel_active_tasks",
                colliding_tasks=colliding,
                hint=(
                    "当前会话指纹名下已存在其他并行活跃任务（claimed/in_review），"
                    "但本流程并未发起它们：host 注入的会话码为项目级（全项目同指纹），"
                    "导致并行对话被识别为同一身份、归属错乱。请为每次对话注入唯一会话码，"
                    "或核对任务归属。"
                ),
            )
    return None


def _session_collision_warn_dict(
    reason: str, colliding_tasks: list[str], hint: str,
) -> dict[str, Any]:
    """构造 ``session_collision_warning`` 告警 dict（E035 告警码，不阻断命令）。Channel C via structured_error."""
    _msg = "session_collision_warning: 同一工作区多会话碰撞"
    _details = [{"reason": reason, "colliding_tasks": colliding_tasks, "warning": "session_collision_warning", "hint": hint}]
    try:
        from orchd.cli import _find_orchd_dir
        _base = _find_orchd_dir()
    except Exception:
        _base = None
    _resp = structured_error("E035", _msg, _details, _base)
    _err = _resp.get("error", {})
    _guidance = _resp.get("guidance")
    return {
        "code": _err.get("code", "E035"),
        "warning": "session_collision_warning",
        "reason": reason,
        "colliding_tasks": colliding_tasks,
        "hint": hint,
        "details": _err.get("details", _details),
        "guidance": _guidance,
        "severity": _err.get("severity", "warning"),
    }
