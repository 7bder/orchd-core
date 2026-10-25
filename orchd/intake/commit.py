"""intake.commit —— 自 ``orchd/intake.py`` 拆出。

摄入产物提交（intake_commit）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

from orchd.names import (
    IDEAS_ARCHIVE_FILE,
    IDEAS_FILE,
    MASTER_FILE,
)


from pathlib import Path
from typing import Any

from .guard import _intake_guard, _resolve_lock_orchd_dir
from .parse import check_idea_statuses

def intake_commit(
    project_root: Path,
    message: str | None = None,
) -> dict[str, Any]:
    """摄入产物校验 + 强制提交（引擎命令 ``orchd intake`` 的后端）。

    Args:
        project_root: 仓库根目录。
        message: 提交消息（缺省用默认 chore(intake) 前缀）。

    Returns:
        结构化结果，永不抛异常：
        - 前置失败（非 main）: ``{"committed": False, "reason": "not_on_main",
          "branch": ...}``
        - 前置失败（非摄入脏）: ``{"committed": False, "reason": "dirty_workspace",
          "dirty_files": [...], "hint": ...}``
        - 提交结果: ``{"committed": bool, "commit": {...}, 可选
          "status_warnings": [...], 可选 "commit_warning": {...}}``
    """
    from orchd.gitops import ensure_committed
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
        resolve_roadmap_path,
    )

    project_root = Path(project_root)
    # canonical 共享读（task-canonical-workspace-docs，2026-08-25）：container 布局
    # 下 resolve_workspace_root 解析到 canonical 主工作树根，摄入产物（IDEAS.md /
    # ROADMAP.md）统一在 main 工作树定位与提交，任务 worktree 本地副本不参与。

    # 1) 前置守卫：main 分支 + 非摄入产物干净（对齐 amend 的 E017 语义）
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        return guard_err

    # 2) IDEAS 条目状态校验（warning 不阻断）
    # canonical 工作区根（task-canonical-workspace-docs，2026-08-25）：
    # resolve_workspace_root 先解析到 canonical 主工作树根（container 布局返回
    # main/，flat 返回本地），IDEAS.md / ROADMAP.md 以主工作树副本为权威，
    # 避免任务 worktree 本地 .orchd/ 拷贝过期导致摄入不一致。
    workspace_root = resolve_workspace_root(project_root)
    status_warnings = check_idea_statuses(workspace_root)

    # 3) 强制提交摄入产物（IDEAS.md + ROADMAP.md）—— 受准入写锁串行
    # （task-admission-lock-engine：D 项，与 amend 共用同一把 .intake.lock）
    orchd_dir = _resolve_lock_orchd_dir(project_root)
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        # task-engine-cli-friction-fix：提交范围加入 _master.json（摄入产物），
        # 消除新任务注册后 _master.json 残留未提交态、agent 需人工 git commit 的摩擦。
        # task-intake-archive-path（F9 下半）：同理由加入 IDEAS-archive.md——白名单
        # 早已放行归档脏改动，提交清单却漏该件，归档侧写了盘却永不入库。
        paths = [
            str(workspace_root / IDEAS_FILE),
            str(workspace_root / IDEAS_ARCHIVE_FILE),
            # task-roadmap-root-resolution：ROADMAP 走独立定位（宿主根优先）——
            # 归根后的根版 ROADMAP.md 同样纳入摄入产物提交范围；被 gitignore
            # 忽略时由 commit 层按既有 roadmap-untracked 逻辑剔除（不 fatal）。
            str(resolve_roadmap_path(project_root)),
            str(_resolve_lock_orchd_dir(project_root) / MASTER_FILE),
        ]
        commit_message = message or "chore(intake): orchd intake — commit intake products"
        commit = ensure_committed(project_root, paths, commit_message)
        result: dict[str, Any] = {
            "committed": commit.get("performed") is True,
            "commit": commit,
        }
        if status_warnings:
            result["status_warnings"] = status_warnings
        # 提交未执行（非 no_changes）→ commit_warning（降级可审计化）
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"摄入产物 commit 未执行（{commit.get('reason')}）：改动可能未入库，"
                    "请人工核对（可运行 orchd status --audit-intake 巡检）"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)
