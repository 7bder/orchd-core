"""cli 通用工具域（W6-C8）：cwd/命令名/master 加载（paths）、IDEAS 归档主流程（archive）、
源码回收后的子进程兜底（fallback）。

域：archive（W6-C8 从 orchd/cli/_util.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchd.errors import ErrorCode, OrchdError

from .fallback import (
    _archive_error_result,
    _archive_ideas_via_subprocess,
    _orchd_source_recycled,
)
from .paths import _resolve_canonical_orchd_dir



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
