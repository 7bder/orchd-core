"""自动清理通道（_auto_clean_item/_auto_move_legacy/auto_clean/_auto_clean_impl）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import time

from orchd.constants import ORCHD_AUTO_CLEAN_ENV
from orchd.doctor.residue import _AUTO_CLEAN_TYPES, _LEGACY_MOVE_TYPES, detect_residues
from orchd.doctor.runtime import (
    _pre_delete_lock_recheck, _resolve_runtime_dir, _run_git,
)
from .delete import _delete_file
from .dispose import _dispose_residual_dir
from .prune import _prune_stale_backups

# §6.3 触发点上限唯一真源：触发名 → (最大项数, 时间预算秒)；超限转 deferred_items。
AUTO_CLEAN_TRIGGER_LIMITS: dict[str, tuple[int, float]] = {
    "status": (20, 2.0),
    "watchdog": (100, 10.0),
    "session": (10, 2.0),
    "done": (10, 2.0),
    "merge": (10, 2.0),
}


def _normalize_path(value: str | Path) -> str:
    """路径归一化（resolve + normcase），用于 ``exclude_paths`` 比对。"""
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except OSError:  # pragma: no cover - 极端路径
        return os.path.normcase(str(value))


def _auto_clean_scope_matcher(
    task_id: str | None,
    session_id: str | None,
    roots: Any = None,
) -> Any:
    """局部触发点范围判定（本会话 / 本任务）。

    两条腿：路径在给定根之下（前缀带 ``os.sep``，防 ``task-a`` 命中 ``task-a2``）；
    或 id 按 **token 边界**出现在 path/reason/task_id/session_id（防前缀串味）。
    判定不到即交给 status/watchdog 全量轮次。
    """
    needles = [n for n in (task_id, session_id) if n]
    root_prefixes = [
        os.path.normcase(str(Path(r).resolve())) + os.sep
        for r in (roots or []) if r
    ]
    if not needles and not root_prefixes:
        return None
    patterns = []
    for needle in needles:
        if all(ch in "0123456789abcdefABCDEF" for ch in needle):
            # 会话 id（hex）：前后不得再接 hex 字符
            patterns.append(re.compile(
                rf"(?<![0-9a-fA-F]){re.escape(needle)}(?![0-9a-fA-F])"))
        else:
            # 任务 id（可含 -/_）：前后不得再接标识符字符，避免前缀串味
            patterns.append(re.compile(
                rf"(?<![A-Za-z0-9_-]){re.escape(needle)}(?![A-Za-z0-9_-])"))

    def _match(item: dict[str, Any]) -> bool:
        raw_path = str(item.get("path") or "")
        if root_prefixes:
            normalized = _normalize_path(raw_path)
            if any(normalized.startswith(prefix) for prefix in root_prefixes):
                return True
        hay = " ".join(
            str(item.get(key) or "")
            for key in ("path", "reason", "task_id", "session_id")
        )
        return any(p.search(hay) for p in patterns)

    return _match


def _auto_clean_item(project_root: Path,
                     item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """执行单个 Auto-Clean 残留的清理（best-effort，失败返回 (False, 带 error)）。

    删除动作与 ``doctor --fix`` 同源：文件删除走 :func:`_delete_file`
    （``_safe_delete``，DR-10）；残留目录走 :func:`_dispose_residual_dir`
    （先移入 ``.doctor-backup/residual/<ts>/``，含真实内容则拒绝处置，DR-3）；
    野账本（``move_to_backup``）走 :func:`_recycle_stray_ledger` 回收桶。
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
        if action == "move_to_backup":
            # 空壳/野账本：移入 <store_root>/.recycle-journal/stray-ledger/<ts>/
            # （可回滚，只搬不删；契约见 _recycle_stray_ledger）。
            return _recycle_stray_ledger(project_root, item)
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


