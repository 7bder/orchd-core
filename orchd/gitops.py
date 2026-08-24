"""Orchd 引擎 best-effort git 提交能力（叶子模块，标准库自包含）。

职责：在 ``done`` / ``amend`` 成功后，把实现者漏提交的改动兜底提交到
当前分支，把"提交纪律"从约定层升级为引擎兜底。与 ``onboard._try_git_branch`` /
``_try_git_merge`` 保持同一 best-effort 语义：任何失败（非 git 仓库、
git 不可用、提交失败）都不抛异常，仅返回结构化结果，由调用方放入
响应字段（对齐 ``review_submit`` 的 ``merged:false`` 契约）。

安全约束：
- 只对调用方显式声明的路径执行 ``git add``，且 ``git commit -- <paths>``
  同样限定路径——即使实现者预先 staged 了范围外文件，也不会被本模块提交；
- 绝不执行 ``git push``（远端推送归管理员）；
- 绝不新增 ledger 事件（事件格式与状态机零改动）。

依赖方向：gitops.py → 标准库（shutil / subprocess / pathlib）。会话锁的账本根解析
复用 ``orchd.ledger.resolve_store_dir`` 的组织语义，但为避免引入 ledger 依赖，
在本模块内对 ``ORCHD_HOME`` 做同语义的本地解析（存量 ORCHD_HOME 重定向场景行为
一致；改动时需与 ``orchd.ledger.resolve_store_dir`` 保持同步）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePath
from typing import Any

from orchd.errors import ErrorCode, OrchdError

# 单条 git 命令超时（秒），与 onboard.py 的 git 辅助保持一致量级
_GIT_TIMEOUT = 10

# git 输出统一按 UTF-8 解码（git 内部以 UTF-8 处理 commit message 等），
# 解码失败按替换符处理——避免中文 Windows 默认 GBK 代码页解码崩溃。
_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"


def _run_git(
    project_root: Path,
    args: list[str],
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """以 UTF-8 解码运行 git 命令（cwd 限定 project_root）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        encoding=_GIT_ENCODING,
        errors=_GIT_ERRORS,
        timeout=timeout,
    )


def _shell_quote(value: str) -> str:
    """shell 单引号转义：' 替换为 '\\''（单引号闭合-转义-重开）。

    用于把文件名安全嵌入 shell 脚本字面量，防 shell 注入
    （hook 模板中文件名来自任务定义，属可信输入，但仍按防御性处理）。
    """
    return "'" + value.replace("'", "'\\''") + "'"


def get_current_branch(project_root: Path) -> str | None:
    """获取当前 git 分支名。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。
    """
    try:
        result = _run_git(project_root, ["branch", "--show-current"])
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_head_commit(project_root: Path) -> str | None:
    """获取当前 HEAD 的 commit SHA（用于 review baseline 追踪）。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。
    """
    try:
        result = _run_git(project_root, ["rev-parse", "HEAD"])
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_default_branch(project_root: Path) -> str | None:
    """检测仓库的默认分支名（best-effort）。

    优先级：
    1. ``git config init.defaultBranch``（用户显式配置）
    2. 本地存在 ``main`` 分支
    3. 本地存在 ``master`` 分支
    4. 都没有返回 None

    非 git 仓库、git 不可用或任何异常返回 None。
    """
    try:
        # 1. 显式配置优先
        cfg = _run_git(project_root, ["config", "--get", "init.defaultBranch"])
        if cfg.returncode == 0:
            name = cfg.stdout.strip()
            if name:
                return name
        # 2. 探测本地常见默认分支名
        for candidate in ("main", "master"):
            check = _run_git(project_root, ["rev-parse", "--verify", "--quiet", candidate])
            if check.returncode == 0:
                return candidate
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def check_workspace_state(project_root: Path) -> dict[str, Any]:
    """检查当前 git 工作区状态：分支名 + 干净度（best-effort）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"available": False}`` 非 git 仓库 / git 不可用 / 异常
              （调用方应静默降级，沿用 best-effort 契约）。
            - ``{"available": True, "branch": <str|None>, "clean": <bool>}``
              branch 为当前分支名（detached HEAD 时为 None）；
              clean 表示无已跟踪文件改动（untracked 文件不视为脏，
              与"工作区干净 = 无已跟踪文件改动"的约定一致）。
    """
    if shutil.which("git") is None:
        return {"available": False}
    try:
        check = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {"available": False}
        branch = get_current_branch(project_root)
        # 已跟踪文件改动（不含 untracked）：--porcelain 输出非空即脏
        status = _run_git(project_root, ["status", "--porcelain", "--untracked-files=no"])
        clean = status.returncode == 0 and not status.stdout.strip()
        return {"available": True, "branch": branch, "clean": clean}
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {"available": False}


def list_tracked_changes(project_root: Path) -> list[str] | None:
    """返回已跟踪文件的未提交改动路径列表（best-effort）。

    用于 amend / intake 的"非摄入产物干净"守卫：区分「摄入产物（IDEAS.md /
    ROADMAP.md / _master.json）允许未提交」与「其余已跟踪改动必须提交」。

    Returns:
        - ``list[str]``：已跟踪文件的改动路径（相对 project_root）。
        - ``None``：非 git 仓库 / git 不可用 / 异常（调用方降级为不阻断）。

    Note:
        仅已跟踪文件（``--untracked-files=no``），与"工作区干净 = 无已跟踪
        改动"的语义一致；untracked 工具/配置文件不列入。
    """
    if shutil.which("git") is None:
        return None
    try:
        check = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return None
        status = _run_git(project_root, ["status", "--porcelain", "--untracked-files=no"])
        if status.returncode != 0:
            return None
        files: list[str] = []
        for line in status.stdout.splitlines():
            if len(line) < 4:
                continue
            # porcelain v1：`XY path`（XY 各 1 字符 + 空格）；rename 为
            # `R  old -> new`（取箭头后路径，保守处理，避免把 rename 目标误列）
            code, _, path = line[:2], line[2], line[3:]
            if code == "R " or code.startswith("R"):
                arrow = path.find(" -> ")
                if arrow != -1:
                    path = path[arrow + 4:]
            files.append(path.strip())
        return files
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


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


