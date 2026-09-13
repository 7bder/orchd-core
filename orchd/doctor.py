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
import os
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
# 分两类：源码资产（与位置无关恒保护）与运行时状态（canonical 账本根保护、
# legacy 位置可清——container 布局下 main/.orchd 的 flat 遗留属于可清残留，
# 见 _detect_legacy_flat_residues）。
_SOURCE_ASSETS = frozenset({
    "_master.json",
    "IDEAS.md",
    "IDEAS-archive.md",
    "ROADMAP.md",
})
_RUNTIME_STATE_FILES = frozenset({
    "_ledger.jsonl",
    "_checkpoint.json",
    "_full_regression.json",
    "session-worktrees.json",
    "merge-acks.json",
})
_PROTECTED_PATHS = _SOURCE_ASSETS | _RUNTIME_STATE_FILES


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



def _detect_stale_worktree_registry(project_root: Path) -> list[dict[str, str]]:
    """检测 git worktree 注册中的 prunable 项（有注册、工作目录已不存在）。

    解析 ``git worktree list --porcelain`` 输出，找含 ``prunable`` 标记的
    worktree。返回 [{path, reason}] 列表；git 不可用时返回空（不干扰其他检查）。
    """
    result = _run_git(project_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []
    stale: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current is not None and "prunable" in current:
                stale.append(current)
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("prunable") and current is not None:
            current["prunable"] = line[len("prunable"):].strip() or "prunable"
            current["reason"] = current["prunable"]
    if current is not None and "prunable" in current:
        stale.append(current)
    return stale


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
    # 扫描根用 detect_layout 的 task_wt_root（与 prune_orphans 一致）：container →
    # 容器根；flat 无任务 worktree 概念 → 直接 ok（不再用 main_wt.parent 硬编码，
    # 避免 flat 布局误报主工作树父目录的无关 task-* 目录）。
    try:
        from orchd.worktree import detect_layout

        layout = detect_layout(Path(project_root))
        task_wt_root = layout.get("task_wt_root")
        if layout.get("layout") != "container" or task_wt_root is None:
            checks.append(
                _make_check("worktree_residual", "ok", "flat 布局无任务 worktree，跳过")
            )
        elif Path(task_wt_root).is_dir():
            residuals = []
            for entry in sorted(Path(task_wt_root).iterdir()):
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
        else:
            checks.append(
                _make_check("worktree_residual", "ok", "task_wt_root 不存在")
            )
    except OSError:
        pass

    # 5b) P0-19b：stale worktree 注册（有 git 登记、工作目录已不存在 = prunable）。
    # 与 worktree_residual 方向相反：后者扫「目录存在但无 .git 登记」，本条扫
    # 「.git/worktrees 有登记但目录已删」。doctor --fix 执行 git worktree prune。
    stale = _detect_stale_worktree_registry(project_root)
    if stale:
        paths = [s["path"] for s in stale]
        checks.append(
            _make_check(
                "worktree_stale_registry",
                "fail",
                f"发现 {len(stale)} 个 prunable worktree 注册（目录已不存在但 git 仍登记）："
                + "、".join(Path(p).name for p in paths[:10])
                + ("..." if len(paths) > 10 else "")
                + "。运行 orchd doctor --fix 执行 git worktree prune 清理。",
            )
        )
    else:
        checks.append(
            _make_check("worktree_stale_registry", "ok", "无 prunable worktree 注册")
        )

    # 6) in_review 任务 worktree/分支完整性（2026-08-30 分支丢失复盘 §3）：
    # in_review 是审查等待期（任务可能 idle 数小时），恰是误删高危窗口；
    # 任一 in_review 任务分支/worktree 缺失即 fail，附重建命令模板（只读不修）。
    checks.extend(_check_in_review_worktree_integrity(project_root))

    # 7) container 任务 worktree 不变量：._master.json 单副本（唯一权威 = 主工作树）
    checks.extend(_check_master_single_copy(project_root))

    return checks


def _check_master_single_copy(project_root: Path) -> list[dict[str, str]]:
    """container 任务 worktree 残留 ``.orchd/_master.json`` 巡检（task-master-single-copy）。

    唯一权威 = 主工作树的 ``.orchd/_master.json``；container 布局下任务 worktree
    由 sparse-checkout/skip-worktree 抑制副本。若某任务 worktree 仍存在
    ``.orchd/_master.json`` → 不变量被破坏（副本漂移风险），报 fail 并附修复 hint。
    flat 布局本就不建任务 worktree（单 worktree，副本在主工作树）→ 直接 ok。
    只读检测；无容器标记 / 布局解析失败 → best-effort ok（不误报）。
    """
    try:
        from orchd.worktree import detect_layout
    except Exception:
        return []
    try:
        layout = detect_layout(project_root)
        if layout.get("layout") != "container":
            return [_make_check("master_single_copy", "ok", "flat 布局：无任务 worktree，无需单副本巡检")]
        task_root = Path(layout["task_wt_root"])
        canonical = Path(layout["main_worktree"]).resolve()
    except Exception:
        return [_make_check("master_single_copy", "ok", "container 标记缺失/解析失败，跳过单副本巡检")]
    if not task_root.is_dir():
        return [_make_check("master_single_copy", "ok", "无任务 worktree 目录，单副本不变量成立")]
    residual: list[str] = []
    try:
        for child in task_root.iterdir():
            if not child.is_dir():
                continue
            # 唯一权威 = 主工作树，其自身的 .orchd/_master.json 必在，不算残留
            if child.resolve() == canonical:
                continue
            wt_master = child / ".orchd" / "_master.json"
            if wt_master.exists():
                residual.append(str(child))
    except OSError:
        return [_make_check("master_single_copy", "ok", "任务 worktree 目录扫描异常，跳过")]
    if not residual:
        return [_make_check("master_single_copy", "ok", "container 任务 worktree 无 .orchd/_master.json 副本（单副本不变量成立）")]
    return [_make_check(
        "master_single_copy", "fail",
        f"{len(residual)} 个任务 worktree 残留 .orchd/_master.json（唯一权威 = 主工作树 "
        f"{canonical}/.orchd/_master.json）：{'；'.join(residual)}。"
        "修复：cd <任务worktree> && git sparse-checkout init --no-cone && "
        "git sparse-checkout set '/*' '!/.orchd/' '/.orchd/*' '!/.orchd/_master.json'；"
        "或在主工作树手工清理残留副本后重跑 claim",
    )]


def _load_master_task_ids(orchd_dir: Path) -> set[str] | None:
    """读取 ``_master.json`` 登记的任务 id 集合；不可读返回 None（不做过滤）。"""
    try:
        from orchd.spec import load_master

        master = load_master(Path(orchd_dir) / "_master.json")
        return {t.get("id") for t in master.tasks if t.get("id")}
    except Exception:
        return None


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
    # 仅巡检**引擎登记任务**：账本中残留的非登记 id（测试夹具 task-1/task-2 等
    # 写入共享账本的噪音、或已从 master 移除的任务）本就没有分支与 worktree，
    # 纳入会把健康仓库误判为 fail。master 不可读时不做过滤（保持 best-effort，
    # 兼容无 _master.json 的最小场景）。
    master_ids = _load_master_task_ids(orchd_dir)
    reviewed = 0
    problems_by_task: dict[str, list[str]] = {}
    for tid, ts in state.items():
        if master_ids is not None and tid not in master_ids:
            continue
        # AC5（task-review-baseline-and-worktree-recycle-fix）：巡检面由 in_review
        # 扩展到 claimed。claimed 是「实现中」长时窗（可跨数小时 / 数天），与
        # in_review 同为分支误删高危期；2026-09-10 事故即发生在 claimed 期
        # （refs/heads/task/ 目录被删、三支分支丢失）。
        if ts.status not in ("in_review", "claimed"):
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
        # 4) 任务 worktree HEAD 指向的 ref 存在（AC5）
        if wt is not None and (wt / ".git").exists():
            head = _run_git(wt, ["symbolic-ref", "-q", "HEAD"])
            head_ref = head.stdout.strip() if head.returncode == 0 else ""
            if not head_ref:
                problems.append(f"worktree HEAD 不可解析（{wt}）")
            else:
                ref_rev = _run_git(
                    project_root, ["rev-parse", "--verify", "--quiet", head_ref]
                )
                if ref_rev.returncode != 0 or not ref_rev.stdout.strip():
                    problems.append(f"worktree HEAD 指向的 ref 不存在（{head_ref}）")
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
                "in_review_integrity", "ok", "无 in_review/claimed 任务（或无账本，跳过）")]
        return [_make_check(
            "in_review_integrity", "ok",
            f"{reviewed} 个 in_review/claimed 任务 worktree/分支完整")]
    # 惰性导入：doctor 为叶子模块（零顶层 orchd 依赖），依赖一律函数内导入。
    try:
        from orchd.worktree import branch_reflog_tip
    except Exception:
        def branch_reflog_tip(_root, _branch):  # type: ignore[misc]
            return None

    hints = []
    for tid, problems in sorted(problems_by_task.items()):
        entry = bindings.get(tid)
        try:
            from orchd.worktree import _task_wt_name

            default_wt = Path(project_root).parent / _task_wt_name(tid)
        except Exception:
            default_wt = Path(project_root).parent / (
                f"task-{tid[5:] if tid.startswith('task-') else tid}")
        wt = Path(entry["worktree"]) if (entry and entry.get("worktree")) else default_wt
        # AC5：引用自愈模板——refs 被删但 reflog 存活时（2026-08-08 / 2026-09-10
        # 两次同型事故），reflog 末行 tip 即分支最后落点，可直接用于重建。
        tip = branch_reflog_tip(Path(project_root), f"task/{tid}")
        if tip:
            rebuild = (
                f"git branch task/{tid} {tip}（reflog tip 自愈："
                f"git reflog show --format=%H task/{tid} 末行）"
            )
        else:
            rebuild = (
                f"git branch task/{tid} <悬空sha>"
                "（git fsck --lost-found 找回；无 reflog 时用此兜底）"
            )
        hints.append(
            f"{tid}：{'；'.join(problems)}。重建：{rebuild}; "
            f"git worktree add {wt} task/{tid}; "
            "并补回任务 worktree 的 .orchd/.layout.json（_propagate_container_marker 语义）"
        )
    return [_make_check("in_review_integrity", "fail", "；".join(hints))]


