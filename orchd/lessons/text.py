"""文本层：长度校验、ID 递增、分词、冲突检测、版本关系、案例投影与字符预算。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re

from orchd.errors import ErrorCode, OrchdError
from .constants import (
    CASES_CHAR_BUDGET, LESSON_ID_RE, SOLUTION_MAX, SOLUTION_TRUNC, SYMPTOM_MAX, SYMPTOM_TRUNC,
    _NEG_WORDS,
)
from .storage import _lib_path, _read_jsonl


# ── 校验与 ID ──────────────────────────────────────────────────────────────────
def _validate_lengths(symptom: str, solution: str) -> None:
    """长度校验（§6.2）：超限抛 E007。"""
    if len(symptom) > SYMPTOM_MAX:
        raise OrchdError(
            ErrorCode.E007,
            f"lesson_validation: symptom 超过 {SYMPTOM_MAX} 字符（当前 {len(symptom)}）",
            [{"field": "symptom", "max": SYMPTOM_MAX, "actual": len(symptom)}],
        )
    if len(solution) > SOLUTION_MAX:
        raise OrchdError(
            ErrorCode.E007,
            f"lesson_validation: solution 超过 {SOLUTION_MAX} 字符（当前 {len(solution)}）",
            [{"field": "solution", "max": SOLUTION_MAX, "actual": len(solution)}],
        )


def _next_lesson_id(orchd_dir: Path | str) -> str:
    """自增 lesson id（复用 ledger 的 id 分配模式，§6.2）。"""
    rows = _read_jsonl(_lib_path(orchd_dir))
    max_n = 0
    for r in rows:
        m = LESSON_ID_RE.match(str(r.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"lesson-{max_n + 1:04d}"


# ── 冲突检测（§6.5）────────────────────────────────────────────────────────────
def _tokenize(text: str) -> set[str]:
    """粗略分词：去标点、小写、CJK 与英文词混合。"""
    text = text.lower()
    # 保留中文连续串与英文单词
    tokens = re.findall(r"[一-鿿]+|[a-z0-9]+", text)
    return {t for t in tokens if len(t) >= 2}


def _conflict_with(existing: list[dict[str, Any]], new_solution: str) -> str | None:
    """同 trigger.key 下新 resolved 条目与已有 verified 条目矛盾检测（关键词粗筛）。

    规则：新 solution 含否定词 且 与旧 solution 共享核心动作词 → 标记 conflicts_with。
    不做 LLM 语义判定（对齐方案 C 搁置），仅关键词粗筛 + 人工裁决。
    """
    if not _NEG_WORDS.intersection(_tokenize(new_solution)):
        return None
    new_tokens = _tokenize(new_solution)
    for e in existing:
        if e.get("status") != "verified":
            continue
        old_sol = e.get("solution", "")
        if not old_sol:
            continue
        if new_tokens & _tokenize(old_sol):
            return e.get("id")
    return None


# ── 版本漂移判定（§9）──────────────────────────────────────────────────────────
def _version_relation(old_v: str | None, cur_v: str) -> str:
    """返回 major / minor / same。best-effort：解析失败回退 same。"""
    if not old_v:
        return "same"
    try:
        from packaging.version import Version

        va, vb = Version(str(old_v)), Version(str(cur_v))
        if va.major != vb.major:
            return "major"
        if va.minor != vb.minor:
            return "minor"
        return "same"
    except Exception:
        # 兜底：仅比较首个整数段
        try:
            a0 = int(str(old_v).split(".")[0])
            b0 = int(str(cur_v).split(".")[0])
            if a0 != b0:
                return "major"
            return "same"
        except (ValueError, IndexError):
            return "same"


def _to_case(entry: dict[str, Any], cur_v: str) -> dict[str, Any]:
    """将库条目转为注入 case（摘要截断 + 版本漂移标注，设计 §8.4/§9）。"""
    symptom = (entry.get("symptom") or "")[:SYMPTOM_TRUNC]
    solution = (entry.get("solution") or "")[:SOLUTION_TRUNC]
    old_v = (entry.get("source") or {}).get("engine_version")
    relation = _version_relation(old_v, cur_v)
    status = entry.get("status", "proposed")
    if relation == "major":
        # 旧大版本：降级为 proposed 标注处理
        status = "proposed"
        suffix = "（旧大版本经验，仅供参考）"
        solution = (solution + suffix)[:SOLUTION_TRUNC + len(suffix)]
    elif relation == "minor":
        suffix = f"（来自 v{old_v}，当前 v{cur_v}，请确认适用性）"
        solution = (solution + suffix)[:SOLUTION_TRUNC + len(suffix)]
    return {
        "id": entry.get("id"),
        "symptom": symptom,
        "solution": solution,
        "status": status,
        "severity": entry.get("severity", "blocking"),
        "drift_note": relation if relation != "same" else None,
    }


def _apply_char_budget(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """字符预算约束（§8.5）：超出 600 字符从高优先级末尾裁剪。"""
    total = sum(len(c.get("symptom", "")) + len(c.get("solution", "")) for c in cases)
    out = list(cases)
    while total > CASES_CHAR_BUDGET and out:
        dropped = out.pop()
        total -= len(dropped.get("symptom", "")) + len(dropped.get("solution", ""))
    return out
