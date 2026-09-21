"""任务注册表端口（task-file-backend）。

端口 / 适配器物理分层（为大目标端口化铺路）：本模块**只含** ——
- 端口：``TaskRegistryBackend``（ABC，不含任何实现细节）；
- 选择器：``register_backend`` / ``get_backend`` / ``load_registry``；
- 公共 API 导出（``from orchd.registry import ...`` 向后兼容）。
文件适配器实现见 ``orchd.registry_file``（一适配器一模块）。

依赖方向：本模块仅标准库（``Master`` 仅在类型标注用，经 ``TYPE_CHECKING``）；
内置适配器由 :func:`get_backend` 经 ``importlib`` 惰性导入 —— 无顶层 import 循环，
端口层不引用适配器实现。``spec.load_master`` 亦委托本模块（保留兼容入口）。
"""

from __future__ import annotations

import importlib
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注
    from orchd.spec import Master

# 适配器选择：显式参数 > 环境变量 > 缺省 file（切换适配器不改调用点语义）。
_BACKEND_ENV = "ORCHD_REGISTRY_BACKEND"
_DEFAULT_BACKEND = "file"

# 内置适配器登记：name -> (module, class)，惰性导入（端口层不含实现细节）。
_BUILTINS: dict[str, tuple[str, str]] = {
    "file": ("orchd.registry_file", "FileRegistryBackend"),
}
# 可切换适配器工厂：name -> 无参工厂（返回 TaskRegistryBackend）。
# 供未来 sqlite 等适配器经 register_backend 注入，不改本模块与调用点。
_FACTORIES: dict[str, Callable[[], "TaskRegistryBackend"]] = {}


class TaskRegistryBackend(ABC):
    """任务注册表端口（单一抽象面）。

    :meth:`load` 返回 ``Master``，覆盖现有注册表全部读取面：``tasks`` /
    ``project`` / ``modules`` / ``shared`` / ``config`` / ``source_path``——
    调用点语义与直接读 ``_master.json`` 时一致。
    """

    name: str = ""

    @abstractmethod
    def load(self, path: Path | str) -> "Master":
        """加载注册表并返回 ``Master``（E001/E002 语义由具体适配器保证）。"""
        raise NotImplementedError


def register_backend(name: str, factory: Callable[[], TaskRegistryBackend]) -> None:
    """登记可切换适配器工厂（供未来 sqlite 等适配器注入）。"""
    _FACTORIES[name] = factory


def get_backend(name: str | None = None) -> TaskRegistryBackend:
    """按 显式参数 / ``ORCHD_REGISTRY_BACKEND`` / 缺省 file 解析适配器实例。"""
    key = name or os.environ.get(_BACKEND_ENV) or _DEFAULT_BACKEND
    if key in _FACTORIES:
        return _FACTORIES[key]()
    if key in _BUILTINS:
        module_name, class_name = _BUILTINS[key]
        module = importlib.import_module(module_name)
        return getattr(module, class_name)()
    raise ValueError(
        f"unknown registry backend: {key!r} "
        f"(known: {sorted(set(_BUILTINS) | set(_FACTORIES))})"
    )


def load_registry(path: Path | str, *, backend: str | None = None) -> "Master":
    """加载任务注册表（统一入口；切换适配器不改调用点）。"""
    return get_backend(backend).load(path)
