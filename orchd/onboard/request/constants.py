"""conflict_policy 与 retract 冷却期常量，叶域。"""

from __future__ import annotations


# conflict_policy（task-inflight-conflict-visibility）：在途任务重叠的处置策略。
# 缺省 warn —— 不新增任何跨任务硬阻断（硬阻断会造成互等死锁，且在途集合本身
# 不稳定）；冲突风险由对账/真源修复在事后吸收。
CONFLICT_POLICY_DEFAULT = "warn"


CONFLICT_POLICIES = ("warn", "serialize", "block")


# retract 认领冷却期（秒）：与 claim._RETRACT_COOLDOWN_S 保持一致（task-retract-bind-cooloff）。
_RETRACT_COOLDOWN_S = 300
