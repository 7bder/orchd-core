"""Orchd 任务生命周期管理 - lifecycle/core 域。

迁移自 orchd/onboard.py（task-split-onboard-lifecycle-core）：
  - done: 任务完成入口（finally 兜底释放会话锁）
  - _done_impl: 任务完成编排（两阶段：锁外 verify + 锁内写事件）
  - _done_precheck: 无锁预校验
  - _run_verify: 锁外执行 verify_command
  - _verify_fail_error: 构造 verify 失败 E014
  - _verify_timeout_error: 构造 verify 超时 E014
  - _verify_failure_early_done: 假失败消除
  - _commit_and_verify_integrity: 自动提交 + 4 个完整性门禁
  - _done_auto_commit: best-effort 自动提交
  - _write_done_event: 锁内二次校验 + 写 DONE/REVIEW_READY
  - _done_lessons_hook: 经验回灌 done 收尾 hook
  - _assemble_done_result: 组装 done 完整响应
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    GUARD_STATUS_NOT_APPLICABLE,
    ensure_committed,
    guard_clean_workspace as _guard_clean_workspace,
    guard_done_branch as _guard_done_branch,
    hook_uninstall,
    record_degraded_guard,
    release_session_lock_if_owned,
    session_lock_check,
    session_lock_release,
)
from orchd.gitops_ops import (
    decode_subprocess_output as _decode_subprocess_output,
    make_event as _make_event,
    verify_output_summary as _verify_output_summary,
)
from orchd.guide import NEXT_ACTION_AWAIT_REVIEW
from orchd.ledger import Store, resolve_review_mode
from orchd.review import find_last_done_event as _find_last_done_event
from orchd.shared_entries import (
    GLOBAL_SHARED_FILES,
    SHARED_ENTRY_TESTS,
    BaselineTestProbe,
    missing_baseline_test_coverage,
    missing_shared_entry_tests,
)
from orchd.subproc import run_shell

# 同包门禁（直接导入子模块，避免循环依赖）
from orchd.onboard.lifecycle.guards import (
    _guard_cross_worktree_dirty,
    _guard_declared_diff,
    _guard_out_of_scope,
    _guard_zero_residual,
)
# 同任务迁移的 regression 域（直接导入子模块，避免循环依赖）
from orchd.onboard.lifecycle.regression import (
    _full_regression_enabled,
    _maybe_full_regression,
    _has_engine_files,
)

# 模块级常量：迁移自 orchd/onboard.py（原行号 120-123）
_VERIFY_TIMEOUT = 120
_FULL_REGRESSION_TIMEOUT = 300


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

    # 7) 强切回默认分支（写事件前，task-done-switch-main）
    checked_out_main = None
    if project_root:
        # 延迟导入避免循环依赖
        from orchd.onboard import _checkout_default_strict
        checked_out_main = _checkout_default_strict(project_root)

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


def _done_precheck(
    store: Store,
    tasks: list[dict[str, Any]],
    task_id: str,
    agent_id: str,
    project_root: Path | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """S-A2 阶段 1：无锁预校验（快速失败）。

    仅要求任务处于 claimed 状态，不比对 caller 指纹与 claimed_by——宿主会话身份漂移
    场景下，done 按认领者记账（见锁内 author_mismatch）。返回 task_def /
    files_to_edit / degraded_guards（门禁降级登记表，随阶段透传、收尾统一挂响应）。
    """
    task_def = None
    for t in tasks:
        if t.get("id") == task_id:
            task_def = t
            break
    if task_def is None:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not found in task list",
            [{"task_id": task_id}],
        )
    files_to_edit: list[str] = list(task_def.get("files_to_edit", []))
    degraded_guards: list[dict[str, Any]] = []

    state = store.replay()
    ts = state.get(task_id)
    if not ts or ts.status != "claimed":
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: task '{task_id}' not in claimed state",
            [{"task_id": task_id, "expected": "claimed", "actual": ts.status if ts else "pending"}],
        )

    if project_root:
        _guard_done_branch(
                project_root,
                task_id=task_id,
                orchd_dir=store.orchd_dir,
                agent_id=agent_id,
                degraded=degraded_guards,
            )

    return task_def, files_to_edit, degraded_guards


def _check_base_freshness(
    project_root: Path | None,
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> None:
    """done 前置基线新鲜度闸口（task-done-base-freshness）。

    堵当前缺口：main 已持续推进（本仓库 main 已 297+ completed）时，任务分支基线与
    main 漂移。既有 :func:`_reconcile_before_verify`（挂载点 ① 的 apply=True 落地对账）
    只在「main 自 merge-base 推进且触及本任务改动文件」时才落到 merge-tree 探测冲突，
    且 apply=True 语义是在任务 worktree **落地合并并保留现场**供实现者解决——对一个
    「先要阻断」的闸口而言太重。

    本闸口与 reconcile 同源判定（复用 :func:`reconcile_with_main`，apply=False 纯只读
    探测，不落地、不动工作区、不建 MERGE_HEAD），但语义收紧为**只拦真正冲突**：

    - main 未推进 / 任务分支不存在 / 无文件交集 / 可干净合并 → **放行**（由 code
      APPROVED 的 merge 兜底；可干净快进的任务分支不阻断，是为避免 deadlock——
      实现者无合法手段在 done 前手动 merge main，落后但无冲突就应允许 done）；
    - main 推进且与 task 实际改动**同文件冲突** → 阻断，返回含「请先 merge main 并
      重跑 verify」hint 的 E015，早发现避免 review 期 E015。

    git 故障等异常 → best-effort 降级为 degraded_guard，不阻断 done（零回归）。
    仅只读探测，不产生任何 git 写操作；与 reconcile 互补——本闸口聚焦「冲突即拦」，
    apply=True 对账聚焦「已阻断后落地解决现场」。
    """
    if project_root is None:
        return
    from orchd.gitops_ops import reconcile_with_main as _reconcile_with_main

    # 任务分支不存在（flat 降级 / 未建独立分支）→ 无对账对象，放行
    # （reconcile_with_main 内部对 no_branch 返回 checked=False，先快速跳过省开销）
    probe = _probe_task_branch(str(project_root), task_id)
    if not probe:
        return
    try:
        rec = _reconcile_with_main(project_root=project_root, task_id=task_id, apply=False)
    except Exception as exc:  # reconcile 本不抛，防御性收纳
        degraded_guards.append({
            "guard": "base_freshness",
            "task_id": task_id,
            "error": str(exc),
            "hint": (
                "基线新鲜度判定异常降级（best-effort），done 继续；"
                "如反复出现请检查仓库 git 状态"
            ),
        })
        return
    # 未探测(checked=False)或未冲突(clean=True) → 放行
    if not rec.get("checked") or rec.get("clean", True):
        return
    # 真正冲突：main 推进且与本任务文件同路径冲突 → 阻断 early
    raise OrchdError(
        ErrorCode.E015,
        "stale_base: 任务分支落后 main 且与 main 存在文件冲突",
        [{
            "task_id": task_id,
            "branch": f"task/{task_id}",
            "files": rec.get("files") or [],
            "hint": (
                "任务分支落后 main 且同文件冲突，请先 merge main 解决冲突并重跑 "
                "verify；完成后重试 done，避免带陈旧基线提交后续 review 期暴露 drift"
            ),
        }],
    )


# 形态 A 的基线 ref 候选（按序解析）：远端默认分支优先，其后是常见本地默认分支名。
# **不硬编码 main**——宿主默认分支为 master（或改名 / 浅克隆无 main）时，硬编码会让形态 A
# 静默失效且无痕（task-e039-escape-hatch-bypass-fix，pass6 Q-2；违反 L-12「不可用即结构化
# 上报」）。
_BASELINE_REF_CANDIDATES = ("main", "master")


def _resolve_baseline_ref(project_root: Path) -> str | None:
    """解析形态 A 的基线 ref（``origin/HEAD`` → ``main`` → ``master``），全失败返回 ``None``。

    ``git symbolic-ref refs/remotes/origin/HEAD`` 指向远端默认分支（形如
    ``refs/remotes/origin/main``），是「本任务从哪条线分出」最权威的线索；远端信息缺失时
    回落本地 ``main`` / ``master``。候选须能 ``rev-parse`` 到 commit 才算命中。
    """
    candidates: list[str] = []
    try:
        head = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if head.returncode == 0 and head.stdout.strip():
            ref = head.stdout.strip()
            if ref.startswith("refs/remotes/"):
                candidates.append(ref[len("refs/remotes/"):])
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.extend(_BASELINE_REF_CANDIDATES)
    for candidate in candidates:
        try:
            probe = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            return candidate
    return None


def _uncommitted_changes(project_root: Path) -> list[str]:
    """任务工作树内已跟踪文件的未提交改动（best-effort，取不到返回空列表）。

    用于共享入口门禁的「实际改动」清单——引擎 auto-commit 发生在 done 靠后阶段，故
    done 判定时刻工作树可能带未提交改动。失败按空列表处理：调用方的 fail-open 口径
    由 :func:`_guard_shared_entry_coverage` 的 docstring 说明（兜底=越界/声明门禁）。
    """
    try:
        from orchd.gitops import list_tracked_changes

        return list(list_tracked_changes(project_root) or [])
    except Exception:
        return []


def _guard_shared_entry_coverage(
    project_root: Path,
    task_def: dict[str, Any],
    task_id: str,
    files_to_edit: list[str],
    degraded_guards: list[dict[str, Any]] | None = None,
) -> None:
    """共享入口改动的 verify 覆盖门禁（E039，task-shared-entry-verify-gate）。

    两条判定规则，同一错误码（救济路径一致：补 verify_command 后重试），由 details.rule 区分：

    - ``registry``：任务分支**实际改动** ∩ 登记表（:mod:`orchd.shared_entries` 的
      ``SHARED_ENTRY_TESTS``）非空时，登记表为该入口指定的既有测试文件必须全部出现在
      ``verify_command`` 的 pytest 目标里；
    - ``global_shared_file``（形态 A，task-global-shared-file-verify-rule）：改动
      ``GLOBAL_SHARED_FILES`` 里的**全局共享文件**（如 ``tests/conftest.py``，影响面无法
      枚举）时，pytest 目标中至少要有**一个基线既有**（本任务动手前已存在于基线分支，基线
      由 :func:`_resolve_baseline_ref` 解析）的测试文件——全量 pytest 禁入 verify_command，
      形态 A 是预算约束下可实现的弱约束替代。

    「实际改动」= **分支已提交 diff ∪ 工作树未提交改动**（task-shared-entry-gate-coverage-
    hardening）。只看已提交 diff 有实测漏检：引擎直到本链路靠后阶段
    （:func:`_commit_and_verify_integrity` → ``ensure_committed``）才 auto-commit，故
    「提交了无关文件、把共享入口留在工作树未提交」的任务会整段逃出视野（实测同仓库
    ``task_branch_files=['other.py']`` 而 ``list_tracked_changes=['orchd/cli/__init__.py']``）。

    为什么这里 fail-open（同族其它守卫多为 fail-closed）：本门禁防「漏跑既有测试」，而
    「未触及入口」与「改动清单取不到」在 best-effort 的
    :func:`orchd.worktree.task_branch_files` 下不可区分——取不到就阻断会把 flat
    布局等正常任务卡死。故取不到改动清单时退化为用声明的 ``files_to_edit`` 判定（声明
    通常覆盖实际改动），两者皆空则放行；形态 A 的基线 ref 全不可解析时同样跳过（fail-open），
    但**留痕不静默**（task-e039-escape-hatch-bypass-fix，pass6 Q-2）：记 ``degraded_guards``
    （E030 / not_applicable）。该 fail-open 的兜底前提=越界 / 声明门禁自身 fail-closed，锁定
    用例见 ``tests/test_done.py::TestDoneGuardFailClosed``。

    空 ``verify_command``（task-e039-gate-self-injury-fix，pass5 N-4）：不静默放行——改动
    命中受管入口时记 ``degraded_guards``（E030 / not_applicable），把「门禁没在守」变成可
    检索事实；仍不阻断（存量 / 纯文档任务可能本就没有 verify_command）。
    """
    verify_cmd = str(task_def.get("verify_command") or "")
    changed: list[str] = []
    if project_root:
        from orchd.worktree import task_branch_files

        try:
            changed = list(task_branch_files(Path(project_root), task_id))
        except Exception:
            # diff 基建故障（如 git 子进程异常）⇒ 退化为声明判定（fail-open）：
            # 「取不到 diff」与「未触及入口」不可区分，且该故障场景下越界/声明门禁
            # 自身会 fail-closed 阻断 done（见 TestDoneGuardFailClosed），本门禁不得
            # 抢先抛出与门禁语义无关的裸异常。
            changed = []
        else:
            # 未提交改动同样计入（见 docstring 实测漏检）；取失败时不影响已提交部分。
            changed += _uncommitted_changes(Path(project_root))
    changed = changed or list(files_to_edit or [])
    if not verify_cmd:
        # 空 verify ⇒ 覆盖门禁无从判定。**不得静默放行**（pass5 N-4）：若改动确实命中受管
        # 入口（登记表 / 全局共享文件），记结构化降级（E030 / not_applicable）供审计与
        # doctor 按码检索；不阻断——存量与纯文档任务可能本就没有 verify_command，阻断会卡死
        # 正常流转（缺 verify_command 的代码类任务已在注册期被 validate 阻断）。
        managed = set(SHARED_ENTRY_TESTS) | set(GLOBAL_SHARED_FILES)
        touched = sorted({str(p).replace("\\", "/") for p in changed} & managed)
        if touched:
            record_degraded_guard(
                degraded_guards,
                guard_name="shared_entry_coverage",
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason="verify_command 为空 ⇒ 共享入口覆盖门禁无从判定",
                context={"task_id": task_id, "touched": touched},
                # 提示只描述动作，不写 amend 旗标串：补登命令的单一生成源是 guide.py
                # （禁止手写 amend 命令串，见 task-amend-decl-patch-channel）。
                hint="该任务缺少 verify_command，覆盖门禁无从判定；补齐自检命令后重试 done",
            )
        return

    gaps = missing_shared_entry_tests(changed, verify_cmd)
    rule = "registry"
    if not gaps:
        # 形态 A 需要读 git 基线（基线分支上是否存在目标文件）⇒ 无 project_root 时无法判定，
        # 按 fail-open 跳过（口径见 docstring）；基线 ref 不可解析时由谓词侧留痕。
        baseline_gaps = (
            missing_baseline_test_coverage(
                changed,
                verify_cmd,
                _nogit_manifest_probe(Path(project_root), task_id, degraded_guards)
                or _baseline_test_predicate(Path(project_root), degraded_guards),
            )
            if project_root
            else None
        )
        if baseline_gaps is None:
            return
        if not baseline_gaps:
            return
        gaps, rule = baseline_gaps, "global_shared_file"

    missing = []
    for gap in gaps:
        required = gap.get("required_test")
        token = (
            f"{gap['shared_entry']} → {required}"
            if required
            else f"{gap['shared_entry']} → 至少一个基线既有测试"
        )
        if token not in missing:
            missing.append(token)
    # hint 里的补登命令由 guide 单一生成（禁止手写 amend 命令串）；主工作树路径失败时
    # 退化为不带 cd 前缀的形态（amend 本就只能在主工作树执行，提示仍可读）。
    from orchd.guide import amend_mainwt_command, amend_patch_cmd

    placeholder = "<把缺失的登记测试加入 pytest 目标后的命令>"
    patch_cmd = amend_patch_cmd(task_id, verify=placeholder)
    if project_root:
        try:
            from orchd.gitops import main_worktree_root

            patch_cmd = amend_mainwt_command(
                task_id, main_worktree_root(Path(project_root)), verify=placeholder
            )
        except Exception:  # noqa: BLE001 - hint 构造 best-effort，不掩盖门禁的阻断
            patch_cmd = amend_patch_cmd(task_id, verify=placeholder)

    if rule == "registry":
        message = "shared_entry_verify_gap: verify_command 未覆盖共享入口的登记测试"
        hint = (
            "共享入口被改动时，其既有断言必须随任务一起跑（否则静默变红入库）；"
            f"缺失 {', '.join(missing)}；补登命令 {patch_cmd}"
        )
    else:
        message = "shared_entry_verify_gap: 全局共享文件改动未覆盖任一基线既有测试"
        hint = (
            "全局共享文件影响面无法枚举（改它会改变其它测试文件的行为），故要求在 pytest "
            "目标中至少包含一个基线既有（本任务动手前已存在于基线分支）的测试文件——"
            "全量 pytest 禁入 verify_command，形态 A 是预算约束下的弱约束替代；"
            f"缺失 {', '.join(missing)}；补登命令 {patch_cmd}"
        )
    raise OrchdError(
        ErrorCode.E039,
        message,
        [{
            "task_id": task_id,
            "rule": rule,
            "gaps": gaps,
            "verify_command": verify_cmd,
            "hint": hint,
        }],
    )


def _nogit_manifest_probe(
    project_root: Path,
    task_id: str | None,
    degraded_guards: list[dict[str, Any]] | None,
) -> BaselineTestProbe | None:
    """无 git 等价：以 **claim 时建立的主目录基线快照**判定「基线既有」（task-nogit-guard-parity）。

    形态 A 的基线在 git 模式下由 ``git ls-tree <基线 ref>`` 判定；无 git 单目录模式下
    分支/ref 不存在，此前整体降级为「基线 ref 不可解析」⇒ 形态 A 门禁在无 git 下**完全
    不生效**。等价实现：claim 已对主目录取基线快照（``base`` manifest，见
    ``orchd.nogit.take_snapshot``），「基线既有」⇔ 该路径出现在 base manifest 中。

    Returns:
        - 非无 git orchd 项目 / 无 ``task_id`` → ``None``（调用方走既有降级）；-
        - 有基线快照 → 谓词（manifest 命中谓 True，否则 False）；
        - 无基线快照（claim 未建立）→ 恒 ``None`` 谓词**并留痕**（reason 与 git 侧的
          「基线 ref 不可解析」可区分，便于定位是快照缺失还是 ref 缺失）。
    """
    if not task_id:
        return None
    from orchd.nogit import is_nogit_project, read_manifest, snapshot_dir

    if not is_nogit_project(project_root):
        return None
    base = read_manifest(snapshot_dir(Path(project_root), task_id, "base"))
    if base is None:
        record_degraded_guard(
            degraded_guards,
            guard_name="shared_entry_coverage",
            status=GUARD_STATUS_NOT_APPLICABLE,
            reason="无 git 且无 claim 基线快照（main-base manifest 缺失）：形态 A 基线不可判定",
            context={
                "guard_rule": "global_shared_file",
                "task_id": task_id,
                "project_root": str(project_root),
            },
            hint=(
                "无 git 单目录模式的形态 A 基线来自 claim 时建立的主目录快照；"
                "本任务缺少 base 快照 ⇒ 覆盖判定被跳过。请重跑 claim 建立基线后重试 done"
            ),
        )
        return lambda path: None
    baseline_paths = {str(p).replace("\\", "/") for p in base}
    return lambda path: str(path).replace("\\", "/") in baseline_paths


def _baseline_test_predicate(
    project_root: Path, degraded_guards: list[dict[str, Any]] | None = None
) -> BaselineTestProbe:
    """构造「文件在基线分支中已存在」的判定谓词（形态 A 用）。

    用 ``git ls-tree --name-only <基线 ref> -- <path>`` 的**输出**而非退出码判定：输出非空 ⇒
    基线既有；输出空 ⇒ 非基线（本任务新建，退出码仍为 0）；rc≠0（非 git 仓库 / ref 不可
    解析 / git 不可用）⇒ ``None``（不可判定，调用方 fail-open 跳过）。

    兼容「修改既有测试文件」：只要该文件在基线上存在即计为基线既有——形态 A 排除的只有
    「本任务新建的文件」，不是「本任务改过的文件」。

    **无 git 等价（task-nogit-guard-parity）**：无 git orchd 项目的基线由
    :func:`_nogit_manifest_probe` 提供（claim 时主目录基线快照），调用方
    （:func:`_guard_shared_entry_coverage`）在其返回非 ``None`` 时优先采用，故本函数
    **签名与调用口径零变化**（既有 monkeypatch 用例不受影响）。

    基线 ref 全不可解析（``origin/HEAD`` / ``main`` / ``master`` 均失败）时**留痕不静默**
    （task-e039-escape-hatch-bypass-fix，pass6 Q-2）：记 ``degraded_guards``（E030 /
    not_applicable）后返回恒 ``None`` 的谓词（调用方按既有 fail-open 口径跳过）。
    """
    ref = _resolve_baseline_ref(project_root)
    if ref is None:
        record_degraded_guard(
            degraded_guards,
            guard_name="shared_entry_coverage",
            status=GUARD_STATUS_NOT_APPLICABLE,
            reason="形态 A 基线 ref 不可解析（origin/HEAD / main / master 均失败）",
            context={"guard_rule": "global_shared_file", "project_root": str(project_root)},
            hint="形态 A 覆盖判定被跳过；确认仓库默认分支名或补 fetch 远端引用后重试 done",
        )
        return lambda path: None

    def is_baseline(path: str) -> bool | None:
        try:
            proc = subprocess.run(
                ["git", "ls-tree", "--name-only", ref, "--", path],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return bool(proc.stdout.strip())

    return is_baseline


def _probe_task_branch(workdir: str, task_id: str) -> bool:
    """任务分支存在性（best-effort）：不存在返回 False（由调用方放行）。"""
    import subprocess

    branch = f"task/{task_id}"
    try:
        proc = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return True  # 探不明一律当作存在，交由 reconcile 判定


def _reconcile_before_verify(
    project_root: Path | None,
    task_id: str,
    degraded_guards: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """done 前置对账（task-done-reconcile-main 挂载点 ①）。

    与 main 对账把冲突左移到实现者手里：main 自本任务 merge-base 以来推进且触及
    本任务改动文件时，冲突在 done 阶段即被检出（原本要等 code APPROVED 的 merge
    才爆发，代价是两轮审查全部作废——实测 task-test-cli-split-domains 4h15m / 1 文件）。

    与 reviewer 认领挂载点**共用同一函数与同一触发条件**
    （:func:`orchd.gitops_ops.reconcile_with_main`，防双写漂移），区别仅在
    ``apply=True``：冲突时在任务 worktree 内保留合并现场（``MERGE_HEAD``）供实现者
    直接解决，并以权威真源（``unmerged_paths``）校正冲突清单。

    冲突 → 抛 E015（附真实冲突清单 + 可执行指引）；git 不可用等异常 → best-effort
    降级为 degraded_guard，不阻断 done（零回归）。
    """
    if project_root is None:
        return None
    try:
        from orchd.gitops_ops import reconcile_with_main

        rec = reconcile_with_main(project_root, task_id, apply=True)
    except Exception as exc:
        degraded_guards.append({
            "guard": "reconcile_with_main",
            "task_id": task_id,
            "error": str(exc),
            "hint": "done 前置对账异常降级（best-effort），done 继续；"
                    "如反复出现请检查仓库 git 状态",
        })
        return None
    if rec.get("reason") != "conflict":
        return rec
    raise OrchdError(
        ErrorCode.E015,
        "merge_conflict: 任务分支与 main 对账冲突（done 前置）",
        [{
            "task_id": task_id,
            "conflict_files": rec.get("files", []),
            "worktree": rec.get("worktree"),
            "reason": rec.get("reason"),
            "hint": rec.get("action"),
        }],
    )


def _run_verify(
    store: Store,
    task_def: dict[str, Any],
    task_id: str,
    project_root: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """S-A2 阶段 3：锁外执行 verify_command，返回 verify_record。

    返回二元组 (verify_record, early_done)：
    - verify_record：verify 通过（或无 verify_command）时的结果记录，供 DONE 入库；
    - early_done：假失败消除命中（本次 attempt 的 DONE 已并发落地、任务已离开
      claimed）时，需提前返回的成功响应；否则为 None（继续主流程）。

    P2：verify 结果摘要随 DONE 事件入库（verify_record），供 review claim 注入引用。
    """
    verify_cmd = task_def.get("verify_command")
    if not verify_cmd or not project_root:
        return None, None

    # B3（ROADMAP 1.1.1）：verify 超时可配置——任务级 verify_timeout_seconds 可选字段，
    # 缺失回退引擎默认 _VERIFY_TIMEOUT=120。
    verify_timeout = task_def.get("verify_timeout_seconds") or _VERIFY_TIMEOUT

    from orchd.spec import verify_command_dangerous_reasons

    _dangerous = verify_command_dangerous_reasons(verify_cmd)
    if _dangerous:
        raise OrchdError(
            ErrorCode.E027,
            "verify_command 含 shell 注入风险，拒绝执行",
            [{
                "task_id": task_id,
                "reasons": _dangerous,
                "hint": (
                    "verify_command 仅允许 pytest / python -c / exit 等，"
                    "禁止命令替换/管道/命令链/重定向/危险命令"
                ),
            }],
        )

    started = time.monotonic()
    try:
        # 通过包模块获取 run_shell（使测试 monkeypatch 传导到本模块）
        from orchd.onboard import run_shell as _run_shell
        result = _run_shell(verify_cmd, str(project_root), verify_timeout)
        elapsed = round(time.monotonic() - started, 1)
        if result.returncode != 0:
            early = _verify_failure_early_done(store, task_id, elapsed)
            if early is not None:
                early["verify"] = {
                    "returncode": result.returncode,
                    "elapsed_seconds": elapsed,
                }
                return None, early
            raise _verify_fail_error(verify_cmd, task_id, result, elapsed)
        verify_record = {
            "ok": True,
            "exit_code": result.returncode,
            "elapsed_seconds": elapsed,
            "output_summary": _verify_output_summary(result.stdout, result.stderr),
            # task-done-root-resolution AC4：记录 verify 实际执行位置（cwd），
            # reviewer 可仅凭 DONE 事件的 verify 摘要辨识 verify 跑在哪个工作树。
            "cwd": str(project_root),
        }
        return verify_record, None
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - started, 1)
        partial_out = _decode_subprocess_output(
            (exc.stdout or b"")[:300] if hasattr(exc, "stdout") else b""
        )
        early = _verify_failure_early_done(store, task_id, elapsed)
        if early is not None:
            early["note"] = "timeout_but_done_already_written"
            early["verify"] = {
                "timeout": verify_timeout,
                "elapsed_seconds": elapsed,
                "partial_stdout": partial_out,
            }
            return None, early
        raise _verify_timeout_error(verify_cmd, task_id, verify_timeout, elapsed,
                                    partial_out)


def _verify_fail_error(
    verify_cmd: str,
    task_id: str,
    result: Any,
    elapsed: float,
) -> OrchdError:
    """构造 verify 非零退出的 E014 错误（含诊断增强出口）。"""
    return OrchdError(
        ErrorCode.E014,
        f"verify_command_failed: exit code {result.returncode} after {elapsed}s",
        [{
            "command": verify_cmd,
            "returncode": result.returncode,
            "elapsed_seconds": elapsed,
            "stderr": _decode_subprocess_output(result.stderr)[:500],
            "stdout": _decode_subprocess_output(result.stdout)[:300],
            "hint": (
                "verify 失败可能因断言不匹配或环境问题；若为 pytest 超长执行，"
                "按 SKILL.md 自检约定改用模块定向 verify_command；"
                "若在 Windows 上 verify 失败，请确认已安装 Git Bash"
                "（verify_command 以 POSIX 语法经 Git Bash 执行）"
            ),
        }],
    )


def _verify_timeout_error(
    verify_cmd: str,
    task_id: str,
    verify_timeout: int,
    elapsed: float,
    partial_out: str,
) -> OrchdError:
    """构造 verify 超时的 E014 错误（含超时诊断与定向 verify 指引）。"""
    return OrchdError(
        ErrorCode.E014,
        f"verify_command_failed: timeout after {verify_timeout}s"
        f"（实际执行 {elapsed}s，未完成）",
        [{
            "command": verify_cmd,
            "timeout": verify_timeout,
            "elapsed_seconds": elapsed,
            "partial_stdout": partial_out,
            "hint": (
                "verify 超时通常是全量 pytest 累计耗时超上限：按 SKILL.md 自检约定"
                "改用模块定向 verify_command（python -m pytest tests/test_<模块>.py "
                "--basetemp=...），并确认测试不触发沙箱拦截"
            ),
        }],
    )


def _verify_failure_early_done(
    store: Store,
    task_id: str,
    elapsed: float,
) -> dict[str, Any] | None:
    """假失败消除：verify 失败/超时但本次 attempt 的 DONE 已并发落地 → 复用成功。

    仅当任务已离开 claimed（本次 done 已写完 DONE）才复用；rework 场景（仍 claimed，
    ledger 残留上一次旧 DONE）是真实失败 → 返回 None（由调用方抛 E014），避免
    误复用上一次 DONE 掩盖真实失败（2026-08-12 实踩：rework 误触发）。

    注意：此处在 verify 期间实时扫描 ledger——并发落地的 DONE 不在 done 开头的
    缓存内（H2 缓存仅覆盖 claim/request/review_submit 等静态查询场景）。
    """
    fresh_state = store.replay()
    cur_ts = fresh_state.get(task_id)
    if cur_ts is not None and cur_ts.status != "claimed":
        prior_done = _find_last_done_event(store, task_id)
    else:
        prior_done = None
    if prior_done is None:
        return None
    return {
        "done": True,
        "task_id": task_id,
        "status": "done",
        "attempt_count": prior_done.get("attempt_count", cur_ts.attempt_count + 1),
        "note": "verify_failed_but_done_already_written",
        "ledger_timestamp": prior_done.get("timestamp"),
        "event_id": prior_done.get("event_id"),
    }


def _commit_and_verify_integrity(
    store: Store,
    task_def: dict[str, Any],
    task_id: str,
    agent_id: str,
    project_root: Path | None,
    files_to_edit: list[str],
    changes_description: str,
    degraded_guards: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """S-A2 阶段 4：锁外自动提交 + 4 个完整性门禁（均 fail-closed）编排器。

    自动提交范围限定任务声明的 files_to_edit，失败/跳过不影响状态机；随后按序执行
    三个独立门禁函数（声明 diff / 零残留 / 越界）+ L1 干净校验。每个门禁跑不起来时
    fail-closed 阻断并留痕。对外仅返回 commit_result（与重构前一致）。
    """
    commit_message = changes_description or task_def.get(
        "name", f"orchd: done {task_id}"
    )
    # task-done-exempt-files-commit：自动提交范围扩展为 files_to_edit ∪ exempt_files，
    # 使 exempt_files 改动（如配套测试）随 done 自动提交进入任务分支 diff，不再被
    # _guard_clean_workspace 以 E017 阻断。扩展仅限提交范围；declared_diff / zero_residual
    # / out_of_scope 门禁仍以 files_to_edit 为准，与既有语义零回归。
    commit_files = list(dict.fromkeys([*files_to_edit, *task_def.get("exempt_files", [])]))
    commit_result = _done_auto_commit(project_root, commit_files, commit_message, task_id)

    # task-commit-timeout-failure-surface AC4：自动提交硬失败（commit_timeout /
    # commit_failed）时，后续干净守卫抛的 E017 附引擎提交诊断（reason/message），
    # hint 明示「自动提交失败」，不再仅呈现裸 dirty_workspace 语义。
    try:
        _guard_declared_diff(project_root, task_id, files_to_edit, degraded_guards)
        _guard_zero_residual(project_root, task_id, files_to_edit, degraded_guards)
        _guard_out_of_scope(project_root, task_def, task_id, degraded_guards)

        if project_root:
            _guard_clean_workspace(
                project_root,
                command="done",
                orchd_dir=store.orchd_dir,
                agent_id=agent_id,
                degraded=degraded_guards,
            )
    except OrchdError as exc:
        hard_failed = bool(
            commit_result
            and commit_result.get("reason") in ("commit_timeout", "commit_failed")
        )
        if hard_failed and exc.code is ErrorCode.E017:
            base_detail = exc.details[0] if exc.details else {}
            raise OrchdError(
                ErrorCode.E017,
                exc.message,
                [{
                    **base_detail,
                    "auto_commit_reason": commit_result.get("reason"),
                    "auto_commit_message": commit_result.get("message"),
                    "auto_commit_timeout_seconds": commit_result.get("timeout_seconds"),
                    "hint": (
                        "自动提交失败（"
                        + str(commit_result.get("reason"))
                        + "），工作区改动未落盘；请检查 git 提交错误后重试 done"
                    ),
                }],
            )
        raise

    return commit_result


def _done_auto_commit(
    project_root: Path | None,
    files_to_edit: list[str],
    commit_message: str,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """锁外 best-effort 自动提交（verify 通过后、写 DONE 事件前）。

    提交范围限定任务声明的 files_to_edit，失败/跳过不影响状态机。

    A0b / 单目录无 git（task-nogit-single-dir-pivot）：「提交」= 推进主目录
    committed 快照（:func:`orchd.nogit.take_snapshot`），不经 git commit；
    返回结构与 ``ensure_committed`` 兼容。判据为主目录基线快照存在（claim 已建立）。
    """
    if not project_root:
        return None
    if task_id:
        from orchd.nogit import git_available as _git_avail, take_snapshot

        if not _git_avail(Path(project_root)):
            info = take_snapshot(Path(project_root), task_id, "committed")
            return {
                "performed": True,
                "reason": "committed",
                "message": "nogit: committed snapshot advanced",
                "files": info["files"],
            }
    if not files_to_edit:
        return None
    return ensure_committed(project_root, files_to_edit, commit_message)


def _write_done_event(
    store: Store,
    task_id: str,
    task_def: dict[str, Any],
    agent_id: str,
    changes_description: str,
    concerns: str | None,
    verify_record: dict[str, Any] | None,
) -> dict[str, Any]:
    """S-A2 阶段 6：锁内 TOCTOU 二次校验 + 写 DONE/REVIEW_READY + checkpoint。

    记账一律以任务认领者 claimed_by 为准，忽略 caller 当前指纹（宿主会话身份
    漂移时仍正常放行并按认领者记账）。返回组装所需字段，不在本函数拼完整响应。
    """
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        ts = state.get(task_id)
        if not ts or ts.status != "claimed":
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: TOCTOU - task state changed during verify",
                [{"task_id": task_id}],
            )

        claimed_by = ts.claimed_by
        author_mismatch = claimed_by != agent_id
        attempt_count = ts.attempt_count + 1
        done_event = _make_event(
            task_id, claimed_by, "DONE",
            changes_description=changes_description,
            attempt_count=attempt_count,
        )
        if concerns:
            done_event["concerns"] = concerns
        if verify_record is not None:
            done_event["verify"] = verify_record
        store.append_event(done_event)

        # 自动 REVIEW_READY（review-unify-r2）
        if resolve_review_mode(store.orchd_dir) == "unified":
            review_type: str | None = None
            review_event = _make_event(task_id, claimed_by, "REVIEW_READY")
        else:
            # 延迟导入避免循环依赖
            from orchd.onboard import _load_config_blocked, _is_doc_single_stage
            blocked_config = _load_config_blocked(store)
            review_type = "code" if _is_doc_single_stage(
                task_def.get("files_to_edit", []), blocked=blocked_config
            ) else "spec"
            review_event = _make_event(task_id, claimed_by, "REVIEW_READY", review_type=review_type)
        store.append_event(review_event)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    return {
        "claimed_by": claimed_by,
        "author_mismatch": author_mismatch,
        "attempt_count": attempt_count,
        "done_event": done_event,
        "review_type": review_type,
        "integrity_warnings": integrity_warnings,
    }


def _done_lessons_hook(
    store: Store,
    task_id: str,
    verify_record: dict[str, Any] | None,
    skip_lesson_review: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """S-A2 阶段 7：经验回灌 done 收尾 hook（设计 §8.6）。

    检测本任务 staged 暂存建议 → resolved 交叉验证 + detected_at 打点；硬门禁
    （require_review=true）下注入 next_action=await_review（收尾挂起待审）。
    --skip-lesson-review 或 lessons 关闭 → 跳过，暂存保留不丢失。
    best-effort：hook 任何异常绝不阻断 done 收尾。
    """
    if skip_lesson_review:
        return None, False
    lessons_summary: dict[str, Any] | None = None
    lessons_require_review = False
    try:
        from orchd.lessons import (
            is_lessons_enabled,
            load_lessons_config,
            run_done_lesson_hook,
        )
        if not is_lessons_enabled(store.orchd_dir):
            return None, False
        verify_passed = verify_record.get("ok") if verify_record else None
        hook_out = run_done_lesson_hook(store.orchd_dir, task_id, verify_passed)
        if hook_out.get("has_lessons"):
            lessons_summary = hook_out
            lessons_require_review = bool(
                load_lessons_config(store.orchd_dir).get("require_review", True)
            )
    except Exception:
        pass
    return lessons_summary, lessons_require_review


def _assemble_done_result(
    *,
    task_id: str,
    attempt_count: int,
    review_type: str | None,
    done_event_id: str,
    integrity_warnings: list[Any],
    degraded_guards: list[dict[str, Any]],
    author_mismatch: bool,
    claimed_by: str,
    caller_fingerprint: str,
    full_regression: dict[str, Any] | None,
    commit_result: dict[str, Any] | None,
    checked_out_main: dict[str, Any] | None,
    lessons_summary: dict[str, Any] | None,
    lessons_require_review: bool,
) -> dict[str, Any]:
    """S-A2 阶段 8：组装 done 完整响应（含降级留痕/author_mismatch/附带字段）。"""
    result: dict[str, Any] = {
        "done": True,
        "task_id": task_id,
        "status": "done",
        "attempt_count": attempt_count,
        "review_created": {"type": review_type or "unified"},
        "event_id": done_event_id,
    }
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    if author_mismatch:
        result["author_mismatch"] = {
            "claimed_by": claimed_by,
            "caller_fingerprint": caller_fingerprint,
            "message": (
                "done 已按任务认领者 claimed_by 记账；caller 指纹与认领者不一致"
                "（宿主会话身份可能漂移），仅提示、不阻断"
            ),
        }
    if full_regression is not None:
        result["full_regression"] = full_regression
    if commit_result is not None:
        result["commit"] = commit_result
    if checked_out_main is not None:
        result["checked_out_main"] = checked_out_main
    if lessons_summary is not None:
        result["lessons"] = lessons_summary
        if lessons_require_review:
            result["next_action"] = NEXT_ACTION_AWAIT_REVIEW
            result["lesson_review_hint"] = (
                f"本任务有 {lessons_summary.get('count', 0)} 条 guidance 增补建议待审核，"
                f"收尾挂起：请运行 `orchd lesson review --task {task_id}` 确认后完成收尾"
            )
    return result