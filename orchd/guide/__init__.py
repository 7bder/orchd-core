"""orchd/guide/__init__.py — 引擎侧无感用户流程引导（task-guide-seamless-guidance）。
# E008/E009 claim语义归位 - guide table already correct per N1

引擎侧增加无感用户流程引导，防止用户不知道下一步做什么。核心思想是**无感**：
不新增需要主动运行的独立命令，而是在所有命令的 JSON 响应中自动附加一个
``guidance`` 字段，并在人类可读输出（``status --text`` / ``--help``）中嵌入提示。

设计约束（与 cli.py 注入点一起满足存取标准）：
- **依赖面受控（W5-G1/G2/G3 建包后实测口径）**：paths / digest / stage / flow / errors / rules 六域已各自迁出为子模块，本入口不再持有任何 orchd 绝对导入；errors / lessons / line / line_ctx 一律由消费者子模块函数级惰性导入。
  聚合入口退化为「docstring + GUIDANCE_STEPS + 重导出」，对子模块对象保持 identity 同一（按对象打点/调用生效）。
- **纯函数**：``next_guidance`` / ``status_guidance_text`` / ``first_time_guide``
  均为接受已派生的状态数据、返回引导文本的纯函数，无副作用、无 I/O。
- **加法式**：只产生新的 ``guidance`` 键，不修改任何既有字段；已有 guidance
  时不覆盖（幂等，由 cli.py 注入点保证）。
- **best-effort**：任何异常应由调用方（cli.py ``_attach_guidance``）静默跳过，
  不阻塞主流程。

引导语言：中文。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 拆包（file-split-program W5-G1/G2/G3）：paths / digest / stage / flow / errors / rules
# 六域已迁出为子模块。聚合入口退化为「docstring + GUIDANCE_STEPS + 重导出」，
# 不再持有任何 orchd 绝对导入；errors/rules 的 orchd 依赖由子模块函数级惰性导入。
# 重导出保持「旧路径零断裂」：外部取到的仍是子模块同一对象（identity 不变）。
from .digest import (
    CONTEXT_DIGEST_FILES,
    NEXT_ACTION_AWAIT_REVIEW,
    NEXT_ACTION_EXIT,
    NEXT_ACTION_REVIEW_FIRST,
    NEXT_ACTION_REVIEW_TAKEOVER,
    NEXT_ACTION_SUBMIT_REVIEW,
    NEXT_ACTION_TO_STEP,
    NEXT_ACTION_WAIT,
    _ENTRY_CMD,
    _T0_CMDS,
    _T1_CMDS,
    _as_declaration,
    _digest_one,
    _quote_value,
    amend_mainwt_command,
    amend_patch_cmd,
    context_digest,
    guidance_tier,
    step_for_next_action,
)
from .errors import (
    ALLOWED_WEAK,
    E007_INTERNAL_DEFENSE,
    ERROR_CODE_CHANNELS,
    ERROR_GUIDANCE,
    EXIT_TYPE_ACTION_MEANING,
    OUT_OF_ENUM_CODES,
    _ERROR_GUIDANCE_TABLE,
    _FALLBACK_COMMAND,
    _FALLBACK_READ_BY_DOMAIN,
    _FALLBACK_RECOVERY,
    _GIT_DOMAIN,
    _INTAKE_DOMAIN,
    _VALID_CHANNELS,
    _VALID_EXIT_TYPES,
    _VERIFY_DOMAIN,
    _extract_validation_hint,
    _fallback_error_guidance,
    _fallback_read,
    _warning_error_guidance,
    annotate_validation_items,
    attach_error_guidance,
    error_guidance,
)
from .rules import (
    _BLOCK_MAX,
    _DEGRADED_PREFIX,
    _HINT_MAX,
    _MAX_CASES_LEN,
    _MAX_COMMAND_LEN,
    _MAX_READ_LEN,
    _REDLINE_PREFIX,
    _assert_budget_consistency,
    _block_layout_overhead,
    _extract_tldr,
    _summarize_rules,
    _truncate,
    _truncate_redline_semantic,
    attach_rule_summaries,
)
from .flow import (
    _CMD_BRANCH_CTX_TIPS,
    _active_transition,
    _current_line_trunk,
    _merge_hint,
    _merge_read,
    _rework_transition,
    _terminal_transition,
    apply_guidance_mode,
    branch_context,
    context_guidance,
    next_guidance,
    slim_guidance,
    status_guidance_text,
    transition_guidance,
)
from .paths import (
    GUIDANCE_SITUATIONS,
    _ROUTING_CACHE,
    _parse_front_matter,
    _require_routing,
    _resolve_paths,
    _split_paths,
    attach_read_versions,
    init_routing,
    read_for,
    resolve_read_paths,
    template_for,
)
from .stage import (
    _classify,
    _derive,
    _impl_step_guidance,
    _review_step_guidance,
    _summarize,
    _terminal_step_guidance,
    first_time_guide,
    lesson_review_guidance,
    stop_wait_guidance,
)
# 引导语义：{step, read, template, command, hint}（step 取值单一事实源见 GUIDANCE_STEPS）。
# step=下一步动作；read=知识路由（规则文件路径数组，可空）；template=方法路由；
# command=建议 orchd 命令；hint=一句话理由。引擎不读 rules/ 内容，read/template 仅为字符串路径。

# guidance step 词表与旁路导航映射（单一事实源，task-guide-step-vocab-and-routing-fix）
# 本集合登记 guidance 的 step 全部可能取值；tests/test_guidance_sync.py 以此为准，
# 禁止散落裸字面量。⚠ orchd/gitops_ops/branch.py 的 "step" 键是分支状态字段，与 guidance
# 语义无关，全包 AST 扫描须排除，不得并入本词表。
GUIDANCE_STEPS: frozenset[str] = frozenset({
    "first_time",       # 未初始化项目（无 _master.json）
    "empty_project",    # 已初始化但 0 任务
    "claim_review",     # 有可领审查任务
    "submit_review",    # 本 agent 已领审查、待提交结论（原实现缺此出口 → 死循环）
    "wait_review",      # 审查已被他人领取 / 有任务在实现中
    "rework_first",     # 有返工任务（曾被审查打回）待认领
    "request_impl",     # 有待认领实现任务
    "done",             # 本 agent 已认领实现任务，待 done
    "optional_cancel",  # 存在已取消任务（可选处置）
    "done_all",         # 全部任务已完成
    "check_status",     # 兜底：无可匹配分支
    "stop_wait",        # request 无候选：停止等待用户指令
    "lesson_review",    # done 收尾挂起：lessons 暂存待人工审核
    "recover",          # 错误恢复指引（error_guidance）
    "audit_merge",      # review code APPROVED 后附 audit_merge guidance（task-review-completion-guidance）
})
