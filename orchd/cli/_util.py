"""Orchd CLI 通用工具函数（无 commands 依赖，打破循环导入）。

从 orchd/cli/parser.py 迁出（task-split-cli-remove-legacy，3a 收尾）：
  - _command_name: 从已解析 args 反推当前命令名
  - _resolve_text_arg: 内联文本 / --xxx-file 二选一参数解析
  - _reject_container_root_cwd: 容器根拒绝纪律护栏（E036）
  - _find_orchd_dir: 查找 .orchd/ 目录
  - _flatten_nargs: 展平 nargs="*" 参数
  - _load_tasks: 加载 master 并返回 (tasks, orchd_dir, master)
  - _maybe_archive_ideas: 任务终态后触发 IDEAS 归档

这些函数不依赖 commands 子包，移到独立模块后，parser.py 和 commands/*.py
均可安全导入，避免 __init__ -> parser -> commands -> parser 的循环导入。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

_ORCHD_ALLOW_CONTAINER_ROOT = "ORCHD_ALLOW_CONTAINER_ROOT"


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
    """查找 .orchd/ 目录。

    搜索策略：从当前工作目录开始，逐级向上遍历父目录，返回第一个包含
    ``.orchd/`` 子目录的路径。若一直未找到，则回退为 ``cwd / ".orchd"``。
    """
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        candidate = parent / ".orchd"
        if candidate.is_dir():
            return candidate
    return cwd / ".orchd"


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


def _load_tasks(master_path: str | None = None) -> tuple[list, Path, Any]:
    """加载 master 并返回 (tasks, orchd_dir, master)。

    canonical-master-read（task-canonical-project-root）：未显式指定
    ``master_path`` 时，master 任务定义统一从 canonical 主工作树
    ``.orchd/_master.json`` 读取（``resolve_canonical_project_root`` 定位），
    避免任务 worktree 本地 checkout 副本与主工作树不同步导致的任务池不一致
    （pool/status/request 等业务读与 main 一致）。flat 布局 canonical == 本地
    → 零回归。返回的 ``orchd_dir`` 保持当前 worktree 定位语义不变（Store 账本
    经 ``resolve_store_dir`` 解析到共享账本根；project_root 物理操作基准不受影响）。
    """
    from orchd.spec import load_master
    from orchd.worktree import resolve_canonical_project_root

    if master_path:
        path = Path(master_path)
        orchd_dir = path.parent
    else:
        orchd_dir = _find_orchd_dir()
        canonical_root = resolve_canonical_project_root(orchd_dir.parent)
        path = canonical_root / ".orchd" / "_master.json"
    master = load_master(path)
    return master.tasks, orchd_dir, master


def _maybe_archive_ideas(orchd_dir: Path) -> dict:
    """best-effort：任务进入终态后触发 IDEAS 归档并自动提交。

    加载 master → 调 ``archive_resolved_ideas`` → 若有归档条目则
    ``ensure_committed([IDEAS.md, IDEAS-archive.md])``。非 main 分支降级
    为不提交（对齐 amend 的 ``not_on_main`` 语义），避免把归档提交进任务分支。
    任何异常静默降级，不阻断调用方。

    container 终态回收守卫（task-review-archive-crash-guard）：review code
    APPROVED 终态回收会删除正在运行的任务 worktree（含 orchd/ 源码）。此后
    ``_cmd_review`` 调本函数时，``orchd_dir`` 与其下的 orchd.ideas 等源码模块
    已从磁盘消失——须在懒加载前先判 ``orchd_dir`` 是否仍存在，否则懒加载
    ``from orchd.ideas import ...`` 抛 ModuleNotFoundError、命令 exit 1、
    best-effort 分支清理被中断。此处降级跳过，并保证懒加载/归档任意异常
    静默降级（对齐"任何异常静默降级"契约）。

    Returns:
        归档结果；若无可归档条目或异常，返回 ``{"archived": [], ...}``。
    """
    # 前置守卫：worktree 已终态回收 → orchd_dir（含 orchd/ 源码）消失，
    # 必须在懒加载之前降级，避免 ModuleNotFoundError 阻断调用方。
    if not orchd_dir.exists():
        return {"archived": [], "kept": 0, "skipped": "worktree_recycled"}
    master_path = orchd_dir / "_master.json"
    if not master_path.exists():
        # task-master-single-copy：container 任务 worktree 已抑制副本，本地无可读
        # _master.json 时回退 canonical 主工作树（唯一权威）；flat 布局 canonical ==
        # 本地，零回归。archived 判定基于权威 master，避免任务 worktree 因缺副本而漏归档。
        from orchd.worktree import resolve_canonical_project_root

        canonical = resolve_canonical_project_root(orchd_dir.parent)
        cand = canonical / ".orchd" / "_master.json"
        if not cand.exists():
            return {"archived": [], "kept": 0, "skipped": "no_master"}
        master_path = cand
        orchd_dir = canonical / ".orchd"
    try:
        from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
        from orchd.ideas import archive_resolved_ideas
        from orchd.spec import load_master

        master = load_master(master_path)
    except Exception:
        return {"archived": [], "kept": 0, "skipped": "archive_error"}
    project_root = orchd_dir.parent
    try:
        result = archive_resolved_ideas(project_root, master)
    except Exception:
        return {"archived": [], "kept": 0, "skipped": "archive_error"}
    if result.get("archived"):
        current_branch = get_current_branch(project_root)
        default_branch = get_default_branch(project_root) or "main"
        if current_branch is not None and current_branch != default_branch:
            result["commit"] = {
                "performed": False,
                "reason": "not_on_main",
                "branch": current_branch,
            }
        else:
            # AC3（task-12-engine-path-abstraction）：工作区文档走统一工作区根
            # helper（默认 .orchd/，兼容旧根路径）——ensure_committed 用相对
            # project_root 的路径，工作区根为 .orchd/ 时路径前缀 .orchd/。
            from orchd.ledger import resolve_workspace_root
            ws_root = resolve_workspace_root(project_root)

            def _rel(name: str) -> str:
                return str((ws_root / name).relative_to(project_root))

            result["commit"] = ensure_committed(
                project_root,
                [_rel("IDEAS.md"), _rel("IDEAS-archive.md")],
                "chore(ideas): 自动归档已完结条目",
            )
    return result
