"""worktree recycle steps 域（W6-C5 纯搬移）。

来源：``orchd/worktree/recycle.py``（1023 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.worktree.bindings import (
    _TASK_WT_PREFIX, _task_wt_name, load_bindings, unbind_task_wt,
)
from orchd.worktree.layout import _GIT_TIMEOUT, detect_layout
from .cleanup import (
    _DIRTY_REFUSAL_MARKER,
    _backup_root,
    _l2_backup_ignored_files,
    _l2_rescue_commit,
    _l3_isolate_dir,
    _l4_mirror_files,
    _ladder_timestamp,
    _wt_dirty_files,
    _wt_dirty_snapshot,
)


def _l4_bundle_patch(
    wt_path: Path,
    backup_root: Path,
    task_id: str,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """L4：bundle（全部引用的提交历史）+ patch（已跟踪改动 diff）+ 状态清单
    + ``files/`` 镜像 + ``manifest.json``（SC1 零丢失）。

    bundle、patch、status 与文件镜像任一失败都记 errors 且 ``ok=False``（每一种
    脏数据必须有可恢复副本）；调用方不得 rmtree，继续 L5 原地保留。
    """
    backup_dir = backup_root / f"{task_id}-{_ladder_timestamp()}"
    while backup_dir.exists():
        backup_dir = backup_dir.with_name(
            f"{backup_dir.name}-{os.getpid() % 10000:04d}")
    errors: list[str] = []
    bundle_path: str | None = None
    patch_path: str | None = None
    status_path: str | None = None
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "dir": None,
                "error": f"{type(exc).__name__}: {exc}"[:300]}
    bundle_target = backup_dir / "rescue.bundle"
    try:
        proc = subprocess.run(
            ["git", "-C", str(wt_path), "bundle", "create", str(bundle_target),
             "--all"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if proc.returncode == 0:
            bundle_path = str(bundle_target)
        else:
            errors.append(((proc.stderr or "") or (proc.stdout or "")).strip()[:300])
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}"[:300])
    try:
        diff = subprocess.run(
            ["git", "-C", str(wt_path), "diff", "HEAD"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if diff.returncode == 0:
            patch_target = backup_dir / "rescue.patch"
            patch_target.write_text(diff.stdout or "", encoding="utf-8")
            patch_path = str(patch_target)
        else:
            errors.append(
                "patch_unavailable: "
                + (((diff.stderr or "") or (diff.stdout or "")).strip()[:300]))
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}"[:300])
    try:
        st = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain=v1",
             "--ignored=matching", "--untracked-files=all"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if st.returncode == 0:
            status_target = backup_dir / "rescue-status.txt"
            status_target.write_text(st.stdout or "", encoding="utf-8")
            status_path = str(status_target)
        else:
            errors.append(
                "status_unavailable: "
                + (((st.stderr or "") or (st.stdout or "")).strip()[:300]))
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}"[:300])

    if snapshot is None:
        snapshot = _wt_dirty_snapshot(wt_path)
    mirror: dict[str, Any]
    if snapshot is None:
        # 快照不可得 = 无法判定用户文件 → 镜像失败，禁止 rmtree。
        mirror = {
            "ok": False,
            "files_dir": None,
            "manifest": None,
            "entries": [],
            "errors": ["dirty_snapshot_unavailable"],
        }
    else:
        mirror_rel = list(snapshot.get("untracked") or []) + list(
            snapshot.get("user_ignored") or [])
        mirror = _l4_mirror_files(wt_path, backup_dir, mirror_rel)
    errors.extend(mirror.get("errors") or [])

    return {
        "ok": (bundle_path is not None and patch_path is not None
           and status_path is not None and bool(mirror["ok"])),
        "dir": str(backup_dir),
        "bundle": bundle_path,
        "patch": patch_path,
        "status_file": status_path,
        "files_dir": mirror.get("files_dir"),
        "manifest": mirror.get("manifest"),
        "mirrored_files": len(mirror.get("entries") or []),
        "errors": errors,
    }



def _classify_remove_failure(err_text: str | None, engine_cwd_inside: bool) -> str | None:
    """worktree 移除失败归因（task-review-submit-mainwt-guidance）。

    ``Permission denied`` 且引擎自身 cwd 当时不在待删目录内 → 持有者极可能是
    调用方 shell 的 cwd（协议要求认领/审查在任务分支执行，review 提交时调用方
    常仍站在任务目录内，Windows 下删 cwd 确定性失败）→ ``cwd_held_by_caller``。
    引擎自身曾在目录内（已自愈仍失败）则归外部句柄（杀毒/索引），返回 None
    走通用失败通道。

    Returns:
        ``"cwd_held_by_caller"`` 或 None（通用失败）。
    """
    if "permission denied" in (err_text or "").lower() and not engine_cwd_inside:
        return "cwd_held_by_caller"
    return None



def _tx_step_remove_worktree(
    stable_wt: Path | None,
    wt_path: Path,
    status: str | None,
    actor: str,
    task_id: str,
    recycle_log: list[dict[str, Any]],
    *,
    discard_uncommitted: bool = False,
    discard_confirmed: bool = False,
) -> dict[str, Any]:
    """事务第 1 步：worktree 移除（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：cwd 自愈 → ``git worktree
    remove`` / 缺席时 prune 陈旧登记 → 残留空壳清理。幂等：目录本就不存在 →
    prune 后视为已回收。

    五级降级（task-cont-dirty-worktree-ladder）：脏 worktree 不再「脏即
    --force」——L1 干净/仅 ignored 缓存普通 remove；脏（tracked/untracked 或有
    ignored 用户文件）走 L2 救援提交（数据保全进 ``refs/heads/orchd/rescue/
    <task>-<UTC>``）并把 ignored 用户文件复制到
    ``<backup_root>/<task>-<UTC>-l2-files/`` 后再移除；复制失败不得 force
    remove，降级 L3 整目录隔离到 ``.orchd/.doctor-backup/worktrees/<task>-<UTC>/``；
    移动失败 L4 bundle+patch+``files/`` 镜像+``manifest.json`` 备份后重试清理；
    镜像失败同样不删，仍失败 L5 原地保留 + manual notice。仅
    ``discard_uncommitted`` 且 ``discard_confirmed`` 双旗标齐备才允许显式
    force 丢弃（``discarded_uncommitted`` 语义 = 数据未保全即销毁）。
    """
    removed = False
    discarded_uncommitted = False
    # task-recycle-observability：丢弃/救援前快照的路径清单（None=未走到或取不到）。
    _remove_record_discarded: list[str] | None = None
    wt_existed = (wt_path / ".git").exists()
    # task-worktree-recycle-cwd-selfheal（AC1）：回收前检测调用方进程 cwd 是否
    # 位于待回收 worktree 内。Windows 下进程 cwd 会锁定目录，导致 git worktree
    # remove 失败（已知原因却不自愈）。若命中则 os.chdir 到主工作树（stable_wt），
    # 释放目录锁后再执行 remove；幂等：cwd 不在 worktree 内时零操作。
    # W-7：os.chdir 是进程级副作用——记录调用方原 cwd，移除阶段 finally 恢复。
    cwd_self_healed = False
    _original_cwd: Path | None = None
    try:
        _cur_cwd = Path.cwd().resolve()
        _original_cwd = _cur_cwd
        _wt_resolved = wt_path.resolve()
        # 与 guard_task_root 同口径（task-worktree-residual-self-heal）：双侧
        # normcase，Windows 路径大小写不一致时不再漏判自愈。
        _cwd_inside = (
            os.path.normcase(str(_cur_cwd)) == os.path.normcase(str(_wt_resolved))
            or str(os.path.normcase(str(_cur_cwd))).startswith(
                str(os.path.normcase(str(_wt_resolved))) + os.sep)
        )
    except OSError:
        _cwd_inside = False
    if _cwd_inside and stable_wt and stable_wt.exists():
        try:
            os.chdir(str(stable_wt))
            cwd_self_healed = True
        except OSError:
            cwd_self_healed = False
    if cwd_self_healed:
        recycle_log.append({
            "action": "cwd_self_heal",
            "task_id": task_id,
            "from": str(_cur_cwd),
            "to": str(stable_wt),
            "actor": actor,
        })
    remove_error: str | None = None
    _remove_reason: str | None = None
    registry_pruned: bool | None = None
    _dirty_snapshot: dict[str, Any] | None = None
    # 五级降级状态（task-cont-dirty-worktree-ladder）：默认 L1；触梯后记录
    # 最终落点与救援产物，供 remove_task_wt 透出（rescue_ref / isolated_to 等）。
    ladder_level = "L1"
    rescue_ref: str | None = None
    isolated_to: str | None = None
    rescue_backup_dir: str | None = None
    discard_authorized = bool(discard_uncommitted and discard_confirmed)
    try:
        if wt_existed:
            # SC1：先取含 ignored 的快照。ignored 用户文件不会让 git 拒绝
            # 删除，普通 remove 会静默删掉它们——判脏必须前置，不能等 remove
            # 失败才发现。仅缓存 ignored 不判脏，仍走 L1。
            _dirty_snapshot = _wt_dirty_snapshot(wt_path)
            _pre_dirty = bool(_dirty_snapshot and _dirty_snapshot["dirty"])
            if _pre_dirty and not discard_authorized:
                proc = subprocess.CompletedProcess(
                    args=["git", "worktree", "remove", str(wt_path)],
                    returncode=1,
                    stdout="",
                    stderr=f"fatal: pre-checked {_DIRTY_REFUSAL_MARKER} files",
                )
            else:
                # P2-9：先无 --force 移除（仅干净 worktree 可移，避免丢弃未提交改动）。
                proc = subprocess.run(
                    ["git", "worktree", "remove", str(wt_path)],
                    cwd=str(stable_wt),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
            if proc.returncode != 0:
                # 五级降级归因：porcelain 快照（权威）+ stderr 脏标记（status
                # 不可得时的兜底）；两者任一判定为脏才进梯——句柄占用/权限/
                # 未知原因一律不进梯（数据零风险，走通用失败通道）。
                _err = (proc.stderr or "").lower()
                if _dirty_snapshot is None:
                    _dirty_snapshot = _wt_dirty_snapshot(wt_path)
                _dirty_refusal = _DIRTY_REFUSAL_MARKER in _err
                _is_dirty = bool(
                    _dirty_snapshot and _dirty_snapshot["dirty"]
                ) or _dirty_refusal
                if not _is_dirty:
                    remove_error = (proc.stderr or proc.stdout or "").strip()[:300]
                    _remove_reason = _classify_remove_failure(
                        proc.stderr or proc.stdout or "", _cwd_inside)
                else:
                    _remove_record_discarded = (
                        list(_dirty_snapshot["files"])
                        if _dirty_snapshot is not None else None)
                    if discard_authorized:
                        # 显式丢弃出口（--discard-uncommitted --confirm 双旗标）：
                        # 操作者已明确授权销毁未提交改动，快照留痕后 --force。
                        proc = subprocess.run(
                            ["git", "worktree", "remove", "--force", str(wt_path)],
                            cwd=str(stable_wt),
                            capture_output=True, encoding="utf-8",
                            errors="replace", timeout=30,
                        )
                        discarded_uncommitted = proc.returncode == 0
                        recycle_log.append({
                            "action": "ladder_explicit_discard",
                            "task_id": task_id,
                            "level": "L1",
                            "discarded": discarded_uncommitted,
                            "files": _remove_record_discarded,
                            "actor": actor,
                        })
                    if not discard_authorized or not discarded_uncommitted:
                        # L2：救援提交（显式丢弃路径 force 失败时同样救援——
                        # 「授权销毁」不等于「必须销毁」，保全严格更安全）。
                        assert stable_wt is not None  # remove.py 守卫已拒绝 None
                        _rescue = _l2_rescue_commit(
                            stable_wt, wt_path, task_id, recycle_log)
                        _l2_files: dict[str, Any] = {
                            "ok": True, "dir": None, "copied": [], "failed": [],
                            "error": None,
                        }
                        if _rescue["ok"]:
                            rescue_ref = _rescue["ref"]
                            recycle_log.append({
                                "action": "ladder_rescue",
                                "task_id": task_id,
                                "level": "L2",
                                "ref": rescue_ref,
                                "commit": _rescue.get("commit"),
                                "files": _remove_record_discarded,
                                "actor": actor,
                            })
                            # SC1 AC2：ignored 用户文件不在 rescue ref 内，必须在
                            # force remove 前复制到独立救援目录；复制失败即不得
                            # force remove，降级 L3 整目录隔离。
                            if _dirty_snapshot is None:
                                _l2_files = {
                                    "ok": False, "dir": None, "copied": [],
                                    "failed": [],
                                    "error": "dirty_snapshot_unavailable",
                                }
                            else:
                                _user_ignored = list(
                                    _dirty_snapshot.get("user_ignored") or [])
                                if _user_ignored:
                                    _l2_files = _l2_backup_ignored_files(
                                        wt_path, _backup_root(stable_wt),
                                        task_id, _user_ignored)
                                else:
                                    _l2_files = {
                                        "ok": True, "dir": None, "copied": [],
                                        "failed": [], "error": None,
                                    }
                            recycle_log.append({
                                "action": "ladder_rescue_files",
                                "task_id": task_id,
                                "level": "L2",
                                "ok": _l2_files["ok"],
                                "dir": _l2_files.get("dir"),
                                "copied": _l2_files.get("copied"),
                                "failed": _l2_files.get("failed"),
                                "error": _l2_files.get("error"),
                                "actor": actor,
                            })
                            if _l2_files["ok"]:
                                ladder_level = "L2"
                                proc = subprocess.run(
                                    ["git", "worktree", "remove", "--force",
                                     str(wt_path)],
                                    cwd=str(stable_wt),
                                    capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=30,
                                )
                                removed = proc.returncode == 0
                                if not removed:
                                    remove_error = (
                                        (proc.stderr or proc.stdout or "")
                                        .strip()[:300])
                            else:
                                # 复制失败：拒绝 force remove，降级 L3。
                                ladder_level = "L3"
                                recycle_log.append({
                                    "action": "ladder_rescue_files_failed",
                                    "task_id": task_id,
                                    "level": "L3",
                                    "error": _l2_files.get("error"),
                                    "actor": actor,
                                })
                        else:
                            ladder_level = "L3"
                            recycle_log.append({
                                "action": "ladder_rescue_failed",
                                "task_id": task_id,
                                "level": "L3",
                                "error": _rescue.get("error"),
                                "actor": actor,
                            })
                    if not removed and ladder_level in ("L2", "L3"):
                        # L3：整目录隔离到 .orchd/.doctor-backup/worktrees/。
                        assert stable_wt is not None
                        _iso = _l3_isolate_dir(
                            wt_path, _backup_root(stable_wt), task_id)
                        if _iso["ok"]:
                            ladder_level = "L3"
                            isolated_to = str(_iso["target"])
                            removed = True
                            recycle_log.append({
                                "action": "ladder_isolate",
                                "task_id": task_id,
                                "level": "L3",
                                "target": isolated_to,
                                "actor": actor,
                            })
                            # 隔离后 recorded path 已消失 → prune 收敛陈旧登记
                            #（否则 task/* 分支名被占用，后继删分支失败）。
                            try:
                                pr = subprocess.run(
                                    ["git", "-C", str(stable_wt), "worktree",
                                     "prune"],
                                    capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=_GIT_TIMEOUT,
                                )
                                registry_pruned = pr.returncode == 0
                            except (subprocess.SubprocessError, FileNotFoundError,
                                    OSError):
                                registry_pruned = False
                        else:
                            ladder_level = "L4"
                            recycle_log.append({
                                "action": "ladder_isolate_failed",
                                "task_id": task_id,
                                "level": "L4",
                                "error": _iso.get("error"),
                                "actor": actor,
                            })
                    if not removed and ladder_level == "L4":
                        # L4：bundle+patch 备份后重试清理（rmtree）。
                        assert stable_wt is not None
                        _b = _l4_bundle_patch(
                            wt_path, _backup_root(stable_wt), task_id,
                            snapshot=_dirty_snapshot)
                        rescue_backup_dir = _b.get("dir")
                        recycle_log.append({
                            "action": "ladder_bundle",
                            "task_id": task_id,
                            "level": "L4",
                            "dir": _b.get("dir"),
                            "bundle": _b.get("bundle"),
                            "patch": _b.get("patch"),
                            "status_file": _b.get("status_file"),
                            "files_dir": _b.get("files_dir"),
                            "manifest": _b.get("manifest"),
                            "mirrored_files": _b.get("mirrored_files"),
                            "bundle_ok": _b["ok"],
                            "errors": _b.get("errors"),
                            "actor": actor,
                        })
                        if _b["ok"]:
                            try:
                                shutil.rmtree(str(wt_path))
                                removed = not wt_path.exists()
                            except OSError as exc:
                                remove_error = (
                                    f"{type(exc).__name__}: {exc}"[:300])
                            if removed:
                                try:
                                    pr = subprocess.run(
                                        ["git", "-C", str(stable_wt), "worktree",
                                         "prune"],
                                        capture_output=True, encoding="utf-8",
                                        errors="replace", timeout=_GIT_TIMEOUT,
                                    )
                                    registry_pruned = pr.returncode == 0
                                except (subprocess.SubprocessError,
                                        FileNotFoundError, OSError):
                                    registry_pruned = False
                    if not removed and ladder_level in ("L4", "L5"):
                        # L5：原地保留 + manual notice（数据零丢失，等人工处置）。
                        ladder_level = "L5"
                        recycle_log.append({
                            "action": "ladder_preserve_in_place",
                            "task_id": task_id,
                            "level": "L5",
                            "target": str(wt_path),
                            "error": remove_error,
                            "actor": actor,
                        })
            removed = removed or proc.returncode == 0
        else:
            # W-8 / R-35：目录本就不存在时仍收敛 git 登记——陈旧登记会占用
            # task/* 分支名导致分支无法删除（幽灵登记 → 分支永久泄漏）。
            try:
                pr = subprocess.run(
                    ["git", "-C", str(stable_wt), "worktree", "prune"],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT,
                )
                registry_pruned = pr.returncode == 0
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                registry_pruned = False
            removed = True  # 独立 worktree 本就不存在 → 视为已回收
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        remove_error = f"{type(exc).__name__}: {exc}"[:300]
        removed = False
        if isinstance(exc, PermissionError):
            _remove_reason = _classify_remove_failure(str(exc), _cwd_inside)
    finally:
        # W-7：os.chdir 是进程级副作用，移除阶段结束即恢复调用方原 cwd。原 cwd 已被
        # 本次回收删除（常见情形）时保持 stable_wt，避免把进程放进失效目录；后续
        # git 调用一律显式 -C stable_wt，不依赖进程 cwd。
        if cwd_self_healed and _original_cwd is not None:
            try:
                if _original_cwd.is_dir():
                    os.chdir(str(_original_cwd))
            except OSError:
                pass
    _remove_record: dict[str, Any] = {
        "action": "worktree_remove" if wt_existed else "worktree_absent",
        "task_id": task_id,
        "target": str(wt_path),
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "status": status,
        "actor": actor,
    }
    if remove_error:
        _remove_record["error"] = remove_error
    if _remove_reason:
        _remove_record["reason"] = _remove_reason
    if _remove_record_discarded and discarded_uncommitted:
        _remove_record["discarded_files"] = _remove_record_discarded
    elif _remove_record_discarded:
        # 救援路径：同一份快照是「被保全」而非「被丢弃」，字段名不撒谎。
        _remove_record["dirty_files"] = _remove_record_discarded
    if registry_pruned is not None:
        _remove_record["registry_pruned"] = registry_pruned
    if ladder_level != "L1" or rescue_ref or isolated_to or rescue_backup_dir:
        _remove_record["ladder"] = ladder_level
    if rescue_ref:
        _remove_record["rescue_ref"] = rescue_ref
    if isolated_to:
        _remove_record["isolated_to"] = isolated_to
    if rescue_backup_dir:
        _remove_record["rescue_backup_dir"] = rescue_backup_dir
    recycle_log.append(_remove_record)
    # P0-19：Windows 下 git worktree remove 可能删除内容但残留空目录
    # （文件句柄 / .lock / 杀毒扫描导致目录删除不完整）。best-effort 清理残留。
    # L5（原地保留）不清理：目录是唯一的数据保全副本，rmtree 会销毁救援底线。
    residual_cleaned = False
    if (
        wt_path.exists()
        and not (wt_path / ".git").exists()
        and ladder_level != "L5"
    ):
        try:
            # 先尝试 rmdir（仅空目录成功，安全）
            wt_path.rmdir()
            residual_cleaned = True
        except OSError:
            # 非空目录或权限问题 → 尝试 rmtree（兜底，可能因杀毒/句柄失败）
            try:
                shutil.rmtree(str(wt_path), ignore_errors=True)
                if not wt_path.exists():
                    residual_cleaned = True
            except Exception:
                pass
    if residual_cleaned:
        recycle_log.append({
            "action": "residual_clean",
            "task_id": task_id,
            "target": str(wt_path),
            "actor": actor,
        })
    # task-debug-worktree-merge-hint：失败归因要能直接照做。``cwd_held_by_caller``
    # 的处置是"人从任务目录里走出来"，光给 reason 等于让 agent 自己猜；这里给出
    # 可执行动作项（含目标 cwd 与复核命令）。既有字段一个不动，纯加法。
    _post_action: dict[str, Any] | None = None
    # 触发条件**语言无关**：只要没删成、目录还在，就给动作项。``remove_reason``
    # 仍保留（cwd_held_by_caller 等），但只作为附加信息——中文 git / 本地化
    # Windows 的 strerror 不含 "permission denied"，拿它当开关会让动作项永不出现。
    if not removed and wt_path.exists():
        if ladder_level == "L5":
            # 五级降级终点：脏工作区数据已原地保全（救援/隔离/bundle 全部失败），
            # 此时的动作项是「人工处置」而不是「重跑回收」——重跑只会重复整梯。
            _post_action = {
                "kind": "manual_intervention",
                "cwd": str(stable_wt) if stable_wt else None,
                "command": "python .orchd/__main__.py doctor",
                "reason_hint": "recycle_ladder_exhausted",
                "hint": (
                    "脏 worktree 五级降级全部失败（L2 救援提交 / L3 目录隔离 / "
                    "L4 bundle+patch 均未成功），目录已原地保留、未提交改动零丢失。"
                    "请人工检查句柄占用（杀毒/索引/打开的编辑器）后处置：释放句柄后"
                    "重跑回收，或手动备份目录内容后删除；勿直接 force 丢弃"
                ),
            }
        else:
            _post_action = {
                "kind": "change_cwd_then_recheck",
                "cwd": str(stable_wt) if stable_wt else None,
                "command": "python .orchd/__main__.py status",
                "reason_hint": _remove_reason or "unknown",
                "hint": (
                    "worktree 目录仍在（分支已删、绑定已解）：最常见原因是**调用方 shell 的 "
                    "cwd 仍在任务目录内**（Windows 无法删除当前目录），其次是杀毒/索引句柄。"
                    "先切到主工作树（cwd）再重跑 command 即收敛；仍失败则是外部句柄，"
                    "等句柄释放后重跑同一条命令，无需 retract 或 force-status"
                ),
            }
    if _post_action is not None:
        _remove_record["post_action"] = _post_action
    return {
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "discarded_files": _remove_record_discarded,
        "residual_cleaned": residual_cleaned,
        "remove_reason": _remove_reason,
        "post_action": _post_action,
        "ladder": ladder_level,
        "rescue_ref": rescue_ref,
        "isolated_to": isolated_to,
        "rescue_backup_dir": rescue_backup_dir,
    }



def _tx_step_delete_branch(
    stable_wt: Path | None,
    task_id: str,
    force_recycle: bool,
    actor: str,
    recycle_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """事务第 2 步：任务分支删除（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：先取未合并提交数，非零
    且无 force_recycle → 拒绝并留痕（W-3 红线）；否则安全 ``-d``（分支已不
    存在视为幂等成功）。幂等：分支缺席 → True。
    """
    # 删任务分支（best-effort）——W-3 / R-17（AGENTS.md 红线：不得用 -D 销毁未合并提交）：
    # ① 先取未合并提交数（git rev-list --count main..task/<id>）；② 非零 → 拒绝删除并
    # 留痕，除非调用方显式 force_recycle；③ 否则用安全 -d；④ 分支已不存在视为幂等成功。
    # 以主工作树为稳定 cwd（git -C）：worktree 已回收时 task/{id} 不再被占用可删除；
    # project_root（任务 worktree）可能已被 git worktree remove 删除，不能作为 cwd。
    branch_deleted = False
    branch_refused: dict[str, Any] | None = None
    unmerged_count: int | None = None
    from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

    branch = resolve_task_branch_for(stable_wt, task_id)
    trunk = resolve_trunk_for(stable_wt)
    try:
        count_proc = subprocess.run(
            ["git", "-C", str(stable_wt), "rev-list", "--count", f"{trunk}..{branch}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if count_proc.returncode == 0:
            _raw_count = (count_proc.stdout or "").strip()
            if _raw_count.isdigit():
                unmerged_count = int(_raw_count)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        unmerged_count = None

    if unmerged_count and unmerged_count > 0 and not force_recycle:
        branch_refused = {
            "branch": branch,
            "unmerged_commits": unmerged_count,
            "reason": "unmerged_branch_protected",
            "hint": "该分支未被主分支包含；确需丢弃请显式 force_recycle（CLI: --force-recycle）",
        }
        recycle_log.append({
            "action": "branch_delete_refused",
            "task_id": task_id,
            "branch": branch,
            "unmerged_commits": unmerged_count,
            "reason": "unmerged_branch_protected",
            "actor": actor,
        })
    else:
        _branch_flag = "-D" if (force_recycle and unmerged_count) else "-d"
        try:
            proc = subprocess.run(
                ["git", "-C", str(stable_wt), "branch", _branch_flag, branch],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
            )
            branch_deleted = proc.returncode == 0
            if not branch_deleted:
                _berr = (proc.stderr or "").lower()
                if (
                    "not found" in _berr
                    or "doesn't exist" in _berr
                    or "does not exist" in _berr
                ):
                    branch_deleted = True  # 幂等：分支已不存在（如 merge 流程已删）
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            branch_deleted = False
        recycle_log.append({
            "action": "branch_delete",
            "task_id": task_id,
            "branch": branch,
            "mode": _branch_flag,
            "unmerged_commits": unmerged_count,
            "deleted": branch_deleted,
            "actor": actor,
        })
    return {
        "branch_deleted": branch_deleted,
        "branch_refused": branch_refused,
        "unmerged_count": unmerged_count,
    }



def _tx_step_unbind(
    store_root: Path,
    task_id: str,
    lock_held: bool,
    actor: str,
    recycle_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """事务第 3 步：绑定解绑（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：best-effort，E002/锁失败
    转 warning 留痕并继续（保证 worktree/分支动作留痕不丢失）。幂等：无绑定
    也成功。注意：经模块全局名调用 :func:`unbind_task_wt`（测试 monkeypatch
    ``wt.unbind_task_wt`` 仍可拦截）。
    """
    unbind_error: str | None = None
    try:
        unbound = unbind_task_wt(store_root, task_id, lock_held=lock_held)
    except Exception as exc:
        # best-effort（task-unbind-recycle-audit-best-effort）：解绑抛 E002
        # （绑定损坏）或锁失败时不再向上传播——异常转 warning 并继续执行，
        # 保证本次 worktree / 分支动作的 recycle_log 仍落盘、不丢失留痕。
        code = getattr(getattr(exc, "code", None), "name", type(exc).__name__)
        unbind_error = f"{code}: {exc}"[:300]
        unbound = {
            "unbound": False,
            "task_id": task_id,
            "unbind_error": unbind_error,
        }
        recycle_log.append({
            "action": "unbind_failed",
            "task_id": task_id,
            "unbound": False,
            "error": unbind_error,
            "actor": actor,
        })
    else:
        recycle_log.append({
            "action": "unbind",
            "task_id": task_id,
            "unbound": unbound.get("unbound", False),
            "actor": actor,
        })
    return {"unbound": unbound, "unbind_error": unbind_error}
