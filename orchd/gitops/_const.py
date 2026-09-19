"""gitops 共享常量/类型（叶子，零内部依赖）。"""

from __future__ import annotations

from typing import TypeVar

_GIT_TIMEOUT = 10

# 写操作（git commit）独立超时预算：pre-commit hook 等写路径单次可越过
# 读操作 10s 上限（实测 Git Bash 宿主 11.7s），强杀会遗留 index.lock /
# next-index-*.lock 并吞成 commit_failed（task-commit-timeout-failure-surface）。
_GIT_COMMIT_TIMEOUT = 60

_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"

_T = TypeVar("_T")