def _has_linked_worktrees(project_root: Path) -> bool:
    """判定仓库是否处于多 worktree 并行场景（存在 linked worktrees）。

    ``git worktree list --porcelain`` 每个 worktree 块以 ``worktree `` 开头；
    >1 即存在 linked worktree（主 worktree + 至少一个 linked）。
    单 worktree（默认）场景返回 False——不创建 merge-wt，保持既有行为零回归
    （checkout_default_strict 仍可在 agent worktree 内切回 main）。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            return False
        return proc.stdout.count("worktree ") > 1
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def ensure_session_lock(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
    session_id: str | None = None,
) -> None:
    """确保当前 session 可写入：门锁串行化"检查+获取"，被其他 session 持有则 E019。

    best-effort：门锁 / 锁获取失败（IO 错误）不抛异常，静默降级。

    与旧"check-then-act"区别：旧实现先 :func:`session_lock_check` 再
    :func:`session_lock_acquire`（覆盖写），两个并发 session 都可通过检查并同时
    "获取"（后写覆盖先写）。本实现先用一个**门锁**（flock gate，ExclusiveFileLock）
    串行化整个"检查 + 写入"——一次仅一个进程能通过检查并写锁标记，其余读到该标记后
    据 session 归属判 E019 或幂等复用（刷新覆盖写）。

    Session Identity Layer：同 ``agent_id`` 但不同 ``session_id`` 视为另一个
    session（即使指纹相同），防止同 agent 多会话互踩/误释放锁。
    """
    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    from orchd.lockfile import ExclusiveFileLock

    # 门锁：串行化后续"检查 + 写入"，消除 check-then-act 竞态。
    gate = ExclusiveFileLock(_get_session_gate_path(orchd_dir))
    acquired_gate = False
    try:
        gate.acquire(blocking=True, timeout_s=10.0)
        acquired_gate = True
    except OrchdError:
        # 门锁获取失败（超时/被占）：静默降级，仅凭当前标记判定（best-effort）
        acquired_gate = False
    try:
        check = session_lock_check(orchd_dir)
        if check.get("locked"):
            holder = check.get("agent_id", "unknown")
            holder_session = check.get("session_id") or ""
            if holder == agent_id and (not holder_session or holder_session == session_id):
                # 本 session 已持锁：刷新覆盖写（幂等复用）
                session_lock_acquire(orchd_dir, agent_id, branch, session_id=session_id)
                return
            raise OrchdError(
                ErrorCode.E019,
                f"workspace_busy: 工作区被 '{holder}' 占用（分支 {check.get('branch', 'N/A')}，"
                f"已锁定 {check.get('age_min', 0):.1f} 分钟）",
                [{
                    "agent_id": agent_id,
                    "holder": holder,
                    "holder_session": holder_session,
                    "holder_branch": check.get("branch"),
                    "holder_timestamp": check.get("timestamp"),
                    "age_min": check.get("age_min"),
                    "hint": "等待该 session 结束，或使用 watchdog --timeout 0 强制释放僵死锁",
                }],
            )
        # 未被持有 / 损坏 / 超时（可覆盖）：直接写锁标记
        session_lock_acquire(orchd_dir, agent_id, branch, session_id=session_id)
    finally:
        if acquired_gate:
            gate.release()


def guard_write_command(
    project_root: Path | None,
    *,
    allowed_branches: set[str] | None,
    require_clean: bool,
    command: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> None:
    """L1 分支守卫 + L2 session 锁：写命令前校验当前分支、工作区干净度与 session 锁（best-effort）。

    - ``project_root`` 为 None（单元测试）或 git 不可用/非 git 仓库
      （``check_workspace_state`` 返回 available=False）→ 静默跳过。
    - 分支不在 ``allowed_branches`` → E018 wrong_branch。
    - ``require_clean`` 且工作区有已跟踪改动 → E017 dirty_workspace。
    - git 可用且 ``orchd_dir`` 和 ``agent_id`` 均提供时，检查 session lock → E019。
    """
    branch = None
    git_available = False
    if project_root is not None:
        state = check_workspace_state(project_root)
        if state.get("available"):
            git_available = True
            branch = state.get("branch")
            if allowed_branches is not None and branch not in allowed_branches:
                expected = sorted(allowed_branches)
                raise OrchdError(
                    ErrorCode.E018,
                    f"wrong_branch: {command} 须在 {expected} 分支执行，当前在 '{branch}'",
                    [{
                        "command": command,
                        "current_branch": branch,
                        "expected_branches": expected,
                        "hint": f"请先切换到 {' 或 '.join(expected)} 分支再执行 {command}",
                    }],
                )
            if require_clean and not state.get("clean"):
                raise OrchdError(
                    ErrorCode.E017,
                    f"dirty_workspace: {command} 要求工作区干净（无已跟踪文件改动）",
                    [{
                        "command": command,
                        "hint": "请先提交或还原已跟踪文件改动（untracked 工具/配置文件不阻塞）",
                    }],
                )

    if git_available and orchd_dir is not None and agent_id is not None:
        ensure_session_lock(orchd_dir, agent_id, branch)


def guard_claim(
    project_root: Path | None,
    *,
    role: str,
    task_id: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> None:
    """claim 前置守卫（L1+L2 意图化封装）：调用点不再拼 allowed_branches / require_clean。

    - reviewer：须在 ``task/{task_id}`` 分支且工作区干净（审查对象是分支上的已提交 diff）；
    - implementer：须在默认分支（main/master）且工作区干净（引擎要从当前 HEAD 建任务分支，
      脏工作区会导致 checkout -b 后分支被污染）。
    """
    if role == "reviewer":
        guard_write_command(
            project_root,
            allowed_branches={f"task/{task_id}"},
            require_clean=True,
            command="review claim",
            orchd_dir=orchd_dir,
            agent_id=agent_id,
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
        )


def guard_done_branch(
    project_root: Path | None,
    *,
    task_id: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> None:
    """done 前置分支守卫（L1+L2 意图化封装）：须在 ``task/{task_id}`` 或默认分支。

    **不要求干净**——files_to_edit 范围内的未提交改动是正常状态（由引擎
    ensure_committed 兜底提交）；干净校验放在自动提交之后
    （见 ``guard_clean_workspace``，提交后仍有已跟踪改动 = 范围外改动）。
    """
    default = get_default_branch(project_root) if project_root else None
    default = default or "main"
    guard_write_command(
        project_root,
        allowed_branches={f"task/{task_id}", default},
        require_clean=False,
        command="done",
        orchd_dir=orchd_dir,
        agent_id=agent_id,
    )


def guard_clean_workspace(
    project_root: Path | None,
    *,
    command: str,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> None:
    """仅干净校验（任意分支，L1+L2 意图化封装）：用于 done 自动提交后的范围外改动兜底。"""
    guard_write_command(
        project_root,
        allowed_branches=None,
        require_clean=True,
        command=command,
        orchd_dir=orchd_dir,
        agent_id=agent_id,
    )


def guard_review_write(
    project_root: Path | None,
    *,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> None:
    """review 提交守卫（L1+L2 意图化封装）：任意分支，不要求干净。"""
    guard_write_command(
        project_root,
        allowed_branches=None,
        require_clean=False,
        command="review",
        orchd_dir=orchd_dir,
        agent_id=agent_id,
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
        raise OrchdError(
            ErrorCode.E017,
            f"{command}_switch_branch: 工作区非干净，拒绝强制切换(避免把未提交改动带离"
            "任务分支)，请先提交或还原已跟踪改动后重试",
            [{"command": command, "hint": "请先提交或还原已跟踪文件改动后重试"}],
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


def parse_conflicts(output: str) -> list[str]:
    """从 git merge 输出中提取冲突文件路径列表（merge 冲突判定，task-14-git-policy-layer）。

    遍历输出行，命中 ``CONFLICT`` 的行取其最后一个词作为冲突文件路径。
    无冲突返回空列表。
    """
    files: list[str] = []
    for line in (output or "").split("\n"):
        if "CONFLICT" in line:
            parts = line.split()
            if parts:
                files.append(parts[-1])
    return files


def main_worktree_root(project_root: Path) -> Path:
    """从任意 worktree 定位主工作树根（task-14-merge-main-tree，AC1）。

    ``git rev-parse --git-common-dir`` 返回公共 git 目录：主 worktree 返回
    ``<根>/.git``，linked worktree（任务 worktree）返回同一主 ``.git``。
    取其父目录即主工作树根（merge 在主工作树内执行、永不切任务 worktree 的 main）。
    非 git / 解析失败回退 ``project_root``（best-effort，flat 单会话零回归）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return project_root
        git_dir = proc.stdout.strip()
        if not git_dir:
            return project_root
        p = Path(git_dir)
        if not p.is_absolute():
            p = (project_root / p).resolve()
        else:
            p = p.resolve()
        if p.name == ".git":
            return p.parent
        return project_root
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return project_root


