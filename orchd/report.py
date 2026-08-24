"""Orchd 状态报告与巡检：status / watchdog。

- status：提供全局状态快照，汇总六种任务状态（pending / claimed / done /
  in_review / completed / cancelled）的计数；也可按 task_id 查询单任务详情。
- watchdog：检测两类僵死任务——实现者（implementer）认领后超时未交付，
  以及审查者（reviewer）认领评审后超时未回复。

依赖方向：report.py → ledger.py（不导入 onboard / cli）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root


# .orchd/merge-acks.json：merge_warning 残留的人工销账清单（task-merge-warning-ack）。
# gitignored（运行时文件，.gitignore 白名单天然忽略），格式：
#   {"<task_id>": {"acked_at": <ISO 时间>, "reason": <str>}}
_MERGE_ACKS_FILENAME = "merge-acks.json"


def merge_acks_path(project_root: Path) -> Path:
    """返回人工销账清单路径 ``<project_root>/.orchd/merge-acks.json``。"""
    return Path(project_root) / ".orchd" / _MERGE_ACKS_FILENAME


def load_merge_acks(project_root: Path) -> dict[str, Any]:
    """读取 merge_warning 人工销账清单（task-merge-warning-ack）。

    文件不存在 / 解析失败 / 非 dict → 视为空清单（不报错，best-effort 降级）。

    Args:
        project_root: 仓库根目录。

    Returns:
        ``{"<task_id>": {"acked_at": ..., "reason": ...}}`` 字典；异常时空 dict。
    """
    path = merge_acks_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def merge_ack(project_root: Path, task_id: str, reason: str) -> dict[str, Any]:
    """登记 merge_warning 人工销账（追加/更新 .orchd/merge-acks.json，原子写）。

    与 resolve_sha 自动销账（task-merge-warning-resolve-sha）互补：人工路径
    兜底旧事件 / 无 resolve_sha 场景（08-22 task-fp-identity-single-source
    残留即此场景）。重复 ack 覆盖更新（acked_at 刷新为当前时间）。

    Args:
        project_root: 仓库根目录。
        task_id: 已人工确认的 task_id（非空）。
        reason: 确认原因（必填，非空）。

    Returns:
        ``{"acked": True, "task_id": ..., "acked_at": ..., "reason": ...}``。

    Raises:
        ValueError: task_id 或 reason 为空。
        OSError: 写入后回读不一致（原子写失败）。
    """
    if not task_id:
        raise ValueError("merge_ack: task_id 不能为空")
    if not reason:
        raise ValueError("merge_ack: reason 必填（人工确认原因）")
    path = merge_acks_path(project_root)
    acks = load_merge_acks(project_root)
    entry = {
        "acked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "reason": reason,
    }
    acks[task_id] = entry
    # 原子写：tmp + os.replace（写后回读确认，失败可审计）
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(acks, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    if load_merge_acks(project_root).get(task_id) != entry:
        raise OSError("merge-acks 写入回读不一致")
    return {"acked": True, "task_id": task_id, **entry}


def status(
    store: Store,
    tasks: list[dict[str, Any]],
    project: dict[str, Any] | None = None,
    text: bool = False,
    task_id: str | None = None,
    project_root: Path | None = None,
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
    # 复活标记（task-force-status-revive-audit）：扫描 ledger 检测 completed→pending
    # 复活操作，对曾被复活的任务附加 revive_marker（reason+evidence_sha+时间），
    # 正常任务不附加（零误伤）。
    try:
        revive_by_task = _scan_revive_markers(store)
    except Exception:
        revive_by_task = {}
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
            if task_id in revive_by_task:
                detail["revive_marker"] = revive_by_task[task_id]
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
        if tid in revive_by_task:
            entry["revive_marker"] = revive_by_task[tid]
        task_statuses.append(entry)

    summary = {"total": len(tasks), **counts}
    result = {
        "project": dict(project) if project else {},
        "tasks": task_statuses,
        "summary": summary,
    }

    # task-14-worktree-lifecycle（AC5）：孤儿 worktree 惰性清理（best-effort，
    # 绑定任务已终态但 worktree 仍在 → 回收；无独立 worktree 场景零操作）。
    if project_root is not None:
        try:
            from orchd.ledger import resolve_store_dir
            from orchd.worktree import prune_orphans

            pruned = prune_orphans(project_root, resolve_store_dir(store.orchd_dir), state)
            if pruned.get("pruned"):
                result["worktree_pruned"] = pruned
        except Exception:
            pass

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
    # 自动销账（task-merge-warning-resolve-sha，2026-08-24）：merge_warning 事件
    # 附加 resolve_sha（task 分支 tip）；main 已包含该 sha（git merge-base
    # --is-ancestor 为真，即手工补 merge 已落地）→ 不再告警（resolve_sha 无效
    # 或缺失的旧事件维持告警，人工核对兜底）。
    branch_ids = {b[len("task/"):] for b in branch_names if b.startswith("task/")}
    resolve_sha_by_task: dict[str, str] = {}
    try:
        for ev in store.backend.read_events():
            if ev.get("type") == "REVIEW_SUBMITTED" and ev.get("resolve_sha"):
                resolve_sha_by_task.setdefault(ev.get("task_id", ""), ev["resolve_sha"])
    except Exception:
        resolve_sha_by_task = {}
    # 人工销账清单（task-merge-warning-ack）：resolve_sha 自动判定未覆盖的
    # 旧事件 / 无 sha 场景，由人工确认后跳过告警。
    merge_acks = load_merge_acks(root)
    for tid in registered_ids:
        ts = state.get(tid)
        if not ts or ts.status != "completed" or not ts.merge_warning:
            continue
        if tid not in branch_ids:
            sha = resolve_sha_by_task.get(tid)
            if sha and _is_ancestor_of_main(root, sha):
                continue  # main 已含实现（resolve_sha 为 main 祖先）→ 自动销账
            if tid in merge_acks:
                continue  # 人工已确认（merge-acks 清单）→ 人工销账
            warnings.append({
                "task_id": tid,
                "reason": "merge_warning_unresolved",
                "message": ts.merge_warning,
                "hint": "该任务标记完成但 git merge 未执行（best-effort 降级），"
                        "且 task 分支已删除，无法从分支差异核对；"
                        "请人工确认 main 是否含实现",
            })
    return {"skipped": False, "warnings": warnings}


def _is_ancestor_of_main(project_root: Path, sha: str) -> bool:
    """main 是否已包含 sha（git merge-base --is-ancestor <sha> main，best-effort）。

    Args:
        project_root: 仓库根目录（git 命令 cwd，与 merge_audit 一致）。
        sha: 待判定的 commit SHA（resolve_sha）。

    Returns:
        True：sha 是 main 的祖先（实现已并入 main）；False：未包含 / git 不可用。
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "main"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def task_integrity_audit(
    store: Store,
    tasks: list[dict[str, Any]],
    project_root: Path | None,
    scope: str = "active",
) -> dict[str, Any]:
    """任务完整性只读巡检：发现跨 worktree 脏写 / 分支 diff 缺失声明文件。

    ``scope="active"``（watchdog 附加字段 ``task_integrity``）：扫描活跃任务
    （claimed / done / in_review）的 ``files_to_edit`` 声明文件——
    - ``main_dirty_overlap``：main 工作树存在同名未提交改动（跨 worktree 脏写：
      实现者在任务 worktree 提交后 main 上仍有残留改动，疑似漏提交/错 worktree）；
    - ``missing_declared_files``：任务分支相对 main 的实际 diff 缺失声明文件
      （实现者改了未声明文件却漏改/漏提交声明文件，branch diff 兜底核对）。

    ``scope="merged"``（status --audit-task 附加字段 ``audit_task``）：扫描
    completed（merged）任务的历史缺失/残留——
    - ``main_residual``：main 工作树仍残留该任务声明文件的未提交改动（合并后
      未提交的残留，无论巡检入口在 main 还是任务 worktree 都能发现）；
    - ``historical_missing``：任务分支 diff 缺失声明文件（历史缺失）。

    只读：不修改工作树、不建分支、不提交（对齐 ``merge_audit`` 先例）。
    非 git / git 不可用 / 无 project_root 时返回 ``{"skipped": True, ...}``，
    不抛异常（best-effort）。

    Args:
        store: ledger Store（replay 取任务状态）。
        tasks: _master.json 的 tasks[] 定义列表。
        project_root: 任意 worktree 根（git 命令 cwd；None → skipped）。
        scope: "active"（活跃任务）或 "merged"（completed 任务历史）。

    Returns:
        ``{"skipped": False, "scope": scope, "issues": [...], "summary": str}``
        或 ``{"skipped": True, "reason": "..."}``。
    """
    import subprocess

    if project_root is None:
        return {"skipped": True, "reason": "no_project_root"}
    root = Path(project_root).resolve()
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
        if check.returncode != 0:
            return {"skipped": True, "reason": "not_a_git_repo"}
    except (subprocess.SubprocessError, OSError):
        return {"skipped": True, "reason": "git_unavailable"}

    state = store.replay()
    # canonical 主工作树脏文件一次性读取（merged 残留检测；flat 单 worktree 即自身）
    try:
        from orchd.gitops import list_tracked_changes, main_worktree_root

        main_dirty = list_tracked_changes(main_worktree_root(root)) or []
    except Exception:
        main_dirty = []

    issues: list[dict[str, Any]] = []
    for task in tasks:
        tid = task.get("id", "")
        ts = state.get(tid)
        s = ts.status if ts else "pending"
        if scope == "active":
            if s not in ("claimed", "done", "in_review"):
                continue
        elif scope == "merged":
            if s != "completed":
                continue
        declared = list(task.get("files_to_edit", []) or [])
        if not declared:
            continue
        entry: dict[str, Any] = {"task_id": tid, "status": s}
        if scope == "active":
            try:
                from orchd.worktree import main_worktree_dirty_overlap

                overlap = main_worktree_dirty_overlap(root, declared)
                if overlap:
                    entry["main_dirty_overlap"] = overlap
            except Exception:
                pass
        else:
            residual = sorted(set(main_dirty) & set(declared))
            if residual:
                entry["main_residual"] = residual
        try:
            from orchd.worktree import missing_declared_branch_files

            missing = missing_declared_branch_files(root, tid, declared)
            if missing:
                if scope == "active":
                    entry["missing_declared_files"] = missing
                else:
                    entry["historical_missing"] = missing
        except Exception:
            pass
        if len(entry) > 2:
            issues.append(entry)

    label = "活跃任务" if scope == "active" else "merged 任务"
    return {
        "skipped": False,
        "scope": scope,
        "issues": issues,
        "summary": f"{len(issues)} 个{label}存在完整性告警",
    }


