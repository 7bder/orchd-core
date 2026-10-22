"""--fix 通道（doctor_fix/_doctor_fix_impl）：复检—处置—剪枝编排。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import shutil
import time

from orchd.doctor.residue import _MANUAL_ACTIONS, detect_residues
from orchd.doctor.runtime import _is_protected_path, _pre_delete_lock_recheck, _run_git
from .delete import _delete_file
from .dispose import _dispose_file_to_backup, _dispose_residual_dir
from .prune import _prune_stale_backups


def doctor_fix(
    project_root: Path,
    *,
    dry_run: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """执行残留清理（DR-7：破坏动作在**账本锁内**执行）。

    与并发 claim / done / amend 的账本写动作互斥，消除「按旧快照删掉刚建立的活动
    状态」的 TOCTOU（旧实现不持锁：活跃 session 心跳文件 / 刚被 claim 建立的锁可能
    在检测与删除之间变为活跃）。锁内执行对 retract_ghost 分支无害——底层
    ``ExclusiveFileLock`` 同进程可重入（共享 fd），``retract`` 内部再取锁不会自死锁。

    Args:
        project_root: 项目根目录。
        dry_run: True 时只输出待清理清单，不执行任何写操作。
        backup_dir: 删除前自动备份到此目录。None 时自动使用
            <project_root>/.orchd/.doctor-backup/<timestamp>/。

    Returns:
        {dry_run, backup_dir, detected, skipped_protected, skipped_manual,
         skipped_clean, cleaned, errors, summary}
    """
    from orchd.ledger import Store

    # task-readonly-no-migrate：此外层 Store 仅作锁柄（acquire/release），
    # 不经它读写账本——只读构造，不触发建库 / 迁移 / 改名。
    # 契约（task-doctor-master-dir-contract）：入参是 **master 目录**；账本根与
    # 锁路径由 Store 内部 resolve_store_dir 派生，与旧写法逐字一致（等价性由
    # tests/test_doctor.py::TestStoreContractNormalization 钉住）。
    store = Store(Path(project_root) / ".orchd", readonly=True)
    store.acquire_lock()
    try:
        return _doctor_fix_impl(project_root,
                                dry_run=dry_run,
                                backup_dir=backup_dir)
    finally:
        store.release_lock()


def _doctor_fix_impl(
    project_root: Path,
    *,
    dry_run: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """残留清理实现（调用方须已持账本锁，见 :func:`doctor_fix`）。

    Args:
        project_root: 项目根目录。
        dry_run: True 时只输出待清理清单，不执行任何写操作。
        backup_dir: 删除前自动备份到此目录。None 时自动使用
            <project_root>/.orchd/.doctor-backup/<timestamp>/。

    Returns:
        {
            "dry_run": bool,
            "backup_dir": str | None,
            "detected": [...],   # detect_residues 原始输出
            "skipped_protected": [...],  # 被白名单保护的路径
            "skipped_manual": [...],     # 无自动清理通道、需人工处置的项
            "skipped_clean": [...],  # 复检跳过未删的项（本轮未删，不计入 cleaned）
            "cleaned": [...],    # 已清理的项（dry_run=True 时为 []）
            "errors": [...],     # 清理失败的项
            "summary": str,
        }
    """
    detected = detect_residues(project_root)
    skipped_protected: list[dict[str, Any]] = []
    to_clean: list[dict[str, Any]] = []

    # 白名单过滤：绝不触碰核心状态文件
    for item in detected:
        path = Path(item["path"])
        if _is_protected_path(path, Path(project_root)):
            skipped_protected.append({
                **item,
                "reason":
                item.get("reason", "") + " [SKILLED: 保护白名单拦截]",
            })
        else:
            to_clean.append(item)

    # 不可自动处置项单列（事件全为 FORCE_STATUS 的幽灵任务 / 含真实内容的残留目录），
    # 避免 --fix 报伪失败；判据为恒人工处置动作集合（单一事实源 _MANUAL_ACTIONS）
    skipped_manual: list[dict[str, Any]] = [
        i for i in to_clean if i.get("action") in _MANUAL_ACTIONS
    ]
    if skipped_manual:
        to_clean = [
            i for i in to_clean if i.get("action") not in _MANUAL_ACTIONS
        ]

    # dry-run 模式：只报告不执行
    if dry_run:
        return {
            "dry_run":
            True,
            "backup_dir":
            None,
            "detected":
            detected,
            "skipped_protected":
            skipped_protected,
            "skipped_manual":
            skipped_manual,
            "skipped_clean": [],
            "cleaned": [],
            "errors": [],
            "summary": (f"[dry-run] 发现 {len(detected)} 项残留"
                        f"（{len(skipped_protected)} 项被白名单保护跳过，"
                        f"{len(skipped_manual)} 项需人工处置，"
                        f"{len(to_clean)} 项可清理）。"
                        f"使用 --fix 执行实际清理。"),
        }

    # 执行模式：先备份再删除（dry-run 已在上面提前返回 → 此处必为具体路径）
    if backup_dir is None:
        backup_path: Path = (Path(project_root) / ".orchd" / ".doctor-backup" /
                             f"{int(time.time())}")
    else:
        backup_path = Path(backup_dir)

    cleaned: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    # task-doctor-fix-cleaned-bucket：复检跳过未删单列——lock_held 与复检异常
    # 同走 _pre_delete_lock_recheck 文案，均入此桶不再进 cleaned（未删除不得计
    # 入已清理）。already_absent（幽灵零写幂等）保留在 cleaned：零操作成功态，
    # 非"未删"，不在本卡范围。
    skipped_clean: list[dict[str, Any]] = []
    untracked: list[str] = []  # 已 git rm --cached 但未提交（半修，需 commit 收口）

    # DR-13：全局 git worktree prune 每轮只执行**一次**（prune 本身是全局动作，
    # 旧实现 N 个 stale 注册项各跑一次：重复执行浪费 + cleaned 计数虚高）。
    # 本轮所有 git_worktree_prune 项共享此结果。
    prune_failed: str | None = None
    if any(i.get("action") == "git_worktree_prune" for i in to_clean):
        _prune = _run_git(project_root, ["worktree", "prune"])
        if _prune.returncode != 0:
            prune_failed = _prune.stderr.strip()

    for item in to_clean:
        path = Path(item["path"])
        action = item.get("action", "delete")

        # 幽灵任务清理（task-runtime-hygiene AC1/AC5）：不走文件删除路径，改用
        # 引擎保留的 admin 控制面撤回其在账本中的孤儿事件，使 replay 与 master
        # 一致（force-status / retract 常规通道对不在 _master.json 的 id 不可用，
        # 故此处是该类残留的专用处置通道）。
        if action == "retract_ghost":
            try:
                from orchd.ledger import Store
                from orchd.onboard import retract as _retract

                # 契约（task-doctor-master-dir-contract）：入参是 master 目录；
                # retract 内部对 store.orchd_dir 的消费一律按 master 目录语义
                # （load_bindings / 会话身份等再各自 resolve_store_dir）。
                ghost_store = Store(Path(project_root) / ".orchd")
                ghost_task_id = item.get("task_id")
                # DR-6 幂等守卫（前置）：任务已不在 replay 派生状态中（先前 --fix 已
                # 撤回 / 账本侧已无活跃事件）→ 零写操作，绝不重复追加 RETRACT。
                if ghost_task_id and ghost_task_id not in ghost_store.replay():
                    cleaned.append({
                        **item,
                        "retracted": False,
                        "skipped": "already_absent",
                        "backup": None,
                    })
                    continue
                ghost_res = _retract(
                    ghost_store,
                    "admin",
                    target_event_id=item.get("event_id"),
                    reason=(f"幽灵任务清理（doctor --fix）："
                            f"{item.get('task_id')} 不在 _master.json"),
                    project_root=Path(project_root),
                )
                # DR-6 幂等守卫（后验）：撤回后目标任务必须已从 replay 派生状态消失；
                # 若仍在（例如链上仍有不可撤事件），**不再重复追加** RETRACT，降级为
                # 需人工处置项（manual 档），避免每次 --fix 反复写账本。
                if ghost_task_id and ghost_task_id in ghost_store.replay():
                    errors.append({
                        **item,
                        "action":
                        "ghost_task_manual",
                        "error": ("retract_incomplete: 撤回后任务仍在 replay 派生状态中"
                                  "（疑似链上仍有不可撤事件）——已停止重复撤回，转人工处置"),
                    })
                    continue
                cleaned.append({
                    **item,
                    "retracted":
                    bool(ghost_res.get("retracted")),
                    "backup":
                    None,
                })
            except Exception as exc:  # 清理失败不中断其余项
                errors.append({
                    **item,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            continue

        if action == "git_worktree_prune":
            # DR-13：prune 已在循环外执行一次，此处只登记结果（不再逐项调用）
            if prune_failed is None:
                cleaned.append({**item, "backup": None, "pruned_once": True})
            else:
                errors.append({
                    **item,
                    "error":
                    f"git worktree prune 失败: {prune_failed}",
                })
            continue

        if action == "delete_dir":
            # 残留任务 worktree 目录：先移入 .doctor-backup/residual/<ts>/（可回滚）；
            # 搬移失败显式标记失败并保留源（NEW-DR3，不强删）；含真实内容一律不删（DR-3）。
            ok, record = _dispose_residual_dir(path, backup_path / "residual")
            if ok:
                cleaned.append({**item, **record})
            else:
                # 检出期为可清、执行期发现内容（竞态 / 判定漂移）→ 不删且失败可见
                errors.append({**item, **record})
            continue

        if action == "move_to_backup":
            # 陈旧提案草稿：整文件搬入既有备份桶（保持相对路径），源消失、内容
            # 逐字节可回滚。此处必须显式分支——未知 action 会落到下面的通用删除
            # 分支（copy2 + _safe_delete），草稿将失去「只有一份副本」的保证并可能
            # 降级进系统 temp（见 _dispose_file_to_backup docstring）。
            ok, record = _dispose_file_to_backup(path, backup_path,
                                                 Path(project_root))
            if ok:
                cleaned.append({**item, **record})
            else:
                errors.append({**item, **record})
            continue

        try:
            # DR-7 单一事实源（_pre_delete_lock_recheck）：--fix 与 auto_clean
            # 删除前共用同一 flock 复检，堵检测→执行并发窗口。
            skip_reason = _pre_delete_lock_recheck(path)
            if skip_reason is not None:
                skipped_clean.append({
                    **item,
                    "skipped": "lock_held",
                    "backup": None,
                    "reason": skip_reason,
                })
                continue
            needs_commit = action == "git_rm_cached_then_delete"
            if needs_commit:
                # 先从 git 移除追踪，再删除文件。untrack 与删除之间是**未提交的
                # 半修状态**，须后续 commit 收口 → 结果项与摘要显式提示（DR-11）。
                git_result = _run_git(
                    project_root,
                    ["rm", "--cached", str(path)])
                if git_result.returncode != 0:
                    errors.append({
                        **item,
                        "error":
                        f"git rm --cached 失败: {git_result.stderr.strip()}",
                    })
                    continue

            # 备份（仅当文件存在时）
            if path.exists() and backup_path is not None:
                backup_path.mkdir(parents=True, exist_ok=True)
                # 保持相对路径结构，避免同名文件冲突
                try:
                    rel = path.relative_to(Path(project_root))
                    dest = backup_path / rel
                except ValueError:
                    dest = backup_path / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(path), str(dest))

            # 执行删除（DR-10：统一走 _delete_file → _safe_delete；裸 unlink 在沙箱
            # 下被劫持为「移入回收站」，回收站不可用时 FAIL_CLOSED 致清理静默失败）
            _delete_file(path, Path(project_root))

            record = {
                **item,
                "backup": str(backup_path) if backup_path else None,
            }
            if needs_commit:
                record["follow_up"] = "need_commit_after_untrack"
                record["hint"] = ("已从 git 索引移除（git rm --cached）但未提交：属半修状态，"
                                  "须 git commit 收口——否则该路径持续呈现「已删除未提交」")
                untracked.append(str(path))
            cleaned.append(record)
        except OSError as exc:
            errors.append({
                **item,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # A1（task-hostfix-pack-b）：收尾顺带收敛备份保留期（best-effort）。
    _pruned = _prune_stale_backups(Path(project_root) / ".orchd" / ".doctor-backup")
    result: dict[str, Any] = {
        "dry_run":
        False,
        "backup_dir":
        str(backup_path) if backup_path else None,
        "detected":
        detected,
        "skipped_protected":
        skipped_protected,
        "skipped_manual":
        skipped_manual,
        "skipped_clean":
        skipped_clean,
        "cleaned":
        cleaned,
        "errors":
        errors,
        "summary":
        (f"清理完成：{len(cleaned)} 项已清理"
         f"（{len(skipped_protected)} 项被白名单保护跳过，"
         f"{len(skipped_manual)} 项需人工处置，"
         f"{len(skipped_clean)} 项复检跳过未删，"
         f"{len(errors)} 项失败）。"
         f"备份目录：{backup_path}" +
         (f"；{len(untracked)} 项已从 git 索引移除但未提交，"
          f"需 git commit 收口：{', '.join(untracked)}" if untracked else "") +
         (f"；过期备份已清理 {len(_pruned)} 项" if _pruned else "")),
    }
    if _pruned:
        result["backups_pruned"] = _pruned
    return result
