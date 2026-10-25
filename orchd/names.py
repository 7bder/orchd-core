"""orchd/names.py — 运行时资产名/目录名单一真源（T4）。

**定位（single-source-truth-convergence / Wave0 基座）**：把散落在 cli、doctor、
ledger、worktree、milestone 等子系统的运行时资产文件名与 ``.orchd/`` 目录名收敛
为**唯一字面量来源**。本模块只登记名称字符串：不解析路径、不读写文件、不导入
任何 ``orchd`` 内部模块（叶子域）。

**值契约（零行为变更）**：下列常量值逐字等于收敛前的既有硬编码字面量。本卡
（task-convergence-names-base）只建基座与锁定测试，**不改任何消费方**；测试见
``tests/test_names.py``。

**消费方迁移（Wave3）**：按子系统分批把 ``"IDEAS.md"`` / ``"_master.json"`` /
``".orchd-runtime"`` 等字面量替换为本模块常量。迁移时禁止改值、改大小写或
改变拼接语义；资产名与目录名分列，不得混用。本模块**不收录**任务/灵感状态词表
（真源分别为 ``orchd.contracts.events`` 与 ``orchd.intake.parse``），也不用于
路径解析（T13 为独立硬阻断项）。
"""

from __future__ import annotations

# 工作区文档（定位逻辑仍由各消费方负责；本模块只给名称）
IDEAS_FILE = "IDEAS.md"
IDEAS_ARCHIVE_FILE = "IDEAS-archive.md"
ROADMAP_FILE = "ROADMAP.md"

# 任务池 / 账本 / 检查点
MASTER_FILE = "_master.json"
LEDGER_FILE = "_ledger.jsonl"
LEDGER_ARCHIVE_FILE = "_ledger.archive.jsonl"
CHECKPOINT_FILE = "_checkpoint.json"

# 课程库
LESSONS_FILE = "lessons.jsonl"
LESSONS_STAGED_FILE = "lessons.staged.jsonl"

# 布局 / 合并确认 / 会话 worktree 绑定
LAYOUT_FILE = ".layout.json"
MERGE_ACKS_FILE = "merge-acks.json"
SESSION_WORKTREES_FILE = "session-worktrees.json"

# 回归证据
FULL_REGRESSION_FILE = "_full_regression.json"
TEST_BASELINE_FILE = "test_baseline.json"

# ``.orchd/`` 目录名（container 布局的运行时根为 ``.orchd-runtime``）
RULES_DIR = "rules"
SHARED_DIR = "shared"
PROPOSALS_DIR = "proposals"
RUNTIME_DIR = ".orchd-runtime"
