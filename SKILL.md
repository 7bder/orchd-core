# Orchd Agent

> 本文档是 **agent 工作流协议**（BOOTSTRAP / WORKER / SELF-HOSTED 三模式与纪律）。
> 面向**人**的安装、命令参考与故障排查使用指南见 [docs/user-manual.md](docs/user-manual.md)。

## Determine your mode
- If `.orchd/` exists AND `.orchd/shared/self-hosted` marker exists -> go to SELF-HOSTED (this repo only)
- If `.orchd/` exists (no self-hosted marker) -> go to WORKER
- If `.orchd/` does NOT exist -> go to BOOTSTRAP

## BOOTSTRAP mode (first agent in a new project)
1. Read `requirements.md` for the project specification
2. Run `orchd bootstrap` — outputs master schema, architect prompt, and decomposition guide
3. Create `.orchd/_master.json` following the schema and guidelines
   - If `shared` is declared, write both files before validate:
     `.orchd/shared/architecture.md` (architecture decisions) and
     `.orchd/shared/conventions.md` (coding conventions). Missing files fail validate (E005).
4. Run `orchd validate` — fix errors if any
5. Run `orchd init` — generates snapshots and empty ledger
6. Project ready. Exit. Next session enters WORKER mode automatically.

## WORKER mode (existing project)

> `orchd` 已预装在当前开发环境中，无需额外安装。

### Implementer workflow (repeat per session)
1. `orchd request --agent {your_id} [--capabilities "lang"] [--exclude "task-id ..."] [--max-active N] [--auto-claim]`
   - `--max-active N`：全局活跃（claimed）任务数达到 N 时拒绝再领取（无人值守容量控制，reason=max_active_reached）。
   - `--auto-claim`：候选非空自动认领（绕过人工确认，适合无人值守）；可与 `--with-context` 组合。
2. Present candidate to human: execute / skip / re-declare capabilities
3. If execute: `orchd claim --task {id} --agent {your_id} [--with-context]`
   - On success returns `claimed: true` + full task definition + file lists.
   - **共享上下文按需（1.1，2026-08-07）**：claim 默认不附加 shared 上下文——仅高风险领域任务（mod-core 或 files_to_edit 含 orchd/ 引擎文件 / .orchd/_master.json）自动附 conventions.md；architecture.md 仅任务 files_to_read 显式引用时提供。需要完整上下文时显式 `--with-context` 附加全部。
   - On failure the CLI exits non-zero with `{"error": {code: E008-E011, ...}}`;
     add the task id to your `--exclude` list and go back to step 1 (do not retry same task).
4. Read files_to_read -> implement -> edit files_to_edit
   - **审查期实现者冻结（R1-b，2026-08-07）**：任务进入 review（REVIEW_CLAIMED）后，任务分支上的 commit 被 L3 hook 拒绝（E017）——审查基线保护；需补提交时先让 reviewer retract 审查。
5. `orchd done --task {id} --agent {your_id} --changes "summary"`
   - Multi-line changes: write to a UTF-8 file and use `--changes-file {path}`.

### Reviewer workflow (when asked to review)
1. `orchd request --agent {your_id} --role reviewer`
   - Candidate includes `review_type` (spec or code) and review context.
   - Your agent ID MUST be in the task's `reviewers` list (defined in _master.json),
     otherwise claim is rejected (E007, response includes the list). If the list is
     wrong, have the human fix it via `orchd amend` — do not retry with another ID.
