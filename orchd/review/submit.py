"""review 提交编排入口（review_submit/_review_submit_impl），调用五段+guidance。"""

from __future__ import annotations

import json

from typing import Any
from pathlib import Path

from orchd.contracts.events import STATUS_CLAIMED, STATUS_IN_REVIEW
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store
from orchd.ledger.session import (
    _session_runtime_path,
    is_session_expired,
    session_runtime_dir,
)
from orchd.gitops import guard_review_write
from orchd.gitops import hook_uninstall
from orchd.gitops import main_worktree_root
from orchd.gitops import release_session_lock_if_owned
from orchd.ledger import resolve_store_dir
from orchd.gitops import session_lock_check
from orchd.gitops import session_lock_release
from .decl_gate import _review_submit_decl_gate
from .guidance import _build_completion_guidance
from .merge_finalize import _review_submit_merge_finalize
from .merge_run import _review_submit_merge_run
from .precheck import _review_submit_precheck
from .record import _review_submit_record

# 自动压实（task-ledger-auto-compact-after-merge）：活跃 payload 阈值、硬上限、
# 前缀最小比例。
_AUTO_COMPACT_MIN_BYTES = 5 * 1024 * 1024
_AUTO_COMPACT_HARD_CAP_BYTES = 10 * 1024 * 1024
_AUTO_COMPACT_MIN_PREFIX_RATIO = 0.8


def _session_live(store: Store, session_id: str) -> bool:
    """会话 runtime 文件存在、active 且未过期（stale 认领的判据）。"""
    if not session_id:
        return False
    try:
        data = json.loads(
            _session_runtime_path(store.orchd_dir, session_id).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and bool(data.get("active"))
        and not is_session_expired(data)
    )


def _active_pool(store: Store, state: dict[str, Any]) -> list[str]:
    """活跃池：claimed / in_review；claimed 会话缺失或过期视为 stale 剔除。"""
    pool: list[str] = []
    for task_id, ts in state.items():
        if ts.status == STATUS_CLAIMED:
            if ts.claimed_session and _session_live(store, ts.claimed_session):
                pool.append(task_id)
        elif ts.status == STATUS_IN_REVIEW:
            if not ts.review_claimed_session or _session_live(
                store, ts.review_claimed_session
            ):
                pool.append(task_id)
    return sorted(pool)


