"""注入查询：lookup_lessons 与命中富化 _enrich_hits。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd import __version__
from .constants import CASES_PROPOSED_CAP, CASES_VERIFIED_CAP
from .storage import _lib_path, _lock_path, _read_jsonl, _rewrite_jsonl
from .text import _apply_char_budget, _to_case


# ── 触发注入（§8.4）────────────────────────────────────────────────────────────
def lookup_lessons(
    orchd_dir: Path | str, trigger_key: str, current_version: str | None = None
) -> list[dict[str, Any]]:
    """按 trigger.key 精确匹配查询注入 cases（设计 §8.2/§8.5）。

    - verified 命中 → 按 hits 降序取顶 3；无 verified 命中 → proposed 取 1（标注未验证）。
    - 摘要截断 + 字符预算（≤600）；版本漂移标注。
    - best-effort：库缺失/损坏返回空列表，不注入。
    """
    if current_version is None:
        current_version = __version__
    try:
        entries = _read_jsonl(_lib_path(orchd_dir))
    except Exception:
        return []
    verified = [
        e for e in entries
        if e.get("status") == "verified" and (e.get("trigger") or {}).get("key") == trigger_key
    ]
    proposed = [
        e for e in entries
        if e.get("status") == "proposed" and (e.get("trigger") or {}).get("key") == trigger_key
    ]
    verified.sort(key=lambda e: e.get("hits", 0), reverse=True)
    cases: list[dict[str, Any]] = []
    for e in verified[:CASES_VERIFIED_CAP]:
        cases.append(_to_case(e, current_version))
    if not cases:
        for e in proposed[:CASES_PROPOSED_CAP]:
            cases.append(_to_case(e, current_version))
    return _apply_char_budget(cases)


# ── 写命令：stage / add / report ───────────────────────────────────────────────
def _enrich_hits(orchd_dir: Path | str, trigger_key: str) -> None:
    """命中计数 +1（供淘汰参考，§6.1 hits）。best-effort。"""
    try:
        path = _lib_path(orchd_dir)
        rows = _read_jsonl(path)
        changed = False
        for r in rows:
            if (r.get("trigger") or {}).get("key") == trigger_key and r.get("status") == "verified":
                r["hits"] = int(r.get("hits", 0)) + 1
                changed = True
        if changed:
            _rewrite_jsonl(path, rows, _lock_path(orchd_dir))
    except Exception:
        pass
