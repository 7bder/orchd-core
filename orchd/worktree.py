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
import sys
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

    task-workspace-docs-isolation：同步工作区规划文档 ROADMAP.md（gitignore
    不入库 → 任务 worktree 缺副本，roadmap_landing_warnings 读本地缺失被跳过，
    与 main 行为不一致）。best-effort 从 main 拷贝，缺失静默跳过；ROADMAP.md
    未跟踪，拷贝不产生未提交改动（不触发 E017）。
    """
    try:
        orchd = task_wt / ".orchd"
        orchd.mkdir(parents=True, exist_ok=True)
        write_layout(orchd, "container", main_wt)
    except Exception:
        pass
    try:
        src = main_wt / ".orchd" / "ROADMAP.md"
        if src.exists():
            shutil.copy2(str(src), str(task_wt / ".orchd" / "ROADMAP.md"))
    except OSError:
        pass


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
    try:
        if (wt_path / ".git").exists():
            # 已存在且是 worktree → 幂等复用（补写布局标记）
            _propagate_container_marker(wt_path, project_root)
            return {"worktree": wt_path, "separate": True, "created": False}
        # 创建期不变量硬化（W-4，复盘 P1 孤儿分支修复）：任务分支必须从**主分支**
        # fork（`git worktree add -b task/<id> <path> <main>`），杜绝因主工作树当
        # 前检出的非 main 分支而生成孤儿/悬空分支。base 解析失败（无 main/master）
        # 则回退当前 HEAD（best-effort），仍可创建但依赖探测结果。
        from orchd.gitops import get_default_branch
        base = get_default_branch(project_root)
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
            return {"worktree": wt_path, "separate": True, "created": True}
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
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": f"worktree add 异常: {exc}",
        }


def _cleanup_stale_task_wt(project_root: Path, wt_path: Path) -> None:
    """best-effort 清理 worktree add 失败残留的半成品元数据（孤儿 worktree）。

    - ``git worktree prune``：移除指向已消失目录的失效 git 注册（安全）。
    - 残留目录仅在其**非有效 worktree**（无 ``.git`` 标记）时删除，有标记
      视为有效 worktree 绝不误删（与 remove_task_wt 的 P0-19 残留清理同构）。
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
    if not wt_path.exists() or (wt_path / ".git").exists():
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


def _log_recycle(records: list[dict[str, Any]]) -> None:
    """把删除决策/动作记录打印到 stderr（``orchd ▸ [回收]`` 前缀，best-effort）。

    审计契约（2026-08-30 task-audit-* 分支丢失复盘 §1）：任何 worktree /
    分支 / 目录的删除动作与「拒绝删除」决策都必须留痕，杜绝 best-effort 静默
    删除无法追溯。单条记录为结构化 dict（action / target / reason / evidence /
    actor），JSON 序列化输出。失败静默跳过，不阻断主流程。
    """
    try:
        for rec in records:
            print(f"orchd ▸ [回收] {json.dumps(rec, ensure_ascii=False)}", file=sys.stderr)
    except Exception:
        pass


def _task_status_for_recycle(store_root: Path, task_id: str) -> str | None:
    """best-effort 读取任务当前状态（删除保护断言与决策打点共用）。

    经 ``orchd.ledger.Store.replay()`` 只读查询；store_root 不可用 / 读取异常
    返回 None（best-effort，不阻断删除——in_review 保护仅在能**确认** in_review
    时生效，避免账本异常导致回收被永久卡死）。
    """
    try:
        from orchd.ledger import Store

        ts = Store(store_root).replay().get(task_id)
        return ts.status if ts else None
    except Exception:
        return None


