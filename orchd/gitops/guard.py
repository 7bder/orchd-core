"""gitops guard 域：守卫体系（18 个 def = 17 个模块级函数 + 1 个嵌套函数；4 个常量）。

整块迁移自 orchd/gitops.py，函数体逐字一致，仅 import 行调整。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from orchd.errors import ErrorCode, NotApplicableError, OrchdError
from orchd.gitops._const import _GIT_ENCODING, _GIT_ERRORS, _GIT_TIMEOUT, _T
from orchd.gitops.query import check_workspace_state, get_default_branch, is_task_worktree
from orchd.gitops.session_lock import ensure_session_lock


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


def _build_wrong_branch_hint(
    allowed_branches: set[str],
    command: str,
    project_root: Path,
) -> str:
    """构建 wrong_branch 错误的 hint（含 task branch worktree 位置指引）。"""
    expected = sorted(allowed_branches)
    task_branches = [b for b in expected if b.startswith("task/")]
    if not task_branches:
        return f"请先切换到 {' 或 '.join(expected)} 分支再执行 {command}"
    hint_parts = []
    for tb in task_branches:
        task_id = tb[len("task/"):]
        # AC4（task-review-diagnostics-hardening）：worktree 目录名单一来源 =
        # worktree_hint(task_id)（内部使用 _task_wt_name），消除双前缀 fallback。
        # task_id 由分支名 task/<id> 截出后已含 task- 前缀，再拼 "task-" 会产出
        # task-task-<id> 双前缀（实测：提示 cd ../task-task-check-test-dedup-utf8/）。
        wt_exists = False
        try:
            from orchd.worktree import worktree_hint, detect_layout

            wt_name = worktree_hint(task_id)
            _layout = detect_layout(project_root)
            if _layout.get("layout") == "container":
                wt_exists = (
                    _layout["task_wt_root"] / wt_name / ".git"
                ).exists()
        except Exception:
            # Fallback: engine unavailable, use equivalent logic (no double prefix)
            short = task_id[5:] if task_id.startswith("task-") else task_id
            wt_name = f"task-{short}"
        if wt_exists:
            hint_parts.append(
                f"{command} 应在任务 worktree 执行：container 布局下请进入"
                f"任务 worktree 目录 {wt_name}/（或 cd ../{wt_name}）"
            )
        else:
            hint_parts.append(
                f"降级模式（无独立任务 worktree）：在主工作树执行 "
                f"git checkout {tb} 后重试"
            )
    non_task = [b for b in expected if not b.startswith("task/")]
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
    if isinstance(branch, str) and branch.startswith("task/"):
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
    from orchd.gitops import get_default_branch, list_tracked_changes

    dirty = list_tracked_changes(project_root)
    if not dirty:
        return [], []
    try:
        base = get_default_branch(project_root) or "main"
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
            "hint": (commit_hint_for_branch(state.get("branch")) + _patch_hint),
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
            allowed_branches={f"task/{task_id}"},
            require_clean=True,
            command="review claim",
            orchd_dir=orchd_dir,
            agent_id=agent_id,
            degraded=degraded,
        )
    else:
        default = get_default_branch(project_root) if project_root else None
        default = default or "main"
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
    default = get_default_branch(project_root) if project_root else None
    default = default or "main"
    allowed: set[str] = {f"task/{task_id}"}
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
        result = subprocess.run(
            ["git", "checkout", default],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise OrchdError(
            ErrorCode.E018,
            f"{command}_switch_branch: git checkout 失败: {exc}",
            [{"command": command, "hint": "切换失败前未写事件，可安全重试"}],
        ) from exc
    if result.returncode != 0:
        raise OrchdError(
            ErrorCode.E018,
            f"{command}_switch_branch: git checkout {default} 失败: "
            f"{result.stderr.strip()[:300]}",
            [{"command": command, "hint": "切换失败前未写事件，可安全重试"}],
        )
    return {"checked_out_to": default}
