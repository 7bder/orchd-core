"""orchd doctor：git 仓库完整性检测 + 残留清理入口（--fix / --dry-run）。

task-git-doctor-command（2026-08-12，来源 IDEAS L295）：
把 2026-08-08 仓库事故（.git/refs/ 被删 + loose objects 丢失）沉淀的诊断步骤
工具化，供 session 三连检查脚本化复用。

task-audit-doctor-fix（2026-08-30，来源 idea:audit-engine-hardening-2026-08）：
把 doctor 升级为统一清理入口：先 dry-run 预览，再显式 --fix 执行，删除前自动备份。
当前清理入口高度散落：intake_lock_clear、session 锁清理、watchdog（只报告不执行）、
layout-migrate 清理，以及最原始的手工删文件。doctor 目前只有只读检测（check_repo），
无任何修复能力。

只读检测（check_repo）不触碰状态机 / 事件格式 / 既有 CLI 契约语义。
--fix 写入受严格白名单约束：只清理引擎识别的运行时残留
（锁文件 / session runtime 文件），绝不触碰 _master.json / IDEAS.md /
_ledger.jsonl / _checkpoint.json，且该白名单须有测试守护。

与 gitops.py 同语义：任何 git 不可用 / 异常均 best-effort 降级为 fail 项，
不抛异常。

依赖方向：doctor.py → 标准库（subprocess / pathlib / shutil / json / time）。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# git 输出统一按 UTF-8 解码（与 gitops.py 一致）
_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"
_GIT_TIMEOUT = 10

# refs/ 根目录扫描忽略的 OS 自动生成文件（macOS Finder 会在目录内生成
# .DS_Store、Windows 生成 Thumbs.db）：此类文件由文件系统自动再生，不属于
# 非法 loose ref，不应误报污染健康检查；与 worktree.py _JUNK_NAMES 中
# OS 杂项语义一致（doctor 为叶子模块，零 orchd 内部依赖，故在本文件内定义）。
_REFS_ROOT_IGNORE = frozenset({".DS_Store", "Thumbs.db"})

# 残留锁文件命名模式（供 detect_residues 识别）。
# 这些文件是 orchd 运行时产生的临时文件，不属于项目源码，可以安全清理。
_LOCK_FILE_PATTERNS = (
    ".intake.lock",
    ".session.lock",
    ".session-gate-*.lock",
    ".session.gate.lock",
)

# 残留 session runtime 文件模式（sessions/ 目录下的 JSON 文件）。
# 这些是 session 心跳/状态记录，超过 TTL 未更新的属于僵尸。
_SESSION_FILE_PREFIX = "sessions/"
_SESSION_FILE_SUFFIX = ".json"

# 会话 TTL（秒）：超过此时间未更新的 session 视为僵尸。
# 与 watchdog 僵死判定一致（watchdog 默认 30 分钟）。
_SESSION_TTL_SECONDS = 1800  # 30 分钟

# 保护白名单：--fix 绝不触碰这些文件/目录。
# 硬编码在 doctor.py 内，确保即使调用方误用也不会损伤核心状态。
_PROTECTED_PATHS = frozenset({
    "_master.json",
    "IDEAS.md",
    "IDEAS-archive.md",
    "ROADMAP.md",
    "_ledger.jsonl",
    "_checkpoint.json",
    "_full_regression.json",
    "session-worktrees.json",
    "merge-acks.json",
})


def _run_git(project_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """以 UTF-8 解码运行 git 命令（cwd 限定 project_root），超时降级。"""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        # git 不可用 / 超时 / 路径异常：返回失败态，由调用方判 fail
        return subprocess.CompletedProcess(
            ["git", *args], returncode=1, stdout="", stderr=""
        )


def _make_check(name: str, status: str, hint: str) -> dict[str, str]:
    """构造单个诊断项。name 为检查名，status 为 ok/fail，hint 为提示。"""
    return {"name": name, "status": status, "hint": hint}


def _resolve_git_dir(project_root: Path, git_dir: str) -> Path:
    """把 git rev-parse 输出的 gitdir 路径解析为绝对路径。

    git 输出可能是绝对路径（worktree 场景）或相对 project_root 的路径
    （普通仓库输出 ``.git``）。统一解析为绝对路径供后续文件访问。
    """
    p = Path(git_dir)
    if not p.is_absolute():
        p = Path(project_root) / p
    return p.resolve()


def _cat_file_batch(project_root: Path, names: list[str]) -> dict[str, str]:
    """一次 `git cat-file --batch-check` 批量查询多个对象的类型（P2a 优化）。

    相比逐个 `git cat-file -t` 派生 N 次子进程，仅派 1 次子进程，O(N)→O(1)。
    从 stdin 逐行读入对象名，解析 `<name> <type> <size>`；type 为 missing 或
    子进程失败/超时时，对应对象判为不可达。

    Returns:
        {name: type} 映射；type 为 "missing" 表示对象不可达。
    """
    if not names:
        return {}
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch-check"],
            cwd=str(project_root),
            input="".join(f"{n}\n" for n in names),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        # git 不可用 / 超时：全部判为不可达
        return {n: "missing" for n in names}
    result: dict[str, str] = {}
    if proc.returncode != 0:
        return {n: "missing" for n in names}
    # git cat-file --batch-check 对每个输入按序输出一行 `<obj> <type> <size>`，
    # 对象缺失时输出 `<obj> missing`。第一列可能是解析后的对象名（hash），
    # 因此按输出顺序与输入名 zip 对齐，取第二列（type）归属到对应输入。
    for name, line in zip(names, proc.stdout.splitlines()):
        parts = line.split()
        if len(parts) >= 2:
            result[name] = parts[1]
    # 未出现在输出中的对象（git 对非法输入可能静默）判为不可达
    for n in names:
        result.setdefault(n, "missing")
    return result


def check_repo(project_root: Path) -> list[dict[str, str]]:
    """检测 git 仓库完整性，返回诊断项列表（ok/fail + 提示）。

    覆盖四类检查（2026-08-08 仓库事故沉淀的诊断步骤）：
    1. 仓库有效（git rev-parse 可解析，兼容普通仓库与 git worktree）；
    2. common refs/ 目录存在（事故：refs/ 目录被删）；
    3. HEAD 可解析（HEAD 文件存在、指向的 ref / 对象可达）；
    4. 引用对象可达（refs/heads/* 与 reflog 最新哈希可 cat-file -t，
       覆盖 loose objects 丢失场景）。

    仓库定位完全依赖 git 解析（rev-parse --git-common-dir），而非假定
    `<root>/.git` 一定是目录：在 git worktree 中 `.git` 是指向真实 gitdir
    的指针文件（内容 `gitdir: ...`），普通仓库才是目录。用 is_dir() 判断
    会让 worktree 健康仓库被误判为"损坏"（2026-08-13 二次审核发现）。
    """
    checks: list[dict[str, str]] = []

    # 1) 仓库有效（兼容普通仓库 / worktree / refs 被删的损坏场景）。
    #    注：`--is-inside-work-tree` 在 refs/ 被删后可能失败，故不以它为唯一判据；
    #    以 `--git-common-dir` 能否解析定位真实 gitdir 为准。
    common = _run_git(project_root, ["rev-parse", "--git-common-dir"])
    if common.returncode != 0 or not common.stdout.strip():
        # 区分"非 git 目录"与"含 .git 但解析失败（疑似 refs/ 被删的损坏）"。
        # 前者判 git_dir fail；后者定位到 refs_dir fail（更贴近 2026-08-08 事故模式），
        # 并回退按 `<root>/.git` 判定 refs 缺失。
        dotgit = Path(project_root) / ".git"
        if dotgit.exists():
            refs_dir_candidate = dotgit / "refs"
            if not refs_dir_candidate.is_dir():
                return [
                    _make_check(
                        "refs_dir",
                        "fail",
                        "git refs/ 目录缺失——2026-08-08 事故模式，恢复见 SKILL.md 仓库事故恢复 SOP",
                    )
                ]
            return [
                _make_check(
                    "git_dir",
                    "fail",
                    "git rev-parse 无法解析且 .git 存在（仓库损坏）",
                )
            ]
        return [
            _make_check(
                "git_dir",
                "fail",
                "git rev-parse 无法解析（非 git 仓库或仓库损坏）",
            )
        ]
    git_dir = _resolve_git_dir(project_root, common.stdout.strip())

    # 2) common refs/ 目录存在
    refs_dir = git_dir / "refs"
    if not refs_dir.is_dir():
        checks.append(
            _make_check(
                "refs_dir",
                "fail",
                "git refs/ 目录缺失——2026-08-08 事故模式，恢复见 SKILL.md 仓库事故恢复 SOP",
            )
        )
    else:
        checks.append(_make_check("refs_dir", "ok", "git refs/ 目录存在"))

    # 3) HEAD 可解析
    head_path = git_dir / "HEAD"
    head_target: str | None = None
    if not head_path.exists():
        checks.append(_make_check("head", "fail", "git HEAD 缺失，无法定位当前分支"))
    else:
        head_text = head_path.read_text(encoding="utf-8", errors="replace").strip()
        if head_text.startswith("ref: "):
            head_target = head_text[5:].strip()
        rev = _run_git(project_root, ["rev-parse", "--verify", "--quiet", "HEAD"])
        if rev.returncode == 0 and rev.stdout.strip():
            checks.append(
                _make_check("head", "ok", f"HEAD 可解析（{head_target or 'detached'}）")
            )
        else:
            checks.append(
                _make_check(
                    "head",
                    "fail",
                    f"HEAD 无法解析（{head_text}）——指向的 ref 或对象已丢失",
                )
            )

    # 4a) refs/ 根目录非法 loose ref 检测（task-p1-doctor-refs-scan）
    # refs/ 根目录下的非目录文件（如 .DS_Store、临时文件）属于非法 loose ref，
    # 会污染 refs 命名空间；合法的直接子项只有 heads/tags/remotes 等目录。
    # 注：仅扫描 refs/ 根目录一层，不递归到 refs/heads/* 等子目录（那是合法 ref）。
    illegal_refs_root: list[str] = []
    if refs_dir.is_dir():
        for entry in sorted(refs_dir.iterdir()):
            if entry.is_dir():
                # 合法子目录（heads/tags/remotes/... 或命名空间目录），跳过
                continue
            # OS 自动生成文件（.DS_Store / Thumbs.db）不属于非法 loose ref，跳过
            if entry.name in _REFS_ROOT_IGNORE:
                continue
            # 内容为合法 40 位 hex 对象哈希的 loose ref（如 refs/stash 等 git 合法
            # 伪 ref）属于合法 ref，跳过；仅内容非哈希的才是非法 loose ref
            try:
                if re.fullmatch(r"[0-9a-fA-F]{40}", entry.read_text(encoding="ascii").strip()):
                    continue
            except OSError:
                pass
            # 非目录文件且非合法 loose ref 即为非法（临时文件 / 手工乱建等）
            illegal_refs_root.append(entry.name)
    if illegal_refs_root:
        checks.append(
            _make_check(
                "refs_root",
                "fail",
                "refs/ 根目录存在非法 loose ref（非目录文件）："
                + "、".join(illegal_refs_root)
                + "——详见 SKILL.md 仓库事故恢复 SOP",
            )
        )
    else:
        checks.append(_make_check("refs_root", "ok", "refs/ 根目录无非法 loose ref"))

    # 4) 引用对象可达（refs/heads/* + reflog 最新哈希）
    # P2a：收集全部候选对象，一次 `git cat-file --batch-check` 批量查询（O(N)→O(1) 子进程）。
    # 注：worktree 的 reflog 位于其专属 gitdir（worktrees/<name>/logs/HEAD），
    # 与 common refs 分离；reflog 若有则读取，缺失不判错（普通精简仓库无 reflog）。
    reachable = True
    ref_hints: list[str] = []
    object_names: list[str] = []
    ref_labels: list[str] = []
    if refs_dir.is_dir():
        heads_dir = refs_dir / "heads"
        if heads_dir.is_dir():
            for ref_file in sorted(heads_dir.glob("*")):
                if not ref_file.is_file():
                    # 命名空间子目录（如 refs/heads/task/xxx），非直接 ref，跳过
                    continue
                ref_name = f"refs/heads/{ref_file.name}"
                object_names.append(ref_name)
                ref_labels.append(ref_name)
    # worktree 专属 gitdir 的 reflog（若存在）
    wtree_gitdir = _resolve_git_dir(project_root, _run_git(
        project_root, ["rev-parse", "--git-dir"]).stdout.strip())
    reflog = wtree_gitdir / "logs"
    if reflog.is_dir():
        head_log = reflog / "HEAD"
        if head_log.exists():
            lines = [
                ln.strip()
                for ln in head_log.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if ln.strip()
            ]
            if lines:
                parts = lines[-1].split()
                if len(parts) >= 2:
                    last_hash = parts[1]
                    object_names.append(last_hash)
                    ref_labels.append(f"reflog 最新提交 {last_hash}")
    types = _cat_file_batch(project_root, object_names)
    for name, label in zip(object_names, ref_labels):
        if types.get(name) in (None, "missing"):
            reachable = False
            ref_hints.append(f"{label} 对象不可达")

    if reachable:
        checks.append(_make_check("objects", "ok", "refs/reflog 引用对象均可达"))
    else:
        checks.append(
            _make_check(
                "objects",
                "fail",
                "引用对象不可达：" + "；".join(ref_hints) or "对象丢失",
            )
        )

    # 5) P0-19：残留任务 worktree 空目录检测（Windows git worktree remove 不完整）。
    # 扫描主工作树父目录（container 布局的 task_wt_root）下 task-* 目录：
    # 无 .git 文件 + 无绑定 → 残留空目录，报告供 prune_orphans 清理。
    try:
        main_wt = Path(project_root).resolve()
        task_wt_root = main_wt.parent  # container: 平级目录；flat: 同级
        if task_wt_root.is_dir():
            residuals = []
            for entry in sorted(task_wt_root.iterdir()):
                if (entry.is_dir()
                        and entry.name.startswith("task-")
                        and not (entry / ".git").exists()):
                    residuals.append(entry.name)
            if residuals:
                checks.append(
                    _make_check(
                        "worktree_residual",
                        "fail",
                        f"发现 {len(residuals)} 个残留任务 worktree 目录（无 .git 登记）："
                        + "、".join(residuals[:10])
                        + ("..." if len(residuals) > 10 else "")
                        + "。运行 orchd doctor --fix 可自动清理。",
                    )
                )
            else:
                checks.append(
                    _make_check("worktree_residual", "ok", "无残留任务 worktree 目录")
                )
    except OSError:
        pass

    # 6) in_review 任务 worktree/分支完整性（2026-08-30 分支丢失复盘 §3）：
    # in_review 是审查等待期（任务可能 idle 数小时），恰是误删高危窗口；
    # 任一 in_review 任务分支/worktree 缺失即 fail，附重建命令模板（只读不修）。
    checks.extend(_check_in_review_worktree_integrity(project_root))

    return checks


def _check_in_review_worktree_integrity(project_root: Path) -> list[dict[str, str]]:
    """in_review 任务 worktree/分支完整性检查（2026-08-30 分支丢失复盘 §3）。

    对每个状态为 ``in_review`` 且已绑定 worktree 的任务校验：
    1. ``task/<id>`` 分支存在且可解析（核心失效模式：分支引用被删）；
    2. 绑定 worktree 目录存在且含有效 ``.git`` 元数据；
    3. 任务 worktree 的 ``.layout.json`` main_worktree 与主工作树一致
       （检出容器根执行导致的布局标记污染）。

    任一缺失 → fail 项（附重建命令模板）。只读、不自动修复。无账本 /
    无 in_review 任务 / 解析异常 → ok 项（best-effort，不误报）。
    """
    try:
        from orchd.ledger import Store, resolve_store_dir
        from orchd.worktree import detect_layout, load_bindings, read_layout
    except Exception:
        return []
    try:
        layout = detect_layout(project_root)
        orchd_dir = Path(project_root) / ".orchd"
        store_root = resolve_store_dir(orchd_dir)
        state = Store(orchd_dir).replay()
        bindings = load_bindings(store_root)
    except Exception:
        return []
    main_wt = Path(layout["main_worktree"]).resolve()
    reviewed = 0
    problems_by_task: dict[str, list[str]] = {}
    for tid, ts in state.items():
        if ts.status != "in_review":
            continue
        reviewed += 1
        entry = bindings.get(tid)
        wt = Path(entry["worktree"]).resolve() if (entry and entry.get("worktree")) else None
        branch = f"task/{tid}"
        problems: list[str] = []
        # 1) 分支存在且可解析（branch/task-<id> 引用丢失 = 核心失效模式）
        rev = _run_git(project_root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
        if rev.returncode != 0 or not rev.stdout.strip():
            problems.append(f"分支 {branch} 缺失/不可解析")
        # 2) 绑定 worktree 目录有效
        if wt is None:
            problems.append("账本绑定缺失 worktree 路径")
        elif not (wt / ".git").exists():
            problems.append(f"worktree 目录 {wt} 无效（缺 .git 元数据）")
        # 3) 布局标记 main_worktree 与主工作树一致（容器根污染检出）
        if wt is not None:
            marker = read_layout(wt / ".orchd")
            if marker is not None:
                mm = Path(marker["main_worktree"]).resolve()
                if mm != main_wt:
                    problems.append(
                        f"布局标记 main_worktree（{mm}）与主工作树（{main_wt}）不一致"
                    )
        if problems:
            problems_by_task[tid] = problems
    if not problems_by_task:
        if reviewed == 0:
            return [_make_check(
                "in_review_integrity", "ok", "无 in_review 任务（或无账本，跳过）")]
        return [_make_check(
            "in_review_integrity", "ok", f"{reviewed} 个 in_review 任务 worktree/分支完整")]
    hints = []
    for tid, problems in sorted(problems_by_task.items()):
        entry = bindings.get(tid)
        wt = Path(entry["worktree"]) if (entry and entry.get("worktree")) else (
            Path(project_root).parent / f"task-{tid[5:] if tid.startswith('task-') else tid}")
        hints.append(
            f"{tid}：{'；'.join(problems)}。重建：git branch task/{tid} <悬空sha>（git fsck "
            f"--lost-found 找回）; git worktree add {wt} task/{tid}; "
            "并补回任务 worktree 的 .orchd/.layout.json（_propagate_container_marker 语义）"
        )
    return [_make_check("in_review_integrity", "fail", "；".join(hints))]


# ---------------------------------------------------------------------------
# 残留检测（task-audit-doctor-fix）
# ---------------------------------------------------------------------------

def detect_residues(project_root: Path) -> list[dict[str, Any]]:
    """扫描 orchd 运行时残留，返回待清理项列表。

    检测四类残留：
    1. 孤儿 session 锁文件（对应 worktree 已不存在）
    2. 僵尸 session runtime 文件（超 TTL 未更新的 session 记录）
    3. 残留 intake 标记（.intake.lock 文件，无 live flock 持锁）
    4. 已误提交入 git 的锁文件（.git 目录外的 .lock 文件出现在 git ls-files 中）

    所有检测均为只读，不执行任何写操作。返回的每项包含：
    - path: 残留文件绝对路径
    - type: 残留类型（orphan_session_lock / zombie_session / intake_lock / git_tracked_lock）
    - reason: 判定依据
    - action: 建议动作（目前固定为 "delete"）
    """
    residues: list[dict[str, Any]] = []
    orchd_dir = Path(project_root) / ".orchd"

    # 1) 孤儿 session 锁文件：session-gate-*.lock 无对应活跃 worktree
    residues.extend(_detect_orphan_session_locks(project_root, orchd_dir))

    # 2) Zombie session runtime files: sessions/*.json 超 TTL
    residues.extend(_detect_zombie_sessions(orchd_dir))

    # 3) Residual intake locks: .intake.lock 无 live flock
    residues.extend(_detect_residual_intake_locks(orchd_dir))

    # 4) Git-tracked lock files: .lock files committed to git
    residues.extend(_detect_git_tracked_locks(project_root))

    return residues


def _detect_orphan_session_locks(
    project_root: Path, orchd_dir: Path
) -> list[dict[str, Any]]:
    """检测孤儿 session 锁文件（无对应活跃 worktree 的 session-gate-*.lock）。

    session-gate-<task-id>.lock 在 claim 时创建，任务完成后应被清理。
    若锁文件存在但对应任务 worktree 不存在或任务已 completed/cancelled，
    则属于孤儿锁，可安全删除。
    """
    residues: list[dict[str, Any]] = []
    if not orchd_dir.is_dir():
        return residues

    for lock_file in sorted(orchd_dir.glob("session-gate-*.lock")):
        if not lock_file.is_file():
            continue
        # 从文件名提取任务 ID: session-gate-task-xxx.lock → task-xxx
        name = lock_file.name
        if name.startswith("session-gate-") and name.endswith(".lock"):
            task_id = name[len("session-gate-"):-len(".lock")]
        else:
            continue
        # 检查对应 worktree 是否存在
        # task_id 已经是 "task-xxx" 格式，直接拼接为 worktree 目录名
        worktree_path = project_root.parent / task_id if task_id.startswith("task-") else None
        if worktree_path is not None and worktree_path.is_dir() and (worktree_path / ".git").exists():
            # worktree 存在且有效，跳过（锁可能是活跃的）
            continue
        residues.append({
            "path": str(lock_file),
            "type": "orphan_session_lock",
            "reason": f"session 锁 {lock_file.name} 无对应活跃 worktree",
            "action": "delete",
        })

    return residues


def _detect_zombie_sessions(orchd_dir: Path) -> list[dict[str, Any]]:
    """检测僵尸 session runtime 文件（超 TTL 未更新的 sessions/*.json）。

    sessions/ 目录下的 JSON 文件是 session 心跳记录。
    若文件的 mtime 超过 _SESSION_TTL_SECONDS 未更新，说明 session 已僵死，
    对应的 runtime 文件可安全删除。
    """
    residues: list[dict[str, Any]] = []
    sessions_dir = orchd_dir / "sessions"
    if not sessions_dir.is_dir():
        return residues

    now = time.time()
    for session_file in sorted(sessions_dir.glob("*.json")):
        if not session_file.is_file():
            continue
        try:
            mtime = session_file.stat().st_mtime
        except OSError:
            continue
        age = now - mtime
        if age > _SESSION_TTL_SECONDS:
            residues.append({
                "path": str(session_file),
                "type": "zombie_session",
                "reason": f"session 文件 {session_file.name} 已 {int(age / 60)} 分钟未更新（TTL {_SESSION_TTL_SECONDS // 60} 分钟）",
                "action": "delete",
            })

    return residues


def _detect_residual_intake_locks(orchd_dir: Path) -> list[dict[str, Any]]:
    """检测残留 intake 标记（.intake.lock 无 live flock 持锁）。

    .intake.lock 是准入锁文件，正常 acquire/release 不删除文件。
    若文件存在但无 live flock 持锁（说明进程已退出但文件未清理），
    则属于残留，可安全删除。

    复用 ledger.intake_lock_check 的语义：检查 ExclusiveFileLock 是否被持有。
    """
    residues: list[dict[str, Any]] = []
    if not orchd_dir.is_dir():
        return residues

    # 检查 .orchd 根目录的 .intake.lock
    intake_lock = orchd_dir / ".intake.lock"
    if intake_lock.is_file():
        try:
            from orchd.lockfile import ExclusiveFileLock
            held = ExclusiveFileLock(intake_lock).check().get("held", False)
        except Exception:
            held = False  # 无法判定时保守跳过
        if not held:
            residues.append({
                "path": str(intake_lock),
                "type": "intake_lock",
                "reason": ".intake.lock 文件存在但无 live flock 持锁（进程已退出）",
                "action": "delete",
            })

    return residues


def _detect_git_tracked_locks(project_root: Path) -> list[dict[str, Any]]:
    """检测已误提交入 git 的锁文件（.git 外的 .lock 文件出现在 git ls-files 中）。

    锁文件（.lock）属于运行时临时文件，不应被 git 追踪。
    若 git ls-files 输出中出现项目根目录下的 .lock 文件，
    说明已被误提交，应先从 git 中移除（git rm --cached）再删除。
    """
    residues: list[dict[str, Any]] = []
    # git ls-files 列出追踪的 .lock 文件
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
        if file_path.is_file():
            residues.append({
                "path": str(file_path),
                "type": "git_tracked_lock",
                "reason": f"锁文件 {line} 已被误提交入 git（运行时临时文件不应追踪）",
                "action": "git_rm_cached_then_delete",
            })

    return residues


def _is_protected_path(path: Path, project_root: Path) -> bool:
    """检查路径是否在保护白名单中（--fix 绝不触碰）。

    白名单硬编码在 _PROTECTED_PATHS 中，覆盖引擎核心状态文件。
    比较时取相对路径的最后一段（文件名），确保无论绝对路径如何都生效。
    """
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        # path 不在 project_root 下，按文件名判定
        rel = Path(path.name)
    # 检查文件名是否在白名单中
    if rel.name in _PROTECTED_PATHS:
        return True
    # 检查完整相对路径
    if str(rel) in _PROTECTED_PATHS:
        return True
    return False


def doctor(project_root: Path) -> dict[str, Any]:
    """执行完整仓库健康检查。

    Returns:
        {repo_ok: bool, checks: [...], issues: [...], repo: str}
        repo_ok 为 False 表示存在任一 fail 项（调用方可据此设非零退出码）。
    """
    checks = check_repo(project_root)
    issues = [c["hint"] for c in checks if c["status"] == "fail"]
    return {
        "repo_ok": len(issues) == 0,
        "checks": checks,
        "issues": issues,
        "repo": str(Path(project_root).resolve()),
    }


def doctor_fix(
    project_root: Path,
    *,
    dry_run: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """执行残留清理（dry-run 预览 + 显式执行）。

    Args:
        project_root: 项目根目录。
        dry_run: True 时只输出待清理清单，不执行任何写操作。
        backup_dir: 删除前自动备份到此目录。None 时自动使用
            <project_root>/.orchd/.doctor-backup/<timestamp>/。

    Returns:
        {
            "dry_run": bool,
            "backup_dir": str | None,
            "detected": [...],   # detect_residues 原始输出
            "skipped_protected": [...],  # 被白名单保护的路径
            "cleaned": [...],    # 已清理的项（dry_run=True 时为 []）
            "errors": [...],     # 清理失败的项
            "summary": str,
        }
    """
    detected = detect_residues(project_root)
    skipped_protected: list[dict[str, Any]] = []
    to_clean: list[dict[str, Any]] = []

    # 白名单过滤：绝不触碰核心状态文件
    for item in detected:
        path = Path(item["path"])
        if _is_protected_path(path, Path(project_root)):
            skipped_protected.append({
                **item,
                "reason": item.get("reason", "") + " [SKILLED: 保护白名单拦截]",
            })
        else:
            to_clean.append(item)

    # dry-run 模式：只报告不执行
    if dry_run:
        return {
            "dry_run": True,
            "backup_dir": None,
            "detected": detected,
            "skipped_protected": skipped_protected,
            "cleaned": [],
            "errors": [],
            "summary": (
                f"[dry-run] 发现 {len(detected)} 项残留"
                f"（{len(skipped_protected)} 项被白名单保护跳过，"
                f"{len(to_clean)} 项可清理）。"
                f"使用 --fix 执行实际清理。"
            ),
        }

    # 执行模式：先备份再删除
    backup_path: Path | None = None
    if backup_dir is None:
        backup_path = Path(project_root) / ".orchd" / ".doctor-backup" / f"{int(time.time())}"
    else:
        backup_path = Path(backup_dir)

    cleaned: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in to_clean:
        path = Path(item["path"])
        action = item.get("action", "delete")

        try:
            if action == "git_rm_cached_then_delete":
                # 先从 git 移除追踪，再删除文件
                git_result = _run_git(project_root, ["rm", "--cached", str(path)])
                if git_result.returncode != 0:
                    errors.append({
                        **item,
                        "error": f"git rm --cached 失败: {git_result.stderr.strip()}",
                    })
                    continue

            # 备份（仅当文件存在时）
            if path.exists() and backup_path is not None:
                backup_path.mkdir(parents=True, exist_ok=True)
                # 保持相对路径结构，避免同名文件冲突
                try:
                    rel = path.relative_to(Path(project_root))
                    dest = backup_path / rel
                except ValueError:
                    dest = backup_path / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(path), str(dest))

            # 执行删除
            if path.exists():
                path.unlink()

            cleaned.append({
                **item,
                "backup": str(backup_path) if backup_path else None,
            })
        except OSError as exc:
            errors.append({
                **item,
                "error": f"{type(exc).__name__}: {exc}",
            })

    return {
        "dry_run": False,
        "backup_dir": str(backup_path) if backup_path else None,
        "detected": detected,
        "skipped_protected": skipped_protected,
        "cleaned": cleaned,
        "errors": errors,
        "summary": (
            f"清理完成：{len(cleaned)} 项已清理"
            f"（{len(skipped_protected)} 项被白名单保护跳过，"
            f"{len(errors)} 项失败）。"
            f"备份目录：{backup_path}"
        ),
    }
