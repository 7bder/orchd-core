"""gitops 共享常量/类型（叶子，零内部依赖）。"""

from __future__ import annotations

from typing import TypeVar

_GIT_TIMEOUT = 10

# 写操作（git commit）独立超时预算：pre-commit hook 等写路径单次可越过
# 读操作 10s 上限（实测 Git Bash 宿主 11.7s），强杀会遗留 index.lock /
# next-index-*.lock 并吞成 commit_failed（task-commit-timeout-failure-surface）。
_GIT_COMMIT_TIMEOUT = 60

# 分支切换（git checkout）独立超时预算（task-flaky-hunt-freeze-gate，2026-09-22
# 实测）：checkout 同属写操作族，且在全量回归 ``-n auto``（16 worker）下的
# 进程/IO 竞争窗口内单次可越过 10s 读上限——实测全量回归两次各触发一例
# ``TimeoutExpired: git checkout main`` 被吞成 E018 done_switch_branch
# （单跑与整文件跑恒绿，纯并行负载抖动）。强杀同样会遗留 index.lock，
# 故与 commit 同口径给足写预算；调用方另配对超时的一次机会重试。
_GIT_CHECKOUT_TIMEOUT = 60

_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"

_T = TypeVar("_T")
