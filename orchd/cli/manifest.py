"""CLI 命令元数据（task-docsgen-manifest）。

登记每个顶层命令的 ``name`` / ``args`` / ``read_only`` / ``requires_lock`` / ``purpose``；
parser 帮助、``orchd docs list`` 与文档生成共用同一份元数据，避免命令面出现第二张手写表。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = (
    "COMMANDS",
    "CommandMeta",
    "by_name",
    "command_names",
    "command_payload",
)


@dataclass(frozen=True)
class CommandMeta:
    """单个顶层命令的只读元数据。"""

    name: str
    args: tuple[str, ...]
    read_only: bool
    requires_lock: bool
    purpose: str


COMMANDS: tuple[CommandMeta, ...] = (
    CommandMeta("validate", (), True, False, "校验 _master.json"),
    CommandMeta("bootstrap", (), True, False, "输出分解套件 JSON"),
    CommandMeta("init", (), False, True, "初始化 .orchd/ 并生成 snapshot"),
    CommandMeta("amend", ("--task", "--files-to-edit", "--exempt-files"),
                False, True, "增量更新 snapshot / 声明域补登"),
    CommandMeta("retract", ("--event", "--reason"), False, True, "撤回事件"),
    CommandMeta("force-status", ("--task", "--status"), False, True, "强制设置任务状态"),
    CommandMeta("merge-ack", ("--task",), False, True, "merge_warning 人工销账"),
    CommandMeta("restore", ("--path",), False, True, "受管工作树还原"),
    CommandMeta("request", ("--sort", "--phase"), True, False, "获取下一个候选任务"),
    CommandMeta("claim", ("--task", "--type", "--confirm"), False, True, "认领任务"),
    CommandMeta("done", ("--task", "--changes"), False, True, "报告任务完成"),
    CommandMeta("review", ("--task", "--type", "--verdict"), False, True, "提交审查结果"),
    CommandMeta("pool", (), True, False, "列出就绪池"),
    CommandMeta("status", ("--text", "--audit-task"), True, False, "全局状态快照"),
    CommandMeta("show", ("<task-id>",), True, False, "单任务卡面详情"),
    CommandMeta("watchdog", ("--timeout", "--takeover", "--full"),
                False, False, "僵死任务巡检 / 接管"),
    CommandMeta("doctor", ("--path", "--fix", "--dry-run", "--backup-dir"),
                False, False, "git 仓库完整性检测 / 残留清理"),
    CommandMeta("milestone-check", ("<milestone>", "--master"), True, False, "里程碑判据门禁"),
    CommandMeta("layout-migrate", (), False, True, "flat → container 布局迁移"),
    CommandMeta("full-regression", (), False, True, "全量 pytest 并记录基线"),
    CommandMeta("ci-record", ("--path",), False, True, "记录远端 CI 结论"),
    CommandMeta("intake", (), False, True, "提交摄入产物"),
    CommandMeta("roadmap-land", ("<version>",), False, True, "ROADMAP 规划落地为 IDEAS"),
    CommandMeta("context-digest", (), True, False, "必读面内容哈希"),
    CommandMeta("ledger-compact", ("--dry-run",), False, True, "终态事件归档"),
    CommandMeta("migrate", ("--dry-run",), False, True, "schema 版本前移"),
    CommandMeta("master-archive", (), False, True, "终态任务定义归档"),
    CommandMeta("storage-rollback", (), False, True, "sqlite→file 回滚"),
    CommandMeta("storage-recutover", ("--dry-run",), False, True, "回滚残留并账"),
    CommandMeta("git", ("merge", "commit"), False, True, "git 写操作代理"),
    CommandMeta("ideas-archive", (), False, True, "归档已完结 IDEAS"),
    CommandMeta("idea", ("append", "close", "confirm", "propose"),
                False, True, "灵感写入门禁"),
    CommandMeta("ideas", ("list",), True, False, "IDEAS 台账只读盘点"),
    CommandMeta("session", ("start", "current", "end"), False, True, "会话生命周期"),
    CommandMeta("lesson", ("add", "archive", "list", "report", "resolve",
                           "review", "show", "stage"),
                False, True, "经验回灌"),
    CommandMeta("sync", ("--push", "--pull", "--compact", "--status", "--remote"),
                False, True, "账本跨设备同步"),
    CommandMeta("line-sync", (), False, True, "跨线回移通道"),
    CommandMeta("check", (), True, False, "静态门禁 ruff + mypy"),
    CommandMeta("release", ("plan", "run", "status"), False, True, "发版受管通道"),
    CommandMeta("guards", ("list", "check", "init"), False, False, "宿主 guard 配置与检查"),
    CommandMeta("deps", ("init", "check", "sync"), False, False, "依赖图真源"),
    CommandMeta("docs", ("list", "check", "write", "sync"), False, True, "契约文档产物清单与生成"),
    CommandMeta(
        "surfaces",
        ("list", "validate", "check", "assemble"),
        False,
        True,
        "落地面登记与汇总：surfaces list / validate / check / assemble",
    ),
)


def command_names() -> tuple[str, ...]:
    """全部顶层命令名（声明顺序）。"""
    return tuple(item.name for item in COMMANDS)


def by_name(name: str) -> CommandMeta | None:
    """按命令名取元数据。"""
    return next((item for item in COMMANDS if item.name == name), None)


def command_payload() -> list[dict[str, object]]:
    """``docs list`` 等文档面使用的纯 dict 视图。"""
    return [
        {
            "name": item.name,
            "args": list(item.args),
            "read_only": item.read_only,
            "requires_lock": item.requires_lock,
            "purpose": item.purpose,
        }
        for item in COMMANDS
    ]
