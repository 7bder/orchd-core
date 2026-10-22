"""自动清理通道（_auto_clean_item/_auto_move_legacy/auto_clean/_auto_clean_impl）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import os
import shutil
import time

from orchd.doctor.residue import _AUTO_CLEAN_TYPES, _LEGACY_MOVE_TYPES, detect_residues
from orchd.doctor.runtime import _pre_delete_lock_recheck, _run_git
from .delete import _delete_file
from .dispose import _dispose_residual_dir
from .prune import _prune_stale_backups


def _auto_clean_item(project_root: Path,
                     item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """执行单个 Auto-Clean 残留的清理（best-effort，失败返回 (False, 带 error)）。

    删除动作与 ``doctor --fix`` 同源：文件删除走 :func:`_delete_file`
    （``_safe_delete``，DR-10）；残留目录走 :func:`_dispose_residual_dir`
    （先移入 ``.doctor-backup/residual/<ts>/``，含真实内容则拒绝处置，DR-3）。
    """
    path = Path(item["path"])
    action = item.get("action", "delete")
    try:
        if action == "git_worktree_prune":
            res = _run_git(project_root, ["worktree", "prune"])
            if res.returncode != 0:
                return False, {**item, "error": res.stderr.strip()}
            return True, {**item, "disposition": "pruned"}
        if action == "delete_dir":
            # DR-3 + NEW-DR3：先移入 .doctor-backup/residual/<ts>/（可回滚）；
            # 搬移失败显式标记失败并保留源（不强删）；含真实内容返回
            # (False, 带 error) → 调用方归入 manual_notice（不删、不发 stderr，
            # 避免读路径噪声）。
            backup_root = (Path(project_root) / ".orchd" / ".doctor-backup" /
                           "residual" / f"{int(time.time())}")
            return _dispose_residual_dir(path, backup_root)
        # DR-7 对齐（task-autoclean-recheck-align）：auto_clean 删除文件前同样
        # 复检 flock 活性，与 --fix 共用 _pre_delete_lock_recheck 单一事实源；
        # 跳过项以 (False, skipped) 返回，调用方归入 manual_notice（可见不静默）。
        skip_reason = _pre_delete_lock_recheck(path)
        if skip_reason is not None:
            return False, {
                **item, "skipped": "lock_held", "reason": skip_reason}
        _delete_file(path, project_root)
        return True, {**item, "disposition": "deleted"}
    except Exception as exc:
        return False, {**item, "error": f"{type(exc).__name__}: {exc}"}


def _auto_move_legacy(project_root: Path,
                      item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """把 flat 遗留状态文件移入 ``.orchd/.doctor-backup/legacy/<ts>/`` 滚动备份区。

    移动而非删除：早期 flat 布局的运行时文件可能仍含历史状态，保留供人工回滚。
    备份根按时间戳分桶，同名不冲突；移动失败返回 (False, 带 error)。
    """
    path = Path(item["path"])
    backup_root = (Path(project_root) / ".orchd" / ".doctor-backup" /
                   "legacy" / f"{int(time.time())}")
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / path.name
        if path.exists():
            shutil.move(str(path), str(dest))
        return True, {**item, "backup": str(dest), "disposition": "moved"}
    except Exception as exc:
        return False, {**item, "error": f"{type(exc).__name__}: {exc}"}


def auto_clean(project_root: Path,
               *,
               emit_stderr: bool = True) -> dict[str, Any]:
    """doctor 自动清洁入口（DR-7：破坏动作在账本锁内，best-effort 取锁）。

    读路径（status / watchdog）调用，故取锁策略与 ``doctor --fix`` 不同：**拿不到
    账本锁**（并发写动作进行中）时**不执行任何破坏动作**，仅返回 ``deferred=True``
    的报告结果——宁可不清理，也不冒「按旧快照误删活动状态」的 TOCTOU 风险。

    Returns:
        ``_auto_clean_impl`` 的结果；锁不可得时附 ``deferred=True``。
    """
    from orchd.ledger import Store

    # task-readonly-no-migrate：此外层 Store 仅作锁柄（acquire/release），
    # 不经它读写账本——只读构造，不触发建库 / 迁移 / 改名。
    # 契约（task-doctor-master-dir-contract）：入参是 **master 目录**；账本根与
    # 锁路径由 Store 内部 resolve_store_dir 派生，与旧写法逐字一致（等价性由
    # tests/test_doctor.py::TestStoreContractNormalization 钉住）。
    store = Store(Path(project_root) / ".orchd", readonly=True)
    locked = False
    try:
        store.acquire_lock()
        locked = True
    except Exception:
        locked = False
    try:
        if not locked:
            deferred = detect_residues(Path(project_root))
            return {
                "auto_cleaned": [],
                "auto_moved": [],
                "manual_notice": [{
                    **item, "disposition": "reported_only",
                    "deferred_reason": "ledger_lock_busy"
                } for item in deferred],
                "disabled":
                False,
                "deferred":
                True,
            }
        return _auto_clean_impl(project_root, emit_stderr=emit_stderr)
    finally:
        if locked:
            store.release_lock()


def _auto_clean_impl(project_root: Path,
                     *,
                     emit_stderr: bool = True) -> dict[str, Any]:
    """doctor 自动清洁实现（调用方须已持账本锁，见 :func:`auto_clean`）：读路径
    （status / watchdog）自动执行的低风险残留清理。

    分级判定与手动 ``doctor --fix`` 共享 ``detect_residues`` 单一事实源：
    - Auto-Clean 类型（orphan_session_lock / zombie_session / residual_dir /
      stale_worktree_registry）直接删/prune，无备份；
    - legacy_flat_residue 移入 ``.orchd/.doctor-backup/legacy/<ts>/`` 滚动备份区
      （可回滚）；
    - 高风险项（ghost_task / git_tracked_lock / intake_lock）仅计入
      ``manual_notice``，不自动执行。

    环境变量 ``ORCHD_AUTO_CLEAN`` ∈ {off, report} 时降级为只报告不执行
    （``disabled=True``）。

    stderr 留痕与开关契约（task-doctor-auto-clean 返工）：
    - ``emit_stderr=False`` 时完全静默（status/watchdog 挂载处按
      ``config.guidance_stderr`` 传入），结果仍并入返回 JSON 的 ``auto_clean`` 字段；
    - 仅对**真正发生**的 sanitize/move 动作（含失败留痕）写 ``[回收]`` stderr，
      ``manual_notice`` / ``reported_only`` 这类未做任何清理的项**不发** stderr，
      避免读路径噪声。
    全程 best-effort，单项失败不中断其余项，亦不抛异常。

    Args:
        project_root: canonical 项目根。
        emit_stderr: 是否写 ``orchd ▸ [回收]`` stderr（默认 True）。

    Returns:
        {
            "auto_cleaned": [...],   # 已自动清理的残留项
            "auto_moved": [...],     # 已移入备份区的残留项
            "manual_notice": [...],  # 仅报告不执行的项
            "disabled": bool,        # ORCHD_AUTO_CLEAN 关闭时为 True
        }
    """
    project_root = Path(project_root).resolve()
    mode = os.environ.get("ORCHD_AUTO_CLEAN", "").strip().lower()
    disabled = mode in ("off", "report")

    from orchd.worktree import _log_recycle, _recycle_actor

    actor = _recycle_actor()
    auto_cleaned: list[dict[str, Any]] = []
    auto_moved: list[dict[str, Any]] = []
    manual_notice: list[dict[str, Any]] = []

    # DR-13：全局 git worktree prune 每轮只执行一次（与 doctor --fix 同口径），
    # 本轮所有 prune 项共享结果，避免逐项调用导致重复执行与计数虚高。
    scanned = detect_residues(project_root)
    prune_failed: str | None = None
    if any(i.get("action") == "git_worktree_prune" for i in scanned):
        _prune = _run_git(project_root, ["worktree", "prune"])
        if _prune.returncode != 0:
            prune_failed = _prune.stderr.strip()

    for item in scanned:
        rtype = item.get("type")
        target = str(Path(item.get("path", "")))
        if disabled:
            # 关闭 / 只报告：不执行、不留痕，仅计数报告
            manual_notice.append({**item, "disposition": "reported_only"})
            continue
        if rtype in _AUTO_CLEAN_TYPES:
            if item.get("action") == "git_worktree_prune":
                # DR-13：prune 已在循环外执行一次，此处只登记结果
                if prune_failed is None:
                    auto_cleaned.append({
                        **item,
                        "disposition": "pruned",
                        "pruned_once": True,
                    })
                else:
                    manual_notice.append({
                        **item,
                        "disposition":
                        "manual",
                        "error":
                        f"git worktree prune 失败: {prune_failed}",
                    })
                continue
            ok, record = _auto_clean_item(project_root, item)
            if ok:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_clean",
                        "type": rtype,
                        "target": target,
                        "disposition": record.get("disposition"),
                        "reason": item.get("reason", ""),
                        "actor": actor,
                    }])
                auto_cleaned.append(record)
            else:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_clean_failed",
                        "type": rtype,
                        "target": target,
                        "error": record.get("error", ""),
                        "actor": actor,
                    }])
                manual_notice.append(record)
        elif rtype in _LEGACY_MOVE_TYPES:
            ok, record = _auto_move_legacy(project_root, item)
            if ok:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_move",
                        "type": rtype,
                        "target": target,
                        "disposition": "moved_to_backup",
                        "backup": record.get("backup"),
                        "reason": item.get("reason", ""),
                        "actor": actor,
                    }])
                auto_moved.append(record)
            else:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_move_failed",
                        "type": rtype,
                        "target": target,
                        "error": record.get("error", ""),
                        "actor": actor,
                    }])
                manual_notice.append(record)
        else:
            # ghost_task / git_tracked_lock / intake_lock 等高风险项：仅报告、
            # 零 stderr（未执行任何清理动作，读写路径均不发噪声）
            manual_notice.append({**item, "disposition": "manual"})

    # A1（task-hostfix-pack-b）：收尾顺带收敛备份保留期（best-effort；有删除才补字段）。
    _pruned = _prune_stale_backups(Path(project_root) / ".orchd" / ".doctor-backup")
    result = {
        "auto_cleaned": auto_cleaned,
        "auto_moved": auto_moved,
        "manual_notice": manual_notice,
        "disabled": disabled,
    }
    if _pruned:
        result["backups_pruned"] = _pruned
    return result
