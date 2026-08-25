# Orchid Agent
> agent 工作流协议（BOOTSTRAP / WORKER 两模式与纪律），存放于宿主项目 **`.orchd/SKILL.md`**。面向人的安装、命令参考与故障排查见 [docs/user-manual.md](../docs/user-manual.md)。

## 入口协议（MUST，每 session 首读）
1. 读本文件 `.orchd/SKILL.md`（纪律红线优先级最高，见下）。
2. 据此定位 `.orchd/` 资源：规则见 `.orchd/rules/`（索引 `rules/README.md`），模板见 `templates/`。
3. 按任意命令响应的 `guidance` 字段导航下一步，形成知识（`read` 该读的规则文件）→ 方法（`template` 该用的模板）→ 动作（`command` 下一步命令）的路由闭环。任一字段缺失即跳过，不阻塞后续。
4. 若经宿主根 `AGENTS.md` 接入：遵循其引导流程（检测 `.orchd/` → 无则自启安装 → 回到本文件）。
5. **输出契约**：所有命令 stdout 为纯 JSON（含 `guidance`）；人看的"下一步"提示走 stderr（`orchd ▸` 块），不污染 stdout。agent 只读 stdout。
6. **转述契约**：收到 `guidance` 后须向用户转述下一步（用户主动问「下一步」或任务完成后需用户决策时显式提示）。初始化用 SVG 全貌卡片（`card` 字段）；日常用 Markdown 引用块 + 粗体转述 `project_view` 与 `agent_view` 两条。

## Determine your mode
- `.orchd/` 存在 → WORKER（摄入 / 纪律红线 / rules / guidance 全能力）
- `.orchd/` 不存在 → BOOTSTRAP（自启动安装，见下）
- `.orchd/shared/self-hosted` marker 仅作来源标记，不是能力开关。

## BOOTSTRAP mode（新项目，自启动安装）
宿主无 `.orchd/` 时，agent 凭本文件即可自我启动：

```bash
git clone https://github.com/7bder/orchd-core.git   # 国内镜像：git clone https://gitee.com/QQ7bder/orchestra.git
python orchd-core/install.py . --agent
python .orchd/__main__.py bootstrap
```

安装后按 `bootstrap` 输出写 `.orchd/_master.json` → `validate` → `init`，项目即就绪，进入 WORKER 模式。

## WORKER mode（已有项目）
- **零根入口**：所有命令统一用 `python .orchd/__main__.py <命令>` 调用，宿主项目根零额外文件、无需安装 `orchd`。
- **无感引导**：任意命令 JSON 响应自动附加 `guidance` 字段，据其逐层导航即可完成任何任务流程（request/claim/done/review）；可忽略自行决策。
- **多 worktree 并行**：任务 worktree 全生命周期由引擎自动管理（claim 自动创建、终态自动回收、孤儿惰性清理），agent 零 worktree 管理操作。双布局兼容（container `main/` + 平级 `task-<id>/`，flat 零迁移）；各 worktree 共享同一账本（`ORCHD_HOME` 可覆盖）；merge 由引擎在主工作树执行，任务 worktree 永不 checkout main。并行仅限无依赖就绪任务。详见 rules/git.md。

## 纪律红线（MUST / MUST NOT，违反任意一条 = 事故）
针对纪律遵守较弱的 agent 的硬约束，不因"不知情 / 为了效率 / 任务需要"豁免。本清单优先级高于本文件其他任何章节。

**MUST NOT（绝对禁止）**：
1. **禁止手动 git 写操作**：不得执行 `git checkout / branch / reset / stash / gc / prune / clean / rebase / cherry-pick / merge / push`。git 操作只允许引擎自动执行（claim 建分支、code APPROVED merge、done/amend 自动提交）。唯一豁免：任务分支上的 `git commit`。确需其他手动 git 操作时，先向用户报告并获明确许可。
2. **禁止破坏性 git 操作**：`git reset --hard`、`git gc --prune=now`、`git clean -fdx`、`git branch -D`、`git push --force` 一律禁止——对象清理后历史不可恢复。
3. **禁止修改范围外文件**：只读 `files_to_read`、只写 `files_to_edit`；不得修改/删除/移动任何范围外文件，不得触碰 `.git/` 内部结构。删文件前先确认属于任务验收范围。
4. **禁止绕过身份**：一个 session 只允许一个身份；不得用多个身份完成同一任务链（含自审）。
5. **禁止未提交即中断**：改动文件后必须提交（或明确报告"未提交+原因"）才能结束 session。
6. **禁止自动摄入与自动写入**：摄入（intake）仅在用户明确指定时执行；灵感只能 `idea propose` 记入 study，confirm/drop 仅用户可执行。
7. **禁止擅自无人值守自动认领**：`request --auto-claim` 默认被引擎拒绝（E032），仅当 `_master.json` 顶层 `config.allow_auto_claim` 显式为 `true` 时才能调用。
8. **禁止在任务分支执行 intake/amend**：只在 main 且工作区干净时执行。
9. **禁止手改 .orchd 运行时文件**：`_ledger.jsonl`、`_checkpoint.json`、`mod-*/spec.json` 由引擎维护，一律不得手改。
10. **版本进发先落地再摄入**：新版本规划先用 `roadmap-land <版本>` 落地为 IDEAS pending 再摄入；无规划的临时想法可直接写入 IDEAS.md。
11. **禁止绕过 claim**：任务必须经 `claim` 由引擎建分支；禁止手动 `git branch/checkout` 创建任务分支。
12. **禁止任务悬空**：claim 后必须走完 done → 审查 → merge；中断/放弃必须先 retract。
13. **禁止声明文件漏提交/漏声明**：`files_to_edit` 声明文件必须随任务分支提交并进入 diff，否则 done 与 review 均被引擎硬门禁拒绝。不得在主工作树直接改任务文件；声明文件确无需改动时走 `amend` 移除或补充说明。

**MUST（强制动作）**：
1. session 开始三连检查：`git status` + `git branch --show-current` + `python .orchd/__main__.py status`。
2. 写文件前先读文件；不读不写。
3. 任务完成（done / review / amend）后，必须核对引擎响应（verify 结果、commit 是否执行、状态流转）。
4. 测试/verify 一律用 `--basetemp` 指向系统临时目录，禁止项目内残留临时文件。
5. 任何异常（verify 失败、merge 冲突、状态不符）立即停止并报告，不自行猜测处置。
6. session 结束时工作区必须干净（无未提交改动），或在报告中说明。
7. completed 任务关闭前运行 `status --audit-task` 实证声明文件完整性告警清零。

## 身份约定（会话级指纹）
- 身份 = 宿主注入 `ORCHD_SESSION_ID` 派生的 12 位 hex 会话指纹；同一对话内指纹不变，不同对话不同指纹。
- 归属 / 忙度 / 自审 / 锁所有权均以会话指纹为主键；同 agent 不同 session 视为不同身份，可并行领取不同任务。
- 自审默认仅提示不阻断（认领附 `self_review_notice`），决策权在人；线上版可设 `config.enforce_self_review_block=true` 恢复阻断。
- 会话生命周期、宿主注入契约与违约后果详见 rules/session.md。

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
- 认领角色由引擎按任务状态自动分流（in_review→审查，pending→实现），身份由 `ORCHD_SESSION_ID` 自动派生会话指纹。
- 实现者禁自审（E016 防自审指纹校验），不再依赖任务的 `reviewers` 名单字段。
- Never read files outside the file list provided by CLI (WORKER mode). In BOOTSTRAP mode, reading `requirements.md` is required.
