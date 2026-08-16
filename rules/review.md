# 审查规则（ID 约定 / 禁止自审 / 证据分层 / merge 前置 / 单阶段判定）

> 原 .orchd/SKILL.md「审查者 ID 约定」+ Reviewer workflow 的细节说明（清单化模板 / 证据分层 / merge 前置 / 文档类单阶段），外置自 task-skill-hub-refactor。

## 审查者 ID 约定（禁止自审）
- 实现任务用各 agent 唯一 ID，命名规范 **MUST** 遵守 `{provider}-{序号}`（provider 为平台/工具名小写，序号为数字，如 qoder-a1、claude-x、codex-1、workbuddy-1），**禁止跨 provider 复用同一序号**（如 qoder-a1 与 claude-a1 视为冲突）；审查一律以固定 ID `reviewer-1` 领取
- **引擎层强制阻断**（task-claim-reviewer-independence，2026-08-06 落地）：
  - **E016 self_review_blocked**：claim review 时，若该任务 DONE 事件的 `agent_id` 与当前 claim agent 相同，引擎拒绝并返回换 agent 指引
  - **Review 优先调度**：implementer 请求任务时（`python .orchd/__main__.py request --agent X`），若存在该 agent 可认领的 in_review 任务，引擎返回 `next_action: "review_first"` + `review_priority` 提示先领取审查
- 领审查前两项自查（引擎已覆盖核心阻断，此为双重保险）：
  - `python .orchd/__main__.py status` 中不存在本 session 实现 ID 名下的 claimed 任务（busy 检查按 ID 判定，换 ID 即可绕过，故必须先自查）
  - 读取目标任务实现侧 `claimed_by`，与本 session 实现 ID 相同 → 跳过该任务
- **code review APPROVED 后必须运行 merge audit 验证**：提交 code APPROVED 且 merge 成功（任务进入 completed）后，立即运行 `python .orchd/__main__.py status --audit-merge`，确认 `merge_audit.warnings` 为空（零告警）。若有告警（completed 任务对应分支仍悬空未入 main），立即在当前 reviewer session 内 cherry-pick 修复并重新验证，不得将漏 merge 遗留到下游

## 清单化模板与证据分层（M2-2，2026-08-06；证据分层 2026-08-08）
- 按 `templates/spec-reviewer.md` / `templates/code-reviewer.md` 的三态/分组清单逐项勾选，每条判定必须引用证据（验收标准编号 + 引擎 verify 状态 / 定向测试结果）；spec 判定三态（满足/不满足/需人工），code 判定分"机器可辅助项（必须核验）"与"人工复核项（弱模型存疑默认放行交人工复审）"两组；禁止以逻辑推演替代测试。
- **证据分层：in_review = 引擎已保证 verify_command 通过（done 前提），reviewer claim 响应附带 `verify` 摘要（P2 注入：ok/exit_code/output_summary），默认引用不重跑；仅 verify 摘要缺失/不可信或 diff 触及测试链路时重跑定向测试（禁全量 pytest / build / venv 重活，120s 预算）。**

## code APPROVED 的 merge 前置语义
- 引擎先执行 git merge，成功才写完成事件（任务 completed）。merge 冲突 → 任务**停留 in_review**（完成事件不落地，`merged: false` + `conflict_files`），由实现者（或人工）在 task 分支执行 `git merge main` 解决冲突并提交后，同一 reviewer 再次提交 code APPROVED 重试 → merge 成功 → completed。
- **completed 语义**：= 实现 + 双阶段审查 + merge 入 main 全部完成；merge 未成功的任务不是 completed（仍为 in_review）。

## 文档类单阶段（Q2 分级，2026-08-06；白名单收紧 2026-08-13）
- files_to_edit 全为**真文档**（`.md` / `.mdx` / `.markdown` / `.rst` / `.txt` 后缀——`docs/`、`doc/` 目录下文件同样须命中后缀白名单，示例代码/JSON/脚本等一律双阶段）且**不碰** SKILL.md / conventions.md / .orchd/_master.json（约定与状态文件）的任务，done 时直接进入 code review（跳过 spec，code 即终审）——文档修改不涉及引擎运行与约定改变；碰引擎代码 / 约定 / 状态文件 / 构建配置（`pyproject.toml`）/ CI / schema JSON 等非文档文件的任务保持 spec + code 双阶段。
