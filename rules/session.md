# Session 规则（状态检查 / 接管 / 优先级 / claim 细节）

> 原 .orchd/SKILL.md「Session 开始」「接管中断 agent 任务」「工作优先级」及 WORKER implementer workflow 的 claim 细节，外置自 task-skill-hub-refactor。

## Session 开始：状态检查（必做）
1. `git status` + `git branch --show-current` + `python .orchd/__main__.py status`
2. 有在握实现任务（本 agent ID 的 claimed 任务）→ 回到对应 `task/{task_id}` 分支继续
3. 无在握任务 → 确认处于 main 且工作区干净，再走下面的优先级流程

## 接管中断 agent 任务（2026-08-08 补充：opencode-1 token 耗尽中断实踩）
- **识别**：`python .orchd/__main__.py status` 存在 claimed 任务，但该 agent 已不可用（session 中断/token 耗尽）；或 ledger 有该任务 CLAIMED 但无 DONE/RETRACT，且无活跃 session（session 锁超 60min watchdog 阈值）
- **标准流程**：
  1. 查 ledger 断点：`grep '<task_id>' .orchd/_ledger.jsonl | tail`——确认实现进行到哪一步（已提交？已 done？）
  2. **清理僵死锁**：`.orchd/.session.lock` 超 60min → 按 L2 watchdog 语义释放（`python .orchd/__main__.py watchdog --timeout 0` 或 Python 删除）；`.git/index.lock` 无 git 进程 → 直接删
  3. **确认实现完整性**：检查 task 分支是否有已提交实现（`git log task/{id}`）；工作区未提交改动若属于该任务 files_to_edit → 提交到 task 分支（不丢实现）
  4. **retract 原 claim**：`python .orchd/__main__.py retract --event <CLAIMED 事件 id> --agent <原 agent> --reason "中断接管"`
  5. **重新 claim**：`python .orchd/__main__.py claim --task <id> --agent {your_id}`（或按用户指示）
  6. **继续**：从 ledger 断点继续（已实现 → done；未完成 → 补实现）
- **禁忌**：不得跳过 retract 直接 done（E007 agent 不匹配）；不得丢弃原 agent 的已提交实现（先确认再接管）

## 工作优先级（按序找活，做完一件再做下一件）
1. **清审查积压**：`python .orchd/__main__.py status` 存在 in_review 且审查未被认领 → 以 `reviewer-1` 领取
2. **领实现任务**：`python .orchd/__main__.py request` → 人工确认 → `python .orchd/__main__.py claim`
3. 三者皆空 → 报告无可做工作，退出
- **摄入（intake）为手动触发**：仅在用户明确指定处理某条/某批 pending 时执行摄入协议 v2（见 rules/intake.md）；**agent 不得主动摄入 IDEAS.md 的 pending 条目**（2026-08-05 用户裁定：摄入需主动指定，不作为领取任务处理）

## claim 细节（claim 两段式 / 共享上下文 / 失败处理 / 审查冻结）
- **确认闸门（task-claim-confirm-gate，2026-08-14）**：无 `--confirm` 时 claim 仅输出预览（`confirm_required: true` + 任务基本信息 / 当前状态 / git 状况 / 将执行动作 / 预期校验），**不写事件、不建分支**——防误领/误执行；核对无误后加 `--confirm` 真正执行（写 CLAIMED 事件 + 建分支）。`request --auto-claim` 无人值守路径内部跳过确认。
- **共享上下文按需（1.1，2026-08-07）**：claim 默认不附加 shared 上下文——仅高风险领域任务（mod-core 或 files_to_edit 含 orchd/ 引擎文件 / .orchd/_master.json）自动附 conventions.md；architecture.md 仅任务 files_to_read 显式引用时提供。需要完整上下文时显式 `--with-context` 附加全部。
- **失败处理**：claim 失败 CLI exits non-zero with `{"error": {code: E008-E011, ...}}`；把 task id 加入 `--exclude` 后回 request 步骤 1（不要对同一任务重试）。
- **审查期实现者冻结（R1-b，2026-08-07）**：任务进入 review（REVIEW_CLAIMED）后，任务分支上的 commit 被 L3 hook 拒绝（E017）——审查基线保护；需补提交时先让 reviewer retract 审查。
