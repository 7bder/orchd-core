# 规则索引（rules/README.md）

> 轻量可读索引，供人/agent 主动查与全局视角。不复制规则正文——按需 Read 对应文件。
> 外置自 .orchd/SKILL.md 规则目录（task-skill-as-entry-docs，2026-08-15）。

## 规则表（10 文件）

| 文件 | 主题 | 何时读 |
|------|------|--------|
| `session.md` | 状态检查 / 优先级 / 接管 / claim 两段式 | 会话开始、claim 前 |
| `intake.md` | 摄入 IDEAS → 注册任务 | 摄入/注册 |
| `verify.md` | verify_command（120s / 模块定向 / --basetemp） | done 前 |
| `git.md` | 分支 / 提交 / merge / amend / exempt_files | 提交/merge 前 |
| `review.md` | 审查者 / 禁自审 / 证据分层 / 模板 | 审查 |
| `testing.md` | 测试纪律（复用 conftest / 参数化） | 写测试 |
| `recovery.md` | 仓库对象/refs 丢失恢复 SOP | 事故 |
| `windows.md` | Windows / 管道编码 | Windows 环境 |
| `safety.md` | 引擎改动触碰 §9.1 停服边界 | 改引擎 |
| `install.md` | 无感安装协议 + BOOTSTRAP | 新项目首次进入 |

## 外置协议文件（自 SKILL.md 外置，SKILL 内留入口链接）

| 文件 | 主题 | 何时读 |
|------|------|--------|
| `skill-bootstrap.md` | BOOTSTRAP 自启动安装全流程 | 宿主无 `.orchd/`、新项目首次进入 |
| `skill-lesson.md` | 经验回灌（lesson）命令入口 | 执行中打点 / 收尾审核 |

## 方法表（templates + CLI）

| 场景 | 模板 |
|------|------|
| 任务分解 | `templates/architect.md` |
| 实现 | `templates/implementer.md` |
| spec 审查 | `templates/spec-reviewer.md` |
| code 审查 | `templates/code-reviewer.md` |

CLI 命令（`python .orchd/__main__.py <cmd>`，完整列表见 `docs/user-manual.md`）：
validate / bootstrap / init / amend / request / pool / claim / done / review /
retract / force-status / status / watchdog / ideas-archive / doctor / intake。
按 `guidance` 字段（`read`/`template`/`command`）自动导航，无需主动翻表。