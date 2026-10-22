"""审查与台账：review_task / resolve / archive / list / show。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from .hook import _review_task_transaction
from .storage import _lib_path, _lock_path, _read_jsonl, _rewrite_jsonl, _staged_path


def review_task(
    orchd_dir: Path | str,
    *,
    task_id: str,
    approve_all: bool = False,
    reject_indices: list[int] | None = None,
) -> dict[str, Any]:
    """lesson review：批量确认任务全部暂存建议（设计 §7/§8.6）。

    - approve → 转主库：resolved=true → verified（正式触发）；resolved=false → proposed。
    - reject（指定 staged 内序号）→ 丢弃，不入库。
    - 确认后清理该任务 staged 行；超时降级：detected_at 超 review_timeout_minutes
      仍被 review → 标注 timed_out（自动放行，不阻塞）。
    - 落库 + 摘除 staged 在**单次持锁事务**内完成（task-lessons-review-single-transaction）：
      消除并发窗口，重跑 review 不产生重复条目。
    - 落库失败 → 保留 staged + deferred 登记；回写失败 → staged_write_skipped 留痕。
    - 锁获取失败 → 整体跳过，返回 lock_skipped 提示（不静默降级）。
    Returns: {promoted:[...], rejected:[...], timed_out:bool, 可选 deferred / hint /
        staged_write_skipped / staged_hint / lock_skipped}
    """
    reject_set = set(reject_indices or [])
    result = _review_task_transaction(
        orchd_dir, task_id=task_id, approve_all=approve_all,
        reject_indices=reject_set,
    )
    if result.get("lock_failed"):
        return {
            "promoted": [], "rejected": [], "timed_out": False,
            "lock_skipped": True,
            "hint": "lessons 锁获取失败（并发冲突），review 未执行，请稍后重试",
        }
    return result


def resolve_lesson(
    orchd_dir: Path | str, *, lesson_id: str, approve: bool
) -> dict[str, Any]:
    """lesson resolve：人工确认信任分级（设计 §7/§9）。

    approve → verified（正式触发）；reject → archived（不再触发）。
    """
    path = _lib_path(orchd_dir)
    rows = _read_jsonl(path)
    target = next((r for r in rows if r.get("id") == lesson_id), None)
    if target is None:
        raise OrchdError(ErrorCode.E007, f"lesson '{lesson_id}' 不存在",
                         [{"lesson_id": lesson_id}])
    target["status"] = "verified" if approve else "archived"
    if not _rewrite_jsonl(path, rows, _lock_path(orchd_dir)):
        return {"resolved": False, "skipped": True,
                "reason": "lesson 写入因并发冲突跳过，不影响主流程"}
    return {"resolved": True, "id": lesson_id, "status": target["status"]}


def archive_lesson(orchd_dir: Path | str, *, lesson_id: str) -> dict[str, Any]:
    """lesson archive：手动归档（不再触发，设计 §7/§9）。"""
    path = _lib_path(orchd_dir)
    rows = _read_jsonl(path)
    target = next((r for r in rows if r.get("id") == lesson_id), None)
    if target is None:
        raise OrchdError(ErrorCode.E007, f"lesson '{lesson_id}' 不存在",
                         [{"lesson_id": lesson_id}])
    target["status"] = "archived"
    if not _rewrite_jsonl(path, rows, _lock_path(orchd_dir)):
        return {"archived": False, "skipped": True,
                "reason": "lesson 写入因并发冲突跳过，不影响主流程"}
    return {"archived": True, "id": lesson_id}


# ── 查询：list / show ──────────────────────────────────────────────────────────
def list_lessons(
    orchd_dir: Path | str,
    *,
    status: str | None = None,
    trigger: str | None = None,
    staged: bool = False,
    all: bool = False,
) -> list[dict[str, Any]]:
    """查看 lesson 库（设计 §7）。

    staged=True → 看暂存区；否则看主库。status/trigger 过滤（主库）。
    """
    if staged:
        rows = _read_jsonl(_staged_path(orchd_dir))
        return [
            {
                "task_id": r.get("task_id"),
                "trigger_key": (r.get("trigger") or {}).get("key"),
                "symptom": r.get("symptom"),
                "resolved": r.get("resolved"),
                "severity": r.get("severity"),
                "conflicts_with": r.get("conflicts_with"),
            }
            for r in rows
        ]
    rows = _read_jsonl(_lib_path(orchd_dir))
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if trigger:
        rows = [r for r in rows if (r.get("trigger") or {}).get("key") == trigger]
    return [
        {
            "id": r.get("id"),
            "trigger_key": (r.get("trigger") or {}).get("key"),
            "symptom": r.get("symptom"),
            "status": r.get("status"),
            "severity": r.get("severity"),
            "hits": r.get("hits", 0),
        }
        for r in rows
    ]


def show_lesson(orchd_dir: Path | str, *, lesson_id: str) -> dict[str, Any] | None:
    """查看完整条目（完整 solution，超出注入摘要，设计 §7/§8.5）。"""
    rows = _read_jsonl(_lib_path(orchd_dir))
    return next((r for r in rows if r.get("id") == lesson_id), None)