# ---------------------------------------------------------------------------
# 残留检测（task-audit-doctor-fix）
# ---------------------------------------------------------------------------

def _resolve_runtime_dir(orchd_dir: Path) -> Path:
    """解析运行时残留根目录（与 Store 同根）。

    container 布局 → ``resolve_store_dir`` 返回 ``<容器>/.orchd-runtime/``
    （锁 / session / intake 标记均落于此）；flat 或解析失败 →
    回退 ``orchd_dir``（``<project_root>/.orchd``），零回归。

    task-runtime-hygiene 路径盲区修复：此前 detect_residues 一律用
    ``<project_root>/.orchd``，container 布局下扫不到 .orchd-runtime 内的
    运行时残留（doctor --fix 恒报 0 项、僵尸 session 持续堆积）。
    """
    try:
        from orchd.ledger import resolve_store_dir

        return Path(resolve_store_dir(orchd_dir))
    except Exception:
        return orchd_dir


def _detect_residual_dirs(project_root: Path) -> list[dict[str, Any]]:
    """检测残留任务 worktree 目录（P0-19 类，无 .git 登记 + 无活跃绑定）。

    判定与 prune_orphans 的 P0-19 分支一致：目录存在但无 .git 文件
    （Windows git worktree remove 不完整）且无 session-worktrees 绑定。
    container 布局下任务 worktree 与主工作树平级（task_wt_root）；flat 布局
    无任务 worktree 概念 → 返回空。
    """
    residues: list[dict[str, Any]] = []
    try:
        from orchd.worktree import detect_layout, load_bindings
        from orchd.ledger import resolve_store_dir

        layout = detect_layout(Path(project_root))
        if layout.get("layout") != "container":
            return residues
        task_wt_root = Path(layout["task_wt_root"])
        bindings = load_bindings(resolve_store_dir(Path(project_root) / ".orchd"))
    except Exception:
        return residues

    if not task_wt_root.is_dir():
        return residues
    for entry in sorted(task_wt_root.iterdir()):
        if (entry.is_dir()
                and entry.name.startswith("task-")
                and not (entry / ".git").exists()
                and entry.name not in bindings):
            residues.append({
                "path": str(entry),
                "type": "residual_dir",
                "reason": "残留任务 worktree 目录（无 .git 登记且无活跃绑定）",
                "action": "delete_dir",
            })
    return residues


