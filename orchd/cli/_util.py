"""Orchd CLI 通用工具函数（无 commands 依赖，打破循环导入）。

从 orchd/cli/parser.py 迁出（task-split-cli-remove-legacy，3a 收尾）：
  - _command_name: 从已解析 args 反推当前命令名
  - _resolve_text_arg: 内联文本 / --xxx-file 二选一参数解析
  - _reject_container_root_cwd: 容器根拒绝纪律护栏（E036）
  - _find_orchd_dir: 查找 .orchd/ 目录
  - _flatten_nargs: 展平 nargs="*" 参数
  - _load_tasks: 加载 master 并返回 (tasks, orchd_dir, master)
  - _maybe_archive_ideas: 任务终态后触发 IDEAS 归档
  - _load_master_with_archived: 归档判据真源视图（活跃 ∪ 定义归档）的加载器
  - archive_ideas_before_master_archive: master-archive 前置止血（先归档 idea 再摘定义）
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
from types import SimpleNamespace
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
    归档结果经 stdout JSON 返回（只取 archived / kept / skipped / error / commit /
    deduped / deferred_comment_unbalanced / comment_hidden_candidate /
    comment_unclosed_at 契约字段，形状与进程内归档一致——进程内归档自
    ideas-dropped-exit 起会在真发生跳写时附 deduped、有条目被注释配对不变量挡下时附
    deferred_comment_unbalanced，task-idea-archive-diagnostics 起有条目被注释区间
    冻结时附 comment_hidden_candidate / comment_unclosed_at；这里不跟着转发就等于
    "兜底面的形状与主面不一致"，而那正是本函数 docstring 承诺的事）。失败（入口缺失 / 非零退出 /
    输出非 JSON / 超时）一律返回 ``_archive_error_result`` 结构化降级——不阻断
    调用方、不静默。

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
    return _forward_archive_payload(payload)


def _forward_archive_payload(payload: dict) -> dict:
    """把子进程归档 JSON 过滤成契约字段（进程内 / 子进程两条面同键）。

    task-idea-archive-diagnostics：抽成纯函数是为了让"两条转发面同键"这件事**可测**
    ——此前过滤是内联在子进程兜底里的，只有真起子进程才走得到，于是新键是否同步
    只能靠人读代码（本仓 deduped 键当年就漏过一次）。
    """
    result: dict[str, Any] = {
        "archived": payload.get("archived", []),
        "kept": payload.get("kept", 0),
    }
    for key in (
        "skipped",
        "error",
        "commit",
        "deduped",
        "deferred_comment_unbalanced",
        "comment_hidden_candidate",
        "comment_unclosed_at",
    ):
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


def _load_master_with_archived(master_path: Path) -> tuple[Any, dict | None]:
    """加载 master 并 best-effort 并入定义归档——归档判据的唯一真源视图。

    task-ideas-archive-archived-def-source：``orchd master-archive`` 会把终态任务
    定义摘出 ``master.tasks``，而 ``orchd/ideas.py::_group_ref_tasks`` 只读
    ``master.tasks`` ⇒ 仅活跃视图会让归档判据对「定义已被归档的 ref」**永久失明**
    （实测：摘除前 ``groups=368 / resolved=['x-demo']``，摘除后 ``groups=0 /
    resolved=[]``，同一份 IDEAS.md、同一批终态任务）。故判据真源取「活跃 ∪ 归档
    定义」：本函数返回的 master-like 视图，其 ``tasks`` 即该并集——``ideas.py``
    全程只消费 ``master.tasks``（duck typing），判据本体零改动。

    与 ``_load_tasks(with_archived=True)`` 的口径差异（**有意**）：后者面向展示面，
    归档清单在场但不可读时 fail-closed 抛 E007（拒绝返回不完整历史）；本处面向
    best-effort 归档面，归档不可读时**退回活跃定义**并返回降级 note，不得让归档件
    损坏阻断整个 IDEAS 归档。

    Args:
        master_path: 已解析的 master 文件路径（调用方负责 fallback 后的权威路径）。

    Returns:
        ``(master_like, degraded)``——``degraded`` 为 ``None`` 表示未降级（含「清单
        缺席 / 空骨架」与「成功并入」两种形态）；否则为可审计的降级原因字典
        （``stage`` / ``type`` / ``message``），此时 ``master_like`` 是活跃定义视图。
    """
    from orchd.definition_archive import (
        DefinitionArchiveError,
        merge_archived_definitions,
        read_archive_with_manifest,
    )
    from orchd.spec import load_master

    master = load_master(master_path)
    try:
        # 清单缺席 / 空骨架 → ([], None)，非错误，零降级。
        archived, _manifest = read_archive_with_manifest(master.raw, master_path)
    except DefinitionArchiveError as exc:
        return master, {
            "stage": "definition_archive",
            "type": type(exc).__name__,
            "message": str(exc)[:300],
        }
    if not archived:
        return master, None
    return (
        SimpleNamespace(tasks=merge_archived_definitions(list(master.tasks), archived)),
        None,
    )


def _unusable(stage: str, exc: BaseException) -> dict:
    """构造「判据不可信」的结构化原因（止血 fail-closed 与审计留痕共用）。"""
    return {"stage": stage, "type": type(exc).__name__, "message": str(exc)[:300]}


def _archivable_ideas_count(
    orchd_dir: Path, master_path: Path | None = None
) -> tuple[int, dict | None]:
    """只读预扫描：剩余可归档 IDEAS 条目数 + 「判据本身是否可信」。

    与 :func:`_maybe_archive_ideas` 共用同一份判据真源视图
    （:func:`_load_master_with_archived`）与同一份 ``Store.replay()`` 口径，故
    「止血判据」与「归档判据」不可能各说各话。

    **判据不可信不得折算成「0 条」**：``0`` 在本卡语义里等于「可以安全摘定义」，
    而归档件不可读 / IDEAS.md 不可读 / 账本不可重放这三类形态都会让 ``0`` 变成
    **假阴性**（条目随后随真源塌缩而永久失明，正是本卡要消灭的失败模式）。故一律
    以 ``unusable`` 上报，由调用方 fail-closed 拒绝；只有「确实一条都没有」才返回
    ``(0, None)``。

    Args:
        orchd_dir: ``.orchd`` 目录（不是项目根）。
        master_path: 显式 master 文件覆盖（``orchd master-archive --path`` 指向的文件）；
            为 ``None`` 时按 ``orchd_dir`` 解析（既有行为，零回归）。与
            :func:`_maybe_archive_ideas` 同一口径，避免 ``--path`` 自定义文件名时
            预扫描落到 ``<orchd_dir>/_master.json`` 而误判「零待归档」。

    Returns:
        ``(count, unusable)``——``unusable is None`` 时 ``count`` 可信；否则为结构化
        原因字典（``stage`` / ``type`` / ``message``），此时 ``count`` 恒为 0 且
        **不得**被当成「没有待归档条目」。
    """
    from orchd.ideas import find_archivable_entries, parse_ideas
    from orchd.ledger import Store, read_text_or_error, resolve_workspace_root
    from orchd.worktree import resolve_master_path_from_dir

    if master_path is None:
        try:
            master_path = resolve_master_path_from_dir(orchd_dir)
        except Exception as exc:
            return 0, _unusable("resolve_master_path", exc)
    if not master_path.exists():
        # 无 master：摘定义本身也会失败，此处无需拦截。
        return 0, None
    master, degraded = _load_master_with_archived(master_path)
    if degraded is not None:
        # 归档件不可读 ⇒ 合并视图退回活跃定义 ⇒ 判据可能漏计（真源已塌缩的 ref 看不见）。
        # run_master_archive 自身对此同样 fail-closed，此处只是提前拒绝，
        # 以免先改写并提交了 IDEAS 文档。
        return 0, degraded
    try:
        ideas_path = resolve_workspace_root(master_path.parent.parent) / "IDEAS.md"
    except Exception as exc:
        return 0, _unusable("resolve_workspace_root", exc)
    if not ideas_path.exists():
        # 无 IDEAS.md：旁路文档缺失不阻断定义瘦身（AC5），且确实无待归档条目。
        return 0, None
    try:
        text, err = read_text_or_error(ideas_path)
    except Exception as exc:
        return 0, _unusable("ideas_read", exc)
    if err is not None:
        # 存在但不可读 ⇒ 无法判定是否有待归档条目（不得当成「没有」）。
        return 0, {
            "stage": "ideas_read",
            "type": "read_error",
            "message": str(err)[:300],
        }
    try:
        # readonly：本函数承诺「只读预扫描」，不得因 sqlite 后端触发迁移等写副作用。
        task_status = {
            tid: ts.status
            for tid, ts in Store(master_path.parent, readonly=True).replay().items()
        }
    except Exception as exc:
        return 0, _unusable("ledger_replay", exc)
    entries = parse_ideas(text)
    return len(find_archivable_entries(master, entries, task_status, text=text)), None


def archive_ideas_before_master_archive(
    orchd_dir: Path, master_path: Path | None = None
) -> dict:
    """``orchd master-archive`` 的 IDEAS 止血前置（task-ideas-archive-archived-def-source）。

    定义归档把终态定义摘出 ``master.tasks``；若此刻 IDEAS.md 仍有可归档条目，
    摘除后归档判据对这些 ref **永久失明**（:func:`_load_master_with_archived` 的合并
    只能救「定义已在归档文件里」的存量，救不了「这一步刚刚发生」的顺序竞态）。
    故从**顺序**上保证「先归档 idea、再摘定义」：

    ① 先跑一次 :func:`_maybe_archive_ideas`（唯一归档入口，覆盖「从未触发过归档」
       的存量积压）——注意本步**可能已改写并提交 IDEAS.md / IDEAS-archive.md**，
       故整个止血不得晚于零落盘预检（调用方 :func:`_cmd_master_archive` 已保证）；
    ② 再只读预扫描剩余可归档条目（:func:`_archivable_ideas_count`，同一份判据真源视图）；
    ③ 判据不可信 ⇒ fail-closed 抛 ``E007`` ``ideas_archive_unusable``；仍非空 ⇒
       fail-closed 抛 ``E007`` ``ideas_archive_pending``。两者均附修复指引。

    Args:
        orchd_dir: ``.orchd`` 目录（不是项目根）。
        master_path: 显式 master 文件覆盖（``orchd master-archive --path`` 指向的文件）；
            为 ``None`` 时按 ``orchd_dir`` 解析（既有行为，零回归）。必须与实际摘除
            的 target 同源，否则止血判据与摘除对象分叉（MAJOR-1）。

    Returns:
        ``{"archived": n}``，并在存在诊断余量时附加 :func:`_maybe_archive_ideas` 的
        降级 / 跳过 / 延后类键（``archive_degraded`` / ``skipped`` / ``error`` /
        ``deduped`` / ``commit`` / ``deferred_comment_unbalanced`` /
        ``comment_hidden_candidate`` / ``comment_unclosed_at``）——否则「这条为什么
        没归档 / 为什么没提交」在命令面不可见，降级留痕只存在于直调层。正常计数
        （如 ``kept``）不透传，保持返回形态可预测。

    Raises:
        OrchdError: 判据不可信（归档件 / IDEAS.md / 账本不可读），或归档后仍有条目
            满足判据却未搬走（锁被占 / 文档读写失败 / 未预知原因）——两种情况下
            master 均**零改动**。
    """
    first = _maybe_archive_ideas(orchd_dir, master_path)
    remaining, unusable = _archivable_ideas_count(orchd_dir, master_path)
    if unusable is not None:
        raise OrchdError(
            ErrorCode.E007,
            f"ideas_archive_unusable: 归档判据不可信（{unusable['stage']}: "
            f"{unusable['type']}），拒绝摘除终态任务定义——无法判定是否仍有待归档条目时"
            "摘定义会让这些条目永久失明",
            [{
                "action": (
                    "先修复归档判据的数据源：确认 .orchd/_master-archive.json 与 "
                    "IDEAS.md 存在且可按 UTF-8 读出、账本可重放，再重跑 master-archive"
                ),
                "ideas_archive": first,
                "unusable": unusable,
            }],
        )
    if remaining:
        raise OrchdError(
            ErrorCode.E007,
            f"ideas_archive_pending: 仍有 {remaining} 条 IDEAS 条目满足归档判据却未搬走，"
            "拒绝摘除终态任务定义——摘除后归档判据将对相关 ref 永久失明",
            [{
                "action": (
                    "先修复 IDEAS 归档：检查 .intake.lock 是否被占、IDEAS.md 与 "
                    "IDEAS-archive.md 是否可读写（UTF-8），再重跑 master-archive"
                ),
                "ideas_archive": first,
                "remaining": remaining,
            }],
        )
    result: dict[str, Any] = {"archived": len(first.get("archived") or [])}
    # 诊断余量透传（白名单：只带「为什么没归档 / 降级 / 归档后提交结果」类键，不含
    # ``kept`` 等正常计数——保持返回形态可预测）；否则降级留痕只存在于直调层，
    # 命令面看不到。``deduped``（去重条数）与 ``commit``（自动提交结果，含
    # ``not_on_main`` 等 reason）同属「动作发生了但结果必须可见」的审计面。
    for key in (
        "archive_degraded",
        "skipped",
        "error",
        "deduped",
        "commit",
        "deferred_comment_unbalanced",
        "comment_hidden_candidate",
        "comment_unclosed_at",
    ):
        if key in first:
            result[key] = first[key]
    return result


def _maybe_archive_ideas(orchd_dir: Path, master_path: Path | None = None) -> dict:
    """best-effort：任务进入终态后触发 IDEAS 归档并自动提交。

    ``master_path`` 是**显式覆盖**（``orchd master-archive --path`` 指向的 master
    文件），为 ``None`` 时按 ``orchd_dir`` 解析（既有行为，零回归）。之所以需要它：
    ``--path`` 的语义是 master **文件**，其文件名不必然是 ``_master.json``；仅凭
    ``orchd_dir`` 重新解析会落到不存在的 ``<orchd_dir>/_master.json`` 上，从而把
    归档与止血整条静默旁路（MAJOR-1）。


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
        canonical_dir = _resolve_canonical_orchd_dir(orchd_dir)
        if canonical_dir is None or not canonical_dir.exists():
            return {"archived": [], "kept": 0,
                    "skipped": "worktree_recycled_no_canonical"}
        orchd_dir = canonical_dir
    # 源码层守卫（task-review-archive-selfdelete-fix AC2）：终态回收同时删除了
    # 本进程的 orchd 源码目录。此时若归档依赖未在回收前预导入（orchd.ideas 是
    # 全仓唯一懒加载点），进程内 import 必然 ModuleNotFoundError —— 改为在
    # canonical 主工作树另起子进程执行 ideas-archive（新进程重新加载源码）。
    # 已预导入（AC1，review 路径）时模块已绑定 sys.modules，进程内归档照常可用。
    if _orchd_source_recycled() and "orchd.ideas" not in sys.modules:
        return _archive_ideas_via_subprocess(orchd_dir)
    try:
        from orchd.ideas import archive_resolved_ideas
        from orchd.worktree import resolve_master_path_from_dir

        # task-master-single-copy：container 任务 worktree 已抑制副本，本地无可读
        # _master.json 时回退 canonical 主工作树（唯一权威）；flat 布局 canonical ==
        # 本地，零回归。archived 判定基于权威 master，避免任务 worktree 因缺副本而漏归档。
        # （task-cli-master-rule-single-source：回退规则收敛到底座，no_master
        # 提前返回分支语义保留。import 须在 try 内：源码回收场景下懒加载失败应
        # 走下方降级（子进程兜底 / archive_error），不得外抛。）
        # MAJOR-1：显式 master_path（--path 指向的文件，文件名不必然是 _master.json）
        # 优先；为 None 时才按 orchd_dir 解析（既有行为，零回归）。
        if master_path is None:
            master_path = resolve_master_path_from_dir(orchd_dir)
        if not master_path.exists():
            return {"archived": [], "kept": 0, "skipped": "no_master"}
        if master_path.parent != orchd_dir:
            # 回退发生（解析落到 canonical 主工作树）→ 后续归档/提交按权威目录走
            orchd_dir = master_path.parent
        # 判据真源 = 活跃 ∪ 归档定义（task-ideas-archive-archived-def-source）：
        # 定义归档摘走终态定义后，仅活跃视图会让归档判据对相关 ref 永久失明；
        # 归档件不可读时退回活跃定义并给 degraded 留痕（best-effort，不阻断）。
        master, degraded = _load_master_with_archived(master_path)
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
    if degraded:
        # 降级必须可审计：否则「这条本来该归档的为什么没归档」重新变成人肉排查。
        result["archive_degraded"] = degraded
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
            "chore(ideas): 自动归档已完结/已放弃条目",
        )
    except Exception as exc:
        # best-effort 静默降级，但必须可审计，且不让异常逃逸成命令 exit 1。
        return {
            "performed": False,
            "reason": "commit_error",
            "error": {"type": type(exc).__name__, "message": str(exc)[:300]},
        }
