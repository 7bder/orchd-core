"""``orchd.doctor`` 会话锁 / 入侵锁 / git-tracked 锁域（W2-D4b，file-split-program 20261015 §4.3）。

职责：锁相关残留的只读判定——孤儿会话锁（``_detect_orphan_session_locks``）、活跃持锁者
（``_live_session_lock_holders``）、僵尸会话（``_detect_zombie_sessions``）、残留入侵锁
（``_detect_residual_intake_locks``）、第三方与 orchd 锁名判定（``_is_third_party_lock_name`` /
``_is_orchd_lock_path``）与 git 跟踪锁（``_detect_git_tracked_locks``）。

承载 10 个顶层定义（7 函数 + 3 随域常量）；函数体与搬迁前基线**逐字一致**（AST 等价性与
字节级判据见 tests/test_doctor_pkg.py）。

依赖方向：标准库（``fnmatch`` / ``json`` / ``pathlib`` / ``time`` / ``typing``）+
``orchd.doctor.runtime``（``_LOCK_FILE_PATTERNS`` / ``_run_git``）；对 ``orchd.lockfile`` /
``orchd.ledger`` 保持**函数级惰性导入**（``ExclusiveFileLock`` / ``read_locked_text`` /
``is_session_expired`` / ``session_ttl_minutes`` / ``_INTAKE_LOCK_TIMEOUT``，搬迁不改取用方式，
保持 monkeypatch 补丁面）。本模块**不得**导入 ``orchd.doctor`` 聚合入口。
"""

from __future__ import annotations

import fnmatch
import json
import time
from pathlib import Path
from typing import Any

from orchd.doctor.runtime import _LOCK_FILE_PATTERNS, _run_git


# 第三方生态锁文件白名单（DR-1）：这些是**项目源码资产**，与 orchd 运行时无关，
# doctor 绝不 untrack / 删除（原实现按 ``*.lock`` 全量判定，会把它们越界销毁）。
_THIRD_PARTY_LOCK_NAMES = frozenset({
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "yarn.lock",
    "composer.lock",
    "packages.lock.json",
    "flake.lock",
    "bun.lock",
})

# 残留 session runtime 文件模式（sessions/ 目录下的 JSON 文件）。
# 这些是 session 心跳/状态记录。**判死口径不在此处定义**：单一真源是
# ``ledger.is_session_expired`` / ``session_ttl_minutes``（见 _detect_zombie_sessions
# 的 docstring）。此处曾自带一个 1800 秒（30 分钟）的死 TTL 常量并按文件 mtime
# 判死，与 ledger 的默认 1440 分钟相差 48 倍 ⇒ 会删掉账本仍判活的会话身份文件。
_SESSION_FILE_PREFIX = "sessions/"
_SESSION_FILE_SUFFIX = ".json"


