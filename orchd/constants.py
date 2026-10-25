"""orchd/constants.py — 环境变量名与 claimed_by 哨兵单一真源（T3/T12）。

**定位（single-source-truth-convergence / Wave0 基座）**：把散落在 cli、doctor、
ledger、gitops、onboard 等子系统的 ``ORCHD_*`` 环境变量名，以及
``claimed_by`` 的 pending / inflight 哨兵值，收敛为**唯一字面量来源**。本模块
只登记字符串常量：不读环境、不解析路径、不导入任何 ``orchd`` 内部模块（叶子域）。

**值契约（零行为变更）**：环境变量常量统一 ``<ENV_VAR>_ENV`` 命名，值逐字等于
现场变量名；哨兵值逐字为 ``pending`` / ``inflight``。本卡
（task-convergence-constants-base）只建基座与锁定测试，**不改任何消费方**。

**消费方迁移（Wave1）**：按子域把 ``os.environ.get("ORCHD_...")`` 等字面量替换
为本模块常量；迁移时禁止改值、改大小写或改变默认值语义。``claimed_by`` 哨兵与
任务状态词表同形但**语义独立、严禁合并**（状态真源见 ``orchd.contracts.events``）。

**T3 特例**：``ORCHD_LEDGER_REF`` 是 ``ledger_sync.refs`` 的 git ref 常量名，
不是环境变量；现场清单把它与 ``ORCHD_*`` 环境变量一并归口，故此处只登记其
契约名（``ORCHD_LEDGER_REF_ENV``），实际 ref 值仍由 ``ledger_sync.refs`` 持有。
"""

from __future__ import annotations

# 身份 / 会话
ORCHD_AGENT_ID_ENV = "ORCHD_AGENT_ID"
ORCHD_SESSION_ID_ENV = "ORCHD_SESSION_ID"
ORCHD_SESSION_TTL_MIN_ENV = "ORCHD_SESSION_TTL_MIN"

# 账本根 / 存储后端 / 快照与注册表 / 测试与自动清洁
ORCHD_HOME_ENV = "ORCHD_HOME"
ORCHD_STORAGE_BACKEND_ENV = "ORCHD_STORAGE_BACKEND"
ORCHD_SNAPSHOT_ROOT_ENV = "ORCHD_SNAPSHOT_ROOT"
ORCHD_REGISTRY_BACKEND_ENV = "ORCHD_REGISTRY_BACKEND"
ORCHD_TEST_STORE_ENV = "ORCHD_TEST_STORE"
ORCHD_AUTO_CLEAN_ENV = "ORCHD_AUTO_CLEAN"

# 认领 / 审查 / 摄入 / 同步锁等待预算
ORCHD_CLAIM_STALE_SECS_ENV = "ORCHD_CLAIM_STALE_SECS"
ORCHD_REVIEW_STALE_SECS_ENV = "ORCHD_REVIEW_STALE_SECS"
ORCHD_INTAKE_LOCK_WAIT_SECS_ENV = "ORCHD_INTAKE_LOCK_WAIT_SECS"
ORCHD_SYNC_LOCK_WAIT_SECS_ENV = "ORCHD_SYNC_LOCK_WAIT_SECS"

# 分线 / 容器根 / ref 写通道逃生口
ORCHD_LINE_ENV = "ORCHD_LINE"
ORCHD_LINE_TASK_PREFIX_ENV = "ORCHD_LINE_TASK_PREFIX"
ORCHD_LINE_TRUNK_ENV = "ORCHD_LINE_TRUNK"
ORCHD_ALLOW_CONTAINER_ROOT_ENV = "ORCHD_ALLOW_CONTAINER_ROOT"
ORCHD_ALLOW_REF_TX_ENV = "ORCHD_ALLOW_REF_TX"

# 静默输出 / hook 与 Python 定位 / 安装期烘焙路径
ORCHD_QUIET_ENV = "ORCHD_QUIET"
ORCHD_PY_ENV = "ORCHD_PY"
ORCHD_PY_LS_ENV = "ORCHD_PY_LS"
ORCHD_HOOK_PY_ENV = "ORCHD_HOOK_PY"
ORCHD_HOOK_LEDGER_ENV = "ORCHD_HOOK_LEDGER"
ORCHD_BAKED_PY_ENV = "ORCHD_BAKED_PY"
ORCHD_BAKED_SRC_ENV = "ORCHD_BAKED_SRC"
ORCHD_SRC_ENV = "ORCHD_SRC"
ORCHD_SRC_DIR_ENV = "ORCHD_SRC_DIR"

# T3 特例：非环境变量，仅锁定 ledger ref 的契约名（见模块 docstring）
ORCHD_LEDGER_REF_ENV = "ORCHD_LEDGER_REF"

# T12：claimed_by 哨兵值（标识冲突来源，不参与状态机；与 T1 状态词表同形但
# 语义独立，严禁合并）
CLAIMED_BY_PENDING = "pending"
CLAIMED_BY_INFLIGHT = "inflight"
