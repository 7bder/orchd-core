"""orchd/ideas/ — 聚合入口（W6-C4：退化为纯重导出）。

来源：``orchd/ideas.py``（1211 行，W6-C4 纯搬移）。

仅重导出 35 个顶层符号（外部导入取到的仍是子模块同一对象）。
"""

from .parse import (
    _TERMINAL,
    parse_ideas,
    extract_entry_block,
    find_resolved_entries,
    _group_ref_tasks,
    find_live_source_refs,
    _entry_is_resolved,
)
from .comments import (
    _COMMENT_SPAN_RE,
    _COMMENT_OPEN_RE,
    _block_comment_balanced,
    _unclosed_open_at,
    unclosed_comment_line,
    comment_visibility,
    mask_closed_comment_spans,
    _html_comment_lines,
)
from .classify import (
    _classify_archivable,
    _select_archivable,
    find_archivable_entries,
    find_deferred_unbalanced_entries,
    find_comment_hidden_entries,
    find_orphan_entries,
)
from .blocks import (
    _remove_blocks,
    _insert_decision_line,
    _strip_decision_lines,
    _canonical_block,
    _archive_has_identical_block,
)
from .archive import (
    _ARCHIVE_PENDING_FILE,
    _ARCHIVE_HEADER,
    archive_resolved_ideas,
    _archive_pending_path,
    _record_archive_pending,
    _clear_archive_pending,
    read_archive_pending,
)
from .decision import (
    _DECISION_ARCHIVABLE_STATUSES,
    idea_archive_decision,
)

__all__ = [
    "_TERMINAL",
    "_ARCHIVE_PENDING_FILE",
    "_ARCHIVE_HEADER",
    "parse_ideas",
    "extract_entry_block",
    "find_resolved_entries",
    "_group_ref_tasks",
    "find_live_source_refs",
    "_COMMENT_SPAN_RE",
    "_COMMENT_OPEN_RE",
    "_block_comment_balanced",
    "_unclosed_open_at",
    "unclosed_comment_line",
    "comment_visibility",
    "mask_closed_comment_spans",
    "_html_comment_lines",
    "_classify_archivable",
    "_select_archivable",
    "find_archivable_entries",
    "find_deferred_unbalanced_entries",
    "find_comment_hidden_entries",
    "_entry_is_resolved",
    "find_orphan_entries",
    "_remove_blocks",
    "archive_resolved_ideas",
    "_DECISION_ARCHIVABLE_STATUSES",
    "_insert_decision_line",
    "_strip_decision_lines",
    "_canonical_block",
    "_archive_has_identical_block",
    "idea_archive_decision",
    "_archive_pending_path",
    "_record_archive_pending",
    "_clear_archive_pending",
    "read_archive_pending",
]
