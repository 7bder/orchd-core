# Orchd Agent
> agent 工作流协议（BOOTSTRAP / WORKER 两模式与纪律，SELF-HOSTED 已并入统一协议，`self-hosted` marker 仅作来源标记），存放于宿主项目 **`.orchd/SKILL.md`**（零根文件 + 自包含 `.orchd/` 模型，task-12-docs-roadmap）；面向**人**的安装、命令参考与故障排查使用指南见 [docs/user-manual.md](../docs/user-manual.md)；面向**skill 分发渠道**的独立引导包见 `skills/orchd/`（标准 Agent Skill，供 skills.sh / 千问广场 / Claude skills 发布用）。

## 入口协议（MUST，每 session 首读）
1. 读本文件 `.orchd/SKILL.md`（纪律红线优先级最高，见下纪律红线节）。
2. 据此定位 `.orchd/` 资源：规则见 `.orchd/rules/`（索引 `rules/README.md`），模板见 `templates/`。
3. 按任意命令响应的 `guidance` 字段导航下一步（知识路由 `read`/`template` + 动作路由 `command`）；遇具体问题按规则目录按需 Read。
   - **知识+方法路由闭环（task-guide-routing-docs）**：agent 收到 `guidance` 后按 `read` 数组读取对应规则文件 → 按 `template` 数组加载对应模板 → 按 `command` 执行。任一字段缺失（空数组 / 不存在）跳过该步，不阻塞后续步骤；与既有 `step`/`command`/`hint` 平铺兼容（不需要该字段则忽略，需要才走）；模板文件不存在时降级为跳过。闭环示例见 `docs/user-manual.md` §3「闭环示例」。
4. 若当前 session 经 skill 分发接入（非读本文件路径）：遵循 `skills/orchd/` 引导流程（检测 `.orchd/` → 无则自启安装 → 回到本文件）。
5. **输出契约（task-guide-block-config，2026-08-16）**：所有命令的 **stdout 为纯 JSON**（机器/agent 解析用，含 `guidance` 字段）；**人看的"下一步"提示块走 stderr**——以 `orchd ▸` 前缀 + 分隔线围成块状，紧跟 JSON 之后输出，**不污染 stdout 的机器解析**。agent 只读 stdout，勿把 stderr 提示当作命令结果。
6. **转述契约（task-guidance-dual-view-docs，2026-08-19）**：收到 `guidance` 后须**向用户转述下一步**（agent 是用户与引擎之间的转述者，不得吞掉引导）。转述时机：① 用户主动问「下一步」；② agent 完成任务后需用户决策下一步时**显式提示**。转述载体：初始化（`guidance` 含 `card` 字段）由接入层渲染 **SVG 全貌卡片**；日常用 **Markdown 引用块 + 粗体** 转述 `project_view`（项目整体视角）与 `agent_view`（当前 agent 视角）两条——`project_view` 与 `agent_view` 为 `guidance` 双视角字段（task-guidance-dual-view-engine），顶层 5 键 `{step, read, template, command, hint}` 语义不变（= `agent_view`，向后兼容）；`card` 字段（`title/phase/steps/current/next`）仅在首次引导 `first_time` 时附带。

## Determine your mode
- If `.orchd/` exists -> WORKER（已具备完整能力：摄入 / 纪律红线 / rules / guidance，协议一份不区分项目来源）
- If `.orchd/` does NOT exist -> BOOTSTRAP（自启动安装，见下一节）
- `.orchd/shared/self-hosted` marker（若存在）仅作"该项目是 orchd 自托管来源"的无害来源标记，**不是能力开关**，不改变任何行为。

## BOOTSTRAP mode (new project, 自启动安装)

> 宿主无 `.orchd/` 时，agent 凭本文件即可自我启动（不依赖 `.orchd/rules/install.md`，细节仍见该文件）：

```bash
git clone https://github.com/7bder/orchd-core.git
python orchd-core/install.py . --agent
python .orchd/__main__.py bootstrap   # 输出 schema + architect prompt + 拆解指南
```

安装后按 `bootstrap` 输出写 `.orchd/_master.json` → `validate` → `init`，项目即就绪，进入 WORKER 模式。

## WORKER mode (existing project)

> **命令调用约定（零根入口，task-12-docs-roadmap）**：所有 `orchd` 命令统一用 **`python .orchd/__main__.py`** 调用——与 `orchd <子命令>` 完全等价。宿主项目根**零额外文件**、无需安装 `orchd`。

> **无感引导（task-guide-seamless-guidance，2026-08-15）**：任意命令 JSON 响应自动附加 `guidance` 字段 `{step, read, template, command, hint}`——`read`（知识路由：该读的规则文件）、`template`（方法路由：该用的模板）、`command`（动作路由：下一步命令）。**据 `guidance` 逐层导航即可完成任何任务流程**（request/claim/done/review）；`guidance` 为加法式字段，不替换既有字段，也可忽略自行决策。
## 纪律与协议（统一适用于所有项目）

