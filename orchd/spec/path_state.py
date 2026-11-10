"""orchd/spec/path_state.py — 声明路径的「磁盘事实」单一判定（纯搬移域）。

来源：``orchd/spec/declarations.py`` 原 L272-484（task-engine-defect-fix-unified-u1
引入的四态分类段），task-v163-spec-decl-split 按域边界纯搬移，常量与函数体零改。
``orchd/spec/declarations.py`` 仍按原路径重导出本域公开名，既有消费点
（``onboard/claim/precheck.py`` / ``onboard/lifecycle/guards.py`` / ``gitops/commit.py`` /
``split/amend_route_state.py``）无需改动。

依赖方向：本域 → 仅标准库（subprocess 直调 git，不导入 orchd 内部模块）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------
# 声明路径的「磁盘事实」单一判定（task-engine-defect-fix-unified-u1）
# ------------------------------------------------------------------
# 背景：声明文件的磁盘事实此前没有单一判定点——claim 完全不校验存在性
# （问题拖到 done 的 E010）、claimed 态 amend 只增不删（幽灵声明只能走
# retract 四步）、E026 对删除态无豁免。本段把四态分类收敛到**唯一实现**：
#   - ``exists``          磁盘存在且被 git 跟踪（常规修改）
#   - ``untracked-new``   磁盘存在但未被 git 跟踪（新建未提交）
#   - ``tracked-deleted`` git 索引仍跟踪、磁盘已删除（声明的是删除目标）
#   - ``ghost``           磁盘不存在且未被跟踪（待新建文件 / 重命名残留）
# claim 预检（declaration_warnings）、claimed 态 amend 撤回窗口、E026 必须
# 共用本实现，禁止在消费点另写存在性/跟踪判定（双写漂移，同 E026 推导的红线）。
# 依赖方向：本段仍只用标准库（subprocess 直调 git，与 worktree/gitinfo 先例同形）。

PATH_STATE_EXISTS = "exists"
PATH_STATE_UNTRACKED_NEW = "untracked-new"
PATH_STATE_TRACKED_DELETED = "tracked-deleted"
PATH_STATE_GHOST = "ghost"

_PATH_STATE_HINTS = {
    PATH_STATE_GHOST: (
        "声明路径在磁盘不存在且未被 git 跟踪：若是本卡待新建文件可忽略；"
        "若是重命名/删除后的残留路径，可在 claimed 态用 amend 撤回"
        "（--remove-files-to-edit <path>）"
    ),
    PATH_STATE_TRACKED_DELETED: (
        "声明路径仍被 git 跟踪但磁盘已删除：按删除目标处理（E026 不再要求"
        "配套测试；claimed 态 amend 不允许移除该声明）"
    ),
}


def _norm_decl_path(path: Any) -> str:
    """声明路径归一（分隔符 + 去空白）；空值返回空串由调用方跳过。"""
    return str(path or "").replace("\\", "/").strip()


def _resolve_decl_repo_root(project_root: Any) -> Path | None:
    """解析声明路径的判定根（container 布局 ``<root>/main`` vs 单工作树）。

    容器布局下 project_root 是容器根、真正的 git 工作树在其 ``main/`` 子目录；
    单工作树布局下两者同一。判定只按 ``.git`` 存在性推导（纯标准库）。
    """
    if project_root is None:
        return None
    root = Path(str(project_root))
    if (root / ".git").exists():
        return root
    nested = root / "main"
    if (nested / ".git").exists():
        return nested
    return root


def _decl_view(raw: str, repo_root: Path | None) -> tuple[bool, str]:
    """返回 (磁盘是否存在, 用于跟踪判定的仓库相对路径)。

    声明路径可能是仓库相对（``orchd/x.py``）或容器相对（``main/orchd/x.py``），
    两种写法按存在性择优；都不存在时保留原样供跟踪判定。
    """
    if repo_root is None:
        return False, raw
    candidates = [raw]
    if raw.startswith("main/"):
        candidates.append(raw[len("main/"):])
    else:
        candidates.append("main/" + raw)
    for cand in candidates:
        try:
            if (repo_root / cand).exists():
                return True, cand
        except OSError:  # pragma: no cover - 损坏路径/权限
            continue
    return False, raw


def _tracked_candidates(raw: str, rel: str) -> list[str]:
    """跟踪判定候选（git 索引路径永不带容器前缀 ``main/``）。

    ``_decl_view`` 在两种写法都不存在时回填 ``raw``；若声明写成容器相对
    ``main/<repo-relative>``，索引里只有去前缀形式，直接按 ``rel`` 查会漏判成
    ghost（F1：tracked-deleted 被误判 → amend 可撤回已实现声明）。故对 ``main/``
    写法在查索引时同时试去前缀形式；其余写法维持单候选，不引入单工作树歧义。
    """
    cands = [rel]
    if raw.startswith("main/"):
        cands.append(raw[len("main/"):])
    return cands


def git_tracked_files(project_root: Any) -> set[str] | None:
    """返回 git 索引中的仓库相对路径集合；git 不可用/非仓库时返回 ``None``。

    best-effort：判定失败一律降级为「跟踪信息未知」，由 :func:`path_state`
    退化为 exists/ghost 两态，不阻断调用方。
    """
    repo_root = _resolve_decl_repo_root(project_root)
    if repo_root is None:
        return None
    # 非仓库目录（大量单测在 tmp_path 上直接校验）不起子进程：直接判定跟踪信息
    # 不可用，由 path_state 降级为 exists/ghost——与 git 调用失败语义一致。
    if not (repo_root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 环境缺 git
        return None
    if proc.returncode != 0:
        return None
    return {p for p in proc.stdout.split("\0") if p}


def declared_path_exists(path: str | Path, project_root: Any) -> bool:
    """声明路径在磁盘上是否存在（container 布局感知；不触碰 git）。"""
    raw = _norm_decl_path(path)
    if not raw:
        return False
    repo_root = _resolve_decl_repo_root(project_root)
    return _decl_view(raw, repo_root)[0]


def path_state(
    path: str | Path,
    project_root: Any,
    *,
    tracked_files: set[str] | None,
) -> str:
    """声明路径的磁盘事实四态分类（**唯一判定点**）。

    Args:
        path: 声明路径（``files_to_edit`` / ``exempt_files`` 项）。
        project_root: 项目根或 container 根（内部按 ``.git`` 定位工作树）。
        tracked_files: git 索引路径集合；``None`` 表示跟踪信息不可用
            （降级为 exists/ghost，不自行起子进程）。

    Returns:
        ``PATH_STATE_*`` 之一。
    """
    raw = _norm_decl_path(path)
    if not raw:
        return PATH_STATE_GHOST
    repo_root = _resolve_decl_repo_root(project_root)
    exists, rel = _decl_view(raw, repo_root)
    tracked_hit = tracked_files is not None and any(
        cand in tracked_files for cand in _tracked_candidates(raw, rel)
    )
    if exists:
        if tracked_files is None or tracked_hit:
            return PATH_STATE_EXISTS
        return PATH_STATE_UNTRACKED_NEW
    if tracked_hit:
        return PATH_STATE_TRACKED_DELETED
    return PATH_STATE_GHOST


def classify_declared_paths(
    declared: list[Any] | tuple[Any, ...] | None,
    project_root: Any,
    *,
    tracked_files: set[str] | None = None,
) -> list[dict[str, str]]:
    """批量分类声明路径（保序、去重），返回 ``[{"path", "state"}]``。

    ``tracked_files is None`` 时自行读一次 git 索引（失败则降级），供一次
    claim / 一次 amend 这类低频调用直接使用。
    """
    if not declared:
        return []
    if tracked_files is None:
        tracked_files = git_tracked_files(project_root)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in declared:
        raw = _norm_decl_path(item)
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append({
            "path": raw,
            "state": path_state(raw, project_root, tracked_files=tracked_files),
        })
    return out


def declaration_path_warnings(
    declared: list[Any] | tuple[Any, ...] | None,
    project_root: Any,
    *,
    tracked_files: set[str] | None = None,
) -> list[dict[str, str]]:
    """claim 预检的声明路径预警（由 :func:`path_state` 单一产出）。

    只对需要关注的态出预警：``ghost``（带撤回出路 hint）与
    ``tracked-deleted``（删除目标提示）；``exists`` / ``untracked-new`` 静默。
    project_root 缺失（纯单测直接调 claim 无项目根）时返回空——无从判定即不预警。
    """
    if project_root is None:
        return []
    warnings: list[dict[str, str]] = []
    for item in classify_declared_paths(
            declared, project_root, tracked_files=tracked_files):
        hint = _PATH_STATE_HINTS.get(item["state"])
        if hint:
            warnings.append({**item, "hint": hint})
    return warnings
