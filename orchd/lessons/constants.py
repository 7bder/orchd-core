"""经验回灌域常量（长度/截断/cap/字符预算/锁超时/ID 正则/否定词/告警码），叶域。"""

from __future__ import annotations
from orchd.errors import ErrorCode

import re


# ── 常量 ──────────────────────────────────────────────────────────────────────
SYMPTOM_MAX = 200          # §6.2 单条 symptom 上限


SOLUTION_MAX = 500         # §6.2 单条 solution 上限


SYMPTOM_TRUNC = 80         # §8.5 注入摘要截断


SOLUTION_TRUNC = 200       # §8.5 注入摘要截断


CASES_VERIFIED_CAP = 3     # §8.5 verified 条数硬 cap


CASES_PROPOSED_CAP = 1     # §8.5 无 verified 时 proposed 候选 ≤ 1


CASES_CHAR_BUDGET = 600    # §8.5 单次注入字符预算


LOCK_TIMEOUT_S = 5.0       # §6.3 并发写锁 best-effort 超时


LESSON_ID_RE = re.compile(r"^lesson-(\d+)$")


# 冲突检测否定词（§6.5）：新 solution 含这些词且复用旧 solution 核心动作 → 标记矛盾。
_NEG_WORDS = {"不要", "禁止", "不应", "不应该", "不能", "勿", "避免", "拒绝", "别", "不建议"}


# warning 级错误码（需触发注入标注「参考（warning）」）。
_WARNING_CODES = {
    ErrorCode.E023.name, ErrorCode.E026.name, ErrorCode.E028.name,
    ErrorCode.E029.name, ErrorCode.E030.name,
}


# 引擎预判「值得上报」的 warning 码（§5.1 信号 A）。
_SUGGEST_REPORT_CODES = {ErrorCode.E030.name}
