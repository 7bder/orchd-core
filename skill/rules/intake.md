# 摄入协议 v2（IDEAS.md pending → orchd 任务）

> TL;DR: ① 三条铁律：原文可追溯 / 推断项标注 / 全量查重 ② 双闸门：草案人工确认"做什么"、claim 确认"谁做" ③ 仅在 main 且工作区干净时 amend ④ 撞车条目置 taskified 或 questioning，不得重复注册

> 原 .orchd/SKILL.md「摄入协议 v2」+「任务拆解粒度启发式」，外置自 task-skill-hub-refactor。

> **双路径（intake-dual-path，2026-08-15）**：摄入有两条合规入口，按是否已有明确规划分流——
> **有规划**：先 `python .orchd/__main__.py roadmap-land <版本>` 把 ROADMAP 规划章节落地为 IDEAS pending 条目，再走本协议的拆解 / 查重 / 注册流程；
> **无规划（临时想法）**：直接写入 IDEAS.md 为 pending 条目，再走本协议的拆解 / 查重 / 注册流程。
> 两条路径最终汇合于「IDEAS pending → 拆解草案 → 任务池」；roadmap-land 由引擎兜底校验（validate E031 检出未落地规划章节）。

> **章节生命周期约定（section-lifecycle，2026-09-13 task-roadmap-section-parse-fix）**：ROADMAP 版本章节（``## / ### 版本``）按「规划 → 落地 → 实现 → 归档/退出」闭环管理——
> ① **规划态**：章节保留在 ROADMAP（近期/远期/派生分支），validate E031 要求 IDEAS.md 或 IDEAS-archive.md 含 ``§版本`` 引用才不告警；
> ② **落地态**：经 `roadmap-land <版本>` 落地为 IDEAS pending 条目（引用含 ``§版本``），E031 消除；
> ③ **实现态**：条目任务化并全部终态后，`archive_resolved_ideas` 将条目移入 IDEAS-archive.md（先写归档、再删主文件）——此时 E031 判据以 IDEAS-archive.md 为准，已归档章节不「回弹告警」；
> ④ **退出态**：版本已发布或已放弃时，**由用户**（agent 不得代行）把 ROADMAP 章节标题标记「历史」（标题含「历史」即被解析器跳过）或整章移出 ROADMAP。若章节已归档但仍留在 ROADMAP 且未标记历史，属**尚未处置的存量**，E031 持续告警提示清理，不构成新增缺陷。
> 职责边界：roadmap-land / 归档由引擎与协议执行；「标记历史 / 移出」是**用户裁决动作**，agent 只在用户明确指示后修改 ROADMAP。

> **写入门禁（idea-write-gate，2026-08-15）**：对话讨论产生的灵感不直接写 pending，先经 4 步流程——
> ① 讨论：对话中沉淀灵感与可行性论证；
> ② `python .orchd/__main__.py idea propose --title <t> --feasibility <论证>`：agent 将灵感**追加为 status: study 条目**（记入 IDEAS.md，非 pending）；
> ③ 用户裁决：`idea confirm --title <t>` 升 pending，或 `idea drop --title <t>` 丢弃（**仅用户可执行 confirm/drop**，agent 不得代行）；
> ④ 摄入：confirm 后的 pending 条目走本协议拆解 / 查重 / 注册流程。
> 原因：把"谁做、值不值得做"的判断权交还用户，避免 agent 自作主张把论证中的灵感直接推入任务池。engine 已实现（idea 子命令组 + study 状态），本协议为流程契约。

