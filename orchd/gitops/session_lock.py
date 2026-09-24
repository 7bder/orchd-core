"""gitops session_lock 域：会话锁（13 函数 + 5 常量，_SESSION_LOCK_REGISTRY 单例）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops._const import _GIT_TIMEOUT
from orchd.gitops.cleanup import _safe_delete


_SESSION_LOCK_FILENAME = ".session.lock"


_SESSION_GATE_FILENAME = ".session.gate.lock"


# 门锁不可得时的「有界重试」预算（task-session-gate-timeout-fail-closed）：
# 单次 acquire 仍沿用既有 10s 超时语义（不改门锁自身预算），仅在其上叠加退避重试，
# 用于吸收瞬时争用（正常争用的持锁窗口是毫秒级「检查 + 写入」）；重试耗尽即
# fail-closed 拒绝本次锁获取。退避为 0 时仅在多次尝试间不留额外等待（测试友好）。
_SESSION_GATE_TIMEOUT_S = 10.0
_SESSION_GATE_ATTEMPTS = 3
_SESSION_GATE_BACKOFF_S = 0.25


_SESSION_LOCK_REGISTRY: dict[str, Any] = {}


_SESSION_LOCK_TIMEOUT_MIN = 60


_SESSION_LOCK_FLOCK_MARKER = "flock_active"


def _log_session_lock_degrade(action: str, payload: dict[str, Any]) -> None:
    """会话锁降级留痕（``orchd ▸ [session-lock]``，best-effort，R2-7）。

    stderr 是留痕通道（stdout 恒为 JSON 机器契约，见 conventions.md「命令输出通道
    契约」）；任何异常静默跳过，不阻断取锁主流程。不被 ``ORCHD_QUIET`` 抑制：
    与 worktree ``[回收]`` 这类常规噪声不同，**会话锁失效意味着并发保护降级**
    （R2-7 的原缺陷正是获取失败零留痕、调用方误以为已持锁），必须可追溯。

    Args:
        action: 动作名（``gate_unavailable`` / ``acquire_failed``）。
        payload: 结构化条目（reason / error / gate_acquired / hint…）。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass
    try:
        record = {"action": action, **payload}
        print(
            f"orchd ▸ [session-lock] {json.dumps(record, ensure_ascii=False)}",
            file=sys.stderr,
        )
    except Exception:
        pass


def _lock_state(acquire_result: dict[str, Any], gate_acquired: bool) -> dict[str, Any]:
    """把 :func:`session_lock_acquire` 结果收口为 :func:`ensure_session_lock` 的返回态。

    R2-7：``acquired=False``（flock / IO 失败）时必须**返回真实持锁态**并留痕，
    不得让调用方误以为已持锁（并发保护静默降级）。

    Args:
        acquire_result: ``session_lock_acquire`` 的结构化结果（唯一真源，不重算）。
        gate_acquired: 本次「检查 + 写入」是否真的被门锁串行化。

    Returns:
        以 acquire 结果为基础 + ``gate_acquired``；未持锁时追加 ``degraded``/``hint``
        并落 stderr 留痕。
    """
    state = dict(acquire_result)
    state["gate_acquired"] = gate_acquired
    if not acquire_result.get("acquired"):
        state["degraded"] = True
        state["hint"] = (
            "会话锁标记写入失败（best-effort 降级）：本会话**未持锁**，"
            "并发保护已失效；请排查锁目录可写性 / 文件占用后重跑"
        )
        _log_session_lock_degrade("acquire_failed", state)
    return state


