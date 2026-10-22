"""gitops guard — 守卫内核与共享低层助手域（W6-C1 拆分，纯搬移）。

来源：``orchd/gitops/guard.py``（1652 行，W6-C1 纯搬移）。

守卫执行器（``run_guard`` / ``_guard_failed`` / ``record_degraded_guard`` /
``_probe_guard_workspace_state``）、nogit 判定、降级留痕与会话锁降级原因，
以及被 write / checkout / restore 三域共用的低层助手
（``_clear_timeout_index_lock`` / ``commit_hint_for_branch`` /
``_sync_lag_exempted`` / ``_container_task_wt_exists``）。

**叶子域**：零包内导入（不反向依赖任何兄弟子模块，否则成加载期环）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from orchd.errors import ErrorCode, NotApplicableError, OrchdError

from orchd.gitops._const import (
    _GIT_TIMEOUT,
    _T,
)

from orchd.gitops.query import check_workspace_state

# 门禁执行器的「语义三分类 + 降级可审计」纪律说明见聚合入口 __init__.py 的 docstring 之后。

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
    message = f"guard_failed: 门禁 {guard_name} 校验故障，fail-closed 拒绝放行"
    details = [{
        **entry,
        "hint": hint or (
            "门禁没跑起来 ≠ 校验通过：请重试本命令；持续失败请检查 git 可用性、"
            "仓库规模或杀毒软件实时扫描，确认环境无异常后人工重试"
        ),
    }]
    # task-channel-a-severity-param-raise ②：本处原为
    # ``raise OrchdError(error_code, ...)``（参数化）——通道 A 扫描器按字面码枚举，
    # 该形态不被识别（全库唯一一处参数化 raise）。取值域经调用链证明恰为
    # {E018, E030}（run_guard 默认 E030；_probe_guard_workspace_state 传 E018），
    # 故显式分派为两个字面码站点使登记表可见；语义与文案零变化。
    if error_code == ErrorCode.E018:
        raise OrchdError(ErrorCode.E018, message, details) from exc
    raise OrchdError(ErrorCode.E030, message, details) from exc

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

def commit_hint_for_branch(branch: str | None) -> str:
    """E017 脏工作区处置指引：按分支限定提交动作（task-e017-hint-branch-triage）。

    - 任务分支（``task/*``）：``git add + git commit`` 为红线 #1 唯一豁免，明示；
    - 主分支 / 未知分支：不推手动提交（main 上无豁免），指引擎通道或报告；
    - 恒附幻影脏分支：内容零差异时勿补声明、无需提交（当前内容差分门禁下此类
      脏位本不应到达 E017，此为防御性指引，防旧引擎 / 边缘口径下的误动作）。
    """
    from orchd.line import parse_task_branch as _parse_tb

    if isinstance(branch, str) and _parse_tb(branch) is not None:
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
