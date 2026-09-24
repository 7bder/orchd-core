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

依赖方向：ideas.py → 标准库（pathlib）+ orchd.errors（准入锁超时降级）+
orchd.intake（``_atomic_write_text`` / ``_resolve_lock_orchd_dir``，**模块级**导入：
``cli._util._preimport_archive_deps`` 预导入 orchd.ideas 时会连带绑定 orchd.intake，
使任务 worktree 终态回收删除源码后进程内归档仍可原子写 + 取准入锁）+
orchd.ledger（读任务终态与准入锁，惰性导入）。

task-intake-atomic-lock-consistent（AC2）：IDEAS.md 的「读-改-写」与
IDEAS-archive.md 的追加写入整体纳入 ``.intake.lock`` 串行（与 amend / intake /
idea propose- confirm 共用同一把准入写锁），两次写入统一走
``intake._atomic_write_text``（tmp + ``os.replace``），崩溃不留半截文件。

task-archive-lock-unavailable-recovery：锁不可用（E012 / OSError）时不再静默
丢弃积压——写入「待归档标记」（时间戳 / 可归档条目数 / 原因）到账本根运行时
文件（``ledger.resolve_store_dir`` 解析，与 ``_ledger.jsonl`` 同级，不随工作区
文档走 git），下次归档触发时优先重试并在成功后清除标记（幂等）；同时暴露
结构化读取 API（``read_archive_pending``）供 doctor/状态命令巡检接线。硬约束
保持：标记写入**不触碰** IDEAS.md / IDEAS-archive.md。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import OrchdError
from orchd.intake import _atomic_write_text, _resolve_lock_orchd_dir

# 终态集合：某 idea 下全部任务处于终态 → 该条目可归档
_TERMINAL = {"completed", "cancelled"}

# 待归档标记文件名（账本根运行时文件；task-archive-lock-unavailable-recovery：
# 锁不可用时的兜底留痕，与 _ledger.jsonl 同级，不随工作区文档走 git）
_ARCHIVE_PENDING_FILE = "archive-pending.json"

# 归档文件头注释（新建时写入）
_ARCHIVE_HEADER = (
    "# IDEAS Archive\n\n"
    "本文件由 orchd 引擎自动维护：已完结 idea 条目（对应任务全部进入终态）"
    "从 IDEAS.md 自动移入此处，保留原文与审计可追溯性。勿手动编辑。\n"
)


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

    遍历任务 ``source: idea:<ref>``，按 ``ref_id`` 分组；某 ``ref_id`` 下
    **全部**任务处于终态（completed / cancelled）→ 该条目（``- id == ref_id``）
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

    # 按 ref_id 分组：source: idea:<ref> + additional_sources[] 中的 idea:<ref>
    # task-idea-multi-source-attribution：归档匹配同时覆盖 source 与 additional_sources，
    # 任一命中即随任务完结归档。
    ref_groups: dict[str, list[str]] = {}
    for t in master.tasks:
        tid = t.get("id", "")
        # 主 source
        source = t.get("source")
        if source and isinstance(source, str):
            prefix, _, ref_id = source.partition(":")
            if prefix == "idea" and ref_id:
                ref_groups.setdefault(ref_id, []).append(tid)
        # additional_sources（加法式：不改变 source 语义）
        additional = t.get("additional_sources")
        if additional and isinstance(additional, list):
            for asrc in additional:
                if not isinstance(asrc, str):
                    continue
                aprefix, _, aref = asrc.partition(":")
                if aprefix == "idea" and aref:
                    ref_groups.setdefault(aref, []).append(tid)

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


