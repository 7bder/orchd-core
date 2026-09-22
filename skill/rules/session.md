---
guide:
  lesson_review: 2
  check_status: 1
  rework_first: 2
  request_impl: 1
  done: 3
  claimed_impl: 1
  changes_requested: 2
  warning_unmapped: 1
---
# Session 规则（状态检查 / 接管 / 优先级 / claim 细节）

> TL;DR: ① session 开始三连检查（git status + branch + status）② 有在握任务回 task 分支继续，无则 main 且工作区干净 ③ 优先级：清审查积压→领实现→三者皆空即停不重试（**自动执行时"清审查"为硬默认：先做完审查闭环再领实现**）④ claim 两段式需 --confirm，auto-claim 默认禁用 ⑤ 摄入仅用户指定

> 原 .orchd/SKILL.md「Session 开始」「接管中断 agent 任务」「工作优先级」及 WORKER implementer workflow 的 claim 细节，外置自 task-skill-hub-refactor。

## Session 开始：状态检查（必做）
1. `git status` + `git branch --show-current` + `python .orchd/__main__.py status`
2. 有在握实现任务（本 agent ID 的 claimed 任务）→ 回到对应 `task/{task_id}` 分支继续
3. 无在握任务 → 确认处于 main 且工作区干净，再走下面的优先级流程

## 转述载体约定（task-guidance-dual-view-docs，2026-08-19）
- **向用户转述 guidance 的两种载体**（agent 是用户与引擎之间的转述者，不得吞掉引导）：
  - **初始化**（`guidance` 含 `card` 字段，`step=first_time`）：由接入层渲染 **SVG 全貌卡片**（card 的 `title/phase/steps/current/next` 结构化数据驱动），呈现整个初始化路径。
  - **日常**（任意命令的 `guidance`）：用 **Markdown 引用块 + 粗体** 转述两条——`project_view`（项目整体视角：`step/command/hint`）与 `agent_view`（当前 agent 视角，顶层 5 键即其值）；两者相同时只转述一条。
- **转述时机**：① 用户主动问「下一步」；② agent 完成任务后需用户决策下一步时显式提示（不得静默结束）。

## 接管中断 agent 任务（2026-08-08 补充：opencode-1 token 耗尽中断实踩）
- **识别**：`python .orchd/__main__.py status` 存在 claimed 任务，但该 agent 已不可用（session 中断/token 耗尽）；或 ledger 有该任务 CLAIMED 但无 DONE/RETRACT，且无活跃 session（session 锁超 60min watchdog 阈值）
- **标准流程**：
  1. 查 ledger 断点（用 python 替代 grep|tail，跨平台无 POSIX 工具依赖）：`python -c "import pathlib; ls=[l for l in pathlib.Path('.orchd/_ledger.jsonl').read_text(encoding='utf-8').splitlines() if '<task_id>' in l]; print(chr(10).join(ls[-10:]))"`——确认实现进行到哪一步（已提交？已 done？）
  2. **清理僵死锁**：`.orchd/.session.lock` 超 60min → 按 L2 watchdog 语义释放（`python .orchd/__main__.py watchdog --timeout 0` 或 Python 删除）；`.git/index.lock` 无 git 进程 → 直接删
  3. **确认实现完整性**：检查 task 分支是否有已提交实现（`git log task/{id}`）；工作区未提交改动若属于该任务 files_to_edit → 提交到 task 分支（不丢实现）
  4. **retract 原 claim**：`python .orchd/__main__.py retract --event <CLAIMED 事件 id> --reason "中断接管"`（身份由引擎自动识别当前会话指纹）。**前提（E034 撤认归属守卫）**：跨 agent 撤认他人事件仅当目标认领已超时（僵尸）时放行——`CLAIMED` 超时阈值 `claim_stale_timeout_s()`（默认 **600s**，`ORCHD_CLAIM_STALE_SECS` 可覆盖）、`REVIEW_CLAIMED` 超时阈值 `review_stale_timeout_s()`（`ORCHD_REVIEW_STALE_SECS` 可覆盖）；**未超时撤认他人 CLAIMED 会被 E034 拒绝**（仅事件作者本人或 admin 可撤），此时应等待其退出或走 `force-status` 控制面，不得重试硬撤。另注意：`retract` 默认 `disposition=abandon` 触发 **300s 认领冷却**（`_RETRACT_COOLDOWN_S`），冷却期内重新 `claim` 被拒，需加 `--force` 绕过；`disposition=retry` / `handoff` 不触发冷却。
  5. **重新 claim**：`python .orchd/__main__.py claim --task <id> --confirm`（身份由引擎自动识别当前会话指纹；或按用户指示）
  6. **继续**：从 ledger 断点继续（已实现 → done；未完成 → 补实现）