def is_task_worktree(project_root: Path) -> bool:
    """判定当前目录是否为 linked worktree（任务 worktree，task-14-merge-main-tree AC3）。

    ``git rev-parse --git-dir``：linked worktree 返回 ``<common>/.git/worktrees/<name>``
    （含 ``worktrees/`` 段），主 worktree 返回 ``<根>/.git``。含 ``worktrees/`` → True。
    非 git / 解析失败返回 False（best-effort）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return False
        return "worktrees/" in (proc.stdout.strip() or "")
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def ensure_committed(
    project_root: Path,
    paths: list[str],
    message: str,
) -> dict[str, Any]:
    """best-effort 将 ``paths`` 范围内的未提交改动提交到当前分支。

    Args:
        project_root: 仓库根目录（git 命令的 cwd）。
        paths: 允许提交的路径列表（相对 project_root 或绝对路径）。
            调用方须传入任务声明的 ``files_to_edit`` 或固定资产路径。
        message: 提交消息。

    Returns:
        结构化结果，永不抛异常：
            - ``{"performed": True, "reason": "committed", "message": message}``
              引擎实际创建了一个提交。
            - ``{"performed": False, "reason": "no_changes", "message": message}``
              范围内无未提交改动（实现者已自行提交、路径不存在或本就无改动），
              无需提交。
            - ``{"performed": False, "reason": "not_a_git_repo"}``
              当前目录不是 git 工作树。
            - ``{"performed": False, "reason": "git_unavailable"}``
              git 可执行文件不在 PATH。
            - ``{"performed": False, "reason": "commit_failed", "message": <stderr>}``
              git diff / commit 失败（如 user 未配置、模拟异常）。

    Raises:
        永不抛异常；所有 git 失败均降级为结构化结果。
    """
    if not paths:
        return {"performed": False, "reason": "no_paths"}

    if shutil.which("git") is None:
        return {"performed": False, "reason": "git_unavailable"}

    try:
        check = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {"performed": False, "reason": "not_a_git_repo"}
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "not_a_git_repo"}

    # intake-commit-enforcement（2026-08-14）：过滤不存在的路径（git 环境已确认
    # 之后）——部分路径缺失（如 ROADMAP.md 尚未创建）不应导致整个 git add/commit
    # 因 pathspec 不匹配而整体 fatal（git add 遇错误 pathspec 会中止，连带阻断
    # 其余存在的摄入产物提交）。
    existing_paths: list[str] = []
    for p in paths:
        ap = Path(p) if Path(p).is_absolute() else project_root / p
        if ap.exists():
            existing_paths.append(p)
    if not existing_paths:
        # 声明路径均不存在 → 范围内无实际文件改动可提交
        return {"performed": False, "reason": "no_changes", "message": message}
    paths = existing_paths

    # 只 add 声明范围。add 失败（如路径不存在）不阻断：
    # 是否真的"无改动"由下一步 diff 精确判断（diff 也限定 paths）。
    try:
        _run_git(project_root, ["add", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # 范围内是否有 staged 改动：0=无，1=有，其余为 git 错误
    try:
        diff = _run_git(project_root, ["diff", "--cached", "--quiet", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git diff failed"}
    if diff.returncode == 0:
        return {"performed": False, "reason": "no_changes", "message": message}
    if diff.returncode != 1:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (diff.stderr or diff.stdout).strip()[:300] or "git diff --cached failed",
        }

    # commit 同样限定 paths：不提交声明范围外的 staged 内容，不 push
    try:
        commit = _run_git(project_root, ["commit", "-m", message, "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git commit failed"}
    if commit.returncode != 0:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (commit.stderr or commit.stdout).strip()[:300] or "git commit failed",
        }
    return {"performed": True, "reason": "committed", "message": message}


def head_drift_check(
    project_root: Path,
    ref: str = "HEAD",
    base_ref: str = "main",
) -> dict[str, Any]:
    """提交前 HEAD 推进检测（task-intake-file-lock AC3，git 层 TOCTOU 防护）。

    准入（intake/amend）在工作区写入全局文件（_master.json / IDEAS.md /
    ROADMAP.md）前，比对 "本地 ref 相对 base_ref 是否已被并行推进"：
    记录本次会话读取 ref 时的 commit，与提交时/写入时的当前 commit 比较——
    若期间 base_ref（如 main）被其他 agent 推进，则本地工作区基于的 base 已过期，
    此时提交会基于过期 base、与并行改动冲突。检测到推进则返回 drift=True，
    caller 应拒绝并提示先更新/重拉。

    best-effort：非 git / ref 解析失败降级 False（不误伤）。

    Args:
        project_root: 仓库根目录（git 命令 cwd）。
        ref: 待检测的 ref（默认 HEAD）。
        base_ref: 比对基线 ref（默认 main）。

    Returns:
        ``{"drift": False}`` 无推进（base 未变化）或不可解析。
        ``{"drift": True, "base_sha": <str>, "head_sha": <str>,
          "ref": <str>, "base_ref": <str>}`` base 与本地已分叉，应重拉。
    """
    if shutil.which("git") is None:
        return {"drift": False}
    try:
        head = _run_git(project_root, ["rev-parse", "--verify", ref])
        if head.returncode != 0:
            head_sha = ""
        else:
            head_sha = (head.stdout or "").strip()
        base = _run_git(project_root, ["rev-parse", "--verify", base_ref])
        if base.returncode != 0:
            return {"drift": False}
        base_sha = (base.stdout or "").strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"drift": False}
    if not head_sha or not base_sha:
        return {"drift": False}
    # base 是 head 的祖先 → 本地未落后（head 包含 base）无漂移；
    # merge-base 非 base_sha → 已分叉/被并行推进，检测漂移。
    try:
        merge_base = _run_git(project_root, ["merge-base", ref, base_ref])
        mb = (merge_base.stdout or "").strip() if merge_base.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError):
        mb = ""
    if mb == base_sha:
        return {"drift": False}
    return {"drift": True, "base_sha": base_sha, "head_sha": head_sha,
            "ref": ref, "base_ref": base_ref}


# ─────────────────────────────────────────────────────────────────────────────
# L3 pre-commit hook 越界提交拦截
# ─────────────────────────────────────────────────────────────────────────────

_HOOK_FILENAME = "pre-commit"


def _get_hook_path(project_root: Path) -> Path:
    """返回 .git/hooks/pre-commit 路径。"""
    return project_root / ".git" / "hooks" / _HOOK_FILENAME


def hook_install(
    project_root: Path,
    task_id: str,
    files_to_edit: list[str],
    exempt_files: list[str] | None = None,
) -> dict[str, Any]:
    """安装 pre-commit hook（任务活跃期越界提交拦截）。

    Hook 逻辑（2026-08-08 增强：由"任务分支校验"升级为"任务活跃校验"）：
    - 读 ledger 判断任务是否活跃（该 task_id 最近事件为 CLAIMED，且无后续
      DONE / RETRACT / REVIEW_SUBMITTED）→ 任务未活跃 → 放行（exit 0）
    - 任务活跃 → **任何分支**都校验 staged 文件 ⊆ files_to_edit
      （堵住任务活跃期间在 main / 幽灵分支越界提交实现内容的事故：
      b3c2e84 直接在 main 提交、task/task-1 幽灵分支）
    - 固定资产豁免：.orchd/_master.json、IDEAS.md 与 .orchd/IDEAS.md
      （amend 自动提交的路径，cli.py ensure_committed 在 main 执行，
      若不豁免会被自身 hook 拦截）
    - R1-b 审查期实现者冻结：任务分支上 REVIEW_CLAIMED 且无后续结论 → 拒绝
    - 越界 → 拒绝提交（exit 1），打印越界文件清单
    - --no-verify 可绕过（git 原生行为，hook 无需特殊处理）

    Args:
        project_root: git 仓库根目录。
        task_id: 当前任务 ID（绑定到 hook 内容）。
        files_to_edit: 允许修改的文件列表（相对路径）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"installed": True, "path": <str>}`` hook 安装成功。
            - ``{"installed": False, "reason": "not_a_git_repo"}`` 非 git 仓库。
            - ``{"installed": False, "reason": "io_error", "error": <str>}``
              写入失败（best-effort 降级）。
            - ``{"installed": False, "reason": "unsafe_task_id", "error": <str>}``
              task_id 含 shell 元字符（注入面），拒绝写入 hook。
    """
    # P1-5 安全加固：task_id 会被裸插值进 shell hook（grep/echo），须严格白名单，
    # 否则单引号/`$(...)`/换行可逃逸出 shell 引号 → 任意命令注入。
    if not task_id or any(not (c.isalnum() or c in "-_") for c in task_id):
        return {
            "installed": False,
            "reason": "unsafe_task_id",
            "error": "task_id 含非 [A-Za-z0-9_-] 字符，拒绝写入 pre-commit hook（防 shell 注入）",
        }

    hooks_dir = project_root / ".git" / "hooks"
    if not hooks_dir.parent.exists():
        return {"installed": False, "reason": "not_a_git_repo"}

    exempt = exempt_files or []

    # 生成 hook 脚本内容（使用纯 shell 逻辑，避免 JSON 解析复杂性）
    files_list = "\n".join(f"#   {f}" for f in files_to_edit)
    if exempt:
        files_list += "\n# Exempt files:"
        files_list += "\n" + "\n".join(f"#   {f}" for f in exempt)
    # 文件名用单引号转义（_shell_quote，防 shell 注入；
    # 既有双引号注入面一并收敛）
    files_check = "\n".join(
        f'    if [ "$FILE" = {_shell_quote(f)} ]; then IN_SCOPE=yes; fi'
        for f in files_to_edit
    )
    exempts_check = "\n".join(
        f'    if [ "$FILE" = {_shell_quote(f)} ]; then IN_SCOPE=yes; fi'
        for f in exempt
    )
    # 无豁免时不输出 Exempt files 标题行（保持与无 exempt_files 行为一致）
    exempt_header = (
        '    echo "Exempt files for this task:"\n' if exempt else ""
    )

    hook_content = f"""#!/bin/sh
