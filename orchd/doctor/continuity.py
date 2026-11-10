"""``orchd.doctor`` 会话接续包检测器域（task-cont-autoclean-detectors；判据收紧
task-cont-autoclean-criteria，设计基线 docs/session-continuity-design.md §6.2）。

检测器面（全部只读：stat / iterdir / git 只读查询；处置由 auto_clean /
doctor --fix 的既有分派链执行，检测器只产出候选与理由，不直接删）：

- 过期 verify tmp：命名（``vf-*`` / ``orchd-vf-*``）+ 24h 双闸 + **活跃任务
  basetemp 引用豁免**（master ``verify_command`` 单一真源，活跃 = 账本非终态）；
- 已合并任务 ``review/``：仅 **completed** 且任务分支已并入默认分支
  （``git merge-base --is-ancestor`` / 引擎 merge 消息回溯）且合并满 7 天；
  cancelled 不按「已合并」处理；
- 旧 runtime 日志：30 天 + 保留最近 10 个 + 活跃 session/task 名引用豁免；
- 超保留期 doctor 备份桶（报告面，真删由 _prune_stale_backups 统一执行）；
- 已知构建缓存：``.orchd/`` + runtime 根 + **task worktree 兄弟目录**子树，
  深度受限；源码树 warm cache 不扫；
- 陈旧 proposal 归档：已注册未清理（classify_proposals 单一真源）∪ 未注册
  超 14 天（设计 §6.2 补口），均走 archive_proposal 手动归档通道（只归档
  不删除）；
- rescue ref 只读报告（``refs/heads/orchd/rescue/*`` 超 30 天，先报告暂不删）。

用户内容（命名不匹配的 tmp 条目、非 ``task-`` 命名的 review 条目、proposal
正文）一律不在检测面内。所有引用面（账本 / master / session runtime）不可
判定时整体跳过报告（fail-closed 宁留勿删），不反向扩大清理面。

依赖方向：标准库（``json`` / ``re`` / ``pathlib`` / ``time``）+
``orchd.doctor.runtime``（``_resolve_runtime_dir`` / ``_run_git`` /
``_unavailable_residue``，顶层）+ ``orchd.contracts.events``（终态别名，顶层）；
``orchd.ledger``、``orchd.doctor.fix.constants``、``orchd.doctor.proposals``、
``orchd.spec`` 与 ``orchd.worktree`` 保持**函数级惰性导入**（doctor 叶子域不
反向牵引账本、fix 子包、proposals 判据与 master 解析面；deps delayed 边
``doctor.continuity`` 同步登记）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from orchd.contracts.events import (
    STATUS_COMPLETED as _STATUS_COMPLETED,
    TERMINAL_STATUSES as _GHOST_TERMINAL_STATUSES,
)
from orchd.doctor.runtime import (
    _resolve_runtime_dir,
    _run_git,
    _unavailable_residue,
)


# 检测器阈值/词表（设计 §6.2：24h verify tmp / 30 天 + keep-10 日志 / 7 天合并
# 冷却 / 14 天未注册草案 / 30 天 rescue ref）。全部只读（stat / iterdir / git
# 只读查询），处置仍由 auto_clean / doctor --fix 的既有分派链执行；用户内容
# （命名不匹配的 tmp 条目、非 task-* 的 review/ 条目、proposal 正文）一律不进
# auto_clean 档。
_VERIFY_TMP_MAX_AGE_S = 24 * 3600        # verify basetemp 遗留判定阈值（24h）
_RUNTIME_LOG_MAX_AGE_S = 30 * 86400      # runtime 顶层 *.log 判旧阈值（30 天）
_RUNTIME_LOG_KEEP_NEWEST = 10            # runtime 日志保留最近 N 个
_REVIEW_MERGED_MIN_AGE_S = 7 * 86400     # review/ 证据保留：合并满 7 天
_PROPOSAL_STALE_MAX_AGE_S = 14 * 86400   # 未注册草案归档阈值（14 天）
_RESCUE_REF_MAX_AGE_S = 30 * 86400       # rescue ref 只读报告阈值（30 天）
_RESCUE_REF_REF_PREFIX = "refs/heads/orchd/rescue/"
_BUILD_CACHE_DIR_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis",
})
_BUILD_CACHE_SCAN_DEPTH = 3              # .orchd / runtime / worktree 树扫描深度上限
_VERIFY_TMP_NAME_PREFIXES = ("vf-", "orchd-vf-")
# --basetemp 取值形态（与 shared_entries._BASETEMP_RE 同语义的本地轻量正则，
# 避免叶子域反向牵引共享登记面）：``--basetemp=<v>`` / ``--basetemp <v>``，
# v 可带引号或 ``${...}`` 前缀；不展开环境，仅取尾段名参与豁免匹配。
_BASETEMP_ARG_RE = re.compile(
    r"--basetemp(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)")


def _iter_subtree_depth_limited(root: Path, depth: int):
    """深度受限的子树遍历（BFS，只产目录；OSError 静默跳过不可入项）。"""
    frontier = [root]
    current_depth = 0
    while frontier and current_depth < depth:
        next_frontier: list[Path] = []
        for directory in frontier:
            try:
                for child in sorted(directory.iterdir()):
                    if child.is_dir():
                        yield child
                        next_frontier.append(child)
            except OSError:
                continue
        frontier = next_frontier
        current_depth += 1


def _active_task_ids(project_root: Path) -> set[str] | None:
    """账本中**非终态**（活跃）任务 id 集合；账本不可读 → ``None``（fail-closed）。"""
    try:
        from orchd.ledger import Store

        state = Store(Path(project_root) / ".orchd", readonly=True).replay()
    except Exception:
        return None
    return {
        tid for tid, ts in state.items()
        if getattr(ts, "status", None) not in _GHOST_TERMINAL_STATUSES
    }


def _master_verify_commands(orchd_dir: Path) -> dict[str, str]:
    """master 登记的 ``verify_command`` 表（task_id → 命令）。

    解析路径与 proposals 域同款（``resolve_master_path_from_dir`` →
    ``load_master``，函数级惰性导入）；master 不存在（bare 项目）→ 空表
    （无引用可豁免，年龄闸独立裁决）；不可解析 → 抛异常由调用方转 fail-closed。
    """
    from orchd.spec import load_master
    from orchd.worktree import resolve_master_path_from_dir

    master_path = resolve_master_path_from_dir(orchd_dir)
    if not master_path.exists():
        return {}
    master = load_master(master_path)
    out: dict[str, str] = {}
    for task in master.tasks:
        tid = task.get("id")
        command = task.get("verify_command")
        if isinstance(tid, str) and isinstance(command, str):
            out[tid] = command
    return out


def _active_verify_basetemps(project_root: Path) -> set[str] | None:
    """活跃任务 verify 命令引用的 basetemp 豁免词表（整路径 + 尾段名，casefold）。

    账本或 master 不可读 → ``None``（fail-closed：引用面缺失时不得反向扩大
    清理面，宁留勿删）。取值不展开环境（``${TMPDIR:-/tmp}/orchd-vf-x`` 只以
    尾段名 ``orchd-vf-x`` 入表）——过度豁免方向安全（少报，绝不误删）。
    """
    active = _active_task_ids(project_root)
    if active is None:
        return None
    try:
        commands = _master_verify_commands(Path(project_root) / ".orchd")
    except Exception:
        return None
    refs: set[str] = set()
    for tid in active:
        for match in _BASETEMP_ARG_RE.finditer(commands.get(tid, "")):
            value = match.group(1).strip("\"'")
            if not value:
                continue
            normalized = value.replace("\\", "/").casefold()
            refs.add(normalized)
            refs.add(normalized.rsplit("/", 1)[-1])
    return refs


def _active_reference_tokens(project_root: Path) -> set[str] | None:
    """活跃 session/claim 的名字引用词表（活跃任务 id + 活跃 session 指纹）。

    runtime ``sessions/*.json`` 中 ``active=true`` 的会话指纹入表（不做 TTL
    复判：过度豁免方向安全——多豁免只可能少报，绝不误删）；账本不可读 →
    ``None``（fail-closed，调用方整体跳过报告）。
    """
    active = _active_task_ids(project_root)
    if active is None:
        return None
    tokens: set[str] = set(active)
    try:
        sessions_dir = (_resolve_runtime_dir(Path(project_root) / ".orchd")
                        / "sessions")
        for path in sessions_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("active"):
                fingerprint = data.get("fingerprint")
                if isinstance(fingerprint, str) and fingerprint:
                    tokens.add(fingerprint)
    except OSError:
        pass
    return tokens


def _resolve_default_branch(project_root: Path) -> str | None:
    """默认分支名（best-effort）：``origin/HEAD`` 符号引用 → 本地 main → master → None。"""
    probe = _run_git(project_root, ["symbolic-ref", "--short",
                                    "refs/remotes/origin/HEAD"])
    if probe.returncode == 0:
        name = (probe.stdout or "").strip()
        if name.startswith("origin/") and len(name) > len("origin/"):
            return name[len("origin/"):]
        if name and "/" not in name:
            return name
    for candidate in ("main", "master"):
        check = _run_git(project_root, ["show-ref", "--verify", "--quiet",
                                        f"refs/heads/{candidate}"])
        if check.returncode == 0:
            return candidate
    return None


def _merge_commit_epoch(project_root: Path, default_branch: str,
                        task_id: str) -> float | None:
    """默认分支历史中合入 ``task/<task_id>`` 的 merge 提交时间（epoch 秒）。

    消息形态与引擎 ``try_git_merge`` 的 ``git merge`` 默认消息同源
    （``Merge branch 'task/<task_id>'``，含 ``... into <branch>`` 变体）；
    ``--fixed-strings`` 子串匹配 + 收尾引号定界，天然排除 task id 前缀误命中
    （``task-x`` 不会匹配 ``task-x-2``）。无 merge 提交（ff 合并）→ ``None``。
    """
    result = _run_git(project_root, [
        "log", "-1", "--format=%ct", "--merges", "--fixed-strings",
        f"--grep=Merge branch 'task/{task_id}'", default_branch,
    ])
    if result.returncode != 0:
        return None
    try:
        return float((result.stdout or "").strip())
    except ValueError:
        return None


def _task_branch_merged_at(project_root: Path, task_id: str) -> float | None:
    """任务分支并入默认分支的时间（epoch 秒）；不可判定 → ``None``（宁留勿删）。

    三段判定：① 分支仍存在 → ``git merge-base --is-ancestor`` 判并入，未并入
    直接 ``None``；② merge 提交时间（引擎默认消息回溯，见
    :func:`_merge_commit_epoch`）；③ 分支存在但无 merge 提交（ff 合并）→ 以
    分支尖端提交时间兜底。分支已删且无 merge 提交（deleted-ff，罕见）→
    ``None``：合并时点不可证即不进清理候选。
    """
    default_branch = _resolve_default_branch(project_root)
    if default_branch is None:
        return None
    branch = f"task/{task_id}"
    tip = _run_git(project_root, ["rev-parse", "--verify", "--quiet",
                                  f"refs/heads/{branch}"])
    has_branch = tip.returncode == 0 and bool((tip.stdout or "").strip())
    if has_branch:
        ancestor = _run_git(project_root, ["merge-base", "--is-ancestor",
                                           branch, default_branch])
        if ancestor.returncode != 0:
            return None  # 存在但未并入默认分支：绝不进清理候选
    merged_at = _merge_commit_epoch(project_root, default_branch, task_id)
    if merged_at is not None:
        return merged_at
    if has_branch:
        commit_ts = _run_git(project_root, ["log", "-1", "--format=%ct",
                                            branch])
        try:
            return float((commit_ts.stdout or "").strip())
        except ValueError:
            return None
    return None


def _detect_stale_verify_tmps(project_root: Path) -> list[dict[str, Any]]:
    """过期 verify tmp：``.orchd/tmp/`` 下 verify basetemp 遗留目录/文件。

    判据（三闸，task-cont-autoclean-criteria 收紧）：命名命中 verify tmp 词表
    （``vf-*`` / ``orchd-vf-*``）**且** mtime 超过 :data:`_VERIFY_TMP_MAX_AGE_S`
    （24h）**且**不被活跃任务引用（master ``verify_command`` 的 basetemp
    路径/尾段名豁免，判据见 :func:`_active_verify_basetemps`；引用面不可判定
    时整体跳过报告——宁留勿删）。命名不匹配的条目一律跳过——可能是用户暂存
    内容，绝不进任何清理通道。处置走 delete_dir / delete（auto_clean 经
    _dispose_residual_dir / _delete_file，可回滚/复检锁）。
    """
    tmp_root = Path(project_root) / ".orchd" / "tmp"
    if not tmp_root.is_dir():
        return []
    refs = _active_verify_basetemps(project_root)
    if refs is None:
        return []  # 引用面不可判定 → 不判任何 tmp 可清（宁留勿删）
    now = time.time()
    out: list[dict[str, Any]] = []
    for entry in sorted(tmp_root.iterdir()):
        name = entry.name.lower()
        if not name.startswith(_VERIFY_TMP_NAME_PREFIXES):
            continue
        if entry.name.casefold() in refs:
            continue  # 活跃任务 basetemp 引用中：跳过
        try:
            age_s = now - entry.stat().st_mtime
        except OSError:
            continue
        if age_s < _VERIFY_TMP_MAX_AGE_S:
            continue
        out.append({
            "path": str(entry),
            "type": "stale_verify_tmp",
            "action": "delete_dir" if entry.is_dir() else "delete",
            "reason": (
                f"verify basetemp 遗留（约 {int(age_s // 3600)}h 未更新且无"
                "活跃任务引用），auto_clean 移入备份区可回滚"),
        })
    return out


def _detect_merged_review_dirs(project_root: Path) -> list[dict[str, Any]]:
    """已合并任务的 ``review/<task_id>/`` 证据目录（判据收紧：task-cont-autoclean-criteria）。

    设计 §6.2：仅 **completed**（cancelled 不按「已合并」处理——分支未并入，
    证据仍可能被返工引用）且任务分支已并入默认分支（判据见
    :func:`_task_branch_merged_at`）且合并满 :data:`_REVIEW_MERGED_MIN_AGE_S`
    （7 天）才进入候选。活跃任务、合并时点不可判定（git 不可用 / 无默认分支 /
    未并入，fail-closed 宁留勿删）、非 ``task-`` 命名的条目（用户内容）一律跳过。
    """
    review_root = Path(project_root) / "review"
    if not review_root.is_dir():
        return []
    try:
        from orchd.ledger import Store

        state = Store(Path(project_root) / ".orchd", readonly=True).replay()
    except Exception:
        return []  # fail-closed：账本不可读时不判任何 review 目录可清
    now = time.time()
    out: list[dict[str, Any]] = []
    for entry in sorted(review_root.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("task-"):
            continue
        ts = state.get(entry.name)
        if ts is None or ts.status != _STATUS_COMPLETED:
            continue
        merged_at = _task_branch_merged_at(Path(project_root), entry.name)
        if merged_at is None:
            continue
        age_s = now - merged_at
        if age_s < _REVIEW_MERGED_MIN_AGE_S:
            continue
        out.append({
            "path": str(entry),
            "type": "merged_review_dir",
            "action": "delete_dir",
            "reason": (
                f"{entry.name} 已完成并合入默认分支约 {int(age_s // 86400)} 天"
                "（completed + 已合并 + 满 7 天，设计 §6.2），review/ 证据归档"
                "面由 auto_clean 移入备份区可回滚"),
        })
    return out


def _detect_stale_runtime_logs(project_root: Path) -> list[dict[str, Any]]:
    """旧 runtime 日志：共享账本根顶层 ``*.log``（30 天 + 保留最近 10 个）。

    判据（task-cont-autoclean-criteria 收紧）：mtime 超
    :data:`_RUNTIME_LOG_MAX_AGE_S`（30 天）**且**不属最新
    :data:`_RUNTIME_LOG_KEEP_NEWEST` 个（按 mtime 降序取保留档，档内超龄也
    豁免）**且**名字不含活跃引用词（活跃任务 id / 活跃 session 指纹，判据见
    :func:`_active_reference_tokens`；引用面不可判定时整体跳过——宁留勿删）。
    账本 / checkpoint / session / 锁等活文件不匹配 ``*.log``，天然豁免；
    子目录（debug 现场等）不在本检测面。处置走 move_to_backup（回收桶，
    逐字节可回滚）。
    """
    runtime_dir = _resolve_runtime_dir(Path(project_root) / ".orchd")
    if not runtime_dir.is_dir():
        return []
    tokens = _active_reference_tokens(project_root)
    if tokens is None:
        return []
    entries: list[tuple[Path, float]] = []
    for entry in sorted(runtime_dir.iterdir()):
        if not entry.is_file() or entry.suffix.lower() != ".log":
            continue
        try:
            entries.append((entry, entry.stat().st_mtime))
        except OSError:
            continue
    keep = {entry for entry, _ in sorted(
        entries, key=lambda pair: -pair[1])[:_RUNTIME_LOG_KEEP_NEWEST]}
    now = time.time()
    out: list[dict[str, Any]] = []
    for entry, mtime in entries:
        age_s = now - mtime
        if age_s < _RUNTIME_LOG_MAX_AGE_S:
            continue
        if entry in keep:
            continue  # 保留最近 N 个：超龄也豁免
        if any(token in entry.name for token in tokens):
            continue  # 活跃 session/claim 名引用：跳过
        out.append({
            "path": str(entry),
            "type": "stale_runtime_log",
            "action": "move_to_backup",
            "reason": (
                f"runtime 日志超 {int(age_s // 86400)} 天未更新且在保留档外"
                f"（30 天 + keep-{_RUNTIME_LOG_KEEP_NEWEST}，设计 §6.2），"
                "auto_clean 移入回收桶可回滚"),
        })
    return out


def _detect_stale_doctor_backups(project_root: Path) -> list[dict[str, Any]]:
    """超保留期的 doctor 备份桶（保留最近 N 份）：**报告面**。

    真删由 auto_clean 收尾的 ``_prune_stale_backups`` 执行（单一执行源，本卡
    不改）；本检测把「超出 keep-newest-N 且超龄」的桶报给 doctor 巡检可见
    （action ``prune_backup_manual`` ∈ _MANUAL_ACTIONS → 恒 manual，读路径
    与 --fix 都不做任何动作）。保留参数与 prune 同源（惰性导入 constants，
    不另立第二份阈值）。
    """
    from orchd.doctor.fix.constants import (
        _BACKUP_RETENTION_DAYS, _BACKUP_RETENTION_KEEP_NEWEST,
    )

    base = Path(project_root) / ".orchd" / ".doctor-backup"
    if not base.is_dir():
        return []
    buckets: list[Path] = []
    try:
        for child in sorted(base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                buckets.append(child)
            elif child.is_dir() and child.name in ("residual", "legacy"):
                buckets.extend(
                    sorted(p for p in child.iterdir()
                           if p.is_dir() and p.name.isdigit()))
    except OSError:
        return []
    if not buckets:
        return []
    now = time.time()
    by_parent: dict[str, list[Path]] = {}
    for bucket in buckets:
        by_parent.setdefault(str(bucket.parent), []).append(bucket)
    out: list[dict[str, Any]] = []
    for siblings in by_parent.values():
        ordered = sorted(siblings, key=lambda p: int(p.name), reverse=True)
        for bucket in ordered[_BACKUP_RETENTION_KEEP_NEWEST:]:
            try:
                age_days = (now - int(bucket.name)) / 86400
            except ValueError:
                continue
            if age_days < _BACKUP_RETENTION_DAYS:
                continue
            out.append({
                "path": str(bucket),
                "type": "stale_doctor_backup",
                "action": "prune_backup_manual",
                "reason": (
                    f"备份桶超出保留档（keep-newest="
                    f"{_BACKUP_RETENTION_KEEP_NEWEST}，约 {int(age_days)} 天）："
                    "由 auto_clean 收尾的 prune 统一删除，此处仅报告"),
            })
    return out


def _detect_known_build_caches(project_root: Path) -> list[dict[str, Any]]:
    """已知构建缓存（限 ``.orchd/``、runtime 根与 **task worktree** 子树，深度受限）。

    只报 :data:`_BUILD_CACHE_DIR_NAMES` 命中的目录——源码树内的同名缓存不在
    扫描面（warm cache 是合法开发态，删了只会拖慢开发）。task worktree 面
    （task-cont-autoclean-criteria，设计 §6.2）：container 与 flat 布局下任务
    worktree 均为 project_root 的**兄弟目录**（``task-*`` 命名），与
    ``.orchd/`` / runtime 同判据扫描（缓存可再生，无用户数据）。处置走
    delete_dir（auto_clean 移入备份区可回滚）。
    """
    orchd_dir = Path(project_root) / ".orchd"
    scan_roots = [orchd_dir]
    try:
        scan_roots.append(_resolve_runtime_dir(orchd_dir))
    except Exception:
        pass
    parent = Path(project_root).parent
    if parent.is_dir():
        try:
            scan_roots.extend(sorted(parent.glob("task-*")))
        except OSError:
            pass
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scan_root in scan_roots:
        if not scan_root.is_dir():
            continue
        try:
            if scan_root.resolve() in {Path(p).resolve() for p in seen}:
                continue
        except OSError:
            pass
        for directory in _iter_subtree_depth_limited(
                scan_root, _BUILD_CACHE_SCAN_DEPTH):
            if directory.name not in _BUILD_CACHE_DIR_NAMES:
                continue
            key = str(directory)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "path": key,
                "type": "build_cache",
                "action": "delete_dir",
                "reason": (
                    "已知构建缓存（可再生），auto_clean 移入备份区可回滚"),
            })
    return out


def _detect_stale_unregistered_proposals(
        project_root: Path) -> list[dict[str, Any]]:
    """未注册超 14 天的提案草案 → 归档候选（设计 §6.2 stale_proposal 补口）。

    在途列单一真源 = ``orchd.doctor.proposals.inflight_proposals``（与卫生门
    [proposals-inflight] 同源，不另造判据副本）；本检测在其上叠加
    :data:`_PROPOSAL_STALE_MAX_AGE_S`（14 天）mtime 闸，动作复用既有
    ``archive_proposal`` 通道（doctor --fix 整文件移入 ``proposals/archive/
    <ts>/``，只归档不删除、逐字节可回滚）。已注册未清理的草案由既有
    ``_detect_stale_proposals`` 负责，两路同型同处置档（manual）。
    master 不可解析 → INV-4a 留痕。
    """
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.doctor.proposals import inflight_proposals

        paths = inflight_proposals(orchd_dir)
    except Exception as exc:  # INV-4a：master 不可解析 → 留痕，不静默空
        return [_unavailable_residue("stale_unregistered_proposals", exc,
                                     orchd_dir)]
    now = time.time()
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            age_s = now - path.stat().st_mtime
        except OSError:
            continue
        if age_s < _PROPOSAL_STALE_MAX_AGE_S:
            continue
        out.append({
            "path": str(path),
            "type": "stale_proposal",
            "reason": (
                f"未注册草案超 {int(age_s // 86400)} 天未动（设计 §6.2："
                "超过 14 天仍未注册 → 归档，不删除）"),
            "action": "archive_proposal",
        })
    return out


def _detect_stale_rescue_refs(project_root: Path) -> list[dict[str, Any]]:
    """``refs/heads/orchd/rescue/*`` 超 30 天 → 只读报告（设计 §6.2：先报告，暂不自动删）。

    rescue ref 是 L2 脏数据救援的唯一副本（rescue 提交不入库不推送），误删
    不可恢复——故恒报告不处置：action ``rescue_ref_manual`` ∈
    ``_MANUAL_ACTIONS``，auto_clean 落 manual_notice、doctor --fix 落
    skipped_manual（未登记 action 会落 --fix 默认删除分支，登记是硬安全约束）。
    ref 名形态 ``<task_id>-<UTC>``（task id 自身含 ``-``，UTC 段无 ``-``），
    任务 id 取最后一个 ``-`` 前缀段；任务仍活跃（账本可读且非终态）→ 跳过
    （ref 可能仍是唯一救援副本）。账本不可读时仅按年龄报告（报告面零风险）。
    """
    result = _run_git(Path(project_root), [
        "for-each-ref", _RESCUE_REF_REF_PREFIX,
        "--format=%(refname)%09%(committerdate:unix)"])
    if result.returncode != 0 or not (result.stdout or "").strip():
        return []
    try:
        state = None
        from orchd.ledger import Store

        state = Store(Path(project_root) / ".orchd",
                      readonly=True).replay()
    except Exception:
        state = {}  # 账本不可读：仅按年龄报告（报告面，零删除风险）
    now = time.time()
    out: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        refname, _, ts = line.partition("\t")
        name = refname[len(_RESCUE_REF_REF_PREFIX):]
        if not name:
            continue
        task_id = name.rsplit("-", 1)[0]
        try:
            created = float(ts)
        except ValueError:
            continue
        if now - created < _RESCUE_REF_MAX_AGE_S:
            continue
        ts_state = state.get(task_id) if state else None
        if ts_state is not None and getattr(
                ts_state, "status", None) not in _GHOST_TERMINAL_STATUSES:
            continue  # 任务仍活跃：ref 可能仍是唯一救援副本
        out.append({
            "path": refname,
            "type": "stale_rescue_ref",
            "task_id": task_id,
            "reason": (
                f"rescue ref 约超 {int((now - created) // 86400)} 天"
                "（设计 §6.2：先报告暂不自动删，人工确认后处置）"),
            "action": "rescue_ref_manual",
        })
    return out


#   Manual notice（高风险 / 无自动通道，仅报告不执行）：
#     ghost_task / git_tracked_lock / intake_lock / stale_rescue_ref
# 环境变量 ORCHD_AUTO_CLEAN ∈ {off, report} → 全部降级为只报告（disabled=True）。
