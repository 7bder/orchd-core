---
guide:
  claim_review: 1
  submit_review: 1
  rework_first: 1
  wait_review: 1
  audit_merge: 1
  done_submitted: 1
  approved_completed: 1
  changes_requested: 1
---
# 审查规则（ID 约定 / 自审默认阻断 / 证据分层 / merge 前置 / 单阶段判定）

> TL;DR: ① 审查须由**当前会话 spawn 的独立 subagent** 执行（异指纹只是下限；三条禁止 + 无 subagent 能力即中断，见「审查者身份约定」）——引擎门已落地：自审恒 `E016 self_review_blocked`（claim 与 submit 双侧、无任何开关）、审查会话 lineage 硬门（kind=reviewer + parent=实现会话）② two_phase：spec-reviewer.md + code-reviewer.md；unified：reviewer.md ③ 审查通过任务才算完成 ④ 审查期实现者冻结（E017），补提交先 retract ⑤ 引擎语义变更（新状态 / 流程 / 规则文件 / 命令）→ 引导层三查，spec 与 code 两阶段均适用

> 原 .orchd/SKILL.md「审查者 ID 约定」+ Reviewer workflow 的细节说明（清单化模板 / 证据分层 / merge 前置 / 文档类单阶段），外置自 task-skill-hub-refactor。

## 审查者身份约定（默认档：独立 subagent 审查为唯一合法形态）
- 实现任务用各 agent 会话级指纹（12 位 hex，由 `ORCHD_SESSION_ID` 派生），**禁止跨对话复用同一指纹**；审查以当前会话指纹领取（不再使用固定 `reviewer-1` ID）
- **审查必须由当前会话主动 spawn 的独立 subagent 执行（重点，2026-10-31 review-subagent-contract-strict；2026-11-04 review-handoff 起身份由引擎交接）**：领审并撰写审查意见的，必须是**本会话用 Agent 工具起出来的子代理上下文**。`done` 成功后引擎在响应里返回 `review_handoff`（已 mint `kind=reviewer`、`parent=本任务最后一次 DONE 的 session_id` 的审查会话，并渲染好 `prompt`——含 token 注入段、claim/review 命令、模板与必读清单、comments 首句双指纹）：实现会话把 `prompt` **原样**交给 subagent，由它在交接的工作树里直接领审与提交，不需要任何额外的会话身份动作。判据是**执行上下文是否独立**，不是指纹是否新——异指纹只是引擎 `E016` 的最低门槛，它只能证明「身份不同」，证明不了「是子代理」。`review_handoff` 缺失或带 `error` 时按 fail-closed 处理（停止并上报），不得退回手写 prompt 或自行拼命令。
  - **三条禁止（冒充独立审查的旁路，命中任一即违规）**：
    ① **自铸 session token 自用**——实现会话自己再取一个新指纹，领的仍是自己刚实现的那份审；
    ② **手设 `ORCHD_SESSION_ID`**——在本会话命令里直接赋值/导出一个环境变量伪造新身份（等价于 ①，且绕开 `session start` 的 runtime 记录）；
    ③ **复用他人 token**——取别的对话或他人正在使用的 session_token 领审，署名不属于本会话 spawn 的任何上下文。
    以上三条都不产生独立上下文，因此都不满足本节口径。引擎已在 claim 与 submit 双侧落 **lineage 硬门**，能拦住「没有专用审查会话」的情形，但拦不住「token 拿到手却没交给真 subagent」——那部分仍以文档口径为准，**不得以「引擎没拒」当作「契约允许」**。
  - **审查会话 lineage 硬门（2026-11-02 review-subagent-hard-block P0-3；DONE 归属口径 task-cont-done-lineage-docs）**：领审与提交都必须来自 `kind=reviewer` 且 `parent_session_id` = 该任务最后一次 DONE 的会话 id 的会话——DONE 事件归属口径为 `agent_id=claimed_by`、`session_id`=执行 done 的调用方会话（真源 `orchd/onboard/lifecycle/core/finish.py`），故 expected parent 即执行 done 的那个实现会话；缺失/伪造一律 `E016`，`details.reason` 区分 `lineage_missing` / `kind_not_reviewer` / `lineage_mismatch`。claim 与 submit 双检；旧账本 DONE 无 session_id 时 fail-closed（仅无引擎身份的 SDK 直连不判定——CLI 写命令在更早 E033 会话门已拦截）。单一真源 `orchd/ledger/session.py::require_review_lineage`。
  - **旁路写法约束（task-review-contract-guard-hardening，守卫 fail-closed）**：契约面**不得以自然语句复述被禁旁路（含否定式）**——「禁止 + 旁路」这类否定句也一律判红（否定 / 建议 / 引述在词面不可穷举区分，守卫不做语义豁免）。需要引用时统一用**占位名**「会话切换旁路」/「身份切换旁路」，再在旁边说明处置；守卫实现见 `orchd/contract_docs.py`；引擎仓由 `tests/test_review_contract_strict.py` 做负控，宿主由 `python .orchd/__main__.py doctor` 的 `contract_docs` 检查执行。本文件中的 `tests/` 路径引用仅指引擎仓（宿主无 tests/）。
  - **无 subagent 能力即中断（默认档唯一处置）**：起不出子代理的 agent，claim review 触发 `E016 self_review_blocked` 自动中断后，动作 = `retract` 释放认领 → **停止并上报**，等人工安排独立审查者。不得改用 ①②③ 任何一种旁路继续；把审查外包给一条平级会话链也不算 subagent（那是同级上下文，不是本会话 spawn 出来的子代理）。
  - **审查 comments 首句写明 subagent 来源（独立审查档硬性）**：首句形如「本审查由实现会话 `<实现者指纹>` spawn 的独立 subagent 执行，审查指纹 `<审查者指纹>`」；缺该来源声明的意见按审查无效处理（此为流程纪律，与 `E016` 的指纹门叠加）。
  - **引擎门已落地（2026-11-02 review-subagent-hard-block P0；本节为口径真源）**：自审在 claim 与 submit 双侧恒 `E016`（判点 `orchd/onboard/claim/execute.py` 与 `orchd/review/precheck.py`，两侧都不读任何开关）；审查身份的建立方式 = 引擎在 `done` 成功时 mint 审查会话并把 `prompt` / token / lineage 放进 `review_handoff` → 实现会话把 `prompt` 原样交给独立 subagent → 由它在该交接身份下领审与提交。身份由引擎落盘（`sessions/<id>.json` 的 `kind` / `parent_session_id`），不依赖宿主自己拼参数。
