"""report — 文本渲染域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

``_format_text`` / 阶段文本 / 自动清理文本与 stderr 提示。

**叶子域**：零包内相对导入。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root



def _format_phase_text(
    phases: list[dict[str, Any]],
    active_phase: str | None,
) -> str:
    """--text 模式下的阶段聚合可读表格（活跃阶段标 *）。"""
    if not phases:
        return "\n[phases] (none)\n"
    lines = ["[phases]"]
    lines.append(
        f"  {'phase':<12}{'total':>6}{'completed':>11}{'active':>8}{'pending':>9}"
    )
    for ph in phases:
        mark = " *" if ph["phase"] == active_phase else ""
        lines.append(
            f"  {ph['phase'] + mark:<14}{ph['total']:>6}"
            f"{ph['completed']:>11}{ph['active']:>8}{ph['pending']:>9}"
        )
    return "\n" + "\n".join(lines) + "\n"



def _format_text(result: dict[str, Any]) -> str:
    """将 status() 返回的结构化结果格式化为人类可读的纯文本表格。

    表格格式：
    - 表头行：ID（20 字符左对齐）、Status（12 字符）、Claimed By（15 字符）、Name
    - 分隔线：70 个连字符
    - 每行一个任务，字段对齐方式同表头（默认仅活跃任务，--all 时含终态）
    - 末尾空行后跟一行汇总：Total / active + 六状态各自的计数

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
        f"active={summary.get('active', 0)} "
        f"pending={summary['pending']} claimed={summary['claimed']} "
        f"done={summary['done']} in_review={summary['in_review']} "
        f"completed={summary['completed']} cancelled={summary['cancelled']}"
    )
    return "\n".join(lines)



def _auto_clean_emit_stderr(project_root: Path) -> bool:
    """auto_clean 是否写 stderr：受 config.guidance_stderr 约束（task-guide-block-config）。

    status()/watchdog() 挂载 auto_clean 时，其 ``[回收]`` stderr 与提示块同开关：
    关闭（false）时读路径零 stderr、stdout 仍并入 auto_clean 字段，避免撞
    test_guidance_stderr_disabled 契约。best-effort，读取失败回退默认 true。

    master 路径经 ``orchd.worktree.resolve_master_path_from_dir`` 单一真源解析
    （本地优先 → canonical 主工作树回退）：container 容器根视角读到 canonical
    权威配置，不再裸读本地副本静默回落 true。缺省与失败语义不变。
    """
    try:
        from orchd.spec import load_master
        from orchd.worktree import resolve_master_path_from_dir

        master_path = resolve_master_path_from_dir(Path(project_root) / ".orchd")
        if not master_path.exists():
            return True
        master = load_master(master_path)
        return bool(master.config.get("guidance_stderr", True))
    except Exception:
        # best-effort：读取失败回退默认 true，绝不阻塞主流程
        return True



def _format_auto_clean_text(ac: dict[str, Any]) -> str:
    """把 status() 的 ``auto_clean`` 字段格式化为人类可读文本（--text 模式）。

    与 _format_text 并列的 append 段：在任务表格汇总行之后输出自动清洁的
    决策上下文（清理 / 备份 / 报告项），无内容时返回空串。

    纵深容错（task-status-text-autoclean-path）：单项缺 ``path`` 时用 ``backup``
    回退、再缺则显示 ``?``——读路径展示绝不因单项缺键崩整表（此前 ``it['path']``
    硬取致 ``status --text`` 落 E999）。
    """
    cleaned = ac.get("auto_cleaned") or []
    moved = ac.get("auto_moved") or []
    notice = ac.get("manual_notice") or []
    if not (cleaned or moved or notice):
        return ""
    lines = [""]
    if ac.get("disabled"):
        lines.append("自动清洁：已关闭（ORCHD_AUTO_CLEAN=off/report，仅报告）")
    else:
        lines.append("自动清洁：")
    for it in cleaned:
        name = Path(it.get("path") or it.get("backup") or "?").name
        lines.append(f"  [清理] {name}（{it.get('type')}）")
    for it in moved:
        name = Path(it.get("path") or "?").name
        lines.append(f"  [备份] {name} → {it.get('backup') or '?'}")
    for it in notice:
        name = Path(it.get("path") or "?").name
        lines.append(
            f"  [报告] {name}（{it.get('type')}）：{it.get('disposition')}"
        )
    return "\n".join(lines)
