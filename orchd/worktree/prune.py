"""任务 worktree 清剪（task-14-worktree-lifecycle）：容器根杂项 + 孤儿回收。

依赖方向：同包 layout/bindings 原语 + recycle（移除/留痕）。
"""


from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.worktree.bindings import _TASK_WT_PREFIX, load_bindings
from orchd.worktree.layout import (
    _git_common_dir, _is_git_tracked_path,
    _is_junk_entry, detect_layout,
)
from orchd.worktree.recycle import (
    _cleanup_stale_session_locks, _log_recycle, _recycle_actor,
    _rmtree_force, remove_task_wt,
)


def _cleanup_container_root_junk(task_wt_root: Path) -> list[str]:
    """清理容器根可再生杂项（best-effort）。

    task-workspace-docs-isolation：测试/缓存杂项（``.pytest-tmp`` /
    ``.pytest_cache`` 等）可能落在容器根（main/ 之外）。复用 ``_is_junk_entry``
    保守名单，**清理前逐条做 ``git ls-files`` tracked 校验**（review W-15：
    此前 docstring 承诺「绝不触碰被跟踪文件」但无校验，可删仓库跟踪的
    ``htmlcov/``）——被跟踪路径跳过；所属仓库不可判定 / 探测异常同样跳过
    （保守）。绝不触碰 .git / .orchd / 工具目录 / 被跟踪文件。

    Returns:
        已清理的条目名清单。
    """
    cleaned: list[str] = []
    try:
        for entry in sorted(task_wt_root.iterdir()):
            if not _is_junk_entry(entry.name):
                continue
            if _is_git_tracked_path(entry):
                continue  # W-15：被 git 跟踪 → 绝不触碰（含 in-cone 的 htmlcov/）
            try:
                if entry.is_dir():
                    removed = _rmtree_force(entry)
                else:
                    entry.unlink()
                    removed = not entry.exists()
                if removed:
                    cleaned.append(entry.name)
            except OSError:
                pass
    except OSError:
        pass
    return cleaned


