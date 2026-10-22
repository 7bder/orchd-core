"""orchd intake 准入锁域（file-split-program W1-L4，2026-10-15）。

本模块自 ``orchd/ledger/__init__.py`` 整块搬出 intake 准入文件锁的全部实现（9 函数 + 5 私有
常量/注册表，语句级等价，零逻辑改动）：``.intake.lock`` 的路径解析、超时残留回收、持有探测、
可重入获取、引用计数释放、孤儿回收与显式清理。

依赖方向：顶层只依赖标准库、``orchd.lockfile``（``ExclusiveFileLock`` / ``read_locked_text``）
与 ``orchd.errors``；对 ``_flock_op``（lockfile 的低层 flock 原语）与 ``resolve_store_dir``
（paths 域）的引用一律用**函数级惰性导入** ``from orchd.ledger import ...``：① 避免与聚合入口
``orchd/ledger/__init__.py`` 形成加载期循环；② 保持「调用点解析」语义——既有探针
``monkeypatch.setattr(orchd.ledger, "_flock_op", ...)``（tests/test_intake_lock_path.py:207/233）
与 ``monkeypatch.setattr("orchd.ledger.resolve_store_dir", ...)`` 的补丁面不变。
``ExclusiveFileLock`` / ``read_locked_text`` 保持模块级导入：既有补丁均为类级或不存在。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock, read_locked_text


# ------------------------------------------------------------------
# 准入/公共文件锁（task-intake-file-lock）
# 锁维度盘点（task-audit-lock-residue-reclaim AC1）：intake 准入写锁
# ``.intake.lock`` 落于共享账本根（resolve_store_dir，container/flat 兼容）。
# 路径解析：ORCHD_HOME 重定向 > container 布局 ``<容器>/.orchd-runtime/`` > flat
# 回退 ``orchd_dir``（intake_lock_path）。生命周期：acquire（阻塞+超时 flock + 写
# 诊断标记）→ release（引用计数归零释放 flock，**不 unlink**，文件保留）→ 无 live
# flock 时由 intake_lock_check 判未持有；长时间未用的超时残留标记由
# intake_lock_check 自动清除（AC3）。迁移孤儿：flat→container 后旧路径
# ``<main>/.orchd/.intake.lock`` 由 reclaim_orphan_intake_locks 在 intake 路径回收（AC2）。
# ------------------------------------------------------------------
# 背景：_master.json / IDEAS.md / ROADMAP.md 是 git 跟踪的全局文件、无进程级锁，
# 与账本文件（被 Store.acquire_lock()) 的 flock 串行不对等。两个 agent 并行准入
# （intake/amend）同时改写这几个文件会互相覆盖 / 全量 amend 撞车。这里提供一把
# **独立快捷锁** ``.intake.lock``，落于共享账本根（resolve_store_dir，container/flat
# 兼容），只用进程内 flock 互斥 + 超时判定，**不**复用账本 Store 锁——从而保证并行
# claim/done（账本锁）不被一次 amend 阻塞。

_INTAKE_LOCK_FILENAME = ".intake.lock"
# 准入写最长持有锁的超时（秒）。超过视为僵死锁，watchdog 可巡检/告警（task-admission-lock-engine）。
_INTAKE_LOCK_TIMEOUT = 120
# 准入写获取锁的阻塞等待上限（秒）。超过即抛 E012 并给出明确处置指引，不无限等待。
# 可用环境变量 ORCHD_INTAKE_LOCK_WAIT_SECS 覆盖（task-admission-lock-engine：A 项）。
_INTAKE_LOCK_WAIT_SECS = 60

# 进程内准入锁注册表（task-admission-lock-engine 修复）：按规范锁路径持有
# (ExclusiveFileLock, refcount)，实现同一进程内嵌套获取的可重入，避免
# init → bootstrap_container 对同一 .intake.lock 的自死锁；跨进程仍依赖底层
# flock 真实互斥（并发 agent 阻塞等待）。
# task-concurrent-amend-lost-update：条目另持 threading.RLock（跨线程互斥；
# 同线程重入放行）。注册表自身的 get-or-create 由下方 guard 串行化（短临界，
# 无阻塞操作）。
_intake_lock_registry: dict = {}
_intake_lock_registry_guard = threading.Lock()


def _intake_lock_wait_secs() -> float:
    """准入写获取锁的阻塞等待上限（秒）。

    默认 :data:`_INTAKE_LOCK_WAIT_SECS`（60s）；环境变量
    ``ORCHD_INTAKE_LOCK_WAIT_SECS`` 可覆盖（须为正数）。
    """
    env = os.environ.get("ORCHD_INTAKE_LOCK_WAIT_SECS")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return float(_INTAKE_LOCK_WAIT_SECS)


def intake_lock_path(orchd_dir: Path) -> Path:
    """返回准入锁文件路径（共享账本根下，container/flat 兼容）。"""
    from orchd.ledger import resolve_store_dir
    return resolve_store_dir(orchd_dir) / _INTAKE_LOCK_FILENAME


def _reclaim_stale_lock_atomically(
    lock_path: Path, timeout_s: float
) -> dict[str, Any]:
    """以原子序清理陈旧准入锁标记（L-7），返回 ``{"status": ...}``。

    序列：``open → 非阻塞 flock → 经该 fd 重读确认仍陈旧 → unlink（持锁删除）
    → 释放``。要点：

    - **拿不到 flock 即让位**（返回 ``status="held"``）：首判据与本次 flock 之间
      他人可能刚获取活锁，此时绝不删除（旧实现 check→read→unlink 三步非原子，
      会删掉活锁造成 unlink-alias：文件消失、别人新建同名文件 → 双方同时持锁）；
    - **持锁重读**：以本 fd 独占时的内容为准，读到的是「此刻没有其它写者」的标记；
    - **持锁删除**：POSIX 下 flock 持有者删除自身锁文件是原子的（无 unlink-alias）；
    - **Windows 回退**：本进程句柄占用时 ``unlink`` 被系统拒绝（无 FILE_SHARE_DELETE），
      故在释放句柄后重试一次——该平台「他人持锁 = 其打开句柄存在 = 删除被系统拒绝」，
      删除动作由 OS 保证不会误删活锁（无需额外复检）。

    Returns:
        ``{"status": "held"|"fresh"|"unreadable"|"cleaned"|"cleanup_failed"}``；
        ``cleaned`` 附带 ``age_s`` 与 ``cleanup_result``（含是否真正删除文件）。
    """
    from orchd.ledger import _flock_op
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError:
        return {"status": "unreadable"}
    owned = False
    stale_age: float | None = None
    removed = False
    try:
        try:
            _flock_op(fd, "lock_nb")
            owned = True
        except (OSError, IOError):
            return {"status": "held"}
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536).decode("utf-8", errors="replace")
        except OSError:
            return {"status": "unreadable"}
        try:
            data = json.loads(raw)
            ts = float(data.get("timestamp", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"status": "unreadable"}
        age = time.time() - ts
        if age < timeout_s or not data.get("agent_id"):
            return {"status": "fresh"}
        stale_age = age
        try:
            lock_path.unlink()
            removed = not lock_path.exists()
        except OSError:
            removed = False  # Windows：本进程句柄占用 → 走 finally 内回退
    finally:
        if owned:
            try:
                _flock_op(fd, "unlock")
            except (OSError, IOError):
                pass
        try:
            os.close(fd)
        except OSError:
            pass
        if owned and stale_age is not None and not removed:
            try:
                lock_path.unlink()
                removed = not lock_path.exists()
            except OSError:
                removed = False
    return {
        "status": "cleaned" if removed else "cleanup_failed",
        "age_s": round(stale_age, 1) if stale_age is not None else None,
        "cleanup_result": {"cleared": removed, "path": str(lock_path)},
    }


def intake_lock_check(
    orchd_dir: Path, timeout_s: float = _INTAKE_LOCK_TIMEOUT
) -> dict[str, Any]:
    """检查准入锁状态（不阻塞），供 watchdog / caller 判定是否僵死。

    以 :class:`ExclusiveFileLock` 的 flock 探测为权威：无 live flock 持有即未锁；
    被持有则尽量读诊断标记（agent_id/timestamp）。task-audit-lock-residue-reclaim
    （AC3）：未持有但存在**超时残留标记**（无 live flock 的陈旧标记）时**自动清除**
    陈旧标记并返回 ``reason="timeout_cleaned"``——陈旧标记不再产生
    ``reason="timeout"`` 误导（watchdog 曾据此误报 stale_marker），返回未锁可续获取。

    Returns:
        ``{"locked": False}`` 未被持有（无 live flock，可获取）。
        ``{"locked": False, "reason": "timeout_cleaned", "age_s",
        "cleanup_result"}`` 未被持有且残留超时标记已被自动清除。
        ``{"locked": True, "agent_id", "timestamp", "age_s"}`` 被持有，诊断标记可读。
        ``{"locked": True, "reason": "no_marker"}`` 被持有但标记不可读。
        ``{"locked": True, "reason": "held"}`` 首判据未持有、但清理前复核发现被
        并发获取（原子清理让位，不删除活锁；L-7）。
    """
    lock_path = intake_lock_path(orchd_dir)
    probe = ExclusiveFileLock(lock_path).check()
    if probe.get("held"):
        # 优先走本进程持锁 fd 读取标记（Windows msvcrt 字节锁阻止新句柄读取）
        content = read_locked_text(lock_path)
        if content is None:
            try:
                content = lock_path.read_text(encoding="utf-8")
            except (OSError, IOError):
                content = None
        if content is None:
            return {"locked": True, "reason": "no_marker"}
        try:
            data = json.loads(content)
            ts = float(data.get("timestamp", 0))
            return {
                "locked": True,
                "agent_id": data.get("agent_id") or "unknown",
                "timestamp": data.get("timestamp", str(ts)),
                "age_s": round(time.time() - ts, 1),
            }
        except (OSError, IOError, json.JSONDecodeError, ValueError, TypeError):
            return {"locked": True, "reason": "no_marker"}
    # 未被持有（probe 已确认无 live flock）：清理陈旧标记必须是**原子序**（L-7），
    # 见 _reclaim_stale_lock_atomically：拿不到 flock 即让位（返回 locked=True/held），
    # 绝不删除他人刚获取的活锁；确认陈旧才持锁删除（Windows 走释放句柄后的 OS 保护回退）。
    if not lock_path.exists():
        return {"locked": False}
    try:
        outcome = _reclaim_stale_lock_atomically(lock_path, timeout_s)
    except (OSError, IOError, ValueError, TypeError):
        return {"locked": False}
    if outcome.get("status") == "held":
        return {"locked": True, "reason": "held"}
    if outcome.get("status") == "cleaned":
        return {
            "locked": False,
            "reason": "timeout_cleaned",
            "age_s": outcome.get("age_s"),
            "cleanup_result": outcome.get("cleanup_result"),
        }
    return {"locked": False}


def intake_lock_acquire(
    orchd_dir: Path, agent_id: str, timeout_s: float | None = None
) -> dict[str, Any]:
    """获取准入写锁（ExclusiveFileLock 原语 + 线程层 RLock，双层互斥）。

    flock 为跨进程唯一互斥权威；同进程跨线程由条目 RLock 互斥（同线程重入放行）
    ——此前"同一进程即重入"把跨线程并发也放行了（线程级 amend 并发零互斥，
    task-concurrent-amend-lost-update 实测撞出 git index.lock）。无强夺接管。

    与账本 Store 锁解耦：这里锁 ``.intake.lock``，不阻塞并行 claim/done。
    多进程尝试准入写时，后到者**阻塞等待** ``timeout_s``（默认 60s，可经
    ``ORCHD_INTAKE_LOCK_WAIT_SECS`` 覆盖）——正常并发的持有者释放后即自动成功，
    **不再**"无声卡死"；仅当真正僵死（持锁进程挂起/未退出）超过等待上限才抛 E012。

    Args:
        orchd_dir: .orchd 目录。
        agent_id: 当前 agent（仅写入诊断标记，不参与互斥）。
        timeout_s: 阻塞等待上限（秒）；``None`` → :func:`_intake_lock_wait_secs`
            （默认 60s，env 可覆盖）。

    Returns:
        锁句柄 dict（传给 :func:`intake_lock_release`）。

    Raises:
        OrchdError: E012 等待 ``timeout_s`` 内仍未拿到锁（持有者僵死）。
    """
    wait = _intake_lock_wait_secs() if timeout_s is None else timeout_s
    canonical = intake_lock_path(orchd_dir).resolve()
    # 进程内可重入（task-admission-lock-engine 修复）：同一进程对同一锁路径，
    # 仅引用计数 +1，不重复 flock——避免 init → bootstrap_container 的嵌套自死锁，
    # 同时保留跨进程真实 flock 互斥（并发 agent 仍阻塞等待）。
    # task-concurrent-amend-lost-update：上述"同一进程即重入"把**跨线程**并发也
    # 放行了（同进程第二线程直接 refcount+1，不碰 flock）——线程级 amend 并发零互斥，
    # 实测撞出 git index.lock 且 master 静默丢失。故加一层与 canonical 绑定的
    # threading.RLock：跨线程阻塞等待（同 timeout_s / E012 口径），同线程重入放行
    # （嵌套调用不断言）。flock 仍是跨进程唯一互斥权威，语义不变。
    with _intake_lock_registry_guard:
        entry = _intake_lock_registry.get(canonical)
        if entry is None:
            entry = {
                "lock": ExclusiveFileLock(canonical),
                "refcount": 0,
                "threadlock": threading.RLock(),
            }
            _intake_lock_registry[canonical] = entry
        threadlock = entry["threadlock"]
    if not threadlock.acquire(blocking=True, timeout=wait):
        raise OrchdError(
            ErrorCode.E012,
            "lock_timeout: failed to acquire .intake.lock (线程级阻塞超时，"
            "同进程他线程正持有准入写锁)",
            [{
                "path": str(canonical),
                "timeout_s": wait,
                "hint": (
                    "同进程内另一线程正在执行准入写且未释放（ информацией见上）。"
                    "已阻塞等待仍未拿到锁；检查持锁线程是否僵死，不要无上限重试。"
                ),
            }],
        )
    try:
        if entry["refcount"] > 0:
            entry["refcount"] += 1
            return {
                "acquired": True,
                "agent_id": agent_id,
                "path": str(canonical),
                "_lock": entry["lock"],
                "reentrant": True,
            }
        lock = entry["lock"]
        try:
            lock.acquire(blocking=True, timeout_s=wait)
        except OrchdError as exc:
            # 注入 intake 语义，保留 E012；hint 明确化（task-admission-lock-engine：C 项）
            raise OrchdError(
                ErrorCode.E012,
                "lock_timeout: failed to acquire .intake.lock (准入写被并发 agent 持有)",
                [{
                    "path": str(canonical),
                    "timeout_s": wait,
                    "hint": (
                        "另一 agent 正在执行准入写（intake / amend / roadmap-land / idea *）。"
                        f"已阻塞等待 {round(wait)}s 仍未拿到锁。若长时间无进展，可能是其进程僵死"
                        "（卡在子进程 / 等待交互）：请检查其进程，或等待其退出"
                        "（flock 将在进程退出时由内核自动释放），不要无上限重试。"
                    ),
                }],
            ) from exc
        entry["refcount"] = 1
    except OrchdError:
        threadlock.release()
        raise
    except Exception:
        threadlock.release()
        raise
    # 诊断标记（best-effort，非互斥依据）：供 intake_lock_check 报障
    # （pass9 F15：显式 utf-8——Windows cp936 默认下含中文路径/Agent 名即乱码）
    try:
        lock.write_text(
            json.dumps({"agent_id": agent_id, "timestamp": str(time.time()),
                        "path": str(canonical)}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    # 迁移孤儿回收（task-audit-lock-residue-reclaim AC2）：账本根重定向
    # （container / ORCHD_HOME）时清理 flat→container 迁移后旧路径
    # ``<main>/.orchd/.intake.lock`` 残留，best-effort 不阻断准入。
    try:
        reclaim_orphan_intake_locks(orchd_dir)
    except Exception:
        pass
    return {"acquired": True, "agent_id": agent_id,
            "path": str(canonical), "_lock": lock}


def _mark_intake_lock_released(
    lock_path: Path | None, lock: dict[str, Any]
) -> None:
    """把准入锁标记改写为「已释放」（best-effort，绝不抛错）。

    task-intake-lock-released-marker（2026-09-15）：释放 flock 后写入
    ``{"agent_id", "released": true, "released_at", "timestamp", "path"}``，
    使 ``doctor._detect_residual_intake_locks`` 与卫生门禁一眼可判「已释放」，
    不再按「无 live flock 且 age >= 120s」报超时残留（消除常驻假红）。

    仍**不删除文件**——保住既有反 unlink-alias 设计（文件永久保留、flock 为唯一
    互斥权威）；重写失败静默降级：下一次准入写会用新鲜标记覆盖，功能不受影响。
    """
    if lock_path is None:
        return
    payload = {
        "agent_id": lock.get("agent_id") or "unknown",
        "released": True,
        "released_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": str(time.time()),
        "path": str(lock_path),
    }
    try:
        Path(lock_path).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except (OSError, IOError, TypeError, ValueError):
        pass


def intake_lock_release(lock: dict[str, Any]) -> None:
    """释放准入锁（ExclusiveFileLock 原语释放）。

    进程内可重入：仅当引用计数归零才真正 flock 释放（与 :func:`intake_lock_acquire`
    的嵌套获取配对），线程层 RLock 随之释放。**不 unlink 锁文件**：文件永久保留，flock 释放后由
    :func:`intake_lock_check` 判为未持有——避免 unlink-alias / 删后误判持锁。

    已释放标记（task-intake-lock-released-marker）：flock 释放后经
    :func:`_mark_intake_lock_released` 把标记改写为 ``released: true``，使残留检测
    与卫生门禁不再把「正常释放后留下的诊断标记」判为需人工处置的超时残留。
    互斥语义零变化：flock 仍是唯一互斥权威，标记仅为诊断/留档。
    """
    lk = lock.get("_lock")
    if lk is None:
        return
    canonical = None
    p = lock.get("path")
    if p:
        try:
            canonical = Path(p).resolve()
        except (OSError, ValueError):
            canonical = None
    entry = _intake_lock_registry.get(canonical) if canonical else None
    if entry is None:
        # 不在注册表（跨进程持锁句柄 / 异常路径）→ 直接释放底层锁，不碰注册表
        try:
            lk.release()
        except (OSError, IOError):
            pass
        _mark_intake_lock_released(canonical, lock)
        return
    entry["refcount"] -= 1
    if entry["refcount"] <= 0:
        entry["refcount"] = 0
        try:
            lk.release()
        except (OSError, IOError):
            pass
        _mark_intake_lock_released(canonical, lock)
    # 条目留注册表（不 pop）：等待线程在阻塞前已取到本 entry 引用；
    # pop 会使其工作在 detached entry 上，其 release 因查无注册而走
    # early-return 漏放 RLock——3+ 线程争用即 strand 等待者（E012）。
    # entry 体积极小（每锁路径一个）且 flock 释放后不占 fd，保留无害。
    # task-concurrent-amend-lost-update 的 2 线程用例恰好躲过此坑
    # （第二完成者后无等待者），4 线程并发 propose 实测撞出。
    # 线程层 RLock 与 acquire 一一配对（task-pass9-ledger-lock-root，pass9 F4）：
    # acquire 每次调用恰 acquire 一次 RLock（重入分支亦然，:1303 无条件获取），
    # release 必须**每次**释放一次——原先仅在 refcount 归零时释放，嵌套场景
    # （init → bootstrap_container）净漏一层计数，其他线程等满超时误报 E012
    # 而 flock 实际空闲。
    _tl = entry.get("threadlock")
    if _tl is not None:
        try:
            _tl.release()
        except Exception:
            pass


def reclaim_orphan_intake_locks(orchd_dir: Path) -> dict[str, Any]:
    """回收迁移后旧路径下的 ``.intake.lock`` 孤儿（task-audit-lock-residue-reclaim AC2）。

    flat→container 迁移后账本根从 ``<main>/.orchd/`` 变为 ``<runtime>/.orchd-runtime/``，
    flat 时代落在 ``<main>/.orchd/.intake.lock`` 的准入锁文件成为**迁移孤儿**（新路径
    在 runtime 根，旧路径不再被读写）。本函数在账本根重定向生效
    （``resolve_store_dir != orchd_dir``，container / ORCHD_HOME 重定向）时识别并清理
    旧路径残留：仅当无 live flock 持有才删除（与 :func:`intake_lock_clear` 同语义，
    防 unlink-alias 竞态）。

    Args:
        orchd_dir: 主工作树的 .orchd 目录。

    Returns:
        ``{"scanned": bool, "cleaned": [<str>], "reason": <str|None>}``：
        - ``scanned=False``：账本根未重定向（flat 未迁移），无迁移孤儿路径，零操作；
        - ``scanned=True``：已检查旧路径；``cleaned`` 列出已删除的孤儿文件路径
          （无 live flock 且旧路径存在时删除）；``reason="held"`` 表示旧路径仍被
          live flock 持有（不删除，保守跳过）。
    """
    from orchd.ledger import resolve_store_dir
    canonical = resolve_store_dir(orchd_dir)
    old_path = Path(orchd_dir) / _INTAKE_LOCK_FILENAME
    if Path(canonical).resolve() == Path(orchd_dir).resolve():
        # 账本根未重定向（flat 未迁移）→ 不存在"旧路径"概念，零操作
        return {"scanned": False, "cleaned": []}
    if not old_path.exists():
        return {"scanned": True, "cleaned": []}
    if ExclusiveFileLock(old_path).check().get("held"):
        return {"scanned": True, "cleaned": [], "reason": "held"}
    try:
        old_path.unlink()
        return {
            "scanned": True,
            "cleaned": [str(old_path)] if not old_path.exists() else [],
        }
    except OSError:
        return {"scanned": True, "cleaned": []}


def intake_lock_clear(orchd_dir: Path) -> dict[str, Any]:
    """强制清理残量准入锁（best-effort，watchdog 调用）。

    仅当无 live flock 持有该锁文件时才删除（避免 unlink-alias）；若被持有则
    返回 ``cleared=False``。正常 acquire/release 不删除文件，此处只兜底清残留。
    """
    lock_path = intake_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"cleared": False, "path": str(lock_path)}
    if ExclusiveFileLock(lock_path).check().get("held"):
        return {"cleared": False, "path": str(lock_path), "reason": "held"}
    try:
        lock_path.unlink()
        return {"cleared": True, "path": str(lock_path)}
    except OSError:
        return {"cleared": False, "path": str(lock_path)}