- **禁忌**：不得跳过 retract 直接 done（E007 agent 不匹配）；不得丢弃原 agent 的已提交实现（先确认再接管）

## 双布局（container / flat）
- **container（默认，多 worktree 并行）**：仓库根为容器，**主工作树在 `main/` 子目录**（带 `(main)` 标记），任务 worktree 为容器根的 `task-<id>/` 独立目录；账本运行时在 `<容器>/.orchd-runtime/`（可 `ORCHD_HOME` 重定向）。**agent 只在任务 worktree 内工作，绝不触碰 main**——merge 由引擎在主工作树执行。
- **flat（单 worktree）**：无 `main/` 子目录，工作树即仓库根；账本运行时在 `.orchd/`。任务认领 / done / review 行为与 container 完全一致（零回归）。
- **定位主工作树**：`git worktree list` 中带 `(main)` 标记的路径；container 布局可从任务 worktree 路径上溯到项目根下的 `main/` 目录，flat 布局主工作树即仓库根本身、无需上溯（见 rules/git.md「持任务 amend 补登」三步流程）。

## 工作优先级（按序找活，做完一件再做下一件）
1. **清审查积压**：`python .orchd/__main__.py status` 存在 in_review 且审查未被认领 → 以当前会话指纹领取
2. **领实现任务**：`python .orchd/__main__.py request` → 人工确认 → `python .orchd/__main__.py claim`
3. 三者皆空 → **立即停止并报告**，不自行重试 `request`、不自行 `claim`、不 `--auto-claim`；等待用户下一条指令（引擎分配为准，无候选即停）
- **自动执行时"清审查"为硬默认（2026-09-20 用户裁定）**：无人值守 / 连续自动跑圈时，池内凡有**可领审查**（in_review 且审查未被认领），**必须先走完审查闭环（claim review → `review` 提交结论 → 尚有 code 阶段则继续到 merged）再领实现**——"清审查"= **做完**，不是只认领；不得把优先级 1 当成一句可跳过的提示。
- **禁止以点名 claim 绕过审查优先**：池内存在可领审查时，**不得用 `claim --task <pending-id>` 摘实现任务**——`request` 有 `review_first` 闸门（`candidate: null` + `next_action: "review_first"` + `blocked_by: "review_priority"`），而 `claim --task` 只按该任务自身状态分流（pending → 实现认领）、**不查全局审查积压**，点名即绕过（2026-09-20 实测：3 个 in_review 因此积压数小时未被领，`request` 早已给出 review_first 而从未被触发）。仅当审查**确实无人可领**（任务 `reviewers` 名单不含本指纹 / 已被他指纹认领 / 本会话 E011 busy）时，才落到优先级 2。
- **摄入（intake）为手动触发**：仅在用户明确指定处理某条/某批 pending 时执行摄入协议 v2（见 rules/intake.md）；**agent 不得主动摄入 IDEAS.md 的 pending 条目**（2026-08-05 用户裁定：摄入需主动指定，不作为领取任务处理）

## 在途冲突与候选可见性（2026-09-12）

- **`conflict_with` 可能含"在途"条目**（`source=inflight`，含 `files` / `other_status` / `policy`）：
  在途 = 分支有未落 main 的改动（done / in_review / force-status 悬空态），**真源是 git 事实**，与状态字段无关。