# orchd L3 pre-commit hook (auto-generated, do not edit)
# Task: {task_id}
# Allowed files:
{files_list}

LEDGER=".orchd/_ledger.jsonl"

# 1) 任务未活跃 → 放行：读 ledger 判该任务是否处于活跃状态
#    （最近事件为 CLAIMED / REVIEW_CLAIMED，且无后续 DONE / RETRACT /
#      REVIEW_SUBMITTED）——in_review 阶段任务同样活跃（审查中，实现者
#      仍可能补提交，需拦截越界）
if [ -f "$LEDGER" ]; then
    LAST_TASK=$(grep -F '"task_id":"{task_id}"' "$LEDGER" 2>/dev/null | grep -E '"type":"(CLAIMED|REVIEW_CLAIMED|DONE|RETRACT|REVIEW_SUBMITTED)"' | tail -1)
    case "$LAST_TASK" in
        *CLAIMED*|*REVIEW_CLAIMED*)
            # 任务活跃，继续校验（任何分支）
            ;;
        *)
            # 任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行
            exit 0
            ;;
    esac
else
    # 无 ledger（异常环境）→ 保守放行（best-effort）
    exit 0
fi

# 2) R1-b 审查期实现者冻结：任务分支上最后 review 事件是 REVIEW_CLAIMED
#    （审查进行中，无后续 REVIEW_SUBMITTED / RETRACT）→ 拒绝提交，保护审查基线。
if [ -f "$LEDGER" ]; then
    LAST_REVIEW=$(grep -F '"task_id":"{task_id}"' "$LEDGER" 2>/dev/null | grep -E '"type":"(REVIEW_CLAIMED|REVIEW_SUBMITTED|RETRACT)"' | tail -1)
    case "$LAST_REVIEW" in
        *REVIEW_CLAIMED*)
            echo "orchd E017: review in progress, commit blocked on task/{task_id}"
            echo "任务正在审查中（REVIEW_CLAIMED）。请等待 reviewer 提交结论，或先执行 retract 撤回审查再提交。"
            echo "To bypass: git commit --no-verify"
            exit 1
            ;;
    esac