def _detect_legacy_flat_residues(project_root: Path) -> list[dict[str, Any]]:
    """检测 flat 布局遗留的状态文件（container / ORCHD_HOME 重定向生效时）。

    ``resolve_store_dir(orchd_dir) != orchd_dir`` 说明账本根已重定向
    （container → ``<容器>/.orchd-runtime``，或 ORCHD_HOME 指定），此时
    ``orchd_dir`` 下的运行时状态文件（_ledger.jsonl / _checkpoint.json 等）是
    历史 flat 遗留，--fix 备份后删除。flat 布局（无重定向）→ 零操作。
    """
    residues: list[dict[str, Any]] = []
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.ledger import resolve_store_dir

        canonical = resolve_store_dir(orchd_dir)
    except Exception:
        return residues
    if canonical.resolve() == orchd_dir.resolve():
        return residues  # flat：无重定向，canonical 即 orchd_dir，零操作
    if not orchd_dir.is_dir():
        return residues
    for name in sorted(_RUNTIME_STATE_FILES):
        p = orchd_dir / name
        if p.is_file():
            residues.append({
                "path": str(p),
                "type": "legacy_flat_residue",
                "reason": (
                    f"container 布局下 flat 遗留状态文件（canonical 在 {canonical}），"
                    "--fix 备份后删除"
                ),
                "action": "delete",
            })
    return residues