def _detect_orphan_session_locks(project_root: Path,
                                 orchd_dir: Path) -> list[dict[str, Any]]:
    """检测孤儿 session 锁文件（无对应活跃 worktree 的 .session-gate-*.lock）。

    ``.session-gate-<task-id>.lock`` 在 claim 时创建（见 session_lock 的
    worktree 维度锁），任务完成后应被清理。若锁文件存在但对应任务 worktree
    不存在，则属于孤儿锁，可安全删除。

    判据修正（DR-2，2026-09-15）：旧实现 glob ``session-gate-*.lock`` **缺前导点**，
    与真实文件名 ``.session-gate-*.lock`` 永不匹配 → 检测恒空属死代码；本函数改为
    按 ``_LOCK_FILE_PATTERNS`` 中的真实模式（含前导点）glob，并兼容历史无点名称。

    双层活性判据（DR-7 配套）：
    ① worktree 维度——目录存在且有 ``.git`` → 视为活跃；
    ② flock 维度——对候选做 flock 探活，仍被持有（活跃会话）→ 不报残留。
    删除前的复检见 :func:`doctor_fix` / :func:`auto_clean`（账本锁内二次探活）。
    """
    residues: list[dict[str, Any]] = []
    if not orchd_dir.is_dir():
        return residues

    candidates: list[Path] = []
    for pattern in (".session-gate-*.lock", "session-gate-*.lock"):
        candidates.extend(p for p in sorted(orchd_dir.glob(pattern))
                          if p.is_file())
    if not candidates:
        return residues

    from orchd.lockfile import ExclusiveFileLock

    for lock_file in sorted(set(candidates)):
        # 从文件名提取任务 ID: .session-gate-task-xxx.lock → task-xxx
        name = lock_file.name.lstrip(".")
        if not (name.startswith("session-gate-") and name.endswith(".lock")):
            continue
        task_id = name[len("session-gate-"):-len(".lock")]
        # ① worktree 维度：对应任务 worktree 存在且有效 → 锁可能是活跃的
        worktree_path = (project_root.parent /
                         task_id if task_id.startswith("task-") else None)
        if (worktree_path is not None and worktree_path.is_dir()
                and (worktree_path / ".git").exists()):
            continue
        # ② flock 维度：仍被持有 = 有活跃持有者（会话/进程未退出）→ 不是孤儿
        try:
            if ExclusiveFileLock(lock_file).check().get("held"):
                continue
        except Exception:
            continue  # 无法判定持有态时保守跳过（不误报、不误删）
        residues.append({
            "path":
            str(lock_file),
            "type":
            "orphan_session_lock",
            "reason":
            (f"session 锁 {lock_file.name} 无对应活跃 worktree 且无 live flock"),
            "action":
            "delete",
        })

    return residues


def _live_session_lock_holders(runtime_dir: Path) -> tuple[set[str], bool]:
    """返回 ``(被 live flock 持有的 session_id 集合, 是否存在身份读不出的活锁)``。

    口径同 :func:`_detect_orphan_session_locks` 的 ②（``ExclusiveFileLock.check()``）
    与 :func:`_detect_residual_intake_locks`（「被 live flock 持有 = 活跃，非残留」）：
    flock 由内核托管，进程退出/崩溃即释放，是强于任何时间戳的活性信号——持锁的会话
    必然活着，其身份文件不得删。

    持有者身份取自锁文件 JSON 的 ``session_id``（写入方
    ``session_lock._prepare_session_lock_payload``）。不能改用
    ``session_lock_check(orchd_dir)``：它按调用方 worktree 解析出**单一**锁名
    （``_get_session_lock_path`` 会给 linked worktree 加 ``.session-<wt>.lock`` 后缀），
    而本函数是共享账本根上的全局巡检，须覆盖所有 worktree 的会话锁。
    ``ExclusiveFileLock.check()`` 自身只返回 ``{held, by_current_process, depth}``，
    不含身份，故持锁与读身份两步分开做。

    读身份必须先走 ``read_locked_text``（复用本进程持锁 fd）：Windows 下 msvcrt 字节锁
    会阻止新句柄读取，直读恒失败（同 ``session_lock.session_lock_check`` 的处置）。
    仍读不出（= **他进程**持活锁，身份不可知）时返回 ``unknown=True``，调用方据此
    整体放弃本轮僵尸判定：无法区分「死会话的文件」与「那个活会话的文件」，而
    ``zombie_session`` 属真删档，宁可漏清不可误删。

    锁名取自 doctor 自有的 ``_LOCK_FILE_PATTERNS``（本模块「orchd 自有运行时锁」命名的
    单一事实源，:func:`_is_orchd_lock_path` 同源消费），过滤出会话锁并排除 gate 锁
    （``.session-gate-*.lock`` / ``.session.gate.lock`` 属并发闸门、不登记 session_id）。
    不从 ``gitops.session_lock`` 取常量：那里只有基础名 ``.session.lock``，覆盖不到
    linked worktree 的 ``.session-<wt>.lock``，且会给依赖图新增一条
    doctor → gitops.session_lock 的跨层边（tests/contract/test_dependency_direction.py）。
    含前导点的真实命名与历史无点名称都要扫（同 :func:`_detect_orphan_session_locks`
    的兼容约定）：漏扫一把活锁的代价是误删一个活会话的身份文件，正是本函数要防的事。
    """
    holders: set[str] = set()
    unknown = False
    try:
        from orchd.lockfile import ExclusiveFileLock, read_locked_text

        patterns = tuple(
            pat for pat in _LOCK_FILE_PATTERNS
            if pat.lstrip(".").startswith("session")
            and not pat.lstrip(".").startswith(("session-gate", "session.gate"))
        )
        # 含前导点的真实命名 + 历史无点名称，两种都扫（同
        # _detect_orphan_session_locks 的约定）：漏扫一把活锁 = 误删一个活会话的身份文件。
        globs = {g for pat in patterns for g in (pat, pat.lstrip("."))}
        candidates = sorted({p for pat in globs for p in runtime_dir.glob(pat)})
        for lock_file in candidates:
            if lock_file.name.lstrip(".").startswith(("session-gate", "session.gate")):
                continue
            if not lock_file.is_file():
                continue
            try:
                if not ExclusiveFileLock(lock_file).check().get("held"):
                    continue
                content = read_locked_text(lock_file)
                if content is None:
                    content = lock_file.read_text(encoding="utf-8")
                data = json.loads(content)
            except Exception:
                unknown = True
                continue
            session_id = data.get("session_id") if isinstance(data, dict) else ""
            if session_id:
                holders.add(str(session_id))
            else:
                unknown = True
    except Exception:
        return set(), True
    return holders, unknown


