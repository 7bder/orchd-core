"""done 后钩子与审查事务（run_done_lesson_hook/_review_task_transaction）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import time

from orchd.contracts.task_schema import (
    ID_FIELD,
    SOURCE_FIELD,
)
from orchd.errors import OrchdError
from orchd.lockfile import ExclusiveFileLock
from .config import load_lessons_config
from .constants import LOCK_TIMEOUT_S
from .storage import (
    _append_line_unlocked, _lib_path, _lock_path, _read_jsonl, _rewrite_jsonl, _staged_path,
    _write_rows_unlocked,
)
from .text import _next_lesson_id


# ── 收尾 hook（§8.6）───────────────────────────────────────────────────────────
def run_done_lesson_hook(
    orchd_dir: Path | str, task_id: str, verify_passed: bool | None
) -> dict[str, Any]:
    """done 收尾 hook：检测暂存建议 + resolved 交叉验证（设计 §8.6 补丁 3）。

    - 读取该任务 staged 条目；无 → 返回 has_lessons=False（零影响）。
    - resolved=true 但 verify 未通过（verify_passed=False/None）→ 引擎降级
      resolved=false，标注降级原因并持久化回 staged。
    - 给每条打 detected_at 时间戳（供 review 超时降级判定）。
    - 返回结构化汇总（items / degraded / summary），由调用方决定是否挂起（门禁）。
    """
    staged = [e for e in _read_jsonl(_staged_path(orchd_dir)) if e.get("task_id") == task_id]
    if not staged:
        return {"has_lessons": False}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    degraded: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    all_staged = _read_jsonl(_staged_path(orchd_dir))
    for s in all_staged:
        if s.get("task_id") != task_id:
            continue
        s["detected_at"] = now_iso
        item = {
            "trigger_key": (s.get("trigger") or {}).get("key"),
            "symptom": s.get("symptom"),
            "severity": s.get("severity", "blocking"),
            "resolved": bool(s.get("resolved")),
            "conflicts_with": s.get("conflicts_with"),
        }
        if s.get("resolved") and not verify_passed:
            s["resolved"] = False
            reason = "resolved 被引擎降级：verify 未通过/未执行"
            s["degraded_reason"] = reason
            item["resolved"] = False
            item["degraded_reason"] = reason
            degraded.append(item)
        items.append(item)
    # 持久化 detected_at / 降级结果（best-effort）
    _rewrite_jsonl(_staged_path(orchd_dir), all_staged, _lock_path(orchd_dir))
    summary = (
        f"本次任务发现 {len(items)} 个 guidance 增补建议"
        f"（resolved={sum(1 for i in items if i['resolved'])}），待人工审核"
    )
    return {
        "has_lessons": True,
        "count": len(items),
        "items": items,
        "degraded": degraded,
        "summary": summary,
    }


# ── 审查收口：review / resolve / archive ──────────────────────────────────────
def _review_task_transaction(
    orchd_dir: Path | str,
    *,
    task_id: str,
    approve_all: bool,
    reject_indices: set[int],
) -> dict[str, Any]:
    """review_task 的单次持锁事务：落库 + 摘除 staged 在同一锁窗口内完成。

    并发语义（task-lessons-review-single-transaction）：持锁期间其他进程
    无法 append/rewrite，消除「append 成功但 rewrite 未完成」的中间态被
    并发 review 重复 promote 的窗口。重跑 review 不产生重复条目。
    失败语义保持不静默：落库失败保留 staged + deferred；回写失败
    staged_write_skipped 留痕。锁获取失败由调用方降级处理。
    """
    lock = ExclusiveFileLock(_lock_path(orchd_dir))
    try:
        lock.acquire(blocking=False, timeout_s=LOCK_TIMEOUT_S)
    except OrchdError:
        return {"lock_failed": True}

    try:
        # 持锁后重新读取 staged（事务一致性：避免锁获取前的快照已过期）
        all_staged = _read_jsonl(_staged_path(orchd_dir))
        staged = [e for e in all_staged if e.get("task_id") == task_id]
        if not staged:
            return {"promoted": [], "rejected": [], "timed_out": False,
                    "note": "无暂存建议"}

        cfg = load_lessons_config(orchd_dir)
        timeout = int(cfg.get("review_timeout_minutes", 60))
        timed_out = False
        now = time.time()
        for s in staged:
            da = s.get("detected_at")
            if da:
                try:
                    dt = time.strptime(da, "%Y-%m-%dT%H:%M:%S%z")
                    if (now - time.mktime(dt)) > timeout * 60:
                        timed_out = True
                except (ValueError, OverflowError):
                    pass

        promoted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        kept_staged: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []

        lib_path = _lib_path(orchd_dir)
        lib_path.parent.mkdir(parents=True, exist_ok=True)

        for idx, s in enumerate(staged):
            if idx in reject_indices:
                rejected.append({"trigger_key": (s.get("trigger") or {}).get("key"),
                                 "symptom": s.get("symptom")})
                continue
            if not approve_all and idx not in reject_indices:
                # 未 approve 且未 reject → 保留在 staged（等待后续处理）
                kept_staged.append(s)
                continue
            lesson_id = _next_lesson_id(orchd_dir)
            status = "verified" if s.get("resolved") else "proposed"
            entry = {
                ID_FIELD: lesson_id,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "trigger": s.get("trigger"),
                "symptom": s.get("symptom"),
                "solution": s.get("solution", ""),
                SOURCE_FIELD: s.get(SOURCE_FIELD),
                "severity": s.get("severity", "blocking"),
                "status": status,
                "hits": 0,
                "conflicts_with": s.get("conflicts_with"),
                "lengths": {"symptom": len(s.get("symptom", "")),
                            "solution": len(s.get("solution", ""))},
            }
            # 无锁原语追加（调用方已持 lessons 锁；ExclusiveFileLock 进程级可重入，
            # 嵌套持锁 depth+1 不死锁——此前“避免嵌套锁死锁”注释为事实错误）：
            # 落库失败 → 保留 staged + deferred 登记（task-intake-atomic-lock-consistent AC3）
            if not _append_line_unlocked(lib_path, entry):
                kept_staged.append(s)
                deferred.append({
                    "trigger_key": (s.get("trigger") or {}).get("key"),
                    "symptom": s.get("symptom"),
                    "reason": "append_failed",
                })
                continue
            promoted.append({
                ID_FIELD: lesson_id,
                "status": status,
                "trigger_key": (s.get("trigger") or {}).get("key"),
                "symptom": s.get("symptom"),
                SOURCE_FIELD: s.get(SOURCE_FIELD),
            })

        # 无锁原语回写 staged（调用方已持 lessons 锁，同上无嵌套锁风险）：
        # 先移除本任务全部条目，再并回 kept_staged（未 approve/reject 的 +
        # 落库失败待重试的）——已成功 promote 的条目即在此被摘除。
        final_staged = [x for x in all_staged if x.get("task_id") != task_id] + kept_staged
        staged_written = _write_rows_unlocked(_staged_path(orchd_dir), final_staged)

        result: dict[str, Any] = {"promoted": promoted, "rejected": rejected,
                                  "timed_out": timed_out}
        if deferred:
            result["deferred"] = deferred
            result["hint"] = "部分 lesson 落库失败（并发冲突/IO），已保留在 staged 供重试"
        if not staged_written:
            # 回写失败：已入库条目可能仍留在暂存区——显式留痕不静默
            result["staged_write_skipped"] = True
            result["staged_hint"] = "staged 回写未成功（IO 失败）：重跑 review 前请先核对暂存区"
        return result
    finally:
        lock.release()