def _scan_revive_markers(store: Store) -> dict[str, dict[str, Any]]:
    """扫描 ledger，返回曾被 completed→pending 复活的任务 → 最近一次复活标记。

    复活 = ``FORCE_STATUS`` 将任务从 ``completed`` 强制回到 ``pending``
    （task-force-status-revive-guard 门禁：需 ``--force + --evidence-sha`` 的 git 证据）。
    标记供 ``status`` / ``claim_preview`` 透明展示，正常任务（无复活历史）不在结果中
    （零误伤）。被撤回（RETRACT）的 FORCE_STATUS 事件不计入（与 replay 状态机一致）。

    Returns:
        ``{task_id: {"revived_at", "revived_by", "reason", "evidence_sha"}}``；
        无 ledger / 读取异常时返回空 dict。
    """
    if not store.ledger_exists():
        return {}
    try:
        events = store._read_ledger_lines(from_line=1)
        retracted = store._collect_retracted_event_ids()
    except Exception:
        return {}
    status_by_task: dict[str, str] = {}
    markers: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.get("type") == "RETRACT" or ev.get("event_id", "") in retracted:
            continue
        tid = ev.get("task_id", "")
        etype = ev.get("type")
        if etype == "CLAIMED":
            status_by_task[tid] = "claimed"
        elif etype == "DONE":
            status_by_task[tid] = "done"
        elif etype == "REVIEW_SUBMITTED":
            if ev.get("verdict") == "APPROVED" and ev.get("review_type") == "code":
                status_by_task[tid] = "completed"
        elif etype == "FORCE_STATUS":
            target = ev.get("target_status", "pending")
            # 测试数据（test_data=True）标记（task-p2-ledger-audit-noise）：测试注入的
            # FORCE_STATUS 事件（如 completed→pending 复活 fixture）须与真实生产事件区分，
            # 避免 revive_audit 审计噪音。仅跳过告警生成，仍推进状态机状态
            # （status_by_task 照常更新）以防误伤后续真实事件判定。
            if not ev.get("test_data"):
                # 复活：当前状态为 completed 且被强制回到 pending
                if target == "pending" and status_by_task.get(tid) == "completed":
                    markers[tid] = {
                        "revived_at": ev.get("timestamp"),
                        "revived_by": ev.get("agent_id"),
                        "reason": ev.get("reason"),
                        "evidence_sha": ev.get("evidence_sha"),
                    }
            status_by_task[tid] = target
    return markers


