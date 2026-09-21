"""orchd/nogit.py — 无 git 模式：快照原语 + 变更检测单一真源（A0a）。

**定位（ROADMAP §1.4.6 无 git 模式三切片之 A0a）**：把「任务改动文件集合」这件事
收敛为**单一真源** :func:`changed_paths`，由两个后端分发：

- **git 后端**（git 可用）：直接复用既有 ``worktree._git_diff_names``
  （``git diff --name-only main...task/{id}``，**无 --diff-filter → 天然含 D/R**），
  字节级零回归（同一函数、同一命令）。
- **快照后端**（无 git）：以**快照 manifest**（相对路径 → 内容 sha256）做差分，
  含删除(D) 与重命名(R)，口径对齐 git ``--name-only``。

**D/R 口径与已知边界（kernel-contract INV-1）**：

- **删除(D)**：两侧均报缺失路径 —— 一致。
- **重命名(R，内容同一)**：git ``--name-only`` 报**新路径**（重命名折叠为单条）；
  本实现同样只报新路径（按内容 sha 同一性判定重命名源）—— 一致。
- **重命名 + 改写**：git 依相似度阈值（``diff.renames`` / 默认 50%）可能仍判重命名，
  本实现按**精确内容同一性**判定，会退化为「旧路径(删) + 新路径(增)」。
  故本实现的路径集合恒为 git 结果的**超集**（只可能更严、**绝不漏检**），
  红线 #3 不因此退化。该边界为**已知且有意保留**（不做相似度算法，避免与 git
  版本/配置耦合）。

**依赖方向**：nogit.py → lockfile.py / errors.py；git 侧经惰性导入复用
``orchd.worktree`` / ``orchd.gitops``，不反向依赖，避免循环导入。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from orchd.lockfile import ExclusiveFileLock

# 快照存储根（默认 <project_root>/.orchd/snapshots，可用 ORCHD_SNAPSHOT_ROOT 覆盖）
SNAPSHOT_DIRNAME = "snapshots"
MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".snapshot.lock"

# 扫描排除：顶层目录名（git 元数据 / 运行时 / 缓存 / 依赖）
_EXCLUDED_TOP_NAMES = frozenset({
    ".git",
    ".orchd-runtime",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
})

# 引擎运行时文件（task-nogit-single-dir-pivot）：单目录快照直接扫描主目录树，
# `.orchd/` 下由引擎持续改写的状态文件（账本/检查点/绑定/锁/课程库/备份区/
# 草案区）必须排除——否则认领/评审等引擎动作自身即构成“改动”，门禁恒红。
# 仅在 `.orchd/` 子树内生效（项目源码同名文件不受影响）；用户可见文档
# （IDEAS.md / shared/）与 _master.json 不在此列（仍参与变更检测——
# _master.json 的门禁影响已由 out-of-scope 固定资产豁免与 residual 声明覆盖
# 过滤中和，test_snapshot_roundtrip_and_lock 锁定其在 manifest 内）。
_HASH_EXCLUDED_FILES = frozenset({
    "_ledger.jsonl",
    "_checkpoint.json",
    "session-worktrees.json",
    "lessons.jsonl",
    "lessons.staged.jsonl",
})
# SQLite 存储后端（A3）的账本库与侧车文件（``-wal`` / ``-shm`` / ``-journal``）同属
# **引擎运行时状态**，无 git 快照口径必须一并排除：否则「切换存储后端」本身就会被
# 越界门禁判成项目改动（task-storage-test-matrix 实测：sqlite 后端在无 git 项目下
# done 被 E010 拦下——A3 × A0 交互缺口）。按前缀排除以覆盖全部侧车文件。
_HASH_EXCLUDED_PREFIXES = ("_ledger.sqlite3",)
_HASH_EXCLUDED_DIRS = frozenset({
    ".doctor-backup",
    "proposals",
})


# ----------------------------------------------------------------------
# 路径与存储根
# ----------------------------------------------------------------------


def snapshot_store_root(project_root: Path) -> Path:
    """快照存储根：``<project_root>/.orchd/snapshots``（或 ORCHD_SNAPSHOT_ROOT）。"""
    override = os.environ.get("ORCHD_SNAPSHOT_ROOT")
    if override:
        return Path(override)
    return Path(project_root) / ".orchd" / SNAPSHOT_DIRNAME


def snapshot_dir(project_root: Path, task_id: str, label: str = "base") -> Path:
    """某任务某标签的快照目录。"""
    return snapshot_store_root(project_root) / task_id / label


def manifest_path(project_root: Path, task_id: str, label: str = "base") -> Path:
    return snapshot_dir(project_root, task_id, label) / MANIFEST_NAME


def snapshot_lock(project_root: Path) -> ExclusiveFileLock:
    """快照存储的排他文件锁（跨进程互斥，复用统一锁原语）。"""
    store = snapshot_store_root(project_root)
    _ensure_store_ignored(store)
    return ExclusiveFileLock(store / _LOCK_NAME)


def ensure_snapshot_root(project_root: Path) -> str:
    """确保快照存储根存在（``orchd init`` 时建立；含自忽略 .gitignore）。

    无 git 模式的落地基础：接入门槛「有目录即可」⇒ init 必须把快照根一并备好，
    后续 claim 可直接建工作目录与基线，无需额外前置动作。
    """
    store = snapshot_store_root(project_root)
    _ensure_store_ignored(store)
    store.mkdir(parents=True, exist_ok=True)
    return str(store)


def _ensure_store_ignored(store: Path) -> None:
    """确保快照存储**不被版本控制**（仓内自包含 .gitignore）。

    存储位于 ``<project_root>/.orchd/snapshots``，若宿主是 git 仓库，
    ``git add -A`` 会把它提交进任务分支（污染 diff 与变更检测）。此处以存储根内
    的 ``.gitignore('*')`` 使整个存储被忽略——只影响该目录，不改宿主根 .gitignore。
    best-effort：写失败不阻断（存储本身仍被 :func:`hash_tree` 按路径排除）。
    """
    gi = store / ".gitignore"
    if gi.exists():
        return
    try:
        store.mkdir(parents=True, exist_ok=True)
        gi.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------
# manifest 读写
# ----------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(project_root: Path, *, store_root: Path | None = None) -> dict[str, str]:
    """扫描工作树，返回 ``{相对路径(posix): 内容 sha256}``。

    - 跳过 ``_EXCLUDED_TOP_NAMES`` 顶层目录；
    - **跳过快照存储根本身**（避免自包含 / 无限增长）；
    - **跳过 `.orchd/` 子树内的引擎运行时文件**（账本/检查点/绑定/锁/课程库/
      备份区/草案区，见 ``_HASH_EXCLUDED_FILES`` / ``_HASH_EXCLUDED_DIRS``）——
      否则认领/评审等引擎动作自身即构成“改动”；用户可见文档（IDEAS.md /
      shared/ / _master.json）仍参与检测；
    - 跳过符号链接与无法读取的文件（best-effort，不中断扫描）。
    """
    root = Path(project_root)
    store = (store_root or snapshot_store_root(root)).resolve()
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for dirpath, dirnames, filenames in os.walk(root):
        cur = Path(dirpath)
        try:
            in_orchd = ".orchd" in cur.relative_to(root).parts
        except ValueError:
            continue
        # 顶层排除 + 快照存储排除（按解析路径判定，兼容 store 位于任意层级）+
        # .orchd/ 子树内引擎运行时目录排除
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_TOP_NAMES
            and not (cur / d).resolve() == store
            and not (in_orchd and d in _HASH_EXCLUDED_DIRS)
        ]
        for name in filenames:
            fp = cur / name
            try:
                if fp.is_symlink() or not fp.is_file():
                    continue
                if in_orchd and (
                    name in _HASH_EXCLUDED_FILES
                    or name.endswith(".lock")
                    or name.startswith(_HASH_EXCLUDED_PREFIXES)
                ):
                    continue
                rel = fp.relative_to(root).as_posix()
                result[rel] = _sha256_file(fp)
            except (OSError, ValueError):
                continue
    return result


def write_manifest(snap_dir: Path, manifest: dict[str, str]) -> Path:
    """原子写入 manifest（临时文件 + os.replace）。"""
    snap_dir.mkdir(parents=True, exist_ok=True)
    target = snap_dir / MANIFEST_NAME
    tmp = snap_dir / (MANIFEST_NAME + ".tmp")
    payload = json.dumps(
        {"version": 1, "files": dict(sorted(manifest.items()))},
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    )
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_manifest(snap_dir: Path) -> dict[str, str] | None:
    """读取 manifest；缺失 / 损坏返回 None（best-effort，不抛异常）。"""
    path = Path(snap_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return None
    return {str(k): str(v) for k, v in files.items()}


def take_snapshot(project_root: Path, task_id: str, label: str = "base") -> dict[str, Any]:
    """对当前工作树取快照（manifest），返回结构化摘要。

    写动作在快照存储排他锁内执行（跨进程串行）。
    """
    manifest = hash_tree(project_root)
    _ensure_store_ignored(snapshot_store_root(project_root))
    lock = snapshot_lock(project_root)
    lock.acquire()
    try:
        path = write_manifest(snapshot_dir(project_root, task_id, label), manifest)
    finally:
        lock.release()
    return {
        "task_id": task_id,
        "label": label,
        "dir": str(snapshot_dir(project_root, task_id, label)),
        "manifest": str(path),
        "files": len(manifest),
    }


# ----------------------------------------------------------------------
# 差分：快照后端
# ----------------------------------------------------------------------


def manifest_changed_paths(
    base: dict[str, str],
    current: dict[str, str],
) -> list[str]:
    """两份 manifest 的变更路径集合（口径对齐 git ``--name-only``）。

    规则：
      - 内容变化 → 报路径；
      - ``base`` 有、``current`` 无 → 删除，报路径（除非被判定为重命名源）；
      - ``current`` 有、``base`` 无 → 新增，报路径；
      - **重命名折叠**：``base`` 中消失且 ``current`` 中新增的路径若**内容 sha 相同**，
        视为重命名 → 只报新路径（对齐 git ``--name-only`` 对重命名折叠为单条的行为）。

    返回排序后的相对路径列表（posix）。
    """
    deleted = [p for p in base if p not in current]
    added = [p for p in current if p not in base]
    modified = [p for p in base if p in current and base[p] != current[p]]

    # 重命名折叠：内容同一的重命名源 → 不再作为「删除」上报
    new_by_hash: dict[str, list[str]] = {}
    for p in added:
        new_by_hash.setdefault(current[p], []).append(p)
    rename_sources: set[str] = set()
    for old in sorted(deleted):
        bucket = new_by_hash.get(base[old])
        if bucket:
            new_by_hash[base[old]] = bucket[1:]
            rename_sources.add(old)

    paths = set(modified) | set(added) | (set(deleted) - rename_sources)
    return sorted(paths)


def snapshot_changed_paths(
    project_root: Path, task_id: str, label: str = "base"
) -> list[str] | None:
    """快照后端变更检测：``None`` 表示**无基线快照**（无法判定，调用方决定降级语义）。"""
    base = read_manifest(snapshot_dir(project_root, task_id, label))
    if base is None:
        return None
    return manifest_changed_paths(base, hash_tree(project_root))


# ----------------------------------------------------------------------
# git 后端与单一真源
# ----------------------------------------------------------------------


def git_available(project_root: Path) -> bool:
    """git 是否可用（三态口径复用 ``check_workspace_state`` 单一真源）。"""
    try:
        from orchd.gitops import check_workspace_state

        return bool(check_workspace_state(Path(project_root)).get("available"))
    except Exception:
        return False


def git_changed_paths(project_root: Path, task_id: str) -> list[str]:
    """git 后端：复用 ``worktree._git_diff_names``（同一函数 → 字节级零回归）。"""
    from orchd.worktree import _git_diff_names

    return _git_diff_names(Path(project_root), task_id)


def changed_paths(project_root: Path, task_id: str, *, label: str = "base") -> list[str]:
    """**变更检测单一真源**：任务相对基线实际改动的文件路径列表。

    - git 可用 → :func:`git_changed_paths`（既有 ``git diff --name-only`` 口径，含 D/R）；
    - 无 git 且存在基线快照 → :func:`snapshot_changed_paths`（含 D/R，超集口径）；
    - 无 git 且**无基线快照** → 空列表（调用方据 :func:`snapshot_changed_paths`
      自行判定降级语义；A0b 起基线由 claim 建立，届时恒非空）。
    """
    root = Path(project_root)
    if git_available(root):
        return git_changed_paths(root, task_id)
    result = snapshot_changed_paths(root, task_id, label)
    return result or []


# ----------------------------------------------------------------------
# 单目录任务变更口径（task-nogit-single-dir-pivot）：无 git 环境不再建立
# sibling 任务工作目录拷贝，所有命令在项目主目录执行；基线快照按任务存于
# 主目录快照存储（claim 建 base、done 推进 committed）。
# ----------------------------------------------------------------------


def maindir_changed_paths(project_root: Path, task_id: str) -> list[str]:
    """主目录相对任务基线快照的变更路径（无基线 → 空列表）。"""
    return snapshot_changed_paths(project_root, task_id, "base") or []


def maindir_residual_paths(project_root: Path, task_id: str) -> list[str]:
    """「提交」后又发生的改动（零残留门禁用；无 committed 基线 → 空列表）。

    语义对齐 git 侧 ``list_tracked_changes``：提交（快照推进）之后**又**发生的改动
    （如 verify_command 期间的写入）应当被检出。
    """
    committed = read_manifest(snapshot_dir(project_root, task_id, "committed"))
    if committed is None:
        return []
    return manifest_changed_paths(committed, hash_tree(project_root))


# ----------------------------------------------------------------------
# 无 git orchd 项目判定 + 主目录不变量（task-nogit-guard-parity）
# ----------------------------------------------------------------------
# 背景：守卫体系（``orchd/gitops/guard.py::guard_write_command`` 的 L1 分支守卫 + L2
# 会话锁、``_guard_shared_entry_coverage`` 的 E039 形态 A）此前在 git 不可用时**整体
# 降级**——无 git 环境等于「门禁不在守」。本段提供守卫等价实现所需的两个判据原语，
# 使「无 git 且属 orchd 项目」不再落进降级路径，而**非 orchd 的无关目录**保持既有
# 降级语义（零回归）。
#
# 判据边界（刻意收窄）：
#   - 「无 git orchd 项目」= 目录带 orchd 项目标记（``.orchd/``）**且** git 不可用；
#   - 无关目录（无 ``.orchd/``，如临时目录 / 其它项目的子目录）不属此列 → 旧语义。


def nogit_project_markers(project_root: Path | str | None) -> bool:
    """目录是否带 orchd 项目标记（``.orchd/`` 目录存在）。

    这是「无 git orchd 项目」的**目录侧**判据（不含 git 探测）——调用方若已掌握
    git 可用性（如 :func:`orchd.gitops.guard.guard_write_command` 的 workspace 探测
    结果），应直接用本函数避免重复探测。
    """
    if project_root is None:
        return False
    try:
        return (Path(project_root) / ".orchd").is_dir()
    except OSError:
        return False


def is_nogit_project(project_root: Path | str | None) -> bool:
    """是否「无 git 的 orchd 项目」：带 orchd 标记 且 git 不可用。

    守卫等价（L1/L2/E039）的**唯一分流判据**：为真时走等价实现，为假时保持既有
    降级语义。``project_root`` 为 None（无项目根）时不可能带 orchd 标记 → 恒假。
    """
    if project_root is None:
        return False
    if not nogit_project_markers(project_root):
        return False
    return not git_available(Path(project_root))


def maindir_invariant(project_root: Path | str) -> dict[str, Any]:
    """单目录不变量：布局解析出的**主工作树**须等于传入的项目根。

    无 git 单目录模式（task-nogit-single-dir-pivot）只有**一个**合法写位置——项目
    主目录。若命令的操作根不是布局主工作树（例如误在任务工作目录 / 拷贝目录内执行），
    则该不变量被打破，调用方（L1 等价守卫）应结构化拒绝。

    Returns:
        ``{"ok": <bool>, "main_worktree": <str>, "project_root": <str>,
        "layout": <str>, "marker_source": <str>}``。
    """
    root = Path(project_root).resolve()
    try:
        from orchd.worktree import detect_layout

        layout = detect_layout(root)
    except Exception:  # 布局探测失败 → 不作判定（保守放行，调用方按无违反处理）
        return {
            "ok": True,
            "main_worktree": str(root),
            "project_root": str(root),
            "layout": None,
            "marker_source": None,
            "probe_failed": True,
        }
    main_wt = Path(layout.get("main_worktree") or root).resolve()
    return {
        "ok": main_wt == root,
        "main_worktree": str(main_wt),
        "project_root": str(root),
        "layout": layout.get("layout"),
        "marker_source": layout.get("marker_source"),
    }