def _detect_zombie_sessions(runtime_dir: Path) -> list[dict[str, Any]]:
    """检测僵尸 session runtime 文件（``sessions/*.json``）。

    判死口径**单一真源在 ledger**：:func:`orchd.ledger.is_session_expired`
    （``last_seen + session_ttl_minutes()``，默认 1440 分钟、``ORCHD_SESSION_TTL_MIN``
    可覆盖），与 ``session current``（ledger.py:867/:917）和 watchdog 的 stale 判定
    （report.py:986）同源。

    此前本函数自带 1800 秒（30 分钟）死 TTL 常量且只看文件 **mtime**，与 ledger 的
    1440 分钟相差 48 倍，实测后果：一个空闲超 30 分钟、账本仍判活的会话，其身份文件被
    自动清洁删除（``zombie_session`` ∈ ``_AUTO_CLEAN_TYPES``），随后引擎命令返回
    E033 ``session_identity_missing``，而账本里该 session 的 CLAIMED 事件仍在。
    mtime **不再参与判死**：它只被写命令刷新（``_touch_session_last_seen``），长只读
    阶段（跑全量测试、复核取证）会让活会话的 mtime 老化，是假信号。

    活性判据（任一成立即非残留，顺序 = 信号强度）：

    1. 该 session 正持有任一 ``.session*.lock`` 的 live flock
       （:func:`_live_session_lock_holders`）；
    2. ``active`` 为真且 :func:`is_session_expired` 判未过期——``last_seen`` 缺失/非法时
       ledger 保守判未过期，此处沿用（缺字段不构成删除依据）。

    存在身份读不出的活锁（他进程持锁，Windows 字节锁阻止读 JSON）时**整轮放弃判定**：
    无法区分死会话文件与该活会话的文件，而本类型属真删档（宁漏清不误删）。

    ``active`` 显式为假 = 会话已正常 ``end``，runtime 无后续用途（账本留有审计轨迹），
    判为可回收。读不出/解析不了的文件一律跳过：无法判定之物不删。
    """
    residues: list[dict[str, Any]] = []
    # 目录名与后缀取自 _SESSION_FILE_PREFIX / _SESSION_FILE_SUFFIX（单一事实源，
    # 消除散落字面量——两常量此前为未引用死常量，DR-12）。
    sessions_dir = runtime_dir / _SESSION_FILE_PREFIX
    if not sessions_dir.is_dir():
        return residues

    from orchd.ledger import is_session_expired, session_ttl_minutes

    holders, holder_unknown = _live_session_lock_holders(runtime_dir)
    if holder_unknown:
        return residues
    ttl_minutes = session_ttl_minutes()
    for session_file in sorted(sessions_dir.glob(f"*{_SESSION_FILE_SUFFIX}")):
        if not session_file.is_file():
            continue
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("session_id") or session_file.stem)
        if session_id in holders:
            continue
        active = bool(data.get("active", True))
        if active and not is_session_expired(data):
            continue
        why = (
            "active=false（会话已 end，runtime 可回收）"
            if not active
            else f"last_seen 超 TTL（{ttl_minutes} 分钟，口径 = ledger.is_session_expired）"
        )
        residues.append({
            "path": str(session_file),
            "type": "zombie_session",
            "reason": f"session 文件 {session_file.name} 已判死：{why}",
            "action": "delete",
        })

    return residues


