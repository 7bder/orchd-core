"""orchd ideas — 决策行域（idea_archive_decision 主流程）（W6-C4 拆分，纯搬移）。

来源：``orchd/ideas.py``（1211 行，W6-C4 纯搬移）。
"""

from __future__ import annotations

import json
import re as _re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.names import (
    IDEAS_ARCHIVE_FILE,
    IDEAS_FILE,
)
from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import OrchdError
from orchd.intake import (
    IDEA_STATUS_VALUES,
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
)

from .comments import (
    _block_comment_balanced,
)

from .blocks import (
    _archive_has_identical_block,
    _canonical_block,
    _insert_decision_line,
    _remove_blocks,
)

from .archive import (
    _ARCHIVE_HEADER,
)




# 用户裁决可归档的状态（idea-archive-decision-channel）。归档通道只认「有无任务把
# source 指向我」，总纲类条目天然不直接产生任务 ⇒ 走不到终态判据，只能由人裁决离场。
# covered 在列：撞车并入的条目无任务 source 指向它，走不到终态判据，须由人裁决离场。
# study 在列（task-intake-exit-verb-close）：未拍板的灵感不再降级为 dropped 留在台账，
#   而是由 idea close --reason 一次离场归档——「放弃」由此成为带理由的裁决而非状态值。
# dropped 已整体下线：自动归档判据收敛为 resolved-only，本通道也不再接受该状态。
# Wave1-T2：可归档面与合法词表当前同集合，故直接引用单一真源（同一 frozenset 对象），
# 不再在本模块复制第二套枚举；若将来两者分化，在此显式收窄并留注释。
_DECISION_ARCHIVABLE_STATUSES = IDEA_STATUS_VALUES



def idea_archive_decision(
    project_root, title: str, reason: str,
    live_refs: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """把一条 IDEAS 条目按用户裁决移入 IDEAS-archive.md（idea close 后端）。

    与 confirm 同族（drop 通道已下线）：共用 ``intake._intake_guard`` 前置与同一把 ``.intake.lock``
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
          / not_archivable_status / source_live_reference /
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
                "idea close 必须给出 --reason：裁决离场是无痕搬块与可审留痕的分界，"
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
    ideas_path = ws / IDEAS_FILE
    archive_path = ws / IDEAS_ARCHIVE_FILE

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
        # study 不再被拒（task-intake-exit-verb-close）：close 一律走归档，未拍板的
        # 灵感同样以「带理由的裁决」离场，不再先降级为 dropped 等机器搬。
        if status not in _DECISION_ARCHIVABLE_STATUSES:
            return {
                "archived": False,
                "reason": "not_archivable_status",
                "title": title,
                "current_status": status,
                "hint": (
                    f"仅 status ∈ {sorted(_DECISION_ARCHIVABLE_STATUSES)} 的条目可 close 归档"
                    f"（当前 '{status}'）；活跃台账已不存在 dropped 状态"
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