fi

# 3) 获取 staged 文件列表（相对路径）
STAGED=$(git diff --cached --name-only --diff-filter=ACM)

# 无 staged 文件 → 放行
if [ -z "$STAGED" ]; then
    exit 0
fi

# 4) 校验每个 staged 文件：固定资产豁免 或 在允许列表内
    OUT_OF_SCOPE=""
    for FILE in $STAGED; do
        IN_SCOPE=no
        # 固定资产豁免（引擎自动提交路径，不在任务 files_to_edit 内）：
        # .orchd/_master.json、IDEAS.md（根布局）与 .orchd/IDEAS.md（发布态自包含
        # .orchd 布局）、ROADMAP.md（根布局）与 .orchd/ROADMAP.md（发布态）——
        # amend 在 main 分支提交它们，若不豁免会被本 hook 拦截
        # （引擎自动提交零改动）。
        case "$FILE" in
            .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md|.orchd/ROADMAP.md)
                IN_SCOPE=yes
                ;;
        esac
{files_check}
{exempts_check}
    if [ "$IN_SCOPE" != "yes" ]; then
        OUT_OF_SCOPE="$OUT_OF_SCOPE$FILE "
    fi
done

# 5) 有越界文件 → 拒绝提交
if [ -n "$OUT_OF_SCOPE" ]; then
    echo "orchd E020: out-of-scope commit blocked (task {task_id} active)"
    echo "Out-of-scope files:"
    for F in $OUT_OF_SCOPE; do
        echo "  - $F"
    done
    echo ""
    echo "Allowed files for this task:"
{chr(10).join(f'    echo "  - {f}"' for f in files_to_edit)}
{exempt_header}{chr(10).join(f'    echo "  - {f}"' for f in exempt)}
    echo ""
    echo "To bypass: git commit --no-verify"
    exit 1
fi

