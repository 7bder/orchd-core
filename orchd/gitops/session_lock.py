"""gitops session_lock 域：会话锁（13 函数 + 5 常量，_SESSION_LOCK_REGISTRY 单例）。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops._const import _GIT_TIMEOUT
from orchd.gitops.cleanup import _safe_delete


_SESSION_LOCK_FILENAME = ".session.lock"


_SESSION_GATE_FILENAME = ".session.gate.lock"


_SESSION_LOCK_REGISTRY: dict[str, Any] = {}


_SESSION_LOCK_TIMEOUT_MIN = 60


_SESSION_LOCK_FLOCK_MARKER = "flock_active"


def ensure_session_lock(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
    session_id: str | None = None,
) -> None:
    """确保当前 session 可写入：门锁串行化"检查+获取"，被其他 session 持有则 E019。

    best-effort：门锁 / 锁获取失败（IO 错误）不抛异常，静默降级。

    与旧"check-then-act"区别：旧实现先 :func:`session_lock_check` 再
    :func:`session_lock_acquire`（覆盖写），两个并发 session 都可通过检查并同时
    "获取"（后写覆盖先写）。本实现先用一个**门锁**（flock gate，ExclusiveFileLock）
    串行化整个"检查 + 写入"——一次仅一个进程能通过检查并写锁标记，其余读到该标记后
    据 session 归属判 E019 或幂等复用（刷新覆盖写）。

    Session Identity Layer：同 ``agent_id`` 但不同 ``session_id`` 视为另一个
    session（即使指纹相同），防止同 agent 多会话互踩/误释放锁。
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
    acquired_gate = False
    try:
        gate.acquire(blocking=True, timeout_s=10.0)
        acquired_gate = True
    except OrchdError:
        # 门锁获取失败（超时/被占）：静默降级，仅凭当前标记判定（best-effort）
        acquired_gate = False
    try:
        check = session_lock_check(orchd_dir)
        if check.get("locked"):
            holder = check.get("agent_id", "unknown")
            holder_session = check.get("session_id") or ""
            if holder == agent_id and (not holder_session or holder_session == session_id):
                # 本 session 已持锁：刷新覆盖写（幂等复用）
                session_lock_acquire(orchd_dir, agent_id, branch, session_id=session_id)
                return
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
        # 未被持有 / 损坏 / 超时（可覆盖）：直接写锁标记
        session_lock_acquire(orchd_dir, agent_id, branch, session_id=session_id)
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
    """执行 flock 获取 + JSON 写入 + 注册表登记，返回结构化结果。"""
    import json

    from orchd.lockfile import ExclusiveFileLock

    try:
        # worktree 维度锁可能落在 ORCHD_HOME 重定向根下，父目录未必存在
        lock_path.parent.mkdir(parents=True, exist_ok=True)
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
            return {
                "acquired": False,
                "reason": "io_error",
                "error": f"flock acquire failed: {exc}",
            }
        flock.write_text(json.dumps(lock_data, ensure_ascii=False))
        _SESSION_LOCK_REGISTRY[str(lock_path)] = flock
        return {"acquired": True, "path": str(lock_path)}
    except (OSError, IOError) as exc:
        return {"acquired": False, "reason": "io_error", "error": str(exc)}


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
        结构化结果，永不抛异常：acquired=True（新锁或 reused=True 刷新），
        或 acquired=False reason=io_error。
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
    - 该 worktree 目录（``<task_wt_root>/<wt>``）**仍存在** → 活跃锁，跳过；
    - 被 live flock 持有（其他进程存活持锁）→ 跳过（防 flock-unlink 竞态）；
    - 否则视为孤儿可回收 → 删除，记入 ``cleaned``。

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
    task_wt_root = main_root.parent
    cleaned: list[str] = []
    try:
        for pattern in (".session-*.lock", ".session-gate-*.lock"):
            for p in sorted(store_root.glob(pattern)):
                name = p.name
                if name.startswith(".session-gate-"):
                    wt = name[len(".session-gate-"):-len(".lock")]
                elif name.startswith(".session-"):
                    wt = name[len(".session-"):-len(".lock")]
                else:
                    continue
                if not wt:
                    continue
                # worktree 目录仍存在 → 活跃，跳过
                if (task_wt_root / wt).exists():
                    continue
                # 他人仍持活锁 → 不删（flock-unlink 竞态，与 _cleanup_stale_session_locks 一致）
                if _probe_session_lock_os_active(p).get("active"):
                    continue
                try:
                    _safe_delete(p, orchd_dir)
                    cleaned.append(name)
                except OSError:
                    pass
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

