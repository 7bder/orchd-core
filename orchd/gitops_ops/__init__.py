"""Orchd git 写子域 + 任务生命周期共享辅助 — 聚合入口（W6-C6：退化为纯重导出）。

来源：``orchd/gitops_ops.py``（1220 行，W6-C6 纯搬移）。

将 onboard.py 中与 git 写入相关的 best-effort 操作，以及 L1 分支守卫 /
L2 session 锁 / 事件构造等在 onboarding 与 review 路径间共享的辅助函数，
统一外置到本包，保持 onboard.py 只保留生命周期主干。与 orchd.gitops 区别：

- orchd.gitops：git 基础设施（工作区检测、hook、session lock、ensure_committed
  等），偏低级、可被任意模块复用。
- orchd.gitops_ops（本包）：任务生命周期特定的 git 写动作（task/{id} 分支、
  merge 前置化、冲突化解）与共享辅助（guard_write_command / make_event 等），
  偏流程层，仅被 onboarding/review 路径调用。

依赖方向：本包不导入 onboard.py / review.py，二者各自单向依赖本包，
杜绝循环依赖。

task-line-trunk：任务分支 fork / merge 目标 / 对账基线按**当前线**解析
（``orchd.line_ctx``：canonical master 的 ``project.lines`` + ``ORCHD_LINE``）；
单线（未配置多线）恒为 ``main`` / ``task/{id}``，与 v1.5.0 逐字一致（opt-in 零回归）。

**可变模块状态不重导出（W6-C6 实踩）**：``events._LAST_TIMESTAMP`` 与
``gitver._merge_tree_supported`` 是被 ``global`` 复写的可变状态。把它们列进本入口会
得到**导入瞬间的值拷贝**（恒 ``None``），而旧单文件里它们是同一个活变量 ⇒ 外部读本入口
会拿到永久陈旧值。这两个名字因此**只保留在各自子域**（``orchd.gitops_ops.events`` /
``orchd.gitops_ops.gitver``），全仓无外部消费者，判据见
tests/test_gitops_ops_pkg.py::TestMutableStateNotReexported。

**本入口不再持有任何实现**：仅重导出 ``events`` / ``output`` / ``branch`` /
``union`` / ``gitver`` / ``reconcile`` 六域的 30 个顶层符号，外加 7 个对
``orchd.gitops`` 的旧路径兼容 re-export（历史导入面，非本包实现）。
外部 ``from orchd.gitops_ops import <名>`` 取到的仍是子模块同一对象；
补丁面/打点键随落点迁移到实际消费者所在子模块（见 tests/test_gitops_ops_pkg.py
的行为级机判）。
"""

from orchd.gitops import (
    check_workspace_state,
    checkout_default_strict,
    ensure_session_lock,
    guard_write_command,
    is_task_worktree,
    main_worktree_root,
    unmerged_paths,
)

from .branch import (
    _abort_merge,
    _classify_merge_failure,
    _clean_stale_index_lock,
    _git_error_summary,
    try_delete_task_branch,
    try_ff_merge_to_main,
    try_git_branch,
    try_git_merge,
)
from .events import (
    _TS_LOCK,
    make_event,
    now_iso,
)
from .gitver import (
    _MERGE_TREE_MIN_VERSION,
    _git_version_tuple,
    _split_nonempty_lines,
    _split_nul,
    merge_tree_supported,
)
from .output import (
    _extract_json_objects,
    _extract_validate_summary,
    _truncate_by_lines,
    decode_subprocess_output,
    verify_output_summary,
)
from .reconcile import (
    reconcile_with_main,
    resolve_task_worktree,
)
from .union import (
    _is_tests_only_file,
    _read_task_verify_command,
    _run_union_verify,
    _try_union_merge_conflicts,
    try_auto_resolve_conflict,
)

__all__ = [
    "check_workspace_state",
    "checkout_default_strict",
    "ensure_session_lock",
    "guard_write_command",
    "is_task_worktree",
    "main_worktree_root",
    "unmerged_paths",
    "_abort_merge",
    "_classify_merge_failure",
    "_clean_stale_index_lock",
    "_git_error_summary",
    "try_delete_task_branch",
    "try_ff_merge_to_main",
    "try_git_branch",
    "try_git_merge",
    "_TS_LOCK",
    "make_event",
    "now_iso",
    "_MERGE_TREE_MIN_VERSION",
    "_git_version_tuple",
    "_split_nonempty_lines",
    "_split_nul",
    "merge_tree_supported",
    "_extract_json_objects",
    "_extract_validate_summary",
    "_truncate_by_lines",
    "decode_subprocess_output",
    "verify_output_summary",
    "reconcile_with_main",
    "resolve_task_worktree",
    "_is_tests_only_file",
    "_read_task_verify_command",
    "_run_union_verify",
    "_try_union_merge_conflicts",
    "try_auto_resolve_conflict",
]
