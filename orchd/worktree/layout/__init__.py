"""orchd/worktree/layout/ — 聚合入口（W6-C5：退化为纯重导出）。

来源：``orchd/worktree/layout.py``（1079 行，W6-C5 纯搬移）。

仅重导出 34 个顶层符号（外部导入取到的仍是子模块同一对象）。
"""

from .marker import (
    _LAYOUT_MARKER,
    _CONTAINER_MAIN_DIR,
    _RUNTIME_DIR,
    _TRASH_DIR,
    _LAYOUT_VERSION,
    marker_path,
    read_layout,
    write_layout,
)
from .gitinfo import (
    _GIT_TIMEOUT,
    _git_toplevel,
    _git_common_dir,
    _is_git_tracked_path,
    nearest_git_root,
    find_orchd_dir_within_git_boundary,
)
from .detect import (
    _auto_detect_layout,
    _build_layout,
    detect_layout,
    detect_container_root_cwd,
)
from .paths import (
    resolve_canonical_project_root,
    resolve_master_path,
    resolve_master_path_from_dir,
    resolve_declaration_source,
    _default_master,
)
from .bootstrap import (
    bootstrap_container,
)
from .migration import (
    _JUNK_NAMES,
    _JUNK_PREFIXES,
    _TOOL_DIR_NAMES,
    _is_junk_entry,
    _LEDGER_RUNTIME_FILES,
    _move_ledger_runtime_files,
    _rollback_layout_migration,
    _intake_lock_filename,
    layout_migrate,
    _layout_migrate_impl,
)

__all__ = [
    "_LAYOUT_MARKER",
    "_CONTAINER_MAIN_DIR",
    "_RUNTIME_DIR",
    "_TRASH_DIR",
    "_LAYOUT_VERSION",
    "_JUNK_NAMES",
    "_JUNK_PREFIXES",
    "_TOOL_DIR_NAMES",
    "_is_junk_entry",
    "_LEDGER_RUNTIME_FILES",
    "_move_ledger_runtime_files",
    "_GIT_TIMEOUT",
    "marker_path",
    "read_layout",
    "write_layout",
    "_git_toplevel",
    "_git_common_dir",
    "_is_git_tracked_path",
    "_auto_detect_layout",
    "_build_layout",
    "detect_layout",
    "detect_container_root_cwd",
    "nearest_git_root",
    "find_orchd_dir_within_git_boundary",
    "resolve_canonical_project_root",
    "resolve_master_path",
    "resolve_master_path_from_dir",
    "resolve_declaration_source",
    "_default_master",
    "bootstrap_container",
    "_rollback_layout_migration",
    "_intake_lock_filename",
    "layout_migrate",
    "_layout_migrate_impl",
]
