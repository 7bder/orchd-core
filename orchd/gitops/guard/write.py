"""gitops guard — 写命令守卫域（W6-C1 拆分，纯搬移）。

来源：``orchd/gitops/guard.py``（1652 行，W6-C1 纯搬移）。

``guard_write_command`` 及其四个薄封装（claim / done_branch / clean_workspace /
review_write），以及工作区与分支守卫
（``_enforce_workspace_clean`` / ``_enforce_branch_allowed`` /
``_build_wrong_branch_hint``）。

依赖 ``core``（顶层导入）；不得反向依赖聚合入口。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

from orchd.gitops._const import (
    _GIT_ENCODING,
    _GIT_ERRORS,
    _GIT_TIMEOUT,
)

from orchd.gitops.session_lock import ensure_session_lock

from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

from .core import (
    GUARD_STATUS_FAILED,
    _container_task_wt_exists,
    _is_nogit_orchd_dir,
    _nogit_maindir_violation,
    _probe_guard_workspace_state,
    _record_git_unavailable_guard,
    _session_lock_degrade_reason,
    _sync_lag_exempted,
    commit_hint_for_branch,
    record_degraded_guard,
)

def _build_wrong_branch_hint(
    allowed_branches: set[str],
    command: str,
    project_root: Path,
) -> str:
    """构建 wrong_branch 错误的 hint（含 task branch worktree 位置指引）。"""
    expected = sorted(allowed_branches)
    # 单根命名空间（pass8 F1-A）：任务分支恒以 task/ 为根；旧 {line}/task/{id}
    # 不再识别（fail-closed）。
    from orchd.line import parse_task_branch

    task_branches = [b for b in expected if parse_task_branch(b) is not None]
    if not task_branches:
        return f"请先切换到 {' 或 '.join(expected)} 分支再执行 {command}"
    hint_parts = []
    for tb in task_branches:
        parsed = parse_task_branch(tb)
        assert parsed is not None  # 来自 task_branches 过滤，不可达 None
        task_id = parsed[1]
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
    non_task = [b for b in expected if parse_task_branch(b) is None]
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
