"""Orchd CLI 通用工具函数（无 commands 依赖，打破循环导入）。

从 orchd/cli/parser.py 迁出（task-split-cli-remove-legacy，3a 收尾）：
  - _command_name: 从已解析 args 反推当前命令名
  - _resolve_text_arg: 内联文本 / --xxx-file 二选一参数解析
  - _reject_container_root_cwd: 容器根拒绝纪律护栏（E036）
  - _find_orchd_dir: 查找 .orchd/ 目录
  - _flatten_nargs: 展平 nargs="*" 参数
  - _load_tasks: 加载 master 并返回 (tasks, orchd_dir, master)
  - _maybe_archive_ideas: 任务终态后触发 IDEAS 归档
  - _orchd_source_recycled / _archive_ideas_via_subprocess: 引擎源码目录被终态
    回收后的归档兜底（在 canonical 主工作树起子进程执行 ideas-archive）
  - _preimport_archive_deps: 回收动作前预导入终态归档依赖（orchd.ideas）

这些函数不依赖 commands 子包，移到独立模块后，parser.py 和 commands/*.py
均可安全导入，避免 __init__ -> parser -> commands -> parser 的循环导入。
"""

from __future__ import annotations

import os
import sys
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
    return master.tasks, orchd_dir, master



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


def _orchd_source_recycled() -> bool:
    """本进程 orchd 包源码目录是否已被回收（container 终态回收删除任务 worktree）。

    container 布局下进程以 ``python .orchd/__main__.py``（cwd = 任务 worktree）
    启动，``orchd.__file__`` 指向任务 worktree 内的引擎副本；review code APPROVED
    的终态回收会删除该目录，此后**未绑定**模块的懒加载都会
    ``ModuleNotFoundError``（模块已加载时自身仍可运行，仅源码目录消失）。

    Returns:
        True = 源码目录已消失（懒加载不可用，需子进程兜底）；无法判定时 False。
    """
    module = sys.modules.get("orchd")
    src_file = getattr(module, "__file__", None) if module is not None else None
    if not src_file:
        return False
    try:
        return not Path(src_file).parent.is_dir()
    except OSError:
        return False


def _archive_ideas_via_subprocess(orchd_dir: Path) -> dict:
    """源码已回收时的归档兜底：在 canonical 主工作树起子进程执行 ideas-archive。

    子进程以 ``<canonical 主工作树>/.orchd/__main__.py`` 重新加载引擎源码，
    归档结果经 stdout JSON 返回（只取 archived / kept / skipped / error / commit
    契约字段，形状与进程内归档一致）。失败（入口缺失 / 非零退出 / 输出非 JSON /
    超时）一律返回 ``_archive_error_result`` 结构化降级——不阻断调用方、不静默。

    Args:
        orchd_dir: canonical 主工作树的 ``.orchd`` 目录。

    Returns:
        子进程归档结果或结构化降级结果。
    """
    import json
    import subprocess

    entry = orchd_dir / "__main__.py"
    if not entry.is_file():
        return _archive_error_result(
            "subprocess", FileNotFoundError(f"canonical 入口不存在: {entry}")
        )
    try:
        proc = subprocess.run(
            [sys.executable, str(entry), "ideas-archive"],
            cwd=str(orchd_dir.parent), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
        )
    except Exception as exc:
        return _archive_error_result("subprocess", exc)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        detail = tail[-1][:200] if tail else ""
        return _archive_error_result(
            "subprocess", RuntimeError(f"exit={proc.returncode}: {detail}")
        )
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        return _archive_error_result("subprocess_parse", exc)
    if not isinstance(payload, dict):
        return _archive_error_result(
            "subprocess_parse",
            ValueError(f"unexpected payload type: {type(payload).__name__}"),
        )
    result: dict[str, Any] = {
        "archived": payload.get("archived", []),
        "kept": payload.get("kept", 0),
    }
    for key in ("skipped", "error", "commit"):
        if key in payload:
            result[key] = payload[key]
    return result