- **`config.conflict_policy` 缺省 `warn`**：warn 下在途重叠**仅提示 + 降权排序，不阻断领取**——收到
  `file_conflict_inflight` 警告时**不得据此跳过 `claim --confirm` 确认闸门**，也不得自行串行化或放弃候选；
  改 `serialize` / `block` 后候选才会被硬排除（此时该候选不出现在可领取集合，按"request 无候选即停止"处理）。
  `claimed` 任务的硬排除与 policy 无关。
- **冲突文件清单以引擎返回的真实路径为准**（真源 = git 未合并路径）。若见到 `tree.` / `HEAD.` / `main.`
  之类路径，那是旧 `CONFLICT` 行末词启发式的产物（已降级为诊断 fallback），**立即停止并按异常报告**，
  不要按该路径执行 git 操作。

## claim 细节（claim 两段式 / 共享上下文 / 失败处理 / 审查冻结）
- **确认闸门（task-claim-confirm-gate，2026-08-14）**：无 `--confirm` 时 claim 仅输出预览（`confirm_required: true` + 任务基本信息 / 当前状态 / git 状况 / 将执行动作 / 预期校验），**不写事件、不建分支**——防误领/误执行；核对无误后加 `--confirm` 真正执行（写 CLAIMED 事件 + 建分支）。
- **auto-claim 默认禁用（2026-08-16）**：`request --auto-claim` 无人值守自动认领**默认拒绝**（E032 `auto_claim_disabled`），仅当 `_master.json` 顶层 `config.allow_auto_claim` 显式为 `true`（用户明确授权）时 agent 才可调用。agent **不得**擅自用 `--auto-claim` 连续领任务绕过人工确认。
- **共享上下文按需（1.1，2026-08-07）**：claim 默认不附加 shared 上下文——仅高风险领域任务（mod-core 或 files_to_edit 含 orchd/ 引擎文件 / .orchd/_master.json）自动附 conventions.md；architecture.md 仅任务 files_to_read 显式引用时提供。需要完整上下文时显式 `--with-context` 附加全部。
- **返工增量读（A5，2026-09-13，读取纪律）**：rework（被 CHANGES_REQUESTED 打回）后重领实现时，**优先消费 claim 响应已附带的 `review_comments` 与变更文件（git diff），不重读全部 `files_to_read`**——claim 响应已含上轮审查意见与实现基线，重读全文既耗时又偏离返工焦点；仅在 `review_comments` / `previous_changes` 缺失或需确认具体上下文时，才按 `files_to_read` 定向补读。
- **失败处理**：claim 失败 CLI exits non-zero with `{"error": {code: E008-E011, ...}}`；**停止并报告失败原因，不自行重试**（不把 task id 加 `--exclude` 后回 request 重试——引擎分配为准，无候选/失败即停，等待用户下一条指令）。
- **审查期实现者冻结（R1-b，2026-08-07）**：任务进入 review（REVIEW_CLAIMED）后，任务分支上的 commit 被 L3 hook 拒绝（E017）——审查基线保护；需补提交时先让 reviewer retract 审查。
- **`claim --type` 取值域**（task-session-start-token-handoff）：审查认领时 `--type` 实际取值为 `spec` / `code`（**不含 `review`**）。`spec` = 规格审查阶段（验收标准/边界/设计），`code` = 代码审查阶段（实现质量/测试/越界），`unified` 单阶段模式下**省略** `--type`（guide.py 生成 `claim --task X --type {phase}` 时 phase 取自任务 `review_phase` 字段；unified 分支不附加该参数，与 `_review_step_guidance` 口径一致）。实现任务认领不传 `--type`（默认识别为实现）。

## 身份约定（会话级指纹）

agent 会话用**会话级指纹**作为身份 id：12 位 hex（SHA-256 短哈希，如 `a1b2c3d4e5f6`），由宿主注入的每对话唯一会话标识派生（`orchd.ledger.resolve_agent_id`）。