def _acquire_session_gate(gate: Any) -> dict[str, Any]:
    """有界重试（退避）获取会话门锁，返回结构化结果（永不抛异常）。

    门锁失败的旧处置只留痕便继续「检查 + 写入」，恰重开了门锁本欲关闭的
    check-then-act 竞态（task-session-gate-timeout-fail-closed）：并发下两个
    session 可同时通过检查、各自写锁标记（后写覆盖先写），即 E019 双取。
    故改为**有界重试**——退避重试 ``_SESSION_GATE_ATTEMPTS`` 次仍不可得即返回
    ``acquired=False``，由 :func:`ensure_session_lock` 收口为拒绝态（fail-closed）。

    Args:
        gate: 已构造的 ``ExclusiveFileLock``（``.session.gate.lock``）。

    Returns:
        ``{"acquired": True, "attempts": <int>}``；或
        ``{"acquired": False, "attempts": <int>, "error": <str>}``
        （``error`` 为最后一次失败摘要；门锁 IO 故障 ``OSError`` 同样按「不可得」
        处置，不抛异常穿透调用方）。
    """
    last_error = ""
    for attempt in range(1, _SESSION_GATE_ATTEMPTS + 1):
        if attempt > 1 and _SESSION_GATE_BACKOFF_S > 0:
            time.sleep(_SESSION_GATE_BACKOFF_S * (attempt - 1))
        try:
            gate.acquire(blocking=True, timeout_s=_SESSION_GATE_TIMEOUT_S)
        except (OrchdError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        return {"acquired": True, "attempts": attempt}
    return {
        "acquired": False,
        "attempts": _SESSION_GATE_ATTEMPTS,
        "error": last_error,
    }


def _gate_unavailable_state(gate_result: dict[str, Any]) -> dict[str, Any] | None:
    """门锁不可得的 fail-closed 收口态：拒绝本次锁获取（含 stderr 留痕）。

    ``reason="gate_timeout"`` + ``degraded=True`` 声明「本会话未持锁、本次写命令
    未经串行化保护」，``exit_type="await-external"`` 指引调用方等待持锁者释放后
    重试（而非降级续跑）。**本次「检查 + 写入」整体被放弃**，锁标记文件不被创建 /
    覆盖——这正是与旧 best-effort 续跑的本质差别。

    返回 ``None`` 仅作测试负控（模拟「拒绝收口」被移除的修复前形态：调用方退回
    非串行 check + 写入），生产路径恒返回拒绝态。
    """
    state: dict[str, Any] = {
        "acquired": False,
        "reason": "gate_timeout",
        "error": gate_result.get("error") or "gate_unavailable",
        "degraded": True,
        "gate_acquired": False,
        "attempts": gate_result.get("attempts"),
        "retriable": True,
        "exit_type": "await-external",
        "hint": (
            "会话门锁不可得（E012 超时，有界重试已耗尽）：本次「检查 + 写入」按 "
            "fail-closed 拒绝执行，本会话**未持锁**、并发保护未生效；请等待持锁者"
            "释放后重试同一命令（僵死持有者用 "
            "python .orchd/__main__.py watchdog --timeout 0 排查释放）"
        ),
    }
    _log_session_lock_degrade("gate_unavailable", {"guard": "session_gate", **state})
    return state


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


def _resolve_store_root(orchd_dir: Path) -> Path:
    """解析会话锁的账本根（与 ``orchd.ledger.resolve_store_dir`` 组织语义完全一致）。

    惰性委托 ``orchd.ledger.resolve_store_dir``（单一来源）：
    - ``ORCHD_HOME`` 设置时重定向到外部目录（多 worktree 共享账本根）；
    - container 布局（``.orchd/.layout.json``）→ ``<容器>/.orchd-runtime``；
    - flat 场景回退 ``orchd_dir``（零回归）。

    task-session-lock-lifecycle（改 A）：此前只认 ``ORCHD_HOME``、不读 container
    布局标记，导致 container 下会话锁落进 ``main/.orchd/.session.lock`` 而非与
    Store 锁同根（``.orchd-runtime/``），两把互斥锁根不一致。惰性导入避免循环
    依赖（``orchd.ledger`` 模块级仅引用 ``orchd.errors``）。
    """
    from orchd.ledger import resolve_store_dir

    return resolve_store_dir(orchd_dir)


def _git_worktree_name(orchd_dir: Path) -> str | None:
    """解析当前 worktree 的 git 名称（``git worktree`` 场景）。

    主 worktree / 非 git / git 不可用时返回 ``None``（会话锁回退主维度）。
    ``git rev-parse --git-dir``：linked worktree 返回 ``<common>/.git/worktrees/<name>``
    （含 ``worktrees/`` 段），主 worktree 返回 ``<root>/.git``（或无后缀）。
    取 ``worktrees/`` 之后的段作为 worktree 维度名；解析失败静默降级。
    """
    project_root = orchd_dir.parent
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        git_dir = proc.stdout.strip()
        marker = "worktrees/"
        if marker not in git_dir:
            return None
        name = git_dir.rsplit(marker, 1)[-1].split(os.sep)[0].strip()
        return name or None
    except (OSError, subprocess.SubprocessError):
        return None


def _get_session_lock_path(orchd_dir: Path) -> Path:
    """返回 session lock 文件路径（worktree 维度唯一）。

    - 账本根解析遵循 ``ORCHD_HOME`` 重定向（多 worktree 共享同一账本根）；
    - worktree 维度后缀：git linked worktree 场景解析出 worktree 名时，
      锁文件按 ``.session-<worktree>`` 命名，不同 worktree 可分别持有锁不互踩；
      主 worktree / 非 git / 解析失败回退 ``.session.lock``（默认单 worktree 场景，
      worktree 维度唯一即全局唯一，行为与改造前一致）。
    """
    base = _resolve_store_root(orchd_dir) / _SESSION_LOCK_FILENAME
    wt = _git_worktree_name(orchd_dir)
    if wt:
        base = _resolve_store_root(orchd_dir) / f".session-{wt}.lock"
    return base


def _get_session_gate_path(orchd_dir: Path) -> Path:
    """返回 session 门锁文件路径（与 session lock 同根，worktree 维度唯一）。

    门锁为 flock gate，用于串行化 :func:`ensure_session_lock` 的"检查+写入"；
    与 session lock 文件（标记文件）分离，避免"标记文件被删除导致门锁失效"。
    """
    base = _resolve_store_root(orchd_dir) / _SESSION_GATE_FILENAME
    wt = _git_worktree_name(orchd_dir)
    if wt:
        base = _resolve_store_root(orchd_dir) / f".session-gate-{wt}.lock"
    return base


def _prepare_session_lock_payload(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None,
    session_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    """准备 session lock：解析 session_id、计算 lock_path、构建 lock_data。"""
    from datetime import datetime, timezone

    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    lock_path = _get_session_lock_path(orchd_dir)
    lock_data = {
        "agent_id": agent_id,
        "session_id": session_id or "",
        "nonce": os.urandom(8).hex(),
        "branch": branch,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        _SESSION_LOCK_FLOCK_MARKER: True,
    }
    return lock_path, lock_data


def _acquire_and_write_session_lock(
    lock_path: Path,
    lock_data: dict[str, Any],
) -> dict[str, Any]:
    """执行 flock 获取 + JSON 写入 + 注册表登记，返回结构化结果。

    task-session-lock-degrade-observability AC1：失败原因**分类**。此前三类语义
    完全不同的失败统一标 ``reason="io_error"``，调用方与响应无法区分「正常并发
    争用」与「真实环境故障」，也无法据此决定是否收紧为 fail-closed：

    - ``flock_contended`` —— 非阻塞 flock 被其他进程持有（``ExclusiveFileLock.acquire``
      抛 E012）。**正常并发语义**（等价 E019 workspace_busy），不是环境故障；
      若按 ``acquired=False`` 直接 fail-closed，此类正常碰撞会被误伤成硬失败。
    - ``lock_dir_unwritable`` —— 锁目录创建失败（EROFS / EACCES / 磁盘满等），
      持久性环境故障。
    - ``lock_write_failed`` —— 锁文件打开 / JSON 写入失败（文件占用、杀软扫描、
      权限等），环境故障。

    ``error`` 摘要字段三种情况下都保留（原文透传，便于排查）。

    Returns:
        ``acquired=True``（新锁或 ``reused=True`` 刷新），或 ``acquired=False``
        并带分类后的 ``reason`` 与 ``error``。
    """
    import json

    from orchd.lockfile import ExclusiveFileLock

    try:
        # worktree 维度锁可能落在 ORCHD_HOME 重定向根下，父目录未必存在
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, IOError) as exc:
        return {"acquired": False, "reason": "lock_dir_unwritable", "error": str(exc)}
    try:
        # 本进程已持同一锁：复用 fd（同 session 刷新覆盖写，可重入）
        existing = _SESSION_LOCK_REGISTRY.get(str(lock_path))
        if existing is not None:
            existing.write_text(json.dumps(lock_data, ensure_ascii=False))
            return {"acquired": True, "reused": True, "path": str(lock_path)}
        # 先持有 OS flock（非阻塞），再写 JSON 标记；flock 由内核托管，
        # 进程退出/崩溃时自动释放，检查方可探活判定 stale。
        flock = ExclusiveFileLock(lock_path)
        try:
            flock.acquire(blocking=False, timeout_s=0.5)
        except OrchdError as exc:
            # E012 = 非阻塞获取失败（锁被其他进程持有）→ 并发争用，非环境故障
            return {
                "acquired": False,
                "reason": "flock_contended",
                "error": f"flock acquire failed: {exc}",
            }
        flock.write_text(json.dumps(lock_data, ensure_ascii=False))
    except (OSError, IOError) as exc:
        # 锁文件打开 / 写入失败（含 Windows 文件占用、权限、磁盘故障）
        return {"acquired": False, "reason": "lock_write_failed", "error": str(exc)}
    _SESSION_LOCK_REGISTRY[str(lock_path)] = flock
    return {"acquired": True, "path": str(lock_path)}


def session_lock_acquire(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """写入 session lock 文件（agent_id + session_id + nonce + branch + timestamp）。

    并发互斥由 ensure_session_lock 内的 flock gate 保证；本函数自身覆盖写入（幂等）。
    新式锁持有 OS flock fd 作进程活性探针，进程退出时内核自动释放，检查方可
    探活判定 stale 并自动清理。锁文件仅作并发互斥载体，不承载 agent 身份。

    Args:
        orchd_dir: .orchd 目录路径。
        agent_id: 当前 session 的 agent ID。
        branch: 当前 git 分支名（可选）。
        session_id: 当前 session ID；缺省时从环境解析。

    Returns:
        结构化结果，永不抛异常：acquired=True（新锁或 reused=True 刷新），或
        acquired=False 且 reason ∈ {flock_contended, lock_dir_unwritable,
        lock_write_failed}（分类见 :func:`_acquire_and_write_session_lock`）。
    """
    lock_path, lock_data = _prepare_session_lock_payload(
        orchd_dir, agent_id, branch, session_id
    )
    result = _acquire_and_write_session_lock(lock_path, lock_data)
    # worktree 孤儿会话锁回收（task-audit-lock-residue-reclaim AC2）：session 路径
    # 每次写锁时 best-effort 识别并清理 worktree 已不存在的锁孤儿（不抛异常、
    # 不阻塞主流程；主 worktree 锁不触碰）。
    try:
        reclaim_orphan_session_locks(orchd_dir, orchd_dir.parent)
    except Exception:
        pass
    return result


def session_lock_release(orchd_dir: Path) -> dict[str, Any]:
    """释放 session lock（幂等：锁文件不存在时不报错）。

    task-session-lock-autoclean（改 B）：若本进程持有该锁的 flock fd
    （注册表登记），先释放 flock 并关闭 fd，再删除 JSON 标记文件；
    非本进程持有的锁（watchdog 清理他人僵死锁）直接删除标记文件
    （flock 由持锁进程退出/释放时内核回收）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"released": True, "reason": "removed"}`` 锁文件已删除。
            - ``{"released": True, "reason": "not_exists"}`` 锁文件本就不存在（幂等）。
            - ``{"released": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    lock_path = _get_session_lock_path(orchd_dir)
    # 先释放本进程持有的 flock fd（若持有），再处理标记文件
    flock = _SESSION_LOCK_REGISTRY.pop(str(lock_path), None)
    if flock is not None:
        flock.release()
    if not lock_path.exists():
        return {"released": True, "reason": "not_exists"}
    # P2-6：删除标记前探测 flock 活性——他人仍持活锁时不得 unlink（flock-unlink 竞态：
    # 同路径新 inode 会被新进程重新加锁，破坏互斥）。活锁跳过，仅 stale（无持有者）才删。
    probe = _probe_session_lock_os_active(lock_path)
    if probe.get("active"):
        return {"released": False, "reason": "held_by_other"}
    try:
        _safe_delete(lock_path, orchd_dir)
        return {"released": True, "reason": "removed"}
    except (OSError, IOError) as exc:
        return {"released": False, "reason": "io_error", "error": str(exc)}


def release_session_lock_if_owned(
    orchd_dir: Path,
    agent_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """条件释放 session 锁：仅当锁存在且持有者 == ``agent_id`` 且 session 一致才释放。

    task-session-lock-lifecycle：写命令（done/review/claim）在退出时（含异常路径）
    调用，确保持有本 agent 的锁不会因异常漏放；绝不误释放他人锁/他 session 锁。

    Returns:
        ``{"released": bool, "reason": str}``。锁缺失 / 持他人锁时
        ``released=False``（如 ``reason="not_owner_or_absent"``）。
    """
    if orchd_dir is None:
        return {"released": False, "reason": "no_project"}
    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    check = session_lock_check(orchd_dir)
    holder_session = check.get("session_id") or ""
    if (
        check.get("locked")
        and check.get("agent_id") == agent_id
        and (not holder_session or not session_id or holder_session == session_id)
    ):
        return session_lock_release(orchd_dir)
    return {"released": False, "reason": "not_owner_or_absent"}


def _probe_session_lock_os_active(lock_path: Path) -> dict[str, Any]:
    """非阻塞 OS 活性探测：尝试对锁文件获取 flock/msvcrt 排他锁。

    task-session-lock-autoclean：新式锁持锁进程保持 fd 打开，flock 由内核
    托管——进程退出/崩溃时内核自动释放。因此「能获取 flock」⇔ 原持锁进程
    已死（stale）；「不能获取」⇔ 活锁（有进程存活持有）。

    Returns:
        ``{"stale": True}`` 原持锁进程已死（本进程刚拿到 flock，已释放）。
        ``{"stale": False, "active": True}`` 活锁（另一进程持锁中）。
    """
    import os as _os
    from orchd.lockfile import _flock_op

    # 探活前文件已消失（并发清理）：等同无锁，不创建新文件
    if not lock_path.exists():
        return {"stale": True, "active": False}

    # 直接用底层 fd + flock 探测，绕过 ExclusiveFileLock 的同进程跨实例重入
    # （重入会让探测实例"成功获取"，误判活锁为 stale）。
    try:
        fd = _os.open(str(lock_path), _os.O_RDWR)
    except OSError:
        # 打开失败（IO 等）：保守视为活锁，不误清
        return {"stale": False, "active": True}

    try:
        _flock_op(fd, "lock_nb")
    except (OSError, IOError):
        # 获取失败 → 活锁（本进程或其他进程持有）
        try:
            _os.close(fd)
        except OSError:
            pass
        return {"stale": False, "active": True}
    # 获取成功：原持锁进程已死，释放探测锁
    try:
        _flock_op(fd, "unlock")
    except (OSError, IOError):
        pass
    try:
        _os.close(fd)
    except OSError:
        pass
    return {"stale": True, "active": False}


def _linked_worktree_names(main_root: Path) -> set[str] | None:
    """git 登记的 worktree 目录名集合（best-effort；``None`` = 探测失败）。

    review W-17：flat 布局不存在「任务 worktree 在主工作树父目录」这一约定，
    活跃性判定改以 ``git worktree list --porcelain`` 的登记名为准（布局无关，
    且绝不指向仓库父目录）。非 git / git 不可用 / 非零退出 → ``None``，
    调用方保守跳过删除。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(main_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            names.add(Path(line[len("worktree "):].strip()).name)
    return names


def reclaim_orphan_session_locks(
    orchd_dir: Path,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """回收 worktree 已不存在的会话锁孤儿（task-audit-lock-residue-reclaim AC2）。

    任务 worktree 终态回收 / 手动删除后，其 worktree 维度锁文件
    （``.session-<wt>.lock`` / ``.session-gate-<wt>.lock``）残留在共享账本根。
    本函数在 **session 路径**（:func:`session_lock_acquire` 每次写锁时）best-effort
    扫描并识别可回收孤儿：

    - 解析文件名中的 worktree 名（``.session-<wt>.lock`` / ``.session-gate-<wt>.lock``）；
    - 该 worktree **仍存活** → 活跃锁，跳过；
    - 被 live flock 持有（其他进程存活持锁）→ 跳过（防 flock-unlink 竞态）；
    - 否则视为孤儿可回收 → 删除，记入 ``cleaned``。

    活跃性判据按布局解析（review W-17）：
    - container → 布局解析出的 ``task_wt_root``（``<容器>``）下是否存在同名目录；
    - flat → 以 ``git worktree list --porcelain`` 的登记名为准（**绝不**使用
      ``main_root.parent``：那是仓库父目录，会指向无关目录并误判/误删活跃锁）；
      git 探测失败 → 保守跳过（不删）。

    另：``.session-*.lock`` 通配也会命中门锁（``.session-gate-<wt>.lock``），
    故门锁只由专用 pattern 处理一次（W-17，避免同一文件被访问两次）。

    主 worktree 锁（``.session.lock`` / ``.session.gate.lock``，无 worktree 后缀）不在
    匹配范围，永不触碰；与 ``worktree._cleanup_stale_session_locks``（task- 前缀 + watchdog
    路径）互补，本函数不限于 task- 前缀、走 session 路径。

    Args:
        orchd_dir: 主工作树的 .orchd 目录（用于解析共享账本根）。
        project_root: 主工作树根（推断任务 worktree 根）；缺省时用 ``orchd_dir.parent``。

    Returns:
        ``{"cleaned": [<str>]}`` 已清理的孤儿锁文件名清单；任何异常降级为空清单
        （best-effort，不阻断锁获取）。
    """
    store_root = _resolve_store_root(orchd_dir)
    main_root = Path(project_root).resolve() if project_root else Path(orchd_dir).parent

    # 快路径：无候选锁 → 不触发布局解析 / git 调用（session 路径零额外开销）。
    candidates: list[Path] = []
    try:
        for pattern in (".session-*.lock", ".session-gate-*.lock"):
            for p in sorted(store_root.glob(pattern)):
                if pattern == ".session-*.lock" and p.name.startswith(".session-gate-"):
                    continue  # 门锁由专用 pattern 处理一次（W-17）
                candidates.append(p)
    except OSError:
        return {"cleaned": []}
    if not candidates:
        return {"cleaned": []}

    try:
        from orchd.worktree import detect_layout

        layout = detect_layout(main_root)
    except Exception:
        layout = {}
    if layout.get("layout") == "container" and layout.get("task_wt_root"):
        task_wt_root: Path | None = Path(layout["task_wt_root"])
        live_names: set[str] | None = None
    else:
        task_wt_root = None
        live_names = _linked_worktree_names(main_root)

    def _worktree_alive(wt: str) -> bool:
        """worktree 名是否仍存活；无法判定时保守返回 True（不删）。"""
        if task_wt_root is not None:
            return (task_wt_root / wt).exists()
        if live_names is None:
            return True  # git 探测失败 → 保守跳过
        return wt in live_names

    cleaned: list[str] = []
    for p in candidates:
        name = p.name
        if name.startswith(".session-gate-"):
            wt = name[len(".session-gate-"):-len(".lock")]
        elif name.startswith(".session-"):
            wt = name[len(".session-"):-len(".lock")]
        else:
            continue
        if not wt:
            continue
        if _worktree_alive(wt):
            continue
        # 他人仍持活锁 → 不删（flock-unlink 竞态，与 _cleanup_stale_session_locks 一致）
        if _probe_session_lock_os_active(p).get("active"):
            continue
        try:
            _safe_delete(p, orchd_dir)
            cleaned.append(name)
        except OSError:
            pass
    return {"cleaned": cleaned}


def session_lock_check(
    orchd_dir: Path,
    timeout_min: int = _SESSION_LOCK_TIMEOUT_MIN,
) -> dict[str, Any]:
    """检查 session lock 状态：是否存在、是否超时、内容是否合法。

    task-session-lock-autoclean（改 B）：新式 flock 活性锁（JSON 含
    ``flock_active: true``）优先做 **OS 非阻塞探活**——
    - 原持锁进程已死（能获取 flock）→ 判定 stale 并**自动清理**（删除 JSON），
      返回 ``{"locked": False, "reason": "stale_cleaned", ...}``；
    - 活锁（不能获取 flock）→ 返回 locked（调用方拒绝写入 E019），
      此时不再仅凭 timeout 判死（活锁即使超时也由 watchdog 另行处理）。
    旧纯 JSON 锁（无 ``flock_active`` 字段）保持兼容：按 timeout 判定，
    不探活、不误清。

    Args:
        orchd_dir: .orchd 目录路径。
        timeout_min: 超时分钟数（默认 60）。超时视为僵死锁，可覆盖。

    Returns:
        结构化结果，永不抛异常：
            - ``{"locked": False}`` 无锁文件 / 锁已超时 / 锁文件损坏（可覆盖）。
            - ``{"locked": False, "reason": "stale_cleaned", "agent_id": <str>,
                 "session_id": <str>, "age_min": <float>,
                 "cleanup_result": {...}}``
              新式锁且持锁进程已死，已自动清理（可覆盖）。
            - ``{"locked": True, "agent_id": <str>, "branch": <str|None>,
                 "timestamp": <str>, "age_min": <float>}``
              锁有效且未超时 / 新式活锁，调用方应拒绝写入（E019 workspace_busy）。

    Note:
        锁文件损坏（JSON 解析失败、缺少必要字段）视为可覆盖（容错），
        返回 ``{"locked": False, "reason": "corrupted"}``。
    """
    import json
    from datetime import datetime, timezone

    lock_path = _get_session_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"locked": False}

    # 读取锁内容：优先走本进程持锁 fd（Windows msvcrt 字节锁阻止新句柄读取）
    from orchd.lockfile import read_locked_text

    content = read_locked_text(lock_path)
    if content is None:
        try:
            content = lock_path.read_text(encoding="utf-8")
        except (OSError, IOError):
            content = None
    if content is None:
        # 读取失败：Windows 下多为「他进程持锁」字节锁阻止读取；探活判定
        probe = _probe_session_lock_os_active(lock_path)
        _read_err = (
            f"lock file unreadable ({lock_path.name}): "
            "read_locked_text returned None, fallback read_text also failed; "
            "likely held by another process (msvcrt byte lock on Windows)"
        )
        if probe.get("active"):
            return {"locked": True, "reason": "no_marker", "read_error": _read_err}
        return {"locked": False, "reason": "corrupted", "read_error": _read_err}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # 锁文件损坏：视为可覆盖
        return {"locked": False, "reason": "corrupted"}

    # 校验必要字段
    agent_id = data.get("agent_id")
    timestamp_str = data.get("timestamp")
    if not agent_id or not timestamp_str:
        return {"locked": False, "reason": "corrupted"}

    # 解析时间戳
    try:
        lock_time = datetime.fromisoformat(timestamp_str)
        if lock_time.tzinfo is None:
            # 兼容无时区的时间戳（视为 UTC）
            lock_time = lock_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"locked": False, "reason": "corrupted"}

    # 计算锁年龄
    now = datetime.now(timezone.utc)
    age_seconds = (now - lock_time).total_seconds()
    age_min = age_seconds / 60.0

    # 新式 flock 活性锁：优先 OS 探活判定 stale（task-session-lock-autoclean）
    if data.get(_SESSION_LOCK_FLOCK_MARKER):
        probe = _probe_session_lock_os_active(lock_path)
        if probe.get("stale"):
            # 原持锁进程已死：自动清理（best-effort），后续可重新获取
            try:
                _safe_delete(lock_path, orchd_dir)
                cleaned = True
            except (OSError, IOError):
                cleaned = False
            return {
                "locked": False,
                "reason": "stale_cleaned",
                "agent_id": agent_id,
                "session_id": data.get("session_id") or "",
                "branch": data.get("branch"),
                "age_min": age_min,
                "cleanup_result": {"cleaned": cleaned, "path": str(lock_path)},
            }
        # 活锁：进程仍持有 flock → 有效锁（即使超时也不仅凭 timeout 判死）
        return {
            "locked": True,
            "agent_id": agent_id,
            "session_id": data.get("session_id") or "",
            "nonce": data.get("nonce") or "",
            "branch": data.get("branch"),
            "timestamp": timestamp_str,
            "age_min": age_min,
            _SESSION_LOCK_FLOCK_MARKER: True,
        }

    # 旧纯 JSON 锁兼容：无 flock 活性标记，按 timeout 判定，不探活不误清
    # 超时视为僵死锁，可覆盖
    if age_min >= timeout_min:
        return {"locked": False, "reason": "timeout", "age_min": age_min}

    # 锁有效且未超时
    return {
        "locked": True,
        "agent_id": agent_id,
        "session_id": data.get("session_id") or "",
        "nonce": data.get("nonce") or "",
        "branch": data.get("branch"),
        "timestamp": timestamp_str,
        "age_min": age_min,
    }