def find_orphan_entries(
    master,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """列出孤儿 idea 条目（task-idea-multi-source-attribution，只读不阻断）。

    孤儿定义：条目 ``status: pending`` 且 ``notes`` 标注「注册为 task-xxx」，
    但该 task 的 ``source`` 与 ``additional_sources`` 均不引用此条目 id。
    此类条目因任务承接多 idea 时只写了主 source 而漏挂次级引用，导致永远
    等不到归档。本函数仅做可见性（供 doctor/CLI 后续巡检接线），不自动
    修改 _master.json 或 IDEAS.md。

    Args:
        master: 已加载的 Master 对象。
        entries: parse_ideas 解析出的条目列表。

    Returns:
        孤儿条目子列表（含 id / title / 注册的 task_id）。
    """
    # 收集所有任务引用的 idea ref（source + additional_sources）
    referenced_refs: set[str] = set()
    task_ref_map: dict[str, list[str]] = {}  # task_id -> [ref_ids]
    for t in master.tasks:
        tid = t.get("id", "")
        refs: list[str] = []
        source = t.get("source")
        if source and isinstance(source, str):
            prefix, _, ref_id = source.partition(":")
            if prefix == "idea" and ref_id:
                refs.append(ref_id)
        additional = t.get("additional_sources")
        if additional and isinstance(additional, list):
            for asrc in additional:
                if isinstance(asrc, str):
                    aprefix, _, aref = asrc.partition(":")
                    if aprefix == "idea" and aref:
                        refs.append(aref)
        for r in refs:
            referenced_refs.add(r)
            task_ref_map.setdefault(tid, []).append(r)

    orphans: list[dict[str, Any]] = []
    import re as _re
    for e in entries:
        if (e.get("status") or "").strip() != "pending":
            continue
        eid = (e.get("id") or "").strip()
        if not eid or eid in referenced_refs:
            continue
        # 检查 notes 是否标注「注册为 task-xxx」
        notes = e.get("notes") or e.get("note") or ""
        if isinstance(notes, str):
            m = _re.search(r"注册为\s*(task-[a-z0-9-]+)", notes)
            if m:
                orphans.append({
                    "id": eid,
                    "title": e.get("title", ""),
                    "registered_task": m.group(1),
                    "notes": notes,
                })
    return orphans


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

    并发与原子性（task-intake-atomic-lock-consistent AC2）：IDEAS.md 的「读-改-写」
    与 IDEAS-archive.md 的追加写入整体在 ``.intake.lock`` 持有范围内完成（与 amend /
    intake / idea propose-confirm 共用同一把准入写锁）；两次写入统一走
    ``intake._atomic_write_text``（tmp + ``os.replace``），崩溃不留半截文件。

    Args:
        project_root: 项目根目录。
        master: 已加载的 Master 对象。

    Returns:
        结构化结果，永不抛异常：
        - ``{"archived": [...], "kept": n}`` 成功归档；archived 为标题列表。
        - ``{"archived": [], "kept": 0, "skipped": "<原因>"}`` 无 IDEAS.md /
          读取失败 / 无可归档条目 / 写入失败 / 准入锁不可用（best-effort 降级，
          绝不无锁并发归档）。
    """
    project_root = Path(project_root)
    # AC3（task-12-engine-path-abstraction）：工作区文档（IDEAS.md /
    # IDEAS-archive.md）走统一工作区根 helper（默认 .orchd/，兼容旧根路径）。
    # Store 的账本根仍由 ORCHD_HOME 解析（与文档根分离）。
    # canonical 共享读（task-canonical-workspace-docs，2026-08-25）：container 布局
    # 下 resolve_workspace_root 解析到 canonical 主工作树根，归档源（IDEAS.md）与
    # 归档目标（IDEAS-archive.md）统一在主工作树读写，任务 worktree 本地副本不参与。
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
        resolve_workspace_root,
    )

    # canonical 工作区根（task-canonical-workspace-docs，2026-08-25）：
    # resolve_workspace_root 先解析到 canonical 主工作树根（container 布局返回
    # main/，flat 返回本地），IDEAS.md / IDEAS-archive.md 以主工作树副本为权威，
    # 避免任务 worktree 本地 .orchd/ 拷贝过期导致归档/引导不一致。
    workspace_root = resolve_workspace_root(project_root)
    ideas_path = workspace_root / "IDEAS.md"
    if not ideas_path.exists():
        return {"archived": [], "kept": 0, "skipped": "no_ideas_file"}

    # 读取任务终态（best-effort：ledger 不可用则无证据，不归档）
    # 注：终态快照在取锁前计算（只读，轻微陈旧最多让条目延后一轮归档），使锁窗口
    # 只覆盖「读 IDEAS.md → 写归档 + 改写 IDEAS.md」，不扩大到账本重放。
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

    # 取锁前的只读预扫描（task-archive-lock-unavailable-recovery）：计算当前可归档
    # 条目，供锁不可用时写入待归档标记（计数/留痕用，不参与实际写入）。锁内仍以
    # 重新读取的最新 IDEAS.md 为准做归档（读-改-写在锁窗口内），预扫描只读、轻微
    # 陈旧最多让标记计数延后一轮，不引入并发写。
    pre_resolved: list[dict[str, Any]] = []
    try:
        pre_entries = parse_ideas(ideas_path.read_text(encoding="utf-8"))
        pre_resolved = find_resolved_entries(master, pre_entries, task_status)
    except (OSError, IOError, UnicodeDecodeError):
        pass

    # AC2（task-intake-atomic-lock-consistent）：归档全流程纳入准入写锁。此前
    # 「读 IDEAS.md → 改主文件 + 写归档」无锁，与 amend / intake / idea
    # propose-confirm 等文档写者并发时可用旧文本覆盖对方改动（丢写）。锁被占/超时
    # 则降级为不归档（best-effort 跳过，绝不无锁并发归档），保持本函数永不抛异常。
    lock_dir = _resolve_lock_orchd_dir(project_root)
    try:
        lk = intake_lock_acquire(lock_dir, resolve_agent_id(lock_dir))
    except (OrchdError, OSError) as exc:
        # 兜底重试标记（task-archive-lock-unavailable-recovery）：锁不可用时不静默
        # 丢弃积压——写入待归档标记（时间戳 / 可归档条目数 / 原因），下次归档触发
        # 时优先重试；绝不无锁写 IDEAS.md / IDEAS-archive.md（硬约束保持）。
        _record_archive_pending(
            project_root,
            reason=(
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            ),
            pending_count=len(pre_resolved),
            pending_ids=[(e.get("id") or "") for e in pre_resolved],
        )
        return {"archived": [], "kept": 0, "skipped": "lock_unavailable"}
    try:
        try:
            text = ideas_path.read_text(encoding="utf-8")
        except (OSError, IOError, UnicodeDecodeError):
            return {"archived": [], "kept": 0, "skipped": "read_error"}

        entries = parse_ideas(text)
        resolved = find_resolved_entries(master, entries, task_status)
        if not resolved:
            # 无待归档条目：若存在上次锁不可用遗留的积压标记，说明条目已被并发/
            # 手动处理，清除标记（幂等），避免残留造成巡检误报。
            _clear_archive_pending(project_root)
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

            # 先写归档文件（成功后再改主文件，避免主文件已删而归档丢失）；两次写入
            # 均原子（tmp + os.replace，AC2）——任一步失败主文件保持原状，不留半截。
            _atomic_write_text(archive_path, archive_text)
            _atomic_write_text(ideas_path, new_ideas)
        except (OSError, IOError):
            return {"archived": [], "kept": len(entries), "skipped": "write_error"}

        # 归档成功：清除积压标记（幂等；不存在则无动作）
        _clear_archive_pending(project_root)
        return {
            "archived": [e["title"] for e in resolved],
            "kept": len(entries) - len(resolved),
        }
    finally:
        intake_lock_release(lk)


def _archive_pending_path(project_root) -> Path:
    """解析待归档标记文件路径（账本根运行时文件）。

    账本根经 ``ledger.resolve_store_dir`` 解析（ORCHD_HOME 优先 → container 布局
    ``<容器>/.orchd-runtime/`` → flat 回退 ``.orchd/``），与 ``_ledger.jsonl``
    同级；标记是运行时状态，不随工作区文档（IDEAS.md / IDEAS-archive.md）走 git，
    也不参与仓库 diff。
    """
    from orchd.ledger import resolve_store_dir

    orchd_dir = _resolve_lock_orchd_dir(project_root)
    return resolve_store_dir(orchd_dir) / _ARCHIVE_PENDING_FILE


def _record_archive_pending(
    project_root,
    *,
    reason: str,
    pending_count: int,
    pending_ids: list[str],
) -> None:
    """锁不可用时写入待归档标记（best-effort，失败静默降级）。

    标记内容：UTC 时间戳（ISO-8601）、可归档条目数、原因、待归档条目 id。
    写入原子化（tmp + os.replace，复用 intake 契约）；**不触碰** IDEAS.md /
    IDEAS-archive.md——标记只是运行时留痕，供下次归档触发时优先重试，以及
    doctor/状态命令巡检发现「长期不归档」积压。
    """
    marker = {
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pending_count": pending_count,
        "pending_ids": pending_ids,
    }
    try:
        _atomic_write_text(
            _archive_pending_path(project_root),
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, IOError, TypeError):
        pass


def _clear_archive_pending(project_root) -> None:
    """清除待归档标记（幂等：不存在即无动作；失败静默降级）。

    归档成功、或确认无待归档条目后调用——积压已被处理，残留标记会造成巡检
    误报，必须清除。
    """
    try:
        _archive_pending_path(project_root).unlink(missing_ok=True)
    except OSError:
        pass


def read_archive_pending(project_root) -> dict[str, Any] | None:
    """结构化读取待归档标记（供 doctor/状态命令巡检接线）。

    Args:
        project_root: 项目根目录。

    Returns:
        标记内容 dict（含 reason / timestamp / pending_count / pending_ids）；
        标记不存在或文件损坏 / 读取失败时返回 None（安全降级，永不抛异常）。
    """
    path = _archive_pending_path(project_root)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data
