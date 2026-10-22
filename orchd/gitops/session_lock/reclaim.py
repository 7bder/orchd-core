"""陈旧锁判定与孤儿锁回收（_reclaim_stale_lock/_linked_worktree_names/reclaim_orphan_session_locks）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import subprocess

from orchd.gitops._const import _GIT_TIMEOUT
from .paths import _resolve_store_root


def _reclaim_stale_lock(lock_path: Path) -> dict[str, Any]:
    """stale 判定 + 回收合一：**持有探测 flock 期间**完成删除。

    task-pass9-session-lock-unlink（pass9 F10）：旧流程 probe 成功后先
    unlock+close、调用方随后才 unlink——间隙内他进程可抢锁，随后 unlink 把
    同路径互斥分裂成两个 inode（POSIX；Windows 因句柄占用 fail-safe）。
    本 helper 持锁内 unlink（POSIX 经典 unlink-under-lock：他进程在窗口内
    的 flock 请求必失败，互斥不再分裂）；Windows 下打开中的文件 unlink 报
    共享冲突 → 降级为关闭 fd 后重试删除（保留既有 fail-safe 行为）。

    Returns:
        ``{"reclaimed": True}`` 已回收（含文件本就不存在的幂等 no-op）；
        ``{"held": True}`` 他人持活锁，不得回收；
        ``{"error": <str>}`` 打开/删除失败（保守不回收）。
    """
    import os as _os
    from orchd.lockfile import _flock_op

    if not lock_path.exists():
        return {"reclaimed": True}
    try:
        fd = _os.open(str(lock_path), _os.O_RDWR)
    except OSError as exc:
        return {"error": str(exc)}
    try:
        _flock_op(fd, "lock_nb")
    except (OSError, IOError):
        # 获取失败 → 活锁（另一进程存活持有）
        try:
            _os.close(fd)
        except OSError:
            pass
        return {"held": True}
    unlinked = False
    try:
        _os.unlink(str(lock_path))  # 持锁内删除：他进程此刻 flock 必失败
        unlinked = True
    except OSError:
        pass  # Windows：打开中的文件不可删 → 降级路径
    try:
        _flock_op(fd, "unlock")
    except (OSError, IOError):
        pass
    try:
        _os.close(fd)
    except OSError:
        pass
    if unlinked:
        return {"reclaimed": True}
    # Windows 降级：fd 已关闭，重试删除（既有 fail-safe 行为；仍被占用则报错）
    try:
        _os.unlink(str(lock_path))
    except OSError as exc:
        return {"error": str(exc)}
    return {"reclaimed": True}


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
        # 他人仍持活锁 → 不删（pass9 F10：持探测锁内 rename-aside，窗口归零）
        reclaim = _reclaim_stale_lock(p)
        if not reclaim.get("reclaimed"):
            continue
        cleaned.append(name)
    return {"cleaned": cleaned}
