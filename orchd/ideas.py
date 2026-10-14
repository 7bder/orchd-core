"""IDEAS.md 自动归档引擎（叶子模块，零 orchd 内部依赖）。

职责：当某 idea 对应的全部任务进入终态（completed / cancelled）时，将该
``##`` 条目**整块原样**从 ``IDEAS.md`` 移入 ``IDEAS-archive.md``，实现
IDEAS.md 的"只增不减"膨胀优化。全程 best-effort 非阻塞，任何解析/读写
异常静默降级，不阻断调用方（对齐 merged:false / amend 自动提交哲学）。

安全约束：
- 只做内容域写操作（IDEAS.md / IDEAS-archive.md），不改事件格式、不改
  状态机分支、不改 CLI 契约语义（§9.1 停服升级边界内零改动）；
- 整块原样搬移，不压缩 notes、不重写用户正文，保留"原文可追溯"铁律；
- 搬块前判块内 HTML 注释标记是否自配（``_block_comment_balanced``）：块边界止于下一个
  ``##`` 行，跨块的半个 ``<!--`` 或 ``-->`` 随块搬走会同时拆掉主文件的封存保护和归档件
  的注释配对，故不自配的块一律不搬，两条例外通道（自动 / 人工裁决）同受此约束；
- ``source: (idea|roadmap):<ref>`` 是自动归档的映射依据；无 source 的存量条目
  无法自动映射，由 ``ideas-archive`` 手动命令一次性回填。两条例外同样按机器判：
  ``status: dropped`` 的条目按"放弃即离场"搬走（被非终态任务引用的除外），
  其余无 ref 条目留在 IDEAS.md 等人工裁决（``idea archive``）。

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

# 待归档标记文件名（账本根运行时文件；task-archive-lock-unavailable-recovery：
# 锁不可用时的兜底留痕，与 _ledger.jsonl 同级，不随工作区文档走 git）
_ARCHIVE_PENDING_FILE = "archive-pending.json"

# 归档文件头注释（新建时写入）
_ARCHIVE_HEADER = (
    "# IDEAS Archive\n\n"
    "本文件由 orchd 引擎自动维护：已完结 idea 条目（对应任务全部进入终态）与用户已"
    "裁决放弃的条目（``- status: dropped``，仍被非终态任务引用的除外）从 IDEAS.md "
    "自动移入此处，保留原文与审计可追溯性。勿手动编辑。\n"
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
    """主入口：把已完结 / 已放弃的 idea 条目从 IDEAS.md 移入 IDEAS-archive.md。

    判据单一入口 :func:`find_archivable_entries`（resolved ∪ dropped，且块边界不得跨
    注释标记），锁外预扫描与锁内判定两处都走它——一处新一处旧会让 lock_unavailable
    待归档标记的计数与实际搬走的块不一致。被配对不变量挡下的候选经
    :func:`find_deferred_unbalanced_entries`（同一个核的另一列）随结果返回，不静默少搬。

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
        - ``{"archived": [...], "kept": n, "deduped": m}`` 同上，但有 m 块因归档件里
          已有同 id 同正文块而跳写（键仅在此时出现，无跳写时形态与既有一致）。
        - 两形态下都可能出现 ``"deferred_comment_unbalanced": [...]``（标题列表）：
          命中离场判据、但块内注释标记不自配因而**没搬**的条目；键仅在非空时出现。
        - 同样两形态下可能出现 ``"comment_hidden_candidate": [...]``：每项为
          ``{"title", "line", "hidden_by"}``（line 为 1 基原文行号；hidden_by 取
          ``unclosed_comment`` = 被未闭合 opener 延伸到文件末尾罩住 /
          ``closed_comment`` = 标题落在成对注释区间内），以及 ``"comment_unclosed_at":
          <行号>``（**仅当文本里确有未闭合 ``<!--``** 时出现）。命中的都是"本该离场却
          被注释区间冻结"的条目；成因逐条点明，故不与 ``ideas list`` 的可见性规则相左。
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
        pre_text = ideas_path.read_text(encoding="utf-8")
        pre_entries = parse_ideas(pre_text)
        pre_resolved = find_archivable_entries(
            master, pre_entries, task_status, text=pre_text
        )
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
        # 读盘兜底走唯一原语（task-idea-archive-hardening）：此前同一形状的 except 在本域
        # 写了两份（本处与归档件读），遗漏一处即"永不抛异常"失实——正是本卡要收的根因面。
        text, text_err = read_text_or_error(ideas_path)
        if text_err is not None:
            return {"archived": [], "kept": 0, "skipped": "read_error"}

        entries = parse_ideas(text)
        resolved = find_archivable_entries(master, entries, task_status, text=text)
        # 诊断列走同一个核（只是另一列返回值），不与放行面各写一遍判据；多算一次的
        # 代价换"挡下了什么"必须可见。
        deferred = [
            e.get("title") or ""
            for e in find_deferred_unbalanced_entries(
                master, entries, task_status, text=text
            )
        ]
        # 第三列诊断（task-idea-archive-diagnostics）：被注释区间冻结的候选 + 未闭合
        # opener 的行号。两者同源派生，故"谁在第几行开了没收尾"与"哪几条被它罩住"
        # 不可能互相矛盾。
        # 逐条带行号与**成因**（spec 审查 MAJOR-1）：候选可能来自"未闭合 opener 延伸到
        # 文件末尾"，也可能来自"标题落在成对注释区间内"——后者按 list 的同一条掩码规则
        # 本就不算条目，两条通道若都只报"冻结"就会各说各话。故逐条点明 hidden_by。
        frozen_lines = comment_visibility(text)[1]
        frozen = [
            {
                "title": e.get("title") or "",
                "line": e["start_line"] + 1,
                "hidden_by": (
                    "unclosed_comment"
                    if e["start_line"] in frozen_lines
                    else "closed_comment"
                ),
            }
            for e in find_comment_hidden_entries(
                master, entries, task_status, text=text
            )
        ]
        unclosed_at = unclosed_comment_line(text)
        if not resolved:
            # 无待归档条目：若存在上次锁不可用遗留的积压标记，说明条目已被并发/
            # 手动处理，清除标记（幂等），避免残留造成巡检误报。
            _clear_archive_pending(project_root)
            idle: dict[str, Any] = {"archived": [], "kept": len(entries)}
            if deferred:
                idle["deferred_comment_unbalanced"] = deferred
            if frozen:
                idle["comment_hidden_candidate"] = frozen
                if unclosed_at is not None:
                    idle["comment_unclosed_at"] = unclosed_at
            return idle

        blocks: list[str] = []
        for e in resolved:
            # 判重键与落档字节共用同一份 canonical 形式（task-idea-archive-hardening 起
            # 收敛到 _canonical_block 单源）：此前这里是内联补尾换行、人工裁决通道则原样
            # 搬，两通道对同一正文可能写出不同字节 ⇒ 去重判不中、归档留双块。
            blocks.append(_canonical_block(extract_entry_block(text, e)))
        new_ideas = _remove_blocks(text, entries, resolved)

        archive_path = workspace_root / "IDEAS-archive.md"
        try:
            if archive_path.exists():
                # 同上：归档件读也走唯一原语（此前这处 except 只捕 OSError/IOError，
                # 非 UTF-8 的 UnicodeDecodeError 会穿到 CLI——同族第四处）。
                archive_text, archive_err = read_text_or_error(archive_path)
                if archive_err is not None:
                    return {
                        "archived": [],
                        "kept": len(entries),
                        "skipped": "read_error",
                    }
            else:
                archive_text = _ARCHIVE_HEADER
            if archive_text and not archive_text.endswith("\n"):
                archive_text += "\n"
            # 追加前逐块过去重键（ideas-dropped-exit AC6）：判据扩到 dropped 之后，
            # 「归档件里已有这一块、主文件里还留着这一块」从不可达变成可达——半写重试
            # （归档已写成、主文件回写失败）与人工/自动两通道撞同一块都会落到这个态。
            # 无脑追加的后果是归档件里出现第二份同 id 同正文块：审计面翻倍，后续任何
            # 按 id 的判据（E031 落地、roadmap-land 判重）都会读到两份历史。
            # 跳写但仍删主文件——内容已在档，删主文件正是"离场"的本意。
            deduped = 0
            for eid, block in zip((e.get("id") or "" for e in resolved), blocks):
                if _archive_has_identical_block(archive_text, eid, block):
                    deduped += 1
                    continue
                archive_text += block

            # 先写归档文件（成功后再改主文件，避免主文件已删而归档丢失）；两次写入
            # 均原子（tmp + os.replace，AC2）——任一步失败主文件保持原状，不留半截。
            _atomic_write_text(archive_path, archive_text)
            _atomic_write_text(ideas_path, new_ideas)
        except (OSError, IOError):
            return {"archived": [], "kept": len(entries), "skipped": "write_error"}

        # 归档成功：清除积压标记（幂等；不存在则无动作）
        _clear_archive_pending(project_root)
        out: dict[str, Any] = {
            "archived": [e["title"] for e in resolved],
            "kept": len(entries) - len(resolved),
        }
        if deduped:
            # 仅在真发生跳写时附加（既有调用方断言 archived/kept/skipped 三键，
            # 无新键即零回归）：让人能看见"这一轮有几块是收敛而非新增"。
            out["deduped"] = deduped
        if deferred:
            out["deferred_comment_unbalanced"] = deferred
        if frozen:
            # 候选逐条带行号与成因；文件级未闭合行号只在确有未闭合 opener 时出现
            # （成对注释区间导致的冻结没有"未闭合行"可指，报 null 会误导消费方）。
            out["comment_hidden_candidate"] = frozen
            if unclosed_at is not None:
                out["comment_unclosed_at"] = unclosed_at
        return out
    finally:
        intake_lock_release(lk)


