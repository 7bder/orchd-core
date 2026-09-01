"""gitops 共享常量/类型（叶子，零内部依赖）。"""

from __future__ import annotations

from typing import TypeVar

_GIT_TIMEOUT = 10

_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"

_T = TypeVar("_T")
