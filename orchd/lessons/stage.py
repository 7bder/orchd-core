"""写入通道：stage（暂存）/ add（直落）/ report（上报）三段。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import time

from orchd.errors import ErrorCode, OrchdError
from .storage import _append_jsonl, _lib_path, _lock_path, _read_jsonl, _staged_path
from .text import _conflict_with, _next_lesson_id, _validate_lengths


def stage(
    orchd_dir: Path | str,
    *,
    task_id: str,
    trigger_type: str,
    trigger_key: str,
    scene: str | None,
    symptom: str,
    solution: str,
    resolved: bool,
    severity: str,
    urgent: bool,
    source: dict[str, Any],
) -> dict[str, Any]:
    """lesson stage：执行中静默打点到任务暂存区（设计 §7/§8.6）。

    写入 lessons.staged.jsonl（带 task_id + resolved 标记），无 stderr 噪音。
    校验：trigger.key 必填、symptom/solution 长度；冲突检测（resolved=true 时）。
    """
    if not trigger_key:
        raise OrchdError(ErrorCode.E007, "lesson_validation: trigger.key 必填",
                         [{"field": "trigger_key"}])
    _validate_lengths(symptom, solution)
    entry: dict[str, Any] = {
        "task_id": task_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": {"type": trigger_type, "key": trigger_key, "scene": scene},
        "symptom": symptom,
        "solution": solution,
        "resolved": bool(resolved),
        "severity": severity,
        "urgent": bool(urgent),
        "source": source,
    }
    if resolved and solution:
        conflict = _conflict_with(_read_jsonl(_lib_path(orchd_dir)), solution)
        if conflict:
            entry["conflicts_with"] = conflict
    ok = _append_jsonl(_staged_path(orchd_dir), entry, _lock_path(orchd_dir))
    if not ok:
        # best-effort 跳过：不阻塞主流程（设计 §6.3）
        return {"staged": False, "skipped": True,
                "reason": "lesson 暂存因并发冲突跳过，不影响主流程"}
    return {"staged": True, "entry": entry}


def add(
    orchd_dir: Path | str,
    *,
    trigger_type: str,
    trigger_key: str,
    scene: str | None,
    symptom: str,
    solution: str,
    severity: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    """lesson add：人工/事后手动入库（不经任务流程，设计 §7）。

    写入主库，status=proposed（信任分级：proposed 仅参考，resolve --approve 才 verified）。
    同 trigger.key + 相近 symptom 提示已有相近 lesson（不自动去重，交人工 resolve）。
    """
    if not trigger_key:
        raise OrchdError(ErrorCode.E007, "lesson_validation: trigger.key 必填",
                         [{"field": "trigger_key"}])
    if not solution:
        raise OrchdError(ErrorCode.E007, "lesson_validation: add 需提供 solution",
                         [{"field": "solution"}])
    _validate_lengths(symptom, solution)
    lesson_id = _next_lesson_id(orchd_dir)
    entry: dict[str, Any] = {
        "id": lesson_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": {"type": trigger_type, "key": trigger_key, "scene": scene},
        "symptom": symptom,
        "solution": solution,
        "source": source,
        "severity": severity,
        "status": "proposed",
        "hits": 0,
        "lengths": {"symptom": len(symptom), "solution": len(solution)},
    }
    # 相近提示（不自动去重）
    similar = [
        e for e in _read_jsonl(_lib_path(orchd_dir))
        if (e.get("trigger") or {}).get("key") == trigger_key
    ]
    note = None
    if similar:
        note = f"已有 {len(similar)} 条相近 trigger.key={trigger_key} 的 lesson，请人工比对"
    ok = _append_jsonl(_lib_path(orchd_dir), entry, _lock_path(orchd_dir))
    if not ok:
        return {"added": False, "skipped": True,
                "reason": "lesson 入库因并发冲突跳过，不影响主流程"}
    return {"added": True, "id": lesson_id, "similar_note": note}


def report(
    orchd_dir: Path | str,
    *,
    trigger_type: str,
    trigger_key: str,
    scene: str | None,
    symptom: str,
    severity: str,
    source: dict[str, Any],
    guidance_flaw: bool = False,
) -> dict[str, Any]:
    """lesson report：只记问题不记解法（设计 §7）。

    status=proposed，solution 为空；guidance_flaw 标记指引缺陷（供后续补 ERROR_GUIDANCE）。
    """
    if not trigger_key:
        raise OrchdError(ErrorCode.E007, "lesson_validation: trigger.key 必填",
                         [{"field": "trigger_key"}])
    _validate_lengths(symptom, "")
    lesson_id = _next_lesson_id(orchd_dir)
    entry: dict[str, Any] = {
        "id": lesson_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": {"type": trigger_type, "key": trigger_key, "scene": scene},
        "symptom": symptom,
        "solution": "",
        "source": source,
        "severity": severity,
        "status": "proposed",
        "hits": 0,
        "guidance_flaw": bool(guidance_flaw),
        "lengths": {"symptom": len(symptom), "solution": 0},
    }
    ok = _append_jsonl(_lib_path(orchd_dir), entry, _lock_path(orchd_dir))
    if not ok:
        return {"reported": False, "skipped": True,
                "reason": "lesson 入库因并发冲突跳过，不影响主流程"}
    return {"reported": True, "id": lesson_id}