def _other_live_sessions(store: Store) -> list[str]:
    """其他活跃会话（排除当前会话），用于自动压实并发门。"""
    from orchd.ledger.identity import resolve_session_identity

    current = resolve_session_identity(store.orchd_dir).get("session_id", "")
    runtime_dir = session_runtime_dir(store.orchd_dir)
    if not runtime_dir.is_dir():
        return []
    found: list[str] = []
    for path in sorted(runtime_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("active"):
            continue
        session_id = data.get("session_id") or path.stem
        if session_id != current and not is_session_expired(data):
            found.append(session_id)
    return found


def _compact_if_needed(store: Store) -> dict[str, Any]:
    """阈值/池/并发门判定；满足时压实并返回 stats，否则 skipped。"""
    from orchd.storage import read_archive_events

    store_dir = resolve_store_dir(store.orchd_dir)
    if not store.ledger_exists():
        return {"status": "skipped", "reason": "no_ledger"}
    events = store._read_ledger_lines(from_line=1)
    arch, _ = read_archive_events(store_dir)
    arch_ids = {e.get("event_id", "") for e in arch if e.get("event_id")}
    active = [
        ev for ev in events
        if not ev.get("event_id") or ev["event_id"] not in arch_ids
    ]
    active_bytes = sum(
        len(json.dumps(ev, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"))
        for ev in active
    )
    base = {"active_bytes": active_bytes, "active_events": len(active)}
    if active_bytes <= _AUTO_COMPACT_MIN_BYTES:
        return {
            "status": "skipped", "reason": "below_threshold",
            "min_bytes": _AUTO_COMPACT_MIN_BYTES, **base,
        }
    state = store.replay()
    pool = _active_pool(store, state)
    if pool and active_bytes <= _AUTO_COMPACT_HARD_CAP_BYTES:
        return {"status": "skipped", "reason": "active_pool",
                "pool": pool[:10], **base}
    live = _other_live_sessions(store)
    if live:
        return {"status": "skipped", "reason": "active_session",
                "sessions": live[:10], **base}
    try:
        stats = store.compact_archive(
            min_prefix_ratio=_AUTO_COMPACT_MIN_PREFIX_RATIO,
        )
    except OrchdError as exc:
        if exc.code is not ErrorCode.E012:
            raise
        return {"status": "skipped", "reason": "lock_busy",
                "error": str(exc), **base}
    stats.update(base)
    if stats.get("status") == "skipped":
        return stats
    if stats.get("moved_events"):
        stats["status"] = "compacted"
    else:
        stats["status"] = "skipped"
        stats["reason"] = "no_prefix"
    return stats


def _auto_compact_after_merge(
    store: Store, main_wt: Path | None = None,
) -> dict[str, Any]:
    """merge 成功后 best-effort 自动压实；异常降级为 warning，不阻断审查。

    container 布局下 code APPROVED 会先回收任务 worktree（连同其 ``.orchd``），
    此时 ``store`` 指向已删除路径；改用主工作树上的 store 读取共享账本根。
    """
    try:
        target = store
        if main_wt is not None:
            main_orchd = Path(main_wt) / ".orchd"
            if main_orchd.resolve() != Path(store.orchd_dir).resolve():
                target = Store(main_orchd)
        return _compact_if_needed(target)
    except Exception as exc:  # noqa: BLE001 - best-effort，任何失败都不改变审查结果
        return {
            "status": "warning",
            "reason": "compact_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }



def _init_review_routing(project_root: Path | None) -> None:
    """为 review 收尾 guidance 初始化路由；容器 worktree 缺 rules 时用 canonical。

    任务 worktree 的 ``.orchd/`` 可能只有 sparse 副本、没有 ``rules/``。此时
    直接 ``init_routing(local)`` 会空返回且不填缓存，后续 completion guidance
    的 ``read_for`` 会 fail-closed。优先用 canonical 主工作树 ``.orchd``（规则
    真源）；本地有 ``rules/`` 时保持原行为。任何失败静默（guidance 仍会安全回退）。
    """
    if project_root is None:
        return
    from orchd.guide import init_routing

    local_orchd = Path(project_root) / ".orchd"
    if local_orchd.is_dir() and (local_orchd / "rules").is_dir():
        init_routing(local_orchd, project_root)
        return
    from orchd.worktree import resolve_canonical_project_root

    canonical = resolve_canonical_project_root(Path(project_root))
    canonical_orchd = Path(canonical) / ".orchd"
    if canonical_orchd.is_dir() and (canonical_orchd / "rules").is_dir():
        init_routing(canonical_orchd, Path(canonical))


def review_submit(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
    authorize_reviewer_resolve: bool = False,
    rework_scope: str | None = None,
) -> dict[str, Any]:
    """提交审查结果（task-session-lock-lifecycle：异常路径也保证释放会话锁）。

    review_type 为 None 表示 unified 单阶段审查（review-unify-r2）。

    ``_review_submit_impl`` 的包装：``finally`` 中经 :func:`release_session_lock_if_owned`
    条件释放本 agent 的 session 锁（仅持有者==本 agent 才释放，幂等）。正常路径
    由 ``_review_submit_impl`` 尾部释放并写 ``session_lock_released``；异常/提前
    返回路径由本包装器的 finally 兜底，杜绝漏放锁（此前需 60min 超时 + watchdog 兜底）。
    """
    # task-guide-routing-meta：防御性重入路由（库调用无 main() 启动初始化时）。
    # .orchd 存在但 rules/ 缺失 → 跳过（保留既有缓存）；其余失败静默（响应路径
    # 不因子虚乌有而崩，缺失路由由 read_for 的 fail-closed 在使用点报错）。
    try:
        _init_review_routing(project_root)
    except Exception:
        pass
    try:
        return _review_submit_impl(
            store, tasks, agent_id, task_id, review_type, verdict, comments,
            project_root, authorize_reviewer_resolve=authorize_reviewer_resolve,
            rework_scope=rework_scope,
        )
    finally:
        if project_root:
            release_session_lock_if_owned(project_root / ".orchd", agent_id)


def _review_submit_impl(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    review_type: str | None,
    verdict: str,
    comments: str | None = None,
    project_root: Path | None = None,
    *,
    authorize_reviewer_resolve: bool = False,
    rework_scope: str | None = None,
) -> dict[str, Any]:
    """提交审查结果（APPROVED 或 CHANGES_REQUESTED）。

    review_type 为 None 表示 unified 单阶段审查（review-unify-r2）。

    锁内校验：任务必须处于 in_review 状态，审查阶段（spec/code）须匹配，
    且审查者须为当前 agent。

    spec APPROVED 自动推进到 code review；code APPROVED 先锁外 git merge，
    成功才写完成事件（任务 completed），merge 冲突则停留 in_review。
    CHANGES_REQUESTED 时任务回退 pending。
    """
    # 意图化守卫（task-14-git-policy-layer）：任意分支、不要求干净。
    # AC4（task-session-lock-degrade-observability）：此前该调用点是四个意图化封装
    # （claim / done / clean / review）中唯一**未透传 degraded** 的——会话锁降级条目
    # 只生成、不入响应，review 路径的「本次未经会话锁保护」不可见。此处补齐接线。
    degraded: list[dict[str, Any]] = []
    guard_review_write(
        project_root,
        orchd_dir=store.orchd_dir,
        agent_id=agent_id,
        degraded=degraded,
    )
    # task-14-worktree-lifecycle（AC2）：目标 root == 任务 worktree（不一致 E018，
    # 防错目录审查）。flat 单会话绑定=主工作树 → 恒通过；无绑定 → best-effort 跳过。
    if project_root:
        from orchd.worktree import guard_task_root

        guard_task_root(project_root, resolve_store_dir(store.orchd_dir), task_id, "review")

    store.acquire_lock()
    try:
        pre = _review_submit_precheck(
            store=store,
            agent_id=agent_id,
            task_id=task_id,
            review_type=review_type,
            verdict=verdict,
            comments=comments,
            project_root=project_root,
            rework_scope=rework_scope,
        )
        result, pending_code_event = _review_submit_record(
            store=store,
            agent_id=agent_id,
            task_id=task_id,
            review_type=review_type,
            verdict=verdict,
            comments=comments,
            project_root=project_root,
            rework_scope=rework_scope,
            **pre,
        )
    finally:
        store.release_lock()
    if pending_code_event is not None:
        _review_submit_decl_gate(
            project_root=project_root,
            tasks=tasks,
            task_id=task_id,
            degraded=degraded,
        )
        # 并发 merge 串行（task-14-merge-main-tree AC4）：以主工作树锁互斥，
        # 多个 code APPROVED 同时进主工作树 merge 时排队，互不干扰。
        # flat（任务 worktree == 主工作树 == 本 store）不加锁——review_submit
        # 已在开头持有并释放同一把 store 锁，此处复用会重复 acquire 死锁（零回归）。
        # task-audit-hint-show：内联审计的 report 导入必须在 merge/回收之前完成——
        # container 下终态回收会删掉任务 worktree 连带其 orchd/ 源码副本，之后再
        # 延迟导入即 ModuleNotFoundError → merge_audit 恒 skipped(no_project_root)
        # （生产 11/11 code APPROVED 实证；与 _cmd_review 的 _preimport_archive_deps
        # 同模式）。此处提前绑定，后续只用不再导入。
        _merge_audit_fn: Any = None
        try:
            from orchd.report import merge_audit as _merge_audit_impl
            _merge_audit_fn = _merge_audit_impl
        except Exception:
            _merge_audit_fn = None
        merge_lock: Any | None = None
        # task-audit-hint-show：main_wt 显式初始化为 None——原仅在
        # `if project_root:` 内赋值，无 project 上下文时后文引用即 NameError；
        # 初始化后无上下文安全降级（审计跳过 + 指引不附主工作树字段）。
        main_wt = None
        if project_root:
            main_wt = main_worktree_root(project_root)
            if main_wt != Path(project_root).resolve():
                merge_lock = Store(main_wt / ".orchd")
                merge_lock.acquire_lock()
        try:
            merge_result, auto_resolved, _nogit_single_dir = _review_submit_merge_run(
                project_root=project_root,
                task_id=task_id,
                result=result,
                degraded=degraded,
                authorize_reviewer_resolve=authorize_reviewer_resolve,
            )
            _review_submit_merge_finalize(
                result=result,
                store=store,
                tasks=tasks,
                agent_id=agent_id,
                task_id=task_id,
                project_root=project_root,
                merge_lock=merge_lock,
                main_wt=main_wt,
                _merge_audit_fn=_merge_audit_fn,
                pending_code_event=pending_code_event,
                merge_result=merge_result,
                auto_resolved=auto_resolved,
                _nogit_single_dir=_nogit_single_dir,
                review_type=review_type,
            )
        finally:
            if merge_lock is not None:
                merge_lock.release_lock()
        # task-cont-autoclean-triggers / task-ledger-auto-compact-after-merge：
        # merge 成功后的 best-effort 收尾（本任务局部清理 + 账本自动压实）。
        # 必须在 merge_lock 释放之后调用——auto_clean / compact 自身要取账本锁，
        # 持锁期内再取会自锁。target 取主工作树（任务 worktree 可能已被回收）。
        if result.get("merged") is True:
            _post_merge_followups(
                result=result,
                store=store,
                main_wt=main_wt,
                task_id=task_id,
                project_root=project_root,
            )
    # L3 pre-commit hook 卸载（best-effort）：code APPROVED 终态后不再需要任务级
    # hook，防止残留（与 done/retract 一致；覆盖 merge 降级/成功全部完成路径）。
    # task-debug-hook-active-gate：传 orchd_dir 让卸载前判活跃认领——hook 目录是
    # core.hooksPath 的全仓共享路径，其他任务仍在实现/审查时必须保留。
    if project_root and result.get("task_status") == "completed":
        hook_uninstall(project_root, orchd_dir=store.orchd_dir)

    if project_root:
        orchd_dir = project_root / ".orchd"
        lock_check = session_lock_check(orchd_dir)
        if lock_check.get("locked") and lock_check.get("agent_id") == agent_id:
            release = session_lock_release(orchd_dir)
            result["session_lock_released"] = release.get("released", False)

    # task-review-completion-guidance（AC1/AC2）：code APPROVED 且 merge 成功
    # （任务 completed）的响应附 guidance，明确下一步在主工作树执行
    # status --audit-merge；回收未成功时额外给出处置指引。
    if result.get("task_status") == "completed" and result.get("merged") is True:
        result["guidance"] = _build_completion_guidance(
            task_id, result.get("worktree_recycled"),
            main_worktree=str(main_wt) if main_wt is not None else None,
        )


    # AC4：守卫降级不静默——会话锁未持有时非空，并入响应（沿用「有降级才补字段」
    # 约定，无降级则维持既有字段集合不变）。
    if degraded:
        result["degraded_guards"] = degraded

    return result


def _post_merge_followups(
    *,
    result: dict[str, Any],
    store: Store,
    main_wt: Path | None,
    task_id: str,
    project_root: Path | None,
) -> None:
    """merge 成功后的 best-effort 收尾：本任务局部清理 + 账本自动压实。

    必须在 merge 主锁释放后调用——``auto_clean`` 与 ``compact_archive`` 都会
    自行取账本锁，持锁期内重入会自锁（E012 同进程双 fd）。两者失败均不改变
    审查结论：auto_clean 静默、compact 降级为 ``compact`` warning。

    定义置于 ``_review_submit_impl`` 之后：`tests/test_autoclean_continuity.py`
    以源码文本位置钉「trigger=\"merge\" 出现在 merge_lock.release_lock() 之后」。
    """
    if result.get("merged") is not True:
        return
    # task-cont-autoclean-triggers：merge 成功触发本任务局部清理，上限 10 项。
    # 范围 = 本任务 id（生产可达项：``review/<task-id>/`` 的 merged_review_dir，
    # 其 path/reason 都带 id）；verify tmp 的命名不含 task id，属 done 的
    # worktree 局部范围，不在此按 id 匹配——避免「看似覆盖、实则恒不命中」。
    # target 取主工作树（任务 worktree 可能已被回收）；best-effort。
    _clean_root = main_wt or project_root
    if _clean_root is not None:
        try:
            from orchd.doctor import auto_clean

            ac_merge = auto_clean(
                _clean_root,
                emit_stderr=False,
                trigger="merge",
                task_id=task_id,
            )
            if (ac_merge.get("auto_cleaned")
                    or ac_merge.get("auto_moved")
                    or ac_merge.get("manual_notice")):
                result["auto_clean"] = ac_merge
        except Exception:
            pass
    # task-ledger-auto-compact-after-merge：merge 后 best-effort 自动压实，
    # 任何失败只降级为 compact warning，不影响审查结果。
    if result.get("task_status") == "completed":
        result["compact"] = _auto_compact_after_merge(store, main_wt)