def _preimport_archive_deps() -> None:
    """在可能触发 worktree 终态回收的动作之前预导入终态归档依赖（AC1）。

    ``orchd.ideas`` 是终态归档的全仓唯一懒加载点（``_maybe_archive_ideas``）。
    review code APPROVED 会终态回收任务 worktree、连带删除本进程的 orchd 源码
    目录；源码消失后再懒加载即 ``ModuleNotFoundError``（归档静默失效）。在回收
    动作之前调用本函数，令模块绑定进 ``sys.modules``，回收后归档调用不再触盘。

    依赖方向：``cli._util → orchd.ideas`` 已是 conventions.md 登记的既有边
    （ideas.py 非零内部依赖：ideas.py → errors.py / intake.py，模块级导入
    _atomic_write_text / _resolve_lock_orchd_dir），故预导入放在本模块而非调用方，
    避免新增未登记的依赖边。

    best-effort：预导入失败不抛异常——``_maybe_archive_ideas`` 的子进程兜底
    （``_archive_ideas_via_subprocess``）会接管。
    """
    try:
        import orchd.ideas  # noqa: F401
    except Exception:
        pass


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
    # 前置守卫：worktree 已终态回收 → orchd_dir（含 orchd/ 源码）消失。
    # task-worktree-recycle-cwd-selfheal（AC2）：不再直接跳过，而是回退到
    # canonical 主工作树的 .orchd 继续归档——IDEAS.md / IDEAS-archive.md
    # 位于主工作树，回收任务 worktree 不应连带阻断归档。仅当主工作树也
    # 无法定位时才显式报告 skip 原因（不静默）。
    if not orchd_dir.exists():
        orchd_dir = _resolve_canonical_orchd_dir(orchd_dir)
        if orchd_dir is None or not orchd_dir.exists():
            return {"archived": [], "kept": 0,
                    "skipped": "worktree_recycled_no_canonical"}
    # 源码层守卫（task-review-archive-selfdelete-fix AC2）：终态回收同时删除了
    # 本进程的 orchd 源码目录。此时若归档依赖未在回收前预导入（orchd.ideas 是
    # 全仓唯一懒加载点），进程内 import 必然 ModuleNotFoundError —— 改为在
    # canonical 主工作树另起子进程执行 ideas-archive（新进程重新加载源码）。
    # 已预导入（AC1，review 路径）时模块已绑定 sys.modules，进程内归档照常可用。
    if _orchd_source_recycled() and "orchd.ideas" not in sys.modules:
        return _archive_ideas_via_subprocess(orchd_dir)
    try:
        from orchd.ideas import archive_resolved_ideas
        from orchd.spec import load_master
        from orchd.worktree import resolve_master_path_from_dir

        # task-master-single-copy：container 任务 worktree 已抑制副本，本地无可读
        # _master.json 时回退 canonical 主工作树（唯一权威）；flat 布局 canonical ==
        # 本地，零回归。archived 判定基于权威 master，避免任务 worktree 因缺副本而漏归档。
        # （task-cli-master-rule-single-source：回退规则收敛到底座，no_master
        # 提前返回分支语义保留。import 须在 try 内：源码回收场景下懒加载失败应
        # 走下方降级（子进程兜底 / archive_error），不得外抛。）
        master_path = resolve_master_path_from_dir(orchd_dir)
        if not master_path.exists():
            return {"archived": [], "kept": 0, "skipped": "no_master"}
        if master_path.parent != orchd_dir:
            # 回退发生（解析落到 canonical 主工作树）→ 后续归档/提交按权威目录走
            orchd_dir = master_path.parent
        master = load_master(master_path)
    except Exception as exc:
        # 源码已回收时的懒加载失败（预导入不完整 / 依赖未绑定）→ 子进程兜底，
        # 不再降级为 archive_error。
        if _orchd_source_recycled():
            return _archive_ideas_via_subprocess(orchd_dir)
        # AC4：archive_error 必须带可审计原因，不再吞掉异常类型/消息。
        return _archive_error_result("load_master", exc)
    project_root = orchd_dir.parent
    try:
        result = archive_resolved_ideas(project_root, master)
    except Exception as exc:
        if _orchd_source_recycled():
            return _archive_ideas_via_subprocess(orchd_dir)
        return _archive_error_result("archive", exc)
    if result.get("archived"):
        result["commit"] = _commit_archived_ideas(project_root)
    return result


def _archive_error_result(stage: str, exc: BaseException) -> dict:
    """归档失败的结构化降级结果（永不阻断调用方，AC4 可审计）。"""
    return {
        "archived": [],
        "kept": 0,
        "skipped": "archive_error",
        "error": {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc)[:300],
        },
    }


def _commit_archived_ideas(project_root: Path) -> dict:
    """归档写盘后，在【canonical 主工作树】提交 IDEAS 文档（AC4 错位根治）。

    ``archive_resolved_ideas`` 经 ``resolve_workspace_root`` 把 IDEAS.md /
    IDEAS-archive.md 写入 **canonical 主工作树**（container 布局为 main/）。旧实现却用
    触发命令的 ``project_root``（review 时为任务 worktree）做分支判定、相对路径换算与
    提交，于是 ``(ws_root/...).relative_to(任务worktree)`` 跨根抛 ValueError：文件已写
    canonical 主工作树却未提交，上层只得到无原因的 archive_error，且主工作树残留未提交
    的摄入产物。此处让分支判定 / 相对路径 / ensure_committed 全部对齐文件真正落点
    canonical 根，写与提交同源，从根上消除 archive_error 与脏工作区。
    """
    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.ledger import resolve_workspace_root
    from orchd.worktree import resolve_canonical_project_root

    canon_root = Path(resolve_canonical_project_root(project_root))
    current_branch = get_current_branch(canon_root)
    default_branch = get_default_branch(canon_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        return {
            "performed": False,
            "reason": "not_on_main",
            "branch": current_branch,
        }
    # AC3（task-12-engine-path-abstraction）：工作区文档走统一工作区根 helper
    # （默认 .orchd/，兼容旧根路径）——ensure_committed 用相对 canon_root 的路径，
    # 工作区根为 .orchd/ 时路径前缀 .orchd/。
    ws_root = resolve_workspace_root(canon_root)

    def _rel(name: str) -> str:
        return str((ws_root / name).relative_to(canon_root))

    try:
        return ensure_committed(
            canon_root,
            [_rel("IDEAS.md"), _rel("IDEAS-archive.md")],
            "chore(ideas): 自动归档已完结条目",
        )
    except Exception as exc:
        # best-effort 静默降级，但必须可审计，且不让异常逃逸成命令 exit 1。
        return {
            "performed": False,
            "reason": "commit_error",
            "error": {"type": type(exc).__name__, "message": str(exc)[:300]},
        }