- **自审恒阻断（不可配置，2026-11-02 P0 收口）**：
  - 实现指纹 == 审查指纹 ⇒ claim 与 submit 均 `E016 self_review_blocked`；**不存在降级档**，config 级与任务级开关已从引擎删除。
  - request 候选与 review_priority 恒排除自审任务（`excluded_self_review` 可见），不再标注成可领候选。
  - submit 侧在 in_review / 阶段 / 认领三重门之后复判自审，防「claim 被旁路或状态被篡改」。
- **Review 优先调度（自动执行时为硬默认，2026-09-20 用户裁定）**：implementer 请求任务时，若存在该 agent 可认领的 in_review 任务，引擎返回 `next_action: "review_first"` + `review_priority` 提示先领取审查；**无人值守 / 连续自动跑圈时必须先把可领审查做完（claim review → `review` 提交结论 → 尚有 code 阶段则到 merged）再领实现**，且**不得用 `claim --task <pending-id>` 点名绕过本闸门**——`claim` 只按该任务自身状态分流、不查全局审查积压（2026-09-20 实测：3 个 in_review 因此积压数小时未被领）。详见 rules/session.md「工作优先级」
- 领审查前两项自查（决策权在人，此为可见性辅助）：
  - `python .orchd/__main__.py status` 中不存在本 session 实现 ID 名下的 claimed 任务（busy 检查按 ID 判定，换 ID 即可绕过，故须自查）
  - 读取目标任务实现侧 `claimed_by`，与本 session 实现 ID 相同 → 自审，恒被 `E016 self_review_blocked` 拒绝（无降级档）
