"""Schema 迁移通道（W1-③，停服核心）。

只做加法式版本前移：``schema_version`` 1 → 2（v2 内容与 v1 同构，
目录即版本标记）。幂等可重放；未知版本拒绝；账本零触碰
（回滚 = 回退引擎版本，审计日志未改）。

本模块仅依赖标准库（刻意不 import orchd 子模块，避免新增依赖边；
版本号与 spec.py 门禁字面量一致，由契约测试双边锁定）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LATEST_SCHEMA_VERSION = 2
EARLIEST_SCHEMA_VERSION = 1


class SchemaVersionError(Exception):
    """未知 schema_version（版本闸门拒绝）。"""


def migrate_master_data(
    data: dict[str, Any], target: int = LATEST_SCHEMA_VERSION
) -> tuple[dict[str, Any], bool]:
    """迁移单个 master 字典到目标版本。幂等，可 retreat。

    Args:
        data: master 字典（不被修改）。
        target: 目标版本，须在支持区间内（默认最新）。

    Returns:
        (迁移后字典, 是否发生变更)。输入字典不被修改。
    """
    version = data.get("schema_version", 1)
    for value in (version, target):
        if not isinstance(value, int) or not (
            EARLIEST_SCHEMA_VERSION <= value <= LATEST_SCHEMA_VERSION
        ):
            raise SchemaVersionError(
                f"unsupported schema_version: {value!r} "
                f"(supported {EARLIEST_SCHEMA_VERSION}"
                f"..{LATEST_SCHEMA_VERSION})"
            )
    if version == target:
        return data, False
    migrated = dict(data)
    migrated["schema_version"] = target
    return migrated, True


def migrate_master_file(
    path: Path | str, dry_run: bool = False, retreat: bool = False
) -> dict[str, Any]:
    """迁移磁盘上的 _master.json 文件。返回执行摘要。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    target = EARLIEST_SCHEMA_VERSION if retreat else LATEST_SCHEMA_VERSION
    migrated, changed = migrate_master_data(data, target=target)
    if changed and not dry_run:
        path.write_text(
            json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "path": str(path),
        "from_version": data.get("schema_version", 1),
        "to_version": migrated.get("schema_version", 1),
        "changed": changed,
        "dry_run": dry_run,
    }
