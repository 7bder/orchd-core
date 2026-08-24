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

依赖方向：本模块只依赖标准库（json / os / pathlib / shutil / subprocess）+ orchd.errors，
不导入 onboard / review / ledger 状态机（叶子化，单一入口可审计）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def _move_ledger_runtime_files(orchd_dir: Path, runtime_root: Path) -> list[str]:
    """把运行时账本文件从 ``<main>/.orchd/`` 搬到 ``<runtime>/``（best-effort）。

    迁移 flat→container 后，``resolve_store_dir`` 默认读 ``<容器>/.orchd-runtime/``，
    若历史账本仍留在 ``main/.orchd/``，引擎将看不到既有任务状态。此函数把
    ledger / checkpoint / 锁 / session-worktrees / mod-* 搬到 runtime 根，
    保证迁移一次成功后任务历史立即可见。任一文件缺失/搬移失败静默跳过。

    Returns:
        已搬移的文件/目录名清单（相对名）。
    """
    moved: list[str] = []
    for name in _LEDGER_RUNTIME_FILES:
        src = orchd_dir / name
        if src.exists():
            try:
                shutil.move(str(src), str(runtime_root / name))
                moved.append(name)
            except OSError:
                pass
    for p in sorted(orchd_dir.glob("mod-*")):
        try:
            shutil.move(str(p), str(runtime_root / p.name))
            moved.append(p.name)
        except OSError:
            pass
    return moved


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
        data = json.loads(marker_path(orchd_dir).read_text(encoding="utf-8"))
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
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out:
                return Path(out)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


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


def resolve_canonical_project_root(project_root: Path) -> Path:
    """解析 canonical 项目根（统一共享读入口：主工作树根，task-canonical-project-root）。

    业务读（pool/status/request 等加载 ``_master.json``）统一从 canonical 主工作树
    读取，避免任务 worktree 本地 checkout 副本与主工作树不同步导致的任务池不一致。

    - container 布局 → 返回主工作树根（``<容器>/main/``，布局标记权威）；
    - flat 布局 → 返回 ``project_root`` 自身（单 worktree，零回归）；
    - 标记缺失 → git 公共目录定位主工作树（linked worktree 返回同一主 ``.git``，
      主 worktree / flat 返回自身）；
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
            return Path(marker["main_worktree"]).resolve()
        return project_root  # flat：自身

    # 2) 标记缺失 → git 公共目录定位主工作树（flat 单 worktree 返回自身）
    try:
        from orchd.gitops import main_worktree_root

        return main_worktree_root(project_root)
    except Exception:
        return project_root


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

    main_dir.mkdir(parents=True, exist_ok=True)
    created.append(str(main_dir.relative_to(project_root)))

    # 默认 master：先写容器根 .orchd/，随后整体移入 main/.orchd/
    orchd_src = project_root / ".orchd"
    orchd_src.mkdir(parents=True, exist_ok=True)
    if not master_path.exists():
        master_path.write_text(
            json.dumps(_default_master(main_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        created.append(str(master_path.relative_to(project_root)))

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

    # 移入 main/.orchd/（若存在）
    orchd_dst = main_dir / ".orchd"
    if orchd_src.exists() and not orchd_dst.exists():
        shutil.move(str(orchd_src), str(orchd_dst))
        created.append(str(orchd_dst.relative_to(project_root)))

    # 共享账本 runtime 根
    runtime = project_root / _RUNTIME_DIR
    runtime.mkdir(parents=True, exist_ok=True)
    created.append(str(runtime.relative_to(project_root)))

    # 布局标记
    write_layout(orchd_dst, "container", main_dir)
    created.append(str(marker_path(orchd_dst).relative_to(project_root)))

    return {
        "container": True,
        "main_worktree": str(main_dir),
        "runtime_root": str(runtime),
        "marker": str(marker_path(orchd_dst)),
        "created": created,
    }


def layout_migrate(project_root: Path) -> dict[str, Any]:
    """flat → container 迁移：内容整体移入 ``main/``，保留 git 历史、可回滚（AC4）。

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
        if proc.returncode == 0 and proc.stdout.count("worktree ") > 1:
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
            dst = main_dir / entry.name
            shutil.move(str(entry), str(dst))
            moved.append(str(entry.name))
    except Exception:
        # 可回滚：把已移动条目移回（best-effort，不破坏原仓库）
        for name in moved:
            src = main_dir / name
            if src.exists():
                try:
                    shutil.move(str(src), str(project_root / name))
                except OSError:
                    pass
        return {
            "migrated": False,
            "reason": "move_failed",
            "hint": "迁移中断，已尝试回滚；请检查残留后重试",
        }

    # 共享账本 runtime 根 + 布局标记
    orchd_dir = main_dir / ".orchd"
    write_layout(orchd_dir, "container", main_dir)

    # 运行时账本搬到共享 runtime 根（容器布局默认账本根），master 目录文件保留
    ledger_moved = _move_ledger_runtime_files(orchd_dir, runtime)

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


