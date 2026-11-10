"""CLI 注册入口（register），依赖 commands。"""

from __future__ import annotations

from orchd.contracts.events import (
    EVENT_AMEND,
    EVENT_CLAIMED,
    EVENT_DONE,
    EVENT_REVIEW_CLAIMED,
    EVENT_REVIEW_READY,
    EVENT_REVIEW_SUBMITTED,
)



from .commands import _cmd_amend, _cmd_force_status, _cmd_merge_ack, _cmd_restore, _cmd_retract


def register(sub) -> None:
    """注册 control 模块的子命令。"""
    # amend
    p = sub.add_parser("amend", help="增量更新 snapshot")
    p.add_argument("--master", default=".orchd/_master.json")
    p.add_argument("--register",
                   default=None,
                   metavar="PROPOSAL",
                   help=("显式注册通道：读提案 JSON（单任务对象或任务数组），经 canonical 全套校验"
                         "（结构/E025/质量/dry-run）后追加并一条龙注册；"
                         "恒以 canonical master 为写目标"))
    p.add_argument("--task", default=None, help="声明域补登：目标任务 id（需至少再带一个补丁字段）")
    p.add_argument("--files-to-edit",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="补登 files_to_edit（并集追加，只增不删；支持重复标志累加）")
    p.add_argument("--exempt-files",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="补登 exempt_files（并集追加，只增不删；支持重复标志累加）")
    p.add_argument("--remove-files-to-edit",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="remove_files_to_edit",
                   help="撤回 files_to_edit 声明（集合差；撤回不存在的路径为幂等 no-op 并留痕）")
    p.add_argument("--remove-exempt-files",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="remove_exempt_files",
                   help="撤回 exempt_files 声明（集合差；同上）")
    p.add_argument("--depends-on",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="depends_on",
                   help="补登 depends_on（并集追加，只增不删；重复 id 去重；"
                   "仅 pending 态放行；未知引用/依赖环由 spec 校验拒绝且不落盘）")
    p.add_argument("--verify-command", default=None, help="覆写 verify_command")
    p.add_argument(
        "--verify-command-file", default=None, metavar="PATH",
        help="从文件读取 verify_command（去尾换行；与 --verify-command 互斥；"
        "仅 --task 生效，--register 请写入提案 JSON；"
        "Windows 下推荐用文件避免 PowerShell 吃引号）",
    )
    p.add_argument("--reviewers",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="覆写 reviewers 名单（整体替换；空值即清空。不在 claimed 白名单"
                   "之外——reviewers 本就在 schema_policy.amendable_fields 内，claimed/终态"
                   "附加通道可补登）")
    p.add_argument("--files-to-read",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="files_to_read",
                   help="覆写 files_to_read（整体替换；条目按 priority=reference 登记，"
                   "需 must_read 请走 --register 提案注册；空值即清空）")
    p.add_argument("--acceptance-criteria",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="acceptance_criteria",
                   help="覆写 acceptance_criteria（整体替换，与 --reviewers / "
                   "--files-to-read 一致；空值拒绝；仅 pending 态放行，"
                   "claimed/done/in_review 一律 E007，终态走 --revise-terminal）")
    p.add_argument("--brief",
                   default=None,
                   help="覆写 brief 文本（单值整体替换，与 --acceptance-criteria 同一"
                   "状态矩阵：仅 pending 态放行，claimed/done/in_review 一律 E007，"
                   "终态配 --revise-terminal + --reason 走纯文本修订；空值拒绝；"
                   "E038 brief 口径对齐的合法出口）")
    p.add_argument("--name",
                   default=None,
                   help="覆写 name 文本（单值整体替换，状态矩阵与 --brief 一致；"
                   "空值拒绝）")
    p.add_argument(
        "--verify-timeout-seconds",
        default=None,
        dest="verify_timeout_seconds",
        help="覆写任务级 verify 预算（秒，正整数 1..600；缺省 120s 不变）。仅当"
        "verify_command 含 hook 密集用例、在引擎 Git Bash 通道下耗时被放大而必然"
        "打爆默认预算时使用；须在任务 notes/交付说明写明实测理由与数据")
    p.add_argument("--additional-sources",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="补登 additional_sources（并集追加，只增不删；支持重复标志累加；"
                   "逐条按与 source 相同口径校验：格式合法 + idea 条目存在且 pending）")
    p.add_argument("--revise-terminal",
                   default=None,
                   dest="revise_terminal",
                   help="终态任务规格文本修订：目标 task_id（须为 completed/cancelled，"
                   "需配 --reason；仅放行 acceptance_criteria / brief / name / "
                   "deliverables，写 AMEND 审计事件并同步快照）")
    p.add_argument("--reason",
                   default=None,
                   help="修订理由（--revise-terminal 必填、非空；写入 AMEND 审计事件；"
                   "同时作为 --allow-shared-write 的共享面放行理由）")
    p.add_argument("--refresh-surface",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="refresh_surface",
                   metavar="NAME",
                   help="声明 generated 面受控刷新（可重复；同面覆盖）。面名须在共享面"
                   "注册表且 mode=generated；path/generator 由注册表解析，不进写集；"
                   "须配 --reason <非空理由>；仅 pending/claimed 态放行")
    p.add_argument("--remove-refresh-surface",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="remove_refresh_surface",
                   metavar="NAME",
                   help="撤回 surface_refresh 声明（按面名；幂等 no-op）")
    p.add_argument("--allow-shared-write",
                   action="append",
                   default=None,
                   dest="allow_shared_write",
                   metavar="PATH",
                   help="显式放行共享面写集声明（重复使用可放行多条；须配 --reason "
                   "<非空理由>）：fragments 面视为串行例外，serial 面仅留痕；"
                   "generated 面不接受该逃生口")
    p.set_defaults(func=_cmd_amend)

    # retract
    p = sub.add_parser("retract", help="撤回事件")
    p.add_argument("--event",
                   required=False,
                   default=None,
                   help="事件 ID（精确撤回）；与 --task + --type 二选一")
    p.add_argument("--task",
                   required=False,
                   default=None,
                   help="任务 ID（配合 --type 自动定位最近匹配事件）")
    p.add_argument("--type",
                   required=False,
                   default=None,
                   dest="event_type",
                   choices=[
                       EVENT_CLAIMED, EVENT_DONE, EVENT_REVIEW_CLAIMED, EVENT_REVIEW_SUBMITTED,
                       EVENT_REVIEW_READY, EVENT_AMEND,
                   ],
                   help="事件类型（配合 --task 自动定位最近匹配事件）")
    p.add_argument("--reason", required=True)
    p.add_argument("--disposition", required=False, default=None,
                   choices=["abandon", "retry", "handoff"],
                   help="撤认处置：abandon（默认，触发300s认领冷却）/ retry（临时撤回后重试，"
                        "免冷却，AC 修正环用它）/ handoff（移交他人，免冷却）")
    p.set_defaults(func=_cmd_retract)

    # force-status
    p = sub.add_parser("force-status", help="强制设置任务状态")
    p.add_argument("--task", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--assignee")
    p.add_argument("--force",
                   action="store_true",
                   help="显式确认走逃生口（claimed→completed / cancelled→pending）")
    p.add_argument("--force-recycle",
                   action="store_true",
                   help="终态回收时显式丢弃未合并的 task/<id> 分支（git branch -D，"
                   "提交不可恢复）；缺省拒绝删除未合并分支并留痕 branch_delete_refused")
    p.add_argument("--discard-uncommitted",
                   action="store_true",
                   help="终态回收允许丢弃脏 worktree 未提交改动（必须与 --confirm "
                   "同时给出，缺一 E007 拒绝；缺省走五级降级：L2 救援提交保全数据"
                   "进 orchd/rescue ref → L3 目录隔离 → L4 bundle+patch → L5 原地保留）")
    p.add_argument("--confirm",
                   action="store_true",
                   help="--discard-uncommitted 的二次确认旗标（双旗标齐备才执行 "
                   "force 丢弃）")
    p.add_argument("--evidence-sha",
                   default=None,
                   help="复活已完成任务的 git 证据 commit SHA（仅 completed→pending 时需要）")
    p.set_defaults(func=_cmd_force_status)

    # merge-ack（task-merge-warning-ack）
    p = sub.add_parser("merge-ack", help="merge_warning 人工销账（merge-acks 确认清单）")
    p.add_argument("--task", required=True, help="已人工确认的 task_id")
    p.add_argument("--reason", required=True, help="确认原因（必填）")
    p.set_defaults(func=_cmd_merge_ack)

    # restore（task-restore-channel）
    p = sub.add_parser("restore", help="受管工作树还原：指定路径回到 HEAD，不碰历史")
    p.add_argument("--path", action="extend", nargs="+", required=True,
                   help="相对仓库根的文件路径（可多个；仅已跟踪且有 HEAD 版本的文件，"
                   "目录/仓库外/未跟踪新建一律拒绝）")
    p.set_defaults(func=_cmd_restore)
