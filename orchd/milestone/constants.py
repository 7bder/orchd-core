"""里程碑域常量：退出码阈值 / 里程碑 ID 表 / 预算 / 制品 / 诚实探针 / 契约锚点，叶域。"""

from __future__ import annotations

from orchd.names import (
    FULL_REGRESSION_FILE,
    ROADMAP_FILE,
    TEST_BASELINE_FILE,
)



# 里程碑 → 出口版本
EXIT_VERSIONS: dict[str, str] = {
    "freeze": "v1.5.0",
    "split-ready": "v1.6.0",
}


# 里程碑 id（与 ROADMAP「## 里程碑」节声明一致）
MILESTONE_IDS: dict[str, str] = {
    "freeze": "local-kernel-freeze",
    "split-ready": "line-split-ready",
}


# 性能冻结口径：``tests/test_perf_budget.py`` 的 git 子进程上限**不得上调**
PERF_BUDGET_TEST = "tests/test_perf_budget.py"


PERF_BUDGET_CONST = "PERF_BASELINE_WATCHDOG_FLOW_GIT_SPAWNS"


PERF_SPAWN_BUDGET_MAX = 40


# M2 演练产物与命名口径
DRILL_ARTIFACT = "line-drill.json"


DRILL_TEST_FILE = "tests/test_line_drill.py"


NAMING_PROBE_TASK = "t1"


NAMING_PROBE_EXPECTED = "task/t1"


LINE_MODULE = "orchd.line"


LINE_SYNC_COMMAND = "line-sync"


RELEASE_SCRIPT = "scripts/sync_orchd_core.sh"


# 契约文档与关键锚点（文档缺失 / 锚点缺失即未达成）
KERNEL_CONTRACT_DOC = "docs/kernel-contract.md"


KERNEL_CONTRACT_ANCHORS = ("不变量", "端口")


ROADMAP_FILE = ROADMAP_FILE


ROADMAP_MILESTONE_ANCHOR = "## 里程碑"


_FULL_REGRESSION_FILE = FULL_REGRESSION_FILE


_BASELINE_FILE = TEST_BASELINE_FILE
