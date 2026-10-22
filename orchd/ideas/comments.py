"""orchd ideas — 注释可见性域（注释跨度 / 未闭合记录 / HTML 注释行），叶子域（W6-C4 拆分，纯搬移）。

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



_COMMENT_SPAN_RE = _re.compile(r"<!--.*?-->", _re.DOTALL)

_COMMENT_OPEN_RE = _re.compile(r"<!--")



def _block_comment_balanced(block: str) -> bool:
    """块内的 HTML 注释标记是否自配（跨块边界的单侧标记 = 不自配）。

    搬块安全的前提（code 审查 BLOCKER 1 实测）：``parse_ideas`` 把条目的 ``end_line``
    定在**下一个 ``##`` 行**，于是"上一条目正文之后、下一条目标题之前"的独立 ``<!--``
    行属于**上一条目的块**。整块搬走它时，那半个 opener 一起进了归档件 ⇒ 主文件留下无
    opener 的 ``-->``，用户亲手封存的模板块当场解禁，下一次触发就把封存内容也搬走
    （实测分支上主文件塌成仅剩 ``# IDEAS``）；归档件那侧同理多出一个无 closer 的
    opener，尾部渲染成注释。判"自配"而非"含注释"：块内成对注释（``<!-- x -->``）照搬
    无损，反向对照必须放行，否则守卫退化成永远拒。
    """
    pos = 0
    while True:
        open_at = block.find("<!--", pos)
        closer = block.find("-->", pos)
        if open_at < 0:
            return closer < 0  # 无 opener：不许有别人的收尾（游离 -->）
        if 0 <= closer < open_at:
            return False  # closer 先于 opener：本块带着上一块的收尾
        close_at = block.find("-->", open_at + 4)
        if close_at < 0:
            return False  # opener 无 closer：封存范围被整块搬走
        pos = close_at + 3



def _unclosed_open_at(text: str) -> int | None:
    """未闭合 ``<!--`` 的字符偏移；无则 ``None``（**单一真源**：过滤器与诊断共用）。"""
    for m in _COMMENT_OPEN_RE.finditer(text):
        if "-->" not in text[m.end():]:
            return m.start()
    return None



def unclosed_comment_line(text: str) -> int | None:
    """未闭合 ``<!--`` 的行号（1 基）；无则 ``None``。

    诊断面用（task-idea-archive-diagnostics）：未闭合 opener 是**文件级**异常，
    此前它之后的所有条目既不放行也不诊断（"数字没变、也没人说为什么"）。把行号
    点名出来，用户才知道该去改哪一行；行号由与过滤器同一份偏移判定派生。
    """
    at = _unclosed_open_at(text)
    if at is None:
        return None
    return text.count("\n", 0, at) + 1



def comment_visibility(text: str) -> tuple[set[int], set[int]]:
    """注释可见性的**单一真源**（task-idea-archive-diagnostics）：返回两组 0 基行号。

    - ``closed``：``<!-- ... -->`` **成对**区间覆盖的标题行（格式示例/封存区）；
    - ``frozen``：未闭合 ``<!--`` 之后（含 opener 行）被罩住的标题行。

    两个通道同源消费它：归档判据要两者并集（:func:`_html_comment_lines`）做过滤；
    ``ideas list`` 用 ``closed`` 剔格式示例、用 ``frozen`` 给"被注释罩住、因此永不
    归档"的条目打 ``comment_hidden`` 标。此前 list 走 :func:`_strip_idea_html_comments`
    （只剔闭合区间），归档判据按"未闭合延伸到文件末尾"过滤，同一份 IDEAS.md 于是
    有两种条目可见性——用户看到 list 里明明有这条、归档却当它不存在，且零诊断。

    判定按**标题行首个非空白字符的位置**（与过滤器同口径）：``## 标题 <!-- 尾注 -->``
    这类真条目不算被罩住。
    """
    spans = [(m.start(), m.end()) for m in _COMMENT_SPAN_RE.finditer(text)]
    unclosed_at = _unclosed_open_at(text)
    closed: set[int] = set()
    frozen: set[int] = set()
    if unclosed_at is None and not spans:
        return closed, frozen
    idx = 0
    acc = 0
    for i, line in enumerate(text.splitlines(keepends=True)):
        while idx < len(spans) and spans[idx][1] <= acc:
            idx += 1
        pos = acc + (len(line) - len(line.lstrip()))
        if idx < len(spans) and spans[idx][0] <= pos < spans[idx][1]:
            closed.add(i)
        elif unclosed_at is not None and pos >= unclosed_at:
            frozen.add(i)
        acc += len(line)
    return closed, frozen



def mask_closed_comment_spans(text: str) -> str:
    """把**成对** HTML 注释区间替换成等长空白（换行保留），行号与原文对齐。

    两个用途（task-idea-archive-diagnostics）：
    - 取字段值：注释区间内不是条目正文，标题/``- id:`` 取值必须与既有口径一致；
    - **保住行号**：只有等长替换才能让 ``start_line`` 与原文一致，从而归档侧（在原文上
      算偏移）与 list 侧的可见性判定谈的是同一组行号。旧实现
      :func:`orchd.cli.commands.ideas._strip_idea_html_comments` 是**删字符**（``re.sub``
      直接删掉区间），同行内联注释会污染 title，注释包住的标题行还会因错位被误当条目。
    """
    def _blank(match: Any) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    return _COMMENT_SPAN_RE.sub(_blank, text)



def _html_comment_lines(text: str) -> set[int]:
    """标题行首个非空白字符落在 HTML 注释内的行号集合（0 基）。

    摄入协议第 0 步前置过滤：只有注释**之外**的 ``##`` 章节才算条目。本通道的 dropped
    分支按 ``- status`` 的**字面值**选块，而 ``parse_ideas`` 不认注释——IDEAS.md 头部的
    格式示例块里就写着 ``- status:`` 与 ``- id:`` 两行模板，示例块一旦被填上 dropped
    （模板存在的意义就是让人复制改写，示例此刻写的是哪个状态词完全不可依赖），不过滤
    就会把模板当真实裁决整块搬走、文件头当场被拆。判定按**标题位置**在注释内还是外，
    不按"该行出现过 ``<!--``"——后者会把 ``## 标题 <!-- 尾注 -->`` 这种真条目永久留在
    场内且零诊断（spec 审查实测）。

    未闭合的 ``<!--`` 单独处理：区间正则匹不上就等于零区间、过滤整体失明，而"半截注释"
    恰恰是手改 IDEAS.md 最可能的坏形态（code 审查实测：未闭合时模板块被整块搬走、主文件
    留下 ``# IDEAS`` + ``<!--`` 的残头）。故把该 opener 视为延伸到文件末尾——宁可此后
    一条都不归档，不可拆头。方向单一：本函数只会少选，不会多选。

    实现由 :func:`comment_visibility` 派生（**单源**）：本函数是那两个集合的并集，
    不再自持一份遍历——否则 list 与归档判据会各有一份"注释罩住哪些行"的实现，
    改一处、另一处静默漂移（spec 审查 MINOR-1）。
    """
    closed, frozen = comment_visibility(text)
    return closed | frozen
