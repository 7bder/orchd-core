"""misc.registration —— 自 ``orchd/cli/commands/misc.py`` 拆出。

argparse 命令注册（register）——本包唯一对外装配点。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import argparse

from .commands import (
    MASTER_ARCHIVE_DEFAULT_RELPATH,
    _cmd_context_digest,
    _cmd_git,
    _cmd_intake,
    _cmd_ledger_compact,
    _cmd_master_archive,
    _cmd_roadmap_land,
)
from .evidence import _cmd_ci_record, _cmd_full_regression
from .storage import (
    _cmd_layout_migrate,
    _cmd_migrate,
    _cmd_storage_recutover,
    _cmd_storage_rollback,
)

def register(sub) -> None:
    """注册 misc 模块的子命令。"""
    # layout-migrate（task-14-worktree-layout）：flat → container 迁移
    p = sub.add_parser(
        "layout-migrate", help="flat → container 布局迁移（保留 git 历史、可回滚）"
    )
    p.add_argument("--path", default=".", help="flat 主工作树根，默认当前目录")
    p.set_defaults(func=_cmd_layout_migrate)

    # full-regression（task-full-regression-gate-r2）：全量回归并记录 last_pass_commit
    p = sub.add_parser(
        "full-regression",
        help="跑全量 pytest 并通过后写 .orchd/_full_regression.json（last_pass_commit）；失败不写通过标记",
    )
    p.add_argument("--path", default=None, help="项目根目录，默认当前目录")
    p.set_defaults(func=_cmd_full_regression)

    # ci-record（卡 A / task-ci-evidence-authority）：把当前 HEAD 的远端 CI 结论写入
    # .orchd/_full_regression.json，供里程碑 F1 的三重判定离线复算（唯一写入口）。
    p = sub.add_parser(
        "ci-record",
        help="把当前 HEAD 的远端 CI 结论写入 .orchd/_full_regression.json（F1 远端证据源）",
    )
    p.add_argument("--path", default=None, help="项目根目录，默认当前目录")
    p.set_defaults(func=_cmd_ci_record)

    # intake（2026-08-14 intake-commit-enforcement）
    p = sub.add_parser("intake", help="提交摄入产物（IDEAS.md + 宿主根 ROADMAP.md + .orchd/_master.json）并校验状态合法性；被 gitignore 忽略的路径由 commit 层剔除")
    p.set_defaults(func=_cmd_intake)

    # roadmap-land（2026-08-15 intake-dual-path）：ROADMAP 规划章节 → IDEAS study 落地
    p = sub.add_parser("roadmap-land", help="为 ROADMAP 规划章节生成 IDEAS study 落地条目")
    p.add_argument("version", help="规划章节版本（如 1.3，匹配 ROADMAP ## 版本 章节头）")
    p.set_defaults(func=_cmd_roadmap_land)

    # context-digest（task-context-digest-command）：必读面内容哈希只读命令
    p = sub.add_parser("context-digest", help="输出必读面内容哈希与字节数（未变即跳过重读的机械判据）")
    p.set_defaults(func=_cmd_context_digest)

    # ledger-compact（task-ledger-archive-compact）：终态任务事件归档压实
    p = sub.add_parser(
        "ledger-compact",
        help="终态任务事件归档至 _ledger.archive.jsonl 并重写 checkpoint（幂等；--dry-run 只算计划）",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="只计算搬运计划，不写任何文件")
    p.set_defaults(func=_cmd_ledger_compact)

    # migrate（W1-③）：schema 版本前移（改版本号 + v3 恒建归档空骨架；--dry-run 只算计划）
    p = sub.add_parser(
        "migrate",
        help="schema 版本前移至最新：改 schema_version 并恒建 definition_archive 空骨架（v3 破坏性变更），账本零触碰；--dry-run 只算计划",
    )
    p.add_argument("--path", default=None, help="master 文件，默认 canonical 主工作树 .orchd/_master.json")
    p.add_argument("--dry-run", action="store_true", help="只计算迁移计划，不写文件")
    p.add_argument("--retreat", action="store_true", help="回退到最早版本（回滚通道）")
    p.set_defaults(func=_cmd_migrate)

    # master-archive（task-master-archive-write-channel）：终态任务**定义**归档
    # 写入通道，与 ledger-compact（终态任务**事件**归档）对称。
    p = sub.add_parser(
        "master-archive",
        help="终态任务定义归档：把终态任务定义摘入归档文件并更新 definition_archive 清单（幂等 + 原子 + 自复验）",
    )
    p.add_argument("--path", default=None,
                   help="master 文件，默认 canonical 主工作树 .orchd/_master.json")
    p.add_argument("--archive-path", default=None,
                   help="首次建立归档清单时使用的归档相对路径（相对项目根），"
                        f"默认 {MASTER_ARCHIVE_DEFAULT_RELPATH}；"
                        "清单已存在时一律以清单 path 为准")
    p.add_argument("--dry-run", action="store_true",
                   help="只计算搬运计划，不写任何文件")
    p.set_defaults(func=_cmd_master_archive)

    # storage-rollback（task-storage-rollback-cmd）：sqlite→file 回滚导出+复验
    p = sub.add_parser(
        "storage-rollback",
        help="sqlite→file 回滚：库内事件原子导出回 _ledger.jsonl 并经文件后端复验一致",
    )
    p.set_defaults(func=_cmd_storage_rollback)

    # storage-recutover（task-storage-recutover-merge-channel）：回滚残留并账入库
    p = sub.add_parser(
        "storage-recutover",
        help="回滚残留并账：活名 file 增量按 event_id 差集并入 sqlite + 原子替换 + 复验（--dry-run 只算计划）",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="只计算差集与并账计划，不写库、不改名活名文件")
    p.set_defaults(func=_cmd_storage_recutover)

    # git 代理（task-git-write-proxy）：红线 #1/#2 引擎化拦截
    p = sub.add_parser(
        "git",
        help=(
            "git 写操作代理：只读透传 / 任务分支 commit 放行 / 其余写操作按红线 #1/#2 "
            "结构化拒绝（无 git 模式降级 no-op）"
        ),
    )
    p.add_argument(
        "git_args",
        nargs=argparse.REMAINDER,
        help="git 参数原样透传，如 status / log --oneline -5 / commit -m 'msg'",
    )
    p.set_defaults(func=_cmd_git)
