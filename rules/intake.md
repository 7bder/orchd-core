# 摄入协议 v2（IDEAS.md pending → orchd 任务）

> 原 .orchd/SKILL.md「摄入协议 v2」+「任务拆解粒度启发式」，外置自 task-skill-hub-refactor。

> **双路径（intake-dual-path，2026-08-15）**：摄入有两条合规入口，按是否已有明确规划分流——
> **有规划**：先 `python .orchd/__main__.py roadmap-land <版本>` 把 ROADMAP 规划章节落地为 IDEAS pending 条目，再走本协议的拆解 / 查重 / 注册流程；
> **无规划（临时想法）**：直接写入 IDEAS.md 为 pending 条目，再走本协议的拆解 / 查重 / 注册流程。
> 两条路径最终汇合于「IDEAS pending → 拆解草案 → 任务池」；roadmap-land 由引擎兜底校验（validate E031 检出未落地规划章节）。

> **写入门禁（idea-write-gate，2026-08-15）**：对话讨论产生的灵感不直接写 pending，先经 4 步流程——
> ① 讨论：对话中沉淀灵感与可行性论证；
> ② `python .orchd/__main__.py idea propose --title <t> --feasibility <论证>`：agent 将灵感**追加为 status: study 条目**（记入 IDEAS.md，非 pending）；
> ③ 用户裁决：`idea confirm --title <t>` 升 pending，或 `idea drop --title <t>` 丢弃（**仅用户可执行 confirm/drop**，agent 不得代行）；
> ④ 摄入：confirm 后的 pending 条目走本协议拆解 / 查重 / 注册流程。
> 原因：把"谁做、值不值得做"的判断权交还用户，避免 agent 自作主张把论证中的灵感直接推入任务池。engine 已实现（idea 子命令组 + study 状态），本协议为流程契约。

> 本协议 = 双闸门（闸门一：草案人工确认"做什么"；闸门二：claim 确认"谁做"）。
> 设计依据：docs/self-hosting-design-merged.md §4.3。约定层实现，引擎/schema 零改动；引擎化（`orchd proposal/confirm` 命令 + 状态机"待确认"态）评估留待 v1.2（触碰 §9.1 边界）。

**三条铁律（防整理偏差，缺一不可）**：

1. **原文可追溯**：每条整理结果必须能对应回原始口语（notes 保留原文或引用）；人工确认闸门看"原文 + 整理文本"对照，不看整理后文本
2. **推断项显式标注**：所有补全/猜测标注"待确认"，**禁止默认补全关键字段**（module / acceptance_criteria / files_to_edit）
3. **全量整理后统一查重**：同一 session 摄入的全部条目必须在同一视野内完成查重，禁止跨 session 拆开处理（撞车教训的机制保证）

**流程（7 步）**：

