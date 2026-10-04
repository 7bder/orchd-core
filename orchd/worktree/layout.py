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
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注：叶子模块，运行时不依赖 ledger（防循环）
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
    # A3（task-hostfix-pack-b）：强制 LF。Windows 文本模式默认把 \n 翻成 \r\n，
    # 而标记文件以 LF 入库（或首次写入即 LF）→ git status 出現内容为空的幻影脏位
    # （ROADMAP.md 同款已在 hook 写盘路径修复，此处同源）。
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
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


def resolve_declaration_source(
    project_root: Path | None,
    fallback_tasks: list[dict[str, Any]],
    degraded: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """解析任务声明权威来源并返回任务定义列表（task-flat-decl-authority）。

    根因（E-01）：flat 布局下任务分支的 ``.orchd/_master.json`` 是 claim 时的快照；
    main 上 amend 之后，任务分支本地副本陈旧，而 done/review/claim 经本地优先读到
    陈旧声明 → 门禁按旧声明误拦（E010）或误放。权威来源按布局收敛：

    - flat + git：``git show <default-branch>:.orchd/_master.json``（main blob，
      永远最新；分支名经 :func:`orchd.gitops.query.get_default_branch` 解析，
      缺失回退 ``"main"``）；
    - container：调用方传入列表即 canonical 主工作树读数，逐字保留、直接返回
      （任务 worktree 副本被抑制，重读本地必空）；
    - nogit / project_root 为 None：本地文件（调用方传入列表），直接返回；
    - blob 不可读（git 不可用 / 无默认分支 / show 失败 / JSON 非法 / 无 tasks 表）
      → 回退传入列表 + degraded 留痕（不清 fail-closed：读不到就按旧口径，
      门禁自身的 E010/E026 照常工作）。

    调用方（done 早检 / claim 预检 / review merge-diff 门禁 / CLI done early guard）
    单次调用、返回的 tasks 复用到底（单 done 调用内 blob 只读一次；CLI early guard
    与引擎各读一次，两次均为只读幂等）。

    Args:
        project_root: 项目根（flat 下即仓库根；None → 本地口径）。
        fallback_tasks: 调用方已持有的任务定义列表（回退与非 flat 口径的返回值）。
        degraded: 降级登记表（None → 不留痕；仅 blob 回退路径写一条）。

    Returns:
        ``(tasks, source)``：tasks 为权威任务定义列表；source 为
        ``{"source": "main_blob"|"canonical_file"|"local_file"|"fallback_local",
        ...}``（ref/reason 按需附带）。
    """
    fallback_tasks = list(fallback_tasks or [])

    def _trace(reason: str, detail: str = "") -> None:
        if degraded is None:
            return
        degraded.append({
            "guard": "declaration_source",
            "reason": reason,
            "detail": detail,
            "hint": "声明权威来源回退到调用方传入列表；门禁按传入声明执行，未静默放行",
        })

    if project_root is None:
        return fallback_tasks, {"source": "local_file", "reason": "no_project_root"}
    try:
        layout = detect_layout(Path(project_root)).get("layout")
    except Exception as exc:
        _trace("layout_unreadable", str(exc)[:200])
        return fallback_tasks, {"source": "fallback_local", "reason": "layout_unreadable"}
    if layout == "container":
        # container 调用方（CLI _load_tasks）已从 canonical 主工作树读取，
        # 此处逐字保留、不重读（任务 worktree 副本被抑制，重读本地必空）。
        return fallback_tasks, {"source": "canonical_file"}
    from orchd.nogit import git_available as _git_avail
    try:
        _git_ok = bool(_git_avail(Path(project_root)))
    except Exception:
        _git_ok = False
    if not _git_ok:
        return fallback_tasks, {"source": "local_file", "reason": "no_git"}
    try:
        from orchd.gitops.query import get_default_branch as _get_default_branch
        ref = _get_default_branch(Path(project_root)) or "main"
    except Exception:
        ref = "main"
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "show", f"{ref}:.orchd/_master.json"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        _trace("blob_unreadable", f"{type(exc).__name__}: {exc}"[:200])
        return fallback_tasks, {"source": "fallback_local", "reason": "blob_unreadable"}
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        _trace("blob_unreadable", (proc.stderr or "")[:200] or f"ref {ref} 无该 blob")
        return fallback_tasks, {"source": "fallback_local", "reason": "blob_unreadable"}
    try:
        data = json.loads(proc.stdout)
        blob_tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(blob_tasks, list):
            raise ValueError("tasks 表缺失")
    except (ValueError, TypeError) as exc:
        _trace("blob_unreadable", f"blob 解析失败: {exc}"[:200])
        return fallback_tasks, {"source": "fallback_local", "reason": "blob_unreadable"}
    return list(blob_tasks), {"source": "main_blob", "ref": ref}


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
        # A0c：**无 git 环境不得阻断 init**——接入门槛降到「有目录即可」。
        # 无 git 可执行文件时 subprocess 抛 FileNotFoundError（旧实现未捕获 → init 整体失败，
        # 这正是「无 git 就进不来」的根因）；此处静默跳过，改由无 git 模式（快照目录 +
        # 文件锁）承接，布局标记照写。
        if not (main_dir / ".git").exists():
            try:
                subprocess.run(
                    ["git", "init", "-q"],
                    cwd=str(main_dir),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT,
                )
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass

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
