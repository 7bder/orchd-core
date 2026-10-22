"""orchd ideas — 文本解析与引用域（W6-C4 拆分，纯搬移）。

来源：``orchd/ideas.py``（1211 行，W6-C4 纯搬移）。
"""

from __future__ import annotations

import json
import re as _re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import OrchdError
from orchd.intake import (
    _atomic_write_text,
    _find_idea_entry,
    _intake_guard,
    _list_idea_headers,
    _resolve_lock_orchd_dir,
)
# 行式台账文本原语（task-idea-archive-hardening）：读盘兜底与自由文本单行化收敛到
# ledger 层（依赖方向见 .orchd/shared/conventions.md——ideas 已模块级导入 intake，
# 刻意不从 intake 反向取；ledger 是本模块与其共用的下层）。
from orchd.ledger import read_text_or_error, single_line


# 终态集合：某 idea 下全部任务处于终态 → 该条目可归档
# （task-terminal-single-p2：收敛到 contracts/events 单一真源，原为硬编码元组）
_TERMINAL = TERMINAL_STATUSES



def parse_ideas(text: str) -> list[dict[str, Any]]:
    """解析 IDEAS.md 条目，返回条目列表（含标题、状态、id、原始行区间）。

    条目识别规则（复用 ``spec._check_idea_reference`` 的解析逻辑）：
    - 以 ``## `` 开头的行为条目标题行；
    - 该行之后、下一个 ``## `` 之前的行用于提取 ``status`` 与 ``id`` 字段，
      支持 ``- status: pending``（列表）与 ``status: pending`` 两种格式；
      ``- id: <slug>``（列表）与 ``id: <slug>`` 两种格式（缺失为空串）。

    Args:
        text: IDEAS.md 全文。

    Returns:
        条目字典列表，每项含：
        - ``title``: 条目标题（去掉 ``## `` 前缀并 strip）。
        - ``status``: 条目状态（未声明则为空串）。
        - ``id``: 条目显式 id（``- id:`` 字段，缺失则为空串）——归档按 id
          精确归属的权威锚点（ideas-archive-exact-match）。
        - ``notes``: 条目备注（``- notes:`` 字段，缺失则为空串，
          task-idea-multi-source-attribution 孤儿可见性用）。
        - ``start_line`` / ``end_line``: 原始行为区间（0-based，半开区间
          ``[start_line, end_line)``），用于 ``extract_entry_block`` 精确切块。
    """
    lines = text.splitlines(keepends=True)
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            if current:
                current["end_line"] = i
                entries.append(current)
            current = {
                "title": stripped[3:].strip(),
                "status": "",
                "id": "",
                "notes": "",
                "start_line": i,
                "end_line": None,
            }
        elif current is not None:
            for marker in ("- status:", "status:"):
                if stripped.startswith(marker):
                    current["status"] = stripped[len(marker):].strip()
                    break
            for marker in ("- id:", "id:"):
                if stripped.startswith(marker):
                    current["id"] = stripped[len(marker):].strip()
                    break
            # task-idea-multi-source-attribution：解析 notes（孤儿可见性用）
            for marker in ("- notes:", "notes:"):
                if stripped.startswith(marker):
                    current["notes"] = stripped[len(marker):].strip()
                    break
    if current:
        current["end_line"] = len(lines)
        entries.append(current)
    return entries



def extract_entry_block(text: str, entry: dict[str, Any]) -> str:
    """精确切出条目整块（``## `` 行到下一个 ``## `` 行之前），逐字节原样。

    Args:
        text: IDEAS.md 全文（与 parse_ideas 相同文本）。
        entry: parse_ideas 返回的条目字典（含 start_line / end_line）。

    Returns:
        该条目整块原文（含行尾换行），不修改任何内容。
    """
    lines = text.splitlines(keepends=True)
    start = entry.get("start_line", 0)
    end = entry.get("end_line", len(lines))
    return "".join(lines[start:end])



