"""orchd/gitops/proxy/ — 聚合入口（W6-C4：退化为纯重导出）。

来源：``orchd/gitops/proxy.py``（1164 行，W6-C4 纯搬移）。

仅重导出 34 个顶层符号（外部导入取到的仍是子模块同一对象）。
"""

from .writetables import (
    _WRITE_TOKENS,
    _WRITE_FAMILIES,
    _WRITE_SUBCOMMANDS,
)
from .classify import (
    READ_ONLY_SUBCOMMANDS,
    CONDITIONAL_READ_FLAGS,
    _REQUIRED_VALUE_FLAGS,
    _OPTIONAL_VALUE_FLAGS,
    _VALUE_FLAGS,
    _BARE_READ_SUBCOMMANDS,
    _is_read_variant,
    _merge_passthrough_forms,
    _is_merge_passthrough,
    classify_git_argv,
)
from .deny import (
    _COMMIT_VALUE_FLAGS,
    _COMMIT_SHORT_WITH_VALUE,
    _COMMIT_MESSAGE_FLAGS,
    _COMMIT_DENY_EXPLAIN,
    _commit_rejections,
    _READ_DENY_CATEGORIES,
    _READ_DENY_SHORT_CHARS,
    _READ_DENY_LABEL,
    _read_denied_hit,
    _read_denied_reason,
)
from .execute import (
    _DRIVER_NEUTRALIZE_FLAGS,
    _DRIVER_NEUTRALIZE_SUBCOMMANDS,
    _neutralized_read_args,
    _git_available,
    _execute_git,
)
from .write import (
    _bound_tasks_for_root,
    _nogit_payload,
    _proxy_commit,
    _proxy_merge,
    _write_rejection,
)
from .runner import (
    run_git_proxy,
)

__all__ = [
    "READ_ONLY_SUBCOMMANDS",
    "CONDITIONAL_READ_FLAGS",
    "_REQUIRED_VALUE_FLAGS",
    "_OPTIONAL_VALUE_FLAGS",
    "_VALUE_FLAGS",
    "_BARE_READ_SUBCOMMANDS",
    "_WRITE_TOKENS",
    "_WRITE_FAMILIES",
    "_WRITE_SUBCOMMANDS",
    "_is_read_variant",
    "_merge_passthrough_forms",
    "_is_merge_passthrough",
    "_COMMIT_VALUE_FLAGS",
    "_COMMIT_SHORT_WITH_VALUE",
    "_COMMIT_MESSAGE_FLAGS",
    "_COMMIT_DENY_EXPLAIN",
    "_commit_rejections",
    "_READ_DENY_CATEGORIES",
    "_READ_DENY_SHORT_CHARS",
    "_READ_DENY_LABEL",
    "_read_denied_hit",
    "_read_denied_reason",
    "classify_git_argv",
    "_DRIVER_NEUTRALIZE_FLAGS",
    "_DRIVER_NEUTRALIZE_SUBCOMMANDS",
    "_neutralized_read_args",
    "_git_available",
    "_execute_git",
    "_bound_tasks_for_root",
    "_nogit_payload",
    "_proxy_commit",
    "_proxy_merge",
    "_write_rejection",
    "run_git_proxy",
]