def bindings_path(store_root: Path) -> Path:
    """返回任务↔worktree 绑定文件路径（位于共享账本根）。"""
    return Path(store_root) / _BINDINGS_FILENAME


def load_bindings(store_root: Path) -> dict[str, Any]:
    """读取任务↔worktree 绑定映射；缺失/损坏返回空 dict。"""
    try:
        data = json.loads(bindings_path(store_root).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _save_bindings(store_root: Path, data: dict[str, Any]) -> None:
    """原子写入绑定映射（tmp + os.replace）。"""
    path = bindings_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def bind_task_wt(store_root: Path, task_id: str, worktree_path: Path) -> dict[str, Any]:
    """绑定「任务 ↔ worktree」到共享账本根 session-worktrees.json（带 Store 文件锁）。

    Args:
        store_root: 共享账本根（resolve_store_dir 结果；未设 ORCHD_HOME 时随布局）。
        task_id: 任务 ID。
        worktree_path: 该任务的 worktree 根（独立任务 worktree 或主工作树降级）。

    Returns:
        ``{"bound": True, "task_id": <str>, "worktree": <str>}``
    """
    from orchd.ledger import Store

    store = Store(store_root)
    store.acquire_lock()
    try:
        data = load_bindings(store_root)
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
    """
    from orchd.ledger import Store

    store = Store(store_root)
    if not lock_held:
        store.acquire_lock()
    try:
        data = load_bindings(store_root)
        data.pop(task_id, None)
        _save_bindings(store_root, data)
    finally:
        if not lock_held:
            store.release_lock()
    return {"unbound": True, "task_id": task_id}


def resolve_task_root(store_root: Path, task_id: str) -> Path | None:
    """从绑定解析任务的 worktree 根；无绑定返回 None。

    Returns:
        ``Path``：绑定 worktree 绝对路径；或 None（未绑定）。
    """
    data = load_bindings(store_root)
    entry = data.get(task_id)
    if not entry or not entry.get("worktree"):
        return None
    return Path(entry["worktree"])


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
    if Path(project_root).resolve() != bound.resolve():
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
    """
    try:
        orchd = task_wt / ".orchd"
        orchd.mkdir(parents=True, exist_ok=True)
        write_layout(orchd, "container", main_wt)
    except Exception:
        pass


def ensure_task_wt(project_root: Path, task_id: str) -> dict[str, Any]:
    """创建/复用任务 worktree（best-effort，幂等）。

    container 布局 → 建独立任务 worktree（``git worktree add <task_wt_root>/task-<id>
    task/<id>``，已存在幂等复用）；flat（既有项目）→ 降级返回主工作树
    （任务 worktree 即主工作树本身，维持现状零回归）。

    Returns:
        ``{"worktree": <Path>, "separate": bool, "created": bool}``
        失败时 best-effort 返回主工作树降级（不抛异常，不阻断 claim）。
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
    try:
        if (wt_path / ".git").exists():
            # 已存在且是 worktree → 幂等复用（补写布局标记）
            _propagate_container_marker(wt_path, project_root)
            return {"worktree": wt_path, "separate": True, "created": False}
        # 先尝试 branch + worktree 一并创建（-b 建分支），分支已存在则回退
        proc = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path)],
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
            # 任务 worktree 自识别容器布局（共享账本根），见 _propagate_container_marker
            _propagate_container_marker(wt_path, project_root)
            return {"worktree": wt_path, "separate": True, "created": True}
        # worktree add 失败（如分支已在别处 checkout）→ best-effort 降级主工作树
        return {"worktree": project_root, "separate": False, "created": False}
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {"worktree": project_root, "separate": False, "created": False}


def remove_task_wt(
    project_root: Path, task_id: str, store_root: Path, *, lock_held: bool = False,
) -> dict[str, Any]:
    """终态回收：git worktree remove + 删 task/{id} 分支 + 解绑（best-effort 幂等）。

    ``lock_held=True``：调用方已在同一共享账本根持有 ``.lock``（同进程），解绑时
    复用该锁、不再重复 flock（见 :func:`unbind_task_wt`），规避同进程双 fd E012 死锁。

    Returns:
        ``{"removed": True, "unbound": True}``；失败降级 ``{"removed": False, ...}``。
    """
    layout = detect_layout(project_root)
    wt_path = layout["task_wt_root"] / _task_wt_name(task_id)
    # task-14-review-branch-cleanup(AC2)：容器布局下 project_root 即任务 worktree
    # （== wt_path），git worktree remove 会删掉该目录。用 main_worktree_root 在
    # 移除前（cwd 仍有效）解析主工作树根作为稳定 cwd，后续删分支不再依赖已删除的
    # cwd。local import 复用 gitops 已有解析（flat 布局回退 project_root，零回归）。
    from orchd.gitops import main_worktree_root

    stable_wt = main_worktree_root(project_root)
    removed = False
    discarded_uncommitted = False
    try:
        if (wt_path / ".git").exists():
            # P2-9：先无 --force 移除（仅干净 worktree 可移，避免丢弃未提交改动）；
            # 脏 worktree 才回退 --force（终态回收 best-effort），并记 discarded 告警。
            proc = subprocess.run(
                ["git", "worktree", "remove", str(wt_path)],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode != 0:
                proc = subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=str(project_root),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                discarded_uncommitted = proc.returncode == 0
            removed = proc.returncode == 0
        else:
            removed = True  # 独立 worktree 本就不存在 → 视为已回收
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        removed = False
    # 删任务分支（best-effort，未合并时 -d/-D 失败不阻断）
    # 以主工作树为稳定 cwd（git -C）：worktree 已回收时 task/{id} 不再被占用可删除；
    # project_root（任务 worktree）可能已被 git worktree remove 删除，不能作为 cwd。
    try:
        subprocess.run(
            ["git", "-C", str(stable_wt), "branch", "-D", f"task/{task_id}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    unbound = unbind_task_wt(store_root, task_id, lock_held=lock_held)
    result = {"removed": removed, "unbound": unbound.get("unbound", False)}
    if discarded_uncommitted:
        result["discarded_uncommitted"] = True
    return result


def prune_orphans(
    project_root: Path,
    store_root: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    """孤儿 worktree 惰性清理（watchdog / status 调用，best-effort）。

    清理两类：
    - 绑定任务已终态（completed/cancelled）但 worktree 仍在 → remove；
    - 绑定任务已不在 master / 无对应活跃任务 → remove + 解绑。

    Returns:
        ``{"pruned": [<str>], "orphans_found": int}``
    """
    from orchd.gitops import _has_linked_worktrees

    project_root = Path(project_root).resolve()
    if not _has_linked_worktrees(project_root):
        return {"pruned": [], "orphans_found": 0}

    bindings = load_bindings(store_root)
    pruned: list[str] = []
    for task_id, entry in list(bindings.items()):
        ts = state.get(task_id)
        status = ts.status if ts else "pending"
        if status in ("completed", "cancelled"):
            # 终态：回收 worktree + 解绑
            result = remove_task_wt(project_root, task_id, store_root)
            if result.get("removed"):
                pruned.append(task_id)
    return {"pruned": pruned, "orphans_found": len(pruned)}


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
        declared = set(declared_files)
        return sorted(set(dirty) & declared)
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
        declared = set(declared_files)
        return sorted(declared - branch_files)
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
        overlap = target_files & set(actual)
        if overlap:
            conflicts.append({
                "task_id": tid,
                "files": sorted(overlap),
                "claimed_by": claimed_by,
                "source": "actual",
            })
    return conflicts