- **code review APPROVED 后必须运行 merge audit 验证**：提交 code APPROVED 且 merge 成功（任务进入 completed）后，立即运行 `python .orchd/__main__.py status --audit-merge`，确认 `merge_audit.warnings` 为空（零告警）。若有告警（completed 任务对应分支仍悬空未入 main），不得将漏 merge 遗留到下游——处置：reviewer 先 `retract` 本次审查结论（释放 R1-b 审查冻结），由实现者（或人工）在 task 分支补提交后重新 `done`，同一 reviewer 再次提交 code APPROVED 重试 → merge 成功 → completed。**禁 reviewer 直接在任务分支提交或 cherry-pick 修复**（R1-b 审查期实现者冻结 + git 代理必拒 E007，无例外）。
- **合入前验碰撞集而非整树干净（2026-09-21）**：code APPROVED 提交前，只需确认主工作树没有"未跟踪、且任务分支已跟踪"的同路径文件（真碰撞会触发 untracked 覆盖拒绝）；已跟踪改动若为 IDEAS / IDEAS-archive / _master.json / ROADMAP 等引擎自提交产物，属后台提交瞬态（ideas-archive 自动归档等），等待数秒重验即可，不得当真脏拦截。真撞上时 merge 诊断会精确报文件名，按指引处置重试。

## 清单化模板与证据分层（M2-2，2026-08-06；证据分层 2026-08-08）
- 按 `templates/spec-reviewer.md` / `templates/code-reviewer.md` 的三态/分组清单逐项勾选，每条判定必须引用证据（验收标准编号 + 引擎 verify 状态 / 定向测试结果）；spec 判定三态（满足/不满足/需人工），code 判定分"机器可辅助项（必须核验）"与"人工复核项（弱模型存疑默认放行交人工复审）"两组；禁止以逻辑推演替代测试。
- **证据分层：in_review = 引擎已保证 verify_command 通过（done 前提），reviewer claim 响应附带 `verify` 摘要（P2 注入：ok/exit_code/output_summary），默认引用不重跑；仅 verify 摘要缺失/不可信或 diff 触及测试链路时重跑定向测试（禁全量 pytest / build / venv 重活，120s 预算）。**
- **存证同源例外（task-review-verify-at-head，2026-10-15）：「默认引用不重跑」以「存证覆盖的就是所审内容」为前提。** DONE 存证只覆盖 DONE 那一刻的代码，故引擎为该存证落 `head` 锚，并在 reviewer claim 时与当前任务分支 tip 比对：**无位移 → 零成本、零新增字段（响应与无此机制时逐字一致）；位移 → 引擎按 HEAD 复跑一次 verify_command**，新旧两份记录并列（`verify` 为 DONE 存证、`verify_at_head` 为复跑结果），附 `verify_head_drift`（`baseline_head` / `current_head` / `severity`）。**复跑失败只告警不阻断认领**（`severity=error` 并进 `degraded_guards`）——「边审边补交」是合法动作，不得惩罚修错；但 reviewer **必须**以 `verify_at_head` 为准复核补交部分，不得只看 DONE 存证下结论。存证无锚（旧存证 / git 不可用）时 fail-open，退化为「默认引用不重跑」。

## code APPROVED 的 merge 前置语义
- 引擎先执行 git merge，成功才写完成事件（任务 completed）。merge 冲突 → 任务**停留 in_review**（完成事件不落地，`merged: false` + `conflict_files`），由实现者（或人工）在 task 分支执行 `orchd git merge main`（受管通道，任务分支放行）解决冲突并提交后，同一 reviewer 再次提交 code APPROVED 重试 → merge 成功 → completed。
- **completed 语义**：= 实现 + 双阶段审查 + merge 入 main 全部完成；merge 未成功的任务不是 completed（仍为 in_review）。

## 文档类单阶段（Q2 分级，2026-08-06；白名单收紧 2026-08-13）
- files_to_edit 全为**真文档**（`.md` / `.mdx` / `.markdown` / `.rst` / `.txt` 后缀——`docs/`、`doc/` 目录下文件同样须命中后缀白名单，示例代码/JSON/脚本等一律双阶段）且**不碰** SKILL.md / conventions.md / .orchd/_master.json（约定与状态文件）的任务，done 时直接进入 code review（跳过 spec，code 即终审）——文档修改不涉及引擎运行与约定改变；碰引擎代码 / 约定 / 状态文件 / 构建配置（`pyproject.toml`）/ CI / schema JSON 等非文档文件的任务保持 spec + code 双阶段。

