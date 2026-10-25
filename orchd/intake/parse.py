"""intake.parse —— 自 ``orchd/intake.py`` 拆出。

IDEAS 文本解析与原子写（_atomic_write_text / _parse_* / _find_idea_entry）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

def _atomic_write_text(path: Path, text: str) -> None:
    """P2-12：原子写（tmp + os.replace），避免读-改-写中途崩溃产生半截 IDEAS.md。

    耐久档（task-os-replace-hardening-io-batch 批次 1b）：tmp 写入 → flush +
    fsync → ``ledger._atomic_replace``（WinError 5/32 有界重试，耗尽抛结构化
    E007，前缀 ``atomic_replace_failed``）→ ``_fsync_dir(父目录)``。IDEAS.md
    是唯一摄入账面，断电零长即丢全部条目，故与账本同档。tmp 形态
    （``<name>.tmp``，无点前缀）与文本形态（调用方给定，平台默认换行翻译）
    原样保留。惰性导入沿用本模块「零 orchd 内部依赖」的叶子约定。
    """
    from orchd.ledger import _atomic_replace, _fsync_dir

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    _atomic_replace(tmp, path)
    _fsync_dir(path.parent)


# 灵感池状态词表**唯一真源**（single-source-truth-convergence Wave1-T2）：
# 值即 IDEAS.md 条目 ``- status:`` 的合法取值；消费方（ideas/classify、
# ideas/decision、intake/idea）一律引用下面的常量，不得再写第二套枚举或裸字面量。
# 注意：本词表与任务状态词表（contracts/events.py 的 STATUS_*）**刻意不合并**——
# 灵感四态与任务六态语义不同、流转不同，只是字形有重叠。
# study（论证中，2026-08-15 idea-write-gate）：idea propose 写入的初始态，待用户 confirm
#   升 pending 或 close 归档（task-intake-exit-verb-close 起 dropped 状态已下线：
#   「放弃」不再是一个留在台账里的状态值，而是由 idea close --reason 一次离场并留痕）。
# covered（撞车并入，task-intake-status-vocab-simplify）：本条诉求已被既有任务覆盖，
#   不再单独注册、待归档，notes 记 task id；旧名（已淘汰）名实不符，并不表示"已注册"。
IDEA_STATUS_PENDING = "pending"
IDEA_STATUS_QUESTIONING = "questioning"
IDEA_STATUS_COVERED = "covered"
IDEA_STATUS_STUDY = "study"
#: 合法状态集合（唯一真源；成员即上面四个常量，``check_idea_statuses`` 的 allowed 面）。
IDEA_STATUS_VALUES = frozenset({
    IDEA_STATUS_PENDING,
    IDEA_STATUS_QUESTIONING,
    IDEA_STATUS_COVERED,
    IDEA_STATUS_STUDY,
})
#: 历史私有名，兼容别名（与 ``IDEA_STATUS_VALUES`` 同一对象；包内与聚合入口沿用）。
_VALID_IDEA_STATUSES = IDEA_STATUS_VALUES


# 条目标题中的显式 id 约定写法：`<标题>（id: <slug>）`
# slug 仅允许字母、数字、连字符、下划线，且以字母或数字开头（与 source: idea:<id>
# 的引用形态一致；中文标题无法可靠派生 ASCII slug，故 id 必须显式声明）。
_TITLE_ID_RE = re.compile(r"id:\s*([A-Za-z0-9][A-Za-z0-9_-]*)")


def _parse_title_id(title: str) -> str | None:
    """从灵感标题中解析显式 id（约定写法 ``...（id: <slug>）``）。

    ``- id:`` 是 E025 溯源校验（``spec._check_idea_reference``）与自动归档
    （``ideas._entry_is_resolved``）的权威锚点：缺 id 的条目既无法通过 source
    引用校验，也永不自动归档。写入侧必须产出该字段，否则与读取侧契约断裂。

    Returns:
        解析到的 slug；标题未声明 id 时返回 None（调用方应 fail-closed 拒绝）。
    """
    m = _TITLE_ID_RE.search(title or "")
    return m.group(1) if m else None


def _parse_idea_statuses(text: str) -> list[tuple[int, str, str]]:
    """解析 IDEAS.md 各条目的 status 字段，返回 [(行号, 标题, status)]。

    条目识别：``## `` 开头的行为标题；其后、下一个 ``## `` 之前行中匹配
    ``- status: <值>`` 或 ``status: <值>``（对齐 spec._check_idea_reference 解析）。
    """
    entries: list[tuple[int, str, str]] = []
    current_title = ""
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_title = stripped[3:].strip()
        elif current_title:
            for marker in ("- status:", "status:"):
                if stripped.startswith(marker):
                    entries.append((i, current_title, stripped[len(marker):].strip()))
                    break
    return entries


def check_idea_statuses(workspace_root: Path) -> list[dict[str, Any]]:
    """校验 IDEAS.md 条目 status 合法性，返回违规清单（空 = 合法）。

    warning 级（不阻断提交）：非法 status 说明整理/状态流转有误，须人工核对。
    """
    ideas_path = workspace_root / "IDEAS.md"
    if not ideas_path.exists():
        return []
    try:
        text = ideas_path.read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return []
    violations: list[dict[str, Any]] = []
    for lineno, title, status in _parse_idea_statuses(text):
        if status not in _VALID_IDEA_STATUSES:
            violations.append({
                "line": lineno + 1,
                "title": title,
                "status": status,
                "allowed": sorted(_VALID_IDEA_STATUSES),
            })
    return violations


def _find_idea_entry(text: str, title: str) -> dict[str, Any] | None:
    """定位 IDEAS.md 中标题匹配 ``title`` 的条目（idea-write-gate）。

    条目识别：``## `` 开头行为标题（约定格式 ``## YYYY-MM-DD <标题>``，日期为
    条目内自带的自然日期前缀）；``title`` 匹配标题**去除日期前缀后的部分**
    （整标题 == title，或以 ``" " + title`` 结尾），与 propose 写入的
    ``## {date} {title}`` 格式对齐。其后、下一个 ``## `` 之前的行收集字段
    （status / 其余 ``- key: value`` 或 ``key: value``）。

    Returns:
        ``{"header_line": int, "title": str, "status": str, "lines": [str], "end": int}``：
        header_line 为标题行号（0 基），lines 为条目全部行（含标题），end 为条目结束行号
        （不含，下一标题行号或文件末尾）。无匹配返回 None。
    """
    lines = text.splitlines()
    target: dict[str, Any] | None = None
    # 第一优先级：精确标题匹配（整标题相等，或以 " " + title 结尾=去日期前缀）
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        header = stripped[3:].strip()
        if header == title or header.endswith(" " + title):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("## "):
                j += 1
            status = ""
            for k in range(i + 1, j):
                s = lines[k].strip()
                for marker in ("- status:", "status:"):
                    if s.startswith(marker):
                        status = s[len(marker):].strip()
                        break
            target = {
                "header_line": i,
                "title": header,
                "status": status,
                "lines": lines[i:j],
                "end": j,
            }
            break
    # 第二优先级（兜底）：裸 slug 匹配条目的 - id: 字段
    # （精确标题优先，不改变既有命中语义；仅当精确匹配未命中时才走兜底）
    if target is None:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("## "):
                continue
            header = stripped[3:].strip()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("## "):
                j += 1
            entry_id = ""
            status = ""
            for k in range(i + 1, j):
                s = lines[k].strip()
                if s.startswith("- id:"):
                    entry_id = s[len("- id:"):].strip()
                for marker in ("- status:", "status:"):
                    if s.startswith(marker):
                        status = s[len(marker):].strip()
                        break
            if entry_id == title:
                target = {
                    "header_line": i,
                    "title": header,
                    "status": status,
                    "lines": lines[i:j],
                    "end": j,
                }
                break
    return target


def _list_idea_headers(text: str) -> list[str]:
    """提取 IDEAS.md 中所有条目标题（去日期前缀后的部分），供近似候选匹配。

    Returns:
        标题列表（每项为 ``## `` 后去除日期前缀的标题本体）。
    """
    headers: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        header = stripped[3:].strip()
        # 去除日期前缀（YYYY-MM-DD ），保留标题本体
        import re
        m = re.match(r"^\d{4}-\d{2}-\d{2}\s+", header)
        if m:
            header = header[m.end():]
        headers.append(header)
    return headers