def remove_task_wt(
    project_root: Path, task_id: str, store_root: Path, *, lock_held: bool = False,
) -> dict[str, Any]:
    """终态回收：git worktree remove + 删 task/{id} 分支 + 解绑（best-effort 幂等）。

    ``lock_held=True``：调用方已在同一共享账本根持有 ``.lock``（同进程），解绑时
    复用该锁、不再重复 flock（见 :func:`unbind_task_wt`），规避同进程双 fd E012 死锁。

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

    # in_review 保护断言（只读查询，确认命中才拒绝；查不到状态不阻断）
    status = _task_status_for_recycle(store_root, task_id)
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

    removed = False
    discarded_uncommitted = False
    wt_existed = (wt_path / ".git").exists()
    try:
        if wt_existed:
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
    recycle_log.append({
        "action": "worktree_remove" if wt_existed else "worktree_absent",
        "task_id": task_id,
        "target": str(wt_path),
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "status": status,
        "actor": actor,
    })
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
    # 删任务分支（best-effort，未合并时 -d/-D 失败不阻断）
    # 以主工作树为稳定 cwd（git -C）：worktree 已回收时 task/{id} 不再被占用可删除；
    # project_root（任务 worktree）可能已被 git worktree remove 删除，不能作为 cwd。
    branch_deleted = False
    try:
        proc = subprocess.run(
            ["git", "-C", str(stable_wt), "branch", "-D", f"task/{task_id}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        branch_deleted = proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        branch_deleted = False
    recycle_log.append({
        "action": "branch_delete",
        "task_id": task_id,
        "branch": f"task/{task_id}",
        "deleted": branch_deleted,
        "actor": actor,
    })
    unbound = unbind_task_wt(store_root, task_id, lock_held=lock_held)
    recycle_log.append({
        "action": "unbind",
        "task_id": task_id,
        "unbound": unbound.get("unbound", False),
        "actor": actor,
    })
    _log_recycle(recycle_log)
    result = {"removed": removed, "unbound": unbound.get("unbound", False),
              "recycle_log": recycle_log}
    if discarded_uncommitted:
        result["discarded_uncommitted"] = True
    if residual_cleaned:
        result["residual_cleaned"] = True
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
    保守名单，绝不触碰 .git / .orchd / 工具目录 / 被跟踪文件。

    Returns:
        已清理的条目名清单。
    """
    cleaned: list[str] = []
    try:
        for entry in sorted(task_wt_root.iterdir()):
            if not _is_junk_entry(entry.name):
                continue
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

    删除决策审计（2026-08-30 复盘 §1）：每次判定「可清理」前输出决策上下文
    （task_id / status / has_active_binding / git 登记 / 判定依据），删除动作由
    ``remove_task_wt`` 内部再记 recycle_log，杜绝 best-effort 静默删除无痕。

    Returns:
        ``{"pruned": [<str>], "orphans_found": int, "residual_cleaned": [<str>],
        "decisions": [<dict>]}``；decisions 为本次全部删除/拒绝决策记录。
    """
    from orchd.gitops import _has_linked_worktrees

    project_root = Path(project_root).resolve()
    bindings = load_bindings(store_root)
    pruned: list[str] = []
    residual_cleaned: list[str] = []
    decisions: list[dict[str, Any]] = []
    actor = _recycle_actor()

    # 既有绑定任务清理（需要 git 层 linked worktrees 存在才执行 git worktree remove）
    if _has_linked_worktrees(project_root):
        for task_id, entry in list(bindings.items()):
            ts = state.get(task_id)
            status = ts.status if ts else "pending"
            if status in ("completed", "cancelled"):
                # 终态：回收 worktree + 解绑（删除动作由 remove_task_wt 记 recycle_log）
                decisions.append({
                    "action": "recycle",
                    "task_id": task_id,
                    "kind": "terminal_binding",
                    "status": status,
                    "bound_worktree": (entry or {}).get("worktree"),
                    "actor": actor,
                })
                _log_recycle(decisions[-1:])
                result = remove_task_wt(project_root, task_id, store_root)
                if result.get("removed"):
                    pruned.append(task_id)

    # P0-19：扫描文件系统残留 task-* 目录（git 不登记但目录仍存在）。
    # 不依赖 _has_linked_worktrees——Windows 下 git worktree remove 成功但目录残留。
    try:
        layout = detect_layout(project_root)
        task_wt_root = layout["task_wt_root"]
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
                        ts = state.get(cid)
                        status = ts.status if ts else "pending"
                        if status not in ("completed", "cancelled"):
                            has_active_binding = True
                        break
                if has_active_binding:
                    continue
                # 无活跃绑定 → 尝试清理残留目录
                if not (entry / ".git").exists():
                    decisions.append({
                        "action": "clean",
                        "target": entry.name,
                        "kind": "residual_dir",
                        "has_active_binding": False,
                        "git_registered": False,
                        "actor": actor,
                    })
                    _log_recycle(decisions[-1:])
                    try:
                        entry.rmdir()  # 仅空目录
                        residual_cleaned.append(entry.name)
                    except OSError:
                        try:
                            shutil.rmtree(str(entry), ignore_errors=True)
                            if not entry.exists():
                                residual_cleaned.append(entry.name)
                        except Exception:
                            pass
    except Exception:
        pass

    # task-workspace-docs-isolation：容器级卫生清理（best-effort）——
    # ① worktree 已不存在的会话锁残留；② 容器根可再生杂项；③ 系统 temp 中
    # 历史 orchd-trash-* 残留（早期 _safe_delete 降级重命名产物）。
    stale_locks_cleaned: list[str] = []
    junk_cleaned: list[str] = []
    trash_residue_cleaned: list[str] = []
    try:
        layout = detect_layout(project_root)
        task_wt_root = layout["task_wt_root"]
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
        declared = set(declared_files)
        missing = sorted(declared - branch_files)
        if not missing:
            return []

        results: list[dict[str, str]] = []
        for fp in missing:
            full = pr / fp
            if not full.exists():
                results.append({
                    "file": fp,
                    "reason": "path_not_found",
                    "detail": f"路径 {fp} 在磁盘不存在",
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
            results.append({
                "file": fp,
                "reason": "not_committed",
                "detail": "文件存在且未被 .gitignore 忽略，但未出现在任务分支 diff 中",
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
        overlap = target_files & set(actual)
        if overlap:
            conflicts.append({
                "task_id": tid,
                "files": sorted(overlap),
                "claimed_by": claimed_by,
                "source": "actual",
            })
    return conflicts
