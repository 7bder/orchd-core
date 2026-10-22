"""orchd/report/ — 报表包聚合入口（W6-C3：退化为纯重导出）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

本入口**不再持有任何实现**：仅重导出 ``acks`` / ``status`` / ``audit`` / ``checks`` /
``phase`` / ``watchdog`` / ``text`` 七域的 24 个顶层符号；外部
``from orchd.report import <名>`` 取到的仍是子模块同一对象。
"""

from .acks import (
    _MERGE_ACKS_FILENAME,
    merge_acks_path,
    load_merge_acks,
    merge_ack,
)
from .audit import (
    merge_audit,
    _changelog_landing_gaps,
    _is_ancestor_of_main,
)
from .checks import (
    task_integrity_audit,
    _scan_revive_markers,
    task_revive_markers,
    revive_audit,
    intake_audit,
)
from .phase import (
    _DIAGNOSTICS_TOP_N,
    _aggregate_top,
    _task_phase_status,
    phase_aggregate,
)
from .status import (
    status,
)
from .text import (
    _format_phase_text,
    _format_text,
    _auto_clean_emit_stderr,
    _format_auto_clean_text,
)
from .watchdog import (
    _auto_clean_disposition_summary,
    _stale_claim_dual_action,
    watchdog,
)

__all__ = [
    "_MERGE_ACKS_FILENAME",
    "merge_acks_path",
    "load_merge_acks",
    "merge_ack",
    "status",
    "merge_audit",
    "_changelog_landing_gaps",
    "_is_ancestor_of_main",
    "task_integrity_audit",
    "_scan_revive_markers",
    "task_revive_markers",
    "revive_audit",
    "intake_audit",
    "_DIAGNOSTICS_TOP_N",
    "_aggregate_top",
    "_task_phase_status",
    "phase_aggregate",
    "_format_phase_text",
    "_auto_clean_disposition_summary",
    "_stale_claim_dual_action",
    "watchdog",
    "_format_text",
    "_auto_clean_emit_stderr",
    "_format_auto_clean_text",
]
