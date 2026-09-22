# Orchid Agent
> agent 工作流协议（BOOTSTRAP / WORKER 与纪律）；使用手册见 [docs/user-manual.md](../docs/user-manual.md)。
> 体积口径：字符（本文件 ≤7200 字符，逼近时外置细则至 rules/，仅留协议概览与索引链接）。

## 入口协议（MUST，每 session 首读）
1. 读 `.orchd/SKILL.md`（本文件；纪律红线优先级最高，见下）。
2. 定位 `.orchd/` 资源：规则见 `.orchd/rules/`（索引 `rules/README.md`），模板见 `templates/`。
3. 按 `guidance` 导航：知识（`read`）→ 方法（`template`）→ 动作（`command`）；字段缺失即跳过。
4. **输出契约**：stdout 纯 JSON（guidance 对象；无内容则省略键）；人看 stderr，agent 只读 stdout。
5. **转述契约**：收到 guidance 即转述下一步（初始化 SVG 卡片，日常引用块 + 粗体 project_view / agent_view）。SVG 卡片 = 首次入场时以 SVG 图形呈现的整体架构概览。

## Determine your mode
- `.orchd/` 存在 → **WORKER**（摄入 / 纪律红线 / rules / guidance 全能力）。
- `.orchd/` 不存在 → **BOOTSTRAP**（自启动安装，走 [skill-bootstrap.md](skill-bootstrap.md)）。
- SELF-HOSTED 已并入统一协议（两模式：WORKER / BOOTSTRAP）；`.orchd/shared/self-hosted` 仅来源标记，不是能力开关。

## WORKER mode（已有项目）
- **零根入口**：统一 `python .orchd/__main__.py <命令>`，宿主根零额外文件免安装。
- **无感引导**：JSON 响应自动附加 `guidance`，逐层导航 request/claim/done/review。
- **多 worktree 并行**：生命周期引擎自动管理；merge 在主工作树执行（见 rules/git.md）。

## 纪律红线（MUST / MUST NOT，违反 = 事故；优先级最高）
**MUST NOT**（禁止项；每条约满足条件 → 禁止动作 → 例外/出口）：
1. **禁手动 git 写操作**：任何会话中，不得执行 `git checkout / branch / reset / stash / gc / prune / clean / rebase / cherry-pick / merge / push`。git 写操作仅由引擎自动执行；豁免 = 任务分支上的 `git commit` + 任务分支上的 `orchd git merge main`（精确形态，受管出口，详见 rules/git.md）。若确需其他手动 git 写，先向人报告获许可。
2. **禁破坏性 git**：`reset --hard` / `gc --prune=now` / `clean -fdx` / `branch -D` / `push --force` 一律禁止，无豁免。
3. **禁改范围外文件**：只读 files_to_read、只写 files_to_edit；不触 `.git/`；删文件前确认属验收范围。确需增删声明文件时回主工作树执行 amend 补登，不手改 _master.json。
4. **禁绕过身份（自审三档）**：
   - 默认（enforce=false）：`self_review_notice` 标注仅提示，不阻断。
   - `config.enforce_self_review_block=true`：E016 硬阻断。
   - 单会话自托管确需自审时：须在 review comments 显式披露。
5. **禁未提交即中断**：改动后必须提交（或报告未提交原因）才结束；session end 硬拦截，`--force` 可放行。
6. **禁止自动摄入与自动写入**：intake 仅用户指定；灵感只 `idea propose` 记 study；confirm/drop 仅用户可执行。
7. **禁擅自 auto-claim**：`request --auto-claim` 默认拒绝（E032），仅 `config.allow_auto_claim: true` 时可调。
8. **禁任务分支执行 intake/amend**：amend 仅在 canonical 主工作树 main 且工作区干净时执行；任务分支 E007 拒绝，回主工作树补声明。
9. **禁手改运行时文件**：`_ledger.jsonl` / `_checkpoint.json` / `mod-*/spec.json` 由引擎维护，不手改；只读诊断（`status` / `doctor`）不受限，数据异常按 rules/recovery.md 处置或报告。
10. **版本进发先落地再摄入**：新版本先 `roadmap-land`（= 将未来版本规划落地为 IDEAS pending）再摄入；临时想法直接写 IDEAS.md。
11. **禁绕过 claim**：任务必须经 claim 建分支；禁手动 `git branch/checkout` 创建。claim 失败或分支异常时按 guidance 处置并报告，不自行建分支绕过。
12. **禁任务悬空**：claim 后必须 done → 审查 → merge；中断/放弃先 retract。
13. **禁声明文件漏提交**：files_to_edit 声明文件必须随分支提交进 diff；不在主工作树直接改任务文件；无需改动走 amend。
14. **禁无候选自行领任务**：request 返回 candidate=None / next_action=exit 时，禁止自行 claim、重试 request、`--auto-claim`；立即停止报告等指令。