exit 0
"""

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = _get_hook_path(project_root)
        hook_path.write_text(hook_content, encoding="utf-8")
        # 设置可执行权限（POSIX）
        if hasattr(hook_path, "chmod"):
            hook_path.chmod(0o755)
        return {"installed": True, "path": str(hook_path)}
    except (OSError, IOError) as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}


def hook_uninstall(project_root: Path) -> dict[str, Any]:
    """卸载 pre-commit hook（幂等：hook 不存在时不报错）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"uninstalled": True, "reason": "removed"}`` hook 已删除。
            - ``{"uninstalled": True, "reason": "not_exists"}`` hook 本就不存在（幂等）。
            - ``{"uninstalled": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    hook_path = _get_hook_path(project_root)
    if not hook_path.exists():
        return {"uninstalled": True, "reason": "not_exists"}
    try:
        _safe_delete(hook_path, project_root)
        return {"uninstalled": True, "reason": "removed"}
    except (OSError, IOError) as exc:
        return {"uninstalled": False, "reason": "io_error", "error": str(exc)}


def _safe_delete(path: Path, base_dir: Path) -> None:
    """沙箱安全的文件删除（unlink 被劫持时降级为重命名移出）。

    部分沙箱把 ``Path.unlink`` 劫持为"移入回收站"（safe-delete），回收站不可用时
    FAIL_CLOSED 抛 OSError（2026-08-06 实踩：windows-sandbox-recycle-bin-unavailable，
    全量 pytest 稳定触发）。此时降级为把文件重命名移动到系统临时目录
    （重命名不经删除劫持），目标位置"消失"，语义等效删除；
    残留物在系统 temp（orchd-trash-*），可手动清理，不影响工作区。

    Args:
        path: 待删除文件。
        base_dir: 用于生成唯一残留名的基准目录名。

    Raises:
        OSError: unlink 与降级重命名均失败时向上抛（由调用方 best-effort 降级）。
    """
    try:
        path.unlink()
    except OSError:
        dest = Path(tempfile.gettempdir()) / (
            f"orchd-trash-{base_dir.name}-{uuid.uuid4().hex[:8]}-{path.name}"
        )
        os.replace(path, dest)


# ------------------------------------------------------------------
# L2 session 工作区锁（2026-08-06 task-l2-session-lock）
# ------------------------------------------------------------------

_SESSION_LOCK_FILENAME = ".session.lock"
# 会话门锁（flock gate）：串行化 ensure_session_lock 的"检查+写入"，防 check-then-act。
_SESSION_GATE_FILENAME = ".session.gate.lock"
# 默认锁超时（分钟），watchdog 超时自动释放僵死锁
_SESSION_LOCK_TIMEOUT_MIN = 60

# 新式 flock 活性锁标记：session_lock_acquire 写入 JSON 时携带该字段，
# 表示锁文件已被持锁进程的 OS fd（flock/msvcrt）活性托管。该字段为真时，
# session_lock_check 优先做非阻塞 OS 探活判定 stale；缺失（旧纯 JSON 锁）
# 时保持按 timeout/no_active_task 兼容判定，不误清。
_SESSION_LOCK_FLOCK_MARKER = "flock_active"

# 本进程持有的 session 锁注册表：lock_path → ExclusiveFileLock。
# 持锁期间保持 fd 打开（flock 由内核托管，进程退出自动释放），
# session_lock_release 据此关闭 fd，避免 fd 泄漏。
_SESSION_LOCK_REGISTRY: dict[str, Any] = {}


def _resolve_store_root(orchd_dir: Path) -> Path:
    """解析会话锁的账本根（与 ``orchd.ledger.resolve_store_dir`` 组织语义完全一致）。

    惰性委托 ``orchd.ledger.resolve_store_dir``（单一来源）：
    - ``ORCHD_HOME`` 设置时重定向到外部目录（多 worktree 共享账本根）；
    - container 布局（``.orchd/.layout.json``）→ ``<容器>/.orchd-runtime``；
    - flat 场景回退 ``orchd_dir``（零回归）。

    task-session-lock-lifecycle（改 A）：此前只认 ``ORCHD_HOME``、不读 container
    布局标记，导致 container 下会话锁落进 ``main/.orchd/.session.lock`` 而非与
    Store 锁同根（``.orchd-runtime/``），两把互斥锁根不一致。惰性导入避免循环
    依赖（``orchd.ledger`` 模块级仅引用 ``orchd.errors``）。
    """
    from orchd.ledger import resolve_store_dir

    return resolve_store_dir(orchd_dir)


def _git_worktree_name(orchd_dir: Path) -> str | None:
    """解析当前 worktree 的 git 名称（``git worktree`` 场景）。

    主 worktree / 非 git / git 不可用时返回 ``None``（会话锁回退主维度）。
    ``git rev-parse --git-dir``：linked worktree 返回 ``<common>/.git/worktrees/<name>``
    （含 ``worktrees/`` 段），主 worktree 返回 ``<root>/.git``（或无后缀）。
    取 ``worktrees/`` 之后的段作为 worktree 维度名；解析失败静默降级。
    """
    project_root = orchd_dir.parent
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        git_dir = proc.stdout.strip()
        marker = "worktrees/"
        if marker not in git_dir:
            return None
        name = git_dir.rsplit(marker, 1)[-1].split(os.sep)[0].strip()
        return name or None
    except (OSError, subprocess.SubprocessError):
        return None


def _get_session_lock_path(orchd_dir: Path) -> Path:
    """返回 session lock 文件路径（worktree 维度唯一）。

    - 账本根解析遵循 ``ORCHD_HOME`` 重定向（多 worktree 共享同一账本根）；
    - worktree 维度后缀：git linked worktree 场景解析出 worktree 名时，
      锁文件按 ``.session-<worktree>`` 命名，不同 worktree 可分别持有锁不互踩；
      主 worktree / 非 git / 解析失败回退 ``.session.lock``（默认单 worktree 场景，
      worktree 维度唯一即全局唯一，行为与改造前一致）。
    """
    base = _resolve_store_root(orchd_dir) / _SESSION_LOCK_FILENAME
    wt = _git_worktree_name(orchd_dir)
    if wt:
        base = _resolve_store_root(orchd_dir) / f".session-{wt}.lock"
    return base


def _get_session_gate_path(orchd_dir: Path) -> Path:
    """返回 session 门锁文件路径（与 session lock 同根，worktree 维度唯一）。

    门锁为 flock gate，用于串行化 :func:`ensure_session_lock` 的"检查+写入"；
    与 session lock 文件（标记文件）分离，避免"标记文件被删除导致门锁失效"。
    """
    base = _resolve_store_root(orchd_dir) / _SESSION_GATE_FILENAME
    wt = _git_worktree_name(orchd_dir)
    if wt:
        base = _resolve_store_root(orchd_dir) / f".session-gate-{wt}.lock"
    return base


def session_lock_acquire(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """写入 session lock 文件（agent_id + session_id + nonce + branch + timestamp）。

    并发互斥由 :func:`ensure_session_lock` 内的**门锁**（flock gate）保证：一次仅一个
    进程能通过"检查 + 写入"，从而消除 check-then-act 竞态；本函数自身覆盖写入
    （幂等，便于同 session 复用/刷新）。

    task-session-lock-autoclean（改 B）：新式锁额外持有 **OS flock/msvcrt fd**
    （复用 :class:`orchd.lockfile.ExclusiveFileLock`）作为进程活性探针——先 flock
    再写 JSON（消除"JSON 在但无 flock"窗口），JSON 写入 ``flock_active: true``
    标记；fd 登记到模块级注册表，由 :func:`session_lock_release` 释放关闭。
    进程异常退出时内核自动释放 flock，后续检查可据此判定 stale 并自动清理，
    不再依赖纯 timeout。

    Args:
        orchd_dir: .orchd 目录路径。
        agent_id: 当前 session 的 agent ID。
        branch: 当前 git 分支名（可选，None 表示非 git 或 detached HEAD）。
        session_id: 当前 session 的唯一 ID；缺省时从环境解析，旧调用点自动兼容。

    Returns:
        结构化结果，永不抛异常：
            - ``{"acquired": True, "path": <str>}`` 锁文件写入成功。
            - ``{"acquired": True, "reused": True, "path": <str>}``
              本进程已持同一锁，仅刷新 JSON（fd 复用）。
            - ``{"acquired": False, "reason": "io_error", "error": <str>}``
              写入失败（best-effort 降级，不阻塞状态机）。

    Note:
        锁文件仅作并发互斥载体，不再承载身份：agent 身份由宿主注入的
        ``ORCHD_SESSION_ID`` 派生（``orchd.ledger.resolve_agent_id``），
        锁被覆盖 / 强释放不会改变 agent 身份（会话级身份稳定）。
    """
    import json
    from datetime import datetime, timezone

    from orchd.lockfile import ExclusiveFileLock

    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    lock_path = _get_session_lock_path(orchd_dir)
    lock_data = {
        "agent_id": agent_id,
        "session_id": session_id or "",
        "nonce": os.urandom(8).hex(),
        "branch": branch,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        _SESSION_LOCK_FLOCK_MARKER: True,
    }
    try:
        # worktree 维度锁可能落在 ORCHD_HOME 重定向根下，父目录未必存在（多 worktree 共享账本根时每个 worktree 先建自己的锁）
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 本进程已持同一锁：复用 fd（同 session 刷新覆盖写，ExclusiveFileLock 可重入）
        existing = _SESSION_LOCK_REGISTRY.get(str(lock_path))
        if existing is not None:
            lock_path.write_text(
                json.dumps(lock_data, ensure_ascii=False), encoding="utf-8"
            )
            return {"acquired": True, "reused": True, "path": str(lock_path)}
        # 先持有 OS flock（非阻塞，被他人持有即失败），再写 JSON 标记。
        # flock 由内核托管：本进程退出/崩溃时自动释放，检查方可探活判定 stale。
        flock = ExclusiveFileLock(lock_path)
        try:
            flock.acquire(blocking=False, timeout_s=0.5)
        except OrchdError as exc:
            return {
                "acquired": False,
                "reason": "io_error",
                "error": f"flock acquire failed: {exc}",
            }
        lock_path.write_text(json.dumps(lock_data, ensure_ascii=False), encoding="utf-8")
        _SESSION_LOCK_REGISTRY[str(lock_path)] = flock
        return {"acquired": True, "path": str(lock_path)}
    except (OSError, IOError) as exc:
        return {"acquired": False, "reason": "io_error", "error": str(exc)}


def session_lock_release(orchd_dir: Path) -> dict[str, Any]:
    """释放 session lock（幂等：锁文件不存在时不报错）。

    task-session-lock-autoclean（改 B）：若本进程持有该锁的 flock fd
    （注册表登记），先释放 flock 并关闭 fd，再删除 JSON 标记文件；
    非本进程持有的锁（watchdog 清理他人僵死锁）直接删除标记文件
    （flock 由持锁进程退出/释放时内核回收）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"released": True, "reason": "removed"}`` 锁文件已删除。
            - ``{"released": True, "reason": "not_exists"}`` 锁文件本就不存在（幂等）。
            - ``{"released": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    lock_path = _get_session_lock_path(orchd_dir)
    # 先释放本进程持有的 flock fd（若持有），再处理标记文件
    flock = _SESSION_LOCK_REGISTRY.pop(str(lock_path), None)
    if flock is not None:
        flock.release()
    if not lock_path.exists():
        return {"released": True, "reason": "not_exists"}
    # P2-6：删除标记前探测 flock 活性——他人仍持活锁时不得 unlink（flock-unlink 竞态：
    # 同路径新 inode 会被新进程重新加锁，破坏互斥）。活锁跳过，仅 stale（无持有者）才删。
    probe = _probe_session_lock_os_active(lock_path)
    if probe.get("active"):
        return {"released": False, "reason": "held_by_other"}
    try:
        _safe_delete(lock_path, orchd_dir)
        return {"released": True, "reason": "removed"}
    except (OSError, IOError) as exc:
        return {"released": False, "reason": "io_error", "error": str(exc)}


def release_session_lock_if_owned(
    orchd_dir: Path,
    agent_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """条件释放 session 锁：仅当锁存在且持有者 == ``agent_id`` 且 session 一致才释放。

    task-session-lock-lifecycle：写命令（done/review/claim）在退出时（含异常路径）
    调用，确保持有本 agent 的锁不会因异常漏放；绝不误释放他人锁/他 session 锁。

    Returns:
        ``{"released": bool, "reason": str}``。锁缺失 / 持他人锁时
        ``released=False``（如 ``reason="not_owner_or_absent"``）。
    """
    if orchd_dir is None:
        return {"released": False, "reason": "no_project"}
    if session_id is None:
        from orchd.ledger import resolve_session_identity

        session_id = resolve_session_identity(orchd_dir)["session_id"]
    check = session_lock_check(orchd_dir)
    holder_session = check.get("session_id") or ""
    if (
        check.get("locked")
        and check.get("agent_id") == agent_id
        and (not holder_session or not session_id or holder_session == session_id)
    ):
        return session_lock_release(orchd_dir)
    return {"released": False, "reason": "not_owner_or_absent"}


def _probe_session_lock_os_active(lock_path: Path) -> dict[str, Any]:
    """非阻塞 OS 活性探测：尝试对锁文件获取 flock/msvcrt 排他锁。

    task-session-lock-autoclean：新式锁持锁进程保持 fd 打开，flock 由内核
    托管——进程退出/崩溃时内核自动释放。因此「能获取 flock」⇔ 原持锁进程
    已死（stale）；「不能获取」⇔ 活锁（有进程存活持有）。

    Returns:
        ``{"stale": True}`` 原持锁进程已死（本进程刚拿到 flock，已释放）。
        ``{"stale": False, "active": True}`` 活锁（另一进程持锁中）。
    """
    from orchd.lockfile import ExclusiveFileLock

    # 探活前文件已消失（并发清理）：等同无锁，不创建新文件
    if not lock_path.exists():
        return {"stale": True, "active": False}

    try:
        probe = ExclusiveFileLock(lock_path)
        probe.acquire(blocking=False, timeout_s=0.1)
    except OrchdError:
        # 获取失败（E012 被其他进程持有）→ 活锁
        return {"stale": False, "active": True}
    except Exception:
        # 探活异常（IO 等）：保守视为活锁，不误清
        return {"stale": False, "active": True}
    # 获取成功：原持锁进程已死，立即释放本次探测锁（不干扰后续 acquire）
    probe.release()
    return {"stale": True, "active": False}


def session_lock_check(
    orchd_dir: Path,
    timeout_min: int = _SESSION_LOCK_TIMEOUT_MIN,
) -> dict[str, Any]:
    """检查 session lock 状态：是否存在、是否超时、内容是否合法。

    task-session-lock-autoclean（改 B）：新式 flock 活性锁（JSON 含
    ``flock_active: true``）优先做 **OS 非阻塞探活**——
    - 原持锁进程已死（能获取 flock）→ 判定 stale 并**自动清理**（删除 JSON），
      返回 ``{"locked": False, "reason": "stale_cleaned", ...}``；
    - 活锁（不能获取 flock）→ 返回 locked（调用方拒绝写入 E019），
      此时不再仅凭 timeout 判死（活锁即使超时也由 watchdog 另行处理）。
    旧纯 JSON 锁（无 ``flock_active`` 字段）保持兼容：按 timeout 判定，
    不探活、不误清。

    Args:
        orchd_dir: .orchd 目录路径。
        timeout_min: 超时分钟数（默认 60）。超时视为僵死锁，可覆盖。

    Returns:
        结构化结果，永不抛异常：
            - ``{"locked": False}`` 无锁文件 / 锁已超时 / 锁文件损坏（可覆盖）。
            - ``{"locked": False, "reason": "stale_cleaned", "agent_id": <str>,
                 "session_id": <str>, "age_min": <float>,
                 "cleanup_result": {...}}``
              新式锁且持锁进程已死，已自动清理（可覆盖）。
            - ``{"locked": True, "agent_id": <str>, "branch": <str|None>,
                 "timestamp": <str>, "age_min": <float>}``
              锁有效且未超时 / 新式活锁，调用方应拒绝写入（E019 workspace_busy）。

    Note:
        锁文件损坏（JSON 解析失败、缺少必要字段）视为可覆盖（容错），
        返回 ``{"locked": False, "reason": "corrupted"}``。
    """
    import json
    from datetime import datetime, timezone

    lock_path = _get_session_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"locked": False}

    try:
        content = lock_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, IOError, json.JSONDecodeError):
        # 锁文件损坏：视为可覆盖
        return {"locked": False, "reason": "corrupted"}

    # 校验必要字段
    agent_id = data.get("agent_id")
    timestamp_str = data.get("timestamp")
    if not agent_id or not timestamp_str:
        return {"locked": False, "reason": "corrupted"}

    # 解析时间戳
    try:
        lock_time = datetime.fromisoformat(timestamp_str)
        if lock_time.tzinfo is None:
            # 兼容无时区的时间戳（视为 UTC）
            lock_time = lock_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"locked": False, "reason": "corrupted"}

    # 计算锁年龄
    now = datetime.now(timezone.utc)
    age_seconds = (now - lock_time).total_seconds()
    age_min = age_seconds / 60.0

    # 新式 flock 活性锁：优先 OS 探活判定 stale（task-session-lock-autoclean）
    if data.get(_SESSION_LOCK_FLOCK_MARKER):
        probe = _probe_session_lock_os_active(lock_path)
        if probe.get("stale"):
            # 原持锁进程已死：自动清理（best-effort），后续可重新获取
            try:
                _safe_delete(lock_path, orchd_dir)
                cleaned = True
            except (OSError, IOError):
                cleaned = False
            return {
                "locked": False,
                "reason": "stale_cleaned",
                "agent_id": agent_id,
                "session_id": data.get("session_id") or "",
                "branch": data.get("branch"),
                "age_min": age_min,
                "cleanup_result": {"cleaned": cleaned, "path": str(lock_path)},
            }
        # 活锁：进程仍持有 flock → 有效锁（即使超时也不仅凭 timeout 判死）
        return {
            "locked": True,
            "agent_id": agent_id,
            "session_id": data.get("session_id") or "",
            "nonce": data.get("nonce") or "",
            "branch": data.get("branch"),
            "timestamp": timestamp_str,
            "age_min": age_min,
            _SESSION_LOCK_FLOCK_MARKER: True,
        }

    # 旧纯 JSON 锁兼容：无 flock 活性标记，按 timeout 判定，不探活不误清
    # 超时视为僵死锁，可覆盖
    if age_min >= timeout_min:
        return {"locked": False, "reason": "timeout", "age_min": age_min}

    # 锁有效且未超时
    return {
        "locked": True,
        "agent_id": agent_id,
        "session_id": data.get("session_id") or "",
        "nonce": data.get("nonce") or "",
        "branch": data.get("branch"),
        "timestamp": timestamp_str,
        "age_min": age_min,
    }
