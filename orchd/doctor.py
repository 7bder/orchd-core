"""orchd doctor：git 仓库完整性只读检测（叶子模块，零 orchd 内部依赖）。

task-git-doctor-command（2026-08-12，来源 IDEAS L295）：
把 2026-08-08 仓库事故（.git/refs/ 被删 + loose objects 丢失）沉淀的诊断步骤
工具化，供 session 三连检查脚本化复用。

只读、不触碰状态机 / 事件格式 / 既有 CLI 契约语义（§9.1 边界外）。
与 gitops.py 同语义：任何 git 不可用 / 异常均 best-effort 降级为 fail 项，
不抛异常。

依赖方向：doctor.py → 标准库（subprocess / pathlib）。
"""

from __future__ import annotations

import re
import subprocess
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

    return checks


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