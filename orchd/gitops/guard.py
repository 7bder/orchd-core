"""gitops guard 域：守卫体系（18 个 def = 17 个模块级函数 + 1 个嵌套函数；4 个常量）。

整块迁移自 orchd/gitops.py，函数体逐字一致，仅 import 行调整。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops._const import (
    _GIT_CHECKOUT_TIMEOUT,
    _GIT_ENCODING,
    _GIT_ERRORS,
    _GIT_TIMEOUT,
    _T,
)
from orchd.gitops.query import check_workspace_state, get_default_branch, is_task_worktree
from orchd.gitops.session_lock import ensure_session_lock
from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for


# ------------------------------------------------------------------
# 门禁执行器（task-audit-git-guard-fail-closed：语义三分类 + 降级可审计）
# ------------------------------------------------------------------
# 背景：全项目多处安全门禁以 ``except OrchdError: raise`` + ``except Exception: pass``
# 实现，把「环境不适用」与「校验故障」两种语义混为一谈——门禁自身出故障时引擎
# 认为「没发现问题」并静默放行（fail-open），且降级完全不可审计。
#
# 本执行器把错误分为三类，处置各不相同：
#   1. ``OrchdError``          → 业务拒绝（门禁判定不通过）→ 原样抛出；
#   2. ``NotApplicableError``  → 环境不适用（非 git 仓库 / 无独立主工作树 /
#                                任务分支不存在）→ 合法降级，但必须留痕；
#   3. 其他 ``Exception``      → 校验故障（git 超时 / IO / 导入失败）→ 按
#                                ``on_error`` 阻断（fail_closed）或升级 E030 告警
#                                （warn），绝不静默放行。
#
# gitops 与 onboard 共用本实现，杜绝每处手写 try/except。依赖方向保持叶子化：
# 本段只依赖 orchd.errors，不导入 onboard / review / worktree。
#
# NotApplicableError 定义于 orchd.errors（全引擎通用的门禁语义类型，非 git 领域
# 类型），此处 re-export 仅为兼容既有 ``from orchd.gitops import NotApplicableError``
# 导入路径，不新增依赖边（``gitops → errors`` 本就存在）。


# 校验故障处置策略（run_guard 的 on_error 取值）
GUARD_FAIL_CLOSED = "fail_closed"  # 阻断：抛 OrchdError（默认）
GUARD_WARN = "warn"                # 告警：留 E030 降级标记并返回 fallback，不阻断

# 降级状态取值（写进 degraded_guards 供命令响应携带）
GUARD_STATUS_NOT_APPLICABLE = "not_applicable"
GUARD_STATUS_FAILED = "failed"


def record_degraded_guard(
    degraded: list[dict[str, Any]] | None,
    *,
    guard_name: str,
    status: str,
    reason: str,
    error: str | None = None,
    context: dict[str, Any] | None = None,
    hint: str | None = None,
) -> dict[str, Any]:
    """登记一条门禁降级记录（可审计标记）。

    降级一律带 ``code=E030 / severity=warning``，与 ledger 的完整性告警同码，
    便于 agent 与 doctor 统一按 E030 检索"哪些门禁没在守"。

    Args:
        degraded: 降级登记列表；为 None 时不登记（仅返回条目，供抛错时塞进
            ``OrchdError.details``）。
        guard_name: 门禁名。
        status: ``GUARD_STATUS_NOT_APPLICABLE``（不适用）/ ``GUARD_STATUS_FAILED``
            （校验故障）。
        reason: 人读原因（禁止空泛口号，须写清"为什么没生效"）。
        error: 异常摘要（``Type: msg``，截断 300 字符）。
        context: 附加上下文（task_id / command 等）。
        hint: 处置建议。

    Returns:
        生成的降级条目 dict。
    """
    entry: dict[str, Any] = {
        "guard": guard_name,
        "code": ErrorCode.E030.name,
        "severity": "warning",
        "status": status,
        "reason": reason,
    }
    if error:
        entry["error"] = error
    if hint:
        entry["hint"] = hint
    if context:
        entry["context"] = context
    # 通道 C（task-errexit-channel-c-structured）：E030 降级条目附加结构化
    # details + 按码 guidance（recovery 指向 doctor）。加法式保留原键，
    # best-effort：挂接失败不击穿降级登记链。
    try:
        from orchd.ledger import _attach_structured_guidance

        _attach_structured_guidance(entry, None)
    except Exception:
        pass
    if degraded is not None:
        degraded.append(entry)
    return entry


def _guard_failed(
    exc: BaseException,
    *,
    guard_name: str,
    on_error: str,
    degraded: list[dict[str, Any]] | None,
    fallback: Any,
    context: dict[str, Any] | None,
    hint: str,
    error_code: ErrorCode,
) -> Any:
    """校验故障的统一处置（run_guard 内部用）。"""
    reason = f"{type(exc).__name__}: {exc}".strip()[:300]
    if on_error == GUARD_WARN:
        record_degraded_guard(
            degraded,
            guard_name=guard_name,
            status=GUARD_STATUS_FAILED,
            reason=reason,
            context=context,
            hint=hint or (
                "门禁校验故障，已降级并留痕（未按门禁结论放行）；"
                "请人工复核该门禁覆盖范围"
            ),
        )
        return fallback
    entry = record_degraded_guard(
        degraded,
        guard_name=guard_name,
        status=GUARD_STATUS_FAILED,
        reason=reason,
        context=context,
        hint=hint,
    )
    raise OrchdError(
        error_code,
        f"guard_failed: 门禁 {guard_name} 校验故障，fail-closed 拒绝放行",
        [{
            **entry,
            "hint": hint or (
                "门禁没跑起来 ≠ 校验通过：请重试本命令；持续失败请检查 git 可用性、"
                "仓库规模或杀毒软件实时扫描，确认环境无异常后人工重试"
            ),
        }],
    ) from exc


def run_guard(
    guard_fn: Callable[[], _T],
    *,
    guard_name: str,
    on_error: str = GUARD_FAIL_CLOSED,
    degraded: list[dict[str, Any]] | None = None,
    fallback: Any = None,
    context: dict[str, Any] | None = None,
    hint: str = "",
    na_hint: str | None = None,
    error_code: ErrorCode = ErrorCode.E030,
) -> Any:
    """执行一个完整性 / 安全门禁，按错误语义三分类统一处置。

    门禁本体通过抛 :class:`NotApplicableError` 声明"环境不适用"（允许降级，
    但必须留痕）；其余异常一律视为**校验故障**，按 ``on_error`` 处置，绝不静默。

    Args:
        guard_fn: 门禁本体（无参调用，返回门禁结果）。
        guard_name: 门禁名（进 degraded_guards，供审计与告警定位）。
        on_error: 校验故障处置——``GUARD_FAIL_CLOSED``（默认）抛
            ``OrchdError(error_code)`` 阻断；``GUARD_WARN`` 仅留 E030 降级标记
            并返回 ``fallback``（仅用于诊断类、非最终边界的门禁）。
        degraded: 降级登记列表。调用方负责放进命令响应
            （如 ``result["degraded_guards"] = degraded``）；为 None 时不留痕
            （仍按 ``on_error`` 阻断/告警，仅不进响应）。
        fallback: 降级时返回的兜底值（默认 None）。**注意**：诊断类门禁应让
            fallback 与"校验通过"的返回值可区分（如用 None 表示未知、
            ``[]`` 表示无问题），避免重演"空清单被当成无缺失"。
        context: 附加上下文（task_id / command 等），进降级记录与错误 details。
        hint: 校验故障（非 ``NotApplicableError`` 的异常）的处置建议，进降级记录
            与错误 details——fail-closed 文案（如「检测未生效，已 fail-closed
            阻断」）只应出现在这里，**不得**被"环境不适用"的合法降级复用。
        na_hint: ``NotApplicableError``（环境不适用，合法降级、不阻断）分支的
            独立处置建议；缺省时按 status 生成「环境不适用，本次未阻断」文案，
            与 fail-closed 文案可区分（task-done-guard-layout-strict L3）。
        error_code: ``on_error=GUARD_FAIL_CLOSED`` 时抛出的错误码（默认 E030；
            L1 分支守卫 / L2 session 锁传 E018）。

    Returns:
        门禁结果；降级（不适用，或 warn 模式下的校验故障）时返回 ``fallback``。

    Raises:
        OrchdError: 门禁业务拒绝（原样透传，不改码不吞），或校验故障且
            ``on_error=GUARD_FAIL_CLOSED``（包装为 ``error_code``）。
    """
    try:
        return guard_fn()
    except OrchdError:
        # 业务拒绝：门禁判定不通过，照常向上抛（保留原错误码与 details）
        raise
    except NotApplicableError as exc:
        record_degraded_guard(
            degraded,
            guard_name=guard_name,
            status=GUARD_STATUS_NOT_APPLICABLE,
            reason=str(exc) or "环境不适用",
            context=context,
            hint=na_hint or (
                "环境不适用，本次未阻断（门禁按降级语义放行，非校验通过）；"
                "原因见 reason"
            ),
        )
        return fallback
    except Exception as exc:  # noqa: BLE001  语义三分类的第三类：校验故障
        return _guard_failed(
            exc,
            guard_name=guard_name,
            on_error=on_error,
            degraded=degraded,
            fallback=fallback,
            context=context,
            hint=hint,
            error_code=error_code,
        )


# ------------------------------------------------------------------
# git 判定层（task-14-git-policy-layer：自 gitops_ops 收敛的判定类逻辑）
# ------------------------------------------------------------------
# 工作区/分支/干净度探测（check_workspace_state / get_default_branch）与
# L1/L2 守卫（guard_write_command + 意图化封装）、强约束切回
# （checkout_default_strict）、merge 冲突判定（parse_conflicts）统一收敛到
# 本模块（专用 git 判定入口，单一入口可审计）。onboard / review 调用点只声明
# 意图（guard_claim / guard_done_branch / guard_clean_workspace /
# guard_review_write），不再拼 allowed_branches / require_clean 参数。
# 依赖方向保持叶子化：本模块只依赖 orchd.errors（异常层级），
# 不导入 onboard / review / ledger 状态机。


def _probe_guard_workspace_state(
    project_root: Path,
    command: str,
    degraded: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """探测工作区状态；state=error 时 fail-closed 抛 E018，否则返回 state。"""
    state = run_guard(
        lambda: check_workspace_state(project_root),
        guard_name="check_workspace_state",
        on_error=GUARD_FAIL_CLOSED,
        error_code=ErrorCode.E018,
        context={"command": command},
        hint=(
            f"git 探测异常（非超时/IO 契约外错误）时 L1 分支守卫与 L2 session 锁"
            f"无法执行，已 fail-closed 拒绝 {command}；请重试本命令"
        ),
        degraded=degraded,
    )
    if state.get("state") == "error":
        raise OrchdError(
            ErrorCode.E018,
            f"git_probe_failed: {command} 的 L1 分支守卫与 L2 session 锁无法执行"
            f"（git 探测故障），fail-closed 拒绝继续",
            [{
                "command": command,
                "reason": state.get("reason"),
                "error": state.get("error"),
                "git_timeout_seconds": _GIT_TIMEOUT,
                "hint": (
                    f"git 探测超时或 IO 故障（单条 git 命令上限 {_GIT_TIMEOUT}s）。"
                    f"请先重试 {command}；持续失败请检查：① git 可执行文件可用；"
                    f"② 仓库规模 / 慢盘 / 杀毒软件实时扫描导致 git 超 "
                    f"{_GIT_TIMEOUT}s；③ 确认分支与工作区状态无误后人工重试"
                    f"（守卫不会在探测失败时放行）"
                ),
            }],
        )
    return state


def _container_task_wt_exists(project_root: Path, task_id: str) -> bool:
    """container 布局下独立任务 worktree 是否存在（wt_exists 门控单一来源）。

    ``_build_wrong_branch_hint`` 的 wt_exists 判定与 reviewer 自动切分支门控共用：
    仅 container 布局且 ``<task_wt_root>/<wt_name>/.git`` 存在时为真；其余
    （flat / 降级 / 探测异常）一律 False。异常永不抛（调用方据此分流，不阻断）。
    """
    try:
        from orchd.worktree import worktree_hint, detect_layout

        _layout = detect_layout(project_root)
        if _layout.get("layout") != "container":
            return False
        return (_layout["task_wt_root"] / worktree_hint(task_id) / ".git").exists()
    except Exception:
        return False


def managed_checkout_branch(
    check_root: Path,
    branch_name: str,
    *,
    command: str,
    degraded: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """受管分支切换（单一实现）：reviewer 认领自动切分支与 amend 受管往返共用。

    task-flat-amend-channel AC4：把 reviewer auto-checkout 的「探测 → 干净 →
    分支存在 → checkout」收敛为**唯一实现**，amend 受管往返复用同一原语，
    禁双写漂移。判定顺序与 L1 守卫同源：

    1. 工作区状态探测——探测故障（state=error / 异常）→ ``ok=False`` /
       ``reason=probe_failed``（不抛，交调用方按各自旧语义处置）；
    2. 非 git 可用 → ``ok=False`` / ``reason=not_available``；
    3. 已在目标分支 → ``ok=True`` / ``changed=False``（不动作）；
    4. 工作区脏 → **E017**（复用 :func:`_enforce_workspace_clean`，错误体与守卫
       逐字一致；先于分支存在性判定，避免错分支 + 脏被分支错误掩盖）；
    5. 目标分支不存在 → ``ok=False`` / ``reason=branch_missing``；
    6. ``git checkout <branch>`` 失败 / 超时 → ``ok=False`` / ``reason`` 见返回。

    Args:
        check_root: 分支探测与切换的根（reviewer 场景为
            :func:`_resolve_claim_check_root`；amend 往返为调用方 cwd）。
        branch_name: 目标分支名。
        command: 触发方命令名（仅用于报错 / 降级文案）。
        degraded: 探测降级登记表（透传 :func:`_probe_guard_workspace_state`）。

    Returns:
        ``{"ok": bool, "changed": bool, "checked_out": branch_name,
        "from_branch": <str|None>, "reason": <str|None>}``。
        ``ok=True`` 表示当前已处于目标分支（``changed`` 标记本次是否发生切换）；
        ``ok=False`` 表示前置不满足 / 切换失败，调用方按各自旧语义处置
        （reviewer 返回 None 交守卫 E018；amend 往返结构化上报）。
    """
    try:
        state = _probe_guard_workspace_state(check_root, command, degraded)
    except Exception:
        # 探测故障：与 reviewer auto-checkout 旧行为一致（返回不适用，
        # 交调用方守卫按 E018 处置）；amend 往返据此结构化上报。
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": None, "reason": "probe_failed",
        }
    if not isinstance(state, dict) or not state.get("available"):
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": None, "reason": "not_available",
        }
    from_branch = state.get("branch")
    if from_branch == branch_name:
        return {
            "ok": True, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": None,
        }
    if not state.get("clean"):
        # 脏 → 与守卫完全相同的 E017（可执行指引），且先于分支判定。
        _enforce_workspace_clean(state, True, command, check_root)
        return {  # 防御：上行恒抛，不应到达
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": "dirty",
        }
    try:
        from orchd.gitops import branch_exists

        exists = branch_exists(check_root, branch_name)
    except Exception:
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": "probe_failed",
        }
    if not exists:
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": "branch_missing",
        }
    try:
        proc = subprocess.run(
            ["git", "checkout", branch_name],
            cwd=str(check_root),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=_GIT_CHECKOUT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _clear_timeout_index_lock(check_root)
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": "timeout",
        }
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": "checkout_failed",
        }
    if proc.returncode != 0:
        return {
            "ok": False, "changed": False, "checked_out": branch_name,
            "from_branch": from_branch, "reason": "checkout_failed",
        }
    return {
        "ok": True, "changed": True, "checked_out": branch_name,
        "from_branch": from_branch, "reason": None,
    }


def _reviewer_auto_checkout(
    store,
    project_root: Path | None,
    task_id: str,
    agent_id: str | None,
    degraded: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """reviewer 认领在 flat/降级布局下自动切任务分支（task-review-auto-checkout）。

    前置（container 行为逐字不变）：仅当独立任务 worktree 不存在
    （``_container_task_wt_exists`` 为 False，含 flat 全场景）才继续；container
    有 worktree 时返回 None，后续守卫走既有 cd-hint + E018。

    触发条件（缺一即返回 None，交由守卫按旧语义 E017/E018 拒绝）：
    已在任务分支 / 工作区非干净（脏 → E017 可执行指引）/ 任务分支不存在 /
    非 git / 探测失败 / 切分支执行失败。

    成功时写 AMEND 审计事件（``reason=auto_branch_prepare``，与连带登记同形：
    append + checkpoint，不影响状态机；best-effort，失败不阻断认领）并在
    degraded 留痕；调用方另将返回值挂认领响应（与 ``checked_out_main`` 往返对称）。

    Returns:
        ``{"checked_out": "task/<id>", "from_branch": <str|None>}`` 或 None（未触发）。
    """
    if project_root is None:
        return None
    branch_name = resolve_task_branch_for(project_root, task_id)
    try:
        check_root = _resolve_claim_check_root(project_root)
    except Exception:
        return None
    if check_root is None:
        return None
    if _container_task_wt_exists(Path(project_root), task_id):
        return None
    # task-flat-amend-channel AC4：切换逻辑收敛到 managed_checkout_branch 单一
    # 实现（探测 / 干净 / 分支存在 / checkout 与 amend 往返共用）。前置不满足
    # （已在任务分支 / 非 git / 分支缺失 / 切换失败）→ None，交守卫按旧语义 E018
    # 拒绝；脏工作区仍由同一执法函数抛 E017（错误体与守卫逐字一致）。
    outcome = managed_checkout_branch(check_root, branch_name, command="review claim")
    if not outcome.get("ok") or not outcome.get("changed"):
        return None
    from_branch = outcome.get("from_branch")
    # AMEND 审计（best-effort，与连带登记同形：append + checkpoint，不影响
    # 状态机；失败不阻断认领）。agent_id 为空时记空串（直调守卫场景；生产路径
    # claim 恒传指纹）。
    try:
        from orchd.gitops_ops import make_event

        _audit_agent = agent_id if isinstance(agent_id, str) else ""
        store.acquire_lock()
        try:
            ev = make_event(
                task_id, _audit_agent, "AMEND",
                reason="auto_branch_prepare",
                branch=branch_name,
                from_branch=from_branch,
                hint="review claim 在 flat/降级布局下由引擎自动切到任务分支",
            )
            store.append_event(ev)
            store.update_checkpoint(store.replay())
        finally:
            store.release_lock()
    except Exception:
        pass  # best-effort：审计落账失败不阻断认领（切换本身已生效）
    info = {"checked_out": branch_name, "from_branch": from_branch}
    if degraded is not None:
        degraded.append({"guard": "review_auto_checkout", "task_id": task_id, **info})
    return info


def _build_wrong_branch_hint(
    allowed_branches: set[str],
    command: str,
    project_root: Path,
) -> str:
    """构建 wrong_branch 错误的 hint（含 task branch worktree 位置指引）。"""
    expected = sorted(allowed_branches)
    # task-line-guard-intake-wiring：任务分支族按线命名空间识别（task/{id} 或 {line}/task/{id}）
    task_branches = [b for b in expected if b.startswith("task/") or "/task/" in b]
    if not task_branches:
        return f"请先切换到 {' 或 '.join(expected)} 分支再执行 {command}"
    hint_parts = []
    for tb in task_branches:
        task_id = tb.rsplit("/task/", 1)[1] if "/task/" in tb else tb[len("task/"):]
        # AC4（task-review-diagnostics-hardening）：worktree 目录名单一来源 =
        # worktree_hint(task_id)（内部使用 _task_wt_name），消除双前缀 fallback。
        # task_id 由分支名 task/<id> 截出后已含 task- 前缀，再拼 "task-" 会产出
        # task-task-<id> 双前缀（实测：提示 cd ../task-task-check-test-dedup-utf8/）。
        wt_exists = _container_task_wt_exists(project_root, task_id)
        try:
            from orchd.worktree import worktree_hint

            wt_name = worktree_hint(task_id)
        except Exception:
            # Fallback: engine unavailable, use equivalent logic (no double prefix)
            short = task_id[5:] if task_id.startswith("task-") else task_id
            wt_name = f"task-{short}"
        if wt_exists:
            hint_parts.append(
                f"{command} 应在任务 worktree 执行：container 布局下请进入"
                f"任务 worktree 目录 {wt_name}/（或 cd ../{wt_name}）"
            )
        elif command == "review claim":
            # task-review-auto-checkout：review claim 由引擎自动切分支，不再给
            # 手动 checkout 指令；走到 E018 说明自动切换未生效（分支不存在 /
            # 工作区非干净 / 切换失败）。
            hint_parts.append(
                f"降级模式（无独立任务 worktree）：review claim 由引擎自动切到 "
                f"任务分支 {tb}；本次未自动切换（分支不存在 / 工作区非干净 / "
                "切换失败），请确认后重试"
            )
        else:
            # task-line-guard-intake-wiring（P2-2 / E-02 残留）：不再输出「手动 git
            # checkout」（红线 #1 禁止手动 checkout）——改指受管通道：引擎在评审
            # 认领路径自动切分支；工作区脏先用 orchd restore 清理后重试。
            hint_parts.append(
                "降级模式（无独立任务 worktree）：请先清理工作区后重试"
                f"（可用 python .orchd/__main__.py restore --path <文件>）；"
                f"引擎会在评审认领时自动切到 {tb}"
            )
    non_task = [b for b in expected if not b.startswith("task/") and "/task/" not in b]
    if non_task:
        hint_parts.append(
            f"或切换到 {' 或 '.join(non_task)} 分支再执行 {command}"
        )
    return "；".join(hint_parts)


def _enforce_branch_allowed(
    branch: str | None,
    allowed_branches: set[str] | None,
    command: str,
    project_root: Path,
) -> None:
    """判定分支是否在允许列表；不匹配时抛 E018 wrong_branch。"""
    if allowed_branches is None or branch in allowed_branches:
        return
    expected = sorted(allowed_branches)
    hint_text = _build_wrong_branch_hint(allowed_branches, command, project_root)
    raise OrchdError(
        ErrorCode.E018,
        f"wrong_branch: {command} 须在 {expected} 分支执行，当前在 '{branch}'",
        [{
            "command": command,
            "current_branch": branch,
            "expected_branches": expected,
            "hint": hint_text,
        }],
    )


def commit_hint_for_branch(branch: str | None) -> str:
    """E017 脏工作区处置指引：按分支限定提交动作（task-e017-hint-branch-triage）。

    - 任务分支（``task/*``）：``git add + git commit`` 为红线 #1 唯一豁免，明示；
    - 主分支 / 未知分支：不推手动提交（main 上无豁免），指引擎通道或报告；
    - 恒附幻影脏分支：内容零差异时勿补声明、无需提交（当前内容差分门禁下此类
      脏位本不应到达 E017，此为防御性指引，防旧引擎 / 边缘口径下的误动作）。
    """
    if isinstance(branch, str) and (branch.startswith("task/") or "/task/" in branch):
        action = (
            "请在任务分支内提交（git add + git commit，红线 #1 唯一豁免）"
            "或还原改动后重试"
        )
    else:
        action = (
            "请勿在主分支手动提交（无豁免）：改动属本任务请回任务 worktree 由 "
            "done 提交；属摄入产物请走 intake/amend 引擎通道；归属不明请报告"
        )
    return (
        "工作区存在未提交的已跟踪文件改动（untracked 工具/配置文件不阻塞）。"
        f"{action}。"
        "若 git diff 显示无内容差异（换行符 EOL / 文件模式类幻影脏），不要补声明 "
        "files_to_edit，也无需提交，可报告后重试。"
    )


def _sync_lag_exempted(
    project_root: Path,
) -> tuple[list[str], list[str]]:
    """划分已跟踪改动为（真脏，同步滞后）（task-done-clean-sync-lag-exempt）。

    “同步滞后” = 工作区文件相对主分支**无内容差异**（``git diff --quiet <base> --
    <path>`` 为 0，含换行符归一化口径——git 入库时 CRLF→LF，字节直比必败，故不
    用字节比较）、且暂存区无该文件改动：典型来源是引擎把主工作树资产
    （ROADMAP.md 等）同步进任务 worktree（见 ``worktree._propagate_container_marker``
    内同步段），同步动作本身在 worktree 侧留下与 index 的差异（M），但内容等于
    主分支现状、无本地编辑。
    判定失败（默认分支不可解析 / git 异常）→ 全部按真脏处理（fail-closed 方向）。
    删除态（工作区文件缺失）恒为真脏——删除即改动，不豁免。
    """
    from orchd.gitops import list_tracked_changes
    from orchd.line_ctx import resolve_trunk_for

    dirty = list_tracked_changes(project_root)
    if not dirty:
        return [], []
    try:
        base = resolve_trunk_for(project_root)
    except Exception:
        return sorted(dirty), []
    # 暂存区改动不豁免：同步动作从不 stage，staged 即本地行为。
    staged: set[str] = set()
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"],
            cwd=str(project_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            for entry in (proc.stdout or "").split("\0"):
                if len(entry) < 4:
                    continue
                if entry[0] != " " and entry[0] != "?":
                    path = entry[3:]
                    arrow = path.find(" -> ")
                    staged.add(path[arrow + 4:] if arrow != -1 else path)
    except (subprocess.SubprocessError, OSError):
        return sorted(dirty), []
    exempted: list[str] = []
    real: list[str] = []
    for path in dirty:
        if path in staged or not (Path(project_root) / path).is_file():
            real.append(path)
            continue
        try:
            diff = subprocess.run(
                ["git", "diff", "--quiet", base, "--", path],
                cwd=str(project_root), capture_output=True,
                timeout=_GIT_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            real.append(path)
            continue
        if diff.returncode == 0:
            exempted.append(path)
        else:
            real.append(path)
    return sorted(real), sorted(exempted)


def _enforce_workspace_clean(
    state: dict[str, Any],
    require_clean: bool,
    command: str,
    project_root: Path | None = None,
) -> None:
    """判定工作区干净度；require_clean 且有已跟踪改动时抛 E017。

    ``command == "done"`` 且传入 ``project_root`` 时，同步滞后文件（工作区字节
    与主分支一致的引擎同步产物）不计入脏（task-done-clean-sync-lag-exempt）；
    其余命令与缺 project_root 时语义不变（零回归）。
    """
    if require_clean and not state.get("clean"):
        exempted: list[str] = []
        if command == "done" and project_root is not None:
            try:
                real, exempted = _sync_lag_exempted(Path(project_root))
            except Exception:
                real, exempted = None, []
            if real is not None and not real:
                return
        try:
            from orchd.guide import amend_patch_cmd as _amend_cmd
            _patch_hint = ("；若脏改动系本任务需新增的声明外文件，先在主工作树补声明："
                           + _amend_cmd("<id>", files=["<file>"], entry="orchd"))
        except Exception:
            _patch_hint = ""
        details: dict[str, Any] = {
            "command": command,
            "hint": (commit_hint_for_branch(state.get("branch")) + _patch_hint
                     + "；或执行 orchd restore --path <文件> 丢弃未提交改动"
                       "（仅已跟踪文件，未跟踪新建文件不适用）"),
        }
        if exempted:
            details["sync_lag_exempted"] = exempted
        raise OrchdError(
            ErrorCode.E017,
            f"dirty_workspace: {command} 要求工作区干净（无已跟踪文件改动）",
            [details],
        )


def _is_nogit_orchd_dir(project_root: Path) -> bool:
    """目录是否带 orchd 项目标记（``.orchd/``）——守卫等价分流的目录侧判据。

    调用前提：``check_workspace_state`` 已判定 git 不可用（本函数不再探测 git，
    避免重复 spawn）。惰性导入 ``orchd.nogit`` 保持依赖方向叶子化。
    """
    from orchd.nogit import nogit_project_markers

    return nogit_project_markers(project_root)


def _nogit_maindir_violation(
    project_root: Path,
    command: str,
) -> dict[str, Any] | None:
    """无 git 单目录模式的**主目录守卫**判据（L1 等价）；返回违反详情或 ``None``。

    L1 分支守卫在 git 模式下的作用是「写命令必须发生在允许的位置」（分支 / 工作树）。
    无 git 单目录模式（task-nogit-single-dir-pivot）没有分支与任务工作目录，唯一合法
    写位置是**项目主目录**，故等价判据为**单目录不变量**：布局解析出的主工作树须等于
    命令的操作根（:func:`orchd.nogit.maindir_invariant`）——不等说明操作根不是主目录，
    典型非法形态：

    - 在**容器根**执行（container 布局下主工作树为 ``<容器>/main``，命令却作用于容器根）；
    - 在遗留 / 拷贝出的**任务工作目录**内执行（无 git 单目录模式不该存在该目录）。

    **判据边界（刻意收窄，零回归）**：不使用进程工作目录（``cwd``）作判据——
    ``orchd`` CLI 的项目根本就**由 cwd 解析**（cwd 恒在项目内，该判据在生产路径上
    不会被触发），而库调用 / 宿主工具链（含测试隔离 cwd）会因此产生误报。位置判据
    只取「布局主工作树 vs 命令操作根」这一条确定性判据。

    Returns:
        违反详情 dict（``rule`` / ``command`` / ``expected_dir`` / ``operating_root``
        / ``hint``），无违反时 ``None``。
    """
    from orchd.nogit import maindir_invariant

    inv = maindir_invariant(project_root)
    if not inv.get("ok"):
        return {
            "rule": "nogit_main_dir",
            "command": command,
            "expected_dir": str(inv.get("main_worktree")),
            "operating_root": str(inv.get("project_root")),
            "layout": inv.get("layout"),
            "marker_source": inv.get("marker_source"),
            "hint": (
                f"无 git 单目录模式下写命令须在项目主目录（{inv.get('main_worktree')}）"
                f"执行；当前操作根不是布局主工作树。请 cd 到项目主目录后重试 {command}"
            ),
        }
    return None


def _record_git_unavailable_guard(
    degraded: list[dict[str, Any]] | None,
    command: str,
    state: dict[str, Any],
) -> None:
    """git 不可用时登记降级守卫（环境不适用的合法降级，必须留痕）。"""
    record_degraded_guard(
        degraded,
        guard_name="guard_write_command",
        status=GUARD_STATUS_NOT_APPLICABLE,
        reason=(
            f"git {state.get('reason') or 'unavailable'}：L1 分支守卫与 "
            f"L2 session 锁不适用，已降级跳过 {command}"
        ),
        context={"command": command, "reason": state.get("reason")},
        hint="非 git 环境下的合法降级；若本应是 git 仓库，请检查仓库初始化状态",
    )


# 会话锁未持锁降级的人读原因（task-session-lock-degrade-observability AC3）：
# 按 session_lock 的 reason 分类给出可操作语义——**争用**与**环境故障**的处置
# 方向完全不同（前者等/重试，后者修磁盘/权限/占用），不可混成一句「锁失败」。
_SESSION_LOCK_DEGRADE_REASONS = {
    "flock_contended": (
        "会话锁被其他进程持有（flock 争用）：本会话未持锁，"
        "本次写命令未经会话锁保护（正常并发语义，非环境故障）"
    ),
    "lock_dir_unwritable": (
        "锁目录不可写（IO/权限故障）：本会话未持锁，本次写命令未经会话锁保护"
    ),
    "lock_write_failed": (
        "锁标记写入失败（IO/文件占用故障）：本会话未持锁，本次写命令未经会话锁保护"
    ),
}


def _session_lock_degrade_reason(lock_state: dict[str, Any]) -> str:
    """会话锁未持锁的可读降级原因（未知 reason 码原样透出，不掩盖）。"""
    code = lock_state.get("reason") or "unknown"
    known = _SESSION_LOCK_DEGRADE_REASONS.get(code)
    if known:
        return known
    return f"会话锁获取失败（reason={code}）：本会话未持锁，本次写命令未经会话锁保护"


def guard_write_command(
    project_root: Path | None,
    *,
    allowed_branches: set[str] | None,
    require_clean: bool,
    command: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> None:
    """L1 分支守卫 + L2 session 锁：写命令前校验分支、工作区干净度与 session 锁。

    fail-closed：state=error（git 超时/IO 故障）抛 E018 阻断；state=unavailable
    且属**无 git orchd 项目**时走等价守卫（L1 主目录守卫 + L2 会话锁，见
    task-nogit-guard-parity）；state=unavailable 且为**无关目录**（无 ``.orchd/``）
    时降级跳过但记入 degraded；分支不符→E018，工作区脏→E017。

    **L1 等价（task-nogit-guard-parity）**：无 git 且属 orchd 项目时不再记
    git-unavailable 降级，改以「主目录守卫」判定——写命令须在项目主目录执行，
    违反结构化拒绝（E018，``details[0].rule="nogit_main_dir"``）。

    **L2 等价（task-nogit-guard-parity）**：会话锁**在无 git 下同样启用**（锁载体是
    文件系统维度，与 git 无关；无 git 单目录锁文件回退 ``.session.lock``，互斥语义
    与 git 模式一致：他 session 持锁时本会话写命令抛 E019）。

    **L2 会话锁降级可观测**（task-session-lock-degrade-observability AC3）：会话锁
    未持有（``ensure_session_lock`` 返回 ``acquired=False``）时经
    :func:`record_degraded_guard` 并入 ``degraded``（guard=``session_lock``、
    E030/warning），使「本次未经会话锁保护」从 stderr-only 变为结构化可检索字段。
    best-effort 语义不变（**不阻断**；fail-closed 属门禁行为变更，另议），且
    ``ensure_session_lock`` 的业务拒绝（E019 workspace_busy / 身份缺失）原样上抛
    ——那是并发保护**生效**的证据，不得吞成降级。未发生降级时响应字段集合不变
    （沿用「有降级才补字段」约定）。

    Args:
        degraded: 可选降级登记列表，调用方放进响应使降级可审计。
    """
    branch = None
    git_available = False
    nogit_equivalent = False
    if project_root is not None:
        state = _probe_guard_workspace_state(project_root, command, degraded)
        if state.get("available"):
            git_available = True
            branch = state.get("branch")
            _enforce_branch_allowed(branch, allowed_branches, command, project_root)
            _enforce_workspace_clean(state, require_clean, command, project_root)
        elif _is_nogit_orchd_dir(project_root):
            # 无 git orchd 项目（task-nogit-guard-parity）：不降级——走 L1 等价
            # 主目录守卫（违反 ⇒ E018 结构化拒绝），并让 L2 会话锁照常启用。
            violation = _nogit_maindir_violation(project_root, command)
            if violation is not None:
                raise OrchdError(
                    ErrorCode.E018,
                    f"wrong_directory: {command} 须在无 git 项目主目录执行"
                    f"（当前不在该目录：{violation.get('operating_root')}）",
                    [violation],
                )
            nogit_equivalent = True
        else:
            _record_git_unavailable_guard(degraded, command, state)

    if (git_available or nogit_equivalent) and orchd_dir is not None and agent_id is not None:
        # AC3：捕获持锁态——此前该返回值被丢弃（R2-7 只补了 stderr 留痕），会话锁失效
        # 时写命令照常执行且响应里看不到。未持锁 → 并入 degraded_guards 供 agent 与
        # 审计按结构化字段检索「本次未经会话锁保护」。
        lock_state = ensure_session_lock(orchd_dir, agent_id, branch)
        if not lock_state.get("acquired"):
            record_degraded_guard(
                degraded,
                guard_name="session_lock",
                status=GUARD_STATUS_FAILED,
                reason=_session_lock_degrade_reason(lock_state),
                error=lock_state.get("error"),
                context={
                    "command": command,
                    "reason": lock_state.get("reason"),
                    "gate_acquired": lock_state.get("gate_acquired"),
                },
                hint=lock_state.get("hint"),
            )
        elif not lock_state.get("gate_acquired"):
            # task-session-gate-degrade-surface：持锁但门锁未串行化
            # （acquired=True 且 gate_acquired=False）——此前只有返回态与
            # stderr 可见，写命令响应查不到。并入 degraded_guards 使「本次
            # 未经门锁串行化」结构化可检索；仍 best-effort，不阻断命令执行。
            record_degraded_guard(
                degraded,
                guard_name="session_gate",
                status=GUARD_STATUS_FAILED,
                reason=(
                    "会话锁标记写入成功，但门锁未获取（检查+写入未串行化）："
                    "本次写命令未经门锁串行化保护"
                ),
                context={
                    "command": command,
                    "gate_acquired": False,
                },
                hint=(
                    "门锁降级为 best-effort，不阻断命令执行；并发下可能多个 "
                    "session 同时通过检查，请排查门锁文件占用/权限后重试"
                ),
            )


def _resolve_claim_check_root(project_root: Path | None) -> Path | None:
    """reviewer claim 分支校验根：container 布局优先当前 worktree，否则回退 project_root。

    2026-08-29（review-claim-container-layout）：_cmd_claim 的 project_root 统一走
    canonical 主工作树（供 master/账本共享读），而 reviewer 在任务 worktree 内认领——
    守卫若仍读 project_root（main）的分支必然误报 E018 wrong_branch。此处把守卫的
    分支探测根指向当前进程所在 worktree（git rev-parse --show-toplevel），实现
    「分支检测优先当前 worktree」。

    2026-08-29（回归修复）：仅当 cwd 与 project_root 属**同一 git 仓库**时采用 cwd
    （container 布局：任务 worktree 是主仓库的 linked worktree）；否则（非 git / 探测
    失败 / 与 project_root 同根 / 属不同仓库——如 pytest 隔离仓库）一律回退 project_root，
    避免无关仓库误读其分支导致 E018 误报。
    """
    if project_root is None:
        return project_root
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(Path.cwd()),
            capture_output=True,
            encoding=_GIT_ENCODING,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return project_root
    if res.returncode != 0:
        return project_root
    cwd_root = Path(res.stdout.strip()).resolve()
    if cwd_root == Path(project_root).resolve():
        return project_root
    if not _same_git_repo(cwd_root, Path(project_root)):
        return project_root
    return cwd_root


def _same_git_repo(a: Path, b: Path) -> bool:
    """判定两个目录是否属于同一 git 仓库（比较 git-common-dir 规范化路径）。

    linked worktree（任务 worktree）与主工作树共享同一 git-common-dir → True；
    无关仓库（测试隔离目录等）common dir 不同 → False。
    非 git / 探测失败 → False（best-effort，宁可回退 project_root）。
    """

    def _common_dir(path: Path) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(path),
                capture_output=True,
                encoding=_GIT_ENCODING,
                errors=_GIT_ERRORS,
                timeout=_GIT_TIMEOUT,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None
        raw = proc.stdout.strip()
        if not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            p = (path / p).resolve()
        else:
            p = p.resolve()
        return os.path.normcase(str(p))

    ca = _common_dir(a)
    cb = _common_dir(b)
    return ca is not None and ca == cb


def guard_claim(
    project_root: Path | None,
    *,
    role: str,
    task_id: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> None:
    """claim 前置守卫（L1+L2 意图化封装）：调用点不再拼 allowed_branches / require_clean。

    - reviewer：须在 ``task/{task_id}`` 分支且工作区干净（审查对象是分支上的已提交 diff）；
    - implementer：须在默认分支（main/master）且工作区干净（引擎要从当前 HEAD 建任务分支，
      脏工作区会导致 checkout -b 后分支被污染）。
    """
    if role == "reviewer":
        guard_write_command(
            _resolve_claim_check_root(project_root),
            allowed_branches={resolve_task_branch_for(project_root, task_id)},
            require_clean=True,
            command="review claim",
            orchd_dir=orchd_dir,
            agent_id=agent_id,
            degraded=degraded,
        )
    else:
        default = resolve_trunk_for(project_root) if project_root else "main"
        guard_write_command(
            project_root,
            allowed_branches={default},
            require_clean=True,
            command="claim",
            orchd_dir=orchd_dir,
            agent_id=agent_id,
            degraded=degraded,
        )


def _layout_is_not_container(project_root: Path) -> bool:
    """判定项目是否为非 container 布局（best-effort，布局未知 / 探测失败 → True）。

    done 分支守卫的布局分流依据：container 布局（多 worktree）下只允许任务分支，
    默认分支（主工作树）不在白名单内——从主工作树执行 done 会被拒绝；flat /
    布局未知一律视为"非 container"，保持既有默认分支放行语义（零回归）。
    """
    try:
        from orchd.worktree import detect_layout

        return detect_layout(project_root).get("layout") != "container"
    except Exception:
        return True


def guard_done_branch(
    project_root: Path | None,
    *,
    task_id: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> None:
    """done 前置分支守卫（L1+L2 意图化封装）：须在 ``task/{task_id}``，container
    布局下默认分支不再放行（布局感知收紧，task-done-guard-layout-strict）。

    **布局分流**：
    - flat 布局（或布局未知 / project_root=None 的非 git 降级）：``task/{task_id}``
      或默认分支均可（既有行为，零回归）；
    - container 布局：仅 ``task/{task_id}``——done 应在其任务 worktree 内执行
      （root 解析由 task-done-root-resolution 承担；本守卫是纵深防御，防"解析
      逻辑因环境异常未生效"时再次从主工作树执行 done 而静默劣化），从主工作树
      执行 done 拒绝（E018，hint 指引"应在任务 worktree 执行"）。

    **不要求干净**——files_to_edit 范围内的未提交改动是正常状态（由引擎
    ensure_committed 兜底提交）；干净校验放在自动提交之后
    （见 ``guard_clean_workspace``，提交后仍有已跟踪改动 = 范围外改动）。
    """
    default = resolve_trunk_for(project_root) if project_root else "main"
    allowed: set[str] = {resolve_task_branch_for(project_root, task_id)}
    if project_root is None or _layout_is_not_container(project_root):
        allowed.add(default)
    guard_write_command(
        project_root,
        allowed_branches=allowed,
        require_clean=False,
        command="done",
        orchd_dir=orchd_dir,
        agent_id=agent_id,
        degraded=degraded,
    )


def guard_clean_workspace(
    project_root: Path | None,
    *,
    command: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> None:
    """仅干净校验（任意分支，L1+L2 意图化封装）：用于 done 自动提交后的范围外改动兜底。"""
    guard_write_command(
        project_root,
        allowed_branches=None,
        require_clean=True,
        command=command,
        orchd_dir=orchd_dir,
        agent_id=agent_id,
        degraded=degraded,
    )


def guard_review_write(
    project_root: Path | None,
    *,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
    degraded: list[dict[str, Any]] | None = None,
) -> None:
    """review 提交守卫（L1+L2 意图化封装）：任意分支，不要求干净。"""
    guard_write_command(
        project_root,
        allowed_branches=None,
        require_clean=False,
        command="review",
        orchd_dir=orchd_dir,
        agent_id=agent_id,
        degraded=degraded,
    )


def checkout_default_strict(
    project_root: Path, command: str = "done"
) -> dict[str, Any]:
    """强约束：写完成/打回事件前强制切回默认分支(main/master)。失败抛 OrchdError 阻断。

    调用方须已保证工作区干净（done / review 的干净校验 prior）。强约束边界：
    **git 可用且当前处于非默认分支**时强制切回，任一步骤失败即抛 E018/E017，
    让调用方在未写事件时失败，可安全重试、无中间态；**非 git / git 不可用 /
    无法确定默认分支**时返回 ``skipped`` 降级（无分支概念，流程照常可用，
    保持既有降级契约——参考 test_done_not_a_git_repo_degrades）：

    - git 不可用 / 非 git 仓库 → ``{"skipped": True, "reason": "git_unavailable"}``
    - git 可用但无默认分支 → ``{"skipped": True, "reason": "no_default_branch"}``
    - 任务 worktree（linked worktree）→ ``{"skipped": True, "reason": "task_worktree"}``
      （task-14-merge-main-tree AC3：任务 worktree 恒 checkout task/<id>、永不切 main，
      main 由主工作树占用；弱 LLM 无感）
    - 已在默认分支 → ``{"checked_out_to": <default>}``
    - 非干净工作区 → ``E017 dirty_workspace``（避免把未提交改动带离任务分支）
    - ``git checkout <default>`` 失败 → ``E018``

    Args:
        project_root: 项目根目录。
        command: 触发方命令名（仅用于报错文案，默认 ``"done"``；
            review_submit CHANGES_REQUESTED 传入 ``"review"``）。
    """
    state = check_workspace_state(project_root)
    if not state.get("available"):
        return {"skipped": True, "reason": "git_unavailable"}
    default = get_default_branch(project_root)
    if not default:
        return {"skipped": True, "reason": "no_default_branch"}
    # task-14-merge-main-tree AC3：任务 worktree（linked）内不再切分支——
    # 任务 worktree 恒 checkout task/{id}，main 由主工作树占用，切分支无意义
    # （原 multi_worktree 分支依赖 merge-wt，已随 merge-wt 废弃）。
    if is_task_worktree(project_root):
        return {"skipped": True, "reason": "task_worktree"}
    cur = state.get("branch")
    if cur == default:
        return {"checked_out_to": default}
    if not state.get("clean"):
        try:
            from orchd.guide import amend_patch_cmd as _amend_cmd2
            _patch_hint2 = ("；若脏改动系本任务需新增的声明外文件，先在主工作树补声明："
                            + _amend_cmd2("<id>", files=["<file>"], entry="orchd"))
        except Exception:
            _patch_hint2 = ""
        raise OrchdError(
            ErrorCode.E017,
            f"{command}_switch_branch: 工作区非干净，拒绝强制切换(避免把未提交改动带离"
            "任务分支)",
            [{"command": command,
              "hint": (commit_hint_for_branch(state.get("branch")) + _patch_hint2)}],
        )
    try:
        result = _checkout_default_with_retry(project_root, default)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise OrchdError(
            ErrorCode.E018,
            f"{command}_switch_branch: git checkout 失败: {exc}",
            [{"command": command, "hint": "切换失败前未写事件，可安全重试",
              "concurrent_git_processes": _count_git_processes()}],
        ) from exc
    if result.returncode != 0:
        raise OrchdError(
            ErrorCode.E018,
            f"{command}_switch_branch: git checkout {default} 失败: "
            f"{result.stderr.strip()[:300]}",
            [{"command": command, "hint": "切换失败前未写事件，可安全重试",
              "concurrent_git_processes": _count_git_processes()}],
        )
    return {"checked_out_to": default}


def _count_git_processes() -> int | None:
    """当前系统 git 进程数（task-ref-tx-hook-cost；best-effort）。

    checkout 超时多由并发 git 负载（回归测试夹具 / 并行会话）引起，
    而失败信息此前无任何环境维度，agent 只能从零自查。失败返回 None
    （不阻断），成功返回非负整数。只读探测，不触碰任何进程。
    """
    import sys as _sys

    try:
        if _sys.platform == "win32":
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10,
            )
            if proc.returncode != 0:
                return None
            out = (proc.stdout or b"").decode("utf-8", errors="replace")
            return sum(
                1 for ln in out.splitlines()
                if ln.strip().strip('"').lower().startswith("git.exe")
            )
        proc = subprocess.run(
            ["pgrep", "-c", "-x", "git"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return int((proc.stdout or b"").decode("utf-8", errors="replace").strip() or 0)
    except Exception:
        return None


def _clear_timeout_index_lock(project_root: Path) -> bool:
    """超时强杀后清理 ``.git/index.lock`` 残留（best-effort）。

    仅由 :func:`_checkout_default_with_retry` 在 ``TimeoutExpired`` 分支调用——
    此时锁必为本进程刚强杀的 git 子进程遗留（Windows ``TerminateProcess`` 不跑
    清理），不同于陈旧锁探测（后者按年龄判定，5 分钟内不动）；故此处不做年龄
    判断，直接清。失败静默（重试自会给出真实错误）。
    """
    try:
        lock = Path(project_root) / ".git" / "index.lock"
        if lock.is_file():
            lock.unlink()
            return True
    except OSError:
        pass
    return False


def restore_worktree_paths(
    project_root: Path, paths: list[str]
) -> dict[str, Any]:
    """受管工作树还原（task-restore-channel）：指定路径回到 HEAD，不碰历史。

    红线 #1 受管出口（与 commit / merge-main 精确形态同族）：agent 把"写文件"与
    "需要干净工作区的命令"排进同一批导致脏工作区拒绝（E017）时，用本通道逐字
    还原，而不必走原生 ``git checkout --``（红线禁止，需审批）。

    逐路径门禁（任一失败即整体 E007，一个也不执行——先验后做）：
    - 路径逃逸仓库根 → 拒绝（路径穿越）；
    - 目录 → 拒绝（只接受文件，防 ``--path .`` 级误伤）；
    - 无 HEAD 版本（未跟踪新建 / 已暂存新文件）→ 拒绝（无可还原目标；
      未跟踪文件的删除不在本通道内，请走审批后手动处置）。

    执行：单次 ``git checkout HEAD -- <paths>``（原子语义）；失败 → E007
    （fail-closed，可安全重试）。

    Args:
        project_root: 仓库根目录（git 命令 cwd）。
        paths: 仓库根相对路径列表（非空）。

    Returns:
        ``{"restored": [...], "project_root": str}``。

    Raises:
        OrchdError(E007): 空路径表 / 非 git 仓库 / 任一路径被拒 / 执行失败。
    """
    root = Path(project_root).resolve()
    # 路径按 posix 归一化（git 的 HEAD:path 形态要求正斜杠；Windows 反斜杠亦可传入）。
    # 绝对路径一律拒绝（必须相对仓库根，避免歧义）。
    raw = [str(p).replace("\\", "/").strip() for p in (paths or [])]
    if any(Path(r).is_absolute() for r in raw if r):
        raise OrchdError(
            ErrorCode.E007,
            "restore_refused: 路径须相对仓库根，拒绝绝对路径",
            [{"hint": "用法：orchd restore --path <相对仓库根的文件路径>..."}],
        )
    rels = [r.strip("/") for r in raw]
    rels = [r for r in rels if r and r != "."]
    if not rels:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_usage: restore 需要至少一个 --path 文件路径",
            [{"hint": "用法：orchd restore --path <相对仓库根的文件路径>..."}],
        )
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True, encoding=_GIT_ENCODING, errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_unavailable: git 不可用：{exc}",
            [{"hint": "确认目录是有效 git 仓库后重试"}],
        ) from exc
    if inside.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            "restore_unavailable: 非 git 仓库，无 HEAD 可还原",
            [{"project_root": str(root)}],
        )
    refused: list[dict[str, str]] = []
    ok: list[str] = []
    for rel in rels:
        reason = _restore_refusal_reason(root, rel)
        if reason is None:
            ok.append(rel)
        else:
            refused.append({"path": rel, "reason": reason})
    if refused:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_refused: {len(refused)} 个路径不可还原（一个也未执行）",
            [{
                "refused": refused,
                "hint": (
                    "仅已跟踪且有 HEAD 版本的文件可还原；目录、仓库外路径、"
                    "未跟踪新建文件一律拒绝（后者无可还原目标，删除请走审批后"
                    "手动处置）"
                ),
            }],
        )
    try:
        proc = subprocess.run(
            ["git", "checkout", "HEAD", "--", *ok],
            cwd=str(root),
            capture_output=True, encoding=_GIT_ENCODING, errors=_GIT_ERRORS,
            timeout=_GIT_CHECKOUT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _clear_timeout_index_lock(root)
        raise OrchdError(
            ErrorCode.E007,
            "restore_timeout: git checkout 超时，未确认是否生效，请先核对后重试",
            [{"paths": ok}],
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_failed: git 执行失败：{exc}",
            [{"paths": ok}],
        ) from exc
    if proc.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_failed: git checkout HEAD 失败：{(proc.stderr or '').strip()[:300]}",
            [{"paths": ok}],
        )
    return {"restored": ok, "project_root": str(root)}


def _restore_refusal_reason(root: Path, rel: str) -> str | None:
    """单路径还原准入判定（None = 放行，否则为拒绝原因码）。"""
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        # ValueError = 逃逸仓库根（路径穿越）；OSError = 不可解析
        return "path_traversal"
    if target.is_dir():
        return "is_directory"
    # 有 HEAD 版本才可还原（未跟踪新建 / 已暂存新文件无还原目标）。
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{rel}"],
            capture_output=True, timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "git_unavailable"
    if probe.returncode != 0:
        return "no_head_version"
    return None


def _checkout_default_with_retry(
    project_root: Path, default: str
) -> subprocess.CompletedProcess[str]:
    """执行 ``git checkout <default>``，超时给一次重试机会（task-flaky-hunt-freeze-gate）。

    根因（2026-09-22 全量回归实测）：全量回归 ``-n auto``（本机 16 worker）下，
    checkout 属写操作族且落在进程/IO 竞争窗口，单次可越过读操作 10s 上限 →
    ``TimeoutExpired`` 被吞成 E018 ``done_switch_branch``。两次全量各触发一例
    （test_no_mergeability_when_not_triggered / test_flat_review_branch_deleted_
    regression），而单跑与整文件跑恒绿——纯并行负载抖动，非判定错误。

    处置：写预算 ``_GIT_CHECKOUT_TIMEOUT``（与 commit 同口径，强杀同样遗留
    index.lock）+ 对超时给**一次**重试（累计上限 2×预算，有界）。重试前清残留锁，
    避免第二次必然锁冲突。返回最终 CompletedProcess；两次皆超时则抛最后一个
    ``TimeoutExpired``（由调用方转 E018，文案含"超时"以便区分环境故障与判定失败）。
    """
    last_exc: subprocess.TimeoutExpired | None = None
    for _attempt in range(2):
        try:
            return subprocess.run(
                ["git", "checkout", default],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_CHECKOUT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            _clear_timeout_index_lock(project_root)
    assert last_exc is not None  # 循环至少执行一次且仅超时才会走到此处
    raise last_exc
