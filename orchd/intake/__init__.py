"""intake —— 聚合入口（W6-C7 自 ``orchd/intake.py`` 拆出，纯重导出）。

原模块 docstring 如下（信息不丢）：

  Orchd 摄入产物提交引擎（叶子模块，零 orchd 内部依赖）。

  intake-commit-enforcement（2026-08-14）：
  把「摄入产物（IDEAS.md / ROADMAP.md）的提交」从约定层下沉为引擎命令
  ``orchd intake``——前置守卫（main + 非摄入产物干净）+ IDEAS 条目状态校验
  + 强制提交。与 amend 互补：amend 负责「注册 + 提交」，intake 负责「只改
  IDEAS/ROADMAP、暂不注册任务」场景的提交。

  安全约束：
  - 只提交 IDEAS.md / ROADMAP.md（经 ``resolve_workspace_root`` 定位，兼容
    根布局与发布态 .orchd 布局），不触碰 _master.json（注册归 amend）；
  - 不 push、不新增 ledger 事件、不改事件格式（与 gitops best-effort 语义一致，
    不触碰 §9.1 状态机——不做历史规划的 proposal/confirm 状态机）。

  依赖方向：intake 包 → 标准库（pathlib）+ 惰性导入 gitops / ledger（避免循环）。

实现按域落在 5 个子模块：``parse.py`` / ``guard.py`` / ``commit.py`` /
``roadmap.py`` / ``idea.py``。
本入口只重导出，不含任何实现。
"""

from __future__ import annotations

from .parse import (
    _atomic_write_text,
    _VALID_IDEA_STATUSES,
    _TITLE_ID_RE,
    _parse_title_id,
    _parse_idea_statuses,
    check_idea_statuses,
    _find_idea_entry,
    _list_idea_headers,
)
from .guard import (
    _intake_guard,
    _resolve_lock_orchd_dir,
    _INTAKE_PRODUCT_FILES,
    _DIRTY_AFTER_INIT_HINT,
)
from .commit import intake_commit
from .roadmap import _roadmap_land_entry, _entry_id_exists, roadmap_land
from .idea import idea_propose, _idea_transition, idea_confirm, idea_drop

__all__ = [
    "_atomic_write_text",
    "_VALID_IDEA_STATUSES",
    "_TITLE_ID_RE",
    "_parse_title_id",
    "_parse_idea_statuses",
    "check_idea_statuses",
    "_find_idea_entry",
    "_list_idea_headers",
    "_intake_guard",
    "_resolve_lock_orchd_dir",
    "_INTAKE_PRODUCT_FILES",
    "_DIRTY_AFTER_INIT_HINT",
    "intake_commit",
    "_roadmap_land_entry",
    "_entry_id_exists",
    "roadmap_land",
    "idea_propose",
    "_idea_transition",
    "idea_confirm",
    "idea_drop",
]