# 用户裁决可归档的状态（idea-archive-decision-channel）。归档通道只认「有无任务把
# source 指向我」，总纲类条目天然不直接产生任务 ⇒ 走不到终态判据，只能由人裁决离场。
# study 不在列：它是尚未拍板的灵感，出路是 idea drop，不是"已交付"。
# dropped 不在列：drop 这个动作本身就是裁决，无需再点一次——它的离场由机器判，
# 走 find_archivable_entries 的 dropped 分支（ideas-dropped-exit）。
_DECISION_ARCHIVABLE_STATUSES = frozenset({"pending", "taskified", "questioning"})


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


def idea_archive_decision(
    project_root, title: str, reason: str,
    live_refs: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """把一条 IDEAS 条目按用户裁决移入 IDEAS-archive.md（idea archive 后端）。

    与 confirm/drop 同族：共用 ``intake._intake_guard`` 前置与同一把 ``.intake.lock``
    准入写锁；搬块复用 ``extract_entry_block`` / ``_remove_blocks``，与自动通道同一套
    原语。块形态自 task-idea-archive-hardening 起**两通道共用** :func:`_canonical_block`
    （剥裁决行 + 统一尾换行）：此前本通道原样搬、自动通道落档前补换行，"IDEAS.md 末块且
    文件不以换行收尾"时同一正文写出的字节不同，``_archive_has_identical_block`` 判不中而
    留第二份；判重键现即该 canonical 形式，条目无 ``- id:`` 时改走正文匹配（idea
    ``archive-dedup-blank-id-gap`` 的两半都在此收口）。读盘（主文件与归档件）统一走
    :func:`orchd.ledger.read_text_or_error`，失败即结构化 ``read_error`` 而不出界——
    本函数 docstring 的「永不抛异常」由此成为事实。先写归档、后改主文件，任一步失败
    主文件原状。块内 ``- status:`` 不改写——正文原样搬走、裁决以追加行留痕（自动通道
    同样不改 status；归档文件的语义是"已消费"，不是某种状态值）。``--reason`` 经
    :func:`orchd.ledger.single_line` 折成单行后写入并同源回执，多行 reason 不再能伪造
    小节头或绕过去重。提交不在本函数内做。

    Args:
        live_refs: ``{idea_ref_id: [非终态任务 id]}``，由调用方经
            :func:`find_live_source_refs` 从 master + 账本算出（后端不读 master，
            免得给 ``ideas.py`` 新造 spec 依赖边）。命中即拒绝搬块。

    Returns:
        结构化结果，永不抛异常：
        - 成功: ``{"archived": True, "title", "from_status", "decision",
          "deduped", "ideas_path", "archive_path"}``
        - 拒绝: ``{"archived": False, "reason": ..., "title": ...}``，reason 取值
          missing_reason / not_on_main 等 guard 原值 / not_found / already_archived
          / use_drop_for_study / not_archivable_status / source_live_reference /
          comment_unbalanced_block / lock_unavailable / read_error / write_error
    """
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
        resolve_workspace_root,
    )

    project_root = Path(project_root)
    if not (reason or "").strip():
        return {
            "archived": False,
            "reason": "missing_reason",
            "title": title,
            "hint": (
                "idea archive 必须给出 --reason：裁决离场是无痕搬块与可审留痕的分界，"
                "理由会写进条目块内供日后追溯"
            ),
        }

    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        return {
            "archived": False,
            **{k: v for k, v in guard_err.items() if k != "committed"},
        }

    ws = resolve_workspace_root(project_root)
    ideas_path = ws / "IDEAS.md"
    archive_path = ws / "IDEAS-archive.md"

    lock_dir = _resolve_lock_orchd_dir(project_root)
    try:
        lk = intake_lock_acquire(lock_dir, resolve_agent_id(lock_dir))
    except (OrchdError, OSError) as exc:
        return {
            "archived": False,
            "reason": "lock_unavailable",
            "title": title,
            "detail": f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
            "hint": "准入写锁被占：绝不无锁搬块，请等持锁方退出后重试",
        }
    try:
        if not ideas_path.exists():
            return {"archived": False, "reason": "not_found", "title": title}
        # 主文件读走唯一兜底原语（task-idea-archive-hardening）：此前这处裸读，非 UTF-8 /
        # 目录形 IDEAS.md 会让 UnicodeDecodeError / PermissionError 穿出函数（docstring
        # 声称「永不抛异常」失实，与归档侧 read_error 兜底口径不齐）。
        text, text_err = read_text_or_error(ideas_path)
        if text_err is not None:
            return {"archived": False, "reason": "read_error", "title": title}
        entry = _find_idea_entry(text, title)
        if entry is None:
            archive_text = ""
            if archive_path.exists():
                try:
                    archive_text = archive_path.read_text(encoding="utf-8")
                except (OSError, IOError, UnicodeDecodeError):
                    archive_text = ""
            if archive_text and _find_idea_entry(archive_text, title) is not None:
                return {
                    "archived": False,
                    "reason": "already_archived",
                    "title": title,
                    "hint": "条目已在 IDEAS-archive.md，不重复搬块（幂等）",
                }
            import difflib

            return {
                "archived": False,
                "reason": "not_found",
                "title": title,
                "hint": (
                    "标题须能在 IDEAS.md 定位：可传完整标题（含「（id: <slug>）」后缀）、"
                    "去日期前缀标题，或裸 slug。查询：python .orchd/__main__.py ideas list"
                ),
                "candidates": difflib.get_close_matches(
                    title, _list_idea_headers(text), n=3, cutoff=0.3
                ),
            }

        status = entry.get("status") or ""
        if status == "study":
            return {
                "archived": False,
                "reason": "use_drop_for_study",
                "title": title,
                "current_status": status,
                "hint": "status: study 是未拍板灵感，出路是 orchd idea drop，不是归档",
            }
        if status not in _DECISION_ARCHIVABLE_STATUSES:
            return {
                "archived": False,
                "reason": "not_archivable_status",
                "title": title,
                "current_status": status,
                "hint": (
                    f"仅 status ∈ {sorted(_DECISION_ARCHIVABLE_STATUSES)} 的条目可裁决归档"
                    f"（当前 '{status}'）；study 走 idea drop"
                ),
            }

        entry_id = ""
        for ln in entry.get("lines") or []:
            s = ln.strip()
            if s.startswith("- id:") or s.startswith("id:"):
                entry_id = s.split(":", 1)[1].strip()
                break
        live = sorted((live_refs or {}).get(entry_id, [])) if entry_id else []
        if live:
            return {
                "archived": False,
                "reason": "source_live_reference",
                "title": title,
                "id": entry_id,
                "live_tasks": live,
                "hint": (
                    f"条目 {entry_id} 仍被未完成任务引用：{', '.join(live)}。"
                    "E025 溯源只读 IDEAS.md，此时搬走即在途任务当场失去来源"
                    "（rules/intake.md ①）。等它们进终态后由自动通道归档，"
                    "或先处置来源任务"
                ),
            }

        # 与自动通道共用行区间语义：_find_idea_entry 的 header_line/end 即 parse 的
        # start_line/end_line，交给 extract_entry_block / _remove_blocks 切与删。
        span = {"start_line": entry["header_line"], "end_line": entry["end"]}
        block = _canonical_block(extract_entry_block(text, span))
        # 同一谓词罩人工裁决通道（AC1 尾条）：这条通道由人点名搬某一条，块边界跨注释时
        # 后果与自动通道一样（相邻封存内容在主文件解禁、归档件尾部变注释），且没有下一轮
        # 判据来纠正它——当场拒绝比事后发现更便宜。
        if not _block_comment_balanced(block):
            return {
                "archived": False,
                "reason": "comment_unbalanced_block",
                "title": title,
                "hint": (
                    "该条目的块边界与 HTML 注释标记不对齐（块内含无配对的 <!-- 或 -->）："
                    "整块搬走会把护住相邻内容的注释一起带走，两份文件同时失去配对。"
                    "请把 <!-- --> 完整收进本条目正文内（或移到首个条目之前的文件头区域）后重试"
                ),
            }
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # 与写入点同源：报给用户的 decision 必须等于写进台账的那一行（否则「留痕」与
        # 「回执」又成两把尺子；_insert_decision_line 内部同样归一，此处喂同一输入即可）。
        decision = f"{single_line(reason)}@{ts}"
        if archive_path.exists():
            base_text, base_err = read_text_or_error(archive_path)
            if base_err is not None:
                return {"archived": False, "reason": "read_error", "title": title}
        else:
            base_text = _ARCHIVE_HEADER
        if base_text and not base_text.endswith("\n"):
            base_text += "\n"
        # 半写收敛：上一轮可能已把**同一份正文**写进归档、却在改主文件时失败，
        # 重试只补删主文件；正文不同则照常追加（宁可双块，也不得销毁未归档的内容）
        deduped = _archive_has_identical_block(base_text, entry_id, block)
        archive_new = (
            base_text
            if deduped
            else base_text + _insert_decision_line(block, reason.strip(), ts)
        )

        try:
            _atomic_write_text(archive_path, archive_new)
            _atomic_write_text(ideas_path, _remove_blocks(text, [], [span]))
        except (OSError, IOError):
            return {
                "archived": False,
                "reason": "write_error",
                "title": title,
                "hint": "归档未写成就不动主文件：IDEAS.md 保持原状，可重试",
            }

        return {
            "archived": True,
            "title": entry["title"],
            "from_status": status,
            "decision": decision,
            "deduped": deduped,
            "ideas_path": str(ideas_path),
            "archive_path": str(archive_path),
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
