"""worktree recycle steps 域（W6-C5 纯搬移）。

来源：``orchd/worktree/recycle.py``（1023 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchd.worktree.bindings import (
    _TASK_WT_PREFIX, _task_wt_name, load_bindings, unbind_task_wt,
)
from orchd.worktree.layout import _GIT_TIMEOUT, detect_layout



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
) -> dict[str, Any]:
    """事务第 1 步：worktree 移除（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：cwd 自愈 → ``git worktree
    remove``（脏工作区升级 ``--force``）/ 缺席时 prune 陈旧登记 → 残留空壳清理。
    幂等：目录本就不存在 → prune 后视为已回收。
    """
    removed = False
    discarded_uncommitted = False
    # task-recycle-observability：--force 前快照的被丢弃路径清单（None=无丢弃
    # 或未走到 --force；[] 不可能——空清单时不附键，见 _remove_record 组装）。
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
    try:
        if wt_existed:
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
                # W-6 / R-35：只有失败原因确为「工作区脏」才升级 --force；句柄占用 /
                # 权限 / 未知原因一律不升级（旧实现无条件升级：真脏时静默销毁未提交
                # 改动，句柄占用时白跑一遍 --force 且掩盖真实原因）。
                _err = (proc.stderr or "").lower()
                if "modified or untracked" in _err:
                    # task-recycle-observability：--force 前快照将被丢弃的路径清单
                    # （静默 discarded_uncommitted=true 即数据丢失尾巴；清单上限 50，
                    # best-effort，取不到不阻断 --force）。
                    discarded_files: list[str] | None = None
                    try:
                        _st = subprocess.run(
                            ["git", "-C", str(wt_path), "status",
                             "--porcelain=v1", "--untracked-files=all"],
                            capture_output=True, encoding="utf-8",
                            errors="replace", timeout=30,
                        )
                        if _st.returncode == 0:
                            discarded_files = [
                                ln[3:].strip() for ln in
                                (_st.stdout or "").splitlines() if ln.strip()
                            ][:50]
                    except (subprocess.SubprocessError, FileNotFoundError, OSError):
                        discarded_files = None
                    proc = subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt_path)],
                        cwd=str(stable_wt),
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                    discarded_uncommitted = proc.returncode == 0
                    if discarded_uncommitted and discarded_files:
                        _remove_record_discarded = list(discarded_files)
                else:
                    remove_error = (proc.stderr or proc.stdout or "").strip()[:300]
                    _remove_reason = _classify_remove_failure(
                        proc.stderr or proc.stdout or "", _cwd_inside)
            removed = proc.returncode == 0
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
    if _remove_record_discarded:
        _remove_record["discarded_files"] = _remove_record_discarded
    if registry_pruned is not None:
        _remove_record["registry_pruned"] = registry_pruned
    recycle_log.append(_remove_record)
    # P0-19：Windows 下 git worktree remove 可能删除内容但残留空目录
    # （文件句柄 / .lock / 杀毒扫描导致目录删除不完整）。best-effort 清理残留。
    residual_cleaned = False
    if wt_path.exists() and not (wt_path / ".git").exists():
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
    return {
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "discarded_files": _remove_record_discarded,
        "residual_cleaned": residual_cleaned,
        "remove_reason": _remove_reason,
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
