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

import json
import sys
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


def _log_line_degrade(reason: str, context: dict[str, Any]) -> None:
    """线解析降级留痕（pass8 F7 收口）。

    master 缺失 / 不可解析时回退单线默认不再静默：stderr 落一条
    ``orchd ▸ [line-degrade]`` 结构化行（与 session-lock 留痕同型）。
    best-effort：任何异常静默跳过，不阻断回退主流程；健康仓库（master
    可读）永不触发，零噪音。
    """
    try:
        _reconfigure = getattr(sys.stderr, "reconfigure", None)
        if callable(_reconfigure):
            try:
                _reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
        record = {"action": reason, **context}
        print(
            f"orchd ▸ [line-degrade] {json.dumps(record, ensure_ascii=False)}",
            file=sys.stderr,
        )
    except Exception:
        pass


def _master_path(project_root: Path | str | None) -> Path | None:
    """解析 canonical ``_master.json`` 路径（本地优先 → 主工作树回退）；None → None。"""
    if project_root is None:
        return None
    try:
        from orchd.worktree import resolve_master_path_from_dir
    except Exception:  # noqa: BLE001 - 环境异常一律降级单线默认（已留痕）
        _log_line_degrade("master_resolve_import_failed",
                          {"project_root": str(project_root)})
        return None
    try:
        return resolve_master_path_from_dir(Path(project_root) / ".orchd")
    except Exception:  # noqa: BLE001 - 解析失败降级单线默认（已留痕）
        _log_line_degrade("master_resolve_failed",
                          {"project_root": str(project_root)})
        return None


def _project_for(project_root: Path | str | None) -> Mapping[str, Any] | None:
    """加载 master 的 ``project`` 段（带 (mtime, size) 缓存）；不可用 → ``None``。"""
    path = _master_path(project_root)
    if path is None:
        # _master_path 内部已对自身失败留痕（project_root 非空时）；此处不重复
        return None
    if not path.is_file():
        _log_line_degrade("master_missing", {"master_path": str(path)})
        return None
    try:
        stat = path.stat()
    except OSError:
        _log_line_degrade("master_stat_failed", {"master_path": str(path)})
        return None
    key = str(path)
    cached = _CACHE.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    try:
        from orchd.spec import load_master

        project = load_master(path).project
    except Exception:  # noqa: BLE001 - 解析失败按无配置处理（单线默认，已留痕）
        _log_line_degrade("master_parse_failed", {"master_path": str(path)})
        return None
    if not isinstance(project, Mapping):
        _log_line_degrade("master_project_not_mapping", {"master_path": str(path)})
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
