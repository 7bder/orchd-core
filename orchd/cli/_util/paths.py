"""cli 通用工具域（W6-C8）：cwd/命令名/master 加载（paths）、IDEAS 归档主流程（archive）、
源码回收后的子进程兜底（fallback）。

域：paths（W6-C8 从 orchd/cli/_util.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchd.constants import ORCHD_ALLOW_CONTAINER_ROOT_ENV
from orchd.errors import ErrorCode, OrchdError


_ORCHD_ALLOW_CONTAINER_ROOT = ORCHD_ALLOW_CONTAINER_ROOT_ENV  # 变量名真源：orchd.constants



def _command_name(args: Any) -> str:
    """从已解析 args 反推当前命令名（子命令首 token，如 status / force-status / session）。

    引擎侧无感引导分级（task-guide-tiering）需要知道所属命令；argparse 不直接暴露
    命令名，这里从 func 名（``_cmd_xxx``）映射回连字符风格命令名。子命令
    （如 ``_cmd_session_current`` → ``session-current``）按 func 名派生。
    """
    func = getattr(args, "func", None)
    if func is None:
        return ""
    name = getattr(func, "__name__", "")
    if name.startswith("_cmd_"):
        return name[len("_cmd_"):].replace("_", "-")
    return name



def _resolve_text_arg(
    inline: str | None,
    file_path: str | None,
    inline_name: str,
    file_name: str,
    required: bool = True,
) -> str | None:
    """解析 内联文本 / --xxx-file 二选一参数。

    Windows shell（尤其 PowerShell / cmd）对多行字符串的解析存在已知问题：
    含换行符的长文本在命令行中可能被 shell 拆分为多个独立参数，导致 argparse
    报错或截断。因此本函数允许调用者将长文本写入临时 UTF-8 文件后经
    file_name 参数传入，绕过 shell 的多行解析限制。
    """
    if inline and file_path:
        raise OrchdError(
            ErrorCode.E007,
            f"{inline_name} 与 {file_name} 只能二选一",
            [{"arguments": [inline_name, file_name]}],
        )
    if file_path:
        p = Path(file_path)
        if not p.exists():
            raise OrchdError(
                ErrorCode.E001,
                f"file not found: {file_path}",
                [{"path": str(p), "message": f"{file_name} 指定的文件不存在"}],
            )
        return p.read_text(encoding="utf-8").strip()
    if inline:
        return inline
    if required:
        raise OrchdError(
            ErrorCode.E007,
            f"必须提供 {inline_name} 或 {file_name}",
            [{"arguments": [inline_name, file_name]}],
        )
    return None



def _reject_container_root_cwd() -> None:
    """纪律护栏：拒绝在容器根（主工作树父目录）执行引擎命令。

    容器根残留 ``.orchd`` junction，``_find_orchd_dir`` 会命中它，使
    project_root 解析成容器根而非主工作树；已实踩会污染任务 worktree
    布局标记并引发 worktree/分支误删（2026-08-30 复盘）。
    设 ``ORCHD_ALLOW_CONTAINER_ROOT=1`` 显式豁免。best-effort：判定失败不阻断。
    """
    if os.environ.get(_ORCHD_ALLOW_CONTAINER_ROOT):
        return
    try:
        from orchd.worktree import detect_container_root_cwd

        reason, main_wt = detect_container_root_cwd(Path.cwd(), _find_orchd_dir())
    except Exception:
        return
    if reason is not None and main_wt is not None:
        raise OrchdError(
            ErrorCode.E036,
            message=(
                f"container_root_cwd: 当前目录 {Path.cwd()} 是容器根而非主工作树；"
                f"请切换到主工作树 {main_wt} 下执行引擎命令，否则会污染任务 "
                "worktree 布局标记并可能误删 worktree/分支"
            ),
            details=[
                {"cwd": str(Path.cwd())},
                {"main_worktree": str(main_wt)},
                {"hint": f"请 cd 到主工作树 {main_wt} 后再执行引擎命令（或设 "
                         "ORCHD_ALLOW_CONTAINER_ROOT=1 显式豁免）"},
            ],
        )



def _find_orchd_dir() -> Path:
    """查找 .orchd/ 目录（CLI 主路径，task-canonical-root-boundary-guard）。

    搜索策略：从当前工作目录开始逐级向上，返回第一个包含 ``.orchd/`` 的路径；
    一直未找到则回退 ``cwd / ".orchd"``。

    仓库边界（AC1/AC2）：与 ``orchd.ledger._find_orchd_dir`` **同源**委托
    :func:`orchd.worktree.find_orchd_dir_within_git_boundary`——遍历不得越过
    cwd 所属的最近 git 仓库根。pytest tmp_path 落在宿主仓库内且其层级目录被
    ``git init`` 成独立仓库时，旧实现会越过内层仓库顶误定位到宿主真实
    ``.orchd``（实测真仓库被切到 task/t1、残留分支）；现内层独立仓库无
    ``.orchd`` 即返回 ``cwd/.orchd``，非 git 目录维持逐级向上（零回归）。
    """
    from orchd.worktree import find_orchd_dir_within_git_boundary

    return find_orchd_dir_within_git_boundary(Path.cwd())



def _flatten_nargs(values: list[str] | None) -> list[str] | None:
    """展平 nargs="*" 参数：引号包裹的单个参数按空白或逗号拆分为多个。

    shell 引号（--capabilities "python git docs"）下 argparse 会把整个
    引号串当作一个参数（["python git docs"]），能力过滤全部不匹配并误报
    "当前无就绪任务"；此处按空白展平，等价于 --capabilities python git docs。
    同时兼容逗号分隔（--exclude "task-a,task-b"）。
    None 或空列表原样返回。
    """
    if not values:
        return values
    out: list[str] = []
    for v in values:
        for token in v.replace(",", " ").split():
            if token:
                out.append(token)
    return out



def _load_tasks(
    master_path: str | None = None, *, with_archived: bool = False
) -> tuple[list, Path, Any]:
    """加载 master 并返回 (tasks, orchd_dir, master)。

    canonical-master-read（task-canonical-project-root）：未显式指定
    ``master_path`` 时，master 任务定义统一从 canonical 主工作树
    ``.orchd/_master.json`` 读取（``resolve_canonical_project_root`` 定位），
    避免任务 worktree 本地 checkout 副本与主工作树不同步导致的任务池不一致
    （pool/status/request 等业务读与 main 一致）。flat 布局 canonical == 本地
    → 零回归。返回的 ``orchd_dir`` 保持当前 worktree 定位语义不变（Store 账本
    经 ``resolve_store_dir`` 解析到共享账本根；project_root 物理操作基准不受影响）。

    回读合并（task-master-archive-readback-merge）：``with_archived=True`` 时把
    master 清单声明的**归档终态定义**追加进返回值——活跃定义保序在前、归档条目按
    归档序追加，同 id 以活跃定义为准
    （见 :func:`orchd.definition_archive.merge_archived_definitions`）。
    **默认 False 是结构性约束**：派发面（``request`` / ``pool``）与里程碑判据必须
    看不到归档定义——不依赖「归档任务必为终态」这条隐式不变量，也不依赖
    ``build_pool`` 的状态过滤；只有展示面（``status`` / ``show``）显式开启。
    无归档（清单缺席或空骨架）时两取值返回值逐字相同（零回归）。

    Args:
        master_path: 显式 master 路径；None 时按 canonical 解析。
        with_archived: 是否合并归档中的终态任务定义（默认 False，见上）。

    Returns:
        ``(tasks, orchd_dir, master)``。``tasks`` **恒为新列表**（两种取值下均不
        回传 ``master.tasks`` 同一对象），调用方可安全就地改动。

    Raises:
        OrchdError: ``with_archived=True`` 且归档清单在场但不可读（缺文件 /
            不可解析 / ``count`` / ``sha256`` 不符 / 条目 id 重复）——E007
            ``archive_unreadable``。**不静默返回不完整历史**：归档是入库载体
            （git-tracked），不可读属非预期完整性问题，与「CI 新克隆无运行时
            账本」这类预期态（走显式降级）不同。
    """
    from orchd.spec import load_master
    from orchd.worktree import resolve_master_path_from_dir

    if master_path:
        path = Path(master_path)
        orchd_dir = path.parent
    else:
        orchd_dir = _find_orchd_dir()
        # canonical-master-read 收敛到底座：本地优先 → canonical 回退
        # （task-cli-master-rule-single-source；两态结论与原逐字实现一致，
        # 仅本地残留副本时读本地，分歧方向更保守）。
        path = resolve_master_path_from_dir(orchd_dir)
    master = load_master(path)
    if not with_archived:
        # 返回**新列表**（不再回传 master.tasks 同一对象）：消除「条件性别名」，
        # 使调用方恒可安全就地排序/过滤而不污染 Master 内存态。
        return list(master.tasks), orchd_dir, master

    from orchd.definition_archive import (
        DefinitionArchiveError,
        merge_archived_definitions,
        read_archive_with_manifest,
    )
    from orchd.errors import ErrorCode, OrchdError

    try:
        # 复用已加载的 master 字典（不二次读盘）：归档位置由清单 path 解析。
        archived, _manifest = read_archive_with_manifest(master.raw, path)
    except DefinitionArchiveError as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"archive_unreadable: 归档清单在场但不可读"
            f"（拒绝返回不完整历史）: {exc}",
            [{"path": str(path)}],
        ) from exc
    if not archived:
        return list(master.tasks), orchd_dir, master
    return merge_archived_definitions(list(master.tasks), archived), orchd_dir, master




def _resolve_canonical_orchd_dir(task_orchd_dir: Path) -> Path | None:
    """从已失效的任务 worktree .orchd 路径推断 canonical 主工作树 .orchd。

    容器布局约定：``<容器>/task-<id>/.orchd`` → ``<容器>/main/.orchd``。
    仅在任务 worktree 已被回收（目录不存在）时由 ``_maybe_archive_ideas``
    调用，作为归档回退路径。flat 布局下 task_orchd_dir.parent 即项目根，
    其自身就是 canonical，直接返回 ``task_orchd_dir``（调用方会再判存在性）。

    Returns:
        主工作树 .orchd 路径（不保证存在）；无法推断时返回 None。
    """
    try:
        task_wt_root = task_orchd_dir.parent
        container_root = task_wt_root.parent
        candidate = container_root / "main" / ".orchd"
        if candidate.exists():
            return candidate
        # flat 布局回退：任务 worktree 与主工作树同级，主工作树名不固定，
        # 尝试用 resolve_canonical_project_root 解析（best-effort）。
        from orchd.worktree import resolve_canonical_project_root

        canonical = resolve_canonical_project_root(task_wt_root)
        if canonical.is_dir():
            return canonical / ".orchd"
        return None
    except Exception:
        return None