2. `orchd claim --task {id} --agent {your_id} --role reviewer`
3. Review against the phase criteria (spec: acceptance_criteria; code: conventions.md)
   - **清单化模板（M2-2，2026-08-06；证据分层 2026-08-08）**：按 `templates/spec-reviewer.md` / `templates/code-reviewer.md` 的三态/分组清单逐项勾选，每条判定必须引用证据（验收标准编号 + 引擎 verify 状态 / 定向测试结果）；spec 判定三态（满足/不满足/需人工），code 判定分"机器可辅助项（必须核验）"与"人工复核项（弱模型存疑默认放行交人工复审）"两组；禁止以逻辑推演替代测试。**证据分层：in_review = 引擎已保证 verify_command 通过（done 前提），reviewer claim 响应附带 `verify` 摘要（P2 注入：ok/exit_code/output_summary），默认引用不重跑；仅 verify 摘要缺失/不可信或 diff 触及测试链路时重跑定向测试（禁全量 pytest / build / venv 重活，120s 预算）。**
4. `orchd review --task {id} --agent {your_id} --type spec|code --verdict APPROVED|CHANGES_REQUESTED [--comments "..."]`
   - Multi-line comments: write to a UTF-8 file and use `--comments-file {path}`.
   - **code APPROVED 的 merge 前置语义**：引擎先执行 git merge，成功才写完成事件（任务 completed）。merge 冲突 → 任务**停留 in_review**（完成事件不落地，`merged: false` + `conflict_files`），由实现者（或人工）在 task 分支执行 `git merge main` 解决冲突并提交后，同一 reviewer 再次提交 code APPROVED 重试 → merge 成功 → completed。
   - **completed 语义**：= 实现 + 双阶段审查 + merge 入 main 全部完成；merge 未成功的任务不是 completed（仍为 in_review）。
   - **文档类单阶段（Q2 分级，2026-08-06；白名单收紧 2026-08-13）**：files_to_edit 全为**真文档**（`.md` / `.mdx` / `.markdown` / `.rst` / `.txt` 后缀——`docs/`、`doc/` 目录下文件同样须命中后缀白名单，示例代码/JSON/脚本等一律双阶段）且**不碰** SKILL.md / conventions.md / .orchd/_master.json（约定与状态文件）的任务，done 时直接进入 code review（跳过 spec，code 即终审）——文档修改不涉及引擎运行与约定改变；碰引擎代码 / 约定 / 状态文件 / 构建配置（`pyproject.toml`）/ CI / schema JSON 等非文档文件的任务保持 spec + code 双阶段。
5. After spec APPROVED the task automatically enters code review: run
   `orchd request --agent {your_id} --role reviewer` again, then repeat steps 2-4
   to complete the code review (both phases in the same session).

## SELF-HOSTED mode

> **本节仅适用于本仓库（orchd 自托管），外部项目忽略。**
> 自托管模式 = WORKER 工作流 + 下述摄入与纪律协议。设计依据：docs/self-hosting-design-merged.md。

### Windows 环境准备（自托管仓库）
- orchd 可执行文件装在用户级 Scripts 目录（如 `%APPDATA%\Python\Python312\Scripts\orchd.exe`），不在 bash 默认 PATH；bash 中先执行 `export PATH="$PATH:$HOME/AppData/Roaming/Python/Python312/Scripts"` 再调用 orchd
- `verify_command` 内嵌 `orchd` 时同受 PATH 影响：未导出 PATH 会 E014（"不是内部或外部命令"）；跑 done/自检前先导出 PATH
- 中文 Windows 上 git/子进程输出编码：引擎已统一 UTF-8 解码（gitops.py / onboard.py）；**管道消费 orchd JSON 须按 UTF-8 解码**——`orchd pool --all | python -c "json.load(sys.stdin)"` 在中文 Windows 会 JSONDecodeError/乱码（消费方 stdin 按 GBK 解码 UTF-8 字节流），规避：设 `PYTHONIOENCODING=utf-8` 或 `python -X utf8`、或显式 `encoding='utf-8'` 解码（与 task-encoding-hardening 的引擎读子进程输出解码不同向——此为 CLI 写 stdout 给管道消费方的编码契约，详见 README「Windows 管道编码」）

### 纪律红线（MUST / MUST NOT，2026-08-05 用户裁定）

