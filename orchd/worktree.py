"""Orchd 1.4 双布局（container / flat）解析与布局标记（task-14-worktree-layout）。

布局类型（设计见 .trae/documents/concurrency-native-multi-task-plan.md §4.1）：
- container（推荐，新项目 / layout-migrate 后）：``<容器>/main/`` 主工作树
  （git 根，恒 checkout main）+ ``<容器>/task-<id>/`` 平级任务 worktree +
  ``<容器>/.orchd-runtime/`` 共享账本根；
- flat（既有项目不重构）：``<项目根>/`` 主工作树（git 根）+ ``<项目根同级>/task-<id>/``
  + ``<项目根同级>/.orchd-runtime/`` 共享账本根。

两种布局的任务 worktree 根 == 账本 runtime 根 == **主工作树父目录**
（container: ``<容器>``；flat: ``<项目根同级>``）；差异仅在主工作树身份：
container 的主工作树是 ``<容器>/main/``，flat 是 ``<项目根>/``。由布局标记
（``<主工作树>/.orchd/.layout.json``）在运行期决定，缺失时自动探测 + 告警
（不静默跑错目录）。

依赖方向：本模块只依赖标准库（json / os / pathlib / shutil / subprocess / sys）+ orchd.errors，
不导入 onboard / review / ledger 状态机（叶子化，单一入口可审计）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注：本模块为叶子，运行时不依赖 ledger（防循环）
    from orchd.ledger import Store

# 布局标记相对路径（位于 <主工作树>/.orchd/ 下）
_LAYOUT_MARKER = ".layout.json"
# container 布局的主工作树子目录名
_CONTAINER_MAIN_DIR = "main"
# 共享账本 runtime 根目录名（主工作树父目录下）
_RUNTIME_DIR = ".orchd-runtime"
# 迁移时无效文件隔离回收子目录（<runtime>/trash/，可手动还原）
_TRASH_DIR = "trash"
_LAYOUT_VERSION = 1

# 保守版无效文件清单（task-14-layout-migrate-junk-clean）：可再生的 OS 杂项 /
# 缓存 / 覆盖产物 / 临时 / 日志。迁移时隔离回收至 trash/ 而非带进 main/，
# 避免目录污染；绝不触碰 .git / .orchd / 工具目录 / venv / 被跟踪文件。
_JUNK_NAMES = frozenset({
    # OS 杂项
    ".DS_Store", "Thumbs.db",
    # 缓存
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    # 覆盖产物
    ".coverage", "htmlcov",
    # 临时
    ".pytest-tmp", ".tmp-pytest",
    # 引擎自动同步日志（可再生）
    ".orchd-core-sync.log",
})
_JUNK_PREFIXES = ("_tmp_", "pytest_tmp_")

# P2-8：迁移时绝不搬入 main/ 的 venv / IDE 目录（环境专属，非项目源码内容）。
# 注：.trae/.workbuddy 等工具配置目录照常移入 main/（test_migrate_keeps_tool_and_tracked 断言）。
_TOOL_DIR_NAMES = frozenset({".venv", ".idea", ".vscode"})


def _is_junk_entry(name: str) -> bool:
    """顶层条目是否为保守版无效文件（可再生，迁移时隔离回收而非移入 main/）。"""
    if name in _JUNK_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _JUNK_PREFIXES)


# 运行时账本文件（task-14-layout-migrate-ledger-move）：容器布局默认账本根为
# <容器>/.orchd-runtime/，迁移时须从 main/.orchd/ 搬入，否则历史对引擎不可见。
# master 目录文件（_master.json/shared/rules/IDEAS/ROADMAP/SKILL/templates/
# proposals/merge-acks.json/.layout.json）保留在 main/.orchd/，不在此列。
_LEDGER_RUNTIME_FILES = (
    "_ledger.jsonl", "_checkpoint.json", ".lock", ".session.lock",
    "session-worktrees.json",
)


def _move_ledger_runtime_files(
    orchd_dir: Path, runtime_root: Path
) -> tuple[list[str], list[dict[str, Any]]]:
    """把运行时账本文件从 ``<main>/.orchd/`` 搬到 ``<runtime>/``（best-effort）。

    迁移 flat→container 后，``resolve_store_dir`` 默认读 ``<容器>/.orchd-runtime/``，
    若历史账本仍留在 ``main/.orchd/``，引擎将看不到既有任务状态。此函数把
    ledger / checkpoint / 锁 / session-worktrees / mod-* 搬到 runtime 根，
    保证迁移一次成功后任务历史立即可见。缺失的条目跳过，搬移失败记入
    ``errors`` 交由调用方回滚/上报（W-11 起不再静默跳过）。

    Returns:
        ``(moved, errors)``：已搬移的文件/目录名清单（相对名），以及搬移失败项
        （``{"name", "error"}``）——W-11 起失败不再静默吞掉，由调用方决定回滚/上报。
    """
    moved: list[str] = []
    errors: list[dict[str, Any]] = []
    _intake_lock_name = _intake_lock_filename()
    for name in _LEDGER_RUNTIME_FILES:
        if name == _intake_lock_name:
            # W-11：迁移全程持 .intake.lock（串行化准入写）——**不搬移持锁文件本身**
            # （Windows 上搬移被本进程句柄占用的文件会失败；且搬走后旧 fd 与新路径
            # 语义分裂）。下次准入写会在新 canonical 路径自动重建该锁。
            continue
        src = orchd_dir / name
        if src.exists():
            try:
                shutil.move(str(src), str(runtime_root / name))
                moved.append(name)
            except OSError as exc:
                # W-11：搬移失败不再静默——记入 errors 供调用方回滚与上报
                errors.append({
                    "name": name,
                    "error": f"{type(exc).__name__}: {exc}",
                })
    for p in sorted(orchd_dir.glob("mod-*")):
        try:
            shutil.move(str(p), str(runtime_root / p.name))
            moved.append(p.name)
        except OSError as exc:
            errors.append({
                "name": p.name,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return moved, errors


_GIT_TIMEOUT = 10


def marker_path(orchd_dir: Path) -> Path:
    """返回布局标记路径（<orchd_dir>/.layout.json）。"""
    return orchd_dir / _LAYOUT_MARKER


def read_layout(orchd_dir: Path) -> dict[str, Any] | None:
    """读取布局标记；文件缺失或损坏返回 None（best-effort）。

    Args:
        orchd_dir: 主工作树的 .orchd 目录。

    Returns:
        标记 dict（含 layout / version / main_worktree），或 None。
    """
    try:
        # utf-8-sig 兼容 Windows 编辑器写入的 BOM（BOM 会导致 json.loads 失败）
        data = json.loads(marker_path(orchd_dir).read_text(encoding="utf-8-sig"))
        layout = data.get("layout")
        main_worktree = data.get("main_worktree")
        if layout in ("container", "flat") and isinstance(main_worktree, str) and main_worktree:
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def write_layout(orchd_dir: Path, layout: str, main_worktree: Path) -> dict[str, Any]:
    """原子写入布局标记（tmp + os.replace），避免标记错乱读半成品。

    Args:
        orchd_dir: 主工作树的 .orchd 目录。
        layout: ``"container"`` 或 ``"flat"``。
        main_worktree: 主工作树绝对路径。

    Returns:
        结构化结果：``{"written": True, "path": <str>, "layout": <str>}``。
    """
    data = {
        "layout": layout,
        "version": _LAYOUT_VERSION,
        "main_worktree": str(Path(main_worktree).resolve()),
    }
    marker = marker_path(orchd_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(f".{marker.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, marker)
    return {"written": True, "path": str(marker), "layout": layout}


def _git_toplevel(project_root: Path) -> Path | None:
    """best-effort 定位 git 主工作树根（git rev-parse --show-toplevel）。

    非 git 仓库 / git 不可用 / 异常返回 None（调用方降级）。

    按次缓存（task-gitops-probe-cache）：缓存键与
    ``orchd.gitops.query`` 同源（目录形态 + .git/HEAD 快照），toplevel 只与
    仓库归属有关、键不变则答案不变。函数内惰性导入（worktree 不在顶层依赖
    gitops，避免循环）。确定性失败（非零退出/空输出）同样缓存；
    抛异常路径永不缓存（task-probe-cache-negative-results）。
    """
    try:
        from orchd.gitops.query import _PROBE_CACHE, _probe_cache_key
    except Exception:
        _PROBE_CACHE = None  # type: ignore[assignment]
        _probe_cache_key = None  # type: ignore[assignment]
    key = _probe_cache_key(project_root) if _probe_cache_key is not None else None
    ckey = ("toplevel", key) if key is not None else None
    if _PROBE_CACHE is not None and ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        value: Path | None = None
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out:
                value = Path(out)
        if _PROBE_CACHE is not None and ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def _git_common_dir(project_root: Path) -> Path | None:
    """定位仓库共享 git 目录（``git rev-parse --git-common-dir``，绝对路径）。

    review W-1 / R-16：残留目录判定需要「曾由 git 登记为本仓库 worktree」的证据，
    证据落点即 ``<common-dir>/worktrees/<name>``。linked worktree 与主工作树返回
    同一 common dir；非 git / git 不可用 / 解析失败 → None（调用方保守降级）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    git_dir = Path(proc.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (Path(project_root) / git_dir).resolve()
    return git_dir


def _is_git_tracked_path(path: Path) -> bool:
    """路径是否被其所属 git 仓库跟踪（junk 清理前的安全校验，review W-15）。

    判定序：以路径本身（目录）或父目录（文件）为 cwd 定位最近 git 仓库根
    （``--show-toplevel``），再以 ``git ls-files --error-unmatch -- <相对路径>``
    判定是否被跟踪。非 git / 路径不在仓库内 → False（不构成「被跟踪」，允许清理）。

    探测异常（git 不可用 / 超时）→ True：保守判为「可能被跟踪」而跳过删除。
    这样 ``_cleanup_container_root_junk`` 的 docstring 承诺「绝不触碰被跟踪文件」
    才有实现支撑（此前只有承诺、无校验）。
    """
    try:
        path = Path(path)
        probe = path if path.is_dir() else path.parent
        root = _git_toplevel(probe)
        if root is None:
            return False
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(rel).replace(os.sep, "/")],
            cwd=str(root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True
    return proc.returncode == 0


def _auto_detect_layout(git_root: Path | None, project_root: Path) -> str:
    """无布局标记时的布局猜测（best-effort，仅用于告警与降级解析）。

    git 根名字为 ``main`` 且其父目录下确为 ``<父>/main`` 结构 → container；
    否则 flat（flat 是既有项目默认，保守不回退到容器）。
    """
    if (
        git_root is not None
        and git_root.name == _CONTAINER_MAIN_DIR
        and (git_root.parent / _CONTAINER_MAIN_DIR) == git_root
    ):
        return "container"
    return "flat"


def _build_layout(layout: str, main_wt: Path, warnings: list[str], source: str) -> dict[str, Any]:
    """由主工作树派生完整布局信息（任务 worktree 根 == runtime 根 == 主工作树父目录）。"""
    return {
        "layout": layout,
        "main_worktree": Path(main_wt).resolve(),
        "task_wt_root": Path(main_wt).resolve().parent,
        "runtime_root": Path(main_wt).resolve().parent / _RUNTIME_DIR,
        "marker_source": source,
        "warnings": warnings,
    }


def detect_layout(project_root: Path) -> dict[str, Any]:
    """解析双布局：主工作树 / 任务 worktree 根 / 共享账本 runtime 根。

    优先读布局标记（``<project_root>/.orchd/.layout.json``，权威）；缺失时
    自动探测（git 定位主工作树）+ 告警（不静默跑错目录）。

    Args:
        project_root: 主工作树根（container 下为 ``<容器>/main/``，flat 下为
            ``<项目根>/``）。

    Returns:
        ``{"layout": "container"|"flat", "main_worktree": <Path>,
        "task_wt_root": <Path>, "runtime_root": <Path>,
        "marker_source": "marker"|"detected", "warnings": [str]}``
    """
    project_root = Path(project_root).resolve()
    orchd_dir = project_root / ".orchd"
    warnings: list[str] = []

    marker = read_layout(orchd_dir)
    if marker is not None:
        main_wt = Path(marker["main_worktree"])
        if main_wt.resolve() != project_root:
            warnings.append(
                f"布局标记 main_worktree（{main_wt}）与当前目录（{project_root}）不一致，"
                "以标记为准"
            )
        return _build_layout(marker["layout"], main_wt, warnings, "marker")

    # 标记缺失 → 自动探测（git 定位主工作树）+ 告警
    git_root = _git_toplevel(project_root) or project_root
    layout = _auto_detect_layout(git_root, project_root)
    warnings.append(
        f"布局标记缺失（{marker_path(orchd_dir)}），自动探测为 {layout}"
    )
    return _build_layout(layout, git_root, warnings, "detected")


def detect_container_root_cwd(cwd: Path, orchd_dir: Path) -> tuple[str | None, Path | None]:
    """检测 cwd 是否为容器根（主工作树父目录），纪律护栏的判定核心。

    容器根特征：其 ``.orchd`` 是 junction/符号链接（指向主工作树 .orchd），
    ``_find_orchd_dir`` 会命中它，使 project_root 被解析成容器根而非主工作树，
    进而污染任务 worktree 布局标记、引发 worktree/分支误删
    （2026-08-30 task-audit-* 分支丢失复盘）。

    Returns:
        ``(reason, main_wt)``：
        - cwd 为容器根 → ``("container_root", <main_worktree>)``；
        - 否则 → ``(None, None)``。
        标记缺失 / 非 container 布局 / 读取异常 → ``(None, None)``（best-effort）。
    """
    try:
        cwd = Path(cwd).resolve()
        marker = read_layout(Path(orchd_dir))
        if marker is None or marker.get("layout") != "container":
            return None, None
        main_wt = Path(marker["main_worktree"]).resolve()
        if cwd == main_wt.parent:
            return "container_root", main_wt
    except Exception:
        return None, None
    return None, None


def nearest_git_root(start: Path) -> Path | None:
    """起点所属的**最近 git 仓库根**（``git rev-parse --show-toplevel``，绝对路径）。

    与 :func:`main_worktree_root` 的分工：后者用 ``--git-common-dir`` 区分
    「linked worktree → 共同主根」与「独立仓库 → 自身」服务 canonical 根解析；
    本函数只回答「起点当前站在哪个 git 仓库里」，用作 ``.orchd`` 向上查找时
    **不可逾越的边界**。非 git 目录 / git 不可用 / 异常 → None（调用方降级）。
    """
    root = _git_toplevel(Path(start))
    return root.resolve() if root is not None else None


def find_orchd_dir_within_git_boundary(start: Path | None = None) -> Path:
    """从 ``start``（默认 cwd）向上定位第一个 ``.orchd/``，但不越过最近 git 仓库根。

    边界规则（task-canonical-root-boundary-guard，AC1）：

    - 起点位于某 git 仓库 R 内：仅在闭区间 ``[start .. R 根]`` 内查找 ``.orchd``。
      区间内（含 R 根）有 ``.orchd`` → 返回它；一路到 R 根都没有 → 返回
      ``start/.orchd``（按 flat/自身处理），**绝不**继续爬到 R 的祖先。这堵住
      「pytest tmp_path 落在宿主仓库内、其层级目录又被 ``git init`` 成独立仓库」
      时解析越过内层仓库顶、误把宿主真实 ``.orchd`` 当工作区的事故。
    - linked worktree：R 根即该任务 worktree 根，其下有引擎传播的 ``.orchd``，
      在区间内即命中（master 读取再由 resolve_canonical_project_root 归主，零回归）。
    - 起点不在任何 git 仓库（``nearest_git_root`` 为 None）：维持历史行为，逐级
      向上直到命中（发布态自包含 / 非 git 降级路径，零回归）。
    """
    start = Path(start or Path.cwd()).resolve()
    git_root = nearest_git_root(start)
    for parent in [start, *start.parents]:
        candidate = parent / ".orchd"
        if candidate.is_dir():
            return candidate
        if git_root is not None and parent == git_root:
            # 已查至最近 git 仓库根仍无 .orchd：停止，禁止越界继续上爬。
            break
    return start / ".orchd"


def resolve_canonical_project_root(project_root: Path) -> Path:
    """解析 canonical 项目根（统一共享读入口：主工作树根，task-canonical-project-root）。

    业务读（pool/status/request 等加载 ``_master.json``）统一从 canonical 主工作树
    读取，避免任务 worktree 本地 checkout 副本与主工作树不同步导致的任务池不一致。

    仓库边界（task-canonical-root-boundary-guard，AC1/AC2）：本函数与
    :func:`find_orchd_dir_within_git_boundary` 同源遵守「不得越过起点所属的最近
    git 仓库根」。标记缺失时经 ``main_worktree_root`` 的 ``--git-common-dir``
    判定——**独立 git 仓库**的 common-dir 指向其自身 ``.git`` → 返回该仓库根
    （其根无 ``.orchd`` 时按 flat/自身处理，不爬向宿主）；仅 **linked worktree**
    （common-dir 指向共享主 ``.git``）才归主工作树根。

    - container 布局 → 返回主工作树根（``<容器>/main/``，布局标记权威）；
    - flat 布局 → 返回 ``project_root`` 自身（单 worktree，零回归）；
    - 标记缺失 → git 公共目录定位主工作树（linked worktree 返回同一主 ``.git``，
      主 worktree / 独立仓库 / flat 返回自身）；
    - 非 git / 解析失败 → best-effort 返回 ``project_root``（调用方降级，不阻断）。

    Args:
        project_root: 任意 worktree 根（主工作树或任务 worktree）。

    Returns:
        canonical 主工作树根（Path）。
    """
    project_root = Path(project_root).resolve()

    # 1) 布局标记优先（权威；任务 worktree 由 _propagate_container_marker 写入标记）
    marker = read_layout(project_root / ".orchd")
    if marker is not None:
        if marker.get("layout") == "container":
            try:
                main_wt = Path(marker["main_worktree"]).resolve()
                # 跨环境防御：标记中的绝对路径在当前环境可能无效（如沙箱→本机混合路径），校验 is_dir()，无效回退 project_root
                if main_wt.is_dir():
                    return main_wt
                return project_root
            except Exception:
                return project_root
        return project_root  # flat：自身

    # 2) 标记缺失 → git 公共目录定位主工作树（flat 单 worktree 返回自身）
    try:
        from orchd.gitops import main_worktree_root

        return main_worktree_root(project_root)
    except Exception:
        return project_root


def resolve_master_path(store: Store) -> Path:
    """解析 master 单一真源路径：本地副本优先，缺失回退 canonical 主工作树。

    根因锚点（task-master-single-copy）：container 布局下任务 worktree 已抑制
    本地 ``.orchd/_master.json`` 副本，凡裸读 ``store.orchd_dir/_master.json``
    且只判 ``exists()`` 的代码会静默按「文件不存在」降级（配置 / 检测形同失效）。
    本函数是 master 路径解析的**唯一**实现（task-master-path-single-source），
    供既有三处消费点复用：

    - ``orchd/onboard/lifecycle/regression.py``（``_full_regression_enabled``）；
    - ``orchd/onboard/_config.py``（``_load_config_blocked``）；
    - ``orchd/doctor.py``（``_detect_ghost_tasks``，container 下曾因本地副本
      缺失而静默空返回）。

    解析语义：本地 ``store.orchd_dir/_master.json`` 存在则直接返回（flat 布局
    canonical == 本地，主工作树调用亦命中本地 → 行为零回归）；缺失时经
    :func:`resolve_canonical_project_root` 归位主工作树（唯一权威）取其
    ``.orchd/_master.json``。本函数不判定文件存在性、不抛异常（canonical
    解析失败时 best-effort 回退 project_root 自身），存在性由调用方判定。

    规则底座 = :func:`resolve_master_path_from_dir`（本函数仅为 store 版薄适配层）；
    只持有目录的消费点（``lessons.load_lessons_config`` /
    ``gitops_ops._read_task_verify_command``，task-master-path-resolver-convergence）
    复用底座，不再裸拼 ``.orchd/_master.json``。

    Args:
        store: 账本存储对象（仅使用 ``orchd_dir`` 属性，duck typing）。

    Returns:
        解析出的 ``_master.json`` 路径（不一定存在，由调用方判定）。
    """
    local = Path(store.orchd_dir) / "_master.json"
    if local.exists():
        return local
    canonical = resolve_canonical_project_root(Path(store.orchd_dir).parent)
    return Path(canonical) / ".orchd" / "_master.json"


def resolve_master_path_from_dir(orchd_dir: Path | str) -> Path:
    """``resolve_master_path`` 的**路径级底座**：master 单一真源的唯一解析规则。

    规则（与 store 版逐字同源，本地优先 → canonical 主工作树回退）：
    本地 ``<orchd_dir>/_master.json`` 存在则返回之；否则经
    :func:`resolve_canonical_project_root` 归位 canonical 主工作树，取
    ``<canonical>/.orchd/_master.json``。不判存在性、不抛异常。

    存在意义（task-master-path-resolver-convergence）：此前只持有目录的消费点各自
    裸拼 ``<orchd_dir>/_master.json``，在 container 布局（任务 worktree 的副本被
    sparse-checkout 抑制）下 `exists()` 为假 → **静默回落默认值**：
    ``lessons.load_lessons_config`` 忽略 master 里的 lessons 段（review_timeout 回落
    60 分钟）、``gitops_ops._read_task_verify_command`` 取不到 verify_command（union
    合并降级为逐文件 pytest）。收敛到本函数后，「master 读路径」只有一处规则。

    Args:
        orchd_dir: ``.orchd`` 目录（不是项目根；调用方需自行拼 ``.orchd``）。

    Returns:
        解析出的 ``_master.json`` 路径（不一定存在，由调用方判定）。
    """
    local = Path(orchd_dir) / "_master.json"
    if local.exists():
        return local
    canonical = resolve_canonical_project_root(Path(orchd_dir).parent)
    return Path(canonical) / ".orchd" / "_master.json"


def _default_master(main_worktree: Path) -> dict[str, Any]:
    """生成 container 新项目的默认 master（通过 schema 的最小合法项目）。"""
    return {
        "schema_version": 1,
        "project": {
            "name": "New Orchd Project",
            "brief": "A new orchd-managed project (container layout).",
        },
        "modules": [
            {
                "id": "mod-core",
                "name": "Core",
                "role": "Engine core module",
            }
        ],
        "tasks": [
            {
                "id": "task-sample",
                "name": "Sample Task",
                "brief": "Sample task created by orchd init.",
                "module": "mod-core",
                "depends_on": [],
                "estimated_hours": 1,
                "importance": "high",
                "difficulty": "low",
                "requires": ["python"],
                "acceptance_criteria": ["sample"],
                "files_to_read": [],
                "files_to_edit": ["README.md"],
                "reviewers": ["reviewer-1"],
            }
        ],
    }


def bootstrap_container(project_root: Path, master_path: Path) -> dict[str, Any]:
    """container 新项目初始化（orchd init 无既有 master 时调用，AC3）。

    零额外操作：建 ``main/``（git init）→ 默认 master 写入 ``<容器>/.orchd/`` →
    移入 ``main/.orchd/`` → 建 ``.orchd-runtime/`` → 写 container 布局标记。

    Args:
        project_root: 容器根（``<容器>/``，本身不是 git 仓库）。
        master_path: 期望的 master 路径（``<容器>/.orchd/_master.json``）。

    Returns:
        ``{"container": True, "main_worktree": <Path>, "runtime_root": <Path>,
        "marker": <Path>, "created": [<str>]}``
    """
    project_root = Path(project_root).resolve()
    main_dir = project_root / _CONTAINER_MAIN_DIR
    created: list[str] = []

    # 初始化串行化（task-admission-lock-engine：E 项）—— 并发 init 竞态防护。
    # 直接把 master / 布局落盘到最终稳定的 main/.orchd（不再经「暂存目录 +
    # shutil.move」搬运）：锁文件始终落在 main/.orchd/.intake.lock（稳定、永不被
    # 搬动），避免 Windows 因持锁目录被 rename 而报 WinError 5/33（E999）。
    # 进程内可重入（ledger 注册表），同进程同路径二次获取不重复阻塞。
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    orchd_dst = main_dir / ".orchd"
    lk = None
    try:
        # 先建最终 orchd 目录（锁文件落点），再获取稳定的 .intake.lock
        orchd_dst.mkdir(parents=True, exist_ok=True)
        lk = intake_lock_acquire(orchd_dst, resolve_agent_id(orchd_dst))
        main_dir.mkdir(parents=True, exist_ok=True)
        created.append(str(main_dir.relative_to(project_root)))

        # 默认 master 直接写入 main/.orchd/（无需暂存 + move）
        master_file = orchd_dst / "_master.json"
        if not master_file.exists():
            master_file.write_text(
                json.dumps(_default_master(main_dir), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            created.append(str(master_file.relative_to(project_root)))

        # git init（best-effort；已是 git 仓库则跳过）
        if not (main_dir / ".git").exists():
            subprocess.run(
                ["git", "init", "-q"],
                cwd=str(main_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
            )

        # 共享账本 runtime 根
        runtime = project_root / _RUNTIME_DIR
        runtime.mkdir(parents=True, exist_ok=True)
        created.append(str(runtime.relative_to(project_root)))

        # 布局标记
        write_layout(orchd_dst, "container", main_dir)
        created.append(str(marker_path(orchd_dst).relative_to(project_root)))

        result = {
            "container": True,
            "main_worktree": str(main_dir),
            "runtime_root": str(runtime),
            "marker": str(marker_path(orchd_dst)),
            "created": created,
        }
    finally:
        if lk is not None:
            intake_lock_release(lk)
    return result


def _rollback_layout_migration(
    project_root: Path,
    main_dir: Path,
    runtime: Path,
    moved: list[str],
    ledger_moved: list[str] | None = None,
) -> list[dict[str, Any]]:
    """回滚半迁移状态（W-11）：账本文件移回 → 删标记 → ``.orchd`` 逐条移回 →
    其余条目移回 → 清空目录。

    ``.orchd`` 必须**逐条**移回（不能目录级 move：目标 ``project_root/.orchd``
    已存在且仍持有 .intake.lock，Windows 下目录级 move 会失败/冲突）。

    返回**回滚失败项** ``[{"item", "error"}]``：迁移/回滚过程中的 OSError 一律上报
    调用方（旧实现 ``except OSError: pass`` 静默吞掉，留下无人知晓的半迁移残留）。
    """
    errors: list[dict[str, Any]] = []
    new_orchd = main_dir / ".orchd"
    orig_orchd = project_root / ".orchd"
    for name in list(ledger_moved or []):
        src = runtime / name
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(new_orchd / name))
        except OSError as exc:
            errors.append({"item": f"ledger:{name}",
                           "error": f"{type(exc).__name__}: {exc}"})
    try:
        marker = marker_path(new_orchd)
        if marker.exists():
            marker.unlink()
    except OSError as exc:
        errors.append({"item": "marker", "error": f"{type(exc).__name__}: {exc}"})
    if new_orchd.is_dir():
        orig_orchd.mkdir(parents=True, exist_ok=True)
        for sub in sorted(new_orchd.iterdir()):
            dst = orig_orchd / sub.name
            if dst.exists():
                continue  # 原位置已有同名条目（.intake.lock 等）→ 保留原物
            try:
                shutil.move(str(sub), str(dst))
            except OSError as exc:
                errors.append({"item": f".orchd/{sub.name}",
                               "error": f"{type(exc).__name__}: {exc}"})
    for name in list(moved):
        if name == ".orchd":
            continue
        src = main_dir / name
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(project_root / name))
        except OSError as exc:
            errors.append({"item": name, "error": f"{type(exc).__name__}: {exc}"})
    for d in (new_orchd, main_dir, runtime):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError as exc:
            errors.append({"item": f"rmdir:{d.name}",
                           "error": f"{type(exc).__name__}: {exc}"})
    return errors


def _intake_lock_filename() -> str:
    """准入锁文件名（单一事实源：``orchd.ledger._INTAKE_LOCK_FILENAME``）。"""
    try:
        from orchd.ledger import _INTAKE_LOCK_FILENAME

        return _INTAKE_LOCK_FILENAME
    except Exception:
        return ".intake.lock"


def layout_migrate(project_root: Path) -> dict[str, Any]:
    """flat → container 迁移**入口**（W-11：全程持 ``.intake.lock`` 串行化）。

    与并发 amend / intake 写（同一把 ``.intake.lock``）互斥：迁移会搬移
    ``.orchd`` 运行时文件并写布局标记，无锁并发会留下「锁根与账本根分裂」的
    半迁移态。取锁失败即拒绝迁移（**不做无锁迁移**），返回结构化原因。

    Args:
        project_root: flat 主工作树根。

    Returns:
        迁移实现 :func:`_layout_migrate_impl` 的结果；取锁失败时为
        ``{"migrated": False, "reason": "intake_lock_unavailable", ...}``。
    """
    from orchd.ledger import intake_lock_acquire, intake_lock_release

    project_root = Path(project_root).resolve()
    try:
        lock = intake_lock_acquire(project_root / ".orchd", "layout-migrate")
    except Exception as exc:
        return {
            "migrated": False,
            "reason": "intake_lock_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "准入锁不可得（并发准入写进行中或锁损坏），稍后重试",
        }
    try:
        return _layout_migrate_impl(project_root)
    finally:
        try:
            intake_lock_release(lock)
        except Exception:
            pass


def _layout_migrate_impl(project_root: Path) -> dict[str, Any]:
    """迁移实现（调用方须已持 ``.intake.lock``，见 :func:`layout_migrate`）。

    flat → container 迁移：内容整体移入 ``main/``，保留 git 历史、可回滚（AC4）。

    前置校验（AC4）：工作区干净（无已跟踪改动）、无活跃 linked worktree 冲突。
    迁移过程 best-effort：任一步骤失败则尝试把已移动条目移回（可回滚，不破坏
    原仓库）；成功输出受影响路径清单。

    无效文件自动清理（task-14-layout-migrate-junk-clean）：迁移前把保守版
    无效文件清单（OS 杂项/缓存/临时/日志，见 ``_JUNK_NAMES``/``_is_junk_entry``）
    隔离回收至 ``<runtime>/trash/``（可还原），而非带进 ``main/`` 造成目录污染；
    清理条目记入 ``cleaned``。绝不清理 ``.git`` / ``.orchd`` / 工具目录 / venv /
    被跟踪文件。

    运行时账本搬运（task-14-layout-migrate-ledger-move）：容器布局默认账本根为
    ``<runtime>/``，迁移后把历史账本（ledger/checkpoint/锁/session-worktrees/mod-*）
    从 ``main/.orchd/`` 搬到 ``<runtime>/``（见 ``_move_ledger_runtime_files``），
    保证迁移一次成功后引擎对既有任务状态立即可见；master 目录文件保留在
    ``main/.orchd/``。搬移条目记入 ``ledger_moved``。

    Args:
        project_root: flat 主工作树根（git 根，含 ``.git`` / ``.orchd``）。

    Returns:
        ``{"migrated": True, "main_worktree": <Path>, "moved": [<str>],
        "cleaned": [<str>], "ledger_moved": [<str>], "marker": <Path>,
        "runtime_root": <Path>, "trash_root": <Path>（有清理时）}``
        或 ``{"migrated": False, "reason": <str>, ...}``（前置校验失败）。
    """
    from orchd.gitops import check_workspace_state

    project_root = Path(project_root).resolve()
    main_dir = project_root / _CONTAINER_MAIN_DIR

    # 前置校验：工作区干净
    state = check_workspace_state(project_root)
    if state.get("available") and not state.get("clean"):
        return {
            "migrated": False,
            "reason": "dirty_workspace",
            "hint": "迁移要求工作区干净（无已跟踪改动），请先提交或还原后再试",
        }

    # 前置校验：无活跃 linked worktree（并发 worktree 冲突）
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        # W-16：按**行前缀**计数（``worktree <path>`` 块头），不再用子串
        # ``count("worktree ")``——``locked`` / ``prunable`` 行或路径里含该子串
        # 都会被误计，导致有 linked worktree 时判「无」或反之。
        if proc.returncode == 0 and len([
            ln for ln in (proc.stdout or "").splitlines()
            if ln.startswith("worktree ")
        ]) > 1:
            return {
                "migrated": False,
                "reason": "active_worktrees",
                "hint": "存在活跃 worktree，先清理（orchd doctor --prune-worktrees 或 git worktree prune）再迁移",
            }
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    main_dir.mkdir(parents=True, exist_ok=True)
    runtime = project_root / _RUNTIME_DIR
    runtime.mkdir(parents=True, exist_ok=True)
    trash_dir = runtime / _TRASH_DIR
    trash_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    cleaned: list[str] = []

    # 迁移：先把保守版无效文件（OS 杂项/缓存/临时/日志）隔离回收至 trash/
    # （不进入 main/，可还原），其余条目移入 main/。
    entries = sorted(
        p for p in project_root.iterdir()
        if p.name not in (_CONTAINER_MAIN_DIR, _RUNTIME_DIR)
    )
    try:
        for entry in entries:
            if _is_junk_entry(entry.name):
                shutil.move(str(entry), str(trash_dir))
                cleaned.append(str(entry.name))
                continue
            if entry.name in _TOOL_DIR_NAMES:
                # P2-8：工具目录/venv 留在原地，不搬入 main/（环境专属，非项目源码）
                continue
            if entry.name == ".orchd":
                # W-11：迁移全程持有 .orchd/.intake.lock，Windows 下**目录级 move 会
                # 因锁住的文件失败（WinError 33）** → 逐条搬移并跳过持锁文件本身
                # （锁在新 canonical 路径由下次准入写重建；旧文件留待 doctor 收敛）。
                dst_orchd = main_dir / ".orchd"
                dst_orchd.mkdir(parents=True, exist_ok=True)
                for sub in sorted(entry.iterdir()):
                    if sub.name == _intake_lock_filename():
                        continue
                    shutil.move(str(sub), str(dst_orchd / sub.name))
                moved.append(entry.name)
                continue
            dst = main_dir / entry.name
            shutil.move(str(entry), str(dst))
            moved.append(str(entry.name))
    except Exception as exc:
        # 可回滚：把已移动条目移回（best-effort，不破坏原仓库）。
        # W-11：回滚过程中的 OSError **不再静默**（记入 rollback_errors 上报）。
        rollback_errors = _rollback_layout_migration(
            project_root, main_dir, runtime, moved
        )
        return {
            "migrated": False,
            "reason": "move_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rollback_errors": rollback_errors,
            "hint": "迁移中断，已尝试回滚；请检查残留后重试",
        }

    # 共享账本 runtime 根 + 布局标记
    orchd_dir = main_dir / ".orchd"
    # W-11：搬移已成功后的写操作（写布局标记 / 移账本）任一失败 → **完整回滚**，
    # 避免「main/ 已就位但锁根/账本根分裂」的半迁移态（旧实现不回滚且吞 OSError）。
    try:
        write_layout(orchd_dir, "container", main_dir)
        ledger_moved, ledger_errors = _move_ledger_runtime_files(orchd_dir, runtime)
    except Exception as exc:
        rollback_errors = _rollback_layout_migration(
            project_root, main_dir, runtime, moved
        )
        return {
            "migrated": False,
            "reason": "post_move_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "rolled_back": list(moved),
            "rollback_errors": rollback_errors,
            "hint": "搬移完成后写标记/移账本失败，已回滚到 flat 布局；请检查残留后重试",
        }
    if ledger_errors:
        # 账本搬移部分失败 = 账本根分裂（部分在 main/.orchd、部分在 runtime）→
        # 同样回滚（含已写布局标记），并把失败项结构化上报（不静默）。
        rollback_errors = _rollback_layout_migration(
            project_root, main_dir, runtime, moved, ledger_moved
        )
        return {
            "migrated": False,
            "reason": "ledger_move_failed",
            "errors": ledger_errors,
            "rolled_back": list(moved),
            "rollback_errors": rollback_errors,
            "hint": "运行时账本搬移不完整，已回滚以保持锁根与账本根一致；请修复后重试",
        }

    result = {
        "migrated": True,
        "main_worktree": str(main_dir),
        "moved": moved,
        "cleaned": cleaned,
        "ledger_moved": ledger_moved,
        "marker": str(marker_path(orchd_dir)),
        "runtime_root": str(runtime),
    }
    if cleaned:
        result["trash_root"] = str(trash_dir)
    return result


# ------------------------------------------------------------------
# 任务 worktree 生命周期（task-14-worktree-lifecycle，阶段 1 并发引擎）
# ------------------------------------------------------------------
# 建（ensure_task_wt）/ 绑（bind_task_wt，session-worktrees.json 带锁）/
# 用（resolve_task_root）/ 回收（remove_task_wt）/ 清理（prune_orphans）
# 全由引擎自动处理，agent 无感（弱 LLM 友好：best-effort 降级 + 明确错误码 hint）。
#
# flat 降级路径（S2 回归面）：flat 单会话（无 linked worktrees）下不建独立
# worktree——任务 worktree 即主工作树本身（维持现状行为零回归）；container 布局
# 或已存在 linked worktrees（多会话并发）才建独立任务 worktree
# （``<task_wt_root>/task-<id>/``）。

_BINDINGS_FILENAME = "session-worktrees.json"
# 任务 worktree 目录名前缀
_TASK_WT_PREFIX = "task-"


def _task_wt_name(task_id: str) -> str:
    """任务 worktree 目录名：短 id 前缀 ``task-``（full id 去掉冗余 ``task-`` 前缀）。

    例：``task-14-worktree-lifecycle`` → ``task-14-worktree-lifecycle``；
    ``t1`` → ``task-t1``。
    """
    short = task_id[5:] if task_id.startswith("task-") else task_id
    return f"{_TASK_WT_PREFIX}{short}"


def worktree_hint(task_id: str) -> str:
    """任务 worktree 目录名提示（单一来源，task-review-diagnostics-hardening AC4）。

    内部使用 ``_task_wt_name`` 输出真实目录名，供 guard.py / review.py / doctor.py
    等多处调用方统一引用，消除 ``task-task-<id>`` 双前缀漏网（task_id 已含
    ``task-`` 前缀时再拼 ``task-`` 即产出双前缀）。

    例：``task-14-worktree-lifecycle`` → ``task-14-worktree-lifecycle``；
    ``t1`` → ``task-t1``。
    """
    return _task_wt_name(task_id)


def task_branch_head(project_root: Path, task_id: str) -> str | None:
    """best-effort 取 ``task/{task_id}`` 分支 tip SHA（审查基线单一事实源）。

    task-review-baseline-and-worktree-recycle-fix AC1：REVIEW_CLAIMED 的
    ``baseline_sha`` 与 review 提交期的 ``current_sha`` 必须取自**同一来源**
    ——任务分支 tip。修复前两处分别取「主工作树 HEAD」（认领期 project_root
    解析为主工作树，恒为 main HEAD）与「任务 worktree HEAD」（任务分支 tip），
    container 布局下二者必然不同，导致漂移检测恒误报（每次审查都输出
    ``baseline_warning``，实质审查基线校验失效）。

    git 不可用 / 分支不存在 / 异常 → None（调用方回退旧行为，best-effort）。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", f"task/{task_id}"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def branch_reflog_tip(project_root: Path, branch: str) -> str | None:
    """best-effort 取分支 reflog 中最新一条记录的 tip SHA。

    refs 目录被删但 reflog 存活时（``.git/logs/refs/heads/<branch>``，
    2026-08-08 / 2026-09-10 两次同型事故形态），reflog 末行即分支最后的落点，
    可作为 ``git branch <branch> <tip>`` 的重建依据。无 reflog / 解析失败 → None。
    """
    try:
        proc = subprocess.run(
            ["git", "reflog", "show", "--format=%H", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            # git reflog show 为倒序输出（最新在前）
            if lines:
                return lines[0]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    # ref 无法解析时（refs 被删）git reflog 失效 → 直读 reflog 文件（正序，末行最新）
    return _reflog_file_tip(project_root, branch)


def _reflog_file_tip(project_root: Path, branch: str) -> str | None:
    """直读 ``.git/logs/refs/heads/<branch>`` 末行的 new sha（ref 缺失兜底）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        git_dir = Path(proc.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (Path(project_root) / git_dir).resolve()
        log = git_dir / "logs" / "refs" / "heads" / branch
        if not log.is_file():
            return None
        lines = [
            ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
        if not lines:
            return None
        # reflog 行格式：<old> <new> <who> <ts> <tz>\t<message>
        fields = lines[-1].split()
        return fields[1] if len(fields) >= 2 else None
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        return None


def bindings_path(store_root: Path) -> Path:
    """返回任务↔worktree 绑定文件路径（位于共享账本根）。"""
    return Path(store_root) / _BINDINGS_FILENAME


def load_bindings(store_root: Path) -> dict[str, Any]:
    """读取任务↔worktree 绑定映射。

    三态区分（W-2 / R-22，禁止把损坏当空表）：
    - **文件不存在** → 返回 ``{}``（正常初次，调用方可安全覆写）；
    - **文件存在但 JSON 解析失败 / 非 dict 结构** → 抛 ``OrchdError(E002)``，
      调用方**不得**把它当作空绑定表继续覆写（否则一次坏绑定吞掉所有活跃绑定）；
    - **解析正常** → 返回 ``data``。

    抛出 E002 而非静默返回空 dict，是让 ``bind_task_wt`` / ``unbind_task_wt`` 等
    写路径在损坏时走到"保留原文件 + 备份损坏副本 + 告警 + 不覆写"，而非整表覆写。
    只读调用方（``resolve_task_root`` / prune）自行 catch E002 后按保守语义处理。
    """
    path = bindings_path(store_root)
    if not path.is_file():
        return {}
    from orchd.errors import ErrorCode, OrchdError

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise OrchdError(
            ErrorCode.E002,
            f"任务↔worktree 绑定文件损坏（{path}）：{exc}",
            [{"path": str(path), "message": str(exc)}],
        ) from exc
    if not isinstance(data, dict):
        raise OrchdError(
            ErrorCode.E002,
            f"任务↔worktree 绑定文件结构非法（非 dict）：{path}",
            [{"path": str(path), "kind": type(data).__name__}],
        )
    return data


def _save_bindings(store_root: Path, data: dict[str, Any]) -> None:
    """原子写入绑定映射（tmp + os.replace）。

    W-2 / R-22：写失败（磁盘满 / IO 错误）不得静默 —— 原文件保持不变（原子
    替换失败即不改动原文件），并清理残留 tmp，向上抛 ``OSError`` 由调用方告警。
    绝不用坏结果整表覆写成功绑定表。
    """
    path = bindings_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _backup_corrupt_bindings(store_root: Path, path: Path, cause: Exception) -> Path:
    """损坏绑定文件的隔离备份（保留原件，另存可排查副本，幂等）。

    W-2 / R-22：损坏发生时**绝不用空表覆写**成功绑定表。把损坏原件复制为
    ``<name>.corrupt-<ts>`` 副本归档（原件保持不动供继续排查），返回副本路径。
    若文件已损坏到无法读取（IO 层），仅备份保留；copy 失败也吞掉（告警以
    ``OrchdError.details`` 承载，不因备份失败升级为中断）。
    """
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    corrupt = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        shutil.copy2(path, corrupt)
    except OSError:
        # 备份失败不阻断主告警链；cause 已在抛出的 OrchdError 承载
        pass
    return corrupt


def bind_task_wt(store_root: Path, task_id: str, worktree_path: Path) -> dict[str, Any]:
    """绑定「任务 ↔ worktree」到共享账本根 session-worktrees.json（带 Store 文件锁）。

    Args:
        store_root: 共享账本根（resolve_store_dir 结果；未设 ORCHD_HOME 时随布局）。
        task_id: 任务 ID。
        worktree_path: 该任务的 worktree 根（独立任务 worktree 或主工作树降级）。

    Returns:
        ``{"bound": True, "task_id": <str>, "worktree": <str>}``

    Raises:
        ``OrchdError(E002)``：绑定文件已损坏时**中断且不覆写**（保留原件 +
        备份损坏副本 + 结构化告警），防止一次坏绑定吞掉其它活跃任务绑定。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import Store

    store = Store(store_root)
    store.acquire_lock()
    try:
        try:
            data = load_bindings(store_root)
        except OrchdError as exc:
            corrupt = _backup_corrupt_bindings(store_root, bindings_path(store_root), exc)
            raise OrchdError(
                ErrorCode.E002,
                f"绑定文件损坏，中止绑定 {task_id} 以避免覆写其它活跃绑定：{exc.message}",
                [{"corrupt_backup": str(corrupt), "path": str(bindings_path(store_root))}],
            ) from exc
        data[task_id] = {
            "worktree": str(Path(worktree_path).resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_bindings(store_root, data)
    finally:
        store.release_lock()
    return {"bound": True, "task_id": task_id, "worktree": str(Path(worktree_path).resolve())}


def unbind_task_wt(
    store_root: Path, task_id: str, *, lock_held: bool = False,
) -> dict[str, Any]:
    """解绑「任务 ↔ worktree」（终态回收时调用，带 Store 文件锁）。

    ``lock_held=True``：调用方（如 code 合并流）已在同一共享账本根持有 ``.lock``
    （同进程）。此时解绑**不得再次 flock**——同一进程内两个独立 open() 的 fd 在
    POSIX flock 下互相独立/互斥（Linux flock(2)「以另一 fd 加锁可能被本进程已有锁
    拒绝」），重 flock 会自锁死 → E012。复用已持锁，跳过 acquire/release。

    Returns:
        ``{"unbound": True, "task_id": <str>}``（无绑定也返回 unbound=True，幂等）。

    Raises:
        ``OrchdError(E002)``：绑定文件已损坏时中断（备份损坏副本 + 告警），
        不尝试在不可解析的表基础上 pop+覆写（避免制造二次损坏）。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import Store

    store = Store(store_root)
    if not lock_held:
        store.acquire_lock()
    try:
        try:
            data = load_bindings(store_root)
        except OrchdError as exc:
            corrupt = _backup_corrupt_bindings(store_root, bindings_path(store_root), exc)
            raise OrchdError(
                ErrorCode.E002,
                f"绑定文件损坏，中止解绑 {task_id}：{exc.message}",
                [{"corrupt_backup": str(corrupt), "path": str(bindings_path(store_root))}],
            ) from exc
        data.pop(task_id, None)
        _save_bindings(store_root, data)
    finally:
        if not lock_held:
            store.release_lock()
    return {"unbound": True, "task_id": task_id}


def resolve_task_root(store_root: Path, task_id: str) -> Path | None:
    """从绑定解析任务的 worktree 根；无绑定返回 None。

    W-9 / R-36：登记路径做 ``is_dir()`` 校验（当前环境有效性）。跨环境（沙箱→
    本机 / 拷贝 runtime 根）迁移后登记路径可能指向本机不存在的目录 —— 直接返回
    会让 ``guard_task_root`` E018 指向不存在目录、agent 死循环。失效登记：
    1) 返回 None（调用方守旧语义，当作未绑定）；2) **自动解绑**该任务（best-effort，
       解绑失败**不阻断**返回值 —— 见下方兜底注释）；3) 落一条引擎回收留痕
       （``_log_recycle`` 的 ``unbind_stale_binding`` 动作，stderr ``[回收]`` +
       recycle_log），并注明失效登记与实际解绑结果。

    Returns:
        ``Path``：绑定且当前环境有效的 worktree 绝对路径；或 None（未绑定 /
        登记路径失效已解绑 / 绑定文件损坏）。
    """
    from orchd.errors import ErrorCode, OrchdError

    try:
        data = load_bindings(store_root)
    except OrchdError:
        # 损坏时不冒险自动解绑（_backup+告警 在 bind/unbind 入口处理，这里只读降级）
        return None
    entry = data.get(task_id)
    if not entry or not entry.get("worktree"):
        return None
    bound = Path(entry["worktree"])
    if not bound.is_dir():
        # 失效登记：留痕 + 自动解绑 + 返回 None（不把失效路径交给 guard 死循环）。
        # 两处兜底都必须是 **宽 except**：本路径是 guard 读路径，且 _save_bindings
        # 现在会 re-raise OSError（只读/权限/磁盘满）——只 catch OrchdError 会让
        # 读路径抛裸 OSError 落 E999（返工整改：与「解绑失败不阻断返回值」契约对齐）。
        try:
            _log_recycle([{
                "action": "unbind_stale_binding",
                "task_id": task_id,
                "target": str(bound),
                "reason": "登记 worktree 路径在本环境不存在（is_dir=False），自动解绑",
                "actor": _recycle_actor(),
            }])
        except Exception:
            pass
        try:
            unbind_task_wt(store_root, task_id, lock_held=False)
        except Exception:
            pass
        return None
    return bound


def resolve_task_branch(store_root: Path, task_id: str) -> str | None:
    """登记表权威分支（W-4）：任务绑定 worktree → 该 worktree 真实分支；无绑定 None。

    分支判定以 `session-worktrees.json` 登记表为**权威来源**：guidance 的
    ``branch_ctx`` 应描述引擎即将操作的 worktree，而非工具进程的 cwd。无绑定
    （flat 兼容 / 未走 claim 绑定）返回 None，由调用方回退 cwd（最后一次兜底）。

    best-effort：读取 / git 探测异常静默返回 None，不阻塞引导。
    """
    wt = resolve_task_root(store_root, task_id)
    if not wt:
        return None
    try:
        from orchd.gitops import get_current_branch
        return get_current_branch(wt)
    except Exception:
        return None


def guard_task_root(
    project_root: Path | None,
    store_root: Path,
    task_id: str,
    command: str = "done",
) -> dict[str, Any]:
    """守卫（AC2）：目标 root == 任务 worktree，不一致 → E018（防错目录提交/审查）。

    无绑定（flat 单会话兼容 / 未走 claim 绑定的场景）→ 跳过（best-effort）。
    传入 project_root 为 None（单元测试 / 非 git）→ 跳过。

    Returns:
        ``{"guarded": True, "bound_root": <str|None>}``（不抛异常时）。
    """
    from orchd.errors import ErrorCode, OrchdError

    if project_root is None:
        return {"guarded": True, "bound_root": None}
    bound = resolve_task_root(store_root, task_id)
    if bound is None:
        return {"guarded": True, "bound_root": None}
    if os.path.normcase(str(Path(project_root).resolve())) != os.path.normcase(
        str(bound.resolve())
    ):
        raise OrchdError(
            ErrorCode.E018,
            f"wrong_worktree: {command} 目标目录（{project_root}）与任务 {task_id} 的 "
            f"worktree（{bound}）不一致",
            [{
                "task_id": task_id,
                "current_root": str(Path(project_root).resolve()),
                "expected_root": str(bound.resolve()),
                "hint": f"请在任务 worktree 目录内执行 {command}（或使用 -C 指向任务 worktree）",
            }],
        )
    return {"guarded": True, "bound_root": str(bound)}


def _propagate_container_marker(task_wt: Path, main_wt: Path) -> None:
    """把 container 布局标记写入任务 worktree 的 ``.orchd/``（best-effort）。

    端到端修复（task-14-layout-migrate-junk-clean 实测暴露）：任务 worktree 是
    git checkout，布局标记（``.layout.json``）被 gitignore 未跟踪 → 从 worktree
    执行 orchd 命令时 ``resolve_store_dir`` 读不到标记，回退到 worktree 本地空
    账本，共享任务状态不可见（done 报 "not in claimed"）。写入标记后 worktree
    自识别 container 布局 → 共享账本根（``<容器>/.orchd-runtime/``）生效。
    失败静默降级（仍可经 ORCHD_HOME 指向共享账本根）。

    task-workspace-docs-isolation：同步工作区规划文档 ROADMAP.md（宿主资产、唯一源
    在宿主项目根；任务 worktree 缺副本 → roadmap_landing_warnings 读本地缺失被跳过，
    与 main 行为不一致）。best-effort 从宿主项目根拷贝（源经
    ``ledger.resolve_roadmap_path`` 定位，目标为任务 worktree 的同一相对位置）；
    源缺失时留痕跳过（不静默）；ROADMAP.md 未跟踪时拷贝不产生未提交改动（不触发
    E017）。
    """
    try:
        orchd = task_wt / ".orchd"
        orchd.mkdir(parents=True, exist_ok=True)
        write_layout(orchd, "container", main_wt)
    except Exception:
        pass
    try:
        # task-roadmap-root-resolution：ROADMAP 同步源随归根（宿主根优先、
        # .orchd/ 回退）而变——统一走 resolve_roadmap_path，目标保持与源相对
        # canonical 主工作树的**同一相对位置**（根版 → 任务 worktree 根，
        # .orchd/ 版 → 任务 worktree .orchd/）。跳过时留痕（不静默）。
        from orchd.ledger import resolve_roadmap_path

        src = resolve_roadmap_path(main_wt)
        if src.exists():
            dst = task_wt / src.relative_to(Path(main_wt))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
    except (OSError, ValueError) as exc:
        # best-effort：同步失败不影响 worktree 可用性，但须留痕（禁静默跳过）
        print(
            "orchd ▸ [worktree] {\"action\": \"roadmap_sync_skipped\", "
            f"\"error\": \"{exc}\"}}",
            file=sys.stderr,
        )


# _master.json 副本抑制（task-master-single-copy）：container 任务 worktree 不再
# 保留 .orchd/_master.json，唯一权威 = 主工作树。用 git sparse-checkout --no-cone
# 仅排除该文件（.orchd/ 其余文件与全仓库仍正常检出）。4 条 pattern 已实证：
# worktree 内文件不存在、git status 干净、git add -A/commit 不记录删除、主工作树
# 前进后 merge 不复活（skip-worktree 方案 merge 时会复活，故为主方案）。
_MASTER_IGNORE_PATTERNS = ["/*", "!/.orchd/", "/.orchd/*", "!/.orchd/_master.json"]


def _git_minor_version() -> int:
    """解析 git 版本为 ``major*100 + minor``（如 2.25 → 225、3.0 → 300）。

    W-14（2026-09-15）：旧实现只返回次版本号（``2.25 → 25``），丢主版本号 ——
    git 3.x 时 ``3.0 → 0`` 会把 ``version >= 25`` 判为**假**，静默关闭
    sparse-checkout（能力探测方向反了）。改口径后比较须用 ``>= 225``。
    解析失败返回 0（按不可用处理）。
    """
    try:
        out = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).stdout or ""
        m = re.search(r"(\d+)\.(\d+)", out)
        if not m:
            return 0
        return int(m.group(1)) * 100 + int(m.group(2))
    except Exception:
        return 0


def _suppress_task_master_copy(wt_path: Path) -> dict[str, Any]:
    """抑制任务 worktree 内 ``.orchd/_master.json`` 副本（best-effort，降级可审计）。

    必须经 Python ``subprocess.run`` 以参数列表直传（``shell=False``），避免
    Git for Windows 的 MSYS 路径转换改写 ``!/...`` pattern（``!/.orchd/`` 等
    会被当作参数做路径归一化，导致 pattern 失效）。

    Returns:
        ``{"ok": bool, "method": str, "reason": str|None}``；``method`` ∈
        ``sparse-checkout`` / ``skip-worktree`` / ``none``。sparse 不可用
        （git < 2.25 或失败）→ 降级 skip-worktree；再失败 → 保留副本 + 告警
        （``ok=False, method="none"``，由调用方以 degraded 透出）。
    """
    version = _git_minor_version()
    if version >= 225:  # W-14：口径 major*100+minor（2.25 → 225；git 3.0 → 300）
        init = subprocess.run(
            ["git", "sparse-checkout", "init", "--no-cone", str(wt_path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(wt_path),
        )
        # 陈旧 sparse 状态：残余 in-cone 标记/禁用；先 reset（--no-cone 下不保留
        # 先前 pattern），保证 set 前为纯 no-cone 基线。
        if init.returncode == 0:
            setp = subprocess.run(
                ["git", "sparse-checkout", "set", *_MASTER_IGNORE_PATTERNS],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                cwd=str(wt_path),
            )
            if setp.returncode == 0:
                # 收敛校验：明确的抑制目标文件应已从 worktree 消失。
                if not (wt_path / ".orchd" / "_master.json").exists():
                    return {"ok": True, "method": "sparse-checkout", "reason": None}
                return {
                    "ok": False, "method": "none",
                    "reason": "sparse-checkout set 成功但 .orchd/_master.json 仍存在",
                }
            reason = (setp.stderr or "").strip()[:200]
            return {
                "ok": False, "method": "none",
                "reason": f"sparse-checkout set 失败: {reason or 'exit' + str(setp.returncode)}",
            }
    # 降级：skip-worktree（merge 时副本可能复活，仍优于保留双副本）
    sw = subprocess.run(
        ["git", "update-index", "--skip-worktree", ".orchd/_master.json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        cwd=str(wt_path),
    )
    if sw.returncode == 0:
        return {
            "ok": True, "method": "skip-worktree",
            "reason": "sparse-checkout 不可用，回退 skip-worktree（merge 时副本可能复活）",
        }
    return {
        "ok": False, "method": "none",
        "reason": "sparse-checkout / skip-worktree 均不可用，保留副本（双副本漂移风险）",
    }


def _master_suppression_entry(wt_path: Path) -> dict[str, Any]:
    """生成 ``ensure_task_wt`` 返回字典中的 ``master_suppression`` 段。"""
    supp = _suppress_task_master_copy(wt_path)
    # 抑制失败 → 副本残留，随 claim 透出 degraded 供 agent 可见（禁止静默）
    return {
        "master_suppression": supp,
        "degraded": supp.get("ok") is False,
        "degraded_reason": (
            f"master 副本抑制失败（{supp.get('method')}）：{supp.get('reason')}"
            if supp.get("ok") is False else None
        ),
    }


def ensure_task_wt(project_root: Path, task_id: str) -> dict[str, Any]:
    """创建/复用任务 worktree（best-effort，幂等）。

    container 布局 → 建独立任务 worktree（``git worktree add <task_wt_root>/task-<id>
    task/<id>``，已存在幂等复用）；flat（既有项目）→ 降级返回主工作树
    （任务 worktree 即主工作树本身，维持现状零回归）。

    Returns:
        ``{"worktree": <Path>, "separate": bool, "created": bool}``；
        container 下独立 worktree 创建失败时返回 ``{"worktree": 主工作树,
        "separate": False, "created": False, "degraded": True, "reason": str}``
        （不抛异常、不阻断 claim，但**降级原因必须显式记录**，禁止静默）。
    """
    project_root = Path(project_root).resolve()
    layout = detect_layout(project_root)

    # 仅 container 布局（推荐并发形态）才建独立任务 worktree；flat（既有项目 /
    # 遗留 merge-wt 多 worktree 场景）保持降级——任务 worktree 即主工作树，
    # 维持现状行为零回归（S2 flat 降级路径）。
    separate = layout["layout"] == "container"
    if not separate:
        return {"worktree": project_root, "separate": False, "created": False}

    branch = f"task/{task_id}"
    wt_path = layout["task_wt_root"] / _task_wt_name(task_id)
    # AC4（task-review-baseline-and-worktree-recycle-fix）：目录名单一来源与不变量。
    # 禁止用含 / 的分支名（task/<id>）拼路径——否则容器根出现 task/task-<id>
    # 嵌套路径（2026-09-10 事故实测形态：task/task-release-docs-version-sync）。
    if wt_path.name != _task_wt_name(task_id) or "/" in wt_path.name or "\\" in wt_path.name:
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": (
                f"worktree 目录名非法（{wt_path.name}）：须由 _task_wt_name(task_id) "
                "生成，禁止使用含分隔符的分支名拼路径"
            ),
        }
    try:
        if (wt_path / ".git").exists():
            # 已存在且是 worktree → 幂等复用（补写布局标记 + 抑制副本）
            # W-4（2026-09-15）：复用路径**必须同样过创建期不变量校验**——「见 .git
            # 即判幂等」会把半检出（HEAD 无法解析）/ 元数据丢失 / 检出其它分支的目录
            # 当作有效 worktree 绑定，agent 随后在错误分支或残缺检出上作业（静默）。
            verify = _verify_task_wt(wt_path, branch)
            if not verify["ok"]:
                _cleanup_stale_task_wt(project_root, wt_path)
                return {
                    "worktree": project_root,
                    "separate": False,
                    "created": False,
                    "degraded": True,
                    "reason": f"复用路径校验失败：{verify['reason']}",
                }
            _propagate_container_marker(wt_path, project_root)
            result = {"worktree": wt_path, "separate": True, "created": False}
            result.update(_master_suppression_entry(wt_path))
            return result
        # 创建期不变量硬化（W-4，复盘 P1 孤儿分支修复）：任务分支必须从**主分支**
        # fork（`git worktree add -b task/<id> <path> <main>`），杜绝因主工作树当
        # 前检出的非 main 分支而生成孤儿/悬空分支。base 解析失败（无 main/master）
        # 则回退当前 HEAD（best-effort），仍可创建但依赖探测结果。
        from orchd.gitops import get_default_branch
        base = get_default_branch(project_root)
        # AC4：建/认领 worktree 只写 .git/worktrees/<name>/HEAD，绝不改写主工作树
        # .git/HEAD（事故形态：主工作树 HEAD 被写成 ref: refs/heads/task/... →
        # 主工作树 unborn、git status 整树 staged、done 被 E017 误拦）。
        main_head_before = _read_main_worktree_head(project_root)
        add_cmd = ["git", "worktree", "add", "-b", branch, str(wt_path)]
        if base:
            add_cmd.append(base)
        proc = subprocess.run(
            add_cmd,
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        if proc.returncode == 0 and (wt_path / ".git").exists():
            # AC4：HEAD 落点不变量校验（对照组为上面采集的 main_head_before）。
            head_drift = _check_main_head_unchanged(project_root, main_head_before)
            if head_drift:
                _cleanup_stale_task_wt(project_root, wt_path)
                return {
                    "worktree": project_root,
                    "separate": False,
                    "created": False,
                    "degraded": True,
                    "reason": head_drift,
                    "head_restored": _restore_main_head(project_root, main_head_before),
                }
            # 创建后校验不变量（W-4）：任务 worktree 检出恰为 task/<id> 分支且 HEAD
            # 解析正常。校验失败即使 add 成功也视为创建异常 → 走降级告警（禁止静默
            # 绑错分支/跑错目录，落地"静默降级禁止"硬约束）。
            verify = _verify_task_wt(wt_path, branch)
            if not verify["ok"]:
                _cleanup_stale_task_wt(project_root, wt_path)
                return {
                    "worktree": project_root,
                    "separate": False,
                    "created": False,
                    "degraded": True,
                    "reason": verify["reason"],
                }
            # 任务 worktree 自识别容器布局（共享账本根），见 _propagate_container_marker
            _propagate_container_marker(wt_path, project_root)
            result = {"worktree": wt_path, "separate": True, "created": True}
            result.update(_master_suppression_entry(wt_path))
            return result
        # worktree add 失败（如分支已在别处 checkout）→ best-effort 降级主工作树，
        # 但降级原因显式记录（供 claim 告警 / 后续排查，禁止静默降级）。
        reason = (proc.stderr or "").strip()[:300] or (
            f"git worktree add {str(wt_path)} 失败（exit {proc.returncode}）"
        )
        # 2026-08-28 bug4 修复：add 失败可能残留半成品 worktree 元数据（git 注册 /
        # 残留目录）。降级前 best-effort 清理孤儿 worktree，避免污染 git worktree
        # list / doctor / 后续终态回收；仅清理「无效 worktree」，绝不误删有效 worktree。
        _cleanup_stale_task_wt(project_root, wt_path)
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": reason,
        }
    except subprocess.TimeoutExpired as exc:
        # W-5（2026-09-15）：超时被杀（SubprocessError 子类）走的是通用 except，
        # 旧实现**不清理**半创建目录/git 注册（对比 returncode≠0 路径会清）→
        # 残留半成品既污染 doctor/worktree list，又可能被后续复用路径当有效工作区。
        _cleanup_stale_task_wt(project_root, wt_path)
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": (
                f"git worktree add 超时（{exc}）：已清理半成品目录并降级主工作树"
            ),
        }
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": f"worktree add 异常: {exc}",
        }


def _read_main_worktree_head(project_root: Path) -> str | None:
    """读取主工作树 ``.git/HEAD`` 原文（best-effort）。

    AC4（HEAD 落点加固）的对照组采集：仅当 project_root 确为主工作树
    （``.git`` 是目录且含 HEAD 文件）时返回内容；linked worktree（``.git`` 为
    gitfile）/ 读取异常 → None（守卫自动失效，不误报）。
    """
    try:
        head = Path(project_root) / ".git" / "HEAD"
        if head.is_file():
            return head.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _check_main_head_unchanged(project_root: Path, before: str | None) -> str | None:
    """比对主工作树 HEAD 是否被改写；未漂移返回 None，漂移返回原因串。"""
    if not before:
        return None
    after = _read_main_worktree_head(project_root)
    if after is None or after == before:
        return None
    return (
        "HEAD 落点异常：git worktree add 改写了主工作树 .git/HEAD"
        f"（{before.strip()} → {after.strip()}）；已回滚本次 worktree 创建"
        "（任务 worktree 只写 .git/worktrees/<name>/HEAD）"
    )


def _restore_main_head(project_root: Path, before: str) -> bool:
    """best-effort 还原主工作树 HEAD（原文须为 symbolic ref，否则不动作）。"""
    ref = before.strip()
    if not ref.startswith("ref: "):
        return False
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "HEAD", ref[len("ref: "):]],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _worktree_registered_paths(project_root: Path) -> set[str] | None:
    """`git worktree list --porcelain` 登记路径集合（normcase 归一化）。

    返回 None 表示 git 不可用/解析失败（调用方保守降级：视为有效、不删）。
    按行前缀解析（W-16 口径），路径经 resolve + normcase 归一化。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    paths: set[str] = set()
    for ln in (proc.stdout or "").splitlines():
        if not ln.startswith("worktree "):
            continue
        p = ln[len("worktree "):].strip()
        if p:
            try:
                paths.add(os.path.normcase(str(Path(p).resolve())))
            except OSError:
                paths.add(os.path.normcase(p))
    return paths


def _is_effective_worktree(project_root: Path, wt_path: Path) -> bool:
    """判定是否为有效 worktree（doctor/worktree 单一真源）。

    有效 ⇔ git 登记含该路径 **且** `git rev-parse --verify HEAD` 可解析。
    任一 git 探测失败 → 保守返回 True（视为有效，绝不误删）。
    无 `.git` 标记 → 返回 False（由调用方按无标记残留路径处理）。
    """
    if not (wt_path / ".git").exists():
        return False
    registered = _worktree_registered_paths(project_root)
    if registered is None:
        return True
    try:
        key = os.path.normcase(str(wt_path.resolve()))
    except OSError:
        return True
    if key not in registered:
        return False
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True
    return head.returncode == 0 and bool((head.stdout or "").strip())


def _cleanup_stale_task_wt(project_root: Path, wt_path: Path) -> None:
    """best-effort 清理 worktree add 失败残留的半成品元数据（孤儿 worktree）。

    - ``git worktree prune``：移除指向已消失目录的失效 git 注册（安全）。
    - 残留目录仅在其**非有效 worktree**时删除：无 ``.git`` 标记的沿旧路径；
      有 ``.git`` 标记的按 :func:`_is_effective_worktree` 判定——登记失效或
      HEAD 不可解析的半检出可回收，有效 worktree 绝不误删（与 doctor 残留
      判定同源）。
    任何失败静默跳过，不阻断降级主流程。
    """
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    if not wt_path.exists():
        return
    if (wt_path / ".git").exists() and _is_effective_worktree(project_root, wt_path):
        return
    try:
        wt_path.rmdir()
    except OSError:
        try:
            shutil.rmtree(str(wt_path), ignore_errors=True)
        except Exception:
            pass


def _verify_task_wt(wt_path: Path, expected_branch: str) -> dict[str, Any]:
    """创建期不变量校验（W-4）：任务 worktree 检出分支恰为 task/<id> 且 HEAD 可解析。

    Returns:
        ``{"ok": True}`` 或 ``{"ok": False, "reason": str}``（best-effort，绝不抛异常）。
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return {"ok": False, "reason": f"任务 worktree HEAD 无法解析（{wt_path}）"}
        cur = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        actual = cur.stdout.strip() if cur.returncode == 0 else ""
        if actual != expected_branch:
            return {"ok": False,
                    "reason": f"任务 worktree 检出分支 {actual or '<null>'}，期望 {expected_branch}"}
        return {"ok": True}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"ok": False, "reason": f"worktree 校验异常: {exc}"}


