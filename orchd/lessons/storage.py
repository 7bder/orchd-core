"""运行时目录解析与 jsonl 读写原语（路径 + 读/追加/重写，锁与原子写）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from orchd.errors import OrchdError
from orchd.lockfile import ExclusiveFileLock
from .constants import LOCK_TIMEOUT_S


# ── 路径解析 ──────────────────────────────────────────────────────────────────
def _resolve_runtime(orchd_dir: Path | str) -> Path:
    """解析 lesson 运行时根目录（与 ledger 同根，设计 §6.3）。

    复用引擎 ``resolve_store_dir`` 作为唯一事实源：container 布局落到
    ``<容器>/.orchd-runtime/``，flat 布局落到 ``orchd_dir`` 本身（与 _ledger.jsonl
    同层）。任一解析失败 best-effort 回退 ``orchd_dir``。
    """
    try:
        from orchd.ledger import resolve_store_dir

        return Path(resolve_store_dir(Path(orchd_dir)))
    except Exception:
        return Path(orchd_dir)


def _lib_path(orchd_dir: Path | str) -> Path:
    return _resolve_runtime(orchd_dir) / "lessons.jsonl"


def _staged_path(orchd_dir: Path | str) -> Path:
    return _resolve_runtime(orchd_dir) / "lessons.staged.jsonl"


def _lock_path(orchd_dir: Path | str) -> Path:
    return _resolve_runtime(orchd_dir) / "lessons.lock"


# ── JSONL 读写（best-effort + 并发锁）─────────────────────────────────────────
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，跳过空行与损坏行（含不完整的末行，设计 §6.3）。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, json.JSONDecodeError):
            # 不完整/损坏行：跳过（末行写一半进程崩溃场景）
            continue
    return out


def _append_line_unlocked(path: Path, entry: dict[str, Any]) -> bool:
    """单行 JSONL 追加（无锁写原语，task-lessons-review-write-seam）。

    调用方须已持有 lessons 锁（ExclusiveFileLock）。行编码（json.dumps +
    "\\n" 追加写）与加锁版 :func:`_append_jsonl` 同源，是全仓唯一的追加实现。

    Returns:
        True 写入成功；False IO 失败。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError:
        return False


def _write_rows_unlocked(path: Path, rows: list[dict[str, Any]]) -> bool:
    """整体覆写 JSONL（无锁写原语，task-lessons-review-write-seam）。

    调用方须已持有 lessons 锁（ExclusiveFileLock）。行编码与加锁版
    :func:`_rewrite_jsonl` 同源，是全仓唯一的覆写实现。持锁（独立锁文件
    lessons.lock）期间整体覆写「数据文件」path：锁 fd 属于锁文件本身，
    不可用于写数据；path 为不同文件，无 Windows msvcrt 字节锁的跨句柄冲突问题。

    Returns:
        True 成功；False IO 失败。
    """
    try:
        from orchd.ledger import _write_text_atomic

        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        _write_text_atomic(path, content)
        return True
    except OSError:
        return False


def _append_jsonl(path: Path, obj: dict[str, Any], lock_path: Path) -> bool:
    """追加一行 JSONL（持 ExclusiveFileLock，best-effort 超时跳过）。

    加锁 + 调无锁原语 :func:`_append_line_unlocked`（嵌套持锁安全：
    ExclusiveFileLock 进程级可重入，同进程同路径 depth+1，不死锁）。

    Returns:
        True 写入成功；False 锁超时跳过（不阻塞主流程）。
    """
    lock = ExclusiveFileLock(lock_path)
    try:
        lock.acquire(blocking=False, timeout_s=LOCK_TIMEOUT_S)
    except OrchdError:
        return False
    try:
        return _append_line_unlocked(path, obj)
    finally:
        lock.release()


def _rewrite_jsonl(path: Path, rows: list[dict[str, Any]], lock_path: Path) -> bool:
    """整体重写 JSONL（持 ExclusiveFileLock，互斥写，设计 §6.3）。

    加锁 + 调无锁原语 :func:`_write_rows_unlocked`（嵌套持锁安全，同上）。

    Returns:
        True 成功；False 锁超时跳过。
    """
    lock = ExclusiveFileLock(lock_path)
    try:
        lock.acquire(blocking=False, timeout_s=LOCK_TIMEOUT_S)
    except OrchdError:
        return False
    try:
        return _write_rows_unlocked(path, rows)
    finally:
        lock.release()
