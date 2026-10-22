"""orchd ideas — 可归档分类域（可归档 / 延迟未平衡 / 注释隐藏 / 孤儿）（W6-C4 拆分，纯搬移）。

来源：``orchd/ideas.py``（1211 行，W6-C4 纯搬移）。
"""

from __future__ import annotations

import json
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
from .parse import (
    extract_entry_block,
    find_live_source_refs,
    find_resolved_entries,
)

from .comments import (
    _block_comment_balanced,
    _html_comment_lines,
)




def _classify_archivable(
    master,
    entries: list[dict[str, Any]],
    task_status: dict[str, str] | None = None,
    *,
    text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """判据核：返回 (可搬走的块, 注释配对不变量挡下的候选, 注释区间内被冻结的候选)。

    两条分支共用一个核，``find_archivable_entries`` 与
    :func:`find_deferred_unbalanced_entries` 只是取它的第 0 / 第 1 个返回值——
    诊断面若另写一遍判据，就会和放行面漂移（本卡反复犯过的错，故结构上锁死）。

    ``text`` 为关键字必传：注释过滤要用原文算行偏移，漏传等价于静默关掉那道网。给默认
    值就是给"少传一个参数"留一条无声退路（spec 审查 C2）。

    两条判据互补，缺一即有状态永不出场：

    - **resolved**：条目 ``- id`` 被任务 ``source``/``additional_sources`` 引用，且
      这些任务全部终态（:func:`find_resolved_entries`，语义与用例零改动，只被复用）；
    - **dropped**：条目 ``- status: dropped`` 且其 id 未被任何**非终态**任务引用。
      用户 ``idea drop`` 已经是裁决结论，但放弃的灵感通常从未被任务化 ⇒ 走不到"全
      终态"判据；``_DECISION_ARCHIVABLE_STATUSES`` 也刻意不收 dropped（放弃条目的
      离场不需要人再点一次按钮）。于是 dropped 两头无出口，每轮 ``ideas list`` 复读。

    在途守护与人工裁决通道同口径（见 :func:`find_live_source_refs`）：命中非终态引用
    即不搬。被非终态任务引用的 dropped 条目本来就已 E025 判红（``_check_idea_reference``
    要求条目 status 为 pending），所以这道网保的不是"一个合法态"，而是两件事：块被销毁
    后诊断退化成「无匹配条目」、在途任务台账失去可追溯的来源文本。无 ``- id:`` 的 dropped
    条目可搬：E025 只认 ``- id:`` 精确相等，无 id 条目不可能被合法引用，搬走不伤任何任务；
    按"无 id 保守"处理反而重新造出一个滞留桶。

    两判据同时命中只保留一次；候选再按 :func:`_block_comment_balanced` 分岔——块边界
    跨注释标记的一律**不搬**（宁可滞留，不可腐蚀两份文件）。返回三列均按
    ``start_line`` 升序，与 :func:`_remove_blocks` 的行区间删除口径一致。

    第三列（task-idea-archive-diagnostics）：本该离场、却被 HTML 注释区间罩住的候选。
    它们此前**既不放行也不诊断**——``ideas list`` 照旧把条目列出来，归档判据按"未闭合
    注释延伸到文件末尾"把它们藏掉，用户看到的是"数字没变，也没人说为什么"。现在与放行
    面同核派生（同一份 ``hidden``、同一份 eligible），只多一列出口；诊断入口是
    :func:`find_comment_hidden_entries`，行号由 :func:`unclosed_comment_line` 从同一份
    偏移判定派生。
    """
    task_status = task_status or {}
    # 注释过滤罩住两条分支（code 审查 S1）：第 0 步"只有注释之外的 ## 章节才算条目"是
    # 全局口径，只罩新增的 dropped 半边就留下"同一份文本两把尺子"——注释里的 ``- id:``
    # 命中终态任务时照样把模板块搬走（实测 main 亦如此，属既有面，本卡一并收敛）。
    hidden = _html_comment_lines(text)
    eligible: dict[int, dict[str, Any]] = {
        e["start_line"]: e
        for e in find_resolved_entries(master, entries, task_status)
    }
    live = find_live_source_refs(master, task_status)
    for e in entries:
        start = e["start_line"]
        if start in eligible:
            continue
        if (e.get("status") or "").strip() != "dropped":
            continue
        eid = (e.get("id") or "").strip()
        if eid and live.get(eid):
            continue
        eligible[start] = e
    moved: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    frozen: list[dict[str, Any]] = []
    for e in (eligible[k] for k in sorted(eligible)):
        if e["start_line"] in hidden:
            frozen.append(e)
            continue
        if _block_comment_balanced(extract_entry_block(text, e)):
            moved.append(e)
        else:
            deferred.append(e)
    return moved, deferred, frozen



def _select_archivable(
    master,
    entries: list[dict[str, Any]],
    task_status: dict[str, str] | None = None,
    *,
    text: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """取判据核的前两列（放行 / 注释配对不变量挡下）——既有调用方口径零变更。"""
    moved, deferred, _frozen = _classify_archivable(
        master, entries, task_status, text=text
    )
    return moved, deferred



def find_archivable_entries(
    master,
    entries: list[dict[str, Any]],
    task_status: dict[str, str] | None = None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    """自动归档通道的唯一判据入口：已完结 ∪ 已放弃，且块可安全搬走（ideas-dropped-exit）。

    判据全文见 :func:`_select_archivable`（resolved ∪ dropped、注释过滤、在途守护、
    注释配对不变量）；本函数取其中"放行"的那一列。
    """
    return _select_archivable(master, entries, task_status, text=text)[0]



def find_deferred_unbalanced_entries(
    master,
    entries: list[dict[str, Any]],
    task_status: dict[str, str] | None = None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    """本该离场、却因块边界跨注释标记而被挡下的条目（诊断面）。

    与 :func:`find_archivable_entries` 同一个核，只是取第 1 个返回值：放行与挡下不可能
    各说各话。挡下必须可见——静默少搬等于把"这条为什么还在"重新变成人肉排查。
    """
    return _select_archivable(master, entries, task_status, text=text)[1]



def find_comment_hidden_entries(
    master,
    entries: list[dict[str, Any]],
    task_status: dict[str, str] | None = None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    """本该离场、却被 HTML 注释区间冻结的条目（第三列诊断）。

    与 :func:`find_archivable_entries` / :func:`find_deferred_unbalanced_entries`
    同一个核的第三个返回值：冻结与放行不可能各说各话。冻结必须可见——未闭合 ``<!--``
    是**文件级**异常（其后所有条目都被罩住），静默冻结等于把"这些条目为什么永不归档"
    重新变回人肉排查（task-idea-archive-diagnostics）。
    """
    return _classify_archivable(master, entries, task_status, text=text)[2]



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
