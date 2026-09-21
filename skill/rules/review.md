# 审查规则（ID 约定 / 禁止自审 / 证据分层 / merge 前置 / 单阶段判定）

> TL;DR: ① 自审默认**仅提示**（`self_review_notice`；线上版 `config.enforce_self_review_block=true` 才恢复 E016 硬阻断），引擎语义 / 门禁行为变更 / 错误码语义 / 状态机类任务**建议**换独立会话审查（非强制，详见 [session.md](session.md) 与 `shared/conventions.md`）② two_phase：spec-reviewer.md + code-reviewer.md；unified：reviewer.md ③ 审查通过任务才算完成 ④ 审查期实现者冻结（E017），补提交先 retract ⑤ 引擎语义变更（新状态 / 流程 / 规则文件 / 命令）→ 引导层三查，spec 与 code 两阶段均适用

> 原 .orchd/SKILL.md「审查者 ID 约定」+ Reviewer workflow 的细节说明（清单化模板 / 证据分层 / merge 前置 / 文档类单阶段），外置自 task-skill-hub-refactor。

## 审查者身份约定（自审默认仅提示）
- 实现任务用各 agent 会话级指纹（12 位 hex，由 `ORCHD_SESSION_ID` 派生），**禁止跨对话复用同一指纹**；审查以当前会话指纹领取（不再使用固定 `reviewer-1` ID）
- **自审默认降级为仅提示**（2026-08-17，单机模型；线上版可设 `_master.json config.enforce_self_review_block=true` 恢复阻断）：
  - **默认（enforce=false）**：claim review 时若 DONE 实现指纹 == 当前指纹，**照常放行**，认领结果附 `self_review_notice`（含 `done_by` + 引导）；request 候选 / review_first 中自审任务标注 `is_self_review: true`，不参与任何决策与阻断
  - **enforce=true（线上版）**：恢复 `E016 self_review_blocked`——claim review 拒绝、request 候选排除自审任务
- **Review 优先调度（自动执行时为硬默认，2026-09-20 用户裁定）**：implementer 请求任务时，若存在该 agent 可认领的 in_review 任务，引擎返回 `next_action: "review_first"` + `review_priority` 提示先领取审查；**无人值守 / 连续自动跑圈时必须先把可领审查做完（claim review → `review` 提交结论 → 尚有 code 阶段则到 merged）再领实现**，且**不得用 `claim --task <pending-id>` 点名绕过本闸门**——`claim` 只按该任务自身状态分流、不查全局审查积压（2026-09-20 实测：3 个 in_review 因此积压数小时未被领）。详见 rules/session.md「工作优先级」
- 领审查前两项自查（决策权在人，此为可见性辅助）：
  - `python .orchd/__main__.py status` 中不存在本 session 实现 ID 名下的 claimed 任务（busy 检查按 ID 判定，换 ID 即可绕过，故须自查）
  - 读取目标任务实现侧 `claimed_by`，与本 session 实现 ID 相同 → 自审，默认仅标注提示（是否继续由人裁决）
- **code review APPROVED 后必须运行 merge audit 验证**：提交 code APPROVED 且 merge 成功（任务进入 completed）后，立即运行 `python .orchd/__main__.py status --audit-merge`，确认 `merge_audit.warnings` 为空（零告警）。若有告警（completed 任务对应分支仍悬空未入 main），立即在当前 reviewer session 内 cherry-pick 修复并重新验证，不得将漏 merge 遗留到下游
- **合入前验碰撞集而非整树干净（2026-09-21）**：code APPROVED 提交前，只需确认主工作树没有"未跟踪、且任务分支已跟踪"的同路径文件（真碰撞会触发 untracked 覆盖拒绝）；已跟踪改动若为 IDEAS / IDEAS-archive / _master.json / ROADMAP 等引擎自提交产物，属后台提交瞬态（ideas-archive 自动归档等），等待数秒重验即可，不得当真脏拦截。真撞上时 merge 诊断会精确报文件名，按指引处置重试。

## 清单化模板与证据分层（M2-2，2026-08-06；证据分层 2026-08-08）
- 按 `templates/spec-reviewer.md` / `templates/code-reviewer.md` 的三态/分组清单逐项勾选，每条判定必须引用证据（验收标准编号 + 引擎 verify 状态 / 定向测试结果）；spec 判定三态（满足/不满足/需人工），code 判定分"机器可辅助项（必须核验）"与"人工复核项（弱模型存疑默认放行交人工复审）"两组；禁止以逻辑推演替代测试。
- **证据分层：in_review = 引擎已保证 verify_command 通过（done 前提），reviewer claim 响应附带 `verify` 摘要（P2 注入：ok/exit_code/output_summary），默认引用不重跑；仅 verify 摘要缺失/不可信或 diff 触及测试链路时重跑定向测试（禁全量 pytest / build / venv 重活，120s 预算）。**

## code APPROVED 的 merge 前置语义
- 引擎先执行 git merge，成功才写完成事件（任务 completed）。merge 冲突 → 任务**停留 in_review**（完成事件不落地，`merged: false` + `conflict_files`），由实现者（或人工）在 task 分支执行 `git merge main` 解决冲突并提交后，同一 reviewer 再次提交 code APPROVED 重试 → merge 成功 → completed。
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