> **来源条目生命周期与 confirm 标题口径（2026-09-13 实测踩坑，两条硬约束）**：
> **① `source: idea:<id>` 的条目在任务非终态期间必须保持 `pending`**——`validate_source`（E025）**全量遍历**任务、只对终态（completed/cancelled）任务豁免，后者是为 ideas-archive 归档已完结条目留的口子。故 **`taskified` 并非"已注册"语义**，它只服务「撞车并入已有任务」（流程 step 2）；注册新任务时提前置 `taskified` 会被 E025 直接阻断。正确顺序：`idea confirm`（study → pending）→ 编辑 master → `amend`。
> **② `idea confirm` / `idea drop` 的 `--title` 必须传完整标题（含 id 后缀）**——`_find_idea_entry` 的匹配口径为「整标题 == title」或「标题以空格 + title 结尾」（标题 = `## ` 行去掉 `## ` 与日期前缀）。`idea propose` 写入的标题带引擎追加的 `（id: <slug>）` 后缀，故 confirm/drop 须传 **propose 的 title 加 `（id: <slug>）`**；只传 propose 时的 title 会返回 `not_found`。

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
3. **冲突与依赖规划**：拆解产物逐一核对新任务与在池 pending 任务、及本次摄入任务之间的 files_to_edit 交集，冲突任务以 depends_on 串行化或合并为同一任务定义；依赖链做无环验证；杜绝注册后并行领取触发 E010 仅靠人工预警。**2026-08-08 语义更新**：`amend` 冲突校验已降级为 warning（`conflict_warnings`，不再拒绝注册），冲突硬边界在 **request 依赖感知强制过滤**——与 pending/claimed 非依赖任务冲突的候选被硬排除（`excluded_conflicts`），依赖链（祖先/子孙）共享文件的任务放行（`conflict_with`）——因此未串行化的共享文件任务对，第二个将无法领取（串行化意图由引擎强制执行，而非注册期人工预警）。**共享文件并行知情决策**（2026-08-06 实踩：6 任务并行共享 onboard.py/errors.py → code APPROVED 连续 merge 冲突）：共享引擎核心文件（orchd/onboard/ / orchd/gitops/ / orchd/cli/ / errors.py / spec.py / tests/*）的任务**默认 depends_on 串行化**；确需并行（多 agent 并发吞吐）时，必须在任务 notes 显式记录"并行理由 + 冲突风险"，不得静默并行——引擎已提供兜底（request excluded_conflicts + merge 自动化解，见 ROADMAP 1.1 L1/L3）。**2026-09-12 在途冲突可见性（task-inflight-conflict-visibility）**：「在途」= 分支上已有改动、尚未落 main 的任务（含 done / in_review / force-status 悬空态），**以 git 事实判定，与状态字段无关**——历史上仅按 `claimed` 判冲突，导致刚 done 尚未 merge 的任务逃出视野（实测数十秒盲区，随后在 merge 期才撞上）。在途重叠按 `config.conflict_policy` 分流：**缺省 `warn` = 仅 `conflict_with` 注解（在途条目含 `source=inflight`）+ 降权排序，不新增任何阻断**（拆解期无需为在途重叠额外串行化）；`serialize` / `block` 才把在途重叠候选硬排除。故 §3 的串行化要求仍聚焦 **claimed / pending 声明文件重叠**，在途重叠属"可见性提示"，不改变可领取性
4. **生成任务定义草案清单**：产物是完整任务定义草案（存放 `.orchd/proposals/<task_id>.json`，已被 .gitignore 忽略，过程产物不入库）。必填：id（`task-` 前缀、小写字母数字连字符）、name、brief、module、acceptance_criteria、files_to_edit；推荐补 depends_on 与 verify_command（见 rules/verify.md）。**验证面闭环（2026-09-21，宿主 FE lint 缺口教训）**：每个任务的验证面在拆解期一次写全——单测文件 + lint/门禁入口 + 发版门禁引用，缺一即在草案内显式标注缺口归属（哪个后续任务补），不得默认"后续有人管"。审查者身份由引擎自动识别会话指纹 + 防自审校验，任务定义不再含 `reviewers` 字段。**exempt_files 字段（2026-08-08 新增，可选）**：任务实现**必然连带修改但不在 files_to_edit 内**的文件（典型：新增错误码 → 连带 `tests/test_errors.py` 计数断言；schema 字段 → 连带 schema 相关测试）可声明 `exempt_files: [...]`——hook 豁免、不占 files_to_edit 额度；**连带文件必须显式声明**（files_to_edit 或 exempt_files 二选一），否则 E020 hook 拦截 + validate_quality E026 预警。**拆分粒度按模块独立性**（粒度锚点见下），参考 `.orchd/shared/architecture.md` 与 `templates/architect.md`，拆出几个算几个。**声明形态要求（2026-09-12）**：`files_to_edit` / `exempt_files` 必须逐条枚举**具体文件路径**——不得写目录、以 `/` 结尾的目录式声明或通配符。门禁（E010 越界、request 冲突检测）一律按枚举做**精确成员匹配**，全仓无目录展开逻辑：目录式声明会在冲突检测处**漏检**（集合交集永远不命中）、在 E010 越界检测处**误报**（精确成员判定）。测试文件按「一个测试文件一个域」声明（见 [testing.md](testing.md)）。**例外（2026-09-13，task-decl-dir-notation-docs）**：上述形态约束仅适用 `files_to_edit` / `exempt_files`；`files_to_read` 供上下文查阅，不参与冲突检测 / E010 / E026，允许目录式路径与通配符，不受此约束。
5. **人工确认/修改（闸门一：确认"做什么"）**：向用户呈现"原文 + 整理文本 + 任务定义草案"对照清单，用户确认或修改后才 `python .orchd/__main__.py amend` 注册。信息不足 → 草案内列具体问题置 `status: questioning`，用户回答即确认；**只有用户可置 dropped**；超出 orchd 能力范围（需外部服务、采购）置 questioning 说明原因
6. **注册**：仅在 main 且工作区干净时执行 `python .orchd/__main__.py amend`（**"工作区干净"判定：以无已跟踪文件改动为准，untracked 工具/配置文件（如 .workbuddy/、reasonix.toml）不阻塞 intake/amend/claim**；**intake-commit-enforcement（2026-08-14）**：摄入产物（IDEAS.md / ROADMAP.md / _master.json）允许未提交态进入 amend，其余已跟踪改动 → E017 阻断注册）；成功后**立即提交** `.orchd/_master.json` 与当次 IDEAS.md 变更（引擎自动提交；commit 失败写入 commit_warning 可审计）——**ROADMAP.md 唯一源 = 宿主项目根且纳入 git**（flat=仓库根，container=`<容器>/main/`），随同次 amend/intake 提交；`.orchd/ROADMAP.md` 已非合法形态、不在提交范围，引擎 ensure_committed 对 gitignore 忽略路径自动剔除（见 rules/git.md）；条目置 `status: taskified`（**例外**：任务的 `source` 引用了该条目时（`idea:<id>`）须保持 `pending`——见上方「来源条目生命周期与 confirm 标题口径」），notes 记录 task id、整理后规范表述、推断项与来源 commit。**只改 IDEAS/ROADMAP、暂不注册任务**时可执行 `python .orchd/__main__.py intake` 单独提交摄入产物（引擎命令，前置守卫 + 强制提交）。**输出提示（task-guide-block-config，2026-08-16）**：注册/摄入等命令的 JSON 响应之后，stderr 会输出 `orchd ▸` 前缀 + 分隔线的"下一步"提示块——stdout 保持纯 JSON 供 agent/脚本解析，stderr 提示仅供人看，勿当作命令结果。**注册用测试骨架 hygiene（2026-09-21 实踩）**：为注册而建的测试占位文件直接写任务 worktree，或注册后即删 main 副本——main 残留的同路径 untracked 文件会在 code APPROVED 合并时触发"untracked would be overwritten"致 merge_env_error；合入前必验 main 干净。
7. **接续（闸门二：确认"谁做"）**：注册完成后走 `python .orchd/__main__.py request` → 人工确认 → `python .orchd/__main__.py claim`，确认领取者后转入优先级 2/3

**任务拆解粒度启发式（锚点，防过度合并/过度拆分）**：

- **下限**（不得更细）：单任务 `files_to_edit` ≥ 1；acceptance_criteria 至少 1 条可机器验证（verify_command 能命中）；不接受纯注释/纯文档单独立任务（除非文档专项模块如 mod-docs）
- **上限**（不得更粗）：单任务 `files_to_edit` ≤ 5 个独立文件（超出优先按模块独立性拆分）；`estimated_hours` ≤ 8（超出按依赖链串行化为 depends_on 队列）；`acceptance_criteria` ≤ 6 条（超出说明任务内聚性不足）。**exempt_files 不占 files_to_edit 额度（2026-08-08）**：必要连带豁免声明（如新增错误码连带 tests/test_errors.py）计入 exempt_files 而非 files_to_edit，不计入 5 文件上限。**exempt_files 必须显式、禁止默认补全（2026-09-12 强化）**：连带文件既不在 files_to_edit 也不在 exempt_files → E020 pre-commit hook 拦截 + `validate_quality` E026 预警，且补救须回到 **canonical 主工作树** `amend` 补声明（任务分支执行 amend 直接 E007 拒绝），是多 agent 并发下最典型的人工往返。拆解时应把**必然连带**的文件（尤其是同域测试文件）一次写全，宁可多声明也不返工。豁免文件须由实现者自行 `git commit`——引擎 `ensure_committed` 只兜底 files_to_edit
- **判断标准**：以"单 agent session 一次完成 + 单 reviewer 一次审完"为锚点；预估上下文 > 30K tokens 过大、< 30 分钟完成过小
- **拆分优先原则**：按模块独立性拆（mod-core / mod-docs / mod-cli / mod-packaging 各自独立任务）优先于按步骤拆；文件冲突任务用 depends_on 串行化，不合并为单任务
