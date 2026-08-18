"""Orchd git 写子域 + 任务生命周期共享辅助。

将 onboard.py 中与 git 写入相关的 best-effort 操作，以及 L1 分支守卫 /
L2 session 锁 / 事件构造等在 onboarding 与 review 路径间共享的辅助函数，
统一外置到此模块，保持 onboard.py 只保留生命周期主干。与 orchd.gitops 区别：

- orchd.gitops：git 基础设施（工作区检测、hook、session lock、ensure_committed
  等），偏低级、可被任意模块复用。
- orchd.gitops_ops（本模块）：任务生命周期特定的 git 写动作（task/{id} 分支、
  merge 前置化、冲突化解）与共享辅助（guard_write_command / make_event 等），
  偏流程层，仅被 onboarding/review 路径调用。

依赖方向：本模块不导入 onboard.py / review.py，二者各自单向依赖本模块，
杜绝循环依赖。
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    check_workspace_state,
    ensure_committed,
    session_lock_acquire,
    session_lock_check,
)
from orchd.ledger import generate_event_id


# ------------------------------------------------------------------
# 共享辅助（onboarding / review 路径共用）
# ------------------------------------------------------------------


def now_iso() -> str:
    """返回当前时间的本地时区 ISO 8601 字符串（精确到秒）。

    先获取 UTC 当前时间，再转换为系统本地时区，避免跨时区机器产生时间混乱。
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_event(
    task_id: str, agent_id: str, etype: str, **extra: Any
) -> dict[str, Any]:
    """构造标准事件字典，用于追加到 ledger（与 onboard._make_event 行为一致）。

    事件 schema 字段：
        v          - 事件版本号（当前固定为 1），便于未来 schema 演进时做兼容判断。
        event_id   - 全局唯一事件 ID，由 generate_event_id() 生成。
        timestamp  - 事件发生的本地时区 ISO 8601 时间戳（精确到秒）。
        task_id    - 关联的任务 ID。
        agent_id   - 触发此事件的 agent 标识。
        type       - 事件类型（如 CLAIMED / DONE / REVIEW_SUBMITTED / FORCE_STATUS 等）。

    **extra 中的键值对会直接合并到事件字典，用于各类型事件的差异化字段
    （如 changes_description、verdict、target_status 等）。
    """
    ev = {
        "v": 1,
        "event_id": generate_event_id(),
        "timestamp": now_iso(),
        "task_id": task_id,
        "agent_id": agent_id,
        "type": etype,
    }
    ev.update(extra)
    return ev


def ensure_session_lock(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
) -> None:
    """确保当前 session 可写入：检查 session lock，若被其他 agent 持有则 E019。

    best-effort：锁获取失败（IO 错误）不抛异常，静默降级。
    """
    check = session_lock_check(orchd_dir)
    if check.get("locked"):
        holder = check.get("agent_id", "unknown")
        if holder != agent_id:
            raise OrchdError(
                ErrorCode.E019,
                f"workspace_busy: 工作区被 '{holder}' 占用（分支 {check.get('branch', 'N/A')}，"
                f"已锁定 {check.get('age_min', 0):.1f} 分钟）",
                [{
                    "agent_id": agent_id,
                    "holder": holder,
                    "holder_branch": check.get("branch"),
                    "holder_timestamp": check.get("timestamp"),
                    "age_min": check.get("age_min"),
                    "hint": "等待该 session 结束，或使用 watchdog --timeout 0 强制释放僵死锁",
                }],
            )
    session_lock_acquire(orchd_dir, agent_id, branch)


