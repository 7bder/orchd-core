"""IDEAS.md 自动归档引擎（叶子模块，零 orchd 内部依赖）。

职责：当某 idea 对应的全部任务进入终态（completed / cancelled）时，将该
``##`` 条目**整块原样**从 ``IDEAS.md`` 移入 ``IDEAS-archive.md``，实现
IDEAS.md 的"只增不减"膨胀优化。全程 best-effort 非阻塞，任何解析/读写
异常静默降级，不阻断调用方（对齐 merged:false / amend 自动提交哲学）。

安全约束：
- 只做内容域写操作（IDEAS.md / IDEAS-archive.md），不改事件格式、不改
  状态机分支、不改 CLI 契约语义（§9.1 停服升级边界内零改动）；
- 整块原样搬移，不压缩 notes、不重写用户正文，保留"原文可追溯"铁律；
- ``source: idea:<ref>`` 是自动归档的唯一映射依据；无 source 的存量条目
  无法自动映射，由 ``ideas-archive`` 手动命令一次性回填。

依赖方向：ideas.py → 标准库（pathlib）+ orchd.ledger（读任务终态，可选）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 终态集合：某 idea 下全部任务处于终态 → 该条目可归档
_TERMINAL = {"completed", "cancelled"}

# 归档文件头注释（新建时写入）
_ARCHIVE_HEADER = (
    "# IDEAS Archive\n\n"
    "本文件由 orchd 引擎自动维护：已完结 idea 条目（对应任务全部进入终态）"
    "从 IDEAS.md 自动移入此处，保留原文与审计可追溯性。勿手动编辑。\n"
)


def parse_ideas(text: str) -> list[dict[str, Any]]:
    """解析 IDEAS.md 条目，返回条目列表（含标题、状态、原始行区间）。

    条目识别规则（复用 ``spec._check_idea_reference`` 的解析逻辑）：
    - 以 ``## `` 开头的行为条目标题行；
    - 该行之后、下一个 ``## `` 之前的行用于提取 ``status`` 字段，
      支持 ``- status: pending``（列表）与 ``status: pending`` 两种格式。

    Args:
        text: IDEAS.md 全文。

    Returns:
        条目字典列表，每项含：
        - ``title``: 条目标题（去掉 ``## `` 前缀并 strip）。
        - ``status``: 条目状态（未声明则为空串）。
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
                "start_line": i,
                "end_line": None,
            }
        elif current is not None:
            for marker in ("- status:", "status:"):
                if stripped.startswith(marker):
                    current["status"] = stripped[len(marker):].strip()
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

    遍历任务 ``source: idea:<ref>``，按 ``ref_id`` 分组；某 ``ref_id`` 下
    **全部**任务处于终态（completed / cancelled）→ 该条目（``ref_id in title``）
    标记 resolved。无 source 的任务不影响判定。

    Args:
        master: 已加载的 Master 对象（含 tasks 与各自 source 字段）。
        entries: parse_ideas 解析出的条目列表。
        task_status: 任务 ID → 状态 的映射（来自 ``Store.replay()`` 或
            ``report.status``）。缺失或为空时按"无终态证据"处理（不误归档）。

    Returns:
        已完结的条目子列表。
    """
    task_status = task_status or {}

    # 按 ref_id 分组：source: idea:<ref>
    ref_groups: dict[str, list[str]] = {}
    for t in master.tasks:
        source = t.get("source")
        if not source or not isinstance(source, str):
            continue
        prefix, _, ref_id = source.partition(":")
        if prefix != "idea" or not ref_id:
            continue
        ref_groups.setdefault(ref_id, []).append(t.get("id", ""))

    resolved_refs = {
        ref_id
        for ref_id, tids in ref_groups.items()
        if tids and all(task_status.get(tid) in _TERMINAL for tid in tids)
    }
    if not resolved_refs:
        return []

    return [
        e for e in entries
        if any(ref in e["title"] for ref in resolved_refs)
    ]


def _remove_blocks(
    text: str, entries: list[dict[str, Any]], resolved: list[dict[str, Any]]
) -> str:
    """从原文中删除 resolved 条目对应的行块，返回剩余文本。

    按行索引过滤，保留非 resolved 块的行；块与块之间残留的空行原样保留
    （不重写正文，最小化对用户文本的改动）。
    """
    lines = text.splitlines(keepends=True)
    remove_ranges = [(e["start_line"], e["end_line"]) for e in resolved]
    keep: list[str] = []
    for i, line in enumerate(lines):
        if any(start <= i < end for start, end in remove_ranges):
            continue
        keep.append(line)
    return "".join(keep)


def archive_resolved_ideas(project_root, master) -> dict[str, Any]:
    """主入口：把已完结 idea 条目从 IDEAS.md 移入 IDEAS-archive.md。

    Args:
        project_root: 项目根目录。
        master: 已加载的 Master 对象。

    Returns:
        结构化结果，永不抛异常：
        - ``{"archived": [...], "kept": n}`` 成功归档；archived 为标题列表。
        - ``{"archived": [], "kept": 0, "skipped": "<原因>"}`` 无 IDEAS.md /
          读取失败 / 无可归档条目 / 写入失败（best-effort 降级）。
    """
    project_root = Path(project_root)
    # AC3（task-12-engine-path-abstraction）：工作区文档（IDEAS.md /
    # IDEAS-archive.md）走统一工作区根 helper（默认 .orchd/，兼容旧根路径）。
    # Store 的账本根仍由 ORCHD_HOME 解析（与文档根分离）。
    from orchd.ledger import resolve_workspace_root

    workspace_root = resolve_workspace_root(project_root)
    ideas_path = workspace_root / "IDEAS.md"
    if not ideas_path.exists():
        return {"archived": [], "kept": 0, "skipped": "no_ideas_file"}

    try:
        text = ideas_path.read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return {"archived": [], "kept": 0, "skipped": "read_error"}

    # 读取任务终态（best-effort：ledger 不可用则无证据，不归档）
    task_status: dict[str, str] = {}
    try:
        from orchd.ledger import Store

        orchd_dir = project_root / ".orchd"
        store = Store(orchd_dir)
        task_status = {
            tid: ts.status for tid, ts in store.replay().items()
        }
    except Exception:
        pass

    entries = parse_ideas(text)
    resolved = find_resolved_entries(master, entries, task_status)
    if not resolved:
        return {"archived": [], "kept": len(entries)}

    blocks = [extract_entry_block(text, e) for e in resolved]
    new_ideas = _remove_blocks(text, entries, resolved)

    archive_path = workspace_root / "IDEAS-archive.md"
    try:
        if archive_path.exists():
            archive_text = archive_path.read_text(encoding="utf-8")
        else:
            archive_text = _ARCHIVE_HEADER
        if archive_text and not archive_text.endswith("\n"):
            archive_text += "\n"
        archive_text += "".join(blocks)

        # 先写归档文件（成功后再改主文件，避免主文件已删而归档丢失）
        archive_path.write_text(archive_text, encoding="utf-8")
        ideas_path.write_text(new_ideas, encoding="utf-8")
    except (OSError, IOError):
        return {"archived": [], "kept": len(entries), "skipped": "write_error"}

    return {"archived": [e["title"] for e in resolved], "kept": len(entries) - len(resolved)}