# Orchid Agent
> agent 工作流协议（BOOTSTRAP / WORKER 两模式与纪律），宿主项目存于 **`.orchd/SKILL.md`**。人向使用手册见 [docs/user-manual.md](../docs/user-manual.md)。

## 入口协议（MUST，每 session 首读）
1. 读 `.orchd/SKILL.md`（纪律红线优先级最高，见下）。
2. 定位 `.orchd/` 资源：规则见 `.orchd/rules/`（索引 `rules/README.md`），模板见 `templates/`。
3. 按命令响应 `guidance` 导航：知识(`read`)→方法(`template`)→动作(`command`)；字段缺失即跳过。
4. 经宿主根 `AGENTS.md` 接入：检测 `.orchd/` →无则自启安装→ 回本文件。
5. **输出契约**：stdout 纯 JSON（guidance 为对象或省略，无内容省略键）；人看提示走 stderr，agent 只读 stdout。
6. **转述契约**：收到 guidance 后向用户转述下一步；初始化用 SVG 卡片，日常用引用块 + 粗体 project_view / agent_view。

## Determine your mode
- `.orchd/` 存在 → WORKER（摄入 / 纪律红线 / rules / guidance 全能力）
- `.orchd/` 不存在 → BOOTSTRAP（自启动安装，见 [skill-bootstrap.md](skill-bootstrap.md)）
- `.orchd/shared/self-hosted` 仅来源标记，不是能力开关。

## WORKER mode（已有项目）
- **零根入口**：统一 `python .orchd/__main__.py <命令>`，宿主根零额外文件、免装 orchd。
- **无感引导**：JSON 响应自动附加 `guidance`，逐层导航 request/claim/done/review；可忽略自行决策。
- **多 worktree 并行**：生命周期引擎自动管理（claim 创建、终态回收、孤儿清理）；各 worktree 共享账本（`ORCHD_HOME` 可覆盖）；merge 由引擎在主工作树执行，任务 worktree 永不 checkout main。详见 rules/git.md。

## 纪律红线（MUST / MUST NOT，违反 = 事故；优先级最高）
**MUST NOT**：
1. **禁手动 git 写操作**：不得 `git checkout / branch / reset / stash / gc / prune / clean / rebase / cherry-pick / merge / push`；仅引擎自动执行，唯一豁免任务分支 `git commit`；确需手动先报告获许可。
2. **禁破坏性 git**：`reset --hard` / `gc --prune=now` / `clean -fdx` / `branch -D` / `push --force` 一律禁止。
3. **禁改范围外文件**：只读 files_to_read、只写 files_to_edit；不触 `.git/`；删文件前确认属验收范围。
4. **禁绕过身份**：一 session 一身份；不得多身份完成同任务链（含自审）。
5. **禁未提交即中断**：改动后必须提交（或报告"未提交+原因"）才可结束。**引擎已硬校验（session end 拦截，--force 可放行）**
6. **禁止自动摄入与自动写入**：intake 仅用户指定；灵感只 `idea propose` 记 study，confirm/drop 仅用户可执行。
7. **禁擅自 auto-claim**：`request --auto-claim` 默认拒绝（E032），仅 `config.allow_auto_claim: true` 时可调。
8. **禁任务分支执行 intake/amend**：只在 main 且工作区干净时执行。
9. **禁手改运行时文件**：`_ledger.jsonl` / `_checkpoint.json` / `mod-*/spec.json` 由引擎维护。
10. **版本进发先落地再摄入**：新版本先 `roadmap-land` 落地为 IDEAS pending 再摄入；临时想法直接写 IDEAS.md。
11. **禁绕过 claim**：任务必须经 claim 建分支；禁手动 git branch/checkout 创建。
12. **禁任务悬空**：claim 后必须 done → 审查 → merge；中断/放弃先 retract。
13. **禁声明文件漏提交**：files_to_edit 声明文件必须随分支提交进 diff；不在主工作树直接改任务文件；无需改动走 amend。
14. **禁无候选自行领任务**：request 返回 candidate=None / next_action=exit 时，禁止自行 claim、重试 request、--auto-claim；立即停止报告等指令。
**MUST**：
1. session 开始三连检查：`git status` + `git branch --show-current` + `python .orchd/__main__.py status`。
2. 不读不写；写前先读。
3. done / review / amend 后核对引擎响应（verify 结果、commit、状态流转）。
4. 测试/verify 用 `--basetemp` 指向系统临时目录，禁止项目内残留临时文件。
5. 任何异常（verify 失败 / merge 冲突 / 状态不符）立即停止报告，不自行猜测处置。
6. session 结束工作区干净，或在报告中说明。
7. completed 关闭前运行 `status --audit-task` 清零声明文件完整性告警。
8. 准入/会话锁（E012/E019）不盲重试：先查持有者，正常并发释放后自动成功；僵死则等其退出（flock 进程退出自动释放）或接管后再重试。

## 按 exit_type 行动（错误出口处置纪律）

> `guidance.exit_type` 是处置唯一依据（设计 §3.2 五分法）。

| exit_type | 动作 | 红线 |
|---|---|---|
| `exec-command` | 照 command 执行后重试 | 禁跳过 command |
| `git-diagnose` | 只读取证→按 recovery 处置 | **禁手动 git 写** |
| `manual-action` | 按 recovery 步骤改 | 确认对象后执行 |
| `await-external` | 查持有者→等待，**禁重试** | E009/E011/E012/E019 |
| `continue` | warning 不阻断，可继续 | 深层征兆→lesson report |

四通道(A异常/B批量/C手工dict/D Shell hook)与码→通道登记见 [rules/recovery.md](rules/recovery.md)。

## 身份约定（会话级指纹）
- 身份 = `ORCHD_SESSION_ID` 派生 12 位 hex 指纹；同对话不变，不同对话不同。
- 归属 / 忙度 / 自审 / 锁所有权以指纹为主键；同 agent 不同 session 可并行领不同任务。
- 自审默认仅提示不阻断（self_review_notice），决策权在人；线上可设 `config.enforce_self_review_block=true` 阻断。
- 生命周期与违约后果详见 rules/session.md。

## 规则目录（见 rules/README.md）
- 会话/claim → rules/session.md · 摄入 → rules/intake.md · verify → rules/verify.md
- 分支/merge → rules/git.md · 审查(templates/spec-reviewer.md + templates/code-reviewer.md) → rules/review.md
- 测试纪律(复用 tests/conftest.py make_task/orchd_dir，参数化，不得另造副本) → rules/testing.md
- 安装 → rules/install.md · 恢复 → rules/recovery.md · lesson → [skill-lesson.md](skill-lesson.md)

## Rules
- One task per session. Exit after `python .orchd/__main__.py done`.
- 审查模式缺省 two_phase；`project.review_mode: "unified"` 启用单阶段。
- request 无候选即停止：不重试、不自行 claim，报告后等指令。
- 认领角色按状态自动分流，身份由 `ORCHD_SESSION_ID` 派生；实现者禁自审(E016)。
