"""Orchd 状态报告与巡检：status / watchdog。

- status：提供全局状态快照，汇总六种任务状态（pending / claimed / done /
  in_review / completed / cancelled）的计数；也可按 task_id 查询单任务详情。
- watchdog：检测两类僵死任务——实现者（implementer）认领后超时未交付，
  以及审查者（reviewer）认领评审后超时未回复。

依赖方向：report.py → ledger.py（不导入 onboard / cli）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root


def status(
    store: Store,
    tasks: list[dict[str, Any]],
    project: dict[str, Any] | None = None,
    text: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    """全局状态快照；给定 task_id 时返回单任务详情。

    当 text=True 时，返回结果中额外包含 ``_text`` 字段，其值为人类可读的
    纯文本表格（由 _format_text 生成），供 CLI 层直接打印，不混入 JSON 输出。

    Returns:
        JSON 结构：project + tasks[] + summary（total + 六状态计数）；
        或单任务详情 {"task": {...}}。

    Raises:
        OrchdError E005: task_id 在 master 中不存在。
    """
    state = store.replay()
    task_map = {t.get("id", ""): t for t in tasks}

    if task_id is not None:
        task_def = task_map.get(task_id)
        if task_def is None:
            raise OrchdError(
                ErrorCode.E005,
                f"task '{task_id}' not found in master",
                [{"task_id": task_id}],
            )
        ts = state.get(task_id)
        detail: dict[str, Any] = {
            "id": task_id,
            "name": task_def.get("name", ""),
            "brief": task_def.get("brief", ""),
            "status": ts.status if ts else "pending",
            "module": task_def.get("module", ""),
            "depends_on": task_def.get("depends_on", []),
            "importance": task_def.get("importance", "normal"),
            "reviewers": task_def.get("reviewers", []),
            "verify_command": task_def.get("verify_command", ""),
        }
        if ts:
            if ts.claimed_by:
                detail["claimed_by"] = ts.claimed_by
            if ts.review_phase:
                detail["review_phase"] = ts.review_phase
            if ts.review_claimed_by:
                detail["review_claimed_by"] = ts.review_claimed_by
            # B1（2026-08-13 full-audit-v2）：completed 但 merge 未落地时展示
            # merge_warning，供 audit-merge 告警后的「人工确认 main 是否含实现」直接查看
            if ts.merge_warning:
                detail["merge_warning"] = ts.merge_warning
            detail["attempt_count"] = ts.attempt_count
        return {"task": detail}

    task_statuses: list[dict[str, Any]] = []
    counts = {"pending": 0, "claimed": 0, "done": 0, "in_review": 0, "completed": 0, "cancelled": 0}

    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        s = ts.status if ts else "pending"
        counts[s] = counts.get(s, 0) + 1
        entry: dict[str, Any] = {
            "task_id": tid,
            "name": task.get("name", ""),
            "status": s,
            "module": task.get("module", ""),
            "attempt_count": ts.attempt_count if ts else 0,
            "source": task.get("source"),
        }
        if ts and ts.claimed_by:
            entry["claimed_by"] = ts.claimed_by
        if ts and ts.review_phase:
            entry["review_phase"] = ts.review_phase
        if ts and ts.review_claimed_by:
            entry["review_claimed_by"] = ts.review_claimed_by
        task_statuses.append(entry)

    summary = {"total": len(tasks), **counts}
    result = {
        "project": dict(project) if project else {},
        "tasks": task_statuses,
        "summary": summary,
    }

    if text:
        result["_text"] = _format_text(result)

    return result


def merge_audit(
    store: Store,
    tasks: list[dict[str, Any]],
    project_root: Path,
) -> dict[str, Any]:
    """只读 merge 巡检：task/* 分支与 main 的并入情况 + 未注册幽灵分支检测。

    扫描全部 task/* 分支（含未注册的幽灵分支）与 main 的差异：
    - 已注册任务的 completed 分支：main 不包含其 tip 时告警
      （branch_not_merged_into_main）；已并入 main 但分支未删告警
      （merged_but_not_cleaned）；非 completed（进行中）任务分支不告警。
    - 未注册任务的幽灵分支（agent 绕过 claim 手动建的分支）：立即告警
      （unregistered_task_branch），含分支名与差异摘要。
    只读：不执行 merge/checkout/reset/commit/push。非 git 仓库、无 main
    分支或 git 不可用时返回 skipped 说明，不抛异常。

    Args:
        store: ledger Store（replay 取任务状态）。
        tasks: _master.json 的 tasks[] 定义列表。
        project_root: 仓库根目录（git 命令 cwd）。

    Returns:
        {"skipped": False, "warnings": [{"task_id"?, "branch", "reason",
        "ahead_count"}]} 或 {"skipped": True, "reason": "..."}。
        幽灵分支告警无 task_id 字段（无注册任务）。
    """
    import subprocess

    root = Path(project_root)

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(root),
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )

    def _ahead(branch: str) -> int:
        revs = _git("rev-list", "--count", f"main..{branch}")
        if revs.returncode != 0:
            return 0
        return int(revs.stdout.strip() or "0")

    try:
        if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return {"skipped": True, "reason": "not_a_git_repo"}
        if _git("rev-parse", "--verify", "main").returncode != 0:
            return {"skipped": True, "reason": "no_main_branch"}
    except (subprocess.SubprocessError, OSError):
        return {"skipped": True, "reason": "git_unavailable"}

    state = store.replay()
    registered_ids = {t.get("id", "") for t in tasks}
    warnings: list[dict[str, Any]] = []
    try:
        refs = _git("for-each-ref", "--format=%(refname:short)", "refs/heads/task/*")
    except (subprocess.SubprocessError, OSError):
        return {"skipped": False, "warnings": warnings}
    branch_names = [
        ln.strip() for ln in refs.stdout.splitlines() if ln.strip()
    ] if refs.returncode == 0 else []

    # 已并入 main 的分支集合（git branch --merged main 判定）：tip 为 main 祖先
    merged_out = _git("branch", "--merged", "main")
    merged_branches: set[str] = set()
    if merged_out.returncode == 0:
        for ln in merged_out.stdout.splitlines():
            name = ln.strip().lstrip("*").strip()
            if name:
                merged_branches.add(name)

    for branch in branch_names:
        tid = branch[len("task/"):] if branch.startswith("task/") else branch
        if tid not in registered_ids:
            # 未注册幽灵分支：agent 绕过 claim 手动建的分支，立即告警
            warnings.append({
                "branch": branch,
                "reason": "unregistered_task_branch",
                "ahead_count": _ahead(branch),
            })
            continue
        ts = state.get(tid)
        if not ts or ts.status != "completed":
            continue  # 进行中任务的分支不告警
        ahead = _ahead(branch)
        if ahead > 0:
            warnings.append({
                "task_id": tid,
                "branch": branch,
                "reason": "branch_not_merged_into_main",
                "ahead_count": ahead,
            })
        elif branch in merged_branches:
            # 已并入 main 但分支未删（卫生）：只读告警，不自动清理
            warnings.append({
                "task_id": tid,
                "branch": branch,
                "reason": "merged_but_not_cleaned",
            })

    # B1（2026-08-13 full-audit-v2）：merge 降级盲区——completed 任务含
    # merge_warning 但 task 分支已删（merge 未落地 + audit 失明）→ 单独告警。
    # 分支仍存在时由上循环的 branch_not_merged / merged_but_not_cleaned 覆盖。
    branch_ids = {b[len("task/"):] for b in branch_names if b.startswith("task/")}
    for tid in registered_ids:
        ts = state.get(tid)
        if not ts or ts.status != "completed" or not ts.merge_warning:
            continue
        if tid not in branch_ids:
            warnings.append({
                "task_id": tid,
                "reason": "merge_warning_unresolved",
                "message": ts.merge_warning,
                "hint": "该任务标记完成但 git merge 未执行（best-effort 降级），"
                        "且 task 分支已删除，无法从分支差异核对；"
                        "请人工确认 main 是否含实现",
            })
    return {"skipped": False, "warnings": warnings}


def intake_audit(project_root: Path) -> dict[str, Any]:
    """只读巡检：检测未提交的摄入产物改动（intake-commit-enforcement，2026-08-14）。

    摄入产物 = ``.orchd/_master.json``、``IDEAS.md`` / ``ROADMAP.md``（根布局与
    发布态 ``.orchd`` 布局）。检测工作区中这些文件的未提交改动 → 告警清单，
    让"摄入/注册后改动未入库"可被巡检发现（对齐 ``--audit-merge`` 只读先例）。

    只读：不执行 add/commit/checkout/reset。非 git 仓库或 git 不可用返回 skipped。
    """
    root = Path(project_root)
    dirty = list_tracked_changes(root)
    if dirty is None:
        return {"skipped": True, "reason": "git_unavailable"}
    # 摄入产物白名单（两种布局，与 split._INTAKE_PRODUCT_FILES 对齐）
    products = {
        ".orchd/_master.json",
        "IDEAS.md",
        ".orchd/IDEAS.md",
        "ROADMAP.md",
        ".orchd/ROADMAP.md",
    }
    hit = sorted(f for f in dirty if f in products)
    warnings = [
        {
            "file": f,
            "reason": "uncommitted_intake_product",
            "hint": (
                "摄入/注册产物未提交：请运行 orchd intake（提交 IDEAS/ROADMAP）"
                "或 orchd amend（注册+提交），或人工 git commit"
            ),
        }
        for f in hit
    ]
    return {"skipped": False, "warnings": warnings}


def watchdog(
    store: Store,
    tasks: list[dict[str, Any]],
    timeout_min: int = 60,
) -> dict[str, Any]:
    """巡检：检测两类僵死任务 + L2 session 锁僵死锁清理。

    两类僵死任务：
    - implementer（实现者僵死）：任务处于 claimed 状态且超过 timeout 分钟，
      说明实现者认领后长时间未交付。
    - reviewer（审查者僵死）：任务处于 in_review 状态、存在 review_claimed_by
      且超过 timeout 分钟，说明审查者认领评审后长时间未给出结论。

    L2 session 锁：判定「审查中锁 ≠ 僵死锁」——锁有效（未超时）时核对
    「锁持有者 agent_id + 活跃任务」：
    - 持有者有进行中的实现（claimed_by == 持有者 && claimed）或审查
      （review_claimed_by == 持有者 && in_review）→ 活锁，报告不释放；
    - 持有者已无活跃任务 → 会话已结束但锁未释放（僵死锁），立即释放
      （reason=no_active_task，即使锁年龄未达 timeout）；
    - 锁已超时（timeout 自动释放锁）→ 释放（reason=timeout）。

    判定依据来自 ledger 中 CLAIMED / REVIEW_CLAIMED 事件的时间戳。

    Returns:
        JSON 含 stuck 列表 + stuck_kind + session_lock 巡检结果。
        exit code 由 CLI 层决定。
    """
    state = store.replay()
    now = datetime.now(timezone.utc)
    stuck: list[dict[str, Any]] = []

    # 读取 ledger 获取事件时间戳
    events = store._read_ledger_lines(from_line=1)
    # 构建 task_id → 最近 CLAIMED / REVIEW_CLAIMED 事件信息
    claim_times: dict[str, datetime] = {}
    review_claims: dict[str, dict[str, Any]] = {}
    for ev in events:
        tid = ev.get("task_id", "")
        ts_str = ev.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ev_time = datetime.fromisoformat(ts_str)
        except ValueError:
            continue
        if ev.get("type") == "CLAIMED":
            claim_times[tid] = ev_time
        elif ev.get("type") == "REVIEW_CLAIMED":
            review_claims[tid] = {
                "time": ev_time,
                "event_id": ev.get("event_id", ""),
                "agent": ev.get("agent_id", ""),
            }

    for tid, ts in state.items():
        if ts.status == "claimed" and tid in claim_times:
            elapsed = (now - claim_times[tid]).total_seconds() / 60
            if elapsed > timeout_min:
                stuck.append({
                    "task_id": tid,
                    "stuck_kind": "implementer",
                    "claimed_by": ts.claimed_by,
                    "stuck_minutes": round(elapsed, 1),
                    "action": f"建议运行: orchd force-status --task {tid} --status pending",
                })
        elif ts.status == "in_review" and ts.review_claimed_by and tid in review_claims:
            elapsed = (now - review_claims[tid]["time"]).total_seconds() / 60
            if elapsed > timeout_min:
                claim_event_id = review_claims[tid].get("event_id", "")
                if claim_event_id:
                    action = (
                        f"如审查已中断：orchd retract --agent {ts.review_claimed_by} "
                        f"--event {claim_event_id} --reason 'abandoned review'（释放审查认领，"
                        f"任务保持 in_review 可被重新认领）；如审查无法继续也可 "
                        f"orchd force-status --task {tid} --status pending（打回实现，较重）"
                    )
                else:
                    action = (
                        f"审查认领事件缺 event_id，无法直接 retract：可 "
                        f"orchd force-status --task {tid} --status pending（打回实现，较重）"
                    )
                stuck.append({
                    "task_id": tid,
                    "stuck_kind": "reviewer",
                    "review_claimed_by": ts.review_claimed_by,
                    "review_claim_event_id": claim_event_id,
                    "stuck_minutes": round(elapsed, 1),
                    "action": action,
                })

    # L2 session 锁巡检：超时自动释放僵死锁
    # 判定语义（审查中锁 ≠ 僵死锁）：锁有效（未超时）时，不能仅凭锁年龄判活，
    # 必须核对「锁持有者(agent_id) + 活跃任务」——
    #   活跃实现   : status == claimed 且 claimed_by == 持有者
    #   活跃审查   : status == in_review 且 review_claimed_by == 持有者
    # 持有者仍有进行中的实现/审查 → 活锁（审查中锁），报告但不释放；
    # 持有者已无活跃任务 → 会话已结束但锁未释放（僵死锁，如已交付实现者
    # 遗留的年轻锁），即使未超时也立即释放，避免误触发 E019 workspace_busy。
    session_lock_stale: dict[str, Any] | None = None
    check = session_lock_check(store.orchd_dir, timeout_min=timeout_min)
    if check.get("locked"):
        lock_agent = check.get("agent_id")
        has_active_task = any(
            (t.claimed_by == lock_agent and t.status == "claimed")
            or (t.review_claimed_by == lock_agent and t.status == "in_review")
            for t in state.values()
        )
        if not has_active_task:
            # 锁持有者已无活跃任务：会话已结束但锁未释放 → 立即释放（僵死锁）
            release = session_lock_release(store.orchd_dir)
            session_lock_stale = {
                "status": "released",
                "reason": "no_active_task",
                "agent_id": lock_agent,
                "age_min": round(check.get("age_min", 0), 1),
                "release_result": release,
            }
        else:
            # 持有者仍有进行中的实现或审查：审查中锁 ≠ 僵死锁，报告但不释放
            session_lock_stale = {
                "status": "active",
                "agent_id": lock_agent,
                "branch": check.get("branch"),
                "age_min": round(check.get("age_min", 0), 1),
            }
    elif check.get("reason") == "timeout":
        # 锁已超时：自动释放
        release = session_lock_release(store.orchd_dir)
        session_lock_stale = {
            "status": "released",
            "reason": "timeout",
            "age_min": round(check.get("age_min", 0), 1),
            "release_result": release,
        }

    return {
        "stuck_tasks": stuck,
        "summary": f"{len(stuck)} 个任务可能僵死",
        # 扩展字段（便于脚本集成）
        "stuck_count": len(stuck),
        "timeout_min": timeout_min,
        "session_lock": session_lock_stale,
    }


def _format_text(result: dict[str, Any]) -> str:
    """将 status() 返回的结构化结果格式化为人类可读的纯文本表格。

    表格格式：
    - 表头行：ID（20 字符左对齐）、Status（12 字符）、Claimed By（15 字符）、Name
    - 分隔线：70 个连字符
    - 每行一个任务，字段对齐方式同表头
    - 末尾空行后跟一行汇总：Total / pending / claimed / done / in_review /
      completed / cancelled 各自的计数

    Args:
        result: status() 返回的字典，需包含 tasks[] 和 summary 字段。

    Returns:
        多行纯文本字符串。
    """
    lines = []
    lines.append(f"{'ID':<20} {'Status':<12} {'Claimed By':<15} {'Name'}")
    lines.append("-" * 70)
    for t in result["tasks"]:
        lines.append(
            f"{t['task_id']:<20} {t['status']:<12} {t.get('claimed_by', '-'):<15} {t['name']}"
        )
    lines.append("")
    summary = result["summary"]
    lines.append(
        f"Total: {summary['total']} | "
        f"pending={summary['pending']} claimed={summary['claimed']} "
        f"done={summary['done']} in_review={summary['in_review']} "
        f"completed={summary['completed']} cancelled={summary['cancelled']}"
    )
    return "\n".join(lines)