0. **前置过滤**：只有 HTML 注释之外的 `##` 章节才算条目；注释块内的内容（含文件顶部的示例模板）一律跳过，不得摄入
1. **全量语言整理**：全部 pending 先统一整理——规范化表述、术语统一、单条单主题、推断项标注、原文可追溯（`goal:` 字段帮助判断拆解方向）
2. **统一查重（两轮）**：① **本次摄入全部条目间相互查重**——近义条目合并为单一任务定义，notes 注明合并理由与合并来源条目；② **与已有任务比对**——`python .orchd/__main__.py status` 按任务 name 比对（列表模式覆盖 pending / claimed / in_review，但不含 brief / files_to_edit）；名称无法排除的候选，直接读 `.orchd/_master.json` 比对 brief 与 files_to_edit。**撞车条目 status 处置二选一，明确写入 notes**：置 `taskified` 并入已有任务（notes 标注已有 task id），或置 `questioning` 待用户裁决（notes 列出冲突任务 id，用户裁决后置 dropped 或确认合并）；不得重复注册，不得留下"两种解读都说得通"的含糊状态
3. **冲突与依赖规划**：拆解产物逐一核对新任务与在池 pending 任务、及本次摄入任务之间的 files_to_edit 交集，冲突任务以 depends_on 串行化或合并为同一任务定义；依赖链做无环验证；杜绝注册后并行领取触发 E010 仅靠人工预警。**2026-08-08 语义更新**：`amend` 冲突校验已降级为 warning（`conflict_warnings`，不再拒绝注册），冲突硬边界在 **request 依赖感知强制过滤**——与 pending/claimed 非依赖任务冲突的候选被硬排除（`excluded_conflicts`），依赖链（祖先/子孙）共享文件的任务放行（`conflict_with`）——因此未串行化的共享文件任务对，第二个将无法领取（串行化意图由引擎强制执行，而非注册期人工预警）。**共享文件并行知情决策**（2026-08-06 实踩：6 任务并行共享 onboard.py/errors.py → code APPROVED 连续 merge 冲突）：共享引擎核心文件（onboard.py / gitops.py / cli.py / errors.py / spec.py / tests/*）的任务**默认 depends_on 串行化**；确需并行（多 agent 并发吞吐）时，必须在任务 notes 显式记录"并行理由 + 冲突风险"，不得静默并行——引擎已提供兜底（request excluded_conflicts + merge 自动化解，见 ROADMAP 1.1 L1/L3）
4. **生成任务定义草案清单**：产物是完整任务定义草案（存放 `.orchd/proposals/<task_id>.json`，已被 .gitignore 忽略，过程产物不入库）。必填：id（`task-` 前缀、小写字母数字连字符）、name、brief、module、acceptance_criteria、files_to_edit；推荐补 depends_on 与 verify_command（见 rules/verify.md）。审查者身份由引擎自动识别会话指纹 + 防自审校验，任务定义不再含 `reviewers` 字段。**exempt_files 字段（2026-08-08 新增，可选）**：任务实现**必然连带修改但不在 files_to_edit 内**的文件（典型：新增错误码 → 连带 `tests/test_errors.py` 计数断言；schema 字段 → 连带 schema 相关测试）可声明 `exempt_files: [...]`——hook 豁免、不占 files_to_edit 额度；**连带文件必须显式声明**（files_to_edit 或 exempt_files 二选一），否则 E020 hook 拦截 + validate_quality E026 预警。**拆分粒度按模块独立性**（粒度锚点见下），参考 `.orchd/shared/architecture.md` 与 `templates/architect.md`，拆出几个算几个
5. **人工确认/修改（闸门一：确认"做什么"）**：向用户呈现"原文 + 整理文本 + 任务定义草案"对照清单，用户确认或修改后才 `python .orchd/__main__.py amend` 注册。信息不足 → 草案内列具体问题置 `status: questioning`，用户回答即确认；**只有用户可置 dropped**；超出 orchd 能力范围（需外部服务、采购）置 questioning 说明原因
6. **注册**：仅在 main 且工作区干净时执行 `python .orchd/__main__.py amend`（**"工作区干净"判定：以无已跟踪文件改动为准，untracked 工具/配置文件（如 .workbuddy/、reasonix.toml）不阻塞 intake/amend/claim**；**intake-commit-enforcement（2026-08-14）**：摄入产物（IDEAS.md / ROADMAP.md / _master.json）允许未提交态进入 amend，其余已跟踪改动 → E017 阻断注册）；成功后**立即提交** `.orchd/_master.json` 与当次 IDEAS.md、ROADMAP.md 变更（引擎自动提交；commit 失败写入 commit_warning 可审计）；条目置 `status: taskified`，notes 记录 task id、整理后规范表述、推断项与来源 commit。**只改 IDEAS/ROADMAP、暂不注册任务**时可执行 `python .orchd/__main__.py intake` 单独提交摄入产物（引擎命令，前置守卫 + 强制提交）。**输出提示（task-guide-block-config，2026-08-16）**：注册/摄入等命令的 JSON 响应之后，stderr 会输出 `orchd ▸` 前缀 + 分隔线的"下一步"提示块——stdout 保持纯 JSON 供 agent/脚本解析，stderr 提示仅供人看，勿当作命令结果。
7. **接续（闸门二：确认"谁做"）**：注册完成后走 `python .orchd/__main__.py request` → 人工确认 → `python .orchd/__main__.py claim`，确认领取者后转入优先级 2/3

**任务拆解粒度启发式（锚点，防过度合并/过度拆分）**：

- **下限**（不得更细）：单任务 `files_to_edit` ≥ 1；acceptance_criteria 至少 1 条可机器验证（verify_command 能命中）；不接受纯注释/纯文档单独立任务（除非文档专项模块如 mod-docs）
- **上限**（不得更粗）：单任务 `files_to_edit` ≤ 5 个独立文件（超出优先按模块独立性拆分）；`estimated_hours` ≤ 8（超出按依赖链串行化为 depends_on 队列）；`acceptance_criteria` ≤ 6 条（超出说明任务内聚性不足）。**exempt_files 不占 files_to_edit 额度（2026-08-08）**：必要连带豁免声明（如新增错误码连带 tests/test_errors.py）计入 exempt_files 而非 files_to_edit，不计入 5 文件上限
- **判断标准**：以"单 agent session 一次完成 + 单 reviewer 一次审完"为锚点；预估上下文 > 30K tokens 过大、< 30 分钟完成过小
- **拆分优先原则**：按模块独立性拆（mod-core / mod-docs / mod-cli / mod-packaging 各自独立任务）优先于按步骤拆；文件冲突任务用 depends_on 串行化，不合并为单任务