def _recycle_actor() -> str:
    """删除/回收动作执行者标识（best-effort）：优先会话指纹，其次原始 session id。"""
    try:
        from orchd.ledger import resolve_agent_id

        agent = resolve_agent_id()
        if agent:
            return agent
    except Exception:
        pass
    return os.environ.get("ORCHD_SESSION_ID") or "<unknown>"


# ``ORCHD_QUIET`` 的真值集合（环境变量宽松解析；未设置 / 空 / 其他值一律不抑制）
_QUIET_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _recycle_quiet() -> bool:
    """回收留痕是否被 ``ORCHD_QUIET`` 抑制（缺省 False = 不抑制）。

    宿主把 orchd 包进「stderr 非空即失败」的外壳时（典型：PowerShell 把
    stderr 行包装成 ``NativeCommandError``，见 conventions.md「命令输出通道
    契约」），可用 ``ORCHD_QUIET=1`` 关闭 ``[回收]`` 留痕。**缺省行为与引入
    开关前逐字节一致**——未设置 / 空值 / 非真值一律不抑制，审计契约不因加
    开关而弱化；抑制仅作用于本留痕，不影响 stdout 的 JSON 契约。

    Returns:
        True = 抑制留痕（不写 stderr）；False = 照常留痕。
    """
    return os.environ.get("ORCHD_QUIET", "").strip().lower() in _QUIET_TRUTHY