def decode_subprocess_output(raw: bytes) -> str:
    """稳健解码子进程输出：UTF-8 优先，GBK 回退（Windows 默认代码页），最后有损 UTF-8。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def verify_output_summary(stdout: bytes, stderr: bytes, limit: int = 400) -> str:
    """从 verify_command 子进程输出提取可读摘要（stdout 尾部 + stderr 头部）。"""
    out = decode_subprocess_output(stdout)
    err = decode_subprocess_output(stderr)
    parts = []
    if out.strip():
        out_s = out.rstrip()
        parts.append(out_s[-limit:] + ("…" if len(out_s) > limit else ""))
    if err.strip():
        err_s = err.strip()
        parts.append(f"stderr: {err_s[:200]}" + ("…" if len(err_s) > 200 else ""))
    return " | ".join(parts)


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


# ------------------------------------------------------------------
# git 写子域（任务生命周期特定）
# ------------------------------------------------------------------


def try_git_branch(project_root: Path, task_id: str) -> None:
    """best-effort 切换到任务分支 task/{task_id}。

    返工场景分支已存在则 checkout 复用，并同步 master 与 main 的差异。
    首次 claim 才 checkout -b 新建。异常静默降级。
    """
    branch = f"task/{task_id}"
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if check.returncode == 0:
            checkout = subprocess.run(
                ["git", "checkout", branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if checkout.returncode == 0:
                sync_master_with_main(project_root, branch)
        else:
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
    except (subprocess.SubprocessError, FileNotFoundError):
        pass


def sync_master_with_main(project_root: Path, branch: str) -> None:
    """分支工作区 .orchd/_master.json 落后 main 时同步为 main 版本（best-effort）。"""
    master_rel = str(Path(".orchd") / "_master.json")
    has_main = subprocess.run(
        ["git", "rev-parse", "--verify", "main"],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    ).returncode == 0
    if not has_main:
        return
    diff = subprocess.run(
        ["git", "diff", "--quiet", "main", "--", master_rel],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if diff.returncode == 0:
        return
    subprocess.run(
        ["git", "checkout", "main", "--", master_rel],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    ensure_committed(
        project_root,
        [master_rel],
        f"chore(claim): sync {branch} master with main",
    )


def try_git_merge(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """best-effort 将任务分支合并到 main。

    - 成功：``{"conflict": False}``
    - 内容冲突：``{"conflict": True, "files": [...]}``
    - 环境异常：``None``（调用方按 best-effort 降级）。
    """
    try:
        checkout = subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if checkout.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "merge", f"task/{task_id}"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            conflict_files: list[str] = []
            for line in result.stdout.split("\n"):
                if "CONFLICT" in line:
                    parts = line.split()
                    if parts:
                        conflict_files.append(parts[-1])
            if conflict_files:
                return {"conflict": True, "files": conflict_files}
            return None
        return {"conflict": False}
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def try_delete_task_branch(project_root: Path, task_id: str) -> bool:
    """best-effort 删除任务分支 task/{task_id}（merge 成功后调用）。

    Returns:
        True：删除成功；False：删除失败或环境不支持（best-effort，不抛异常）。
    """
    branch = f"task/{task_id}"
    try:
        result = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def try_auto_resolve_conflict(
    project_root: Path, task_id: str
) -> dict[str, Any] | None:
    """L3：merge 冲突自动化解——恢复 main → 分支 merge main 预演 → 自动合并或返回清单。

    Returns:
        ``{"resolved": True}``：自动化解成功（main 已含任务分支实现）。
        ``{"resolved": False, "conflict_files": [...], "action": "..."}``：仍需人工解决。
        ``None``：git 环境异常（best-effort 降级）。
    """

    def run(*args: str, timeout: int = 30) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    def parse_conflicts(output: str) -> list[str]:
        files: list[str] = []
        for line in output.split("\n"):
            if "CONFLICT" in line:
                parts = line.split()
                if parts:
                    files.append(parts[-1])
        return files

    try:
        run("merge", "--abort")
        co = run("checkout", f"task/{task_id}")
        if co is None or co.returncode != 0:
            return None
        pre = run("merge", "main")
        if pre is None:
            return None
        if pre.returncode != 0:
            run("merge", "--abort")
            files = parse_conflicts(pre.stdout or "")
            return {
                "resolved": False,
                "conflict_files": files,
                "action": (
                    f"分支 task/{task_id} 与 main 合并冲突：请在 task 分支上执行 "
                    f"git merge main 解决冲突并提交（{len(files) or '若干'} 个文件），"
                    f"然后由同一 reviewer 重试 code APPROVED"
                ),
            }
        co2 = run("checkout", "main")
        if co2 is None or co2.returncode != 0:
            return None
        final = run("merge", f"task/{task_id}")
        if final is None:
            return None
        if final.returncode != 0:
            files = parse_conflicts(final.stdout or "")
            return {
                "resolved": False,
                "conflict_files": files,
                "action": (
                    f"main 与任务分支合并仍冲突（{len(files) or '若干'} 个文件），"
                    f"请人工处理"
                ),
            }
        return {"resolved": True}
    except Exception:
        return None