> **通用性说明（mode-unify，2026-08-15）**：本节纪律红线 / 摄入协议 / rules 目录对**所有项目一视同仁**，不再区分"仅本仓库特供"。特定于 orchd 自托管仓库的**运维专属动作**（如 IDEAS 自动归档、摄入门禁巡检）对普通外部宿主项目**不要求执行**——外部项目只需遵守通用工作流纪律，无需执行自托管专属运维。
### 纪律红线（MUST / MUST NOT，2026-08-05 用户裁定）

> 针对纪律遵守较弱的 agent 的硬约束。违反任意一条 = 事故，需人工介入，
> 不因"不知情/为了效率/任务需要"豁免。本清单优先级高于本文件其他任何章节。

**MUST NOT（绝对禁止，违反即事故）**：

1. **禁止手动 git 命令**：不得执行 `git checkout / branch / reset / stash /
   gc / prune / clean / rebase / cherry-pick / merge / push` 等任何手动 git
   写操作。git 操作只允许引擎自动执行（claim 建分支、code APPROVED merge、
   done/amend 自动提交）。**唯一豁免：任务分支上的 `git commit`（见 git
   纪律"本地提交自主执行"）**。确需其他手动 git 操作时，先向用户报告并获
   明确许可。**merge 后分支清理（task-merge-auto-delete-branch，
   2026-08-15）**：引擎 merge 成功后 best-effort 自动删除 `task/{id}` 分支，
   agent **无需手动 `git branch -d` 清理**；仅当引擎自动删除失败、`status
   --audit-merge` 告警兜底时，才需人工授权删除。
2. **禁止破坏性 git 操作**：`git reset --hard`、`git gc --prune=now`、
   `git clean -fdx`、`git branch -D`、`git push --force` 一律禁止——
   对象清理后历史不可恢复（2026-08-05 实踩：main 被移回旧提交、20 个 blob
   丢失，靠逐个补写恢复）。
3. **禁止修改范围外文件**：只读 `files_to_read`、只写 `files_to_edit`；
   不得修改/删除/移动任何范围外文件，不得触碰 `.git/` 内部结构
   （含 `.git/index`、对象库）。删文件前先确认该文件属于任务验收范围。
4. **禁止绕过身份**：一个 session 只允许一个身份指纹；不得用多个指纹完成
   同一任务链（2026-08-05 实踩：同一会话以 workbuddy-1 实现 + reviewer-1 审查，
   属自审绕过）。**自审降级（2026-08-17）**：引擎对自审默认仅提示不阻断
   （认领附 `self_review_notice`、request 标注 `is_self_review`），决策权在人；
   线上版可设 `_master.json config.enforce_self_review_block=true` 恢复 E016 阻断。
5. **禁止未提交即中断**：改动文件后必须提交（或明确报告"未提交+原因"）才能
   结束 session；不得把未提交改动留在工作区后静默离开（2026-08-05 实踩：
   trae-a1 改 SKILL.md 未提交即中断）。**引擎兜底（intake-commit-enforcement，
   2026-08-14）**：摄入/注册流程由引擎强制提交——amend 前置"非摄入产物干净"
   守卫（E017 阻断）+ commit 失败可审计（commit_warning）+ `orchd intake`
   命令提交摄入产物 + `orchd status --audit-intake` 巡检未提交摄入产物。
6. **禁止自动摄入与自动写入（idea-write-gate，2026-08-15 扩展）**：摄入（intake）仅在用户明确指定时执行；不得主动摄入 IDEAS.md 的 pending 条目（2026-08-05 用户裁定）。灵感写入同样受限——对话讨论产生的灵感不得直接写 pending：agent 只能 `idea propose` 记入 study（论证中），**confirm/drop 仅用户可执行**（2026-08-15 用户裁定，把"值不值得做"的判断权交还用户）。
7. **禁止擅自无人值守自动认领（auto-claim 默认禁用，2026-08-16）**：`request --auto-claim` 自动连续领任务**默认被引擎拒绝**（E032 `auto_claim_disabled`），仅当 `_master.json` 顶层 `config.allow_auto_claim` 显式为 `true`（用户明确授权）时才能调用。agent **不得**擅自用 `--auto-claim` 绕过 claim 人工确认闸门连续领任务。
8. **禁止在任务分支执行 intake/amend**：intake/amend 只在 main 且工作区
   干净时执行（引擎 not_on_main 兜底存在，但不得依赖）。
9. **禁止手改 .orchd 运行时文件**：`_ledger.jsonl`、`_checkpoint.json`、
   `mod-*/spec.json` 等运行时状态由引擎维护，agent 一律不得手改。
10. **版本进发先落地再摄入（intake-dual-path，2026-08-15）**：进入新版本规划时，
   先把 ROADMAP 规划章节用 `python .orchd/__main__.py roadmap-land <版本>` 落地为
   IDEAS pending 条目，再走摄入拆解；`validate` 以 E031 兜底检出带 id 且未落地的
   规划章节。无规划的临时想法可直接写入 IDEAS.md 走摄入，不必落地。
