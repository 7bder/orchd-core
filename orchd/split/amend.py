from __future__ import annotations

from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store
from orchd.spec import Master
from pathlib import Path
from typing import Any
from .amend_admission import _amend_admission
from .amend_persist import _amend_persist
from .amend_precheck import _amend_precheck
from .amend_route_tasks import _amend_route_tasks
from .log import _log_amend_guard_degrade

def amend(
    orchd_dir: Path,
    master: Master,
    store: Store,
    revise_terminal: str | None = None,
    reason: str | None = None,
    expected_head: str | None = None,
    release_lock: bool = True,
) -> dict[str, Any]:
    """增量更新 snapshot：按状态约束矩阵过滤变更。

    约束矩阵：
    - pending：可改全部字段（含 acceptance_criteria；合法 CLI 通道为
      ``amend --task <id> --acceptance-criteria ...``，整体覆写，仅 pending
      放行——task-amend-ac-pending-channel）
    - claimed：仅允许修改 verify_command / reviewers 白名单字段，其余拒绝（E007）
    - done / in_review（review 组）：仅允许修改 reviewers
    - completed / cancelled：默认拒绝；``revise_terminal`` 指向该任务时，纯文本规格
      字段（acceptance_criteria / brief / name / deliverables）可修订并写 AMEND 审计
      事件，其余字段仍逐项走既有通道判定（task-terminal-spec-revision-channel）

    review 阶段归一化比对：对于 done / in_review 状态的任务，先将新定义的
    reviewers 字段还原为旧值，再与旧定义做全量比对；若相等则说明仅有
    reviewers 发生了变更，允许通过；否则拒绝（提示"仅允许修改 reviewers"）。

    新增任务始终允许。删除任务不阻止，但在摘要中报告 removed_tasks。

    红线 7（task-redline7-amend-refuse，roadmap:constraint-hardening）：
    amend 只在 default（main）分支执行。在非 main 分支上调用 amend 直接
    拒绝注册（抛出 E007），而非降级为"仅不提交"——否则任务分支内仍会
    注册任务并改写 master，污染 master 注册来源。分支判定仅在 amend 阶段
    强制：init / validate / status 等只读或冷启动命令不受限。

    Args:
        orchd_dir: ``.orchd/`` 目录（快照与账本根）。
        master: 待生效的 master 定义。
        store: 账本 Store（本函数内自持锁）。
        revise_terminal: 终态规格文本修订通道的开关——目标 task_id；仅该任务放行
            文本字段修订（须为终态任务，否则 E007）。
        reason: 修订理由（``revise_terminal`` 非空时必填、非空白，写入审计事件）。

    Returns:
        变更摘要（含 ``terminal_spec_revisions``：本次文本修订的 fields 与 rationale；
        以及条件字段 ``degraded_guards``：准入锁 / HEAD 漂移检测 best-effort 降级时
        非空，逐条给出 guard / reason / error / hint，供 agent 判断「本次未经准入锁
        保护」而非误读为「已确认无并发注册」）。
        ``release_lock=False`` 时另含 ``_intake_lock``（调用方提交后释放；仅内部
        通道使用，调用方须在返回响应前 pop 掉，不外泄）。

    并发语义（task-concurrent-amend-lost-update）：``expected_head`` 为调用方读
    master 时的 HEAD。持锁后比对，不一致 → E007 ``stale_base`` fail-closed（重试
    即重读最新 base，自然收敛）。读与 dry-run（数十秒）发生在锁外，仅靠准入锁
    串行化不够——两 amend 可先后读同一 base 再先后写，后写者静默覆盖（丢失更新）；
    ``head_drift_check`` 判的是工作区相对自分支，不覆盖此形，故独立比对。
    ``expected_head=None``（老调用 / 非 git）→ 跳过比对。
    """
    (orchd_dir, tasks, modules, project_root, default_branch, errors,
     updated_tasks, whitelisted_updates, new_tasks, unchanged_tasks,
     sources_missing, sources_invalid, source_errors, attachable_sync,
     terminal_decl_sync, terminal_text_revisions,
     pruned_modules) = _amend_precheck(orchd_dir, master)


    # task-intake-file-lock（AC1/AC3）：准入写锁 + 提交前 HEAD 推进检测。
    # 准入写（改 _master.json / IDEAS.md / ROADMAP.md）受独立 .intake.lock 串行，
    # 不复用账本锁（避免一次 amend 阻塞并行 claim/done）；HEAD 漂移检测发现
    # base 被并行推进则拒绝注册（git 层 TOCTOU）。
    # R2-4（task-split-guard-no-silent-swallow）：两者仍是 best-effort（失败不阻断
    # amend），但**降级必须可见**——原 `except Exception: pass` 把「锁没拿到 / 检测
    # 本身坏了」与「确实无并发」混为一谈且零留痕。现在：异常被捕获时显式释放并把
    # intake_lock 置 None（明确降级为无锁准入），同时产出结构化留痕（stderr +
    # 响应 degraded_guards），与 session_lock.py 的 best-effort 降级语义对齐但可见。
    intake_lock: dict[str, Any] | None = None
    degraded_guards: list[dict[str, Any]] = []
    guard_stage = "intake_lock_acquire"
    try:
        from orchd.gitops import head_drift_check
        from orchd.ledger import intake_lock_acquire, intake_lock_release

        if orchd_dir is not None:
            from orchd.ledger import resolve_agent_id

            intake_lock = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
        # 提交前 HEAD 推进检测：main 被并行推进则拒绝（AC3）
        guard_stage = "head_drift_check"
        drift = head_drift_check(project_root, ref="HEAD", base_ref=default_branch)
        if drift.get("drift"):
            if intake_lock is not None:
                intake_lock_release(intake_lock)
                intake_lock = None
            raise OrchdError(
                ErrorCode.E007,
                f"stale_base: main 已被并行推进（base {(drift.get('base_sha') or '')[:7]}"
                " 与本地 HEAD 分叉），拒绝注册——请先更新工作区 main 后重试",
                [{"reason": "stale_base",
                  "base_sha": drift.get("base_sha"), "head_sha": drift.get("head_sha")}],
            )
    except OrchdError:
        raise
    except Exception as exc:
        # best-effort 降级：不阻断 amend，但显式释放 + 置 None + 结构化留痕。
        guard_entry: dict[str, Any] = {
            "guard": guard_stage,
            "severity": "warning",
            "status": "degraded",
            "reason": "guard_exception",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "准入锁 / HEAD 漂移检测未能执行（best-effort 降级）：本次 amend 在"
                "无准入锁保护下继续，不可据此断言无并发注册；请排查后重跑"
            ),
        }
        degraded_guards.append(guard_entry)
        if intake_lock is not None:
            try:
                from orchd.ledger import intake_lock_release as _release_intake_lock

                _release_intake_lock(intake_lock)
            except Exception:
                pass
            intake_lock = None
        _log_amend_guard_degrade(guard_entry)

    store.acquire_lock()
    try:
        state, revision_reason, store_root, existing_tasks = _amend_admission(
            orchd_dir=orchd_dir, project_root=project_root, tasks=tasks,
            master=master, store=store, intake_lock=intake_lock,
            revise_terminal=revise_terminal, reason=reason,
            expected_head=expected_head,
        )
        conflict_warnings, removed_tasks = _amend_route_tasks(
            tasks=tasks, state=state, existing_tasks=existing_tasks,
            master=master, project_root=project_root, orchd_dir=orchd_dir,
            revise_terminal=revise_terminal, revision_reason=revision_reason,
            errors=errors, updated_tasks=updated_tasks,
            whitelisted_updates=whitelisted_updates, new_tasks=new_tasks,
            unchanged_tasks=unchanged_tasks, sources_missing=sources_missing,
            sources_invalid=sources_invalid, source_errors=source_errors,
            attachable_sync=attachable_sync,
            terminal_decl_sync=terminal_decl_sync,
            terminal_text_revisions=terminal_text_revisions,
        )
        quality_warnings, _exempted_terminal_warnings = _amend_persist(
            orchd_dir=orchd_dir, tasks=tasks, modules=modules, master=master,
            store=store, state=state, store_root=store_root,
            project_root=project_root, new_tasks=new_tasks,
            updated_tasks=updated_tasks,
            terminal_text_revisions=terminal_text_revisions,
            conflict_warnings=conflict_warnings, pruned_modules=pruned_modules,
        )
    except Exception:
        # 调用方持有模式（release_lock=False）异常亦释放——调用方永无对象可放，
        # 不释放即进程内泄漏；默认模式沿旧行为（异常即泄漏，进程退出回收）。
        if intake_lock is not None and not release_lock:
            try:
                intake_lock_release(intake_lock)
            except Exception:
                pass
        raise
    finally:
        store.release_lock()

    # task-intake-file-lock：成功路径释放准入写锁；release_lock=False 时跳过释放
    # （所有权移交调用方，锁对象经下方 result["_intake_lock"] 透出，调用方提交后释放）。
    if intake_lock is not None and release_lock:
        try:
            intake_lock_release(intake_lock)
        except Exception:
            pass

    result: dict[str, Any] = {
        "amended": True,
        "new_tasks": new_tasks,
        "updated_tasks": updated_tasks,
        "whitelisted_updates": whitelisted_updates,
        "attachable_sync": attachable_sync,
        "terminal_decl_sync": terminal_decl_sync,
        "terminal_spec_revisions": terminal_text_revisions,
        # task-pass9-amend-response-ux（pass9 UX-1）：只回计数与增量——完整 id
        # 列表在 600 任务规模下单响应 40KB+，对 agent 是纯语境成本；增量
        # （new/updated/removed）保持原样。
        "unchanged_tasks_count": len(unchanged_tasks),
        "removed_tasks": removed_tasks,
        "pruned_modules": pruned_modules,
        "quality_warnings": _annotate_if_needed(quality_warnings, orchd_dir),
        "exempted_terminal_warnings": _exempted_terminal_warnings,
        "conflict_warnings": conflict_warnings,
        "sources_missing": sources_missing,
        "sources_invalid": sources_invalid,
    }
    # R2-4：准入守卫降级不静默——非空时并入响应（无降级则维持既有字段集合）
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    # task-concurrent-amend-lost-update：调用方持有模式透出锁对象（写+提交原子化）。
    if intake_lock is not None and not release_lock:
        result["_intake_lock"] = intake_lock
    return result

def _annotate_if_needed(items: list[dict[str, Any]], orchd_dir: Path) -> list[dict[str, Any]]:
    """批量校验结果附加 guidance（best-effort，异常时原样返回）。"""
    if not items:
        return items
    try:
        from orchd.guide import annotate_validation_items
        return annotate_validation_items(items, orchd_dir)
    except Exception:
        return items
