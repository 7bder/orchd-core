"""Schema 派生策略（W1-②）。任务字段集合的唯一真源是
``schema/_master.schema.json`` 的 ``x-amendable`` /
``x-terminal-attachable`` 注解；本模块只做机读推导，不手写集合。

调用方切换在 W1-④；此前 split.py 手写 frozenset 仍为执行真源，
一致性由 tests/contract/test_schema_policy_parity.py 对拍锁定。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def _schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schema" / "_master.schema.json"


@lru_cache(maxsize=1)
def _task_properties() -> dict:
    data = json.loads(_schema_path().read_text(encoding="utf-8"))
    return data["properties"]["tasks"]["items"]["properties"]


def amendable_fields() -> frozenset[str]:
    """amend 声明域可补登字段（对标 split._AMEND_ATTACHABLE_FIELDS）。"""
    return frozenset(
        name for name, prop in _task_properties().items() if prop.get("x-amendable")
    )


def terminal_attachable_fields() -> frozenset[str]:
    """终态可附加字段（对标 split._TERMINAL_ATTACHABLE_FIELDS）。"""
    return frozenset(
        name
        for name, prop in _task_properties().items()
        if prop.get("x-terminal-attachable")
    )


def task_schema_fields() -> frozenset[str]:
    """任务全部 schema 字段（对标 split._TASK_SCHEMA_FIELDS）。"""
    return frozenset(_task_properties().keys())
