"""worktree recycle remove 域（W6-C5 纯搬移）。

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
from .journal import (
    _TX_STEPS,
    _log_recycle,
    _recycle_actor,
    _recycle_journal_path,
    _task_status_for_recycle,
    _tx_fact_branch_gone,
    _tx_fact_unbound,
    _tx_fact_wt_gone,
    _tx_journal_clear,
    _tx_journal_read,
    _tx_journal_write,
    _tx_mark_step_done,
    _tx_prune_registry,
)

from .steps import (
    _tx_step_delete_branch,
    _tx_step_remove_worktree,
    _tx_step_unbind,
)




def remove_task_wt(
    project_root: Path, task_id: str, store_root: Path, *, lock_held: bool = False,
    force_recycle: bool = False,
) -> dict[str, Any]:
    """终态回收：git worktree remove + 删 task/{id} 分支 + 解绑（best-effort 幂等）。

    ``lock_held=True``：调用方已在同一共享账本根持有 ``.lock``（同进程），解绑时
    复用该锁、不再重复 flock（见 :func:`unbind_task_wt`），规避同进程双 fd E012 死锁。

    分支删除安全闸（review W-3 / R-17，AGENTS.md 红线）：
    - 默认只用安全 ``git branch -d``（拒绝删除未合并分支），**绝不再无条件 ``-D``**；
    - 删除前以 ``git rev-list --count main..task/<id>`` 取未合并提交数：非零即拒绝删除，
      计数与拒绝原因记入 ``recycle_log``（``action=branch_delete_refused``）并在返回值
      中给出 ``branch_delete_refused``；
    - 仅当调用方显式传 ``force_recycle=True``（CLI 侧对应 ``--force-recycle``）时，
      才对未合并分支升级 ``-D``；计数为 0 时仍走 ``-d``（无需强删）。

    worktree 移除安全闸（review W-6）：非 ``--force`` 移除失败时，仅当 stderr 表明
    「contains modified or untracked files」才升级 ``--force``（终态回收允许丢弃脏
    工作区并记 ``discarded_uncommitted``）；句柄占用 / 权限 / 未知原因一律不升级，
    失败原因写入 ``recycle_log`` 与 ``residual``（可读、不静默）。

    稳定 cwd 闸（review W-7）：``stable_wt`` 必须是与待回收 worktree **不同**且真实
    存在的主工作树根（``main_worktree_root`` 探测失败会回退 ``project_root``，容器
    布局下那往往就是待删的 worktree 本身）；不满足即拒绝执行删除动作（``reason=
    unstable_main_worktree``）。回收前的 ``os.chdir`` 自愈在移除阶段 ``finally`` 中
    恢复调用方原 cwd（原 cwd 已被本次回收删除时保持 ``stable_wt``，不落进失效目录）。

    幽灵登记收敛（review W-8）：worktree 目录本就不存在时，仍执行 ``git worktree
    prune`` 清理陈旧登记（否则 ``task/*`` 分支被残留 worktree 登记占用而无法删除，
    永久泄漏），结果记入 ``recycle_log`` 的 ``registry_pruned``。

    in_review 保护断言（2026-08-30 复盘 §1）：任务状态为 in_review 时拒绝删除。
    in_review 是审查等待期（任务可能 idle 数小时），恰是分支误删高危窗口；正常
    回收流（review 通过 / force_status 终态）都在状态写入 completed/cancelled
    **之后**才调用本函数，故出现 in_review 即视为异常请求。

    Returns:
        ``{"removed": True, "unbound": True}``；失败降级 ``{"removed": False, ...}``。
        in_review 保护命中返回 ``{"removed": False, "reason": "in_review_protected",
        "status": "in_review"}``；删除动作/决策记录于 ``recycle_log``。
    """
    layout = detect_layout(project_root)
    wt_path = layout["task_wt_root"] / _task_wt_name(task_id)
    # task-14-review-branch-cleanup(AC2)：容器布局下 project_root 即任务 worktree
    # （== wt_path），git worktree remove 会删掉该目录。用 main_worktree_root 在
    # 移除前（cwd 仍有效）解析主工作树根作为稳定 cwd，后续删分支不再依赖已删除的
    # cwd。local import 复用 gitops 已有解析（flat 布局回退 project_root，零回归）。
    from orchd.gitops import main_worktree_root

    stable_wt = main_worktree_root(project_root)
    actor = _recycle_actor()
    recycle_log: list[dict[str, Any]] = []

    # in_review 保护断言（只读查询，确认命中才拒绝）。W-10：账本读取失败**不再
    # 静默放行**（旧实现返回 None 会让保护在账本瞬时不可读时失效）→ 拒绝回收并
    # 留痕，只有显式 force_recycle 才覆盖。
    try:
        status = _task_status_for_recycle(store_root, task_id)
    except Exception as exc:
        if not force_recycle:
            record = {
                "action": "blocked",
                "reason": "status_unreadable",
                "task_id": task_id,
                "target": str(wt_path),
                "error": str(exc)[:300],
                "actor": actor,
            }
            recycle_log.append(record)
            _log_recycle(recycle_log)
            return {
                "removed": False,
                "unbound": False,
                "reason": "status_unreadable",
                "recycle_log": recycle_log,
            }
        status = None
    if status == "in_review":
        record = {
            "action": "blocked",
            "reason": "in_review_protected",
            "task_id": task_id,
            "target": str(wt_path),
            "status": status,
            "actor": actor,
        }
        recycle_log.append(record)
        _log_recycle(recycle_log)
        return {
            "removed": False,
            "unbound": False,
            "reason": "in_review_protected",
            "status": status,
            "recycle_log": recycle_log,
        }

    # W-7 / R-35：stable_wt 必须是与待回收 worktree 不同的真实主工作树根。
    # main_worktree_root 在 git 探测失败时回退 project_root——容器布局下
    # project_root 往往就是待删的任务 worktree 本身；此时继续执行会让
    # git worktree remove / 分支删除带着失效 cwd 冒进（旧实现静默如此）。
    # 此处 fail-safe：拒绝执行删除动作并留痕（宁可留残留交 doctor 处置）。
    _stable_resolved: Path | None = None
    try:
        _stable_resolved = Path(stable_wt).resolve() if stable_wt else None
    except OSError:
        _stable_resolved = None
    _wt_resolved_for_guard = wt_path.resolve()
    if (
        _stable_resolved is None
        or _stable_resolved == _wt_resolved_for_guard
        or not _stable_resolved.is_dir()
    ):
        recycle_log.append({
            "action": "blocked",
            "reason": "unstable_main_worktree",
            "task_id": task_id,
            "target": str(wt_path),
            "stable_worktree": str(_stable_resolved) if _stable_resolved else None,
            "actor": actor,
        })
        _log_recycle(recycle_log)
        return {
            "removed": False,
            "unbound": False,
            "reason": "unstable_main_worktree",
            "recycle_log": recycle_log,
        }

    # ---- F4 事务 preamble：journal 载入 + 重放判定（task-worktree-recycle-tx）----
    # 早退守卫（状态 / in_review / stable）均在本行之前：journal 只在"即将执行
    # 删除动作"时读写，不污染拒绝路径 recycle_log 的精确形态（== ["blocked"]）。
    # 意图先落 journal（四步全 pending）：崩溃落在任何位置都有据可重放；落盘失败
    # （store_root 为 None / 目录不可写）→ journal-less 降级，既有行为零变化。
    _jx_path: Path | None = (
        _recycle_journal_path(store_root, task_id)
        if store_root is not None else None
    )
    _jx_doc = _tx_journal_read(_jx_path)
    _jx_resumed = isinstance(_jx_doc, dict)
    if not _jx_resumed:
        _jx_fresh: dict[str, Any] = {
            "task_id": task_id,
            "steps": {name: {"state": "pending"} for name in _TX_STEPS},
        }
        if not _tx_journal_write(_jx_path, _jx_fresh):
            _jx_path = None
    _jx_prior = ((_jx_doc or {}).get("steps") or {}) if _jx_resumed else {}

    def _jx_was_done(_name: str) -> bool:
        _ent = _jx_prior.get(_name)
        return isinstance(_ent, dict) and _ent.get("state") == "done"

    # 记 done + 事实验证通过 → 跳过并收敛子动作；否则重做（事实不明按重做计，
    # 重做本身 best-effort，失败口径与既有行为一致）。
    _skip_remove = _jx_was_done("worktree_remove") and _tx_fact_wt_gone(wt_path)
    _skip_branch = _jx_was_done("branch_delete") and (
        _tx_fact_branch_gone(stable_wt, task_id) is True)
    _skip_unbind = _jx_was_done("unbind") and (
        _tx_fact_unbound(store_root, task_id) is True)
    if _jx_resumed:
        recycle_log.append({
            "action": "tx_resume",
            "task_id": task_id,
            "skipped": sorted(
                _n for _n, _s in (("worktree_remove", _skip_remove),
                                  ("branch_delete", _skip_branch),
                                  ("unbind", _skip_unbind)) if _s),
            "actor": actor,
        })
    else:
        recycle_log.append({
            "action": "tx_begin",
            "task_id": task_id,
            "steps": list(_TX_STEPS),
            "actor": actor,
        })

    # ---- 事务第 1 步：worktree 移除（失败/拒绝不阻断后继步骤，与既有线性一致）----
    if _skip_remove:
        removed = True
        discarded_uncommitted = False
        discarded_files = None
        residual_cleaned = False
        _remove_reason = None
        # 子动作收敛：prune 仍跑（防崩溃落在 prune 与记 done 之间——幽灵登记会
        # 占用分支名，致后继删分支失败，属半完成态，必须在重放中收敛）。
        _tx_prune_registry(stable_wt)
        recycle_log.append({
            "action": "tx_skip",
            "tx_step": "worktree_remove",
            "task_id": task_id,
            "verified": "wt_absent",
            "actor": actor,
        })
    else:
        _s1 = _tx_step_remove_worktree(
            stable_wt=stable_wt, wt_path=wt_path, status=status,
            actor=actor, task_id=task_id, recycle_log=recycle_log)
        removed = _s1["removed"]
        discarded_uncommitted = _s1["discarded_uncommitted"]
        discarded_files = _s1.get("discarded_files")
        residual_cleaned = _s1["residual_cleaned"]
        _remove_reason = _s1["remove_reason"]
        if removed:
            _tx_mark_step_done(
                _jx_path, task_id, "worktree_remove", {"removed": True})
    # 删任务分支（best-effort）——W-3 / R-17（AGENTS.md 红线：不得用 -D 销毁未合并提交）：
    # ---- 事务第 2 步：分支删除（拒绝/失败不阻断第 3 步，与既有线性一致）----
    if _skip_branch:
        branch_deleted = True
        branch_refused = None
        recycle_log.append({
            "action": "tx_skip",
            "tx_step": "branch_delete",
            "task_id": task_id,
            "verified": "branch_absent",
            "actor": actor,
        })
    else:
        _s2 = _tx_step_delete_branch(
            stable_wt=stable_wt, task_id=task_id, force_recycle=force_recycle,
            actor=actor, recycle_log=recycle_log)
        branch_deleted = _s2["branch_deleted"]
        branch_refused = _s2["branch_refused"]
        if branch_deleted:
            _tx_mark_step_done(
                _jx_path, task_id, "branch_delete", {"deleted": True})

    # ---- 事务第 3 步：解绑 ----
    if _skip_unbind:
        unbound = {"unbound": True, "task_id": task_id}
        unbind_error = None
        recycle_log.append({
            "action": "tx_skip",
            "tx_step": "unbind",
            "task_id": task_id,
            "verified": "binding_absent",
            "actor": actor,
        })
    else:
        _s3 = _tx_step_unbind(
            store_root=store_root, task_id=task_id, lock_held=lock_held,
            actor=actor, recycle_log=recycle_log)
        unbound = _s3["unbound"]
        unbind_error = _s3["unbind_error"]
        if unbound.get("unbound", False):
            _tx_mark_step_done(_jx_path, task_id, "unbind", {"unbound": True})

    # ---- 事务第 4 步 commit：三步全成 → 删 journal；否则保留供重放 ----
    # 保留 journal = 显式半完成态记录（可恢复/可重放，AC2），非新增静默状态：
    # 失败本就经 residual/branch_delete_refused/unbind_error 返回，journal 只是
    # 让"下次调用"能接着做完而不是从头再试（AC3：故障注入可收敛）。
    _tx_committed = False
    if removed and branch_deleted and unbound.get("unbound", False):
        # journal-less 降级（落盘失败）无 journal 可删，视同已提交，不误报。
        _tx_committed = _jx_path is None or _tx_journal_clear(_jx_path)
        recycle_log.append({
            "action": "tx_commit" if _tx_committed else "tx_commit_failed",
            "task_id": task_id,
            "actor": actor,
        })
    _log_recycle(recycle_log)
    result: dict[str, Any] = {
        "removed": removed,
        "unbound": unbound.get("unbound", False),
        "branch_deleted": branch_deleted,
        "recycle_log": recycle_log,
    }
    if unbind_error is not None:
        result["unbind_error"] = unbind_error
        result["warnings"] = [{
            "code": "unbind_failed",
            "task_id": task_id,
            "error": unbind_error,
        }]
    if branch_refused:
        # W-3：未合并分支被拒绝删除 —— 显式回传（含未合并提交数与逃生开关指引）
        result["branch_delete_refused"] = branch_refused
    if discarded_uncommitted:
        result["discarded_uncommitted"] = True
        if discarded_files:
            result["discarded_files"] = discarded_files
    if residual_cleaned:
        result["residual_cleaned"] = True
    if _jx_resumed:
        result["resumed_from_journal"] = True
    if not _tx_committed and (
        removed and branch_deleted and unbound.get("unbound", False)
    ):
        # 三步全成但 journal 落盘删不掉（目录不可写等）：残留 journal 下次调用
        # 重放收敛（全验证通过→再次 commit），此处显式标记供审计。
        result["journal_commit"] = False
    # AC6（task-review-baseline-and-worktree-recycle-fix）：回收失败可解释。
    # 修复前 removed=false 时仅静默返回（unbind + 删分支照旧），磁盘残留空壳目录
    # 无人知晓（实测 task-check-test-dedup-utf8/ 残留，仅 doctor 可检出）。现显式
    # 返回 residual 标记并指向 worktree_residual 处置入口（禁止静默失败）。
    residual_dir = str(wt_path) if wt_path.exists() else None
    if not removed or residual_dir:
        # task-recycle-observability：残留附诊断束——并发 git 进程数（复用
        # guard 计数，只读）、调用方 cwd/pid（句柄占用归因线索）。无 sysinternals
        # 不可得句柄持有者 PID，此处不做无据指认，只给可执行排查信息。
        _diag: dict[str, Any] = {}
        try:
            from orchd.gitops.guard import _count_git_processes

            _diag["concurrent_git_processes"] = _count_git_processes()
        except Exception:
            pass
        try:
            _diag["caller_cwd"] = str(Path.cwd().resolve())
        except OSError:
            pass
        try:
            _diag["caller_pid"] = os.getpid()
            _diag["caller_ppid"] = os.getppid()
        except (OSError, AttributeError):
            pass
        result["residual"] = {
            "path": residual_dir or str(wt_path),
            "removed": removed,
            "residual_cleaned": residual_cleaned,
            "reason": (
                "调用方 shell 的 cwd 仍位于待删目录，git worktree remove 被句柄占用拒绝"
                "（切出目录后重试，或等下次引擎调用自动回收）"
                if _remove_reason == "cwd_held_by_caller"
                else (
                    "git worktree remove 未成功（常见于调用方 cwd 位于该 worktree 内，"
                    "或 Windows 文件句柄占用）"
                    if not removed
                    else "worktree 已注销但目录仍存在（残留空壳，Windows 句柄/杀毒扫描常见）"
                )
            ),
            "hint": (
                "退出该目录后重试回收；或运行 python .orchd/__main__.py doctor "
                "查看 worktree_residual 项并执行 --fix 清理"
            ),
            "doctor_check": "worktree_residual",
        }
        if _diag:
            result["residual"]["diagnostics"] = _diag
    return result