def _log_recycle(records: list[dict[str, Any]]) -> None:
    """把删除决策/动作记录打印到 stderr（``orchd ▸ [回收]`` 前缀，best-effort）。

    审计契约（2026-08-30 task-audit-* 分支丢失复盘 §1）：任何 worktree /
    分支 / 目录的删除动作与「拒绝删除」决策都必须留痕，杜绝 best-effort 静默
    删除无法追溯。单条记录为结构化 dict（action / target / reason / evidence /
    actor），JSON 序列化输出。失败静默跳过，不阻断主流程。

    ``ORCHD_QUIET=1`` 时整体跳过（见 :func:`_recycle_quiet`），抑制判定在
    任何输出之前——不触发 W-13 的 stderr UTF-8 reconfigure，零副作用。
    """
    if _recycle_quiet():
        return
    try:
        # W-13：Windows 控制台 stderr 常为 gbk，审计留痕含中文/箭头/路径时会被
        # 乱码或直接抛 UnicodeEncodeError（留痕不可读＝审计失效）→ 输出前把
        # stderr 切到 UTF-8（幂等；不支持 reconfigure 的载体静默跳过）。
        try:
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
        for rec in records:
            print(f"orchd ▸ [回收] {json.dumps(rec, ensure_ascii=False)}", file=sys.stderr)
    except Exception:
        pass


