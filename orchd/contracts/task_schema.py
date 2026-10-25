"""orchd/contracts/task_schema.py — 任务定义 schema 字段名单一真源（T6）。

**定位（single-source-truth-convergence / Wave0 基座）**：集中登记任务定义
（``tasks[]``）的 23 个 schema 字段名，供 ``spec.py`` / ``schema_policy.py`` /
摄入与展示层共用。本模块只登记字段名字符串：不复制 schema 规则、不解析 JSON、
不做校验、不导入任何 ``orchd`` 内部模块（叶子域）。

**唯一真源**：字段集仍以 ``schema/_master.schema.json`` 的
``properties.tasks.items.properties`` 为准；本模块是它的**常量镜像**，测试直读
v1/v2/v3 三份 schema JSON 对拍。schema 增删字段时对拍测试即红，禁止手写副本静默
漂移。

**值契约（零行为变更）**：常量值逐字等于字段名。本卡
（task-convergence-schema-keys）只建基座与锁定测试，**不改 schema 文件与既有
推导/校验逻辑**。

**消费方迁移（Wave2）**：按子域把 ``task.get("...")`` / 字面量键替换为本模块
常量；迁移时禁止改值、改大小写或改变缺省语义。
"""

from __future__ import annotations

ACCEPTANCE_CRITERIA_FIELD = "acceptance_criteria"
ADDITIONAL_SOURCES_FIELD = "additional_sources"
BACKPORT_FIELD = "backport"
BRIEF_FIELD = "brief"
DELIVERABLES_FIELD = "deliverables"
DEPENDS_ON_FIELD = "depends_on"
DIFFICULTY_FIELD = "difficulty"
ESTIMATED_HOURS_FIELD = "estimated_hours"
EXEMPT_FILES_FIELD = "exempt_files"
FILES_TO_EDIT_FIELD = "files_to_edit"
FILES_TO_READ_FIELD = "files_to_read"
ID_FIELD = "id"
IMPORTANCE_FIELD = "importance"
MAX_ATTEMPTS_FIELD = "max_attempts"
MODULE_FIELD = "module"
NAME_FIELD = "name"
PHASE_FIELD = "phase"
REQUIRE_INDEPENDENT_REVIEW_FIELD = "require_independent_review"
REQUIRES_FIELD = "requires"
REVIEWERS_FIELD = "reviewers"
SOURCE_FIELD = "source"
VERIFY_COMMAND_FIELD = "verify_command"
VERIFY_TIMEOUT_SECONDS_FIELD = "verify_timeout_seconds"

# 全量任务字段集（覆盖上方全部 *_FIELD 常量；与三份 schema JSON 直读对拍）
TASK_FIELDS = frozenset({
    ACCEPTANCE_CRITERIA_FIELD,
    ADDITIONAL_SOURCES_FIELD,
    BACKPORT_FIELD,
    BRIEF_FIELD,
    DELIVERABLES_FIELD,
    DEPENDS_ON_FIELD,
    DIFFICULTY_FIELD,
    ESTIMATED_HOURS_FIELD,
    EXEMPT_FILES_FIELD,
    FILES_TO_EDIT_FIELD,
    FILES_TO_READ_FIELD,
    ID_FIELD,
    IMPORTANCE_FIELD,
    MAX_ATTEMPTS_FIELD,
    MODULE_FIELD,
    NAME_FIELD,
    PHASE_FIELD,
    REQUIRE_INDEPENDENT_REVIEW_FIELD,
    REQUIRES_FIELD,
    REVIEWERS_FIELD,
    SOURCE_FIELD,
    VERIFY_COMMAND_FIELD,
    VERIFY_TIMEOUT_SECONDS_FIELD,
})