> 针对纪律遵守较弱的 agent 的硬约束。违反任意一条 = 事故，需人工介入，
> 不因"不知情/为了效率/任务需要"豁免。本清单优先级高于本文件其他任何章节。

**MUST NOT（绝对禁止，违反即事故）**：

1. **禁止手动 git 命令**：不得执行 `git checkout / branch / reset / stash /
   gc / prune / clean / rebase / cherry-pick / merge / push` 等任何手动 git
   写操作。git 操作只允许引擎自动执行（claim 建分支、code APPROVED merge、
   done/amend 自动提交）。**唯一豁免：任务分支上的 `git commit`（见 git
   纪律"本地提交自主执行"）**。确需其他手动 git 操作时，先向用户报告并获
   明确许可。
2. **禁止破坏性 git 操作**：`git reset --hard`、`git gc --prune=now`、
   `git clean -fdx`、`git branch -D`、`git push --force` 一律禁止——
   对象清理后历史不可恢复（2026-08-05 实踩：main 被移回旧提交、20 个 blob
   丢失，靠逐个补写恢复）。
3. **禁止修改范围外文件**：只读 `files_to_read`、只写 `files_to_edit`；
   不得修改/删除/移动任何范围外文件，不得触碰 `.git/` 内部结构
   （含 `.git/index`、对象库）。删文件前先确认该文件属于任务验收范围。
4. **禁止绕过身份**：一个 session 只允许一个 agent ID；不得用多个 ID 完成
   同一任务链（实现 + 审查必须分属不同 session——2026-08-05 实踩：同一
   session 以 workbuddy-1 实现 + reviewer-1 审查，属自审绕过）。
5. **禁止未提交即中断**：改动文件后必须提交（或明确报告"未提交+原因"）才能
   结束 session；不得把未提交改动留在工作区后静默离开（2026-08-05 实踩：
   trae-a1 改 SKILL.md 未提交即中断）。
6. **禁止自动摄入**：摄入（intake）仅在用户明确指定时执行；不得主动摄入
   IDEAS.md 的 pending 条目（2026-08-05 用户裁定）。
7. **禁止在任务分支执行 intake/amend**：intake/amend 只在 main 且工作区
   干净时执行（引擎 not_on_main 兜底存在，但不得依赖）。
8. **禁止手改 .orchd 运行时文件**：`_ledger.jsonl`、`_checkpoint.json`、
   `mod-*/spec.json` 等运行时状态由引擎维护，agent 一律不得手改。
9. **禁止绕过 claim**：任务必须经 `orchd claim` 由引擎建分支；禁止手动
   `git branch/checkout` 创建任务分支（含不规范命名，如 task/task-1——
   2026-08-05 实踩：绕过 claim 手动建分支，实现悬空未 merge）。
10. **禁止任务悬空**：claim 后必须走完 done → 双阶段审查 → merge；
    中断/放弃必须先 retract，不得让任务停在 claimed/pending 且实现悬空
    （2026-08-05 实踩：task-amend-branch-guard-patch 实现悬空、
    task-merge-audit-workflow 实现未提交）。

**MUST（强制动作）**：

1. session 开始三连检查：`git status` + `git branch --show-current` +
   `orchd status`，确认分支与工作区状态后才动手。
2. 写文件前先读文件；不读不写。
3. 任务完成（done / review / amend）后，必须核对引擎响应（verify 结果、
   commit 是否执行、状态流转），确认成功后才算结束。
4. 测试/verify 一律用 `--basetemp` 指向系统临时目录，禁止项目内残留
   `pytest_tmp_*` / `.tmp-*`；session 结束确认工作区干净。
5. 任何异常（verify 失败、merge 冲突、状态不符）立即停止并报告，
   不自行猜测处置。
6. session 结束时工作区必须干净（无未提交改动），或在报告中说明。

