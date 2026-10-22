"""verify 超时常量（_VERIFY_TIMEOUT_MIN/_VERIFY_TIMEOUT_MAX），叶域。"""

from __future__ import annotations

# task-verify-timeout-amend-channel：任务级 verify 预算的 CLI 通道值域。
# schema（schema/_master.schema.json → tasks[].verify_timeout_seconds）只约束
# ``minimum: 1``；上限 600s 为**防呆硬顶**——>10 分钟会让 done 在单点阻塞过久，且
# 违背「verify 只跑定向档」的分级约定（见 .orchd/rules/verify.md）。schema 若新增
# ``maximum``，以 schema 为单一真源并同步本常量：一致性由
# tests/test_cli_control.py::TestAmendVerifyTimeout 机器断言（含引擎默认 120s 不变）。
_VERIFY_TIMEOUT_MIN = 1
_VERIFY_TIMEOUT_MAX = 600
