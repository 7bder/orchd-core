# 安装规则（Agent 无感安装协议 + BOOTSTRAP mode）

> TL;DR: ① BOOTSTRAP 自启动安装流程 ② --cleanup 安装后自动删除克隆源（无痕安装） ③ 安装/claim 流程自动启用仓库自带 `.githooks`（`core.hooksPath` 是本地配置、不随 clone 传播；已自定义为其他路径则仅提示不改写）

> 低频块外置自 `.orchd/SKILL.md`（task-skill-as-entry-docs，2026-08-15）：SKILL 仅留一句指针，本文件承载全文，零信息丢失。
> 适用场景：新项目首次进入（项目无 `.orchd/`）时阅读；已有 `.orchd/` 的项目无需每次读本文件。

## Agent 无感安装协议（task-121-docs，2026-08-15）

检测到**项目无 `.orchd/`**（BOOTSTRAP 前置）时，agent 按**通用协议**自行完成 orchd-core 安装，无需等待人或平台专属 skill 插件：
1. `git clone` orchd-core 源码仓库（通用安装协议见 `release/README.md`）。
2. 运行安装器 `python orchd-core/install.py <宿主路径> --agent --cleanup`（`--agent` 输出纯 JSON，便于无人值守解析；`--cleanup` 安装成功后自动删除克隆源 `orchd-core/`，宿主根零残留）。
3. 安装器组装出宿主 `.orchd/`（vendored 引擎 + 资源 + SKILL + 零根入口 + `shared/`/`proposals/` 骨架），随后进入下方 BOOTSTRAP 模式。

> 约定：**不写平台专属 skill 插件**（如 `.claude/`、`.cursor/` 定制），统一走该通用安装协议，保证跨平台一致性。已存在 `.orchd/` 时如需升级，用 `python install.py <宿主路径> --update`（保留宿主数据；需保留克隆源以便升级则不加 `--cleanup`）。

## 新 clone 后 hooks 如何生效（task-decl-hooks-autoset）

仓库自带 `.githooks/`（如 `pre-push` 发版同步保护）**随 clone 检出**，但 `core.hooksPath` 是 **git 本地仓库配置**（写在 `.git/config`），**不随 clone 传播**——新 clone 检出后 `.githooks/` 不生效，质量门禁形同虚设。

**安装流程自动启用**（`python install.py <宿主> [--update|--force]`，含 `--agent` 无感安装）：安装器装好 `.orchd/` 后检查宿主仓库，按下表处置（幂等）：

| 仓库状态 | 处置 |
|---|---|
| 非 git 仓库 / 仓库内无 `.githooks/` | 不动（`not_a_git_repo` / `hooks_dir_missing`） |
| `core.hooksPath` 未设置或为空 | 自动设为 `.githooks`（相对仓库根，多 worktree 下各自生效） |
| 已指向 `.githooks`（相对或等价绝对路径） | 幂等跳过，不重复改写 |
| 已指向其他路径（用户显式自定义） | **不改写**，仅提示手动改法；不覆盖用户配置 |

处置结果见安装 JSON 的 `hooks_path` 字段（`reason` ∈ `set` / `already_set` / `custom_hooks_path` / `hooks_dir_missing` / `not_a_git_repo`）；非 `--agent` 模式另在 stdout 打印一行人类可读提示。

**claim 期兜底**：任务 `claim` 安装 L3 pre-commit hook 前，引擎执行同一检查（`orchd/gitops/hook._ensure_hooks_path`，best-effort，先于 hooks 目录解析），保证 pre-commit 落到 `core.hooksPath` 指向的目录而非回退 `.git/hooks`；结果附在 `hook_install` 返回的 `hooks_path` 字段。既有 hooksPath 解析语义（相对 → 相对仓库根 / 绝对 → 原样 / 缺省 → `.git/hooks`）不变。

**手动启用（未走安装器时）**：

```bash
git config core.hooksPath .githooks     # 相对仓库根；每台机器 / 每个 clone 各需设置一次
git config --get core.hooksPath         # 校验
```

> `core.hooksPath` 属本地配置，不入库、不共享；若仓库已自定义为别的 hooks 目录，请沿用自定义目录，不要强行改为 `.githooks`。

## BOOTSTRAP mode (first agent in a new project)

1. Read `requirements.md`; run `python .orchd/__main__.py bootstrap` — outputs master schema, architect prompt, decomposition guide
2. Create `.orchd/_master.json` following the schema (write `.orchd/shared/architecture.md` + `conventions.md` before validate if `shared` declared, E005)
3. Run `python .orchd/__main__.py validate`, then `python .orchd/__main__.py init` (snapshots + empty ledger)
4. Project ready. Exit. Next session enters WORKER mode automatically.