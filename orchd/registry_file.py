"""文件适配器：从 ``_master.json`` 直接加载（registry 域默认后端）。

端口 / 适配器分层（task-file-backend）：本模块**只含文件适配器实现**；端口
（``TaskRegistryBackend``）与选择器在 ``orchd.registry``。行为与拆分前逐字节
等价（E001 文件不存在 / E002 JSON 解析失败）。
"""

from __future__ import annotations

import json
from pathlib import Path

from orchd.errors import ErrorCode, OrchdError
from orchd.registry import TaskRegistryBackend
from orchd.spec import Master


class FileRegistryBackend(TaskRegistryBackend):
    """文件适配器：从 ``_master.json`` 直接加载（行为 = 现状）。

    逐字节等价于原 ``spec.load_master``：E001 文件不存在 / E002 JSON 解析失败
    （含编码错误）。
    """

    name = "file"

    def load(self, path: Path | str) -> Master:
        path = Path(path)
        if not path.exists():
            raise OrchdError(
                ErrorCode.E001,
                f"file not found: {path}",
                [{"path": str(path), "message": "目标文件不存在"}],
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OrchdError(
                ErrorCode.E002,
                f"invalid JSON in {path}: {exc}",
                [{"path": str(path), "message": str(exc)}],
            ) from exc
        return Master(raw=raw, source_path=path)
