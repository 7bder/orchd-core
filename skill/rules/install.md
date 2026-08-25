# 安装规则（Agent 无感安装协议 + BOOTSTRAP mode）

> 低频块外置自 `.orchd/SKILL.md`（task-skill-as-entry-docs，2026-08-15）：SKILL 仅留一句指针，本文件承载全文，零信息丢失。
> 适用场景：新项目首次进入（项目无 `.orchd/`）时阅读；已有 `.orchd/` 的项目无需每次读本文件。

## Agent 无感安装协议（task-121-docs，2026-08-15）

检测到**项目无 `.orchd/`**（BOOTSTRAP 前置）时，agent 按**通用协议**自行完成 orchd-core 安装，无需等待人或平台专属 skill 插件：
1. `git clone` orchd-core 源码仓库（通用安装协议见 `release/README.md`）。
2. 运行安装器 `python orchd-core/install.py <宿主路径> --agent`（`--agent` 输出纯 JSON，便于无人值守解析）。
3. 安装器组装出宿主 `.orchd/`（vendored 引擎 + 资源 + SKILL + 零根入口 + `shared/`/`proposals/` 骨架），随后进入下方 BOOTSTRAP 模式。

> 约定：**不写平台专属 skill 插件**（如 `.claude/`、`.cursor/` 定制），统一走该通用安装协议，保证跨平台一致性。已存在 `.orchd/` 时如需升级，用 `python install.py <宿主路径> --update`（保留宿主数据）。

## BOOTSTRAP mode (first agent in a new project)

1. Read `requirements.md`; run `python .orchd/__main__.py bootstrap` — outputs master schema, architect prompt, decomposition guide
2. Create `.orchd/_master.json` following the schema (write `.orchd/shared/architecture.md` + `conventions.md` before validate if `shared` declared, E005)
3. Run `python .orchd/__main__.py validate`, then `python .orchd/__main__.py init` (snapshots + empty ledger)
4. Project ready. Exit. Next session enters WORKER mode automatically.