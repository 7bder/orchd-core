"""orchd/line_ctx.py — 线感知配置加载器（task-line-trunk）。

把 :mod:`orchd.line` 的**纯解析**接到运行时上下文：从 canonical master 的
``project`` 段读 ``project.lines`` / ``default_line``，叠加 ``ORCHD_LINE``，
解析当前线的 trunk 与任务分支名。

为什么独立模块（不并入 ``orchd/line.py``）：``line.py`` 必须保持纯解析（仅依赖
``errors.py``，由 ``tests/test_line_core.py::test_module_depends_only_on_errors``
锁死）；本模块需要 master 路径解析与加载，属「接线层」，与纯解析层分离。

降级（opt-in 零回归）：master 缺失 / 不可解析 → 单线默认（``main`` / ``task/{id}``），
与 v1.5.0 逐字一致。``project.lines`` 存在时 ``ORCHD_LINE`` 指向未登记线 → E005
硬拒绝（不静默回退 main）。

依赖方向：``line_ctx.py → line.py / errors.py``，并对 ``spec.py`` / ``worktree.py``
**函数内惰性导入**（避免模块级环依赖）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchd.line import (
    DEFAULT_TRUNK,
    resolve_line,
    resolve_task_branch_name,
    resolve_trunk,
)

# master 加载缓存：path → (mtime_ns, size, project)。任务生命周期内 master 不变，
# 避免每次 fork / merge 重复解析 2MB 级 JSON。
_CACHE: dict[str, tuple[int, int, Mapping[str, Any]]] = {}


def _master_path(project_root: Path | str | None) -> Path | None:
    """解析 canonical ``_master.json`` 路径（本地优先 → 主工作树回退）；None → None。"""
    if project_root is None:
        return None
    try:
        from orchd.worktree import resolve_master_path_from_dir
    except Exception:  # noqa: BLE001 - 环境异常一律降级单线默认
        return None
    try:
        return resolve_master_path_from_dir(Path(project_root) / ".orchd")
    except Exception:  # noqa: BLE001
        return None


def _project_for(project_root: Path | str | None) -> Mapping[str, Any] | None:
    """加载 master 的 ``project`` 段（带 (mtime, size) 缓存）；不可用 → ``None``。"""
    path = _master_path(project_root)
    if path is None or not path.is_file():
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _CACHE.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        from orchd.spec import load_master

        project = load_master(path).project
    except Exception:  # noqa: BLE001 - 解析失败按无配置处理（单线默认）
        return None
    if not isinstance(project, Mapping):
        return None
    _CACHE[key] = (stat.st_mtime_ns, stat.st_size, project)
    return project


def resolve_line_for(
    project_root: Path | str | None, env: Mapping[str, str] | None = None
) -> str:
    """当前线名；无 master / 未配置多线 → ``default``。"""
    project = _project_for(project_root)
    return resolve_line(project, env)


def resolve_trunk_for(
    project_root: Path | str | None, env: Mapping[str, str] | None = None
) -> str:
    """当前线的 trunk；无 master / 未配置多线 → ``main``（单线零回归）。"""
    project = _project_for(project_root)
    if project is None:
        return DEFAULT_TRUNK
    return resolve_trunk(resolve_line(project, env), project)


def resolve_task_branch_for(
    project_root: Path | str | None,
    task_id: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """任务分支名（线命名空间）；无 master / 未配置多线 → ``task/{id}``。"""
    project = _project_for(project_root)
    if project is None:
        return f"task/{task_id}"
    return resolve_task_branch_name(task_id, resolve_line(project, env), project)