## 审查检查项：引擎语义变更同步（引导层三查，task-guidance-sync-convention，2026-09-13）

diff 命中以下任一触发条件时，审查者必须逐项核对 `shared/conventions.md` 的同名检查项（「错误出口同步」向引擎语义域的扩面）：

- 新增 / 修改状态机分支，或新增 / 修改 guidance `step`、`next_action` 取值；
- 新增 / 删除规则文件（`rules/*.md`）或规则章节；
- 新增命令、hint 文案或建议命令。

三查（每条须引用证据，任一不满足即 CHANGES_REQUESTED）：

1. **step 词表登记**——实际返回的 `step` / `next_action` 取值与 guide 词表逐项对齐：无漏登记、无死词汇、无双词表并行且无映射断言；
2. **read / template 路由完备**——新增规则文件 / 模板 / 规则章节已接入 `guidance.read` 与 `template` 路由，`max_read` 等上限未把关键必读（如 `rules/verify.md`）挤出；
3. **hint 文案与实际命令一致**——hint / 建议命令参数完整（两段式含 `--confirm`）、执行位置正确（任务分支 / worktree 而非 main），且与 rules/ 同语义条款一致、硬要求（如 `status --audit-merge`）不漏。
4. **人类可见窗口（stderr 提示块）预算自洽**——`_emit_guidance` 渲染层不做硬编码总数裁剪（无 200/197 字面量），预算由 `guide.py` 的 `_BLOCK_MAX` 求和不等式单一真源管理（布局开销 + hint + command + read + cases ≤ _BLOCK_MAX），红线用语义截断（按点分割只装整点，装不下退化为「红线 N 条（见路径）」），渲染体包 try/except；命令逐字完整不被腰斩。

**spec 与 code 两阶段均适用**：spec 阶段核对「新增语义是否已在词表 / 路由 / 文档中登记」的规格完备性；code 阶段核对代码与文案的实际一致（含 `--confirm`、执行位置、硬要求链路）。unified 单阶段审查同样适用。

## 审查检查项：门禁 / 守门类改动必查四态（task-e039-gate-self-injury-fix，2026-09-19）

diff 命中「新增或修改门禁、守卫、覆盖判定、hook、CI / pre-push 条件、verify 覆盖面规则」时，审查者与实现者都必须逐项给出**四态负例**的证据（pass5 教训：E039 门禁引入两天即被实证三洞——**新守门面本身需要被守门**）：

1. **超集态**——比门禁要求更宽的合法输入不得被误判为不合规（如 verify 跑全量 / 目录级收集是登记测试的超集，必须视为已覆盖，否则「恰需宽域 verify」的任务被自己的门禁卡死）；
2. **旁路态**——排除 / 收窄选项不得绕过判定（`--ignore` / `--deselect` / `-k` / `--ignore-glob` 指向被要求的测试时仍须判未覆盖；非 pytest 段里的路径不得算作已跑）；**形态识别必须校验位置**——同一字面量出现在**选项取值位**（`--rootdir tests/`、`--cov tests/`、`--cov=tests/`）不等于出现在目标位，否则「更宽的覆盖」会被误判为成立（本清单 R-1 打回项即此形态）；
3. **未提交窗口**——判定输入必须覆盖「工作树未提交改动」，不得只读已提交 diff（引擎 auto-commit 在 done 靠后阶段）；
4. **空输入**——受管输入为空（无 verify_command、无改动清单、判定不可用）时不得静默放行：要么阻断，要么记结构化降级（`degraded_guards`，E030）留痕。

判定口径与单一真源：覆盖判定集中在 `orchd/shared_entries.py`（`verify_command_covers_all_tests` / `verify_command_test_targets` / `missing_*`），四态各配正负用例；`details.rule` 区分缺口性质（`registry` / `global_shared_file`）。

## 审查意见回看（只读 --show，task-review-comments-readback）

- `python .orchd/__main__.py review --show --task <id>` 只读回看该任务全部历史
  审查意见（每条含 review_type / verdict / timestamp / comments），completed
  归档任务同样可读，无意见返回空列表。
- 与 `--verdict` 互斥（同时提供直接拒绝）；只读不写任何事件、不改任务状态、
  不要求会话身份；`--type` 在回看模式下不作过滤。
