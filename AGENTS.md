# AGENTS.md

本仓库（orchd-core）是 **orchd 引擎的源码发行版**。任何 agent 进入本仓库，请按以下步骤接入。

## 接入（MUST，每 session 首读）

1. 读 `skill/SKILL.md` 进入工作流——它是协议适配层，定义 BOOTSTRAP / WORKER 模式与全部纪律。
2. 完整规则按需读 `skill/rules/`（索引 `skill/rules/README.md`），勿全量载入。

## 把 orchd 装进宿主项目

宿主项目（你自己的项目）没有 `.orchd/` 时，自动安装（二选一）：

- GitHub：`git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent`
- Gitee（国内镜像）：`git clone https://gitee.com/QQ7bder/orchd-core.git && python orchd-core/install.py . --agent`

装好后读宿主 `.orchd/SKILL.md` 进入工作流。

## 工作流

- **BOOTSTRAP**（新项目）：`bootstrap` → 写 `.orchd/_master.json` → `validate` → `init` → 项目就绪
- **WORKER**（日常任务）：`request → claim → 实现 → done → review`，每一步的下一步由命令响应的 `guidance` 字段自动导航

所有命令统一用 `python .orchd/__main__.py <命令>` 调用，stdout 为纯 JSON。

## 纪律红线（违反任意一条 = 事故）

- **禁止手动 git 写操作**：不得执行 `git checkout / branch / reset / stash / merge / push` 等。git 操作只允许引擎自动执行（claim 建分支、code APPROVED merge、done/amend 自动提交）。唯一豁免：任务分支上的 `git commit`。
- **禁止破坏性 git 操作**：`git reset --hard`、`git clean -fdx`、`git branch -D`、`git push --force` 一律禁止。
- **禁止修改范围外文件**：只读 `files_to_read`、只写 `files_to_edit`，不得触碰 `.git/` 内部结构。
- **禁止绕过身份**：一个 session 只允许一个身份；不得自审（实现 + 审查同一身份）。
- **禁止未提交即中断**：改动文件后必须提交（或明确报告原因）才能结束 session。
- **禁止绕过 claim**：任务必须经 `claim` 由引擎建分支，禁止手动 `git branch/checkout` 创建任务分支。
- **禁止手改 .orchd 运行时文件**：`_ledger.jsonl`、`_checkpoint.json`、`mod-*/spec.json` 由引擎维护，一律不得手改。

## 规则目录

完整规则按需 Read，勿全量载入：

- 会话/优先级/接管/claim 两段式 → `skill/rules/session.md`
- 摄入 IDEAS → 注册 → `skill/rules/intake.md`
- verify_command → `skill/rules/verify.md`
- 分支/提交/merge/exempt → `skill/rules/git.md`
- 审查 → `skill/rules/review.md`
- 测试纪律 → `skill/rules/testing.md`
- 安装 → `skill/rules/install.md`
- 事故/Windows/引擎边界 → `skill/rules/{recovery,windows,safety}.md`
