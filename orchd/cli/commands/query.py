"""Orchd CLI 路由：query 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-query-init-misc）：
  - _cmd_pool: pool 命令（列出就绪池）
  - _cmd_doctor: doctor 命令（git 仓库完整性检测 / 残留清理）
  - _cmd_status: status 命令（全局状态快照 / 单任务详情）
  - _cmd_watchdog: watchdog 命令（僵死任务巡检）

3a 阶段说明：本模块是 query 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.cli.skeleton import _cli_skeleton
from orchd.cli.identity import (
    _current_task_from_branch,
    _resolve_agent_id,
    _session_collision_warning,
)
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)



def _cmd_pool(args) -> dict:
    """列出当前就绪池中的可认领任务。

    CLI 参数: args.capabilities（可选能力过滤）、args.show_all（--all，包含非就绪任务）。
    返回: {"pool": [...], "pool_size": N}。
    """
    from orchd.cli import _load_tasks
    from orchd.ledger import Store
    from orchd.pool import (
        build_pool,
        compute_downstream_blocked,
        effective_importance,
        sort_candidates,
    )

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    state = store.replay()
    imp_thresholds = master.config.get("importance") if hasattr(master, "config") else None

    def _entry(task: dict, blocked_count: int) -> dict:
        entry = {
            "task_id": task.get("id", ""),
            "name": task.get("name", ""),
            "brief": task.get("brief", ""),
            "module": task.get("module", ""),
            "importance": effective_importance(task, blocked_count, imp_thresholds),
            "blocked_downstream_count": blocked_count,
            "source": task.get("source"),
        }
        if "difficulty" in task:
            entry["difficulty"] = task["difficulty"]
        return entry

    blocked_counts = compute_downstream_blocked(tasks, state)

    if args.show_all:
        # --all：包含非就绪任务并附加 status 字段
        all_entries = []
        for task in tasks:
            tid = task.get("id", "")
            ts = state.get(tid)
            entry = _entry(task, blocked_counts.get(tid, 0))
            entry["status"] = ts.status if ts else "pending"
            all_entries.append(entry)
        return {"pool": all_entries, "pool_size": len(all_entries), "all": True}

    candidates = build_pool(
        tasks, state, capabilities=_flatten_nargs(args.capabilities)
    )
    candidates = sort_candidates(candidates, importance_thresholds=imp_thresholds)
    return {
        "pool": [_entry(c.task, c.blocked_downstream_count) for c in candidates],
        "pool_size": len(candidates),
    }

def _cmd_doctor(args):
    """检测 git 仓库完整性（只读），或执行残留清理（--fix / --dry-run）。

    CLI 参数:
        args.path: 项目根目录（默认当前目录）。
        args.fix: 显式执行残留清理。
        args.dry_run: 仅预览残留项，不删除任何文件。
        args.backup_dir: 清理前备份目录（可选）。

    返回: (result, exit_code) 元组——检出任一 fail 项时 exit_code 为 1，
    供 session 三连检查脚本化复用。
    """
    from orchd.doctor import doctor, doctor_fix

    # 残留清理模式（--fix 或 --dry-run）
    if getattr(args, "fix", False) or getattr(args, "dry_run", False):
        backup_dir = getattr(args, "backup_dir", None)
        dry_run = not getattr(args, "fix", False) or getattr(args, "dry_run", False)
        result = doctor_fix(
            Path(args.path),
            dry_run=dry_run,
            backup_dir=Path(backup_dir) if backup_dir else None,
        )
        # 退出码语义（脚本化感知残留）：dry-run 有可清项 → 1；--fix 有失败 → 1；
        # 否则 0。与只读模式「有 fail 项 → 1」保持一致。
        exit_code = 0
        if dry_run:
            cleanable = (
                len(result.get("detected", []))
                - len(result.get("skipped_protected", []))
                - len(result.get("skipped_manual", []))
            )
            if cleanable > 0:
                exit_code = 1
        elif result.get("errors"):
            exit_code = 1
        return result, exit_code

    # 只读检测模式
    result = doctor(Path(args.path))
    if not result["repo_ok"]:
        return result, 1
    return result, 0

def _format_audit_text(audit: dict) -> str:
    """task-status-text-flag-silent-noop：将 audit 结果字典格式化为人类可读文本。

    递归展开顶层键值，列表/字典缩进展示；超长值截断。best-effort，
    任何异常回退 repr 截断。
    """
    try:
        lines = []
        for k, v in audit.items():
            if isinstance(v, (list, tuple)):
                lines.append(f"{k}: {len(v)} item(s)")
                for i, item in enumerate(v[:5]):
                    if isinstance(item, dict):
                        lines.append(f"  [{i}] " + ", ".join(f"{ik}={iv}" for ik, iv in list(item.items())[:4]))
                    else:
                        lines.append(f"  [{i}] {str(item)[:80]}")
                if len(v) > 5:
                    lines.append(f"  ... and {len(v) - 5} more")
            elif isinstance(v, dict):
                lines.append(f"{k}:")
                for ik, iv in list(v.items())[:6]:
                    lines.append(f"  {ik}: {str(iv)[:80]}")
            else:
                lines.append(f"{k}: {str(v)[:100]}")
        return "\n".join(lines)
    except Exception:
        return repr(audit)[:500]


@_cli_skeleton
def _cmd_status(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """获取全局状态快照或单任务详情。

    CLI 参数: args.task（可选，任务 ID）、args.text（--text，人类可读表格输出）、
    args.all（--all，显示全量任务含终态；默认仅活跃任务）、
    args.audit_merge（--audit-merge，附加只读 merge 巡检）。
    返回: 全局状态字典或单任务详情字典；若 --text 模式则直接打印表格并返回 None。
    """
    from orchd.ledger import stale_review_claims
    from orchd.report import intake_audit, merge_audit, revive_audit, status, task_integrity_audit
    # 红线 8（R3）：status 前置校验运行时文件完整性（只读告警，不阻断）
    integrity_warnings = store.check_integrity()
    result = status(
        store, tasks, project=master.project, text=args.text, task_id=args.task,
        project_root=orchd_dir.parent,
        active_only=not getattr(args, "all", False),
    )
    # W-2 僵尸审查认领：status 读路径浮现超时未提交的审查认领（同 request 判定），
    # 让"任何一次状态查看"都能暴露僵局，不依赖后续再发 request。
    if not args.text:
        try:
            _stale = stale_review_claims(store.replay())
        except Exception:
            _stale = {}
        if _stale:
            result["stale_reviews"] = [
                {"task_id": tid, **v}
                for tid, v in sorted(_stale.items(), key=lambda kv: kv[1]["age_s"], reverse=True)
            ]
    # 会话指纹碰撞只读告警（task-contract-session-collision-warning）：不阻断、不落状态
    if not args.text:
        exclude = args.task or _current_task_from_branch(orchd_dir.parent)
        collision = _session_collision_warning(agent_id, store, exclude_task_id=exclude)
        if collision:
            result["session_collision_warning"] = collision
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    # task-status-text-flag-silent-noop：--audit-* 仅全局模式生效；
    # 单任务模式（args.task）时不再静默忽略，stderr 给出明确提示。
    import sys as _sys
    _audit_flags = [
        ("audit_merge", "--audit-merge"),
        ("audit_intake", "--audit-intake"),
        ("audit_revive", "--audit-revive"),
        ("audit_task", "--audit-task"),
    ]
    if args.task is not None:
        for _flag, _opt in _audit_flags:
            if getattr(args, _flag, False):
                print(
                    f"orchd status: {_opt} 仅在全局模式（无 task-id）下生效，"
                    f"当前指定了 task '{args.task}'，该标志已被忽略。"
                    f"正确用法：orchd status {_opt}（不加 task-id）",
                    file=_sys.stderr,
                )
    if args.audit_merge and args.task is None:
        result["merge_audit"] = merge_audit(store, tasks, orchd_dir.parent)
    if getattr(args, "audit_intake", False) and args.task is None:
        result["intake_audit"] = intake_audit(orchd_dir.parent)
    if getattr(args, "audit_revive", False) and args.task is None:
        result["revive_audit"] = revive_audit(store, tasks, orchd_dir.parent)
    if getattr(args, "audit_task", False) and args.task is None:
        result["audit_task"] = task_integrity_audit(
            store, tasks, orchd_dir.parent, scope="merged"
        )
    if args.text and "_text" in result:
        # --text 为人类可读展示层：只输出表格，不再混入 JSON。
        # 末尾追加无感引导文字（task-guide-seamless-guidance，best-effort）。
        table = result.pop("_text")
        # task-status-text-flag-silent-noop：--text 叠加 --audit-* 时，
        # 巡检结论追加到表格文本，杜绝「算完即丢」。
        for _audit_key, _label in (
            ("merge_audit", "Merge Audit"),
            ("intake_audit", "Intake Audit"),
            ("revive_audit", "Revive Audit"),
            ("audit_task", "Task Integrity Audit"),
        ):
            _audit = result.get(_audit_key)
            if _audit is not None:
                table += f"\n\n=== {_label} ===\n"
                table += _format_audit_text(_audit)
        try:
            from orchd.guide import status_guidance_text
            from orchd.ledger import resolve_review_mode
            # status 命令前置必有 master → has_master=True（空项目时显示 empty_project 而非 first_time）
            # review-unify-r2：传 review_mode，引导文字按模式分流（模板路径不影响文字，但保持一致）
            review_mode = resolve_review_mode(store.orchd_dir)
            table += status_guidance_text(store.replay(), tasks, has_master=True,
                                           review_mode=review_mode)
        except Exception:
            pass  # 引导失败静默跳过，不影响表格输出
        print(table)
        return None
    return result

def _cmd_watchdog(args):
    """巡检僵死任务（实现者超时 / 审查者超时）。

    CLI 参数: args.timeout（超时分钟数，默认 60）。
    返回: 巡检结果字典；若存在僵死任务则以 ``(result, 1)`` 元组返回以设置非零 exit code。
    """
    from orchd.cli import _load_tasks
    from orchd.ledger import Store
    from orchd.report import watchdog

    tasks, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    result = watchdog(
        store, tasks, timeout_min=args.timeout, project_root=orchd_dir.parent,
        agent_id=_resolve_agent_id(orchd_dir), takeover=args.takeover,
    )
    if result["stuck_count"] > 0:
        return result, 1
    return result


def register(sub) -> None:
    """注册 query 模块的子命令。"""
    # pool
    p = sub.add_parser("pool", help="列出就绪池")
    p.add_argument("--capabilities", nargs="*")
    p.add_argument("--all", action="store_true", dest="show_all")
    p.set_defaults(func=_cmd_pool)

    # status
    p = sub.add_parser("status", help="全局状态快照；可跟 task-id 查单任务详情")
    p.add_argument("task", nargs="?", default=None, help="可选：任务 ID，查询单任务详情")
    p.add_argument("--text", action="store_true")
    p.add_argument("--all", action="store_true",
                   help="显示全量任务（含终态 completed/cancelled）；默认仅活跃任务")
    p.add_argument("--audit-merge", action="store_true",
                   help="附加 merge 巡检：completed 任务对应 task/{id} 分支未并入 main 的告警清单（只读）")
    p.add_argument("--audit-intake", action="store_true",
                   help="附加摄入产物审计：未提交的 IDEAS.md / _master.json 改动告警（只读）")
    p.add_argument("--audit-revive", action="store_true",
                   help="附加复活巡检：扫描 ledger 中 completed→pending 的强制复活操作，列告警（只读）")
    p.add_argument("--audit-task", action="store_true",
                   help="附加任务完整性巡检：merged 任务的历史缺失/残留（main 残留 + 分支 diff 缺失声明文件，只读）")
    p.set_defaults(func=_cmd_status)

    # watchdog
    p = sub.add_parser("watchdog", help="僵死任务巡检")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--takeover", action="store_true",
                   help="对 stale_claims 中确认会话已失效的任务执行 force-status 回退 pending（best-effort）")
    p.set_defaults(func=_cmd_watchdog)

    # doctor
    p = sub.add_parser("doctor", help="git 仓库完整性只读检测 / 残留清理（--fix / --dry-run）")
    p.add_argument("--path", default=".", help="项目根目录（含 .git），默认当前目录")
    p.add_argument("--fix", action="store_true",
                   help="显式执行残留清理（默认 dry-run 预览）")
    p.add_argument("--dry-run", action="store_true",
                   help="仅预览残留项，不删除任何文件（可与 --fix 同时使用）")
    p.add_argument("--backup-dir", default=None,
                   help="清理前备份目录（默认 .orchd/.doctor-backup/<timestamp>）")
    p.set_defaults(func=_cmd_doctor)