def _recycle_stray_ledger(project_root: Path,
                          item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """把单个空壳/野账本搬入 ``<store_root>/.recycle-journal/stray-ledger/<ts>/``。

    与 ``--fix`` 的 ``move_to_backup`` 同语义：整文件移动（不双写），可逐字节回滚。
    回收桶在 git 工作树之外（store_root 与账本根同源），落点保持相对 ``project_root``
    结构、越界时退化为文件名；同名用 ``O_CREAT|O_EXCL`` 占位递增后缀，**绝不覆盖桶内
    已有文件**。每桶写 ``manifest.json``（sha256 / size / rollback 等）；同轮共用秒级
    桶（持锁单进程，无并发写同桶竞态）。
    """
    def _sha256(target: Path) -> str:
        digest = hashlib.sha256()
        with open(target, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _append_manifest(bucket: Path, entry: dict[str, Any]) -> None:
        """追加一条回收记录（读-改-写 + 原子 ``os.replace``；坏 manifest 重建）。"""
        manifest = bucket / "manifest.json"
        entries: list[dict[str, Any]] = []
        if manifest.is_file():
            try:
                loaded = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    entries = loaded
            except (OSError, ValueError):
                entries = []
        entries.append(entry)
        tmp = manifest.with_name(".manifest.json.tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        os.replace(tmp, manifest)

    path = Path(item["path"])
    bucket = (_resolve_runtime_dir(Path(project_root) / ".orchd") /
              ".recycle-journal" / "stray-ledger" /
              time.strftime("%Y%m%d-%H%M%S"))
    try:
        sha256 = _sha256(path)
        size = path.stat().st_size
        try:
            rel = path.relative_to(project_root)
        except ValueError:
            rel = Path(path.name)
        bucket.mkdir(parents=True, exist_ok=True)

        def _reserve_dest() -> Path:
            """桶内原子占位（``O_CREAT|O_EXCL``），返回唯一可写落点。

            同名冲突时递增 ``-1`` / ``-2`` 后缀；占位成功即锁住该名字，随后的
            ``shutil.move`` 只会覆盖**本函数刚创建的空占位文件**——不会覆盖桶内
            既有回收文件（多根同名野账本的 10B/20B/30B 逐份保真，见 S1 回归）。
            """
            base = bucket / rel
            base.parent.mkdir(parents=True, exist_ok=True)
            for index in range(1000):
                candidate = (base if index == 0
                             else base.with_name(f"{base.name}-{index}"))
                try:
                    os.close(os.open(candidate, os.O_CREAT | os.O_EXCL |
                                     os.O_WRONLY))
                except FileExistsError:
                    continue
                return candidate
            raise RuntimeError(f"回收桶内无法为 {rel} 分配唯一落点")

        dest = _reserve_dest()
        try:
            shutil.move(str(path), str(dest))
        except Exception:
            dest.unlink(missing_ok=True)  # 释放占位；源仍在，交给上层 move_failed
            raise
        entry = {
            "original_path": str(path),
            "recycled_to": str(dest),
            "relative_path": str(rel),
            "sha256": sha256,
            "size": size,
            "recycled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "rollback": f"把 {dest} 搬回 {path} 即恢复（sha256 校验内容未变）",
        }
        record: dict[str, Any] = {
            **item,
            "backup": str(dest),
            "disposition": "recycled",
            "sha256": sha256,
            "size": size,
        }
        try:
            _append_manifest(bucket, entry)
        except OSError as exc:
            # 内容已安全落桶；manifest 写失败不撤回搬移，但显式留痕（可人工按
            # 目录内容对账），绝不因此强删或把源文件搬回（避免二次故障窗口）。
            record["manifest_error"] = f"{type(exc).__name__}: {exc}"
        return True, record
    except Exception as exc:  # best-effort：失败保留源，绝不强删
        return False, {
            **item,
            "error": f"{type(exc).__name__}: {exc}",
            "move_failed": True,
            "source_preserved": path.exists(),
        }


def auto_clean(project_root: Path,
               *,
               emit_stderr: bool = True,
               trigger: str = "status",
               task_id: str | None = None,
               session_id: str | None = None,
               limit: int | None = None,
               time_budget: float | None = None,
               exclude_paths: Any = None,
               scope_roots: Any = None) -> dict[str, Any]:
    """doctor 自动清洁入口（DR-7：破坏动作在账本锁内，best-effort 取锁）。

    读路径与局部触发点共用；**拿不到账本锁**时不执行任何破坏动作，仅返回
    ``deferred=True`` 的报告（宁可不清，也不冒按旧快照误删的 TOCTOU 风险）。

    Args:
        project_root: canonical 项目根。
        emit_stderr: 是否写 ``orchd ▸ [回收]`` stderr。
        trigger: 触发点名，决定默认上限（:data:`AUTO_CLEAN_TRIGGER_LIMITS`）。
        task_id / session_id: 局部触发点范围（``None`` 不限）；limit / time_budget
        覆盖上限；exclude_paths 本轮不得处置；scope_roots 路径前缀属本任务范围。

    Returns:
        ``_auto_clean_impl`` 的结果；锁不可得时附 ``deferred=True``。
    """
    from orchd.ledger import Store

    cap_items, cap_seconds = AUTO_CLEAN_TRIGGER_LIMITS.get(trigger, (None, None))
    if limit is not None:
        cap_items = limit
    if time_budget is not None:
        cap_seconds = time_budget
    matcher = _auto_clean_scope_matcher(task_id, session_id, scope_roots)
    excluded = ({_normalize_path(p) for p in exclude_paths}
                if exclude_paths else None)

    # task-readonly-no-migrate：外层 Store 仅作锁柄，只读构造不触发建库/迁移；
    # 入参为 master 目录，账本根与锁路径由 Store 内部派生（等价性见
    # tests/test_doctor.py::TestStoreContractNormalization）。
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
            if matcher is not None:
                deferred = [item for item in deferred if matcher(item)]
            if excluded:
                deferred = [
                    item for item in deferred
                    if _normalize_path(item.get("path", "")) not in excluded
                ]
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
                "trigger":
                trigger,
            }
        return _auto_clean_impl(
            project_root,
            emit_stderr=emit_stderr,
            trigger=trigger,
            matcher=matcher,
            limit=cap_items,
            budget=cap_seconds,
            excluded=excluded,
        )
    finally:
        if locked:
            store.release_lock()


def _auto_clean_impl(project_root: Path,
                     *,
                     emit_stderr: bool = True,
                     trigger: str = "status",
                     matcher: Any = None,
                     limit: int | None = None,
                     budget: float | None = None,
                     excluded: Any = None) -> dict[str, Any]:
    """doctor 自动清洁实现（调用方须已持账本锁，见 :func:`auto_clean`）。

    分级判定与 ``doctor --fix`` 共享 ``detect_residues`` 单一事实源：Auto-Clean
    类型直接删/prune；stray_ledger 进 ``.recycle-journal`` 回收桶、
    legacy_flat_residue 进 ``.doctor-backup/legacy``（均可回滚）；高风险项
    （ghost_task / git_tracked_lock / intake_lock / stale_rescue_ref）只进
    ``manual_notice``——本通道零判据：各类型候选条件（verify tmp 活跃引用豁免 /
    review/ 已合并满 7 天 / runtime 日志 30 天 + keep-10 / 未注册草案 14 天归档闸）
    单一真源在 ``orchd/doctor/continuity.py``，执行面只按 type/action 分级处置。
    ``ORCHD_AUTO_CLEAN`` ∈ {off, report} → 只报告；stderr 仅对真正发生的动作
    写出。全程 best-effort，单项失败不中断、不抛异常。

    Returns:
        ``auto_cleaned`` / ``auto_moved`` / ``manual_notice`` / ``disabled`` /
        ``trigger`` / ``deferred``（锁忙或超上限）/ ``deferred_items``。
    """
    project_root = Path(project_root).resolve()
    mode = os.environ.get(ORCHD_AUTO_CLEAN_ENV, "").strip().lower()
    disabled = mode in ("off", "report")

    from orchd.worktree import _log_recycle, _recycle_actor

    actor = _recycle_actor()
    auto_cleaned: list[dict[str, Any]] = []
    auto_moved: list[dict[str, Any]] = []
    manual_notice: list[dict[str, Any]] = []
    deferred_items: list[dict[str, Any]] = []

    # DR-13：全局 git worktree prune 每轮只执行一次（与 doctor --fix 同口径），
    # 本轮所有 prune 项共享结果，避免逐项调用导致重复执行与计数虚高。
    scanned = detect_residues(project_root)
    if matcher is not None:
        # 局部触发点（session/done/merge）：只保留本会话/本任务相关项；范围外条目
        # 不属于本轮职责，交给 status/watchdog 的全量轮次（不报告、不删除）。
        scanned = [item for item in scanned if matcher(item)]
    if excluded:
        # 局部触发点自己刚写下的审计件（如 session end 的 runtime json）不得被
        # 本轮当成 zombie 清掉——否则 force_bypass 等留痕当场丢失。
        scanned = [
            item for item in scanned
            if _normalize_path(item.get("path", "")) not in excluded
        ]
    prune_failed: str | None = None
    if any(i.get("action") == "git_worktree_prune" for i in scanned):
        _prune = _run_git(project_root, ["worktree", "prune"])
        if _prune.returncode != 0:
            prune_failed = _prune.stderr.strip()

    started = time.monotonic()
    attempted = 0
    for item in scanned:
        rtype = item.get("type")
        target = str(Path(item.get("path", "")))
        if not disabled:
            # §6.3 每轮上限：项数/时间任一超限即将本项及后续项转 deferred_items。
            over_items = limit is not None and attempted >= limit
            # 用尽即停（>=）：Windows monotonic 粒度较粗，`>` 在预算恰为 0 时可能判不出
            over_time = (budget is not None
                         and (time.monotonic() - started) >= budget)
            if over_items or over_time:
                deferred_items.append({
                    **item,
                    "disposition": "deferred",
                    "deferred_reason":
                    "iteration_cap" if over_items else "time_budget",
                })
                continue
            attempted += 1
        if disabled:
            # 关闭 / 只报告：不执行、不留痕，仅计数报告（报告轮不计上限）
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
        "trigger": trigger,
        "deferred": bool(deferred_items),
    }
    if deferred_items:
        result["deferred_items"] = deferred_items
    if _pruned:
        result["backups_pruned"] = _pruned
    return result
