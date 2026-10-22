"""misc.commands —— 自 ``orchd/cli/commands/misc.py`` 拆出。

其余零散运维命令（intake / roadmap-land / context-digest / ledger-compact / master-archive / git）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

from pathlib import Path

from orchd.cli._util import _find_orchd_dir

def _cmd_intake(args) -> dict:
    """提交摄入产物（IDEAS.md / ROADMAP.md）并校验条目状态（intake-commit-enforcement）。

    CLI 参数: 无（项目根由 .orchd/ 定位）。
    返回: 提交结果字典（committed / commit / 可选 status_warnings / commit_warning）。
    """
    from orchd.intake import intake_commit

    orchd_dir = _find_orchd_dir()
    return intake_commit(orchd_dir.parent)


def _cmd_roadmap_land(args) -> dict:
    """为 ROADMAP 规划章节生成 IDEAS study 落地条目（intake-dual-path）。

    CLI 参数: args.version — 规划章节版本（如 1.3）。
    返回: 落地结果字典（landed / version / section_id / commit）。
    """
    from orchd.intake import roadmap_land

    orchd_dir = _find_orchd_dir()
    return roadmap_land(orchd_dir.parent, args.version)


def _cmd_context_digest(args) -> dict:
    """输出必读面内容哈希与字节数（task-context-digest-command，只读 T0 命令）。

    CLI 参数: 无（项目根由 .orchd/ 定位）。
    返回: 必读面 digest 字典（files[] 每项 path/sha256/bytes/exists + 聚合
    digest + root；无时间戳/pid/mtime 等易变字段，同一工作区重复调用字节稳定）。
    """
    from orchd.guide import context_digest

    orchd_dir = _find_orchd_dir()
    return context_digest(orchd_dir.parent)


def _cmd_ledger_compact(args) -> dict:
    """终态任务事件归档压实（task-ledger-archive-compact）。

    CLI 参数: args.dry_run（只算搬运计划，不写文件）。
    返回: compact 摘要（moved_events / archived_tasks / active_before-after /
    checkpoint_lines / journal_recovered / dry_run）——后者为 c2（双文件归档）
    决策的增长曲线输入。
    """
    from orchd.ledger import open_store

    orchd_dir = _find_orchd_dir()
    store = open_store(orchd_dir)
    return store.compact_archive(dry_run=bool(getattr(args, "dry_run", False)))


#: 首次建立归档清单时写入的归档相对路径（相对项目根）。**这是该默认值的唯一
#: 落点**：orchd/definition_archive.py 刻意不含任何归档文件名字面量，归档位置
#: 一律由 master 的 definition_archive.path 解析（单一真源）。
MASTER_ARCHIVE_DEFAULT_RELPATH = ".orchd/_master-archive.json"


def _cmd_master_archive(args) -> dict:
    """终态任务定义归档（task-master-archive-write-channel）。

    CLI 参数: args.path（master 文件，默认 canonical 主工作树 .orchd/_master.json）；
    args.archive_path（首次建立归档清单时的相对路径，之后一律以清单为准）；
    args.dry_run（只算计划，零落盘）。

    返回: 执行摘要（to_archive / remaining / archive_count_after / manifest /
    verified 等）。低版本 master 与账本不可重建两种情况均 fail-closed 拒绝
    （E007，带修复指引）——把不可用静默当「无终态任务」会让全部终态定义留在
    master 且清单写成空，是本通道最危险的失败模式。
    """
    from orchd.cli._util import archive_ideas_before_master_archive
    from orchd.definition_archive import run_master_archive
    from orchd.worktree import resolve_master_path_from_dir

    orchd_dir = _find_orchd_dir()
    default = resolve_master_path_from_dir(orchd_dir)
    target = Path(getattr(args, "path", None) or default)
    relpath = getattr(args, "archive_path", None) or MASTER_ARCHIVE_DEFAULT_RELPATH
    dry_run = bool(getattr(args, "dry_run", False))
    # IDEAS 止血（task-ideas-archive-archived-def-source）：摘除终态任务定义会让
    # 归档判据的真源（活跃定义）塌缩 ⇒ 摘除前先归档 IDEAS 可归档条目；判据不可信或
    # 仍有积压则 fail-closed 拒绝摘除（否则那些条目永久失明且零诊断）。--dry-run
    # 语义是「只算计划、零落盘」，故不触发止血。
    # 顺序要点：止血必须**晚于**零落盘预检——否则命令因低版本 master / 账本不可重建
    # 等前置失败而报错时，IDEAS 文档已被改写并提交，破坏「失败 ⇒ 零改动」契约。
    ideas_archive = None
    if not dry_run:
        run_master_archive(target, archive_relpath=relpath, dry_run=True)
        # 止血对象跟随 --path 指向的**文件**（target）与项目（target.parent），与摘除
        # 同一目标：--path 的语义是 master 文件、文件名不必然是 _master.json，若只传
        # 目录而让止血内部重新解析，会落到不存在的 <dir>/_master.json 上并整条静默
        # 旁路（MAJOR-1）。故显式透传 master_path=target。
        ideas_archive = archive_ideas_before_master_archive(
            target.parent, master_path=target
        )
    result = run_master_archive(target, archive_relpath=relpath, dry_run=dry_run)
    if ideas_archive is not None:
        result["ideas_archive"] = ideas_archive
    return result


def _cmd_git(args) -> dict:
    """git 写操作代理（task-git-write-proxy）：红线 #1/#2 引擎化拦截。
    返回: 代理载荷——只读子命令透传执行；任务分支 ``commit`` 放行；无 git 模式
          降级（``commit`` 推进 committed 快照，其余 no-op）。
    异常: 写操作被拒 → E007（红线 #1/#2）；``commit`` 不在任务分支 → E018。
    """
    from orchd.gitops.proxy import run_git_proxy
    from orchd.ledger import resolve_agent_id

    orchd_dir = _find_orchd_dir()
    return run_git_proxy(
        list(getattr(args, "git_args", None) or []),
        project_root=orchd_dir.parent,
        orchd_dir=orchd_dir,
        agent_id=resolve_agent_id(orchd_dir),
    )
