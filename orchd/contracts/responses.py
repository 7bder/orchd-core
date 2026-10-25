"""orchd/contracts/responses.py — CLI JSON 响应契约键单一真源（T5）。

**定位（single-source-truth-convergence / Wave0 基座）**：集中登记 CLI stdout
JSON 的顶层契约键与通用 envelope 键，供命令 handler、guidance 渲染与测试共用。
本模块只登记键名字符串：不构造响应、不读写文件、不导入任何 ``orchd`` 内部模块
（叶子域）。

**值契约（零行为变更）**：所有常量值逐字等于现场响应键。本卡
（task-convergence-response-keys）只建基座与锁定测试，**不改任何生产消费方**。

**边界**：不收录任务定义 schema 字段（T6，任务定义侧另行收敛），也不收录事件
类型名（T7，真源为 ``orchd.contracts.events``）。``ERRORS_KEY`` / ``WARNINGS_KEY``
是响应 envelope 的复数键，与单数 ``ERROR_KEY`` 语义不同，不得合并。

**消费方迁移（Wave2）**：按子域把响应字典里的键字面量替换为本模块常量；迁移时
禁止改值、改大小写或改变嵌套层级（``PROJECT_VIEW_KEY`` 等仍位于 ``guidance`` 内）。
"""

from __future__ import annotations

# 核心六键（T5 现场高频且跨命令稳定出现）
TASK_ID_KEY = "task_id"
STATUS_KEY = "status"
COMMAND_KEY = "command"
STEP_KEY = "step"
GUIDANCE_KEY = "guidance"
NEXT_ACTION_KEY = "next_action"

# 通用响应 envelope：错误 / 提示 / 校验结果 / 计数
ERROR_KEY = "error"
MESSAGE_KEY = "message"
HINT_KEY = "hint"
OK_KEY = "ok"
WARNINGS_KEY = "warnings"
ERRORS_KEY = "errors"
VALID_KEY = "valid"
COUNT_KEY = "count"

# 集合 / 任务池 envelope
TASK_KEY = "task"
TASKS_KEY = "tasks"
PROJECT_KEY = "project"
SUMMARY_KEY = "summary"
CANDIDATE_KEY = "candidate"
POOL_SIZE_KEY = "pool_size"

# 工作流结果 envelope
CLAIMED_KEY = "claimed"
DONE_KEY = "done"
EVENT_ID_KEY = "event_id"
BRANCH_KEY = "branch"
MERGED_KEY = "merged"
TASK_STATUS_KEY = "task_status"
VERDICT_KEY = "verdict"
REVIEW_TYPE_KEY = "review_type"
COMMENTS_KEY = "comments"

# guidance 子 envelope（嵌套在 GUIDANCE_KEY 内）
PROJECT_VIEW_KEY = "project_view"
AGENT_VIEW_KEY = "agent_view"
CWD_SWITCH_KEY = "cwd_switch"

# 会话 envelope
SESSION_ID_KEY = "session_id"
FINGERPRINT_KEY = "fingerprint"

# 合并审计 envelope
MERGE_AUDIT_KEY = "merge_audit"

# 全量键集（覆盖上方全部 *_KEY 常量；新增键须同步登记）
RESPONSE_KEYS = frozenset({
    TASK_ID_KEY,
    STATUS_KEY,
    COMMAND_KEY,
    STEP_KEY,
    GUIDANCE_KEY,
    NEXT_ACTION_KEY,
    ERROR_KEY,
    MESSAGE_KEY,
    HINT_KEY,
    OK_KEY,
    WARNINGS_KEY,
    ERRORS_KEY,
    VALID_KEY,
    COUNT_KEY,
    TASK_KEY,
    TASKS_KEY,
    PROJECT_KEY,
    SUMMARY_KEY,
    CANDIDATE_KEY,
    POOL_SIZE_KEY,
    CLAIMED_KEY,
    DONE_KEY,
    EVENT_ID_KEY,
    BRANCH_KEY,
    MERGED_KEY,
    TASK_STATUS_KEY,
    VERDICT_KEY,
    REVIEW_TYPE_KEY,
    COMMENTS_KEY,
    PROJECT_VIEW_KEY,
    AGENT_VIEW_KEY,
    CWD_SWITCH_KEY,
    SESSION_ID_KEY,
    FINGERPRINT_KEY,
    MERGE_AUDIT_KEY,
})