def _task_status_for_recycle(store_root: Path, task_id: str) -> str | None:
    """读取任务当前状态（删除保护断言与决策打点共用）。

    三态（W-10，2026-09-15：禁用 fail-open）：

    - **有记录** → 返回状态字符串（``in_review`` 等），调用方据此决定是否保护；
    - **确认无记录**（任务不在 replay 派生结果中）→ 返回 ``None``，可安全回收；
    - **读取失败**（store_root 不可用 / replay 异常）→ 抛 ``OrchdError(E030)``，
      由调用方显式处置（拒绝回收 + 留痕，可 force_recycle 覆盖）。旧实现在此处
      返回 None，与「确认无记录」不可区分 → 账本瞬时不可读时 in_review 保护
      **静默失效**，正在审查的任务 worktree 可能被回收。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import Store

    try:
        ts = Store(store_root).replay().get(task_id)
    except Exception as exc:
        raise OrchdError(
            ErrorCode.E030,
            f"任务状态读取失败，无法确认回收保护（{store_root}）：{exc}",
            [{
                "task_id": task_id,
                "store_root": str(store_root),
                "hint": (
                    "账本不可读时不再按「无记录」放行回收；请修复账本后重试，"
                    "或显式 force-recycle 覆盖保护"
                ),
            }],
        ) from exc
    return ts.status if ts else None


def remove_task_wt(
    project_root: Path, task_id: str, store_root: Path, *, lock_held: bool = False,
    force_recycle: bool = False,
) -> dict[str, Any]:
    """终态回收：git worktree remove + 删 task/{id} 分支 + 解绑（best-effort 幂等）。

    ``lock_held=True``：调用方已在同一共享账本根持有 ``.lock``（同进程），解绑时
    复用该锁、不再重复 flock（见 :func:`unbind_task_wt`），规避同进程双 fd E012 死锁。

    分支删除安全闸（review W-3 / R-17，AGENTS.md 红线）：
    - 默认只用安全 ``git branch -d``（拒绝删除未合并分支），**绝不再无条件 ``-D``**；
    - 删除前以 ``git rev-list --count main..task/<id>`` 取未合并提交数：非零即拒绝删除，
      计数与拒绝原因记入 ``recycle_log``（``action=branch_delete_refused``）并在返回值
      中给出 ``branch_delete_refused``；
    - 仅当调用方显式传 ``force_recycle=True``（CLI 侧对应 ``--force-recycle``）时，
      才对未合并分支升级 ``-D``；计数为 0 时仍走 ``-d``（无需强删）。

    worktree 移除安全闸（review W-6）：非 ``--force`` 移除失败时，仅当 stderr 表明
    「contains modified or untracked files」才升级 ``--force``（终态回收允许丢弃脏
    工作区并记 ``discarded_uncommitted``）；句柄占用 / 权限 / 未知原因一律不升级，
    失败原因写入 ``recycle_log`` 与 ``residual``（可读、不静默）。

    稳定 cwd 闸（review W-7）：``stable_wt`` 必须是与待回收 worktree **不同**且真实
    存在的主工作树根（``main_worktree_root`` 探测失败会回退 ``project_root``，容器
    布局下那往往就是待删的 worktree 本身）；不满足即拒绝执行删除动作（``reason=
    unstable_main_worktree``）。回收前的 ``os.chdir`` 自愈在移除阶段 ``finally`` 中
    恢复调用方原 cwd（原 cwd 已被本次回收删除时保持 ``stable_wt``，不落进失效目录）。

    幽灵登记收敛（review W-8）：worktree 目录本就不存在时，仍执行 ``git worktree
    prune`` 清理陈旧登记（否则 ``task/*`` 分支被残留 worktree 登记占用而无法删除，
    永久泄漏），结果记入 ``recycle_log`` 的 ``registry_pruned``。

    in_review 保护断言（2026-08-30 复盘 §1）：任务状态为 in_review 时拒绝删除。
    in_review 是审查等待期（任务可能 idle 数小时），恰是分支误删高危窗口；正常
    回收流（review 通过 / force_status 终态）都在状态写入 completed/cancelled
    **之后**才调用本函数，故出现 in_review 即视为异常请求。

    Returns:
        ``{"removed": True, "unbound": True}``；失败降级 ``{"removed": False, ...}``。
        in_review 保护命中返回 ``{"removed": False, "reason": "in_review_protected",
        "status": "in_review"}``；删除动作/决策记录于 ``recycle_log``。
    """
    layout = detect_layout(project_root)
    wt_path = layout["task_wt_root"] / _task_wt_name(task_id)
    # task-14-review-branch-cleanup(AC2)：容器布局下 project_root 即任务 worktree
    # （== wt_path），git worktree remove 会删掉该目录。用 main_worktree_root 在
    # 移除前（cwd 仍有效）解析主工作树根作为稳定 cwd，后续删分支不再依赖已删除的
    # cwd。local import 复用 gitops 已有解析（flat 布局回退 project_root，零回归）。
    from orchd.gitops import main_worktree_root

    stable_wt = main_worktree_root(project_root)
    actor = _recycle_actor()
    recycle_log: list[dict[str, Any]] = []

    # in_review 保护断言（只读查询，确认命中才拒绝）。W-10：账本读取失败**不再
    # 静默放行**（旧实现返回 None 会让保护在账本瞬时不可读时失效）→ 拒绝回收并
    # 留痕，只有显式 force_recycle 才覆盖。
    try:
        status = _task_status_for_recycle(store_root, task_id)
    except Exception as exc:
        if not force_recycle:
            record = {
                "action": "blocked",
                "reason": "status_unreadable",
                "task_id": task_id,
                "target": str(wt_path),
                "error": str(exc)[:300],
                "actor": actor,
            }
            recycle_log.append(record)
            _log_recycle(recycle_log)
            return {
                "removed": False,
                "unbound": False,
                "reason": "status_unreadable",
                "recycle_log": recycle_log,
            }
        status = None
    if status == "in_review":
        record = {
            "action": "blocked",
            "reason": "in_review_protected",
            "task_id": task_id,
            "target": str(wt_path),
            "status": status,
            "actor": actor,
        }
        recycle_log.append(record)
        _log_recycle(recycle_log)
        return {
            "removed": False,
            "unbound": False,
            "reason": "in_review_protected",
            "status": status,
            "recycle_log": recycle_log,
        }

    # W-7 / R-35：stable_wt 必须是与待回收 worktree 不同的真实主工作树根。
    # main_worktree_root 在 git 探测失败时回退 project_root——容器布局下
    # project_root 往往就是待删的任务 worktree 本身；此时继续执行会让
    # git worktree remove / 分支删除带着失效 cwd 冒进（旧实现静默如此）。
    # 此处 fail-safe：拒绝执行删除动作并留痕（宁可留残留交 doctor 处置）。
    _stable_resolved: Path | None = None
    try:
        _stable_resolved = Path(stable_wt).resolve() if stable_wt else None
    except OSError:
        _stable_resolved = None
    _wt_resolved_for_guard = wt_path.resolve()
    if (
        _stable_resolved is None
        or _stable_resolved == _wt_resolved_for_guard
        or not _stable_resolved.is_dir()
    ):
        recycle_log.append({
            "action": "blocked",
            "reason": "unstable_main_worktree",
            "task_id": task_id,
            "target": str(wt_path),
            "stable_worktree": str(_stable_resolved) if _stable_resolved else None,
            "actor": actor,
        })
        _log_recycle(recycle_log)
        return {
            "removed": False,
            "unbound": False,
            "reason": "unstable_main_worktree",
            "recycle_log": recycle_log,
        }

    removed = False
    discarded_uncommitted = False
    wt_existed = (wt_path / ".git").exists()
    # task-worktree-recycle-cwd-selfheal（AC1）：回收前检测调用方进程 cwd 是否
    # 位于待回收 worktree 内。Windows 下进程 cwd 会锁定目录，导致 git worktree
    # remove 失败（已知原因却不自愈）。若命中则 os.chdir 到主工作树（stable_wt），
    # 释放目录锁后再执行 remove；幂等：cwd 不在 worktree 内时零操作。
    # W-7：os.chdir 是进程级副作用——记录调用方原 cwd，移除阶段 finally 恢复。
    cwd_self_healed = False
    _original_cwd: Path | None = None
    try:
        _cur_cwd = Path.cwd().resolve()
        _original_cwd = _cur_cwd
        _wt_resolved = wt_path.resolve()
        # 与 guard_task_root 同口径（task-worktree-residual-self-heal）：双侧
        # normcase，Windows 路径大小写不一致时不再漏判自愈。
        _cwd_inside = (
            os.path.normcase(str(_cur_cwd)) == os.path.normcase(str(_wt_resolved))
            or str(os.path.normcase(str(_cur_cwd))).startswith(
                str(os.path.normcase(str(_wt_resolved))) + os.sep)
        )
    except OSError:
        _cwd_inside = False
    if _cwd_inside and stable_wt and stable_wt.exists():
        try:
            os.chdir(str(stable_wt))
            cwd_self_healed = True
        except OSError:
            cwd_self_healed = False
    if cwd_self_healed:
        recycle_log.append({
            "action": "cwd_self_heal",
            "task_id": task_id,
            "from": str(_cur_cwd),
            "to": str(stable_wt),
            "actor": actor,
        })
    remove_error: str | None = None
    registry_pruned: bool | None = None
    try:
        if wt_existed:
            # P2-9：先无 --force 移除（仅干净 worktree 可移，避免丢弃未提交改动）。
            proc = subprocess.run(
                ["git", "worktree", "remove", str(wt_path)],
                cwd=str(stable_wt),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode != 0:
                # W-6 / R-35：只有失败原因确为「工作区脏」才升级 --force；句柄占用 /
                # 权限 / 未知原因一律不升级（旧实现无条件升级：真脏时静默销毁未提交
                # 改动，句柄占用时白跑一遍 --force 且掩盖真实原因）。
                _err = (proc.stderr or "").lower()
                if "modified or untracked" in _err:
                    proc = subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt_path)],
                        cwd=str(stable_wt),
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                    discarded_uncommitted = proc.returncode == 0
                else:
                    remove_error = (proc.stderr or proc.stdout or "").strip()[:300]
            removed = proc.returncode == 0
        else:
            # W-8 / R-35：目录本就不存在时仍收敛 git 登记——陈旧登记会占用
            # task/* 分支名导致分支无法删除（幽灵登记 → 分支永久泄漏）。
            try:
                pr = subprocess.run(
                    ["git", "-C", str(stable_wt), "worktree", "prune"],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT,
                )
                registry_pruned = pr.returncode == 0
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                registry_pruned = False
            removed = True  # 独立 worktree 本就不存在 → 视为已回收
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        remove_error = f"{type(exc).__name__}: {exc}"[:300]
        removed = False
    finally:
        # W-7：os.chdir 是进程级副作用，移除阶段结束即恢复调用方原 cwd。原 cwd 已被
        # 本次回收删除（常见情形）时保持 stable_wt，避免把进程放进失效目录；后续
        # git 调用一律显式 -C stable_wt，不依赖进程 cwd。
        if cwd_self_healed and _original_cwd is not None:
            try:
                if _original_cwd.is_dir():
                    os.chdir(str(_original_cwd))
            except OSError:
                pass
    _remove_record: dict[str, Any] = {
        "action": "worktree_remove" if wt_existed else "worktree_absent",
        "task_id": task_id,
        "target": str(wt_path),
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "status": status,
        "actor": actor,
    }
    if remove_error:
        _remove_record["error"] = remove_error
    if registry_pruned is not None:
        _remove_record["registry_pruned"] = registry_pruned
    recycle_log.append(_remove_record)
    # P0-19：Windows 下 git worktree remove 可能删除内容但残留空目录
    # （文件句柄 / .lock / 杀毒扫描导致目录删除不完整）。best-effort 清理残留。
    residual_cleaned = False
    if wt_path.exists() and not (wt_path / ".git").exists():
        try:
            # 先尝试 rmdir（仅空目录成功，安全）
            wt_path.rmdir()
            residual_cleaned = True
        except OSError:
            # 非空目录或权限问题 → 尝试 rmtree（兜底，可能因杀毒/句柄失败）
            try:
                shutil.rmtree(str(wt_path), ignore_errors=True)
                if not wt_path.exists():
                    residual_cleaned = True
            except Exception:
                pass
    if residual_cleaned:
        recycle_log.append({
            "action": "residual_clean",
            "task_id": task_id,
            "target": str(wt_path),
            "actor": actor,
        })
    # 删任务分支（best-effort）——W-3 / R-17（AGENTS.md 红线：不得用 -D 销毁未合并提交）：
    # ① 先取未合并提交数（git rev-list --count main..task/<id>）；② 非零 → 拒绝删除并
    # 留痕，除非调用方显式 force_recycle；③ 否则用安全 -d；④ 分支已不存在视为幂等成功。
    # 以主工作树为稳定 cwd（git -C）：worktree 已回收时 task/{id} 不再被占用可删除；
    # project_root（任务 worktree）可能已被 git worktree remove 删除，不能作为 cwd。
    branch_deleted = False
    branch_refused: dict[str, Any] | None = None
    unmerged_count: int | None = None
    try:
        count_proc = subprocess.run(
            ["git", "-C", str(stable_wt), "rev-list", "--count", f"main..task/{task_id}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if count_proc.returncode == 0:
            _raw_count = (count_proc.stdout or "").strip()
            if _raw_count.isdigit():
                unmerged_count = int(_raw_count)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        unmerged_count = None

    if unmerged_count and unmerged_count > 0 and not force_recycle:
        branch_refused = {
            "branch": f"task/{task_id}",
            "unmerged_commits": unmerged_count,
            "reason": "unmerged_branch_protected",
            "hint": "该分支未被主分支包含；确需丢弃请显式 force_recycle（CLI: --force-recycle）",
        }
        recycle_log.append({
            "action": "branch_delete_refused",
            "task_id": task_id,
            "branch": f"task/{task_id}",
            "unmerged_commits": unmerged_count,
            "reason": "unmerged_branch_protected",
            "actor": actor,
        })
    else:
        _branch_flag = "-D" if (force_recycle and unmerged_count) else "-d"
        try:
            proc = subprocess.run(
                ["git", "-C", str(stable_wt), "branch", _branch_flag, f"task/{task_id}"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
            )
            branch_deleted = proc.returncode == 0
            if not branch_deleted:
                _berr = (proc.stderr or "").lower()
                if (
                    "not found" in _berr
                    or "doesn't exist" in _berr
                    or "does not exist" in _berr
                ):
                    branch_deleted = True  # 幂等：分支已不存在（如 merge 流程已删）
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            branch_deleted = False
        recycle_log.append({
            "action": "branch_delete",
            "task_id": task_id,
            "branch": f"task/{task_id}",
            "mode": _branch_flag,
            "unmerged_commits": unmerged_count,
            "deleted": branch_deleted,
            "actor": actor,
        })
    unbind_error: str | None = None
    try:
        unbound = unbind_task_wt(store_root, task_id, lock_held=lock_held)
    except Exception as exc:
        # best-effort（task-unbind-recycle-audit-best-effort）：解绑抛 E002
        # （绑定损坏）或锁失败时不再向上传播——异常转 warning 并继续执行，
        # 保证本次 worktree / 分支动作的 recycle_log 仍落盘、不丢失留痕。
        code = getattr(getattr(exc, "code", None), "name", type(exc).__name__)
        unbind_error = f"{code}: {exc}"[:300]
        unbound = {
            "unbound": False,
            "task_id": task_id,
            "unbind_error": unbind_error,
        }
        recycle_log.append({
            "action": "unbind_failed",
            "task_id": task_id,
            "unbound": False,
            "error": unbind_error,
            "actor": actor,
        })
    else:
        recycle_log.append({
            "action": "unbind",
            "task_id": task_id,
            "unbound": unbound.get("unbound", False),
            "actor": actor,
        })
    _log_recycle(recycle_log)
    result: dict[str, Any] = {
        "removed": removed,
        "unbound": unbound.get("unbound", False),
        "branch_deleted": branch_deleted,
        "recycle_log": recycle_log,
    }
    if unbind_error is not None:
        result["unbind_error"] = unbind_error
        result["warnings"] = [{
            "code": "unbind_failed",
            "task_id": task_id,
            "error": unbind_error,
        }]
    if branch_refused:
        # W-3：未合并分支被拒绝删除 —— 显式回传（含未合并提交数与逃生开关指引）
        result["branch_delete_refused"] = branch_refused
    if discarded_uncommitted:
        result["discarded_uncommitted"] = True
    if residual_cleaned:
        result["residual_cleaned"] = True
    # AC6（task-review-baseline-and-worktree-recycle-fix）：回收失败可解释。
    # 修复前 removed=false 时仅静默返回（unbind + 删分支照旧），磁盘残留空壳目录
    # 无人知晓（实测 task-check-test-dedup-utf8/ 残留，仅 doctor 可检出）。现显式
    # 返回 residual 标记并指向 worktree_residual 处置入口（禁止静默失败）。
    residual_dir = str(wt_path) if wt_path.exists() else None
    if not removed or residual_dir:
        result["residual"] = {
            "path": residual_dir or str(wt_path),
            "removed": removed,
            "residual_cleaned": residual_cleaned,
            "reason": (
                "git worktree remove 未成功（常见于调用方 cwd 位于该 worktree 内，"
                "或 Windows 文件句柄占用）"
                if not removed
                else "worktree 已注销但目录仍存在（残留空壳，Windows 句柄/杀毒扫描常见）"
            ),
            "hint": (
                "退出该目录后重试回收；或运行 python .orchd/__main__.py doctor "
                "查看 worktree_residual 项并执行 --fix 清理"
            ),
            "doctor_check": "worktree_residual",
        }
    return result


def _cleanup_stale_session_locks(store_root: Path, task_wt_root: Path) -> list[str]:
    """清理 worktree 已不存在的会话锁残留（best-effort）。

    task-workspace-docs-isolation：任务 worktree 终态回收后，其会话锁标记
    （``.session-<wt>.lock`` / ``.session-gate-<wt>.lock``）残留在共享账本根。
    仅清理 worktree 名以 ``task-`` 开头且对应目录已不存在的锁；删除前探活
    flock（他人仍持活锁则跳过，防 flock-unlink 竞态，见
    ``gitops._probe_session_lock_os_active``）。主 worktree 锁
    （``.session.lock`` / ``.session.gate.lock``）不在匹配范围，不触碰。

    Returns:
        已清理的锁文件名清单。
    """
    from orchd.gitops import _probe_session_lock_os_active

    cleaned: list[str] = []
    try:
        for pattern in (".session-*.lock", ".session-gate-*.lock"):
            for p in sorted(store_root.glob(pattern)):
                name = p.name
                if pattern == ".session-*.lock" and name.startswith(".session-gate-"):
                    # review W-17：``.session-*.lock`` 通配也会命中门锁
                    # （``.session-gate-<wt>.lock``），门锁交由专用 pattern 处理，
                    # 避免同一文件在一轮回收里被访问两次。
                    continue
                if name.startswith(".session-gate-"):
                    wt = name[len(".session-gate-"):-len(".lock")]
                elif name.startswith(".session-"):
                    wt = name[len(".session-"):-len(".lock")]
                else:
                    continue
                if not wt.startswith(_TASK_WT_PREFIX):
                    continue  # 仅任务 worktree 维度锁；主 worktree / 其他锁不碰
                if (task_wt_root / wt).exists():
                    continue  # worktree 仍存在 → 活跃，跳过
                if _probe_session_lock_os_active(p).get("active"):
                    continue  # 他人仍持活锁 → 不删（flock-unlink 竞态）
                try:
                    p.unlink()
                    cleaned.append(name)
                except OSError:
                    pass
    except OSError:
        pass
    return cleaned


def _rmtree_force(path: Path) -> bool:
    """删除目录树；Windows 下先清只读属性再删（git 对象文件只读导致 rmtree 失败）。

    task-workspace-docs-isolation：测试残留（如 .pytest-tmp 内 git 仓库）对象文件
    带只读属性，``shutil.rmtree`` 在 Windows 上删除失败（PermissionError WinError 5）。
    onerror 回调清只读后重试单文件删除，其余错误静默跳过。

    Returns:
        ``True`` 目录已不存在（删除成功或本就不存在）。
    """
    from orchd.gitops import _os_delete_tree

    return _os_delete_tree(path)


def _cleanup_container_root_junk(task_wt_root: Path) -> list[str]:
    """清理容器根可再生杂项（best-effort）。

    task-workspace-docs-isolation：测试/缓存杂项（``.pytest-tmp`` /
    ``.pytest_cache`` 等）可能落在容器根（main/ 之外）。复用 ``_is_junk_entry``
    保守名单，**清理前逐条做 ``git ls-files`` tracked 校验**（review W-15：
    此前 docstring 承诺「绝不触碰被跟踪文件」但无校验，可删仓库跟踪的
    ``htmlcov/``）——被跟踪路径跳过；所属仓库不可判定 / 探测异常同样跳过
    （保守）。绝不触碰 .git / .orchd / 工具目录 / 被跟踪文件。

    Returns:
        已清理的条目名清单。
    """
    cleaned: list[str] = []
    try:
        for entry in sorted(task_wt_root.iterdir()):
            if not _is_junk_entry(entry.name):
                continue
            if _is_git_tracked_path(entry):
                continue  # W-15：被 git 跟踪 → 绝不触碰（含 in-cone 的 htmlcov/）
            try:
                if entry.is_dir():
                    removed = _rmtree_force(entry)
                else:
                    entry.unlink()
                    removed = not entry.exists()
                if removed:
                    cleaned.append(entry.name)
            except OSError:
                pass
    except OSError:
        pass
    return cleaned


def prune_orphans(
    project_root: Path,
    store_root: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """孤儿 worktree 惰性清理（watchdog / status 调用，best-effort）。

    清理三类：
    - 绑定任务已终态（completed/cancelled）但 worktree 仍在 → remove；
    - 绑定任务已不在 master / 无对应活跃任务 → remove + 解绑；
    - 文件系统残留 task-* 空目录（P0-19，Windows git worktree remove 不完整）→ 清理。

    安全边界（review W-1 / W-12 / W-15 / R-16 / R-23）：
    - **布局门**：仅 container 布局存在独立任务 worktree。flat 下 ``detect_layout``
      把 ``task_wt_root`` 解析为**仓库父目录**，扫它会越界删除父目录下无关的
      ``task-*`` 目录与 ``htmlcov`` 等杂项 —— flat 直接早返回：零扫描、零删除
      （判据与 ``doctor.py`` 的 ``layout != "container"`` 门一致）。
    - **持锁 + 新鲜状态**：全部破坏性动作（worktree 回收 / 目录删除）在
      ``Store.acquire_lock()`` 内执行；每个 task_id 处理前重新 ``replay()`` 取新鲜
      状态，消除「入口陈旧快照判定 → 并发重新 claim → 按陈旧 completed 强删」的
      TOCTOU 窗口。新鲜账本中查不到该任务时回退调用方快照（保持既有调用契约；
      凡新鲜账本有记录，一律以记录为准）。
    - **残留证据**：``task-*`` 目录仅在存在「曾由 git 登记为本仓库 worktree」证据
      （``<git-common-dir>/worktrees/<name>``）时才判为残留；``rmdir`` 失败
      **禁止**升级 ``rmtree``，改为留痕保留（交 doctor / 人工处置）。

    删除决策审计（2026-08-30 复盘 §1）：每次判定「可清理」或「拒绝清理」前输出
    决策上下文（task_id / status / status_source / has_active_binding / git 登记 /
    判定依据），删除动作由 ``remove_task_wt`` 内部再记 recycle_log，杜绝 best-effort
    静默删除无痕。

    Returns:
        ``{"pruned": [<str>], "orphans_found": int, "residual_cleaned": [<str>],
        "decisions": [<dict>]}``；decisions 为本次全部删除/拒绝决策记录。
        flat 布局 / 锁不可用等「未执行」场景额外返回 ``skipped``（不静默）。
    """
    from orchd.gitops import _has_linked_worktrees

    project_root = Path(project_root).resolve()

    # 布局门（W-1 / R-16）：flat 无任务 worktree 概念，且 task_wt_root 会解析为
    # 仓库父目录 —— 扫描/删除即越界操作他人目录，直接早返回。
    try:
        layout = detect_layout(project_root)
    except Exception:
        return {"pruned": [], "orphans_found": 0, "skipped": "layout_unresolved"}
    if layout.get("layout") != "container":
        return {"pruned": [], "orphans_found": 0, "skipped": "flat_layout"}
    task_wt_root = Path(layout["task_wt_root"])

    try:
        from orchd.errors import OrchdError

        bindings = load_bindings(store_root)
    except OrchdError:
        # 绑定表损坏：信息不全时保守跳过本轮 prune（不基于空表误删 worktree）
        return {"pruned": [], "orphans_found": 0, "skipped": "bindings_corrupt"}
    pruned: list[str] = []
    residual_cleaned: list[str] = []
    decisions: list[dict[str, Any]] = []
    actor = _recycle_actor()

    # 破坏性段落持账本锁（W-12 / R-23）：加锁后才判定、才删除。
    try:
        from orchd.ledger import Store

        store = Store(store_root)
        store.acquire_lock()
    except Exception as exc:
        # 锁不可用（超时 / 后端异常）→ 放弃本轮破坏性清理（best-effort，不抛）
        _log_recycle([{
            "action": "skip",
            "kind": "prune_orphans",
            "reason": "store_lock_unavailable",
            "error": str(exc),
            "actor": actor,
        }])
        return {"pruned": [], "orphans_found": 0, "skipped": "store_lock_unavailable"}

    def _fresh_status(task_id: str) -> tuple[str | None, str]:
        """锁内重新 replay 取新鲜状态，返回 ``(status, status_source)``。

        新鲜账本有该任务记录 → 以记录为准（``fresh_replay``，修正陈旧快照）；
        无记录 → 回退调用方入口快照（``entry_snapshot_fallback``，保持既有调用
        契约）；两者皆无 → ``(None, "absent")``，调用方按「非终态」保守处理。
        """
        try:
            fresh = store.replay()
        except Exception:
            fresh = {}
        ts = fresh.get(task_id)
        if ts is not None:
            return ts.status, "fresh_replay"
        snapshot = state.get(task_id)
        if snapshot is not None:
            return snapshot.status, "entry_snapshot_fallback"
        return None, "absent"

    try:
        # 既有绑定任务清理（需要 git 层 linked worktrees 存在才执行 git worktree remove）
        if _has_linked_worktrees(project_root):
            for task_id, entry in list(bindings.items()):
                status, status_source = _fresh_status(task_id)
                effective = status or "pending"
                if effective in ("completed", "cancelled"):
                    # 终态：回收 worktree + 解绑（删除动作由 remove_task_wt 记 recycle_log）
                    decisions.append({
                        "action": "recycle",
                        "task_id": task_id,
                        "kind": "terminal_binding",
                        "status": effective,
                        "status_source": status_source,
                        "bound_worktree": (entry or {}).get("worktree"),
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
                    # lock_held=True：复用本函数已持有的账本锁（避免同进程双 fd E012）
                    result = remove_task_wt(
                        project_root, task_id, store_root, lock_held=True,
                    )
                    if result.get("removed"):
                        pruned.append(task_id)

        # P0-19：扫描文件系统残留 task-* 目录（git 不登记但目录仍存在）。
        # 不依赖 _has_linked_worktrees——Windows 下 git worktree remove 成功但目录残留。
        common_dir = _git_common_dir(project_root)
        registry = (common_dir / "worktrees") if common_dir is not None else None
        if task_wt_root.exists():
            for entry in sorted(task_wt_root.iterdir()):
                if (not entry.is_dir()
                        or not entry.name.startswith(_TASK_WT_PREFIX)):
                    continue
                # 是 task-* 目录 → 检查是否有活跃绑定
                # 从目录名反推 task_id（task-<short> → task-<short> 或 task/<short>）
                short = entry.name[len(_TASK_WT_PREFIX):]
                candidate_ids = [f"task-{short}", short]
                has_active_binding = False
                for cid in candidate_ids:
                    if cid in bindings:
                        status, status_source = _fresh_status(cid)
                        if (status or "pending") not in ("completed", "cancelled"):
                            has_active_binding = True
                        break
                if has_active_binding:
                    continue
                if (entry / ".git").exists():
                    continue
                # W-1 / R-16：残留证据 —— 仅「曾由 git 登记为本仓库 worktree」的目录
                # 才可能是 remove 不完整的残留；无证据 = 他人/无关目录，保留并留痕。
                registered = registry is not None and (registry / entry.name).exists()
                if not registered:
                    decisions.append({
                        "action": "keep",
                        "target": entry.name,
                        "kind": "residual_dir",
                        "reason": "no_git_registry_evidence",
                        "has_active_binding": False,
                        "git_registered": False,
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
                    continue
                decisions.append({
                    "action": "clean",
                    "target": entry.name,
                    "kind": "residual_dir",
                    "has_active_binding": False,
                    "git_registered": True,
                    "actor": actor,
                })
                _log_recycle(decisions[-1:])
                try:
                    entry.rmdir()  # 仅空目录（R-16：失败禁止升级 rmtree）
                    residual_cleaned.append(entry.name)
                except OSError as exc:
                    decisions.append({
                        "action": "keep",
                        "target": entry.name,
                        "kind": "residual_dir",
                        "reason": "not_empty_or_locked",
                        "error": str(exc),
                        "git_registered": True,
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
    finally:
        try:
            store.release_lock()
        except Exception:
            pass

    # task-workspace-docs-isolation：容器级卫生清理（best-effort）——
    # ① worktree 已不存在的会话锁残留；② 容器根可再生杂项；③ 系统 temp 中
    # 历史 orchd-trash-* 残留（早期 _safe_delete 降级重命名产物）。
    stale_locks_cleaned: list[str] = []
    junk_cleaned: list[str] = []
    trash_residue_cleaned: list[str] = []
    try:
        # task_wt_root 已在布局门处解析（container 布局），此处不再重复 detect_layout
        if task_wt_root.exists():
            stale_locks_cleaned = _cleanup_stale_session_locks(store_root, task_wt_root)
            junk_cleaned = _cleanup_container_root_junk(task_wt_root)
        from orchd.gitops import _cleanup_trash_residue

        trash_residue_cleaned = _cleanup_trash_residue()
    except Exception:
        pass

    result: dict[str, Any] = {"pruned": pruned, "orphans_found": len(pruned)}
    if residual_cleaned:
        result["residual_cleaned"] = residual_cleaned
    if decisions:
        result["decisions"] = decisions
    if stale_locks_cleaned:
        result["stale_locks_cleaned"] = stale_locks_cleaned
    if junk_cleaned:
        result["junk_cleaned"] = junk_cleaned
    if trash_residue_cleaned:
        result["trash_residue_cleaned"] = trash_residue_cleaned
    return result


def _git_diff_names(project_root: Path, task_id: str) -> list[str]:
    """git diff --name-only main...task/<id>（best-effort，E010 增强用）。

    返回任务分支相对 main 实际改动的文件路径列表；分支不存在 / 非 git /
    git 不可用返回空列表。
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"main...task/{task_id}"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return []


def task_branch_files(project_root: Path, task_id: str) -> list[str]:
    """返回任务分支相对 main 的实际改动文件清单（best-effort）。"""
    return _git_diff_names(project_root, task_id)


def main_worktree_dirty_overlap(
    project_root: Path,
    declared_files: list[str] | set[str],
) -> list[str]:
    """检测主工作树中与声明文件重叠的已跟踪脏文件（跨 worktree 漏写防护）。

    flat 单 worktree（project_root == main_worktree_root）不适用，返回空列表。
    """
    try:
        from orchd.gitops import list_tracked_changes, main_worktree_root

        main_root = main_worktree_root(project_root)
        if main_root.resolve() == Path(project_root).resolve():
            return []
        dirty = list_tracked_changes(main_root)
        if dirty is None:
            return []
        from orchd.pool import _prefix_overlap
        return _prefix_overlap(dirty, declared_files)
    except Exception:
        return []


def missing_declared_branch_files(
    project_root: Path,
    task_id: str,
    declared_files: list[str] | set[str],
) -> list[str]:
    """返回任务分支 diff 中缺失的声明文件（best-effort）。

    flat / 非任务 worktree 场景跳过；仅 container 独立任务 worktree 才对比。
    """
    try:
        from orchd.gitops import is_task_worktree

        if not is_task_worktree(Path(project_root)):
            return []
        branch_files = set(task_branch_files(Path(project_root), task_id))
        if not branch_files:
            # 无实际任务分支改动（测试/flat/未实现）不强制比对，避免误伤
            return []
        from orchd.pool import _is_path_covered as is_path_covered
        # 目录式声明感知差集（task-decl-dir-match-conflict）：声明路径若被
        # branch_files 中任一文件覆盖（即目录下有改动），则视为已覆盖。
        declared = list(declared_files)
        missing: list[str] = []
        for dp in declared:
            if dp in branch_files:
                continue
            if any(is_path_covered(dp, bf) for bf in branch_files):
                continue
            missing.append(dp)
        return sorted(missing)
    except Exception:
        return []


def diagnose_missing_branch_files(
    project_root: Path,
    task_id: str,
    declared_files: list[str] | set[str],
) -> list[dict[str, str]]:
    """返回缺失声明文件的结构化诊断（Bug #20b，2026-08-27）。

    对每个缺失文件做三路判定：
    - path_not_found：文件在磁盘不存在
    - gitignored：文件存在但被 .gitignore 忽略（附命中规则）
    - not_committed：文件存在且未被忽略，但未进入任务分支 diff（漏提交）

    flat / 非任务 worktree 场景返回空列表（与原函数行为一致）。
    """
    try:
        from orchd.gitops import is_task_worktree

        pr = Path(project_root)
        if not is_task_worktree(pr):
            return []
        branch_files = set(task_branch_files(pr, task_id))
        if not branch_files:
            return []
        from orchd.pool import _is_path_covered as is_path_covered
        # 目录式声明感知差集：声明路径若被 branch_files 中任一文件覆盖（即目录下有改动），
        # 则视为已覆盖，不报 missing；精确文件仍用差集判定。
        declared = list(declared_files)
        missing: list[str] = []
        for dp in declared:
            if dp in branch_files:
                continue
            if any(is_path_covered(dp, bf) for bf in branch_files):
                continue
            missing.append(dp)
        missing = sorted(missing)
        if not missing:
            return []

        results: list[dict[str, str]] = []
        for fp in missing:
            full = pr / fp
            if not full.exists():
                # task-e010-delete-parity-done-guards：区分「删除态未提交」与幽灵
                # 路径。两者 reason 均保持 path_not_found（未提交的声明内删除仍报
                # path_not_found，防「删了不提交」漏网），但 hint 引导不同——删除态
                # 应「提交删除态后保持声明」（与 _guard_out_of_scope 的 D 感知口径
                # 对齐；勿移除声明，否则删除变声明外改动触发 out_of_scope E010）；
                # 幽灵路径才是「修正路径或从声明中移除」。deleted 标记仅供
                # _guard_declared_diff 组装 hint 使用，不进事件 details。
                deleted_flag = "false"
                try:
                    st = subprocess.run(
                        ["git", "status", "--short", "--", fp],
                        cwd=str(pr),
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                    )
                    # 删除态：status 短格式的 XY 位含 D（staged 'D ' / unstaged ' D'）；
                    # XY 与路径间有空格分隔，strip 后首位即 X/Y 状态位，不会误匹配路径名。
                    if st.returncode == 0 and any(
                        ln.strip().startswith("D")
                        for ln in st.stdout.splitlines() if ln.strip()
                    ):
                        deleted_flag = "true"
                except (subprocess.SubprocessError, FileNotFoundError, OSError):
                    pass  # 探测失败按非删除态处理（维持原 hint，不放大阻断面）
                detail = (
                    f"路径 {fp} 在磁盘不存在（检测到未提交的删除态）"
                    if deleted_flag == "true"
                    else f"路径 {fp} 在磁盘不存在"
                )
                results.append({
                    "file": fp,
                    "reason": "path_not_found",
                    "detail": detail,
                    "deleted": deleted_flag,
                })
                continue
            # git check-ignore：退出码 0 = 被忽略，1 = 未被忽略
            try:
                proc = subprocess.run(
                    ["git", "check-ignore", "-v", fp],
                    cwd=str(pr),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    results.append({
                        "file": fp,
                        "reason": "gitignored",
                        "detail": proc.stdout.strip(),
                    })
                    continue
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass
            # task-master-single-copy：区分"漏提交"与"声明未改动"。未进分支
            # diff 的文件若在工作树/暂存区有改动 → 真漏提交（not_committed，
            # 阻断）；完全无改动 → 声明冗余（E020 预防性声明），不阻断。
            try:
                st = subprocess.run(
                    ["git", "status", "--short", "--", fp],
                    cwd=str(pr),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                has_changes = st.returncode == 0 and bool(st.stdout.strip())
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                has_changes = True  # 无法判定时保持 fail-closed 语义
            if has_changes:
                results.append({
                    "file": fp,
                    "reason": "not_committed",
                    "detail": "文件存在且未被 .gitignore 忽略，但未出现在任务分支 diff 中"
                              "（工作树/暂存区有改动未提交）",
                })
        return results
    except Exception:
        return []


def actual_changes_conflict(
    project_root: Path | None,
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    target_task: dict[str, Any],
) -> list[dict[str, Any]]:
    """E010 增强（task-14-worktree-lifecycle AC7）：声明 ∪ 实际改动文件冲突。

    claim 期额外比对活跃（claimed）任务的**分支实际改动**（``git diff --name-only
    main...task/<id>``）与候选任务 ``files_to_edit`` 的重叠——未声明文件的重叠
    编辑提前到 claim 期拦截（比 merge 期返工更早）。

    Args:
        project_root: 主工作树根（None / 非 git → 空结果，best-effort）。
        state: Store.replay() 结果。
        tasks: 全部任务定义。
        target_task: 候选目标任务。

    Returns:
        实际改动冲突列表：``[{"task_id", "files", "claimed_by", "source": "actual"}]``。
    """
    if project_root is None:
        return []
    try:
        from orchd.pool import _build_claimed_files
    except Exception:
        return []
    claimed_files = _build_claimed_files(state, tasks)
    target_files = set(target_task.get("files_to_edit", []))
    if not target_files:
        return []
    conflicts: list[dict[str, Any]] = []
    target_id = target_task.get("id", "")
    for tid, (_, claimed_by) in claimed_files.items():
        if tid == target_id:
            continue
        actual = _git_diff_names(project_root, tid)
        from orchd.pool import _prefix_overlap
        overlap = _prefix_overlap(target_files, actual)
        if overlap:
            conflicts.append({
                "task_id": tid,
                "files": overlap,
                "claimed_by": claimed_by,
                "source": "actual",
            })
    return conflicts