def detect_residues(project_root: Path) -> list[dict[str, Any]]:
    """扫描 orchd 运行时残留，返回待清理项列表。

    检测八类残留：
    1. 孤儿 session 锁文件（对应 worktree 已不存在）
    2. 僵尸 session runtime 文件（超 TTL 未更新的 session 记录）
    3. 残留 intake 标记（.intake.lock 文件，无 live flock 持锁）
    4. 已误提交入 git 的锁文件（.git 目录外的 .lock 文件出现在 git ls-files 中）
    5. 幽灵任务（账本/checkpoint 派生存在但不在 ``_master.json``）
    6. stale worktree 注册（prunable，目录已删但 git 仍登记）
    7. 残留任务 worktree 目录（无 .git 登记 + 无活跃绑定，P0-19 类）
    8. flat 遗留状态文件（container 重定向生效时 main/.orchd 的历史 runtime 文件）

    运行时残留（1/2/3/8）以**共享账本根**为扫描根（与 Store 同根，见
    :func:`_resolve_runtime_dir`）：container 布局扫 ``<容器>/.orchd-runtime/``，
    flat 扫 ``<project_root>/.orchd``。

    所有检测均为只读，不执行任何写操作。返回的每项包含：
    - path: 残留文件绝对路径
    - type: 残留类型（orphan_session_lock / zombie_session / intake_lock /
      git_tracked_lock / ghost_task / stale_worktree_registry / residual_dir /
      legacy_flat_residue）
    - reason: 判定依据
    - action: 建议动作（delete / delete_dir / git_rm_cached_then_delete /
      git_worktree_prune / retract_ghost / ghost_task_manual）
    - disposition: 处置档（auto_clean / legacy_move / manual），判据与自动清理
      通道同源（``_AUTO_CLEAN_TYPES`` / ``_LEGACY_MOVE_TYPES``）；卫生门禁据此
      只对 manual 档判失败，auto_clean / legacy_move 档交由清理通道处置
    """
    residues: list[dict[str, Any]] = []
    orchd_dir = Path(project_root) / ".orchd"
    # 运行时残留根 = 共享账本根（container → <容器>/.orchd-runtime；flat → .orchd）
    runtime_dir = _resolve_runtime_dir(orchd_dir)

    # 1) 孤儿 session 锁文件：session-gate-*.lock 无对应活跃 worktree
    residues.extend(_detect_orphan_session_locks(project_root, runtime_dir))

    # 2) Zombie session runtime files: sessions/*.json 超 TTL
    residues.extend(_detect_zombie_sessions(runtime_dir))

    # 3) Residual intake locks: .intake.lock 无 live flock
    residues.extend(_detect_residual_intake_locks(runtime_dir))

    # 4) Git-tracked lock files: .lock files committed to git
    residues.extend(_detect_git_tracked_locks(project_root))

    # 5) 幽灵任务：账本/checkpoint 派生存在、但不在 _master.json（task-runtime-hygiene AC1）
    residues.extend(_detect_ghost_tasks(project_root))

    # 6) stale worktree 注册（prunable）：--fix 执行 git worktree prune
    for s in _detect_stale_worktree_registry(project_root):
        residues.append({
            "path": s["path"],
            "type": "stale_worktree_registry",
            "reason": s.get("reason", "prunable"),
            "action": "git_worktree_prune",
        })

    # 7) 残留任务 worktree 目录（P0-19，--fix 关闭 doctor 断链：worktree_residual → delete_dir）
    residues.extend(_detect_residual_dirs(project_root))

    # 8) flat 遗留状态文件（container 布局下 main/.orchd 的历史 runtime 文件）
    residues.extend(_detect_legacy_flat_residues(project_root))

    # 与自动清理通道判据对齐（task-takeover-residue-alignment AC3）：为每项标注处置档
    # （auto_clean / legacy_move / manual），单一事实源为 _AUTO_CLEAN_TYPES /
    # _LEGACY_MOVE_TYPES——卫生门禁据此区分「清理器即将处理」与「需人工处置」，不再把
    # auto-clean 级残留当卫生失败报红（消除检测器与清理通道抢跑的抖动）。
    for item in residues:
        item.setdefault("disposition", _residue_disposition(item.get("type")))
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
    """检测残留 intake 标记（.intake.lock 无 live flock 且已超时）。

    .intake.lock 是准入锁文件，正常 acquire/release 不删除文件（task-intake-lock-path-fix
    AC5：准入写释放后留下的新鲜标记不得立即判残留，否则卫生门禁每次准入写后闪红约 120s）。
    判据与 ``ledger.intake_lock_check`` 的 timeout 语义对齐：仅当无 live flock 持有
    **且**标记年龄 >= ``_INTAKE_LOCK_TIMEOUT``（默认 120s）时才算残留；新鲜标记
    （刚释放，flock 已放但文件保留）返回空。超时残留仍可被 ``intake_lock_check``
    自动清除（AC3 timeout_cleaned 语义不回归）。

    时间源优先用标记内 JSON ``timestamp``（acquire 写入的诊断标记），缺失/非法时
    回退文件 mtime；均不可得时保守跳过（不误报）。
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
        "path": str(intake_lock),
        "type": "intake_lock",
        "reason": (
            ".intake.lock 无 live flock 且已超时 "
            f"（age {age_s:.1f}s >= {timeout_s:.0f}s，进程已退出）"
        ),
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


def _detect_ghost_tasks(project_root: Path) -> list[dict[str, Any]]:
    """检测幽灵任务：账本/checkpoint 派生存在、但不在 ``_master.json`` 的任务。

    成因（2026-09-10 附录 D 补录）：测试夹具（如 pytest 泄漏的 ``task-2``）的
    CLAIMED 事件落进共享账本根，但任务从未注册进 ``_master.json``；于是
    ``status`` 不可见，``watchdog`` 却按僵死判定返回 exit 1，而
    ``force-status`` / ``retract`` 的常规通道对"不在 master 的 id"不可用
    （retract 另有跨 agent 归属守卫 E034）。

    本检测为**只读**；清理走 ``doctor --fix`` 的 ``retract_ghost`` 分支——
    以引擎保留的 ``admin`` 控制面撤回该任务的孤儿事件，使 replay 与 master 一致。

    兼容性：仅当 ``<project_root>/.orchd/_master.json`` 存在且可读时才检测；
    ledger / master 任一不可用时静默返回空（保持 doctor 叶子模块的 best-effort 语义）。
    """
    residues: list[dict[str, Any]] = []
    orchd_dir = Path(project_root) / ".orchd"
    master_path = orchd_dir / "_master.json"
    if not master_path.is_file():
        return residues
    try:
        from orchd.ledger import Store, resolve_store_dir
        from orchd.spec import load_master

        master = load_master(master_path)
        master_ids = {t["id"] for t in master.tasks}
        store = Store(resolve_store_dir(orchd_dir))
        state = store.replay()
        events = store.backend.read_events()
    except Exception:
        return residues

    # 按任务归集事件，用于定位可撤回的起点事件
    events_by_task: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        _tid = ev.get("task_id")
        if _tid:
            events_by_task.setdefault(_tid, []).append(ev)

    for task_id in sorted(state):
        if task_id in master_ids:
            continue
        target_event_id = _ghost_retract_target(events_by_task.get(task_id, []))
        if target_event_id is None:
            # 仅含 FORCE_STATUS（防篡改不可撤）→ 无法自动清理，单列需人工处置
            residues.append({
                "path": f"ledger#{task_id}",
                "task_id": task_id,
                "type": "ghost_task",
                "reason": (
                    f"任务 {task_id} 存在于账本但不属于 _master.json，且其事件"
                    f"均为不可撤的 FORCE_STATUS（防篡改保护）——需人工/admin 专案处置"
                ),
                "action": "ghost_task_manual",
            })
            continue
        residues.append({
            "path": f"ledger#{task_id}",
            "task_id": task_id,
            "type": "ghost_task",
            "reason": (
                f"任务 {task_id} 存在于账本/checkpoint 但不在 _master.json"
                f"（幽灵任务，watchdog 会误判僵死）"
            ),
            "action": "retract_ghost",
            "event_id": target_event_id,
        })

    return residues


def _ghost_retract_target(events: list[dict[str, Any]]) -> str | None:
    """为幽灵任务挑选可撤回的起点事件 id。

    优先 ``CLAIMED``（retract 级联撤回其后续事件）；否则取首个非
    ``FORCE_STATUS`` 事件。``FORCE_STATUS`` 受引擎防篡改保护（retract 恒拒
    E007），故事件全为 FORCE_STATUS 时返回 None，调用方降级为需人工处置。
    """
    for ev in events:
        if ev.get("type") == "CLAIMED":
            return ev.get("event_id")
    for ev in events:
        if ev.get("type") != "FORCE_STATUS":
            return ev.get("event_id")
    return None


def _is_protected_path(path: Path, project_root: Path) -> bool:
    """检查路径是否在保护白名单中（--fix 绝不触碰）。

    源码资产（_master.json / IDEAS.md 等）与位置无关恒保护；运行时状态文件
    （_ledger.jsonl / _checkpoint.json 等）仅当位于 canonical 账本根时保护——
    container 布局下 main/.orchd 的 flat 遗留属于可清残留（不保护，由
    _detect_legacy_flat_residues 检出后备份清理）。
    """
    name = path.name
    if name in _SOURCE_ASSETS:
        return True
    if name in _RUNTIME_STATE_FILES:
        orchd_dir = Path(project_root) / ".orchd"
        canonical = _resolve_runtime_dir(orchd_dir)
        try:
            path.resolve().relative_to(canonical.resolve())
            return True
        except ValueError:
            return False
    # 完整相对路径保护（保持向后兼容：白名单内完整路径也保护）
    try:
        rel = path.relative_to(project_root)
        return str(rel) in _PROTECTED_PATHS
    except ValueError:
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
            "skipped_manual": [...],     # 无自动清理通道、需人工处置的项
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

    # 不可自动处置项单列（如事件全为 FORCE_STATUS 的幽灵任务），避免 --fix 报伪失败
    skipped_manual: list[dict[str, Any]] = [
        i for i in to_clean if i.get("action") == "ghost_task_manual"
    ]
    if skipped_manual:
        to_clean = [i for i in to_clean if i.get("action") != "ghost_task_manual"]

    # dry-run 模式：只报告不执行
    if dry_run:
        return {
            "dry_run": True,
            "backup_dir": None,
            "detected": detected,
            "skipped_protected": skipped_protected,
            "skipped_manual": skipped_manual,
            "cleaned": [],
            "errors": [],
            "summary": (
                f"[dry-run] 发现 {len(detected)} 项残留"
                f"（{len(skipped_protected)} 项被白名单保护跳过，"
                f"{len(skipped_manual)} 项需人工处置，"
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

        # 幽灵任务清理（task-runtime-hygiene AC1/AC5）：不走文件删除路径，改用
        # 引擎保留的 admin 控制面撤回其在账本中的孤儿事件，使 replay 与 master
        # 一致（force-status / retract 常规通道对不在 _master.json 的 id 不可用，
        # 故此处是该类残留的专用处置通道）。
        if action == "retract_ghost":
            try:
                from orchd.ledger import Store, resolve_store_dir
                from orchd.onboard import retract as _retract

                ghost_store = Store(resolve_store_dir(Path(project_root) / ".orchd"))
                ghost_res = _retract(
                    ghost_store,
                    "admin",
                    target_event_id=item.get("event_id"),
                    reason=(
                        f"幽灵任务清理（doctor --fix）："
                        f"{item.get('task_id')} 不在 _master.json"
                    ),
                    project_root=Path(project_root),
                )
                cleaned.append({
                    **item,
                    "retracted": bool(ghost_res.get("retracted")),
                    "backup": None,
                })
            except Exception as exc:  # 清理失败不中断其余项
                errors.append({
                    **item,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            continue

        if action == "git_worktree_prune":
            try:
                prune_result = _run_git(project_root, ["worktree", "prune"])
                if prune_result.returncode != 0:
                    errors.append({
                        **item,
                        "error": f"git worktree prune 失败: {prune_result.stderr.strip()}",
                    })
                else:
                    cleaned.append({**item, "backup": None})
            except Exception as exc:
                errors.append({
                    **item,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            continue

        if action == "delete_dir":
            # 残留任务 worktree 目录：先 rmdir（空目录），非空则 _rmtree_force
            # （Windows 句柄场景兜底，与 prune_orphans P0-19 分支语义一致）。
            try:
                if path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        from orchd.worktree import _rmtree_force

                        if not _rmtree_force(path):
                            raise OSError(f"目录清理失败: {path}")
                cleaned.append({**item, "backup": None})
            except Exception as exc:
                errors.append({
                    **item,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            continue

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
        "skipped_manual": skipped_manual,
        "cleaned": cleaned,
        "errors": errors,
        "summary": (
            f"清理完成：{len(cleaned)} 项已清理"
            f"（{len(skipped_protected)} 项被白名单保护跳过，"
            f"{len(errors)} 项失败）。"
            f"备份目录：{backup_path}"
        ),
    }


# ---------------------------------------------------------------------------
# 自动清洁（task-doctor-auto-clean）
# ---------------------------------------------------------------------------
# 分级模型（与手动 doctor --fix 共享 detect_residues 单一事实源）：
#   Auto-Clean（低风险，直接处理，无备份）：
#     orphan_session_lock / zombie_session → 直接删文件
#     residual_dir → 删目录（rmdir / _rmtree_force 兜底）
#     stale_worktree_registry → git worktree prune
#   Legacy move（可回滚）：legacy_flat_residue → 移入 .doctor-backup/legacy/<ts>/ 滚动区
#   Manual notice（高风险 / 无自动通道，仅报告不执行）：
#     ghost_task / git_tracked_lock / intake_lock
# 环境变量 ORCHD_AUTO_CLEAN ∈ {off, report} → 全部降级为只报告（disabled=True）。
_AUTO_CLEAN_TYPES = frozenset({
    "orphan_session_lock",
    "zombie_session",
    "residual_dir",
    "stale_worktree_registry",
})
_LEGACY_MOVE_TYPES = frozenset({"legacy_flat_residue"})

# 卫生门禁豁免档位（单一事实源，消费方 scripts/verify_project_hygiene.py）：
# 属这两档的残留由自动清理通道处置，不与清理器抢跑、不计入卫生失败判定。
AUTO_CLEAN_DISPOSITIONS: tuple[str, ...] = ("auto_clean", "legacy_move")


def _residue_disposition(rtype: str | None) -> str:
    """残留项处置档：``auto_clean`` / ``legacy_move`` / ``manual``。

    判据与自动清理通道同源（``_AUTO_CLEAN_TYPES`` / ``_LEGACY_MOVE_TYPES``）：
    - ``auto_clean``  ：读路径（status / watchdog）的 ``auto_clean`` 会直接处置；
    - ``legacy_move`` ：移入 ``.doctor-backup/legacy/<ts>/`` 滚动备份区；
    - ``manual``      ：无自动通道、仅报告（ghost_task / git_tracked_lock / intake_lock）。

    卫生门禁（``scripts/verify_project_hygiene.py``）只对 ``manual`` 档失败，
    避免「检测器报清理器即将删除之物」的抖动（residue-report-autoclean-alignment）。
    """
    if rtype in _AUTO_CLEAN_TYPES:
        return "auto_clean"
    if rtype in _LEGACY_MOVE_TYPES:
        return "legacy_move"
    return "manual"


def _auto_clean_item(project_root: Path, item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """执行单个 Auto-Clean 残留的清理（best-effort，失败返回 (False, 带 error)）。"""
    path = Path(item["path"])
    action = item.get("action", "delete")
    try:
        if action == "git_worktree_prune":
            res = _run_git(project_root, ["worktree", "prune"])
            if res.returncode != 0:
                return False, {**item, "error": res.stderr.strip()}
            return True, {**item, "disposition": "pruned"}
        if action == "delete_dir":
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    from orchd.worktree import _rmtree_force

                    if not _rmtree_force(path):
                        raise OSError(f"目录清理失败: {path}")
            return True, {**item, "disposition": "deleted"}
        if path.exists():
            path.unlink()
        return True, {**item, "disposition": "deleted"}
    except Exception as exc:
        return False, {**item, "error": f"{type(exc).__name__}: {exc}"}


def _auto_move_legacy(
    project_root: Path, item: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """把 flat 遗留状态文件移入 ``.orchd/.doctor-backup/legacy/<ts>/`` 滚动备份区。

    移动而非删除：早期 flat 布局的运行时文件可能仍含历史状态，保留供人工回滚。
    备份根按时间戳分桶，同名不冲突；移动失败返回 (False, 带 error)。
    """
    path = Path(item["path"])
    backup_root = (
        Path(project_root) / ".orchd" / ".doctor-backup" / "legacy" / f"{int(time.time())}"
    )
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / path.name
        if path.exists():
            shutil.move(str(path), str(dest))
        return True, {**item, "backup": str(dest), "disposition": "moved"}
    except Exception as exc:
        return False, {**item, "error": f"{type(exc).__name__}: {exc}"}


def auto_clean(
    project_root: Path, *, emit_stderr: bool = True
) -> dict[str, Any]:
    """doctor 自动清洁：读路径（status / watchdog）自动执行的低风险残留清理。

    分级判定与手动 ``doctor --fix`` 共享 ``detect_residues`` 单一事实源：
    - Auto-Clean 类型（orphan_session_lock / zombie_session / residual_dir /
      stale_worktree_registry）直接删/prune，无备份；
    - legacy_flat_residue 移入 ``.orchd/.doctor-backup/legacy/<ts>/`` 滚动备份区
      （可回滚）；
    - 高风险项（ghost_task / git_tracked_lock / intake_lock）仅计入
      ``manual_notice``，不自动执行。

    环境变量 ``ORCHD_AUTO_CLEAN`` ∈ {off, report} 时降级为只报告不执行
    （``disabled=True``）。

    stderr 留痕与开关契约（task-doctor-auto-clean 返工）：
    - ``emit_stderr=False`` 时完全静默（status/watchdog 挂载处按
      ``config.guidance_stderr`` 传入），结果仍并入返回 JSON 的 ``auto_clean`` 字段；
    - 仅对**真正发生**的 sanitize/move 动作（含失败留痕）写 ``[回收]`` stderr，
      ``manual_notice`` / ``reported_only`` 这类未做任何清理的项**不发** stderr，
      避免读路径噪声。
    全程 best-effort，单项失败不中断其余项，亦不抛异常。

    Args:
        project_root: canonical 项目根。
        emit_stderr: 是否写 ``orchd ▸ [回收]`` stderr（默认 True）。

    Returns:
        {
            "auto_cleaned": [...],   # 已自动清理的残留项
            "auto_moved": [...],     # 已移入备份区的残留项
            "manual_notice": [...],  # 仅报告不执行的项
            "disabled": bool,        # ORCHD_AUTO_CLEAN 关闭时为 True
        }
    """
    project_root = Path(project_root).resolve()
    mode = os.environ.get("ORCHD_AUTO_CLEAN", "").strip().lower()
    disabled = mode in ("off", "report")

    from orchd.worktree import _log_recycle, _recycle_actor

    actor = _recycle_actor()
    auto_cleaned: list[dict[str, Any]] = []
    auto_moved: list[dict[str, Any]] = []
    manual_notice: list[dict[str, Any]] = []

    for item in detect_residues(project_root):
        rtype = item.get("type")
        target = str(Path(item.get("path", "")))
        if disabled:
            # 关闭 / 只报告：不执行、不留痕，仅计数报告
            manual_notice.append({**item, "disposition": "reported_only"})
            continue
        if rtype in _AUTO_CLEAN_TYPES:
            ok, record = _auto_clean_item(project_root, item)
            if ok:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_clean",
                        "type": rtype,
                        "target": target,
                        "disposition": record.get("disposition"),
                        "reason": item.get("reason", ""),
                        "actor": actor,
                    }])
                auto_cleaned.append(record)
            else:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_clean_failed",
                        "type": rtype,
                        "target": target,
                        "error": record.get("error", ""),
                        "actor": actor,
                    }])
                manual_notice.append(record)
        elif rtype in _LEGACY_MOVE_TYPES:
            ok, record = _auto_move_legacy(project_root, item)
            if ok:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_move",
                        "type": rtype,
                        "target": target,
                        "disposition": "moved_to_backup",
                        "backup": record.get("backup"),
                        "reason": item.get("reason", ""),
                        "actor": actor,
                    }])
                auto_moved.append(record)
            else:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_move_failed",
                        "type": rtype,
                        "target": target,
                        "error": record.get("error", ""),
                        "actor": actor,
                    }])
                manual_notice.append(record)
        else:
            # ghost_task / git_tracked_lock / intake_lock 等高风险项：仅报告、
            # 零 stderr（未执行任何清理动作，读写路径均不发噪声）
            manual_notice.append({**item, "disposition": "manual"})

    return {
        "auto_cleaned": auto_cleaned,
        "auto_moved": auto_moved,
        "manual_notice": manual_notice,
        "disabled": disabled,
    }
