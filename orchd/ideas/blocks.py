"""orchd ideas — 块与决策行操作域（W6-C4 拆分，纯搬移）。

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
from .parse import (
    extract_entry_block,
    parse_ideas,
)




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



def _insert_decision_line(block: str, reason: str, ts: str) -> str:
    """在条目块内最后一个非空行之后插入归档裁决行。

    块尾通常留空行分隔下一条目，直接追加会让裁决行落到空行之后、脱离条目本体。

    ``reason`` 经 :func:`orchd.ledger.single_line` 归一再写入（task-idea-archive-hardening）：
    行式台账里"前缀 + 自由文本拼一行"，多行 reason 能伪造出假 ``##`` 小节头，并让按
    ``- id:`` 的判重读到空 id（实测探针：归档件 ``##`` 头 1→2）。归一放在写入点，
    调用方无需记得检查；``idea_archive_decision`` 返回的 ``decision`` 字段同源，
    保证"写进台账的字节"与"报给用户的字节"一致。
    """
    lines = block.splitlines(keepends=True)
    idx = len(lines)
    for k in range(len(lines) - 1, -1, -1):
        if lines[k].strip():
            idx = k + 1
            break
    if idx and not lines[idx - 1].endswith("\n"):
        lines[idx - 1] += "\n"
    return (
        "".join(lines[:idx])
        + f"- 归档裁决: {single_line(reason)}@{ts}\n"
        + "".join(lines[idx:])
    )



def _strip_decision_lines(block: str) -> str:
    """剥掉块内的归档裁决行，只留条目正文（用于比较是否同一份内容）。"""
    return "".join(
        ln for ln in block.splitlines(keepends=True)
        if not ln.strip().startswith("- 归档裁决:")
    )



def _canonical_block(block: str) -> str:
    """块 canonical 形式：剥裁决行 + 统一以 ``\\n`` 结尾（判重键与两通道落档共用）。

    单源理由（task-idea-archive-hardening）：判重键此前比「剥裁决行后的正文」字节，
    而尾换行由写方各自决定——自动通道落档前补 ``\\n``、人工裁决通道原样搬；于是
    「IDEAS.md 末块且文件不以换行收尾」这一形态下，同一份正文在两通道写出的字节不同、
    :func:`_archive_has_identical_block` 判不中，半写重跑就在归档里留下第二份同 id
    同正文块（idea ``archive-dedup-blank-id-gap``）。正文一字未改，统一的只是分隔符。
    """
    body = _strip_decision_lines(block)
    if not body.endswith("\n"):
        body += "\n"
    return body



def _archive_has_identical_block(
    archive_text: str, entry_id: str, src_block: str
) -> bool:
    """归档里是否已有与源块**同 id 且正文相同**的块（半写收敛判据）。

    判重不能只看标题或 id 是否存在：同 id 异正文的块在归档里本就可能存在
    （``idea_propose`` 的查重只扫 IDEAS.md），若据此跳过追加，主文件那一块仍会被删
    ⇒ 未归档的正文当场销毁。判据 = 两边的 :func:`_canonical_block` 逐字节相同。

    条目 ``- id:`` 为空时改走**正文 content-only** 匹配：``find_archivable_entries``
    明确允许无 id 的 dropped 条目离场（E025 只认 id 精确相等，无 id 条目不可能被合法
    引用），而见空 id 就 ``return False`` 会让这类条目的半写重跑在归档里留下两份同正文
    块——正是本判据要防的双块态。
    """
    if not archive_text:
        return False
    src = _canonical_block(src_block)
    eid = (entry_id or "").strip()
    for e in parse_ideas(archive_text):
        if eid and (e.get("id") or "") != eid:
            continue
        if _canonical_block(extract_entry_block(archive_text, e)) == src:
            return True
    return False
