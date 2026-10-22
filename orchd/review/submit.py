"""review 提交编排入口（review_submit/_review_submit_impl），调用五段+guidance。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.ledger import Store
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
        from orchd.guide import init_routing

        _rr_orchd = Path(project_root) / ".orchd" if project_root else None
        if _rr_orchd is not None and _rr_orchd.is_dir():
            init_routing(_rr_orchd, project_root)
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
    # L3 pre-commit hook 卸载（best-effort）：code APPROVED 终态后不再需要任务级
    # hook，防止残留（与 done/retract 一致；覆盖 merge 降级/成功全部完成路径）。
    if project_root and result.get("task_status") == "completed":
        hook_uninstall(project_root)

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
