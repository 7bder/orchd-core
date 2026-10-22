"""两个 NamedTuple：MilestoneContext（评估上下文）与 _Check（单条判据）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Callable
from typing import NamedTuple


class MilestoneContext(NamedTuple):
    """判据求值上下文（只读）。"""

    project_root: Path          # 主工作树（HEAD 与文档基准）
    master_dir: Path            # canonical master 所在目录（.orchd）
    store: Any                  # orchd.ledger.Store（账本边界）
    tasks: list[dict[str, Any]]  # 任务定义
    state: dict[str, Any]       # store.replay() 结果（任务 → 状态）


class _Check(NamedTuple):
    id: str
    group: str
    title: str
    fn: Callable[[MilestoneContext], tuple[bool, str, str]]