def prune_orphans(
    project_root: Path,
    store_root: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """孤儿 worktree 惰性清理（watchdog / status 调用，best-effort）。

    清理三类：
    - 绑定任务已终态（completed/cancelled）但 worktree 仍在 → remove；
    - 绑定任务已不在 master / 无对应活跃任务 → remove + 解绑；
    - 文件系统残留 task-* 空目录（P0-19，Windows git worktree remove 不完整）→ 清理。

    安全边界（review W-1 / W-12 / W-15 / R-16 / R-23）：
    - **布局门**：仅 container 布局存在独立任务 worktree。flat 下 ``detect_layout``
      把 ``task_wt_root`` 解析为**仓库父目录**，扫它会越界删除父目录下无关的
      ``task-*`` 目录与 ``htmlcov`` 等杂项 —— flat 直接早返回：零扫描、零删除
      （判据与 ``doctor.py`` 的 ``layout != "container"`` 门一致）。
    - **持锁 + 新鲜状态**：全部破坏性动作（worktree 回收 / 目录删除）在
      ``Store.acquire_lock()`` 内执行；每个 task_id 处理前重新 ``replay()`` 取新鲜
      状态，消除「入口陈旧快照判定 → 并发重新 claim → 按陈旧 completed 强删」的
      TOCTOU 窗口。新鲜账本中查不到该任务时回退调用方快照（保持既有调用契约；
      凡新鲜账本有记录，一律以记录为准）。
    - **残留证据**：``task-*`` 目录仅在存在「曾由 git 登记为本仓库 worktree」证据
      （``<git-common-dir>/worktrees/<name>``）时才判为残留；``rmdir`` 失败
      **禁止**升级 ``rmtree``，改为留痕保留（交 doctor / 人工处置）。

    删除决策审计（2026-08-30 复盘 §1）：每次判定「可清理」或「拒绝清理」前输出
    决策上下文（task_id / status / status_source / has_active_binding / git 登记 /
    判定依据），删除动作由 ``remove_task_wt`` 内部再记 recycle_log，杜绝 best-effort
    静默删除无痕。

    Returns:
        ``{"pruned": [<str>], "orphans_found": int, "residual_cleaned": [<str>],
        "decisions": [<dict>]}``；decisions 为本次全部删除/拒绝决策记录。
        flat 布局 / 锁不可用等「未执行」场景额外返回 ``skipped``（不静默）。
    """
    from orchd.gitops import _has_linked_worktrees

    project_root = Path(project_root).resolve()

    # 布局门（W-1 / R-16）：flat 无任务 worktree 概念，且 task_wt_root 会解析为
    # 仓库父目录 —— 扫描/删除即越界操作他人目录，直接早返回。
    try:
        layout = detect_layout(project_root)
    except Exception:
        return {"pruned": [], "orphans_found": 0, "skipped": "layout_unresolved"}
    if layout.get("layout") != "container":
        return {"pruned": [], "orphans_found": 0, "skipped": "flat_layout"}
    task_wt_root = Path(layout["task_wt_root"])

    try:
        from orchd.errors import OrchdError

        bindings = load_bindings(store_root)
    except OrchdError:
        # 绑定表损坏：信息不全时保守跳过本轮 prune（不基于空表误删 worktree）
        return {"pruned": [], "orphans_found": 0, "skipped": "bindings_corrupt"}
    pruned: list[str] = []
    residual_cleaned: list[str] = []
    decisions: list[dict[str, Any]] = []
    actor = _recycle_actor()

    # 破坏性段落持账本锁（W-12 / R-23）：加锁后才判定、才删除。
    try:
        from orchd.ledger import Store

        store = Store(store_root)
        store.acquire_lock()
    except Exception as exc:
        # 锁不可用（超时 / 后端异常）→ 放弃本轮破坏性清理（best-effort，不抛）
        _log_recycle([{
            "action": "skip",
            "kind": "prune_orphans",
            "reason": "store_lock_unavailable",
            "error": str(exc),
            "actor": actor,
        }])
        return {"pruned": [], "orphans_found": 0, "skipped": "store_lock_unavailable"}

    def _fresh_status(task_id: str) -> tuple[str | None, str]:
        """锁内重新 replay 取新鲜状态，返回 ``(status, status_source)``。

        新鲜账本有该任务记录 → 以记录为准（``fresh_replay``，修正陈旧快照）；
        无记录 → 回退调用方入口快照（``entry_snapshot_fallback``，保持既有调用
        契约）；两者皆无 → ``(None, "absent")``，调用方按「非终态」保守处理。
        """
        try:
            fresh = store.replay()
        except Exception:
            fresh = {}
        ts = fresh.get(task_id)
        if ts is not None:
            return ts.status, "fresh_replay"
        snapshot = state.get(task_id)
        if snapshot is not None:
            return snapshot.status, "entry_snapshot_fallback"
        return None, "absent"

    try:
        # 既有绑定任务清理（需要 git 层 linked worktrees 存在才执行 git worktree remove）
        if _has_linked_worktrees(project_root):
            for task_id, entry in list(bindings.items()):
                status, status_source = _fresh_status(task_id)
                effective = status or "pending"
                if effective in ("completed", "cancelled"):
                    # 终态：回收 worktree + 解绑（删除动作由 remove_task_wt 记 recycle_log）
                    decisions.append({
                        "action": "recycle",
                        "task_id": task_id,
                        "kind": "terminal_binding",
                        "status": effective,
                        "status_source": status_source,
                        "bound_worktree": (entry or {}).get("worktree"),
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
                    # lock_held=True：复用本函数已持有的账本锁（避免同进程双 fd E012）
                    result = remove_task_wt(
                        project_root, task_id, store_root, lock_held=True,
                    )
                    if result.get("removed"):
                        pruned.append(task_id)

        # P0-19：扫描文件系统残留 task-* 目录（git 不登记但目录仍存在）。
        # 不依赖 _has_linked_worktrees——Windows 下 git worktree remove 成功但目录残留。
        common_dir = _git_common_dir(project_root)
        registry = (common_dir / "worktrees") if common_dir is not None else None
        if task_wt_root.exists():
            for entry in sorted(task_wt_root.iterdir()):
                if (not entry.is_dir()
                        or not entry.name.startswith(_TASK_WT_PREFIX)):
                    continue
                # 是 task-* 目录 → 检查是否有活跃绑定
                # 从目录名反推 task_id（task-<short> → task-<short> 或 task/<short>）
                short = entry.name[len(_TASK_WT_PREFIX):]
                candidate_ids = [f"task-{short}", short]
                has_active_binding = False
                for cid in candidate_ids:
                    if cid in bindings:
                        status, status_source = _fresh_status(cid)
                        if (status or "pending") not in ("completed", "cancelled"):
                            has_active_binding = True
                        break
                if has_active_binding:
                    continue
                if (entry / ".git").exists():
                    continue
                # W-1 / R-16：残留证据 —— 仅「曾由 git 登记为本仓库 worktree」的目录
                # 才可能是 remove 不完整的残留；无证据 = 他人/无关目录，保留并留痕。
                registered = registry is not None and (registry / entry.name).exists()
                if not registered:
                    decisions.append({
                        "action": "keep",
                        "target": entry.name,
                        "kind": "residual_dir",
                        "reason": "no_git_registry_evidence",
                        "has_active_binding": False,
                        "git_registered": False,
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
                    continue
                decisions.append({
                    "action": "clean",
                    "target": entry.name,
                    "kind": "residual_dir",
                    "has_active_binding": False,
                    "git_registered": True,
                    "actor": actor,
                })
                _log_recycle(decisions[-1:])
                try:
                    entry.rmdir()  # 仅空目录（R-16：失败禁止升级 rmtree）
                    residual_cleaned.append(entry.name)
                except OSError as exc:
                    decisions.append({
                        "action": "keep",
                        "target": entry.name,
                        "kind": "residual_dir",
                        "reason": "not_empty_or_locked",
                        "error": str(exc),
                        "git_registered": True,
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
    finally:
        try:
            store.release_lock()
        except Exception:
            pass

    # task-workspace-docs-isolation：容器级卫生清理（best-effort）——
    # ① worktree 已不存在的会话锁残留；② 容器根可再生杂项；③ 系统 temp 中
    # 历史 orchd-trash-* 残留（早期 _safe_delete 降级重命名产物）。
    stale_locks_cleaned: list[str] = []
    junk_cleaned: list[str] = []
    trash_residue_cleaned: list[str] = []
    try:
        # task_wt_root 已在布局门处解析（container 布局），此处不再重复 detect_layout
        if task_wt_root.exists():
            stale_locks_cleaned = _cleanup_stale_session_locks(store_root, task_wt_root)
            junk_cleaned = _cleanup_container_root_junk(task_wt_root)
        from orchd.gitops import _cleanup_trash_residue

        trash_residue_cleaned = _cleanup_trash_residue()
    except Exception:
        pass

    summary: dict[str, Any] = {"pruned": pruned, "orphans_found": len(pruned)}
    if residual_cleaned:
        summary["residual_cleaned"] = residual_cleaned
    if decisions:
        summary["decisions"] = decisions
    if stale_locks_cleaned:
        summary["stale_locks_cleaned"] = stale_locks_cleaned
    if junk_cleaned:
        summary["junk_cleaned"] = junk_cleaned
    if trash_residue_cleaned:
        summary["trash_residue_cleaned"] = trash_residue_cleaned
    return summary
