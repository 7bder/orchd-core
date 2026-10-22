"""worktree layout bootstrap 域（W6-C5 纯搬移）。

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
    write_layout,
)

from .gitinfo import (
    _GIT_TIMEOUT,
)

from .paths import (
    _default_master,
)




def bootstrap_container(project_root: Path, master_path: Path) -> dict[str, Any]:
    """container 新项目初始化（orchd init 无既有 master 时调用，AC3）。

    零额外操作：建 ``main/``（git init）→ 默认 master 写入 ``<容器>/.orchd/`` →
    移入 ``main/.orchd/`` → 建 ``.orchd-runtime/`` → 写 container 布局标记。

    Args:
        project_root: 容器根（``<容器>/``，本身不是 git 仓库）。
        master_path: 期望的 master 路径（``<容器>/.orchd/_master.json``）。

    Returns:
        ``{"container": True, "main_worktree": <Path>, "runtime_root": <Path>,
        "marker": <Path>, "created": [<str>]}``
    """
    project_root = Path(project_root).resolve()
    main_dir = project_root / _CONTAINER_MAIN_DIR
    created: list[str] = []

    # 初始化串行化（task-admission-lock-engine：E 项）—— 并发 init 竞态防护。
    # 直接把 master / 布局落盘到最终稳定的 main/.orchd（不再经「暂存目录 +
    # shutil.move」搬运）：锁文件始终落在 main/.orchd/.intake.lock（稳定、永不被
    # 搬动），避免 Windows 因持锁目录被 rename 而报 WinError 5/33（E999）。
    # 进程内可重入（ledger 注册表），同进程同路径二次获取不重复阻塞。
    from orchd.ledger import (
        _write_text_atomic,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    orchd_dst = main_dir / ".orchd"
    lk = None
    try:
        # 先建最终 orchd 目录（锁文件落点），再获取稳定的 .intake.lock
        orchd_dst.mkdir(parents=True, exist_ok=True)
        lk = intake_lock_acquire(orchd_dst, resolve_agent_id(orchd_dst))
        main_dir.mkdir(parents=True, exist_ok=True)
        created.append(str(main_dir.relative_to(project_root)))

        # 默认 master 直接写入 main/.orchd/（无需暂存 + move）
        master_file = orchd_dst / "_master.json"
        if not master_file.exists():
            _write_text_atomic(
                master_file,
                json.dumps(_default_master(main_dir), ensure_ascii=False, indent=2) + "\n",
            )
            created.append(str(master_file.relative_to(project_root)))

        # git init（best-effort；已是 git 仓库则跳过）
        # A0c：**无 git 环境不得阻断 init**——接入门槛降到「有目录即可」。
        # 无 git 可执行文件时 subprocess 抛 FileNotFoundError（旧实现未捕获 → init 整体失败，
        # 这正是「无 git 就进不来」的根因）；此处静默跳过，改由无 git 模式（快照目录 +
        # 文件锁）承接，布局标记照写。
        if not (main_dir / ".git").exists():
            try:
                subprocess.run(
                    ["git", "init", "-q"],
                    cwd=str(main_dir),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT,
                )
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass

        # 共享账本 runtime 根
        runtime = project_root / _RUNTIME_DIR
        runtime.mkdir(parents=True, exist_ok=True)
        created.append(str(runtime.relative_to(project_root)))

        # 布局标记
        write_layout(orchd_dst, "container", main_dir)
        created.append(str(marker_path(orchd_dst).relative_to(project_root)))

        result = {
            "container": True,
            "main_worktree": str(main_dir),
            "runtime_root": str(runtime),
            "marker": str(marker_path(orchd_dst)),
            "created": created,
        }
    finally:
        if lk is not None:
            intake_lock_release(lk)
    return result
