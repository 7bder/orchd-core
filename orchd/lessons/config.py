"""config.lessons 读取与开关判定（load_lessons_config/is_lessons_enabled），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json


# ── 配置 ──────────────────────────────────────────────────────────────────────
def load_lessons_config(orchd_dir: Path | str) -> dict[str, Any]:
    """读取 lessons 配置（设计 §9.5 / §10.1），缺省：enabled=true / require_review=true
    / review_timeout_minutes=60。best-effort：任何异常回退默认。

    task-master-path-resolver-convergence：master **路径解析**统一走
    :func:`orchd.worktree.resolve_master_path_from_dir`（唯一真源）。此前裸读
    ``<orchd_dir>/_master.json``：container 布局下任务 worktree 已抑制该副本 →
    宿主在 master 里配的 lessons 段被无声忽略，全部回落默认值
    （``require_review=True`` / ``review_timeout_minutes=60``）。
    """
    default = {"enabled": True, "require_review": True, "review_timeout_minutes": 60}
    try:
        from orchd.worktree import resolve_master_path_from_dir

        mp = resolve_master_path_from_dir(orchd_dir)
        if mp.exists():
            cfg = json.loads(mp.read_text(encoding="utf-8")).get("config", {})
            lessons = cfg.get("lessons")
            if isinstance(lessons, dict):
                if "enabled" in lessons:
                    default["enabled"] = bool(lessons["enabled"])
                if "require_review" in lessons:
                    default["require_review"] = bool(lessons["require_review"])
                if "review_timeout_minutes" in lessons:
                    try:
                        default["review_timeout_minutes"] = int(lessons["review_timeout_minutes"])
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return default


def is_lessons_enabled(orchd_dir: Path | str) -> bool:
    """lessons.enabled 开关（设计 §9.5）。关闭时注入/引导/写命令全部拒绝。"""
    return bool(load_lessons_config(orchd_dir).get("enabled", True))