### Session 开始：状态检查（必做）
1. `git status` + `git branch --show-current` + `orchd status`
2. 有在握实现任务（本 agent ID 的 claimed 任务）→ 回到对应 `task/{task_id}` 分支继续
3. 无在握任务 → 确认处于 main 且工作区干净，再走下面的优先级流程

### 接管中断 agent 任务（2026-08-08 补充：opencode-1 token 耗尽中断实踩）
- **识别**：`orchd status` 存在 claimed 任务，但该 agent 已不可用（session 中断/token 耗尽）；或 ledger 有该任务 CLAIMED 但无 DONE/RETRACT，且无活跃 session（session 锁超 60min watchdog 阈值）
- **标准流程**：
  1. 查 ledger 断点：`grep '<task_id>' .orchd/_ledger.jsonl | tail`——确认实现进行到哪一步（已提交？已 done？）
  2. **清理僵死锁**：`.orchd/.session.lock` 超 60min → 按 L2 watchdog 语义释放（`orchd watchdog --timeout 0` 或 Python 删除）；`.git/index.lock` 无 git 进程 → 直接删
  3. **确认实现完整性**：检查 task 分支是否有已提交实现（`git log task/{id}`）；工作区未提交改动若属于该任务 files_to_edit → 提交到 task 分支（不丢实现）
  4. **retract 原 claim**：`orchd retract --event <CLAIMED 事件 id> --agent <原 agent> --reason "中断接管"`
  5. **重新 claim**：`orchd claim --task <id> --agent {your_id}`（或按用户指示）
  6. **继续**：从 ledger 断点继续（已实现 → done；未完成 → 补实现）
- **禁忌**：不得跳过 retract 直接 done（E007 agent 不匹配）；不得丢弃原 agent 的已提交实现（先确认再接管）

### 工作优先级（按序找活，做完一件再做下一件）
1. **清审查积压**：`orchd status` 存在 in_review 且审查未被认领 → 以 `reviewer-1` 领取
2. **领实现任务**：`orchd request` → 人工确认 → `orchd claim`
3. 三者皆空 → 报告无可做工作，退出
- **摄入（intake）为手动触发**：仅在用户明确指定处理某条/某批 pending 时执行摄入协议 v2（见下）；**agent 不得主动摄入 IDEAS.md 的 pending 条目**（2026-08-05 用户裁定：摄入需主动指定，不作为领取任务处理）

### 摄入协议 v2（2026-08-05 提前落地；IDEAS.md pending → orchd 任务）

> 本协议 = 双闸门（闸门一：草案人工确认"做什么"；闸门二：claim 确认"谁做"）。
> 设计依据：docs/self-hosting-design-merged.md §4.3。约定层实现，引擎/schema 零改动；引擎化（`orchd proposal/confirm` 命令 + 状态机"待确认"态）评估留待 v1.2（触碰 §9.1 边界）。

**三条铁律（防整理偏差，缺一不可）**：

1. **原文可追溯**：每条整理结果必须能对应回原始口语（notes 保留原文或引用）；人工确认闸门看"原文 + 整理文本"对照，不看整理后文本
2. **推断项显式标注**：所有补全/猜测标注"待确认"，**禁止默认补全关键字段**（module / acceptance_criteria / files_to_edit / reviewers）
3. **全量整理后统一查重**：同一 session 摄入的全部条目必须在同一视野内完成查重，禁止跨 session 拆开处理（撞车教训的机制保证）

**流程（7 步）**：

