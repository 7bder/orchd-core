"""worktree layout detect 域（W6-C5 纯搬移）。

来源：``orchd/worktree/layout.py``（1079 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from .marker import (
    _CONTAINER_MAIN_DIR,
    _RUNTIME_DIR,
    marker_path,
    read_layout,
)

from .gitinfo import (
    _git_toplevel,
)




def _auto_detect_layout(git_root: Path | None, project_root: Path) -> str:
    """无布局标记时的布局猜测（best-effort，仅用于告警与降级解析）。

    git 根名字为 ``main`` 且其父目录下确为 ``<父>/main`` 结构 → container；
    否则 flat（flat 是既有项目默认，保守不回退到容器）。
    """
    if (
        git_root is not None
        and git_root.name == _CONTAINER_MAIN_DIR
        and (git_root.parent / _CONTAINER_MAIN_DIR) == git_root
    ):
        return "container"
    return "flat"



def _build_layout(layout: str, main_wt: Path, warnings: list[str], source: str) -> dict[str, Any]:
    """由主工作树派生完整布局信息（任务 worktree 根 == runtime 根 == 主工作树父目录）。"""
    return {
        "layout": layout,
        "main_worktree": Path(main_wt).resolve(),
        "task_wt_root": Path(main_wt).resolve().parent,
        "runtime_root": Path(main_wt).resolve().parent / _RUNTIME_DIR,
        "marker_source": source,
        "warnings": warnings,
    }



def detect_layout(project_root: Path) -> dict[str, Any]:
    """解析双布局：主工作树 / 任务 worktree 根 / 共享账本 runtime 根。

    优先读布局标记（``<project_root>/.orchd/.layout.json``，权威）；缺失时
    自动探测（git 定位主工作树）+ 告警（不静默跑错目录）。

    Args:
        project_root: 主工作树根（container 下为 ``<容器>/main/``，flat 下为
            ``<项目根>/``）。

    Returns:
        ``{"layout": "container"|"flat", "main_worktree": <Path>,
        "task_wt_root": <Path>, "runtime_root": <Path>,
        "marker_source": "marker"|"detected", "warnings": [str]}``
    """
    project_root = Path(project_root).resolve()
    orchd_dir = project_root / ".orchd"
    warnings: list[str] = []

    marker = read_layout(orchd_dir)
    if marker is not None:
        main_wt = Path(marker["main_worktree"])
        if main_wt.resolve() != project_root:
            warnings.append(
                f"布局标记 main_worktree（{main_wt}）与当前目录（{project_root}）不一致，"
                "以标记为准"
            )
        return _build_layout(marker["layout"], main_wt, warnings, "marker")

    # 标记缺失 → 自动探测（git 定位主工作树）+ 告警
    git_root = _git_toplevel(project_root) or project_root
    layout = _auto_detect_layout(git_root, project_root)
    warnings.append(
        f"布局标记缺失（{marker_path(orchd_dir)}），自动探测为 {layout}"
    )
    return _build_layout(layout, git_root, warnings, "detected")



def detect_container_root_cwd(cwd: Path, orchd_dir: Path) -> tuple[str | None, Path | None]:
    """检测 cwd 是否为容器根（主工作树父目录），纪律护栏的判定核心。

    容器根特征：其 ``.orchd`` 是 junction/符号链接（指向主工作树 .orchd），
    ``_find_orchd_dir`` 会命中它，使 project_root 被解析成容器根而非主工作树，
    进而污染任务 worktree 布局标记、引发 worktree/分支误删
    （2026-08-30 task-audit-* 分支丢失复盘）。

    Returns:
        ``(reason, main_wt)``：
        - cwd 为容器根 → ``("container_root", <main_worktree>)``；
        - 否则 → ``(None, None)``。
        标记缺失 / 非 container 布局 / 读取异常 → ``(None, None)``（best-effort）。
    """
    try:
        cwd = Path(cwd).resolve()
        marker = read_layout(Path(orchd_dir))
        if marker is not None and marker.get("layout") == "container":
            main_wt = Path(marker["main_worktree"]).resolve()
            if cwd == main_wt.parent:
                return "container_root", main_wt
        # 反向不变量（debug-pack C1）：容器根即使 .layout.json 缺失（坏态）也须命中。
        # 原判定只在「主工作树标记在场」时成立——检测依据只在正常态存在，坏态必然
        # 缺证，静默放过后给出误导性 E001。按容器约定路径特征兜底：cwd 下同时存在
        # main/.orchd（主工作树载体）与 .orchd-runtime（容器 runtime 根）。
        _main = cwd / _CONTAINER_MAIN_DIR
        if (_main / ".orchd").is_dir() and (cwd / _RUNTIME_DIR).is_dir():
            return "container_root", _main
    except Exception:
        return None, None
    return None, None
