"""misc —— 聚合入口（W6-C7 自 ``orchd/cli/commands/misc.py`` 拆出，纯重导出）。

原模块 docstring 如下（信息不丢）：

  Orchd CLI 路由：misc 命令 handler。

  迁移自 orchd/cli.py（task-split-cli-cmds-query-init-misc）：
    - _cmd_full_regression: full-regression 命令（全量回归并记录 last_pass_commit）
    - _cmd_layout_migrate: layout-migrate 命令（flat → container 布局迁移）
    - _cmd_intake: intake 命令（提交摄入产物并校验状态合法性）
    - _cmd_roadmap_land: roadmap-land 命令（ROADMAP 规划章节 → IDEAS study 落地）
    - _cmd_migrate: migrate 命令（W1-③ schema 版本前移；v3 起恒建 definition_archive 空骨架）

  3a 阶段说明（历史叙述，W6-C7 加注）：本模块是 misc 域的目标落点。当时 cli.py（legacy）
  仍保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），本模块随 3a 收尾
  （删除 orchd/cli.py）后接管。函数体逐字一致（AST 校验 IDENTICAL，仅允许 import 行变化），
  零逻辑变化。**该过渡已完成**：`orchd/cli.py` 早已删除、现为 `orchd/cli/` 包，本包是唯一
  实现；上述「兼容层透传」描述仅存史料价值。

实现按域落在 4 个子模块：``evidence.py`` / ``storage.py`` / ``commands.py`` / ``registration.py``。
本入口只重导出，不含任何实现。
"""

from __future__ import annotations

from .evidence import (
    _commit_regression_evidence,
    _EVIDENCE_NONCOMMIT_OK,
    _warn_evidence_not_committed,
    _cmd_full_regression,
    _cmd_ci_record,
)
from .storage import (
    _RECUTOVER_BACKUP_SUFFIX,
    _recutover_backup_live_files,
    _cmd_storage_recutover,
    _cmd_storage_rollback,
    _cmd_migrate,
    _cmd_layout_migrate,
)
from .commands import (
    _cmd_intake,
    _cmd_roadmap_land,
    _cmd_context_digest,
    _cmd_ledger_compact,
    MASTER_ARCHIVE_DEFAULT_RELPATH,
    _cmd_master_archive,
    _cmd_git,
)
from .registration import register

__all__ = [
    "_commit_regression_evidence",
    "_EVIDENCE_NONCOMMIT_OK",
    "_warn_evidence_not_committed",
    "_cmd_full_regression",
    "_cmd_ci_record",
    "_RECUTOVER_BACKUP_SUFFIX",
    "_recutover_backup_live_files",
    "_cmd_storage_recutover",
    "_cmd_storage_rollback",
    "_cmd_migrate",
    "_cmd_layout_migrate",
    "_cmd_intake",
    "_cmd_roadmap_land",
    "_cmd_context_digest",
    "_cmd_ledger_compact",
    "MASTER_ARCHIVE_DEFAULT_RELPATH",
    "_cmd_master_archive",
    "_cmd_git",
    "register",
]