0. **前置过滤**：只有 HTML 注释之外的 `##` 章节才算条目；注释块内的内容（含文件顶部的示例模板）一律跳过，不得摄入
1. **全量语言整理**：全部 pending 先统一整理——规范化表述、术语统一、单条单主题、推断项标注、原文可追溯（`goal:` 字段帮助判断拆解方向）
2. **统一查重（两轮）**：① **本次摄入全部条目间相互查重**——近义条目合并为单一任务定义，notes 注明合并理由与合并来源条目；② **与已有任务比对**——`orchd status` 按任务 name 比对（列表模式覆盖 pending / claimed / in_review，但不含 brief / files_to_edit）；名称无法排除的候选，直接读 `.orchd/_master.json` 比对 brief 与 files_to_edit。**撞车条目 status 处置二选一，明确写入 notes**：置 `taskified` 并入已有任务（notes 标注已有 task id），或置 `questioning` 待用户裁决（notes 列出冲突任务 id，用户裁决后置 dropped 或确认合并）；不得重复注册，不得留下"两种解读都说得通"的含糊状态
3. **冲突与依赖规划**：拆解产物逐一核对新任务与在池 pending 任务、及本次摄入任务之间的 files_to_edit 交集，冲突任务以 depends_on 串行化或合并为同一任务定义；依赖链做无环验证；杜绝注册后并行领取触发 E010 仅靠人工预警。**2026-08-08 语义更新**：`amend` 冲突校验已降级为 warning（`conflict_warnings`，不再拒绝注册），冲突硬边界在 **request 依赖感知强制过滤**——与 pending/claimed 非依赖任务冲突的候选被硬排除（`excluded_conflicts`），依赖链（祖先/子孙）共享文件的任务放行（`conflict_with`）——因此未串行化的共享文件任务对，第二个将无法领取（串行化意图由引擎强制执行，而非注册期人工预警）。**共享文件并行知情决策**（2026-08-06 实踩：6 任务并行共享 onboard.py/errors.py → code APPROVED 连续 merge 冲突）：共享引擎核心文件（onboard.py / gitops.py / cli.py / errors.py / spec.py / tests/*）的任务**默认 depends_on 串行化**；确需并行（多 agent 并发吞吐）时，必须在任务 notes 显式记录"并行理由 + 冲突风险"，不得静默并行——引擎已提供兜底（request excluded_conflicts + merge 自动化解，见 ROADMAP 1.1 L1/L3）
4. **生成任务定义草案清单**：产物是完整任务定义草案（存放 `.orchd/proposals/<task_id>.json`，已被 .gitignore 忽略，过程产物不入库）。必填：id（`task-` 前缀、小写字母数字连字符）、name、brief、module、acceptance_criteria、files_to_edit、reviewers（统一 `["reviewer-1"]`）；推荐补 depends_on 与 verify_command（见自检约定）。**exempt_files 字段（2026-08-08 新增，可选）**：任务实现**必然连带修改但不在 files_to_edit 内**的文件（典型：新增错误码 → 连带 `tests/test_errors.py` 计数断言；schema 字段 → 连带 schema 相关测试）可声明 `exempt_files: [...]`——hook 豁免、不占 files_to_edit 额度；**连带文件必须显式声明**（files_to_edit 或 exempt_files 二选一），否则 E020 hook 拦截 + validate_quality E026 预警。**拆分粒度按模块独立性**（粒度锚点见下），参考 `.orchd/shared/architecture.md` 与 `templates/architect.md`，拆出几个算几个
5. **人工确认/修改（闸门一：确认"做什么"）**：向用户呈现"原文 + 整理文本 + 任务定义草案"对照清单，用户确认或修改后才 `orchd amend` 注册。信息不足 → 草案内列具体问题置 `status: questioning`，用户回答即确认；**只有用户可置 dropped**；超出 orchd 能力范围（需外部服务、采购）置 questioning 说明原因
6. **注册**：仅在 main 且工作区干净时执行 `orchd amend`（**"工作区干净"判定：以无已跟踪文件改动为准，untracked 工具/配置文件（如 .workbuddy/、reasonix.toml）不阻塞 intake/amend/claim**）；成功后**立即提交** `.orchd/_master.json` 与当次 IDEAS.md 变更；条目置 `status: taskified`，notes 记录 task id、整理后规范表述、推断项与来源 commit
7. **接续（闸门二：确认"谁做"）**：注册完成后走 `orchd request` → 人工确认 → `orchd claim`，确认领取者后转入优先级 2/3

**任务拆解粒度启发式（锚点，防过度合并/过度拆分）**：

- **下限**（不得更细）：单任务 `files_to_edit` ≥ 1；acceptance_criteria 至少 1 条可机器验证（verify_command 能命中）；不接受纯注释/纯文档单独立任务（除非文档专项模块如 mod-docs）
- **上限**（不得更粗）：单任务 `files_to_edit` ≤ 5 个独立文件（超出优先按模块独立性拆分）；`estimated_hours` ≤ 8（超出按依赖链串行化为 depends_on 队列）；`acceptance_criteria` ≤ 6 条（超出说明任务内聚性不足）。**exempt_files 不占 files_to_edit 额度（2026-08-08）**：必要连带豁免声明（如新增错误码连带 tests/test_errors.py）计入 exempt_files 而非 files_to_edit，不计入 5 文件上限
- **判断标准**：以"单 agent session 一次完成 + 单 reviewer 一次审完"为锚点；预估上下文 > 30K tokens 过大、< 30 分钟完成过小
- **拆分优先原则**：按模块独立性拆（mod-core / mod-docs / mod-cli / mod-packaging 各自独立任务）优先于按步骤拆；文件冲突任务用 depends_on 串行化，不合并为单任务

### 审查者 ID 约定（禁止自审）
- 实现任务用各 agent 唯一 ID，命名规范 **MUST** 遵守 `{provider}-{序号}`（provider 为平台/工具名小写，序号为数字，如 qoder-a1、claude-x、codex-1、workbuddy-1），**禁止跨 provider 复用同一序号**（如 qoder-a1 与 claude-a1 视为冲突）；审查一律以固定 ID `reviewer-1` 领取
- **引擎层强制阻断**（task-claim-reviewer-independence，2026-08-06 落地）：
  - **E016 self_review_blocked**：claim review 时，若该任务 DONE 事件的 `agent_id` 与当前 claim agent 相同，引擎拒绝并返回换 agent 指引
  - **Review 优先调度**：implementer 请求任务时（`orchd request --agent X`），若存在该 agent 可认领的 in_review 任务，引擎返回 `next_action: "review_first"` + `review_priority` 提示先领取审查
- 领审查前两项自查（引擎已覆盖核心阻断，此为双重保险）：
  - `orchd status` 中不存在本 session 实现 ID 名下的 claimed 任务（busy 检查按 ID 判定，换 ID 即可绕过，故必须先自查）
  - 读取目标任务实现侧 `claimed_by`，与本 session 实现 ID 相同 → 跳过该任务
- 任务 completed 后，把实现者 ID 回写对应 IDEAS 条目 notes（弥补共享 reviewer-1 导致的审计缺口）。**责任方：完成 code review APPROVED 的 reviewer session**——在 APPROVED 后立即从 `orchd status` 读取该任务 `claimed_by`，写入对应 IDEAS 条目 notes（注明 `{agent_id}，reviewer-1 审查通过`），不再依赖人工/专门 commit
- **code review APPROVED 后必须运行 merge audit 验证**：提交 code APPROVED 且 merge 成功（任务进入 completed）后，立即运行 `orchd status --audit-merge`，确认 `merge_audit.warnings` 为空（零告警）。若有告警（completed 任务对应分支仍悬空未入 main），立即在当前 reviewer session 内 cherry-pick 修复并重新验证，不得将漏 merge 遗留到下游

### 自检约定（verify_command）
- **⏱ 120s 预算硬约束（2026-08-08 新增）**：引擎 verify 上限 `_VERIFY_TIMEOUT=120s`（onboard.py），**verify_command 必须在 120s 内完成**——写 verify_command 时先预算：模块定向 pytest（只跑相关文件，秒级）+ 轻量断言；**禁止** `python -m build` / `pip install` / `venv` / 全量 pytest（无 -k/-p 定向）等重命令段（重活留给 CI，不在 verify 跑）。2026-08-08 实踩两例：task-auto-claim 全量 pytest 210s 超时、task-release-pipeline build+venv 段 144.7s 超时 → done E014 卡死
- **代码类：模块定向，禁全量 pytest**（2026-08-06 优化：全量 `pytest tests/` 约 30s，累计 210s 超出引擎 verify 超时上限 → done 必卡死；且随测试膨胀线性恶化）：`python -m pytest tests/test_<涉及模块>.py [相关测试文件] -q --basetemp="${TMPDIR:-/tmp}/orchd-vf-$$" && python -m orchd.cli validate .orchd/_master.json`——定向文件 = files_to_edit 中 `orchd/x.py` 对应 `tests/test_x.py` + 显式列出的 tests/ 文件
- 触及高风险区域（状态机分支、CLI 契约、锁协议）追加第三环：`&& python -m orchd.cli status`
- **文档 / 基础设施类**（files_to_edit 不含 .py/orchd/ 代码）：文件存在/内容断言（`python -c "..."`），**不跑 pytest 全量**；必须非空
- `orchd` 命令统一用 `python -m orchd.cli` 形式（bash PATH 无 orchd，避免 E014）
- **cmd 兼容**：verify_command 用纯 `cmd1 && cmd2` 链，**禁止** `;` 分隔与嵌套 `python -c "..."` 引号（JSON→cmd→shell 三层转义易失效 → SyntaxError，2026-08-08 实踩 task-release-pipeline）

### git 纪律（引擎 best-effort 建分支/merge + done/amend 自动提交，从不 push）
- **实现者**：实现过程中可自行多次提交（细粒度保留）；未提交的 `files_to_edit` 范围内改动由引擎在 `done`（verify 通过后）自动兜底提交，不重复提交、不 squash；verify 失败不影响已产生的提交，修复后追加提交再重试
- **intake/amend**：只在 main 执行；amend 成功后引擎自动提交 `.orchd/_master.json` 与 IDEAS.md（避免脏 master 被 checkout -b 带进任务分支）；仓库停在任务分支上时不做 intake。引擎兜底：若误在非 main 分支执行 amend，`_get_current_branch()` 检测当前分支，非 main 时 `commit` 响应 `{"performed": false, "reason": "not_on_main", "branch": "<当前分支名>"}` 降级为不提交（注册不受影响）；非 git 仓库或 git 不可用时回退为正常提交路径
- **claim 前提**：处于 main 且工作区干净（**"干净"= 无已跟踪文件改动；untracked 工具/配置文件不阻塞**）；引擎从当前 HEAD 建分支，上个任务未 merge 归还会导致 base 错误
- **审查者**：领取前确认处于对应 task 分支且工作区干净；审查对象是分支上的已提交 diff
- **本地提交自主执行**：任务分支上的 `git commit` 是协议动作，agent 直接执行、无需管理员确认（纪律红线唯一豁免的手动 git 命令）；只提交协议范围内（files_to_edit / IDEAS.md 回写等）改动，不 push
- **不 push**：远端推送不在 agent 职责内，由项目管理员负责
- **L3 pre-commit hook 生命周期**（2026-08-08 语义升级）：claim 时安装到真实仓库 `.git/hooks/pre-commit`，**任务活跃时任何分支**都校验 staged ⊆ files_to_edit ∪ exempt_files（堵住 main/幽灵分支越界提交实现内容）；任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行；`--no-verify` 可绕过。**固定资产豁免**：`.orchd/_master.json`、`IDEAS.md`（amend 自动提交路径，main 分支提交不被拦）。**exempt_files 豁免（2026-08-08 新增）**：任务定义可声明 `exempt_files`（必要连带文件，如新增错误码连带更新的 `tests/test_errors.py` 断言），claim 安装期即随 hook 生效（staged 文件 ∈ exempt_files 放行）；豁免文件**引擎 ensure_committed 不兜底提交**——实现者须自行 git commit，done 后 `require_clean` E017 兜底。done 执行 verify_command 前临时卸载、verify 后重装（避免 verify 期间真实仓库 git 操作被误伤），done 末尾 / retract 真正卸载

### 仓库事故恢复（git 对象/refs 丢失 SOP，2026-08-08 沉淀）
> 2026-08-06 repack 事故 + 2026-08-08 refs/ 目录被删 + loose objects 丢失，两次实踩后沉淀。
> 事故模式复现：`.git/refs/` 目录被删、08-04 之后 loose objects 全部丢失（pack 仅达历史某点）、
> reflog 完整但指向对象 invalid、`git status` 报 not a git repository。工作区文件与 .orchd ledger 通常完整，
> 恢复核心是**用写入覆盖重建基线，不依赖删除**（沙箱 safe-delete 拦删除不拦写入）。

**六步恢复流程**：

1. **诊断**——`git status` 报 not a git repository 时，依次查：`.git/refs/`（目录是否缺失）、`.git/packed-refs`（是否仍含旧 ref）、`.git/logs/HEAD`（reflog，完整则含最新提交哈希）、`git verify-pack`（pack 中可用 commit）。
2. **确认损失边界**——对照 reflog 哈希逐个 `git cat-file -t`，找出对象真正丢失的最新 commit（reflog 有哈希但不代表对象还在）。
3. **备份现场**——`cp -r .git .git.damaged-<date>` 留存元数据。**注意**：沙箱环境下 cp 可能静默丢 pack，备份不完整，仅作元数据留存，不作为恢复依据。
4. **沙箱 safe-delete 绕过法（关键）**——WorkBuddy 沙箱拦截 `.git` 内删除（SAFE_DELETE_BULK_CONFIRM_REQUIRED / trash-failed），`rm -rf` / `shutil.rmtree` / Remove-Item 均被拦。**绕过思路：不删旧 refs，用写入覆盖**——`git write-tree`（或空树 `4b825dc`）+ `git commit-tree <tree> -m <msg>` 构建基线 commit + `git update-ref refs/heads/main <sha>` 覆盖无效 ref + `git symbolic-ref HEAD refs/heads/main`。
5. **重建提交**——`git add -A && git commit` 以工作区内容重建单一基线（实证 aa08c2d，62 files/21229 insertions）。
6. **预防清单**——a) 定期 `git bundle` 备份（**bundle 是单文件快照，不受沙箱逐文件拦截影响**，替代 cp .git）；b) 仓库健康检查纳入 session 三连检查（`git fsck --full` 快速扫描）；c) 事故后对工作区 `git status` 确认无未提交残留混入基线。

> 关联：IDEAS L272（unlink 沙箱拦截，同源环境限制——safe-delete 拦删除不拦写入，是本法依据）；
> 工具化检测见 `orchd doctor`（task-git-doctor-command）。

### 安全边界（详见 docs/self-hosting-design-merged.md §9）
- 禁止走自托管的"停服升级"（人工执行）：schema required 字段与既有枚举语义、事件格式与 `_apply_event` 语义、spec.py 校验逻辑主干
- 任何把内容域假设写死进引擎的改动同样归入停服边界
- 高风险但可走管线的改动（状态机分支 / CLI 契约 / 锁协议）：三连自检 + files_to_read 含相关设计文档章节 + 审查者对照本规范逐条核对

## Rules
- One task per session. Exit after `orchd done`.
- Reviewer exception: complete both review phases (spec + code) of one task in the same session.
- Maintain --exclude list across retries. Feed it back to step 1.
- `--role` defaults to `implementer`. Use `--role reviewer` for reviews.
- Reviewer agent ID must match an entry in the task's `reviewers` field.
- Never read files outside the file list provided by CLI (WORKER mode). In BOOTSTRAP mode, reading `requirements.md` is required.