def _detect_residual_intake_locks(orchd_dir: Path) -> list[dict[str, Any]]:
    """检测残留 intake 标记（.intake.lock 无 live flock 且已超时）。

    .intake.lock 是准入锁文件，正常 acquire/release 不删除文件（task-intake-lock-path-fix
    AC5：准入写释放后留下的新鲜标记不得立即判残留，否则卫生门禁每次准入写后闪红约 120s）。
    判据与 ``ledger.intake_lock_check`` 的 timeout 语义对齐：仅当无 live flock 持有
    **且**标记年龄 >= ``_INTAKE_LOCK_TIMEOUT``（默认 120s）时才算残留；新鲜标记
    （刚释放，flock 已放但文件保留）返回空。超时残留仍可被 ``intake_lock_check``
    自动清除（AC3 timeout_cleaned 语义不回归）。

    时间源优先用标记内 JSON ``timestamp``（acquire 写入的诊断标记），缺失/非法时
    回退文件 mtime；均不可得时保守跳过（不误报）。

    已释放标记（task-intake-lock-released-marker）：``released: true`` 的标记是
    ``intake_lock_release`` 显式改写的留档（无 live flock 属正常态）→ 直接判非残留，
    不产生 residue 项；无该字段的旧格式标记保持原判据（向后兼容）。
    """
    residues: list[dict[str, Any]] = []
    if not orchd_dir.is_dir():
        return residues

    # 检查运行时根的 .intake.lock（调用方已解析到共享账本根，container/flat 兼容）
    intake_lock = orchd_dir / ".intake.lock"
    if not intake_lock.is_file():
        return residues
    try:
        from orchd.lockfile import ExclusiveFileLock
        held = ExclusiveFileLock(intake_lock).check().get("held", False)
    except Exception:
        return residues  # 无法判定持有态时保守跳过，避免误报
    if held:
        return residues  # 被 live flock 持有 = 活跃锁，非残留

    try:
        from orchd.ledger import _INTAKE_LOCK_TIMEOUT

        timeout_s = float(_INTAKE_LOCK_TIMEOUT)
    except Exception:
        timeout_s = 120.0
    age_s: float | None = None
    try:
        content = intake_lock.read_text(encoding="utf-8")
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("released") is True:
                # 已释放标记（task-intake-lock-released-marker）：intake_lock_release
                # 在 flock 释放后显式改写为 released=true 的**留档**标记，「无 live
                # flock」正是其正常态 → 不是残留（不进 auto_clean.manual_notice、
                # 不被卫生门禁判 FAIL）。无 released 字段的旧格式标记保持现判据。
                return residues
            ts = float(data.get("timestamp", 0))
            if ts > 0:
                age_s = time.time() - ts
        except (ValueError, TypeError, AttributeError):
            pass
    except (OSError, IOError):
        return residues
    if age_s is None:
        try:
            age_s = time.time() - intake_lock.stat().st_mtime
        except OSError:
            return residues
    if age_s < timeout_s:
        return residues  # 新鲜标记：准入写刚释放，门禁不闪红
    residues.append({
        "path":
        str(intake_lock),
        "type":
        "intake_lock",
        "reason": (".intake.lock 无 live flock 且已超时 "
                   f"（age {age_s:.1f}s >= {timeout_s:.0f}s，进程已退出）"),
        "action":
        "delete",
    })

    return residues