11. **禁止绕过 claim**：任务必须经 `python .orchd/__main__.py claim` 由引擎建分支；禁止手动
    `git branch/checkout` 创建任务分支（含不规范命名，如 task/task-1——
    2026-08-05 实踩：绕过 claim 手动建分支，实现悬空未 merge）。
12. **禁止任务悬空**：claim 后必须走完 done → 双阶段审查 → merge；
   中断/放弃必须先 retract，不得让任务停在 claimed/pending 且实现悬空
   （2026-08-05 实踩：task-amend-branch-guard-patch 实现悬空、
    task-merge-audit-workflow 实现未提交）。

**MUST（强制动作）**：

1. session 开始三连检查：`git status` + `git branch --show-current` +
   `python .orchd/__main__.py status`，确认分支与工作区状态后才动手。
2. 写文件前先读文件；不读不写。
3. 任务完成（done / review / amend）后，必须核对引擎响应（verify 结果、
   commit 是否执行、状态流转），确认成功后才算结束。
4. 测试/verify 一律用 `--basetemp` 指向系统临时目录，禁止项目内残留
   `pytest_tmp_*` / `.tmp-*`；session 结束确认工作区干净。
5. 任何异常（verify 失败、merge 冲突、状态不符）立即停止并报告，
   不自行猜测处置。
6. session 结束时工作区必须干净（无未提交改动），或在报告中说明。

### 身份约定（会话级指纹，session-id-fingerprint，2026-08-16）

agent 会话用**会话级指纹**作为身份 id：12 位 hex（SHA-256 短哈希，如
`a1b2c3d4e5f6`），由宿主注入的每对话唯一会话标识派生（`orchd.ledger.resolve_agent_id`）：
- 宿主在每次对话启动时注入 `ORCHD_SESSION_ID`，orchd 据此确定性派生指纹
  （同一对话内所有命令返回同一指纹，切换对话注入新值即换指纹），实现
  「**一个对话一个永久指纹**」。
- 各 agent 宿主统一接入：TRAE 会话由 `ICUBE_CODEMAIN_SESSION` **自动搬运**到
  `ORCHD_SESSION_ID`（开箱即用）；codex / opencode / workbuddy 等由各自接入层
  把会话唯一码写入 `ORCHD_SESSION_ID`。
- 已**彻底废除 `.orchd/.agent_id`**：引擎不再读写该文件，未注入 `ORCHD_SESSION_ID`
  时不生成、不借用、不落盘任何身份（写命令由引擎拒绝并提示宿主注入；只读命令
  可匿名运行）。存量历史 `.agent_id` 文件不再参与身份判定。

- **指纹生命周期**：同一对话内指纹永不变；不同对话（不同 `ORCHD_SESSION_ID`）
  返回不同指纹。切换对话即可获得新身份——这是「换对话领 review」的机制保证。
- **E021 对指纹形态豁免**：12 位 hex 形态的 agent_id 视为自动化会话身份，
  不与人名 `git user.name` 硬比对，`claim` / `done` / `review` 不触发 E021
  `identity_mismatch` warning。
- **何时用指纹 vs `{provider}-{序号}`**：宿主受管自动化会话用指纹作身份锚定；
  具名 agent 身份（如 `marvis-1`、`workbuddy-1`）用于人工可追溯场景。
- 纪律红线第 4 条不变：一个 session 只允许一个 agent ID。**自审默认仅提示不阻断**
  （2026-08-17 降级）：实现 + 审查可在同一指纹下完成，引擎在认领结果附
  `self_review_notice`、request 候选标注 `is_self_review`，**不参与任何流程决策**；
  决策权在人（调度者）。线上版可设 `_master.json config.enforce_self_review_block=true`
  恢复 E016 硬阻断（详见 rules/review.md）。
## 规则目录（纪律红线见上，优先级最高；完整索引见 .orchd/rules/README.md，按需 Read，勿全量载入）
- 会话/优先级/接管/claim 两段式 → rules/session.md   ·  摄入 IDEAS→注册 → rules/intake.md
- verify_command（120s / --basetemp）→ rules/verify.md   ·  分支/提交/merge/exempt → rules/git.md
- 审查（templates/spec-reviewer.md、templates/code-reviewer.md + 证据分层）→ rules/review.md
- 测试纪律（复用 tests/conftest.py 的 make_task / orchd_dir，参数化，禁止在测试文件内另造副本）→ rules/testing.md
- 安装 → rules/install.md   ·  事故/Windows/引擎边界 → rules/{recovery,windows,safety}.md
## Rules
- One task per session. Exit after `python .orchd/__main__.py done`.
- Reviewer exception: complete both review phases (spec + code) of one task in the same session.
- Maintain --exclude list across retries. Feed it back to step 1.
- `--role` 已移除：认领角色由引擎按任务状态自动分流（in_review→审查，pending→实现），身份由宿主注入的 `ORCHD_SESSION_ID` 自动派生会话指纹。
- 审查者身份为当前会话指纹；实现者禁自审（E016 防自审指纹校验），不再依赖任务的 `reviewers` 名单字段。
- Never read files outside the file list provided by CLI (WORKER mode). In BOOTSTRAP mode, reading `requirements.md` is required.