"""cli 通用工具聚合入口（W6-C8 从单文件 `orchd/cli/_util.py` 拆为三域）。

**本入口不持有任何实现**：只重导出三域的全部 19 个顶层符号，维持
`orchd.cli._util.<名>` 与 `from orchd.cli._util import <名>` 两条既有导入路径可解析
（`cli/commands/*`、`cli/parser.py`、`cli/guidance.py` 等大量消费者依赖它）。

域划分（`paths` 为叶子，`archive` → `fallback` + `paths`）：

- :mod:`.paths` —— cwd 纪律护栏（E036）、命令名反推、`--xxx-file` 参数、master / tasks 加载
- :mod:`.archive` —— IDEAS 归档主流程（终态后触发、master-archive 前置止血）
- :mod:`.fallback` —— 引擎源码目录被回收后的子进程归档兜底

旧文件因模块级 import 而暴露、但有意不再提供的绑定名（`os` / `subprocess` / `sys` /
`Path` / `SimpleNamespace` / `Any` / `ErrorCode` / `OrchdError`）不重导出——纯 import
绑定，非本包实现（`ErrorCode` / `OrchdError` 由 `orchd.errors` 承载，消费者直连该模块）。
"""

from __future__ import annotations

from .archive import (
    _archivable_ideas_count,
    _commit_archived_ideas,
    _load_master_with_archived,
    _maybe_archive_ideas,
    _unusable,
    archive_ideas_before_master_archive,
)
from .fallback import (
    _archive_error_result,
    _archive_ideas_via_subprocess,
    _forward_archive_payload,
    _orchd_source_recycled,
    _preimport_archive_deps,
)
from .paths import (
    _ORCHD_ALLOW_CONTAINER_ROOT,
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _load_tasks,
    _reject_container_root_cwd,
    _resolve_canonical_orchd_dir,
    _resolve_text_arg,
)

__all__ = [
    "_ORCHD_ALLOW_CONTAINER_ROOT",
    "_archive_error_result",
    "_archive_ideas_via_subprocess",
    "_archivable_ideas_count",
    "_command_name",
    "_commit_archived_ideas",
    "_find_orchd_dir",
    "_flatten_nargs",
    "_forward_archive_payload",
    "_load_master_with_archived",
    "_load_tasks",
    "_maybe_archive_ideas",
    "_orchd_source_recycled",
    "_preimport_archive_deps",
    "_reject_container_root_cwd",
    "_resolve_canonical_orchd_dir",
    "_resolve_text_arg",
    "_unusable",
    "archive_ideas_before_master_archive",
]
