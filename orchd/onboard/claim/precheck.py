"""claim 域 —— 认领前置守卫（precheck）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`_claim_precheck`：claim 预校验（无锁快速失败；声明权威解析 / 状态机
  只读门（先于写 hook）/ 角色推断 / reviewer 自动切分支 / implementer 滞留自愈 /
  guard_claim）。
- :func:`_branch_degraded_guard`：try_git_branch failed 状态转 degraded_guard。
- :func:`_check_master_uncommitted`：主工作树 _master.json 未提交检测（best-effort）。
- :func:`_check_retract_cooldown`：retract 认领冷却检查。
- ``_owns_task`` / ``_owns_review`` / ``_find_last_review_claim_event``：归属谓词与
  审查认领事件查询（自 execute 迁入，保持「每次 claim 只调一次」的单一真源）。
- ``_setup_nogit_worktree`` / ``_install_claim_hook``：worktree 就绪分支逻辑与 L3
  hook 安装（自 execute 迁入；patch 面依赖以参数注入，打点继续落在 execute）。

叶子域：默认不导入同包其它域；唯一例外是状态机校验单一来源
（函数内延迟导入 ``execute._validate_claim_state``，见 deps.json delayed 边，
task-engine-defect-fix-unified-u3）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_RETRACT,
    EVENT_REVIEW_CLAIMED,
    STATUS_IN_REVIEW,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import guard_claim as _guard_claim
from orchd.gitops_ops import try_git_branch as _try_git_branch
from orchd.ledger import Store, resolve_session_identity, resolve_store_dir

# retract 认领冷却期（秒）：与 control._RETRACT_COOLDOWN_S 保持一致（task-retract-bind-cooloff）。
# 任务被 retract（disposition=abandon）后默认 300s 内拒绝重新认领，--force 可绕过。
_RETRACT_COOLDOWN_S = 300


def _branch_degraded_guard(task_id: str, branch_state: dict[str, Any]) -> dict[str, Any]:
    """把 try_git_branch 的 failed 状态转成一条 degraded_guard（task-git-branch-fail-report）。

    与 bind_task_wt 失败先例一致：claim 结果 JSON 通过 degraded_guards 显式暴露
    git 任务分支 checkout / 创建失败的原因（step + stderr 摘要），使 agent 可见可查，
    而非静默吞错后在下游 checkout 崩溃。
    """
    return {
        "guard": "try_git_branch",
        "task_id": task_id,
        "state": branch_state.get("state"),
        "step": branch_state.get("step"),
        "error": branch_state.get("error"),
        "hint": "git 任务分支 checkout/创建失败，claim 已降级继续；请据 error 排查仓库状态"
        "（分支冲突 / 脏工作区 / 仓库损坏），必要时先 git 修复再重试",
    }


def _owns_task(t_state: Any, agent_id: str, session_id: str | None) -> bool:
    """实现认领归属判定（session 优先；无 session 信息时退回 agent 比对）。

    claim 侧 E011 与 CLI 预览共用本谓词（task-debug-claim-register-ux）：预览若自行
    简化成「只比 agent」，同一 agent 换了 session 就会**假红**（预览说占着、confirm 放行）。
    """
    claimed_by = getattr(t_state, "claimed_by", None)
    claimed_session = getattr(t_state, "claimed_session", None)
    if claimed_session and session_id:
        return claimed_session == session_id and claimed_by == agent_id
    return bool(claimed_by and claimed_by == agent_id)


def _owns_review(t_state: Any, agent_id: str, session_id: str | None) -> bool:
    """审查认领归属判定（同上，读 review_claimed_* 字段）。"""
    claimed_by = getattr(t_state, "review_claimed_by", None)
    claimed_session = getattr(t_state, "review_claimed_session", None)
    if claimed_session and session_id:
        return claimed_session == session_id and claimed_by == agent_id
    return bool(claimed_by and claimed_by == agent_id)


def _find_last_review_claim_event(store: Store, task_id: str) -> dict[str, Any] | None:
    """返回任务最近一次 REVIEW_CLAIMED 事件（reviewer claim 幂等复判用）。"""
    latest = None
    for ev in store._read_ledger_lines(from_line=1):
        if ev.get("task_id") == task_id and ev.get("type") == EVENT_REVIEW_CLAIMED:
            latest = ev
    return latest


def _setup_nogit_worktree(
    project_root: Path | str,
    task_id: str,
    store: Store,
    degraded_guards: list[dict[str, Any]],
    *,
    take_snapshot: Any,
    bind_task_wt: Any,
) -> str:
    """单目录无 git 的 claim 工作区就绪（task-nogit-single-dir-pivot）。

    不建 sibling 拷贝：在项目主目录建立任务基线快照并绑定任务到主目录
    （flat-git 同口径）。基线/绑定失败仅降级留痕，不阻断认领。实现保留在
    execute（``hook_install`` 等补丁面），此处只承载分支逻辑，依赖以参数注入。
    """
    resolved = str(Path(project_root).resolve())
    try:
        take_snapshot(Path(project_root), task_id, "base")
    except Exception as exc:
        degraded_guards.append({
            "guard": "nogit_base_snapshot", "task_id": task_id,
            "error": str(exc), "hint": "基线快照建立失败，已降级留痕",
        })
    try:
        bind_task_wt(resolve_store_dir(store.orchd_dir), task_id, Path(resolved))
    except Exception as exc:  # pragma: no cover - 绑定失败降级留痕
        degraded_guards.append({
            "guard": "bind_task_wt", "task_id": task_id,
            "error": str(exc), "hint": "binding failed, degraded",
        })
    return resolved


def _install_claim_hook(
    project_root: Path,
    task_id: str,
    files_to_edit: list[str],
    exempt_files: list[str] | None,
    degraded_guards: list[dict[str, Any]],
    *,
    hook_install: Any,
    hook_mod: Any,
) -> None:
    """安装 L3 越界门禁 hook；安装失败/运行时不可用转 degraded_guard 留痕。

    ``hook_install`` / ``hook_mod`` 由 execute 注入，保证 monkeypatch 打点
    （``orchd.onboard.claim.execute.hook_install``）继续生效。
    """
    hook_result = hook_install(
        project_root, task_id, files_to_edit, exempt_files=exempt_files)
    if not (isinstance(hook_result, dict) and not hook_result.get("installed")):
        return
    _reason = hook_result.get("reason")
    if _reason not in ("git_unavailable", hook_mod.HOOK_RUNTIME_UNAVAILABLE):
        return
    guard: dict[str, Any] = {
        "guard": "hook_install",
        "task_id": task_id,
        "reason": _reason,
        "error": hook_result.get("error", ""),
        "hint": hook_result.get("hint") or (
            "hook 安装已 fail-closed：本次未装 L3 越界门禁（E020）；"
            "排查 git / sh 运行时后重新 claim 重烘焙"),
    }
    if _reason == hook_mod.HOOK_RUNTIME_UNAVAILABLE:
        # 竞态兜底（review 残余）：precheck 探测通过后运行时在 install 时转不可用
        # （sh 被移走 / 损坏）——此时 CLAIMED 已落账，不能再 fail-closed 撤回，
        # 必须写 degraded 记录让 status/doctor 标红，不得静默 fail-open。
        _rec = hook_mod.record_hook_runtime_degraded(
            project_root, task_id, {
                "shell": hook_result.get("shell"),
                "error": hook_result.get("error", ""),
            })
        guard["shell"] = hook_result.get("shell")
        guard["degraded_record"] = _rec.get("recorded_at")
    degraded_guards.append(guard)


# ------------------------------------------------------------------
# claim（写操作，锁内）
# ------------------------------------------------------------------


def _claim_task_context(ts: Any, status_now: str) -> dict[str, Any]:
    """E018 诊断上下文：任务当前状态与占用者（u3 AC2）。

    分支/工作区守卫此前只能解释「分支不对」，agent 看不到任务其实卡在
    in_review/已被他人认领等状态机原因；把同一份 replay 结果传给守卫，
    E018 的 details/hint 与状态机错误共用同一状态来源。
    """
    return {
        "task_status": status_now,
        "claimed_by": getattr(ts, "claimed_by", None),
        "review_phase": getattr(ts, "review_phase", None),
        "review_claimed_by": getattr(ts, "review_claimed_by", None),
    }


def _claim_hook_runtime_gate(
    project_root: Path,
    task_id: str,
    task_def: dict[str, Any],
    degraded_guards: list[dict[str, Any]],
) -> None:
    """实现者认领前的 hook 运行时探测（写 CLAIMED 之前 fail-closed）。

    L3 pre-commit hook 由 sh 执行：运行时不可用时 git 会静默跳过 hook（E020
    越界校验 fail-open）。因此实现认领必须在写事件前探测 ``sh -c 'exit 0'``；
    不可用且未显式降级即 E007 拒绝，避免留下「有 claim 无 hook」的半状态。
    ``ORCHD_ALLOW_HOOK_DEGRADED=1`` 显式放行时写入 degraded 记录（doctor/status 标红）。
    """
    # 经已登记边 ``precheck -> gitops`` 取 hook install 域（orchd.gitops 聚合入口
    # 已导入 .hook 子包），避免新增 deps.json 未登记边触发 done 期 E042。
    import orchd.gitops as _gitops

    hook_install = _gitops.hook.install
    HOOK_RUNTIME_UNAVAILABLE = hook_install.HOOK_RUNTIME_UNAVAILABLE
    ORCHD_ALLOW_HOOK_DEGRADED_ENV = hook_install.ORCHD_ALLOW_HOOK_DEGRADED_ENV
    probe_hook_runtime = hook_install.probe_hook_runtime
    record_hook_runtime_degraded = hook_install.record_hook_runtime_degraded

    if not task_def.get("files_to_edit"):
        return
    if not (Path(project_root) / ".git").exists():
        return
    probe = probe_hook_runtime()
    if probe.get("ok"):
        return
    if os.environ.get(ORCHD_ALLOW_HOOK_DEGRADED_ENV) == "1":
        record = record_hook_runtime_degraded(project_root, task_id, probe)
        degraded_guards.append({
            "guard": "hook_runtime",
            "task_id": task_id,
            "reason": HOOK_RUNTIME_UNAVAILABLE,
            "shell": probe.get("shell"),
            "error": probe.get("error", ""),
            "degraded_record": record.get("recorded_at"),
            "hint": (
                "已按 ORCHD_ALLOW_HOOK_DEGRADED=1 放行：本次 L3 越界门禁（E020）不可用，"
                "已写 degraded 记录（doctor/status 标红）；修复 sh 后重新 claim 重烘焙"
            ),
        })
        return
    raise OrchdError(
        ErrorCode.E007,
        f"hook_runtime_unavailable: task '{task_id}' 实现认领需要可执行的 sh",
        [{
            "reason": HOOK_RUNTIME_UNAVAILABLE,
            "task_id": task_id,
            "shell": probe.get("shell"),
            "error": probe.get("error", ""),
            "hint": (
                "实现认领会安装 L3 pre-commit hook（E020 越界门禁），但当前找不到可执行的 sh："
                "请安装 sh / Git Bash 并确保其在 PATH，或设置 ORCHD_HOOK_SHELL 指向 sh；"
                "如确认要在无 hook 状态下继续，显式设置 ORCHD_ALLOW_HOOK_DEGRADED=1"
                "（会写 degraded 记录并在 doctor/status 标红）"
            ),
        }],
    )


def _claim_precheck(
    store: Store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str | None,
    project_root: Path | None,
    review_type: str | None,
    force: bool = False,
) -> tuple[
    dict[str, Any], str, str | None,
    list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None,
    dict[str, Any],
]:
    # task-flat-decl-authority：task_def 经 resolve_declaration_source 解析
    # （flat 下从 main blob 读权威声明，防任务分支本地副本陈旧）。
    degraded_guards: list[dict[str, Any]] = []
    from orchd.worktree import resolve_declaration_source
    tasks = resolve_declaration_source(project_root, tasks, degraded_guards)[0]
    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(
            ErrorCode.E005, f"task '{task_id}' not found in master",
            [{"task_id": task_id,
              "hint": f"任务 {task_id} 在 _master.json 中不存在，"
                      "检查 id 是否拼写正确或是否已注册"}])
    session_id = resolve_session_identity(store.orchd_dir)["session_id"]
    pre_state = store.replay()
    pre_ts = pre_state.get(task_id)
    status_now = pre_ts.status if pre_ts else "pending"
    if role is None:
        role = "reviewer" if status_now == STATUS_IN_REVIEW else "implementer"
    # task-debug-claim-type-guard：``--type`` 只在**审查认领**路径有意义。实现认领分支
    # 从不消费该参数（历史行为：静默丢弃），于 agent 而言表现为「带了 --type 却按实现
    # 认领」，只能事后 retract（账本实证：task-gate-evidence-unify 08:55:29 CLAIMED →
    # 08:58:55 RETRACT「审查者误领实现认领」）。故在**任何写副作用之前**（本函数先于
    # _guard_claim / 建分支 / 落事件）硬拒绝。
    if role == "implementer" and review_type is not None:
        raise OrchdError(
            ErrorCode.E007,
            f"claim_type_not_applicable: task '{task_id}' 当前 {status_now}，"
            f"--type {review_type} 仅审查认领有效",
            [{
                "reason": "claim_type_not_applicable",
                "task_id": task_id,
                "current_status": status_now,
                "got_type": review_type,
                "hint": ("--type 用于在 in_review 阶段指定审查阶段（spec/code）；实现认领不要带 "
                         "--type。若你以为这是审查认领，请先确认任务已 done 并进入 in_review"
                         "（status 查看 current_status），再以审查者身份 claim --type"),
            }],
        )
    # task-engine-defect-fix-unified-u3 AC1/AC3：状态机校验单一来源，**先于任何
    # 写 hook**（reviewer 自动切分支 / implementer 滞留自愈 / guard_claim 的 L2
    # 会话锁）。判定实现 = execute._validate_claim_state（与锁内复核同一份）；此处
    # 无锁只读快速失败，最终权威仍在锁内复核。
    from .execute import _validate_claim_state

    _validate_claim_state(
        store, tasks, task_def, task_id, agent_id, session_id, role,
        review_type, project_root, force=force, check_serial=False)
    # 实现者认领前的 hook 运行时探测：不可用即 E007，且先于任何写副作用。
    if role == "implementer" and project_root:
        _claim_hook_runtime_gate(project_root, task_id, task_def, degraded_guards)
    auto_checkout: dict[str, Any] | None = None
    if role == "reviewer" and project_root:
        from orchd.worktree import _task_wt_name, detect_layout
        _layout = detect_layout(project_root)
        if _layout.get("layout") == "container":
            _wt_dir = _layout["task_wt_root"] / _task_wt_name(task_id)
            if not (_wt_dir / ".git").exists():
                branch_state = _try_git_branch(project_root, task_id)
                if branch_state and branch_state.get("state") == "failed":
                    degraded_guards.append(_branch_degraded_guard(task_id, branch_state))
        else:
            # task-review-auto-checkout：flat/降级无独立 worktree → 引擎自动切
            # 任务分支（+ AMEND 审计 reason=auto_branch_prepare；脏工作区 / 分支
            # 缺失时回落守卫 E017/E018）。container 行为逐字不变。
            from orchd.gitops.guard import _reviewer_auto_checkout
            auto_checkout = _reviewer_auto_checkout(
                store, project_root, task_id, agent_id, degraded_guards)
    trunk_restore: dict[str, Any] | None = None
    if role == "implementer" and project_root:
        # task-pass9-lifecycle-release-invariant：implementer claim 滞留自愈（读侧
        # 兜底）——主工作树滞留 task/{id} 且该任务无活跃认领时自动恢复 trunk
        # 基线；仍活跃则不动，交 _guard_claim 按既有语义 E018。
        from orchd.gitops.guard import _implementer_ensure_trunk
        trunk_restore = _implementer_ensure_trunk(
            store, project_root, agent_id=agent_id, degraded=degraded_guards)
    _guard_claim(
        project_root, role=role, task_id=task_id, orchd_dir=store.orchd_dir,
        agent_id=agent_id, degraded=degraded_guards,
        task_context=_claim_task_context(pre_ts, status_now))
    # task-engine-defect-fix-unified-u7 AC1/AC2：claim 期 verify 可执行性预演
    # （与注册期 dry-run 共用 orchd.spec.verify_preview 单一实现；此处纯静态，
    # 不再把 expected_pending 当放行——目标文件缺失但已声明时提示先创建）。
    verify_preview = _claim_verify_preview(task_def, project_root)
    return (task_def, role, session_id, degraded_guards, auto_checkout,
            trunk_restore, verify_preview)


def _check_master_uncommitted(project_root: Path | None) -> dict[str, Any] | None:
    """task-engine-cli-friction-fix：检测主工作树 _master.json 是否有未提交改动。

    有则返回 intake_warning（hint 输出 orchd intake，而非人工 git commit）；
    无或检测失败返回 None。best-effort，不阻断认领。
    """
    if project_root is None:
        return None
    try:
        from orchd.gitops import list_tracked_changes
        dirty = list_tracked_changes(project_root)
        if dirty is None:
            return None
        master_dirty = [f for f in dirty if f.endswith("_master.json")]
        if not master_dirty:
            return None
        return {
            "type": "master_uncommitted",
            "files": master_dirty,
            "hint": (
                "_master.json 有未提交的摄入/注册产物，请运行 "
                "python .orchd/__main__.py intake 自动提交（勿人工 git commit）"
            ),
            "command": "python .orchd/__main__.py intake",
        }
    except Exception:
        return None


def _check_retract_cooldown(store: Store, task_id: str) -> dict[str, Any] | None:
    """检查任务是否在 retract 认领冷却期内（task-retract-bind-cooloff）。"""
    from datetime import datetime
    all_events = store._read_ledger_lines(from_line=1)
    last_retract = None
    last_claimed_ts = None
    for ev in all_events:
        if ev.get("task_id") != task_id:
            continue
        etype = ev.get("type")
        if etype == EVENT_RETRACT and not ev.get("retracted"):
            last_retract = ev
        elif etype == EVENT_CLAIMED and not ev.get("retracted"):
            last_claimed_ts = ev.get("timestamp")
    if last_retract is None:
        return None
    if last_claimed_ts:
        try:
            rt = datetime.fromisoformat(last_retract["timestamp"])
            ct = datetime.fromisoformat(last_claimed_ts)
            if ct > rt:
                return None
        except (ValueError, KeyError):
            pass
    disposition = last_retract.get("disposition", "abandon")
    if disposition != "abandon":
        return None
    # admin 执行的 retract 视为系统/管理员操作，不触发认领冷却（避免破坏既有测试
    # 中 admin 撤回后立即重新认领的场景；用户主动放弃由非 admin 操作者触发）。
    if last_retract.get("agent_id") == "admin":
        return None
    try:
        rt = datetime.fromisoformat(last_retract["timestamp"])
        now = datetime.now(rt.tzinfo) if rt.tzinfo else datetime.now()
        seconds_ago = (now - rt).total_seconds()
    except (ValueError, KeyError):
        return None
    if seconds_ago < _RETRACT_COOLDOWN_S:
        return {
            "retracted_at": last_retract["timestamp"],
            "seconds_ago": seconds_ago,
            "disposition": disposition,
        }
    return None


def _claim_verify_preview(
    task_def: dict[str, Any],
    project_root: Path | None,
) -> dict[str, Any]:
    """claim 期 verify 可执行性预演（u7 AC1/AC2）。

    单一实现：``orchd.spec.verify_preview.claim_verify_preview``（纯静态、不跑
    子进程，避免每次认领 30s 预演开销）；本函数只做薄封装，禁止在 claim 侧另写
    目标文件就绪判定。
    """
    from orchd.spec.verify_preview import claim_verify_preview

    return claim_verify_preview(task_def, project_root)


def _declaration_path_warnings(
    task_def: dict[str, Any],
    project_root: Path | None,
) -> list[dict[str, str]]:
    """claim 预检：声明路径的磁盘事实预警（task-engine-defect-fix-unified-u1 AC1）。

    单一来源：``orchd/spec/declarations.path_state``（四态分类只在那边实现一次）；
    本函数只做薄封装，禁止在 claim 侧另写存在性/跟踪判定。project_root 缺失或
    非 git 环境时 declaration 层自行降级（跟踪信息未知 → exists/ghost 两态）。
    """
    from orchd.spec.declarations import declaration_path_warnings
    return declaration_path_warnings(
        task_def.get("files_to_edit") or [], project_root)
