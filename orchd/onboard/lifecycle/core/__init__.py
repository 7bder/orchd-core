"""Orchd 任务生命周期管理 - lifecycle/core 域聚合入口（W6-C6：退化为纯重导出）。

迁移自 orchd/onboard.py（task-split-onboard-lifecycle-core）；
W6-C6 再按职责切为七域（纯搬移）：

- ``orchestrator``：done 主流程编排（``done`` / ``_done_impl``）
- ``stages``：S-A2 阶段 1/4/5（预校验、自动提交 + 完整性门禁）
- ``verify``：verify_command 执行与 E014 构造（含审查侧 rerun）
- ``baseline``：基线 ref 解析与「基线既有」谓词
- ``reconcile``：done 前置两闸口（基线新鲜度 + 与 main 对账）
- ``entry_guard``：共享入口 verify 覆盖门禁（E039）
- ``finish``：锁内落账 DONE/REVIEW_READY 与 done 收尾组装

**本入口不再持有任何实现**：仅重导出七域 25 个顶层符号（26 个迁移符号中
``_FULL_REGRESSION_TIMEOUT`` 不重导出——它在 core 域内零消费者，值 300 与引擎真源
``lifecycle/regression.py`` 的 600 已分叉，重导出只会把错误预算固化成公共口径）；
另保留 2 个历史模块级别名（``_decode_subprocess_output`` / ``_verify_output_summary``）
指向 ``verify`` 域同名对象，维持旧导入路径可解析。外部
``from orchd.onboard.lifecycle.core import <名>`` 取到的仍是子模块同一对象；
补丁面/打点键随落点迁移到实际消费者所在子模块（见
tests/test_lifecycle_core_pkg.py 的行为级机判）。
"""

from .baseline import (
    _BASELINE_REF_CANDIDATES,
    _baseline_test_predicate,
    _nogit_manifest_probe,
    _resolve_baseline_ref,
    _uncommitted_changes,
)
from .entry_guard import _guard_shared_entry_coverage
from .finish import (
    _assemble_done_result,
    _done_lessons_hook,
    _write_done_event,
)
from .orchestrator import (
    _done_impl,
    done,
)
from .reconcile import (
    MANAGED_MERGE_MAIN_CMD,
    _check_base_freshness,
    _probe_task_branch,
    _reconcile_before_verify,
)
from .stages import (
    _commit_and_verify_integrity,
    _done_auto_commit,
    _done_precheck,
)
from .verify import (
    _VERIFY_TIMEOUT,
    _current_head,
    _decode_subprocess_output,
    _run_verify,
    _verify_fail_error,
    _verify_failure_early_done,
    _verify_output_summary,
    _verify_timeout_error,
    rerun_verify_at_head,
)

__all__ = [
    "_BASELINE_REF_CANDIDATES",
    "_baseline_test_predicate",
    "_nogit_manifest_probe",
    "_resolve_baseline_ref",
    "_uncommitted_changes",
    "_guard_shared_entry_coverage",
    "_assemble_done_result",
    "_done_lessons_hook",
    "_write_done_event",
    "_done_impl",
    "done",
    "MANAGED_MERGE_MAIN_CMD",
    "_check_base_freshness",
    "_probe_task_branch",
    "_reconcile_before_verify",
    "_commit_and_verify_integrity",
    "_done_auto_commit",
    "_done_precheck",
    "_VERIFY_TIMEOUT",
    "_current_head",
    "_decode_subprocess_output",
    "_run_verify",
    "_verify_fail_error",
    "_verify_failure_early_done",
    "_verify_output_summary",
    "_verify_timeout_error",
    "rerun_verify_at_head",
]
