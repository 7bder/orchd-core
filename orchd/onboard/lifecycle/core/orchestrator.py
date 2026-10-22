"""lifecycle core orchestrator 域 — done 主流程编排（唯一汇总点）。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

``done`` 是 ``_done_impl`` 的 finally 兜底包装（保证会话锁释放），
``_done_impl`` 按 S-A2 阶段顺序调用各域函数，自身不做实质判定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orchd.gitops import (
    hook_uninstall,
    release_session_lock_if_owned,
    session_lock_check,
    session_lock_release,
)
from orchd.ledger import Store

from orchd.onboard.lifecycle.guards import _guard_cross_worktree_dirty
from orchd.onboard.lifecycle.regression import _maybe_full_regression

from .entry_guard import _guard_shared_entry_coverage
from .finish import _assemble_done_result, _done_lessons_hook, _write_done_event
from .reconcile import _check_base_freshness, _reconcile_before_verify
from .stages import _commit_and_verify_integrity, _done_precheck
from .verify import _run_verify


def done(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    changes_description: str,
    concerns: str | None = None,
    project_root: Path | None = None,
    skip_lesson_review: bool = False,
) -> dict[str, Any]:
    """报告任务完成（task-session-lock-lifecycle：异常路径也保证释放会话锁）。

    ``_done_impl`` 的包装：``finally`` 中经 :func:`release_session_lock_if_owned`
    条件释放本 agent 的 session 锁（仅持有者==本 agent 才释放，幂等）。正常路径
    由 ``_done_impl`` 尾部释放并写 ``session_lock_released``；异常/提前返回路径
    由本包装器的 finally 兜底，杜绝漏放锁（此前要求 60min 超时 + watchdog 兜底）。
    """
    try:
        return _done_impl(
            store, tasks, agent_id, task_id, changes_description, concerns,
            project_root, skip_lesson_review,
        )
    finally:
        if project_root:
            release_session_lock_if_owned(project_root / ".orchd", agent_id)


def _done_impl(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    changes_description: str,
    concerns: str | None = None,
    project_root: Path | None = None,
    skip_lesson_review: bool = False,
) -> dict[str, Any]:
    """报告任务完成。verify_command 锁外执行，锁内二次校验 + 写事件。

    采用两阶段模式以避免长时间持锁：
      1. 锁外阶段：预校验任务状态 → 执行 verify_command（可能耗时较长）。
      2. 锁内阶段：TOCTOU 二次校验状态是否仍为 claimed → 写 DONE 事件 →
         自动追加 REVIEW_READY(spec) 事件，触发 spec 审查流程。
     这种模式确保 verify_command 的长耗时不会阻塞其他 agent 的并发操作。

    S-A2 重构（task-audit-onboard-done-split）：原 645 行单函数按职责拆为
    ``_done_precheck`` / ``_run_verify`` / ``_commit_and_verify_integrity`` /
    ``_write_done_event`` 四段阶段函数，本函数仅做编排；对外签名与返回结构零变化。
    """
    # 0) task-done-root-resolution AC1/AC2：container 布局下从主工作树执行 done 时，
    #    解析 project_root 到任务 worktree（分支实际 checkout 处）。strict 模式下
    #    解析不到任务 worktree 时抛错阻断（不回退主工作树、不静默写 DONE）。
    #    flat 布局与本身即任务 worktree 时原样返回（零回归）。
    if project_root:
        from orchd.gitops_ops import resolve_task_worktree
        project_root = resolve_task_worktree(project_root, task_id, strict=True)

    # 1) 预校验（无锁）：任务查找 + files_to_edit + claimed 状态 + L1/root 守卫
    task_def, files_to_edit, degraded_guards = _done_precheck(
        store, tasks, task_id, agent_id, project_root,
    )

    # 2) 跨 worktree 脏写检测（fail-closed，E017）
    _guard_cross_worktree_dirty(project_root, files_to_edit, task_id, degraded_guards)

    # 2.5) 基线新鲜度闸口（task-done-base-freshness）：main 已在实现期间持续推进（本
    #      仓库 main 已 297+ completed），任务分支基线与 main 漂移。既有 reconcile
    #      （挂载点 ① 下一段）只在“main 推进且触及本任务文件”时才阻断，而“main 推进但
    #      未触及 task 改动文件”的场景当前静默放行——实现者带陈旧基线 done、合并后到
    #      review 期才暴露 drift（E015）。此处先判 task 分支是否落后 main，落后即阻断，
    #      早发现避免 review 期 E015。仅判落后与否，不产生 git 写操作；与随后的
    #      reconcile 冲突对账互补互不冲突。
    _check_base_freshness(project_root, task_id, degraded_guards)

    # 3) 与 main 对账（task-done-reconcile-main 挂载点 ①）：顺序约束——必须先对账、
    #     后 verify，否则 verify 跑在对账前的旧内容上。此处仅在 main 推进且触及本任务
    #     文件时检出冲突（2.5 已覆盖“未触及文件”的陈旧基线场景，二者互补）。
    _reconcile_before_verify(project_root, task_id, degraded_guards)

    # 3.5) 共享入口 verify 覆盖门禁（task-shared-entry-verify-gate，E039）：改了被多个
    #      既有测试文件断言的共享入口（如 .githooks/pre-push、orchd/cli/__init__.py）时，
    #      verify_command 必须含其登记的既有测试——否则「只跑自己新写的测试」会让既有断言
    #      静默变红并随双审入库（2026-09-19 一夜三次：prepush-tag-gate / cli-json-envelope
    #      / canonical-root-dedup）。刻意早于 verify 执行：缺口是静态可判定的，先判先省。
    #      无 project_root 时同样执行：函数内 changed 回退声明的 files_to_edit（纯函数可判
    #      registry 规则），形态 A 的基线判定在无 project_root 时自行跳过（fail-open）。
    _guard_shared_entry_coverage(
        project_root, task_def, task_id, files_to_edit, degraded_guards
    )

    # 4) verify_command 锁外执行（含超时/失败假消除；假失败消除命中时提前返回）
    verify_record, early_done = _run_verify(store, task_def, task_id, project_root)
    if early_done is not None:
        return early_done

    # 5) 自动提交 + 声明/残留/越界/干净完整性门禁（均 fail-closed）
    commit_result = _commit_and_verify_integrity(
        store, task_def, task_id, agent_id, project_root,
        files_to_edit, changes_description, degraded_guards,
    )

    # 6) 全量回归（task-full-regression-gate-r2：默认关闭，仅 config 显式 true 时跑）
    full_regression = _maybe_full_regression(store, files_to_edit, project_root)

    # 7) 终态收敛：done 写事件前恢复主工作树 trunk 基线
    #    （task-pass9-done-review-finalizer-unify：经 release_task_lifecycle
    #    strict 统一出口；checked_out_main 响应形状经 compat 保持兼容）。
    checked_out_main = None
    if project_root:
        # 延迟导入避免循环依赖
        from orchd.gitops.guard import compat_checked_out_main, release_task_lifecycle

        checked_out_main = compat_checked_out_main(release_task_lifecycle(
            store, project_root, task_id=task_id, agent_id=agent_id,
            command="done", mode="strict",
        ))

    # 7.5) task-engine-cli-friction-fix：切回主工作树后 os.chdir，保证同进程
    #      内 done 后的后续操作（如 status 检查）不因 cwd 指向已回收的任务 worktree
    #      而报 cwd does not exist。best-effort，异常静默跳过。
    if project_root:
        try:
            import os
            os.chdir(str(project_root))
        except Exception:
            pass

    # 8) 锁内二次校验 + 写 DONE/REVIEW_READY + checkpoint
    written = _write_done_event(
        store, task_id, task_def, agent_id, changes_description, concerns, verify_record,
    )

    # 9) 经验回灌 done 收尾 hook（best-effort，§8.6）
    lessons_summary, lessons_require_review = _done_lessons_hook(
        store, task_id, verify_record, skip_lesson_review,
    )

    # 10) 结果组装（degraded/author_mismatch/full_regression/commit/lessons 附带字段）
    result = _assemble_done_result(
        task_id=task_id,
        attempt_count=written["attempt_count"],
        review_type=written["review_type"],
        done_event_id=written["done_event"]["event_id"],
        integrity_warnings=written["integrity_warnings"],
        degraded_guards=degraded_guards,
        author_mismatch=written["author_mismatch"],
        claimed_by=written["claimed_by"],
        caller_fingerprint=agent_id,
        full_regression=full_regression,
        commit_result=commit_result,
        checked_out_main=checked_out_main,
        lessons_summary=lessons_summary,
        lessons_require_review=lessons_require_review,
    )

    # task-done-cwd-hook-hygiene AC1：post_done_cwd 须指向 canonical 主工作树，
    # 而非此处 project_root（已在上方 task-done-root-resolution 解析为任务 worktree）。
    # 任务终态 worktree 回收后，若仍指向任务 worktree，则 guidance 生成的
    # Set-Location 命令必失败（cwd does not exist）。改取 resolve_canonical_project_root
    # 回到主工作树根，与 hint「请切回主工作树」方向一致。
    # 在 _done_impl 中设置（_assemble_done_result 无 project_root 参数）。
    if project_root:
        from orchd.worktree import resolve_canonical_project_root

        result["post_done_cwd"] = str(resolve_canonical_project_root(project_root))
    else:
        result["post_done_cwd"] = ""

    # 11) L3 pre-commit hook 卸载 + 释放本 agent session 锁（best-effort，锁外）
    if project_root:
        hook_uninstall(project_root)
    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    return result
