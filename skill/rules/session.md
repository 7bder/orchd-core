# Session 规则（状态检查 / 接管 / 优先级 / claim 细节）

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
  1. 查 ledger 断点：`grep '<task_id>' .orchd/_ledger.jsonl | tail`——确认实现进行到哪一步（已提交？已 done？）
  2. **清理僵死锁**：`.orchd/.session.lock` 超 60min → 按 L2 watchdog 语义释放（`python .orchd/__main__.py watchdog --timeout 0` 或 Python 删除）；`.git/index.lock` 无 git 进程 → 直接删
  3. **确认实现完整性**：检查 task 分支是否有已提交实现（`git log task/{id}`）；工作区未提交改动若属于该任务 files_to_edit → 提交到 task 分支（不丢实现）
  4. **retract 原 claim**：`python .orchd/__main__.py retract --event <CLAIMED 事件 id> --reason "中断接管"`（身份由引擎自动识别当前会话指纹）
  5. **重新 claim**：`python .orchd/__main__.py claim --task <id> --confirm`（身份由引擎自动识别当前会话指纹；或按用户指示）
  6. **继续**：从 ledger 断点继续（已实现 → done；未完成 → 补实现）
- **禁忌**：不得跳过 retract 直接 done（E007 agent 不匹配）；不得丢弃原 agent 的已提交实现（先确认再接管）

## 工作优先级（按序找活，做完一件再做下一件）
1. **清审查积压**：`python .orchd/__main__.py status` 存在 in_review 且审查未被认领 → 以当前会话指纹领取
2. **领实现任务**：`python .orchd/__main__.py request` → 人工确认 → `python .orchd/__main__.py claim`
3. 三者皆空 → 报告无可做工作，退出
- **摄入（intake）为手动触发**：仅在用户明确指定处理某条/某批 pending 时执行摄入协议 v2（见 rules/intake.md）；**agent 不得主动摄入 IDEAS.md 的 pending 条目**（2026-08-05 用户裁定：摄入需主动指定，不作为领取任务处理）

## claim 细节（claim 两段式 / 共享上下文 / 失败处理 / 审查冻结）
- **确认闸门（task-claim-confirm-gate，2026-08-14）**：无 `--confirm` 时 claim 仅输出预览（`confirm_required: true` + 任务基本信息 / 当前状态 / git 状况 / 将执行动作 / 预期校验），**不写事件、不建分支**——防误领/误执行；核对无误后加 `--confirm` 真正执行（写 CLAIMED 事件 + 建分支）。
- **auto-claim 默认禁用（2026-08-16）**：`request --auto-claim` 无人值守自动认领**默认拒绝**（E032 `auto_claim_disabled`），仅当 `_master.json` 顶层 `config.allow_auto_claim` 显式为 `true`（用户明确授权）时 agent 才可调用。agent **不得**擅自用 `--auto-claim` 连续领任务绕过人工确认。
- **共享上下文按需（1.1，2026-08-07）**：claim 默认不附加 shared 上下文——仅高风险领域任务（mod-core 或 files_to_edit 含 orchd/ 引擎文件 / .orchd/_master.json）自动附 conventions.md；architecture.md 仅任务 files_to_read 显式引用时提供。需要完整上下文时显式 `--with-context` 附加全部。
- **失败处理**：claim 失败 CLI exits non-zero with `{"error": {code: E008-E011, ...}}`；把 task id 加入 `--exclude` 后回 request 步骤 1（不要对同一任务重试）。
- **审查期实现者冻结（R1-b，2026-08-07）**：任务进入 review（REVIEW_CLAIMED）后，任务分支上的 commit 被 L3 hook 拒绝（E017）——审查基线保护；需补提交时先让 reviewer retract 审查。

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
- **自审降级**：实现 + 审查可在同一指纹下完成，引擎在认领结果附 `self_review_notice`、request 候选标注 `is_self_review`，不参与任何流程决策；决策权在人（调度者）。线上版可设 `_master.json config.enforce_self_review_block=true` 恢复 E016 硬阻断（详见 rules/review.md）。