def find_resolved_entries(
    master,
    entries: list[dict[str, Any]],
    task_status: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """判定哪些条目已完结（对应任务全部终态）。

    遍历任务 ``source: (idea|roadmap):<ref>``，按 ``ref_id`` 分组；某 ``ref_id`` 下
    **全部**任务处于终态（completed / cancelled）→ 该条目（``- id == ref_id``）
    标记 resolved。无 source 的任务不影响判定。roadmap 前缀与 idea 同口径
    （task-roadmap-auto-archive：roadmap 源任务终态同样触发归档）。

    Args:
        master: 已加载的 Master 对象（含 tasks 与各自 source 字段）。
        entries: parse_ideas 解析出的条目列表。
        task_status: 任务 ID → 状态 的映射（来自 ``Store.replay()`` 或
            ``report.status``）。缺失或为空时按"无终态证据"处理（不误归档）。

    Returns:
        已完结的条目子列表。
    """
    task_status = task_status or {}

    # 按 ref_id 分组：source: (idea|roadmap):<ref> + additional_sources[] 中的同前缀 ref。
    # task-idea-multi-source-attribution：归档匹配同时覆盖 source 与 additional_sources，
    # 任一命中即随任务完结归档。
    # task-roadmap-auto-archive：分组前缀由 idea 扩为 idea|roadmap——roadmap 源任务
    # （source: roadmap:<id>）终态后同样触发条目归档（此前仅认 idea，roadmap 条目永不归档）。
    ref_groups = _group_ref_tasks(master)

    resolved_refs = {
        ref_id
        for ref_id, tids in ref_groups.items()
        if tids and all(task_status.get(tid) in _TERMINAL for tid in tids)
    }
    if not resolved_refs:
        return []

    return [
        e for e in entries
        if _entry_is_resolved(e, resolved_refs)
    ]



def _group_ref_tasks(master) -> dict[str, list[str]]:
    """按 ``source`` / ``additional_sources`` 的 idea|roadmap ref 分组任务 id。

    单一口径：``find_resolved_entries``（全终态 ⇒ 可归档）与
    ``find_live_source_refs``（存在非终态 ⇒ 禁止人工裁决搬走）都读本函数，
    两侧分组规则一旦漂移就会一个放行一个放行得不对，故不得各写一遍。

    Returns:
        ``{ref_id: [task_id, ...]}``（无匹配 ref 的任务不入表）。
    """
    ref_groups: dict[str, list[str]] = {}
    for t in master.tasks:
        tid = t.get("id", "")
        # 主 source
        source = t.get("source")
        if source and isinstance(source, str):
            prefix, _, ref_id = source.partition(":")
            if prefix in ("idea", "roadmap") and ref_id:
                ref_groups.setdefault(ref_id, []).append(tid)
        # additional_sources（加法式：不改变 source 语义）
        additional = t.get("additional_sources")
        if additional and isinstance(additional, list):
            for asrc in additional:
                if not isinstance(asrc, str):
                    continue
                aprefix, _, aref = asrc.partition(":")
                if aprefix in ("idea", "roadmap") and aref:
                    ref_groups.setdefault(aref, []).append(tid)
    return ref_groups



def find_live_source_refs(master, task_status: dict[str, str]) -> dict[str, list[str]]:
    """仍被非终态任务引用的 idea ref ⇒ ``{ref_id: [task_id, ...]}``。

    与 :func:`find_resolved_entries` 互补：前者判「全部终态 ⇒ 可自动归档」，
    本函数判「还有非终态 ⇒ 人工裁决不得把它从 IDEAS.md 搬走」——E025 溯源只读
    IDEAS.md，条目一旦被搬走，在途任务当场失去来源。
    """
    task_status = task_status or {}
    return {
        ref_id: sorted(tid for tid in tids if task_status.get(tid) not in _TERMINAL)
        for ref_id, tids in _group_ref_tasks(master).items()
        if any(task_status.get(tid) not in _TERMINAL for tid in tids)
    }



def _entry_is_resolved(entry: dict[str, Any], resolved_refs: set[str]) -> bool:
    """条目是否应自动归档（显式 id 强约束 + 保守兜底）。

    主机制：条目 ``- id:`` 与 resolved ref **精确相等**（id 权威，废除标题
    裸子串匹配——日期词 ref 因无对应条目 id 必然被拒，从数据模型杜绝歧义）。

    保守兜底：无 id 条目**永不自动归档**（即使标题完整词命中 ref 也不归档，
    防未来遗漏 id 的条目被日期词/标题词 ref 误伤）。

    防御纵深：精确完整词标题匹配（``spec._exact_ref_match``）保留用于 roadmap
    溯源，此处不启用——日期词 ref（如 ``2026-08-22``）在标题中同样是完整词，
    无法防误伤，故以「id 权威 + 无 id 保守」为唯一归档路径。
    """
    eid = (entry.get("id") or "").strip()
    if not eid:
        return False
    return eid in resolved_refs
