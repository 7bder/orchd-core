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
from typing import Any

import orchd


def _schema_path() -> Path:
    """``<repo>/schema/_master.schema.json``。

    W7-A：锚点取 ``orchd.__file__``（包根）而非本文件层级——拆包 / 搬迁后不再静默错根。
    """
    return Path(orchd.__file__).resolve().parent.parent / "schema" / "_master.schema.json"


@lru_cache(maxsize=1)
def _task_properties() -> dict:
    data = json.loads(_schema_path().read_text(encoding="utf-8"))
    return data["properties"]["tasks"]["items"]["properties"]


def amendable_fields() -> frozenset[str]:
    """amend 声明域可补登字段（真源：schema 的 ``x-amendable`` 注解）。"""
    return frozenset(
        name for name, prop in _task_properties().items() if prop.get("x-amendable")
    )


def terminal_attachable_fields() -> frozenset[str]:
    """终态可附加字段（真源：schema 的 ``x-terminal-attachable`` 注解）。"""
    return frozenset(
        name
        for name, prop in _task_properties().items()
        if prop.get("x-terminal-attachable")
    )


def task_schema_fields() -> frozenset[str]:
    """任务全部 schema 字段（真源：``properties.tasks.items.properties`` 键集）。"""
    return frozenset(_task_properties().keys())


@lru_cache(maxsize=1)
def _task_items() -> dict:
    data = json.loads(_schema_path().read_text(encoding="utf-8"))
    return data["properties"]["tasks"]["items"]


@lru_cache(maxsize=1)
def required_task_fields() -> frozenset[str]:
    """``tasks[].required`` —— 注册必填字段唯一真源
    （task-engine-defect-fix-unified-u8 AC1：代码不再硬编码第二份清单）。"""
    return frozenset(_task_items().get("required") or ())


@lru_cache(maxsize=1)
def task_field_defaults() -> dict[str, Any]:
    """schema 中带 ``default`` 的任务字段缺省值（注册时按真源补齐）。"""
    props = _task_items().get("properties") or {}
    return {
        name: prop["default"]
        for name, prop in props.items()
        if "default" in prop
    }
