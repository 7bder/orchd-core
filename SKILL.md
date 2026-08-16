# Orchd Agent
> agent 工作流协议（BOOTSTRAP / WORKER 两模式与纪律，SELF-HOSTED 已并入统一协议，`self-hosted` marker 仅作来源标记），存放于宿主项目 **`.orchd/SKILL.md`**（零根文件 + 自包含 `.orchd/` 模型，task-12-docs-roadmap）；面向**人**的安装、命令参考与故障排查使用指南见 [docs/user-manual.md](../docs/user-manual.md)；面向**skill 分发渠道**的独立引导包见 `skills/orchd/`（标准 Agent Skill，供 skills.sh / 千问广场 / Claude skills 发布用）。

## 入口协议（MUST，每 session 首读）
1. 读本文件 `.orchd/SKILL.md`（纪律红线优先级最高，见下纪律红线节）。
2. 据此定位 `.orchd/` 资源：规则见 `.orchd/rules/`（索引 `rules/README.md`），模板见 `templates/`。
3. 按任意命令响应的 `guidance` 字段导航下一步（知识路由 `read`/`template` + 动作路由 `command`）；遇具体问题按规则目录按需 Read。
4. 若当前 session 经 skill 分发接入（非读本文件路径）：遵循 `skills/orchd/` 引导流程（检测 `.orchd/` → 无则自启安装 → 回到本文件）。

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
4. **禁止绕过身份**：一个 session 只允许一个 agent ID；不得用多个 ID 完成
   同一任务链（实现 + 审查必须分属不同 session——2026-08-05 实踩：同一
   session 以 workbuddy-1 实现 + reviewer-1 审查，属自审绕过）。
5. **禁止未提交即中断**：改动文件后必须提交（或明确报告"未提交+原因"）才能
   结束 session；不得把未提交改动留在工作区后静默离开（2026-08-05 实踩：
   trae-a1 改 SKILL.md 未提交即中断）。**引擎兜底（intake-commit-enforcement，
   2026-08-14）**：摄入/注册流程由引擎强制提交——amend 前置"非摄入产物干净"
   守卫（E017 阻断）+ commit 失败可审计（commit_warning）+ `orchd intake`
   命令提交摄入产物 + `orchd status --audit-intake` 巡检未提交摄入产物。
6. **禁止自动摄入与自动写入（idea-write-gate，2026-08-15 扩展）**：摄入（intake）仅在用户明确指定时执行；不得主动摄入 IDEAS.md 的 pending 条目（2026-08-05 用户裁定）。灵感写入同样受限——对话讨论产生的灵感不得直接写 pending：agent 只能 `idea propose` 记入 study（论证中），**confirm/drop 仅用户可执行**（2026-08-15 用户裁定，把"值不值得做"的判断权交还用户）。
7. **禁止在任务分支执行 intake/amend**：intake/amend 只在 main 且工作区
   干净时执行（引擎 not_on_main 兜底存在，但不得依赖）。
8. **禁止手改 .orchd 运行时文件**：`_ledger.jsonl`、`_checkpoint.json`、
   `mod-*/spec.json` 等运行时状态由引擎维护，agent 一律不得手改。
9. **版本进发先落地再摄入（intake-dual-path，2026-08-15）**：进入新版本规划时，
   先把 ROADMAP 规划章节用 `python .orchd/__main__.py roadmap-land <版本>` 落地为
   IDEAS pending 条目，再走摄入拆解；`validate` 以 E031 兜底检出带 id 且未落地的
   规划章节。无规划的临时想法可直接写入 IDEAS.md 走摄入，不必落地。
10. **禁止绕过 claim**：任务必须经 `python .orchd/__main__.py claim` 由引擎建分支；禁止手动
    `git branch/checkout` 创建任务分支（含不规范命名，如 task/task-1——
    2026-08-05 实踩：绕过 claim 手动建分支，实现悬空未 merge）。
11. **禁止任务悬空**：claim 后必须走完 done → 双阶段审查 → merge；
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
- `--role` defaults to `implementer`. Use `--role reviewer` for reviews.
- Reviewer agent ID must match an entry in the task's `reviewers` field.
- Never read files outside the file list provided by CLI (WORKER mode). In BOOTSTRAP mode, reading `requirements.md` is required.