def _is_third_party_lock_name(name: str) -> bool:
    """是否为第三方生态锁文件名（项目源码资产，doctor 绝不 untrack / 删除）。"""
    return name in _THIRD_PARTY_LOCK_NAMES


def _is_orchd_lock_path(file_path: Path, project_root: Path) -> bool:
    """是否为 orchd **自有**运行时锁（DR-1 判据收敛）。

    两条判据任一成立即认定：
    ① 文件名命中 ``_LOCK_FILE_PATTERNS``（orchd 锁命名白名单）；
    ② 相对路径中任一级目录以 ``.orchd`` 开头（``.orchd/`` / ``.orchd-runtime/``
       等 orchd 自有目录下的锁一律视为运行时残留）。

    第三方生态锁（``_THIRD_PARTY_LOCK_NAMES``）恒排除：``poetry.lock`` /
    ``Cargo.lock`` 等是**项目源码资产**，原实现按 ``*.lock`` 全量判定会把它们
    ``git rm --cached`` + 删文件（越界破坏，2026-09-14 审核 DR-1）。
    """
    if _is_third_party_lock_name(file_path.name):
        return False
    if any(
            fnmatch.fnmatchcase(file_path.name, pat)
            for pat in _LOCK_FILE_PATTERNS):
        return True
    try:
        rel_parts = file_path.relative_to(project_root).parts
    except ValueError:
        rel_parts = file_path.parts
    return any(part.startswith(".orchd") for part in rel_parts[:-1])


def _detect_git_tracked_locks(project_root: Path) -> list[dict[str, Any]]:
    """检测已误提交入 git 的 **orchd 自有**锁文件（DR-1 判据收敛）。

    判定：``git ls-files --cached`` 中已跟踪、且路径命中 :func:`_is_orchd_lock_path`
    （orchd 锁命名白名单或 ``.orchd*`` 自有目录）的 ``*.lock`` 文件。仓库根第三方
    生态锁与普通 ``*.lock`` 源码文件**不在此列**（原实现按 ``*.lock`` 全量判定，
    会把 ``poetry.lock`` 等源码资产 untrack + 删文件，属越界破坏）。

    命中的文件应先从 git 索引移除（``git rm --cached``）再删除；untrack 与删除之间
    是**未提交的半修状态**，须后续 commit 收口——故每项附
    ``follow_up="need_commit_after_untrack"``，由 :func:`doctor_fix` 在清理结果与
    摘要中显式提示（DR-11）。
    """
    residues: list[dict[str, Any]] = []
    # git ls-files 列出追踪的 .lock 文件（判据在白名单侧收敛，不靠 pathspec）
    result = _run_git(project_root, ["ls-files", "--cached", "--", "*.lock"])
    if result.returncode != 0:
        return residues

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # 只处理项目根目录下的 .lock 文件（不在 .git/ 内）
        file_path = Path(project_root) / line
        if ".git" in file_path.parts:
            continue
        if not _is_orchd_lock_path(file_path, Path(project_root)):
            continue  # 非 orchd 自有锁（第三方生态锁 / 普通 *.lock）：绝不触碰
        if file_path.is_file():
            residues.append({
                "path":
                str(file_path),
                "type":
                "git_tracked_lock",
                "reason": (f"orchd 锁文件 {line} 已被误提交入 git（运行时临时文件不应追踪）"),
                "action":
                "git_rm_cached_then_delete",
                "follow_up":
                "need_commit_after_untrack",
            })

    return residues
