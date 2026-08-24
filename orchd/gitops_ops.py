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
# task-14-git-policy-layer：判定类逻辑（guard / checkout_default_strict /
# ensure_session_lock / parse_conflicts）已收敛到 orchd.gitops（专用 git 判定模块，
# 单一入口）；此处 re-import 保持旧导入路径兼容。
from orchd.gitops import (
    checkout_default_strict,
    check_workspace_state,
    ensure_committed,
    ensure_session_lock,
    get_default_branch,
    guard_write_command,
    main_worktree_root,
    parse_conflicts,
)
from orchd.ledger import generate_event_id, resolve_session_identity


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
    session_identity = resolve_session_identity()
    ev = {
        "v": 1,
        "event_id": generate_event_id(),
        "timestamp": now_iso(),
        "task_id": task_id,
        "agent_id": agent_id,
        "type": etype,
        "session_id": session_identity["session_id"],
    }
    ev.update(extra)
    return ev


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
    """best-effort 将任务分支合并到主工作树的 main（task-14-merge-main-tree）。

    始终在**主工作树**内执行（``git rev-parse --git-common-dir`` 定位；flat 单会话
    即 project_root，零回归），任务 worktree 永不 checkout main。merge-wt 已废弃。

    - 成功：``{"conflict": False}``
    - 内容冲突：``{"conflict": True, "files": [...]}``
    - 环境异常：``None``（调用方按 best-effort 降级）。
    """
    try:
        workdir = main_worktree_root(project_root)
        checkout = subprocess.run(
            ["git", "-C", str(workdir), "checkout", "main"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if checkout.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "-C", str(workdir), "merge", f"task/{task_id}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            conflict_files = parse_conflicts(result.stdout or "")
            if conflict_files:
                # P2-7：冲突后立即 abort 清理 MERGE_HEAD，避免残留中间态阻塞后续 git 操作。
                # try_auto_resolve_conflict 开头会再次 abort（幂等），此处先清理无副作用。
                subprocess.run(
                    ["git", "-C", str(workdir), "merge", "--abort"],
                    capture_output=True, timeout=10,
                )
                return {"conflict": True, "files": conflict_files}
            return None
        return {"conflict": False}
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def try_delete_task_branch(project_root: Path, task_id: str) -> bool:
    """best-effort 删除任务分支 task/{task_id}（merge 成功后调用）。

    分支删除以**主工作树**为稳定 cwd（git -C）：容器布局下 project_root 是任务
    worktree，task/{id} 曾由该 worktree checkout，git 拒绝删除被占用分支；worktree
    已回收后（remove_task_wt 或分支已删）分支不再被占用，-d 成功或报分支不存在。
    flat 布局 main_worktree_root 回退 project_root，cwd 即主工作树，零回归。

    Returns:
        True：删除成功，或分支已不存在（幂等视为成功，best-effort 不抛异常）。
        False：删除失败或环境不支持。
    """
    branch = f"task/{task_id}"
    try:
        workdir = main_worktree_root(project_root)
        result = subprocess.run(
            ["git", "-C", str(workdir), "branch", "-d", branch],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return True
        # 分支已不存在（如 remove_task_wt 的 -D 已删）→ 幂等视为成功
        err = (result.stderr or "").lower()
        if (
            "not found" in err
            or "doesn't exist" in err
            or "does not exist" in err
        ):
            return True
        return False
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def try_auto_resolve_conflict(
    project_root: Path, task_id: str
) -> dict[str, Any] | None:
    """L3：merge 冲突自动化解——恢复 main → 分支 merge main 预演 → 自动合并或返回清单。

    全部 git 操作在**主工作树**内以 ``git -C`` 执行（``git rev-parse --git-common-dir``
    定位；flat 单会话即 project_root，零回归），任务 worktree 永不 checkout main。
    merge-wt 已废弃。

    Returns:
        ``{"resolved": True}``：自动化解成功（main 已含任务分支实现）。
        ``{"resolved": False, "conflict_files": [...], "action": "..."}``：仍需人工解决。
        ``None``：git 环境异常（best-effort 降级）。
    """

    def run(workdir: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    try:
        workdir = main_worktree_root(project_root)
        run(workdir, "merge", "--abort")
        co = run(workdir, "checkout", f"task/{task_id}")
        if co is None or co.returncode != 0:
            return None
        pre = run(workdir, "merge", "main")
        if pre is None:
            return None
        if pre.returncode != 0:
            run(workdir, "merge", "--abort")
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
        co2 = run(workdir, "checkout", "main")
        if co2 is None or co2.returncode != 0:
            return None
        final = run(workdir, "merge", f"task/{task_id}")
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
