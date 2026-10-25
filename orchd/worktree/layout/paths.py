"""worktree layout paths 域（W6-C5 纯搬移）。

来源：``orchd/worktree/layout.py``（1079 行，W6-C5 纯搬移）。
"""

from __future__ import annotations

from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD,
    BRIEF_FIELD,
    DEPENDS_ON_FIELD,
    DIFFICULTY_FIELD,
    ESTIMATED_HOURS_FIELD,
    FILES_TO_EDIT_FIELD,
    FILES_TO_READ_FIELD,
    ID_FIELD,
    IMPORTANCE_FIELD,
    MODULE_FIELD,
    NAME_FIELD,
    REQUIRES_FIELD,
    REVIEWERS_FIELD,
    SOURCE_FIELD,
)


import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchd.ledger import Store
from .marker import (
    read_layout,
)

from .gitinfo import (
    _GIT_TIMEOUT,
)

from .detect import (
    detect_layout,
)




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
        ``{SOURCE_FIELD: "main_blob"|"canonical_file"|"local_file"|"fallback_local",
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
        return fallback_tasks, {SOURCE_FIELD: "local_file", "reason": "no_project_root"}
    try:
        layout = detect_layout(Path(project_root)).get("layout")
    except Exception as exc:
        _trace("layout_unreadable", str(exc)[:200])
        return fallback_tasks, {SOURCE_FIELD: "fallback_local", "reason": "layout_unreadable"}
    if layout == "container":
        # container 调用方（CLI _load_tasks）已从 canonical 主工作树读取，
        # 此处逐字保留、不重读（任务 worktree 副本被抑制，重读本地必空）。
        return fallback_tasks, {SOURCE_FIELD: "canonical_file"}
    from orchd.nogit import git_available as _git_avail
    try:
        _git_ok = bool(_git_avail(Path(project_root)))
    except Exception:
        _git_ok = False
    if not _git_ok:
        return fallback_tasks, {SOURCE_FIELD: "local_file", "reason": "no_git"}
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
        return fallback_tasks, {SOURCE_FIELD: "fallback_local", "reason": "blob_unreadable"}
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        _trace("blob_unreadable", (proc.stderr or "")[:200] or f"ref {ref} 无该 blob")
        return fallback_tasks, {SOURCE_FIELD: "fallback_local", "reason": "blob_unreadable"}
    try:
        data = json.loads(proc.stdout)
        blob_tasks = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(blob_tasks, list):
            raise ValueError("tasks 表缺失")
    except (ValueError, TypeError) as exc:
        _trace("blob_unreadable", f"blob 解析失败: {exc}"[:200])
        return fallback_tasks, {SOURCE_FIELD: "fallback_local", "reason": "blob_unreadable"}
    return list(blob_tasks), {SOURCE_FIELD: "main_blob", "ref": ref}



def _default_master(main_worktree: Path) -> dict[str, Any]:
    """生成 container 新项目的默认 master（通过 schema 的最小合法项目）。"""
    return {
        "schema_version": 1,
        "project": {
            NAME_FIELD: "New Orchd Project",
            BRIEF_FIELD: "A new orchd-managed project (container layout).",
        },
        "modules": [
            {
                ID_FIELD: "mod-core",
                NAME_FIELD: "Core",
                "role": "Engine core module",
            }
        ],
        "tasks": [
            {
                ID_FIELD: "task-sample",
                NAME_FIELD: "Sample Task",
                BRIEF_FIELD: "Sample task created by orchd init.",
                MODULE_FIELD: "mod-core",
                DEPENDS_ON_FIELD: [],
                ESTIMATED_HOURS_FIELD: 1,
                IMPORTANCE_FIELD: "high",
                DIFFICULTY_FIELD: "low",
                REQUIRES_FIELD: ["python"],
                ACCEPTANCE_CRITERIA_FIELD: ["sample"],
                FILES_TO_READ_FIELD: [],
                FILES_TO_EDIT_FIELD: ["README.md"],
                REVIEWERS_FIELD: ["reviewer-1"],
            }
        ],
    }