- **会话生命周期**：`orchd session start [--agent NAME]` 生成唯一 `session_id` 与 `session_token`，写入 `.orchd-runtime/sessions/<session_id>.json`；宿主把 `session_token` 注入 `ORCHD_SESSION_ID`，该会话内所有命令恒同身份。`session current` 查看当前会话；`session end` 结束会话并释放会话锁（flock 活性锁，进程异常退出后引擎自动清理，无需手工清锁）。
- **宿主接入**：TRAE 会话由 `ICUBE_CODEMAIN_SESSION` 自动搬运到 `ORCHD_SESSION_ID`（开箱即用）；codex / opencode / workbuddy 等由各自接入层在会话启动时调用 `orchd session start` 并把 session_token 写入 `ORCHD_SESSION_ID`。
- **`.agent_id` 已废除**：引擎不再读写该文件，未注入 `ORCHD_SESSION_ID` 时不生成、不借用、不落盘任何身份（写命令拒绝并提示先 `session start`；只读命令可匿名运行）。存量历史 `.agent_id` 文件不再参与身份判定。
- **会话级判定**：归属 / 忙度 / 自审 / 锁所有权均以 `session_id` 为主键；同 agent 不同 session 视为不同身份，可并行领取不同任务。
- **指纹生命周期**：同一对话内指纹与 `session_id` 永不变；不同对话（不同 `ORCHD_SESSION_ID` / 不同 session runtime）返回不同指纹。切换对话即可获得新身份——这是「换对话领 review」的机制保证。
- **宿主注入契约**：`ORCHD_SESSION_ID` 最好是 `session start` 返回的 `session_token`；若宿主自行注入，必须是**会话级**标识——每个对话启动时生成唯一值，并在该对话所有命令中保持不变。项目级/工作区级或其他跨对话共享的标识不符合契约。判定标准：同一对话命令得到同一指纹，不同并行对话即使位于同一项目/工作区也必须得到不同指纹。
- **宿主违约后果**：多个对话共享项目级指纹时，引擎会把并行工作误判为同一身份，造成任务归属混淆、E011 单任务忙度冲突、E016 自审纠缠。发现同指纹并行时应先核对宿主注入粒度并切换到正确的会话级标识，不得通过伪造 agent ID 绕过身份校验。
- **E021 豁免**：12 位 hex 形态的 agent_id 视为自动化会话身份，不与人名 `git user.name` 硬比对，`claim` / `done` / `review` 不触发 E021 `identity_mismatch` warning。
- **指纹 vs 具名身份**：宿主受管自动化会话用指纹作身份锚定；具名 agent 身份（如 `marvis-1`、`workbuddy-1`）用于人工可追溯场景。
- **自审降级与分级策略**（task-self-review-independence-policy，D7 裁定）：实现 + 审查可在同一指纹下完成，引擎在认领结果附 `self_review_notice`、request 候选标注 `is_self_review`，不参与任何流程决策；决策权在人（调度者）。线上版可设 `_master.json config.enforce_self_review_block=true` 恢复 E016 硬阻断（详见 rules/review.md）。**分级建议（非强制，2026-09-19 按用户裁决收口）**：引擎语义 / 门禁行为变更 / 错误码语义 / 状态机类任务**建议**换一个独立会话（不同指纹）担任审查者——引擎当前**无分级实现**（只有全局 `enforce_self_review_block` 开关，默认仅提示、不硬阻断），故本判据写成建议而非禁令；低风险任务（纯文档 / 纯测试 / 不触及上述类别的局部实现）可自审。**硬约束升级（2026-09-21 用户裁定，D1-③＋）**：上述四类高危任务**必须**异指纹 reviewer（由调度者分派，引擎 `self_review_notice` + E035 碰撞告警自动识别）；同指纹提交的审查结论视为无效，须换会话重审。**自审时必附三项披露（流程纪律）**：① review comments 首句披露自审（`实现者 = 审查者 = <指纹>`）② 证伪性探针（主动构造反例/边界并记录结果）③ 全量回归证据（verify_command 全绿 + 触及测试链路时重跑定向测试）。完整建议表与可执行命令示例见 `shared/conventions.md`「审查者 ID 约定与分级自审策略」。
