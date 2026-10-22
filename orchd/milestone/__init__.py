"""orchd.milestone — 里程碑判据门禁（**单一真源**）聚合入口。

W8-9（task-split2-w8-9-milestone-pkg）：旧 orchd/milestone.py（742 行）纯搬移拆为
本包七域。本 __init__.py 纯重导出（零顶层定义，仅有 __all__），外部
``from orchd.milestone import X`` / ``orchd.milestone.X`` 零改动。

判据清单的**单一真源** = 本包 ``table.py`` 的 ``MILESTONES``（M1 ``local-kernel-freeze`` /
M2 ``line-split-ready``）；``ROADMAP.md`` 的「## 里程碑」节只声明组名与出口版本，不复述
明细（避免文档与实现双写必漂移）。判据变更只改 ``table.py`` + 对应 ``m1.py`` / ``m2.py``。

**M2 的 D 组演练产物格式**（``<账本根>/line-drill.json``；本段自旧模块 docstring 逐字搬迁，
是字段规范的历史真源——原指针 ``orchd/milestone.py 模块 docstring`` 现指本段）：::

    {
      "lines": ["main", "line-drill"],
      "regressions": {
        "main": {"passed": 2900, "failed": 0},
        "line-drill": {"passed": 2880, "failed": 0}
      },
      "backport": [
        {"task_id": "...", "source_sha": "...",
         "target_line": "line-drill", "event": "..."}
      ],
      "repo_head": "<演练时源仓库 HEAD（40-hex）>",
      "drill_commit": "<clone 内演练配置提交（其父须为 repo_head）>",
      "lifecycle": {"task_id": "...", "line": "line-drill",
                    "completed": true, "merge_commit": "...",
                    "verify": "tests/test_line_core.py"},
      "created_at": "2026-09-21T00:00:00+00:00"
    }

``regressions`` 两线均 ``failed == 0``、``backport`` 非空、``lifecycle.completed`` 为 true、
``repo_head`` 在当前仓库可达且为 HEAD 祖先（演练过期即失效，防快照 + 防手写），才算演练通过。

依赖方向：constants / context（叶）<- probe <- m1/m2 <- table <- entry 编排器。

monkeypatch 打点面（W8-9 迁移）：修补 ``orchd.milestone.<符号>`` 只改本聚合入口的重导出
属性，对实际调用点无效；补丁须打**消费该符号的域模块属性**（如 ``orchd.milestone.m2.``
``_module_spec``、``orchd.milestone.entry.milestone_names``；表在 table 域、探针在 probe 域）。
实测消费面见 tests/test_milestone_pkg.py 的登记表。
"""

from .constants import (
    EXIT_VERSIONS, MILESTONE_IDS, PERF_BUDGET_TEST, PERF_BUDGET_CONST, PERF_SPAWN_BUDGET_MAX,
    DRILL_ARTIFACT, DRILL_TEST_FILE, NAMING_PROBE_TASK, NAMING_PROBE_EXPECTED, LINE_MODULE,
    LINE_SYNC_COMMAND, RELEASE_SCRIPT, KERNEL_CONTRACT_DOC, KERNEL_CONTRACT_ANCHORS, ROADMAP_FILE,
    ROADMAP_MILESTONE_ANCHOR, _FULL_REGRESSION_FILE, _BASELINE_FILE,
)
from .context import MilestoneContext, _Check
from .probe import (
    _git_head_short, _sha_covers, _status_counts, _read_json, _module_spec, _cli_command_names,
    _perf_budget_value,
)
from .m1 import (
    _m1_a1_active_clear, _m1_a2_pool_priority_clear, _m1_a3_dep_closure, _m1_b1_full_regression,
    _m1_b2_baseline_fresh, _m1_b3_validate, _m1_b4_merge_audit, _m1_c1_kernel_contract,
    _m1_c2_roadmap_milestones, _m1_d1_gate_self,
)
from .m2 import (
    _m2_a1_line_capability, _m2_b1_line_sync_channel, _m2_c1_release_per_line, _attest_repo_head,
    _m2_d1_drill_artifact, _m2_e1_gate_self, _m2_f1_behavior, _m2_f2_perf, _m2_f3_naming,
    _m2_f4_robustness,
)
from .table import MILESTONES
from .entry import milestone_names, build_context, evaluate

__all__ = (
    'EXIT_VERSIONS',
    'MILESTONE_IDS',
    'PERF_BUDGET_TEST',
    'PERF_BUDGET_CONST',
    'PERF_SPAWN_BUDGET_MAX',
    'DRILL_ARTIFACT',
    'DRILL_TEST_FILE',
    'NAMING_PROBE_TASK',
    'NAMING_PROBE_EXPECTED',
    'LINE_MODULE',
    'LINE_SYNC_COMMAND',
    'RELEASE_SCRIPT',
    'KERNEL_CONTRACT_DOC',
    'KERNEL_CONTRACT_ANCHORS',
    'ROADMAP_FILE',
    'ROADMAP_MILESTONE_ANCHOR',
    '_FULL_REGRESSION_FILE',
    '_BASELINE_FILE',
    'MilestoneContext',
    '_Check',
    '_git_head_short',
    '_sha_covers',
    '_status_counts',
    '_read_json',
    '_module_spec',
    '_cli_command_names',
    '_perf_budget_value',
    '_m1_a1_active_clear',
    '_m1_a2_pool_priority_clear',
    '_m1_a3_dep_closure',
    '_m1_b1_full_regression',
    '_m1_b2_baseline_fresh',
    '_m1_b3_validate',
    '_m1_b4_merge_audit',
    '_m1_c1_kernel_contract',
    '_m1_c2_roadmap_milestones',
    '_m1_d1_gate_self',
    '_m2_a1_line_capability',
    '_m2_b1_line_sync_channel',
    '_m2_c1_release_per_line',
    '_attest_repo_head',
    '_m2_d1_drill_artifact',
    '_m2_e1_gate_self',
    '_m2_f1_behavior',
    '_m2_f2_perf',
    '_m2_f3_naming',
    '_m2_f4_robustness',
    'MILESTONES',
    'milestone_names',
    'build_context',
    'evaluate',
)
