"""session lock 编排入口（ensure_session_lock）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import os

from orchd.errors import ErrorCode
from orchd.errors import OrchdError
from .acquire import session_lock_acquire
from .check import session_lock_check
from .gate import _acquire_session_gate
from .paths import _get_session_gate_path
from .state import _gate_unavailable_state, _lock_state


def ensure_session_lock(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """确保当前 session 可写入：门锁串行化"检查+获取"，被其他 session 持有则 E019。

    best-effort：门锁 / 锁获取失败（IO 错误）不抛异常；但 **R2-7 起降级不再静默**——
    失败时写结构化 stderr 留痕（``orchd ▸ [session-lock]``）并返回「未持锁」真实态，
    调用方不得据返回值假定已持锁（此前两处 ``session_lock_acquire`` 的返回值被忽略，
    写锁失败时并发保护静默降级）。

    与旧"check-then-act"区别：旧实现先 :func:`session_lock_check` 再
    :func:`session_lock_acquire`（覆盖写），两个并发 session 都可通过检查并同时
    "获取"（后写覆盖先写）。本实现先用一个**门锁**（flock gate，ExclusiveFileLock）
    串行化整个"检查 + 写入"——一次仅一个进程能通过检查并写锁标记，其余读到该标记后
    据 session 归属判 E019 或幂等复用（刷新覆盖写）。

    门锁不可得的 fail-closed（task-session-gate-timeout-fail-closed）：门锁获取走
    :func:`_acquire_session_gate` 有界重试，重试耗尽即**拒绝本次锁获取**——不再执行
    非串行的「检查 + 写入」：旧 best-effort 续跑在并发下可让两个 session 同时通过
    检查并各自写标记（后写覆盖先写），正是门锁本欲消除的 E019 双取竞态；拒绝是唯一
    安全出口（瞬时争用由重试预算吸收，正常路径零回归）。

    Session Identity Layer：同 ``agent_id`` 但不同 ``session_id`` 视为另一个
    session（即使指纹相同），防止同 agent 多会话互踩/误释放锁。

    **无 git 等价（task-nogit-guard-parity）**：本锁的载体是文件系统（账本根下的锁标记
    文件 + flock gate），与 git 无关。无 git 单目录模式下 :func:`_git_worktree_name`
    返回 ``None``，锁路径回退 ``.session.lock``（worktree 维度唯一 ⇒ 全局唯一），互斥
    语义与 git 模式**完全一致**：他 session 持锁时本会话写命令抛 E019 ``workspace_busy``，
    锁的释放仍由调用方（claim/done/review 的 ``finally`` →
    :func:`release_session_lock_if_owned`）负责。此前调用方
    （``orchd/gitops/guard.py``）把 L2 与 L1 挂在同一个 ``git_available`` 条件下，无 git
    环境下并发保护**静默缺失**；现改为「git 可用 **或** 无 git orchd 项目」均启用。

    Returns:
        本会话的持锁真实态（以 :func:`session_lock_acquire` 结果为真源，永不抛异常
        之外的路径）：
        - ``{"acquired": True, "reused": <bool>, "path": <str>, "gate_acquired": <bool>}``
          已持锁（``reused=True`` 表示同 session 刷新覆盖写）；
        - ``{"acquired": False, "reason": "flock_contended" | "lock_dir_unwritable" |
          "lock_write_failed", "error": <str>, "degraded": True, "gate_acquired": <bool>,
          "hint": <str>}`` 锁标记写入失败——**本会话未持锁**，并发保护已降级（调用方
          须自行决定是否继续；``orchd/gitops/guard.py`` 当前为 best-effort 继续并把
          降级并入响应 ``degraded_guards``）。``reason`` 为分类后的失败原因
          （task-session-lock-degrade-observability AC1），此前统一为 ``io_error``。
        - ``{"acquired": False, "reason": "gate_timeout", "error": <str>,
          "degraded": True, "gate_acquired": False, "attempts": <int>,
          "retriable": True, "exit_type": "await-external", "hint": <str>}``
          门锁有界重试耗尽（E012 超时 / 门锁 IO 故障）——**fail-closed 拒绝**：本次
          「检查 + 写入」整体未执行、锁标记未写入、本会话未持锁
          （task-session-gate-timeout-fail-closed）；调用方按 ``exit_type`` 等待
          持锁者释放后重试同一命令。
        - ``gate_acquired`` 反映「检查 + 写入」是否真被门锁串行化（fail-closed 收口后
          正常路径恒为 ``True``；``False`` 仅在「拒绝收口被移除」的回退形态出现，
          届时另出一条 ``gate_unavailable`` stderr 留痕）。
    """
    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    # P0-1：写入锁前校验身份字段——空 agent_id 会导致"幽灵锁"（holder="unknown"），
    # 后续 E019 报错信息误导且难以定位。session_id 为空时自动生成进程级 fallback
    # （保证锁元数据非空，测试环境不设 ORCHD_SESSION_ID 时不阻断）。
    if not agent_id:
        raise OrchdError(
            ErrorCode.E019,
            "lock_identity_empty: agent_id 为空，无法获取 session 锁",
            [{"agent_id": agent_id, "session_id": session_id or "",
              "hint": "请确认 ORCHD_AGENT_ID 环境变量或宿主注入已正确配置"}],
        )
    if not session_id:
        session_id = f"auto-{os.getpid()}-{id(orchd_dir):x}"
    from orchd.lockfile import ExclusiveFileLock

    # 门锁：串行化后续"检查 + 写入"，消除 check-then-act 竞态。
    gate = ExclusiveFileLock(_get_session_gate_path(orchd_dir))
    gate_result = _acquire_session_gate(gate)
    acquired_gate = bool(gate_result.get("acquired"))
    if not acquired_gate:
        # 门锁不可得 → fail-closed（task-session-gate-timeout-fail-closed）：有界重试
        # 耗尽即**拒绝本次锁获取**，绝不退回非串行「检查 + 写入」——旧 best-effort
        # 续跑在并发下会让多个 session 同时通过检查并各自写标记（E019 双取竞态）。
        blocked = _gate_unavailable_state(gate_result)
        if blocked is not None:
            return blocked
    try:
        check = session_lock_check(orchd_dir)
        if check.get("locked"):
            holder = check.get("agent_id", "unknown")
            holder_session = check.get("session_id") or ""
            if holder == agent_id and (not holder_session or holder_session == session_id):
                # 本 session 已持锁：刷新覆盖写（幂等复用）；返回值不再被忽略（R2-7）
                lock_result = session_lock_acquire(
                    orchd_dir, agent_id, branch, session_id=session_id
                )
                return _lock_state(lock_result, acquired_gate)
            raise OrchdError(
                ErrorCode.E019,
                f"workspace_busy: 工作区被 '{holder}' 占用（分支 {check.get('branch', 'N/A')}，"
                f"已锁定 {check.get('age_min', 0):.1f} 分钟）",
                [{
                    "agent_id": agent_id,
                    "holder": holder,
                    "holder_session": holder_session,
                    "holder_branch": check.get("branch"),
                    "holder_timestamp": check.get("timestamp"),
                    "age_min": check.get("age_min"),
                    "hint": "等待该 session 结束，或使用 watchdog --timeout 0 强制释放僵死锁",
                }],
            )
        # 未被持有 / 损坏 / 超时（可覆盖）：直接写锁标记；返回值不再被忽略（R2-7）
        lock_result = session_lock_acquire(
            orchd_dir, agent_id, branch, session_id=session_id
        )
        return _lock_state(lock_result, acquired_gate)
    finally:
        if acquired_gate:
            gate.release()