**MUST**（强制项）：条件 → 动作 → 出口/核实：
1. 会话开始三连检查：`git status` + `git branch --show-current` + `python .orchd/__main__.py status`。
2. 不读不写；写前先读。
3. done / review / amend 后核对引擎响应（verify 结果、commit、状态流转）。
4. 测试/verify 用 `--basetemp` 指向系统临时目录，禁止项目内残留临时文件。
5. 任何异常立即停止报告，不擅自处置；session 结束工作区干净或说明。
6. completed 关闭前运行 `status --audit-task` 清零声明文件完整性告警。
7. 准入/会话锁（E012/E019）不盲重试：先查持有者，正常并发释放后自动成功；僵死则等其退出或接管后再重试。
8. **读取纪律（A1/A2/A3，机器可判据）**：
   - A1 按digest跳过重读：先 `python .orchd/__main__.py context-digest` 取必读面sha256，与上次比对；哈希一致即跳过，仅重读变化项。引擎只吐哈希、跳过与否由宿主/agent决定。
   - A2 大文件定位读：>64KB 禁整读，grep/offset 定位读。
   - A3 响应即定义：claim 响应已含任务定义与 review_comments，不再读 master.json。

## 按 exit_type 行动（错误出口处置纪律）
> `guidance.exit_type` 是处置唯一依据（出处：docs/implementation-design.md §3.2 五分法）。
> 处置动作遵循上面「选择 exit_type 的行动」，同时遵守「纪律红线」——尤其 git-diagnose 类**禁手动 git 写**。

| exit_type | 动作 | 红线 |
|---|---|---|
| `exec-command` | 照 command 执行后重试 | 禁跳过 command |
| `git-diagnose` | 只读取证 → 按 recovery 处置 | **禁手动 git 写** |
| `manual-action` | 按 recovery 步骤改 | 确认对象后执行 |
| `await-external` | 查持有者 → 等待，**禁重试** | E009/E011/E012/E019 |
| `continue` | warning 不阻断，可继续 | 深层征兆 → lesson report |

四通道与码 → 通道登记见 [rules/recovery.md](rules/recovery.md)。

## 身份约定（会话级指纹）
- 身份 = `ORCHD_SESSION_ID` 派生 12 位 hex 指纹；同对话不变，不同对话不同。
- 归属 / 忙度 / 自审 / 锁所有权以指纹为主键；同 agent 不同 session 可并行领不同任务。
- 自审口径（默认提示 / E016 / comments 披露）见红线 4 与 rules/session.md。

## 规则目录（见 rules/README.md）
- 会话/claim → rules/session.md · 摄入 → rules/intake.md · verify → rules/verify.md
- 分支/merge → rules/git.md · 审查 → rules/review.md（模板 templates/spec-reviewer.md + templates/code-reviewer.md；语义变更三查；`review --show` 只读回看历史意见）
- 测试纪律（复用 tests/conftest.py make_task/orchd_dir，参数化，不得另造副本）→ rules/testing.md
- 安装 → rules/install.md · 恢复 → rules/recovery.md · lesson → [skill-lesson.md](skill-lesson.md)

## Rules
- One task per session. Exit after `python .orchd/__main__.py done`.
- 引擎语义变更同步：新增状态 / 流程 / 规则文件 / 命令时须同步引导层三查（step 词表登记 / read-template 路由完备 / hint 与命令一致），详见 rules/review.md。
- 认领角色按状态自动分流，身份由 `ORCHD_SESSION_ID` 派生；自审见「禁绕过身份」红线。