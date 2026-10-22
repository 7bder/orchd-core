"""orchd/guide/errors.py — 错误码注册表与错误指引域（W5-G3 拆分，纯搬移）。

来源：``orchd/guide/__init__.py`` L269-677（退出类型五分法、E007 内部防线、通道表、错误指引表、
域兜底表、fallback/warning 指引、validation 项标注与 guidance 附着）。依赖方向：``digest`` /
``paths``（顶层）+ ``orchd`` / ``orchd.lessons`` / ``orchd.errors``（函数级惰性）；不导入聚合入口。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .digest import _ENTRY_CMD, amend_patch_cmd
from .paths import read_for, resolve_read_paths
from .rules import attach_rule_summaries


# 错误恢复指引映射（error recovery guidance）：错误码 → 恢复指引。
# 只提示不代行——不自动执行任何修复命令，修复决策权始终在人/agent。
# (task-audit-error-guidance-coverage) 数据化：14 个结构相同的 dict 收敛为
# 表格常量 + 循环构建，覆盖 ErrorCode 全部枚举成员（数量**不写死**，避免随枚举增删漂移；
# 既有「36」于 task-pass13-p3-pack-recheck 实测已漂移为 39）——新增错误码未补指引时，
# 测试 set(ERROR_GUIDANCE) ⊇ {e.name for e in ErrorCode} 直接失败。
# (task-errexit-type-contract) 静态表 5 元组：新增 exit_type 五分法。
# (pass13 §11-8a task-exit-type-decoupled-from-blocking) 更正旧表述「warning 码
# 统一 continue」——exit_type 只回答「agent 接下来怎么行动」，**不回答「是否阻断」**；
# 阻断性由 ERROR_CODE_CHANNELS 的通道 A 决定。旧表述会把可硬阻断的告警码
# （E028/E029/E030 同属 WARNING_CODES 又有 A 通道位点）指成「继续干」。
_VALID_EXIT_TYPES: frozenset[str] = frozenset({
    "exec-command", "git-diagnose", "manual-action", "await-external", "continue",
})

# 每档的动作语义 + 是否隐含「不阻断」。隐含不阻断只有 continue 一档，
# 且其准入条件是「该码无通道 A」（由 tests/test_guide.py 判据实时按登记表校验）。
EXIT_TYPE_ACTION_MEANING: dict[str, str] = {
    "exec-command": "执行 command 字段给出的命令即可解除阻塞（命令是动作本体）；不隐含不阻断",
    "git-diagnose": "先做 git 侧诊断（状态/分支/工作树）再决定；不隐含不阻断",
    "manual-action": "需人/agent 决策后走别的命令（给出的 command 仅诊断参考）；不隐含不阻断",
    "await-external": "等外部条件（他方释放锁/完成认领），禁止重试原命令；不隐含不阻断",
    "continue": "这不是阻断，按原计划继续；**仅允许无通道 A（不可 raise 阻断）的码使用**",
}
_ERROR_GUIDANCE_TABLE: tuple[tuple[str, str, tuple[str, ...], str, str, str], ...] = (
    # (错误码, recovery, read 规则文件, command, tier, exit_type)
    ("E001", "源码/任务文件缺失：补齐或修正 details.path 指向的文件路径后重试，运行 python .orchd/__main__.py validate 定位缺失文件", ("rules/intake.md",), f"{_ENTRY_CMD} validate", "suggest", "manual-action"),
    ("E002", "JSON 解析失败：用 doctor 检查并修复损坏的 intake/schema 文件", ("rules/intake.md",), f"{_ENTRY_CMD} doctor", "suggest", "exec-command"),
    ("E003", "schema 校验失败：运行 validate 定位并修复不符项后重试", ("rules/intake.md",), f"{_ENTRY_CMD} validate", "suggest", "exec-command"),
    ("E004", "检测到 DAG 依赖环：检查 depends_on 引用，消除循环依赖", ("rules/intake.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E005", "引用缺失：补全被引用的任务/源条目（IDEAS/ROADMAP/source）", ("rules/intake.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E006", "ID 重复：确认任务标识唯一，合并或重命名冲突项", ("rules/intake.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E007", "任务状态不合法：当前 <current> 不允许 <event_type>→<target>，允许集合 <allowed>，请用 python .orchd/__main__.py status 查看当前状态并修正", ("rules/session.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E008", "任务未就绪：确认任务处于 pending 状态再 claim", ("rules/session.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E009", "任务已被认领：已被 <holder> 认领，不要重试原命令，等待其完成或由其 retract", ("rules/session.md",), f"{_ENTRY_CMD} status", "suggest", "await-external"),
    ("E010", "文件冲突：与 <claimed_by> 任务 files_to_edit 重叠（<files>），需串行化或合并任务，等待其 done 后重试", ("rules/intake.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E011", "agent 忙：已持有其他任务，不要重试原命令，先完成或 retract 再领新任务", ("rules/session.md",), f"{_ENTRY_CMD} status", "suggest", "await-external"),
    ("E012", "锁持有超时：用 watchdog 查看锁持有者，不要重试原命令，失效则清理并重试", ("rules/recovery.md",), f"{_ENTRY_CMD} watchdog", "suggest", "await-external"),
    ("E013", "引擎未初始化：先运行 init 初始化工作区再操作", ("rules/install.md",), f"{_ENTRY_CMD} init", "auto", "exec-command"),
    ("E014", "verify 失败：查看 verify 输出，修复实现后重试 done", ("rules/verify.md",), "python -m pytest <定向测试>", "suggest", "exec-command"),
    ("E015", "合并冲突/基线陈旧：在任务分支执行受管出口 git merge main（禁手写 git merge、禁强推），解决冲突后 commit 并重跑 verify；细节看 command 输出的冲突清单", ("rules/git.md",), f"{_ENTRY_CMD} git merge main", "manual", "exec-command"),
    # E016 命令相位无关（task-guidance-completeness-gate AC6）：claim 的 --type choices
    # 仅 [spec, code]，unified 单阶段须省略该参数——历史上此处写 `--type review` 属非法取值
    # （与 _review_step_guidance 的 unified 分支口径一致）。
    ("E016", "自审被阻断：当前会话已实现 <task_id>——请起独立指纹的 subagent/新对话领审（换会话/身份后重新 claim --task <task_id> --confirm，相位无关），或由他人审查", ("rules/review.md",), f"{_ENTRY_CMD} claim --task <id> --confirm", "suggest", "manual-action"),
    ("E017", "工作区脏：先提交/清理未提交改动，再重试", ("rules/git.md",), "git status", "suggest", "git-diagnose"),
    ("E018", "分支错误：确认处于正确分支；主工作树滞留 task/<id> 且该任务已放弃/无活跃认领时，重新 claim 将自动恢复 trunk 基线（implementer_trunk_restore），或走 force-status + amend 受管往返", ("rules/git.md",), "git branch --show-current", "suggest", "git-diagnose"),
    ("E019", "工作区忙：检查持有会话锁的 agent，不要重试原命令，等待其释放或按需接管", ("rules/session.md",), f"{_ENTRY_CMD} watchdog", "suggest", "await-external"),
    ("E020", "范围外提交：只改 files_to_edit 声明文件", ("rules/git.md",), "git status", "suggest", "git-diagnose"),
    ("E021", "身份不匹配：确认 ORCHD_SESSION_ID 与认领者一致，必要时重连", ("rules/session.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E022", f"缺少 verify_command：任务 <task_id> 缺少 verify_command，请运行 {amend_patch_cmd('<id>', verify='<cmd --basetemp=...>')} 补充后重试", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<cmd>"), "suggest", "exec-command"),
    ("E023", "验收标准模糊（警告不阻断）：建议用可量化标准，可 amend 修订；结构术语（如六环/论证链）在库内无定义时同属本码——补定义或登记 shared/conventions.md 术语表", ("rules/intake.md",), f"{_ENTRY_CMD} amend", "suggest", "continue"),
    ("E024", "verify_command 缺 --basetemp：用 amend 补充跨平台 basetemp", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<cmd --basetemp=...>"), "suggest", "exec-command"),
    ("E025", "source 引用缺失：任务 <task_id> 需关联 IDEAS.md/ROADMAP.md 条目 <source>，请补齐 source 后重试", ("rules/intake.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E026", "测试连带未声明（警告不阻断）：用 amend 声明测试或加入 exempt_files", ("rules/verify.md",), f"{_ENTRY_CMD} amend", "suggest", "continue"),
    ("E027", "verify_command 含不安全/不兼容段：用 amend 改为跨平台安全命令", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<安全命令>"), "suggest", "exec-command"),
    ("E028", f"dry-run 断言不匹配：任务 <task_id> 的 verify_command 断言失败，请核对断言或运行 {amend_patch_cmd('<id>', verify='<修正命令>')} 调整；A 通道下该失败**阻断注册**（不写 snapshot），修正后重试即解除", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<修正命令>"), "suggest", "exec-command"),
    ("E029", "任务拆解粒度越界：任务 <task_id> 按 R4 文件≤5/行≤60/小时≤8 拆分为更小任务，参考 docs/decomposition-guide.md；B 通道下为告警不阻断，A 通道下**拒绝注册**——须先拆小并补登记新任务后重试", ("rules/intake.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E030", "运行时文件完整性校验失败：用 doctor 诊断并修复引擎文件；B/C 通道下为告警不阻断，A 通道下**阻断回收**（无法确认 in_review 保护），doctor 修复后重试", ("rules/recovery.md",), f"{_ENTRY_CMD} doctor", "manual", "exec-command"),
    ("E031", "ROADMAP 规划章节 <chapter> 未落地 IDEAS：先运行 python .orchd/__main__.py roadmap-land <版本> 落地为 IDEAS study（待用户 confirm 升 pending）后再 intake；若该版本已发布或已放弃，在 ROADMAP 标记「历史」或移出", ("rules/intake.md",), f"{_ENTRY_CMD} roadmap-land <版本>", "suggest", "continue"),
    ("E032", "auto-claim 被禁：需人工确认 claim 或 config.allow_auto_claim", ("rules/session.md",), f"{_ENTRY_CMD} claim --task <id> --confirm", "suggest", "exec-command"),
    ("E033", "会话身份缺失：先 session start 取 token 并执行其注入命令（$env:ORCHD_SESSION_ID=\"<token>\" / export ORCHD_SESSION_ID=\"<token>\"），再重试", ("rules/session.md",), f"{_ENTRY_CMD} session start", "suggest", "exec-command"),
    ("E034", "撤认归属守卫：仅事件作者 <owner> 或 admin 可撤回 <task_id>，当前 <caller> 无权；跨 agent 撤认仅限超时（僵尸）认领（CLAIMED 600s / REVIEW_CLAIMED 300s，env 可覆盖），未超时请停止", ("rules/session.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E035", "会话冲突告警（警告不阻断）：同一工作区多会话碰撞，确认各会话职责避免写入竞争", ("rules/session.md",), f"{_ENTRY_CMD} watchdog", "suggest", "continue"),
    ("E036", "容器根执行被拒：切换到主工作树（details.main_worktree）下执行，或设 ORCHD_ALLOW_CONTAINER_ROOT=1 豁免", ("rules/git.md",), f"{_ENTRY_CMD} status", "suggest", "manual-action"),
    ("E037", "verify_command 引用路径未声明且不存在：将路径加入 files_to_edit/exempt_files，或改指向实际存在的文件（声明口径一致性）", ("rules/intake.md",), f"{_ENTRY_CMD} amend --task <id> --files-to-edit <path>", "suggest", "exec-command"),
    # task-hint-ghost-fix（pass7 P1-5/D2）：原建议命令 `amend --brief` 旗标不存在（幽灵命令）。
    # 改为两条可执行通道：补声明（--files-to-edit）或终态文本修订（--revise-terminal）。
    ("E038", f"brief 声明的 files_to_edit 数量口径与实际声明不一致（warning 不阻断）：对齐二者——补声明 {amend_patch_cmd('<id>', files=['<f>'])}，或修订 brief 文本（pending 态 {amend_patch_cmd('<id>', brief='<对齐后的 brief>')}；终态任务用 {_ENTRY_CMD} amend --revise-terminal <id> --brief \"<对齐后的 brief>\" --reason <理由>）", ("rules/intake.md",), amend_patch_cmd("<id>", files=["<f>"]), "suggest", "continue"),
    ("E039", f"共享入口改动未覆盖登记的既有测试：<missing> 必须出现在 verify_command 的 pytest 目标里（否则既有断言静默变红），用 {amend_patch_cmd('<id>', verify='<补入登记测试后的命令>')} 补登后重试", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<补入登记测试后的命令>"), "suggest", "exec-command"),
    # task-ac-verify-coverage：AC → verify 反向一致性（E040 ac_verify_gap）。AC 声称
    # 全仓零残留 / 全仓可判定性质而 verify_command 无对应覆盖时告警；warning 不阻断
    # （exit_type=continue）。全仓声称要求 repo 级 grep，文件级不算覆盖。
    ("E040", f"AC 声称「全仓零残留 / 全仓可判定」性质但 verify_command 未覆盖（warning 不阻断）：补 repo 级 grep（grep -r / rg / git grep）到 verify_command，用 {amend_patch_cmd('<id>', verify='<含 repo 级 grep 的命令>')} 补登后重试；或把该 AC 降级为文件级 / manual（review 表据此标红，指引补 repo 级 grep）", ("rules/verify.md",), amend_patch_cmd("<id>", verify="<含 repo 级 grep 的命令>"), "suggest", "continue"),
)
# 显式豁免集：允许裸弱出口的码（默认空集，加入需逐一说理注释）
ALLOWED_WEAK: frozenset[str] = frozenset()
# E007 内部防御白名单：正常协作流不可达的位点，无需补 hint（按文件显式登记）
E007_INTERNAL_DEFENSE: frozenset[str] = frozenset({
    # W8-8：lessons.py 拆为 lessons/ 包（task-split2-w8-8-lessons-pkg）；8 处字段校验随域落位——
    # _validate_lengths ×2 → text；stage ×1 / add ×2 / report ×1 → stage；resolve/archive 各 1 → review。
    "orchd/lessons/text.py",
    "orchd/lessons/stage.py",
    "orchd/lessons/review.py",
    # W8-2：split.py 拆为 split/ 包（task-split2-w8-2-split-pkg）；4 处结构校验落点：
    "orchd/split/amend_precheck.py",  # 结构校验 invalid_branch
    "orchd/split/amend.py",  # 结构校验 stale_base
    "orchd/split/amend_admission.py",  # 结构校验 stale_base / revise_terminal_not_terminal
    "orchd/split/amend_route_tasks.py",  # 结构校验 cannot_amend
    "orchd/cli/commands/lessons.py:lesson",  # lesson 字段校验（cli 拆包后路径）
})

# ── 码→通道登记表（设计 §2.2 / §8.5）───────────────────────────────────────
# 每个 ErrorCode 至少登记一条可达通道，防止死映射与 B/C/D 漏接线。
# 通道含义：A=异常(OrchdError 构造后冒泡), B=批量校验(ValidationError),
#           C=手工dict(structured_error / "code" 字面量), D=Shell hook(echo/print)
# 本表由 tests/test_guide.py::TestChannelRegistry 做**四通道双向**反向核对
# （pass13 §11-8b）：登记而实测无 → 死档；实测有而登记缺 → 漏登，两个方向都判红。
# 实测口径（与扫描器一字不差）：A=任意 `OrchdError(...)` 构造点（raise / builder
# return / 赋值皆算，含 `A if cond else B` 条件形态两支），B=`ValidationError(code=…)`，
# C=`structured_error(…)` 首参或 dict 字面量 `"code": Exxx`，D=gitops/hook.py 内
# `echo`/`print(` 同排之后出现的 E0xx（注释行不计）。
ERROR_CODE_CHANNELS: dict[str, frozenset[str]] = {
    "E001": frozenset({"A"}),       # file_not_found: raise OrchdError ×4
    "E002": frozenset({"A"}),       # invalid_json: raise OrchdError ×1
    "E003": frozenset({"A", "B"}),  # schema_validation_failed: raise ×2 + spec ValidationError
    "E004": frozenset({"B"}),       # dag_cycle: spec ValidationError
    "E005": frozenset({"A", "B"}),  # reference_not_found: raise ×1 + spec ValidationError
    "E006": frozenset({"B"}),       # duplicate_id: spec ValidationError
    "E007": frozenset({"A", "C"}),  # invalid_state: raise ×48
                                    # + C：cli/__init__.py:193 argparse 解析失败的 JSON 封套
                                    #   （复用了 E007 名义，语义存疑，见 OUT_OF_ENUM_CODES 邻注）
    "E008": frozenset({"A"}),       # task_not_ready: raise ×5
    "E009": frozenset({"A"}),       # already_claimed: raise ×2
    "E010": frozenset({"A"}),       # file_conflict: raise ×5
    "E011": frozenset({"A"}),       # agent_busy: raise ×3
    "E012": frozenset({"A"}),       # lock_timeout: raise ×4
    "E013": frozenset({"A"}),       # not_initialized: raise ×1
    "E014": frozenset({"A"}),       # verify_command_failed: lifecycle/core/verify.py（_run_verify）
                                    #   builder 构造后由调用方 raise（旧扫描器看不见，靠 discard 豁免）
    "E015": frozenset({"A", "C"}),  # merge_conflict: A=done 前置对账 raise（core/reconcile.py 挂载点①）
                                    # + C：review.py:1009 structured_error("E015") 挂 result.error
    "E016": frozenset({"A"}),       # self_review_blocked: raise ×1
    "E017": frozenset({"A", "D"}),  # dirty_workspace: raise ×5
                                    # + D：hook.py:892 echo "orchd E017: review in progress"
    "E018": frozenset({"A"}),       # wrong_branch: raise ×5
    "E019": frozenset({"A"}),       # workspace_busy: raise ×2
    "E020": frozenset({"D"}),       # out_of_scope_commit: git hook echo（文本由本表 E020 行派生）
    "E021": frozenset({"C"}),       # identity_mismatch: cli 手工 dict (warning)
    "E022": frozenset({"A", "B"}),  # missing_verify_command: raise ×1 + spec ValidationError
    "E023": frozenset({"B"}),       # vague_acceptance_criteria: spec ValidationError (warning)
    "E024": frozenset({"B"}),       # verify_command_missing_basetemp: spec ValidationError
    "E025": frozenset({"A", "B"}),  # source_reference_not_found: raise ×1
                                    # + B：spec.py:1210 起 10 处 ValidationError(code=E025)
    "E026": frozenset({"B"}),       # unexempted_test_coupling: spec ValidationError (warning)
    "E027": frozenset({"A", "B"}),  # verify_command_unsafe: raise ×1 + B：spec.py:1035
    "E028": frozenset({"A", "C"}),  # dry_run_assertion_mismatch: A=control.py:879 条件 raise（阻断注册）
                                    # + C：control.py:807 structured_error("E028")
                                    # 旧登记写作 {B,C}：spec.py 内并无 code=ErrorCode.E028，B 是死档
    "E029": frozenset({"A", "B"}),  # granularity_overflow: B=spec ValidationError (warning)
                                    # + A：control.py:576 amend --acceptance-criteria 拒绝注册（硬阻断）
    "E030": frozenset({"A", "B", "C"}),  # runtime_file_integrity: C=gitops/ledger warn dict (warning)
                                          # + A：recycle.py:247 账本不可读阻断回收
                                          # + B：spec.py:848 degraded guard 条目
    "E031": frozenset({"C"}),       # roadmap_landing_warning: spec.py:1582 structured_error("E031")
                                    # 旧登记含 B：spec.py 内无 code=ErrorCode.E031，B 是死档
    "E032": frozenset({"C"}),       # auto_claim_disabled: cli 手工 dict
    "E033": frozenset({"A"}),       # session_identity_missing: raise ×6
    "E034": frozenset({"A"}),       # retract_not_authorized: raise ×1
    "E035": frozenset({"C"}),       # session_collision_warning: cli 手工 dict (warning)
    "E036": frozenset({"A"}),       # container_root_cwd: raise ×1
    "E037": frozenset({"B"}),       # verify_reference_drift: spec ValidationError (blocking)
    "E038": frozenset({"B"}),       # brief_decl_count_mismatch: spec ValidationError (warning)
    "E039": frozenset({"A"}),       # shared_entry_verify_gap: done 前置挂载点 raise ×1
                                    # （onboard/lifecycle/core/entry_guard.py _guard_shared_entry_coverage）
    "E040": frozenset({"B"}),       # ac_verify_gap: spec ValidationError (warning)
}

# 非 ErrorCode 枚举成员的兜底发射码：code → 实测位点。
# E999 是「意外异常最后防线」的对外封套码（cli/__init__.py:253），刻意不进
# ErrorCode 枚举（枚举段 E001–E040 无空洞是被锁的契约），故单独登记，
# 由 test_channel_c_registered_equals_measured 校验其位点真实存在——
# 白名单若指不到实测发射行，本身就是判据错误。
OUT_OF_ENUM_CODES: dict[str, str] = {
    "E999": "orchd/cli/__init__.py:253",  # unexpected_error: 兜底 except 的 stdout JSON 封套
}

# 通道有效值（用于断言新增码必须登记有效通道）
_VALID_CHANNELS = frozenset({"A", "B", "C", "D"})

ERROR_GUIDANCE: dict[str, dict[str, Any]] = {
    code: {"recovery": recovery, "read": list(reads), "command": command, "tier": tier, "exit_type": exit_type}
    for code, recovery, reads, command, tier, exit_type in _ERROR_GUIDANCE_TABLE
}

# 未命中映射时的通用兜底指引（保证任何错误码都有恢复指引）。
# 设计 §3 补丁1：本兜底「立即停止并报告」**不算"明确指引"**——归入"无 guidance"
# 分支，允许 agent 分析自愈（否则所有未映射错误码都有 fallback 指引，自愈永不触发）。
# (task-audit-error-guidance-coverage) fallback 的 read 不再硬编码 rules/session.md，
# 而是按错误码所属域推导：verify→rules/verify.md、git→rules/git.md、
# intake→rules/intake.md，其余默认 rules/session.md，使兜底指引贴近错误发生场景。
_FALLBACK_RECOVERY = "遇到错误：立即停止并报告，不自行猜测处置"
_FALLBACK_COMMAND = f"{_ENTRY_CMD} status"

_VERIFY_DOMAIN = frozenset({"E014", "E022", "E023", "E024", "E026", "E027", "E028",
                            "E037", "E039", "E040"})
_GIT_DOMAIN = frozenset({"E010", "E015", "E017", "E018", "E019", "E020"})
_INTAKE_DOMAIN = frozenset({"E001", "E002", "E003", "E004", "E005", "E006", "E025", "E029"})
_FALLBACK_READ_BY_DOMAIN: dict[str, tuple[str, ...]] = {
    "verify": ("rules/verify.md",),
    "git": ("rules/git.md",),
    "intake": ("rules/intake.md",),
    "session": ("rules/session.md",),
}


def _fallback_read(code: str) -> tuple[str, ...]:
    """按错误码所属域返回兜底指引的 read 规则文件（未知码默认 session）。"""
    if code in _VERIFY_DOMAIN:
        return _FALLBACK_READ_BY_DOMAIN["verify"]
    if code in _GIT_DOMAIN:
        return _FALLBACK_READ_BY_DOMAIN["git"]
    if code in _INTAKE_DOMAIN:
        return _FALLBACK_READ_BY_DOMAIN["intake"]
    return _FALLBACK_READ_BY_DOMAIN["session"]


def _fallback_error_guidance(code: str) -> dict[str, Any]:
    """未映射错误码的兜底指引（read 按错误码域推导）。"""
    return {
        "recovery": _FALLBACK_RECOVERY,
        "read": list(_fallback_read(code)),
        "command": _FALLBACK_COMMAND,
        "tier": "manual",
        "exit_type": "manual-action",
    }

# warning 级错误独立指引（设计 §5）：warning 不阻断操作，agent 应继续而非停止，
# 与 _fallback_error_guidance 的"立即停止"语义不匹配。命中 warning 码（且未映射
# 到具体 ERROR_GUIDANCE）时走本指引，而非 fallback。本任务映射覆盖全部枚举码后，
# 此分支主要防御未来新增的 warning 码未补映射的情形。
def _warning_error_guidance() -> dict[str, Any]:
    """warning 级未映射码的独立指引构造（task-guide-routing-meta）。

    原模块常量 ``_WARNING_ERROR_GUIDANCE`` 的函数化：read 经
    ``read_for("warning_unmapped")`` 反向生成，杜绝常量内手写字面量
    （AST 门禁不留例外）。调用方（error_guidance）每次取新 dict，可安全改写。
    """
    return {
        "recovery": (
            "警告不阻断：可继续当前流程；若你判定该警告是更深问题的征兆，"
            "可 lesson report 上报（设计 §5.1 三重信号判定）"
        ),
        "read": read_for("warning_unmapped"),
        "command": f"{_ENTRY_CMD} status",
        "tier": "manual",
        "exit_type": "continue",
    }


def error_guidance(code: str) -> dict[str, Any]:
    """按错误码返回恢复指引（纯函数）。

    Args:
        code: 错误码名（ErrorCode.name，如 ``"E017"``）。

    Returns:
        ``{step: "recover", recovery, read, command}``；未命中映射时返回
        通用兜底指引，保证任何错误码都有恢复指引。warning 码（且未映射具体指引）
        走独立 warning 指引（设计 §5）。suggest_report 码额外标注，供 agent 决策
        是否打点 lesson（§5.1 信号 A）。
    """
    from orchd.errors import is_suggest_report_code, is_warning_code

    g = ERROR_GUIDANCE.get(code)
    if g is None and is_warning_code(code):
        g = _warning_error_guidance()
    if g is None:
        g = _fallback_error_guidance(code)
    out = {"step": "recover", **g}
    if is_suggest_report_code(code):
        out["suggest_report"] = True
    return out


def annotate_validation_items(
    items: list[dict[str, Any]],
    base_dir: str | os.PathLike[str] | None,
) -> list[dict[str, Any]]:
    """批量校验结果逐条附加 error_guidance（通道 B：validate/split 批量输出）。

    纯函数：输入 ``[{code, path, message}, ...]`` 列表，输出每条附加
    ``guidance: {recovery, command, exit_type}``，并将 message 中已定位的
    对象（cycle_nodes / 重复 id / 缺失引用 id）透传进 ``guidance.hint``。

    Args:
        items: 校验错误/告警列表，每条含 ``code``（错误码名，如 ``"E004"``）、
              ``path``（JSON Path）、``message``（人类可读描述）。
        base_dir: 项目 .orchd/ 目录，用于 resolve_read_paths；None 时跳过路径校验。

    Returns:
        新列表（不修改原列表），每条附加 ``guidance`` 键。
    """
    import re as _re

    out: list[dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        code = item.get("code", "")
        # 批量项的 code 可能是 ValidationError 的 str(code) 全名（"ErrorCode.E038"），
        # 查表键是码名（"E038"）——归一化后查表，否则恒落 fallback（实测 E038
        # warning 的 guidance 一直是通用兜底，E038 词条里的对齐命令从未到达用户；
        # task-amend-text-field-patch-channel AC4：响应必须给出可执行的对齐命令）。
        code_name = code.rsplit(".", 1)[-1] if isinstance(code, str) else code
        g = error_guidance(code_name)
        # 场景化 hint：从 message 中提取已定位的对象
        hint = _extract_validation_hint(code_name, item.get("message", ""))
        if hint:
            g["hint"] = hint
            g["recovery"] = hint
        entry["guidance"] = g
        out.append(entry)
    return out


def _extract_validation_hint(code: str, message: str) -> str:
    """从校验错误 message 中提取场景化 hint（纯函数）。

    - E004 (DAG cycle): 提取 cycle 节点列表
    - E006 (duplicate id): 提取重复的 id
    - E005 (reference not found): 提取缺失的引用 id
    """
    import re as _re

    if code == "E004":
        m = _re.search(r"dependency cycle detected involving: (.+)", message)
        if m:
            nodes = m.group(1).strip()
            return f"依赖环涉及任务: {nodes}，请检查 depends_on 消除循环依赖"
    elif code == "E006":
        m = _re.search(r"duplicate (task_id|module_id): '([^']+)'", message)
        if m:
            id_type = m.group(1)
            dup_id = m.group(2)
            return f"重复的 {id_type}: '{dup_id}'，请合并或重命名冲突项"
    elif code == "E005":
        m = _re.search(r"module '([^']+)' not found", message)
        if m:
            mod = m.group(1)
            return f"模块 '{mod}' 未在 modules[] 中定义，请检查 module 字段"
        m = _re.search(r"shared file not found: '([^']+)'", message)
        if m:
            ref = m.group(1)
            return f"共享文件 '{ref}' 不存在，请检查 shared 配置"
        m = _re.search(r"task '([^']+)' not found in master", message)
        if m:
            tid = m.group(1)
            return f"任务 '{tid}' 在 _master.json 中不存在，检查 id 是否拼写正确或是否已注册"
    return ""


def attach_error_guidance(
    resp: dict[str, Any],
    code: str,
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """错误响应附加 guidance（加法式，纯函数）。

    调用方（cli.py ``except OrchdError`` 分支）在 ``to_json_response`` 之后
    透传错误码与 base_dir，本函数返回**新增 ``guidance`` 键**的错误响应副本：
    ``{step: "recover", recovery, read, rules, command}``——read 走
    ``resolve_read_paths`` + ``attach_rule_summaries``，错误指引同样带红线摘要。

    - 幂等：已有 guidance 不覆盖。
    - best-effort：任何异常由调用方静默跳过。
    """
    out = dict(resp)
    if "guidance" in out:
        return out
    g = error_guidance(code)
    guidance = attach_rule_summaries(resolve_read_paths(g, base_dir), base_dir)
    # 场景化 hint 优先（2026-08-28 bug1 修复）：错误详情含 hint（如 done 越界 →
    # amend 补声明、锁冲突 → watchdog）时，用它覆盖通用 recovery，使恢复指引贴近
    # 具体原因而非泛化描述。取首个含 hint 的 detail，缺失则保持通用 recovery。
    error_details = (resp.get("error") or {}).get("details") or []
    for detail in error_details:
        if isinstance(detail, dict) and detail.get("hint"):
            guidance["hint"] = detail["hint"]
            guidance["recovery"] = detail["hint"]
            break
    # invalid_usage 分流（task-e007-invalid-usage-split）：argparse 封套不得套
    # 状态机模板（占位符外漏 + 指错 rules/session.md）。reason 词汇沿 E007 reason
    # 字段（task-e007-reason-field），此处只认 invalid_usage，其余 reason 不动；
    # read 置空（usage 以 stderr argparse 原文为准，无适用规则文档），command 指
    # 向本次调用的子命令 --help（由 details argv 推导，沿用封套的脱敏 argv）。
    for detail in error_details:
        if (isinstance(detail, dict) and code == "E007"
                and detail.get("reason") == "invalid_usage"):
            argv = detail.get("argv") or []
            sub = next(
                (a for a in argv
                 if isinstance(a, str) and not a.startswith("-")
                 and a != "…(truncated)"),
                "",
            )
            guidance["read"] = []
            guidance["command"] = (
                f"{_ENTRY_CMD} {sub} --help" if sub else f"{_ENTRY_CMD} --help"
            )
            break
    # 版本占位代入（task-caliber-residue-and-surface-gate AC1）：E031 一类的文案**真源是
    # 本模块的错误码注册表**（带 `<版本>` 占位），调用方只传 ``details[].version``，**不再
    # 自带一份措辞**——此前 validate 用另一处文案覆盖注册表，同一件事两份说法、改一处
    # 另一处照旧（E031 的 "IDEAS pending" 假口径就是这么活下来的）。代入收敛在这一处。
    for detail in error_details:
        if isinstance(detail, dict) and detail.get("version"):
            for key in ("hint", "recovery"):
                text = guidance.get(key)
                if isinstance(text, str) and "<版本>" in text:
                    guidance[key] = text.replace("<版本>", str(detail["version"]))
            break
    # 经验回灌触发注入（设计 §8.4）：错误码命中的 lesson cases 挂到 guidance.cases。
    # best-effort：任何异常静默跳过，绝不阻塞错误响应主流程。
    try:
        from orchd import __version__
        from orchd.lessons import is_lessons_enabled, lookup_lessons

        bd = Path(base_dir) if base_dir is not None else None
        if bd is not None and is_lessons_enabled(bd):
            cases = lookup_lessons(bd, code, __version__)
            if cases:
                guidance["cases"] = cases
    except Exception:
        pass
    out["guidance"] = guidance
    return out
