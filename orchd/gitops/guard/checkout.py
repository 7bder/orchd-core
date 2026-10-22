"""gitops guard — 检出与分支切换守卫域（W6-C1 拆分，纯搬移）。

来源：``orchd/gitops/guard.py``（1652 行，W6-C1 纯搬移）。

``managed_checkout_branch`` / ``_reviewer_auto_checkout`` /
``checkout_default_strict`` / ``_checkout_default_with_retry``。

依赖 ``core`` / ``write`` / ``restore``（顶层导入，单向）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

from orchd.gitops._const import (
    _GIT_CHECKOUT_TIMEOUT,
    _GIT_ENCODING,
    _GIT_ERRORS,
)

from orchd.gitops.query import check_workspace_state, get_default_branch, is_task_worktree

from orchd.line_ctx import resolve_task_branch_for

from .core import (
    _clear_timeout_index_lock,
    _container_task_wt_exists,
    _probe_guard_workspace_state,
    commit_hint_for_branch,
)
from .restore import _count_git_processes
from .write import _enforce_workspace_clean, _resolve_claim_check_root

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
