"""worktree layout marker 域（W6-C5 纯搬移）。

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


if TYPE_CHECKING:  # 仅类型标注：叶子模块，运行时不依赖 ledger（防循环）
    from orchd.ledger import Store


# 布局标记相对路径（位于 <主工作树>/.orchd/ 下）
_LAYOUT_MARKER = ".layout.json"

# container 布局的主工作树子目录名
_CONTAINER_MAIN_DIR = "main"

# 共享账本 runtime 根目录名（主工作树父目录下）
_RUNTIME_DIR = ".orchd-runtime"

# 迁移时无效文件隔离回收子目录（<runtime>/trash/，可手动还原）
_TRASH_DIR = "trash"

_LAYOUT_VERSION = 1



def marker_path(orchd_dir: Path) -> Path:
    """返回布局标记路径（<orchd_dir>/.layout.json）。"""
    return orchd_dir / _LAYOUT_MARKER



def read_layout(orchd_dir: Path) -> dict[str, Any] | None:
    """读取布局标记；文件缺失或损坏返回 None（best-effort）。

    Args:
        orchd_dir: 主工作树的 .orchd 目录。

    Returns:
        标记 dict（含 layout / version / main_worktree），或 None。
    """
    try:
        # utf-8-sig 兼容 Windows 编辑器写入的 BOM（BOM 会导致 json.loads 失败）
        data = json.loads(marker_path(orchd_dir).read_text(encoding="utf-8-sig"))
        layout = data.get("layout")
        main_worktree = data.get("main_worktree")
        if layout in ("container", "flat") and isinstance(main_worktree, str) and main_worktree:
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None



def write_layout(orchd_dir: Path, layout: str, main_worktree: Path) -> dict[str, Any]:
    """原子写入布局标记（tmp + fsync + _atomic_replace + 目录项 fsync）。

    原子 rename 只保证「要么旧的、要么新的」，防标记错乱读半成品；fsync 再补
    「新内容已落设备」，消除断电零长窗口（task-os-replace-hardening-worktree-batch
    批次 1a：与账本 ``ledger._write_text_atomic`` 同档，复用其
    ``_atomic_replace`` 的 WinError 5/32 有界重试——耗尽抛结构化 E007 前缀
    ``atomic_replace_failed``——与 ``_fsync_dir``）。tmp 点前缀命名、强制 LF
    与 JSON 文本形态原样保留。

    Args:
        orchd_dir: 主工作树的 .orchd 目录。
        layout: ``"container"`` 或 ``"flat"``。
        main_worktree: 主工作树绝对路径。

    Returns:
        结构化结果：``{"written": True, "path": <str>, "layout": <str>}``。
    """
    from orchd.ledger import _atomic_replace, _fsync_dir

    data = {
        "layout": layout,
        "version": _LAYOUT_VERSION,
        "main_worktree": str(Path(main_worktree).resolve()),
    }
    marker = marker_path(orchd_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f".{marker.name}.tmp")
    # A3（task-hostfix-pack-b）：强制 LF。Windows 文本模式默认把 \n 翻成 \r\n，
    # 而标记文件以 LF 入库（或首次写入即 LF）→ git status 出現内容为空的幻影脏位
    # （ROADMAP.md 同款已在 hook 写盘路径修复，此处同源）。
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    _atomic_replace(tmp, marker)
    _fsync_dir(marker.parent)
    return {"written": True, "path": str(marker), "layout": layout}
