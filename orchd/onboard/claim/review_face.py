"""claim 域 —— reviewer 认领面（review face）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`_claim_review_branch`：reviewer claim 的响应组装（files_to_review /
  分支 diff 诊断 / mergeability 对账 / verify 同源校验 / 自审提示）。
- :func:`_resolve_review_worktree`：诊断/复跑作用域解析到任务 worktree
  （单源委托 gitops_ops.resolve_task_worktree）。
- :func:`_attach_verify_head_drift`：DONE 存证与所审内容同源校验（无位移零成本）。
- :func:`_reconcile_mergeability`：审查期前置对账（纯探测不落地）。
- :func:`_build_self_review_notice`：自审提示实体化。

依赖：不导入同包其它域（review_face 是叶子；run.py 编排层负责串联）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, NotApplicableError
from orchd.gitops import (
    GUARD_WARN,
    branch_exists,
    is_task_worktree,
    run_guard,
)
from orchd.gitops.repo import for_project as _repo_for_project
from orchd.ledger import Store, TaskDerived
from orchd.line_ctx import resolve_task_branch_for
from orchd.review import (
    extract_review_comments as _extract_review_comments,
    find_last_done_event as _find_last_done_event,
)
from orchd.worktree import (
    diagnose_missing_branch_files,
    task_branch_head,
)


def _resolve_review_worktree(project_root: Path | None, task_id: str) -> Path | None:
    """审查分支 diff 诊断的作用域根（AC2）：按布局解析到任务 worktree。

    container 布局下审查认领由主工作树发起，``project_root`` 即主工作树 →
    ``is_task_worktree`` 恒 False → 诊断恒降级（``not worktree``）。本函数把
    作用域解析到 ``<task_wt_root>/task-<id>``；本身就是任务 worktree 则原样
    返回；解析不到（flat / 无独立 worktree）回退 ``project_root``，维持既有
    降级语义（best-effort，绝不抛异常）。

    实现委托 :func:`orchd.gitops_ops.resolve_task_worktree`（单源，
    task-done-reconcile-main）：对账与诊断必须解析到同一 worktree，两处各写
    一份必然漂移。
    """
    from orchd.gitops_ops import resolve_task_worktree

    return resolve_task_worktree(project_root, task_id)


def _attach_verify_head_drift(
    result: dict[str, Any],
    task_def: dict[str, Any],
    task_id: str,
    project_root: Path | None,
    done_event: dict[str, Any],
    degraded: list[dict[str, Any]],
) -> None:
    """DONE 存证与所审内容的同源校验：**仅位移时**按 HEAD 复跑（不阻断）。

    task-review-verify-at-head（裁决 A'）：DONE 时跑的那一次 verify 只覆盖 DONE
    那一刻的代码，「DONE 之后补交提交」在机器视角里不受任何 verify 覆盖——审查
    凭据与所审内容不同源，「补一笔改动再送审」即可绕过机器复跑。本函数补上该洞，
    但按**条件触发**以守住两条边界：

    - **无位移**（含「存证无锚」这一旧存证 / 降级态）：**零子进程、零新增字段**，
      响应与改动前逐字一致（AC3）——无条件复跑会让每次认领多等最长 120s，且与
      ``rules/review.md:32``「默认引用不重跑」冲突；
    - **位移**：按 HEAD 复跑 verify_command，把新记录塞进 ``verify_at_head`` 与
      DONE 存证 ``verify`` **并列**（AC2）；复跑失败只把 ``severity`` 提到
      ``error`` 并进 ``degraded``，**不阻断认领**（AC4）——「边审边补交」是合法
      动作（本轮 A/B2 两卡均在审查期补交提交），阻断它等于惩罚修错。

    复跑目录解析到**任务 worktree**（:func:`_resolve_review_worktree`）：container
    布局下审查由主工作树发起，直接拿 ``project_root`` 会让 verify 跑错目录。

    Args:
        result: 认领响应字典（原地并入字段）。
        task_def: 任务定义（复跑取 ``verify_command``）。
        task_id: 任务 id。
        project_root: 审查发起目录（主工作树或任务 worktree）。
        done_event: DONE 事件（取其 ``verify.head`` 作为基线锚）。
        degraded: 降级留痕列表（复跑异常时追加）。

    Returns:
        None（副作用落在 ``result`` 与 ``degraded`` 上）。
    """
    verify = done_event.get("verify") or {}
    baseline_head = verify.get("head")
    if not baseline_head or project_root is None:
        return  # 无锚 → 无从比对，fail-open（行为退化为改动前）
    current_head = task_branch_head(project_root, task_id)
    if not current_head or current_head == baseline_head:
        return  # 无位移 → 零成本：不跑子进程、不产噪声字段
    # **不**回退 project_root：解析不到任务 worktree 时复跑会落在主工作树，
    # 而 current_head 取的是任务分支 tip —— 执行目录与所审 HEAD 不同源，跑出来的
    # ok 是**误导性凭据**，比不复跑更糟（本卡要防的正是假凭据）。故解析不到就
    # 走降级通道留痕、不复跑。
    wt_root = _resolve_review_worktree(project_root, task_id)

    def _rerun() -> dict[str, Any] | None:
        if wt_root is None:
            raise NotApplicableError("任务 worktree 未解析到：不在主工作树复跑")
        from orchd.onboard.lifecycle.core import rerun_verify_at_head

        return rerun_verify_at_head(task_def, task_id, wt_root)

    at_head = run_guard(
        _rerun, guard_name="verify_at_head_rerun", on_error=GUARD_WARN,
        fallback=None, context={"task_id": task_id},
        hint="verify 按 HEAD 复跑失败（不阻断认领）", degraded=degraded,
    )
    result["verify_head_drift"] = {
        "code": "verify_head_drift",
        "baseline_head": baseline_head,
        "current_head": current_head,
        "severity": "info" if (at_head or {}).get("ok") else "error",
    }
    if at_head is not None:
        result["verify_at_head"] = at_head


def _reconcile_mergeability(
    project_root: Path | None, task_id: str
) -> dict[str, Any] | None:
    """reviewer 认领前置对账（task-done-reconcile-main 挂载点 ②）。

    与 done 挂载点**共用同一函数与同一触发条件**
    （:func:`orchd.gitops_ops.reconcile_with_main`，防双写漂移），区别仅在
    ``apply=False``：纯探测、不落地、不留 ``MERGE_HEAD``——审查期不触碰工作区
    （E017 审查期冻结不受影响）。

    覆盖「done 之后、merge 之前」残余窗口（实测事故：main 于 done 后 84 秒推进，
    两轮审查后才在 merge 撞冲突）。仅在**对账真正触发**（``checked``）时返回，
    未触发（main 未推进 / 无文件交集）返回 ``None``，使无风险场景的认领响应与
    现状逐字段一致（零噪音、零回归）。不改变可领取性、不新增阻断——reviewer
    仍可选择继续审并记录风险。

    best-effort：任何异常一律 ``None``，绝不阻断认领。
    """
    if project_root is None:
        return None
    try:
        from orchd.gitops_ops import reconcile_with_main

        rec = reconcile_with_main(project_root, task_id, apply=False)
    except Exception:
        return None
    if not rec.get("checked"):
        return None
    return {
        "clean": rec.get("clean"),
        "files": rec.get("files", []),
        "reason": rec.get("reason"),
        "action": rec.get("action"),
    }


def _build_self_review_notice(
    done_by: str | None,
    enforce_self_review_block: bool,
) -> dict[str, Any]:
    """构造自审提示（task-self-review-disclosure-and-log-channel，AC1/AC2）。

    历史实现是占位符（``message="self_review"`` / ``hint="enforce flag"``）：
    agent 收到后既不知道「正在审自己的实现」，也不知道既有约定的披露义务
    （只写在 rules/review.md 与 conventions.md 文本里，引擎响应从未表达）；
    且 ``enforce_self_review_block`` 硬编码 False——enforce=True 时
    :func:`_claim_write_event` 已先抛 E016 早退，恒假字段零信息量。

    实体化后文案含两项可执行信息（审查者据此可直接行动）：

    - **判定依据**：DONE 实现指纹 == 当前 session（同指纹即判自审）；
    - **披露要求**：继续审查须在 review comments 首句披露自审，或改由另一
      会话（换指纹）/他人审查。

    Args:
        done_by: 最后一次 DONE 事件的 agent_id（实现者指纹）。
        enforce_self_review_block: 引擎自审阻断开关的**真实取值**（不再硬编码）。

    Returns:
        claim 响应的 ``self_review_notice`` 字段值。
    """
    who = done_by or "<unknown>"
    return {
        "message": (
            "自审：本任务最后一次 DONE 由当前会话提交"
            f"（判定依据：DONE 实现指纹 {who} == 当前 session），"
            "你正在审查自己的实现"
        ),
        "hint": (
            "继续审查须在 review comments 首句披露自审"
            f"（写明「本审查为自审，实现者 = 审查者 = {who}」）；"
            "更稳妥的做法是换一个会话（换指纹）或交由他人审查"
        ),
        "done_by": done_by,
        "enforce_self_review_block": enforce_self_review_block,
    }


def _claim_review_branch(
    store: Store,
    task_id: str,
    task_def: dict[str, Any],
    project_root: Path | None,
    role: str,
    derived: TaskDerived,
    review_phase: str | None,
    is_self_review: bool,
    event: dict[str, Any],
    degraded_guards: list[dict[str, Any]],
    shared: dict[str, Any] | None = None,
    enforce_self_review_block: bool = False,
) -> dict[str, Any] | None:
    if role != "reviewer":
        return None
    files_to_review = [
        {"path": p, "priority": "must_read"}
        for p in task_def.get("files_to_edit", [])
    ]
    if shared:
        if review_phase == "spec":
            arch = shared.get("architecture")
            if arch:
                files_to_review.append({"path": arch, "priority": "reference", "hint": "arch"})
        elif review_phase == "code":
            conv = shared.get("conventions")
            if conv:
                files_to_review.append({"path": conv, "priority": "must_read", "hint": "conv"})
        else:
            arch = shared.get("architecture")
            if arch:
                files_to_review.append({"path": arch, "priority": "reference", "hint": "arch"})
            conv = shared.get("conventions")
            if conv:
                files_to_review.append({"path": conv, "priority": "must_read", "hint": "conv"})
    done_event = _find_last_done_event(store, task_id, derived)
    changes_description = done_event.get("changes_description") if done_event else None
    result = {
        "claimed": True, "task_id": task_id, "review_type": review_phase,
        "files_to_review": files_to_review,
        "acceptance_criteria": task_def.get("acceptance_criteria", []),
        "changes_description": changes_description,
        "review_comments": _extract_review_comments(store, task_id, derived),
        "event_id": event["event_id"],
    }
    if is_self_review:
        _done_by = done_event.get("agent_id") if done_event else None
        result["self_review_notice"] = _build_self_review_notice(
            _done_by, enforce_self_review_block
        )
    if done_event and done_event.get("verify"):
        result["verify"] = done_event["verify"]
    # task-review-completion-guidance（AC3）：reviewer 可粘贴的定向重跑命令模板，
    # 由 build_reviewer_rerun_command 从 verify_command 单一事实源提取 pytest 段，
    # 不手写字符串；含跨平台 --basetemp 形式。verify_command 无 pytest 段时不附加。
    try:
        from orchd.review import build_reviewer_rerun_command

        rerun = build_reviewer_rerun_command(task_def)
        if rerun:
            result["rerun_command"] = rerun
    except Exception:
        pass  # best-effort：重跑模板生成失败不阻断认领
    review_degraded: list[dict[str, Any]] = []

    def _diag() -> dict[str, Any]:
        if project_root is None:
            raise NotApplicableError("no root")
        exists = branch_exists(project_root, resolve_task_branch_for(project_root, task_id))
        if exists is None:
            raise RuntimeError("git fail")
        if not exists:
            raise NotApplicableError("no branch")
        # AC2（task-review-baseline-and-worktree-recycle-fix）：container 布局下
        # 审查认领由主工作树发起，project_root 即主工作树 → is_task_worktree 恒
        # False → 诊断恒降级 not worktree（branch_files / missing_declared_files
        # 恒为 null）。先按布局把诊断作用域解析到任务 worktree 再判定。
        diag_root = _resolve_review_worktree(project_root, task_id)
        if diag_root is None or not is_task_worktree(diag_root):
            raise NotApplicableError("not worktree")
        # 诊断作用域已解析到任务 worktree（恒 git linked）→ 经端口分发恒命中
        # GitBackend，与原 task_branch_files 同函数（task-repo-migration-callsites）。
        return {
            "branch_files": _repo_for_project(diag_root).changed_paths(task_id),
            "missing_declared_files": diagnose_missing_branch_files(
                diag_root, task_id, task_def.get("files_to_edit", [])),
        }

    diag = run_guard(
        _diag, guard_name="review_branch_diff_diagnosis", on_error=GUARD_WARN,
        fallback=None, context={"task_id": task_id}, hint="diag fail",
        degraded=review_degraded)
    if diag is None:
        result["branch_files"] = None
        result["missing_declared_files"] = None
    else:
        result["branch_files"] = diag["branch_files"]
        result["missing_declared_files"] = diag["missing_declared_files"]
    if review_degraded or degraded_guards:
        result["degraded_guards"] = degraded_guards + review_degraded
    # task-done-reconcile-main 挂载点 ②：reviewer 认领前置对账（纯探测、零副作用、
    # 不阻断）。把冲突从「审查之后」挪到「审查之前」——避免白审两轮。
    mergeability = _reconcile_mergeability(project_root, task_id)
    if mergeability is not None:
        result["mergeability"] = mergeability
    # task-review-verify-at-head：DONE 存证与所审内容同源校验——无位移零成本，
    # 位移才按 HEAD 复跑并把新旧两份并列；复跑失败只告警、不阻断认领。
    if done_event:
        _attach_verify_head_drift(
            result, task_def, task_id, project_root, done_event, review_degraded)
        if review_degraded:
            # 复跑的降级留痕发生在本行之前、上面那个合并点之后 —— 补一次合并，
            # 否则「复跑失败」这条留痕不会出现在响应里（审查侧就看不见）。
            result["degraded_guards"] = degraded_guards + review_degraded
    return result