def task_revive_markers(store: Store) -> dict[str, dict[str, Any]]:
    """公开封装：返回 ``{task_id: revive_marker}``（曾被 completed→pending 复活的任务）。"""
    return _scan_revive_markers(store)


def revive_audit(
    store: Store,
    tasks: list[dict[str, Any]],
    project_root: Path | None = None,
) -> dict[str, Any]:
    """只读巡检：扫描 ledger 中 FORCE_STATUS 将 completed→pending 的复活操作，列 warning。

    与 ``merge_audit`` / ``intake_audit`` 同级只读先例：仅告警不阻断（audit 照常返回，
    warning 列表新增复活项），提示「请确认是否有授权」（revive-guard 要求
    ``--force + --evidence-sha`` 的 git 证据）。

    Args:
        store: ledger Store（replay 取任务状态）。
        tasks: _master.json 的 tasks[] 定义列表（占位兼容，当前扫描全 ledger）。
        project_root: 仓库根目录（占位兼容，当前未使用）。

    Returns:
        ``{"skipped": False, "warnings": [{task_id, revived_at, revived_by,
        reason, evidence_sha, hint}]}``；无 ledger 时 ``{"skipped": True, ...}``。
    """
    markers = _scan_revive_markers(store)
    warnings = [
        {
            "task_id": tid,
            "revived_at": m["revived_at"],
            "revived_by": m["revived_by"],
            "reason": m["reason"],
            "evidence_sha": m["evidence_sha"],
            "hint": "该任务曾被从 completed 强制复活为 pending（completed→pending），"
                    "请确认是否有授权（revive-guard 要求 --force + --evidence-sha 的 git 证据）",
        }
        for tid, m in markers.items()
    ]
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
    project_root: Path | None = None,
    agent_id: str | None = None,
    takeover: bool = False,
) -> dict[str, Any]:
    """巡检：检测两类僵死任务 + L2 session 锁僵死锁清理。

    两类僵死任务：
    - implementer（实现者僵死）：任务处于 claimed 状态且超过 timeout 分钟，
      说明实现者认领后长时间未交付。
    - reviewer（审查者僵死）：任务处于 in_review 状态、存在 review_claimed_by
      且超过 timeout 分钟，说明审查者认领评审后长时间未给出结论。

    L2 session 锁：判定「审查中锁 ≠ 僵死锁」——锁有效（未超时）时核对
    「锁持有者 agent_id + 活跃任务」：
    - 新式 flock 活性锁（``flock_active``）：``session_lock_check`` 先做 OS
      非阻塞探活——持锁进程已死则自动清理（reason=stale_cleaned），本巡检
      直接上报 released（reason=stale_os_probe），不依赖 timeout/no_active_task；
    - 持有者有进行中的实现（claimed_by == 持有者 && claimed）或审查
      （review_claimed_by == 持有者 && in_review）→ 活锁，报告不释放；
    - 持有者已无活跃任务 → 会话已结束但锁未释放（僵死锁），立即释放
      （reason=no_active_task，即使锁年龄未达 timeout）；
    - 锁已超时（timeout 自动释放锁）→ 释放（reason=timeout）。
    - 旧纯 JSON 锁（无 flock_active）保持兼容：仅按 timeout/no_active_task 判定，
      不探活、不误清。

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
                        f"如审查已中断：orchd retract "
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

    # Session Identity Layer：僵死 session 检测（task-session-watchdog-stale）。
    # 对 claimed/done/in_review 任务，若实现者或审查者的 session runtime 已缺失、
    # inactive 或过期，则计入 stale_claims。不自动释放有活跃 session 的任务锁。
    stale_sessions: list[dict[str, Any]] = []
    stale_claims: list[dict[str, Any]] = []
    try:
        from orchd.ledger import session_runtime_dir

        session_dir = session_runtime_dir(store.orchd_dir)
        sessions_by_id: dict[str, dict[str, Any]] = {}
        if session_dir.exists():
            for f in session_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                sid = data.get("session_id")
                if sid:
                    sessions_by_id[sid] = data

        for tid, ts in state.items():
            if ts.status not in ("claimed", "done", "in_review"):
                continue
            for role, sid, owner in (
                ("implementer", ts.claimed_session, ts.claimed_by),
                ("reviewer", ts.review_claimed_session, ts.review_claimed_by),
            ):
                if not sid:
                    continue
                runtime = sessions_by_id.get(sid)
                reason = ""
                if runtime is None:
                    reason = "missing_runtime"
                elif not runtime.get("active", True):
                    reason = "inactive"
                if reason:
                    stale_sessions.append({
                        "session_id": sid,
                        "task_id": tid,
                        "role": role,
                        "owner": owner,
                        "reason": reason,
                    })
                    stale_claims.append({
                        "task_id": tid,
                        "role": role,
                        "owner": owner,
                        "session_id": sid,
                        "reason": reason,
                        "action": (
                            f"建议运行: orchd force-status --task {tid} --status pending"
                            " --reason 'stale session takeover'"
                        ),
                    })
    except Exception:
        # best-effort：session runtime 不可用时静默降级，不影响既有 watchdog 行为
        pass

    takeover_results: list[dict[str, Any]] = []
    if takeover and stale_claims:
        try:
            from orchd.onboard import force_status

            for claim_info in stale_claims:
                try:
                    result = force_status(
                        store,
                        agent_id=agent_id or "admin",
                        task_id=claim_info["task_id"],
                        target_status="pending",
                        reason="stale session takeover",
                        project_root=project_root,
                    )
                    takeover_results.append({
                        "task_id": claim_info["task_id"],
                        "role": claim_info["role"],
                        "session_id": claim_info["session_id"],
                        "ok": True,
                        "new_status": result.get("new_status"),
                    })
                except OrchdError as exc:
                    takeover_results.append({
                        "task_id": claim_info["task_id"],
                        "role": claim_info["role"],
                        "session_id": claim_info["session_id"],
                        "ok": False,
                        "error": str(exc),
                    })
        except Exception:
            # takeover 属 best-effort：失败不阻断 watchdog 巡检
            pass

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
                "session_id": check.get("session_id") or "",
                "age_min": round(check.get("age_min", 0), 1),
                "release_result": release,
            }
        else:
            # 持有者仍有进行中的实现或审查：审查中锁 ≠ 僵死锁，报告但不释放
            session_lock_stale = {
                "status": "active",
                "agent_id": lock_agent,
                "session_id": check.get("session_id") or "",
                "branch": check.get("branch"),
                "age_min": round(check.get("age_min", 0), 1),
            }
    elif check.get("reason") == "stale_cleaned":
        # 新式 flock 活性锁：持锁进程已死，session_lock_check 已自动清理
        # （复用同一探活逻辑，不再只依赖 timeout/no_active_task）
        session_lock_stale = {
            "status": "released",
            "reason": "stale_os_probe",
            "agent_id": check.get("agent_id") or "",
            "session_id": check.get("session_id") or "",
            "branch": check.get("branch"),
            "age_min": round(check.get("age_min", 0), 1),
            "cleanup_result": check.get("cleanup_result"),
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

    # task-14-worktree-lifecycle（AC5）：孤儿 worktree 惰性清理（best-effort，
    # 绑定任务已终态但 worktree 仍在 → 回收；无独立 worktree 场景零操作）。
    worktree_pruned: dict[str, Any] | None = None
    if project_root is not None:
        try:
            from orchd.ledger import resolve_store_dir
            from orchd.worktree import prune_orphans

            pruned = prune_orphans(project_root, resolve_store_dir(store.orchd_dir), state)
            if pruned.get("pruned"):
                worktree_pruned = pruned
        except Exception:
            pass

    result = {
        "stuck_tasks": stuck,
        "summary": f"{len(stuck)} 个任务可能僵死",
        # 扩展字段（便于脚本集成）
        "stuck_count": len(stuck),
        "timeout_min": timeout_min,
        "session_lock": session_lock_stale,
        "stale_sessions": stale_sessions,
        "stale_claims": stale_claims,
        # task-engine-audit-coverage：任务完整性巡检（只读 best-effort；
        # project_root 为 None / 非 git 时返回 skipped 结构，不影响既有字段）。
        "task_integrity": task_integrity_audit(
            store, tasks, project_root, scope="active"
        ),
    }
    if takeover_results:
        result["takeover_results"] = takeover_results
    if worktree_pruned:
        result["worktree_pruned"] = worktree_pruned
    return result


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
