# orchd-core

跨 AI agent 平台的任务编排 CLI 核心套件。本仓库（orchd-core）是 **orchd 引擎的源码发行版**——宿主项目通过**安装器**把引擎与资源组装进自身 `.orchd/`，形成单一 `.orchd/` 自包含工作空间，宿主项目根零额外文件。

orchd 不做需求推理、不内置 LLM 调用。它只为一个项目里的多个 AI agent 提供可靠的协作基础设施：事件溯源账本、文件锁并发控制、DAG 就绪池、两阶段审查（spec → code）。

任何平台的 agent（Claude Code / Qoder / Codex / …）只通过 `orchd` CLI 与编排系统交互，人作为最终调度器确认候选任务。

**通用前置依赖环境**（所有接入入口都需要）：**git**（克隆 orchd-core 到宿主项目、agent 在项目内建任务分支）、**Python >= 3.10**（运行安装器与引擎；`jsonschema` 依赖由安装器自动装入 `.orchd/`）、网络可访问 `github.com`。

---

## 接入

按你想怎么把 agent 接进来，选一种入口。

### 入口 A · 给 agent 一句话（推荐，agent 自动接入）

把下面这句直接发给任意 agent（Claude Code / Qoder / Codex / …），它会自动完成克隆、安装与引导：

```text
用这些命令把 orchd 接入当前项目并引导：
git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent
装好后读 .orchd/SKILL.md 进入工作流。
```

agent 会自动完成：`git clone` → `python orchd-core/install.py . --agent` → 读 `.orchd/SKILL.md` 进入 BOOTSTRAP → `bootstrap` → `validate` → `init` → 项目就绪。全程无感：宿主项目根零额外文件、无需安装 `orchd`、无需配置 PATH。

> **给项目配长期入口**：把这段放在宿主项目 `README.md` 顶部，团队任何 agent 进场都会自动检测并安装：
>
> ```markdown
> > 🤖 AI agents: 用 orchd 管理本项目。如果 .orchd/ 不存在，请自动安装：
> > git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent
> > 然后读 .orchd/SKILL.md 按 guidance 导航。
> ```

### 入口 B · 人工配置：手动下载 skill 包

orchd 引导 skill（`skills/orchd/`，SKILL.md + references/）暂未上架任何 skill 市场，需手动获取：

1. `git clone https://github.com/7bder/orchd-core.git`（或在此仓库取 `skills/orchd/`）
2. 把 `skills/orchd/` 整目录放入 agent 的 skill 目录；或直接把 `SKILL.md` 内容作为说明交给 agent
3. agent 据 skill 自主接入：检测到项目无 `.orchd/` → 自动 `git clone` + `python orchd-core/install.py . --agent` → 读 `.orchd/SKILL.md` 进入工作流

### 无 agent 时手动安装

前置依赖环境见上方「通用前置依赖环境」。

```bash
git clone https://github.com/7bder/orchd-core.git
python orchd-core/install.py ./你的项目
cd 你的项目
python .orchd/__main__.py --version
```

安装后 `python .orchd/__main__.py <子命令> ...` 即完整命令入口（无需安装、不依赖 PATH）。

---

## 核心概念

- **六状态机**：pending → claimed → done → in_review → completed（+ cancelled）。claimed 认领执行中、done 提交完成（verify 通过）、in_review 两阶段审查、completed 先 merge 成功才写入。
- **事件溯源**：所有状态变化 append-only 写入 `_ledger.jsonl`，可重放、可撤回（`retract`）。
- **文件锁**：`.lock` 排他锁防并发写损坏；`.session.lock` 防多个 agent 同时写工作区（E019）。
- **DAG 就绪池**：仅当全部 `depends_on` 完成才进入候选池；支持能力（`requires`）过滤与文件冲突检测（E010）。
- **两阶段审查**：纯文档任务单阶段 code 终审；涉及代码/约定的任务走 spec → code 双阶段。
- **越界保护**：L3 pre-commit hook 拦截提交 `files_to_edit` 之外的文件（E020）。
- **零根入口**：安装器在宿主根维护 `AGENTS.md`（指向 `.orchd/SKILL.md`），不扫隐藏目录的 agent 也能发现引擎入口。

---

## 快速开始

1. 按上方「接入」让 agent 进入 BOOTSTRAP。
2. 在项目根放 `requirements.md`（需求文档，人写 / AI 对话生成 / 现有 PRD）。
3. agent 读 `.orchd/SKILL.md` 执行：

```bash
python .orchd/__main__.py bootstrap            # 输出 schema + architect prompt + 分解指南
# 按 schema 与拆解指南写 .orchd/_master.json
python .orchd/__main__.py validate .orchd/_master.json   # 结构 + 引用 + 质量校验
python .orchd/__main__.py init                 # 生成 mod-*/spec.json 快照 + 空 ledger + checkpoint
```

4. 写项目共享上下文 `.orchd/shared/architecture.md` 与 `.orchd/shared/conventions.md`。
5. agent 进入工作流：`request → claim → 实现 → done → review`。每一步的下一步由响应 `guidance` 字段自动提示。

---

## CLI 命令一览

| 命令 | 作用 |
|------|------|
| `bootstrap` | 输出分解套件（schema + architect prompt + guide） |
| `validate <master>` | 校验任务清单（结构/引用/质量/source） |
| `init` | 初始化快照 + 空 ledger + checkpoint |
| `amend` | 增量更新快照（按状态约束矩阵过滤） |
| `request` | 获取下一个候选任务（有 in_review 任务时引擎优先返回审查候选） |
| `pool` | 列出就绪池（`--all` 含非就绪） |
| `claim` | 认领任务（自动建 `task/{id}` 分支，`--confirm` 两段式确认） |
| `done` | 报告完成（跑 verify_command → 自动进入审查） |
| `review` | 提交审查结论（APPROVED / CHANGES_REQUESTED） |
| `retract` | 撤回事件（级联） |
| `force-status` | 强制设置状态（受"允许从"矩阵约束） |
| `status` | 全局状态快照 / 单任务详情 |
| `watchdog` | 僵死任务巡检 |
| `ideas-archive` | 自动归档已完结的 IDEAS 条目 |
| `doctor` | git 仓库完整性只读检测 |

所有命令统一 JSON（UTF-8）输出，便于脚本/管道消费，统一用 `python .orchd/__main__.py <命令>` 调用。

---

## 目录结构（安装后宿主单一 `.orchd/`）

```
你的项目/
└── .orchd/
    ├── orchd/                  # vendored 只读引擎（cli / spec / split / ledger / pool / onboard / report / errors / gitops / ideas / doctor）
    ├── schema/_master.schema.json
    ├── templates/              # architect / implementer / spec-reviewer / code-reviewer prompt
    ├── docs/decomposition-guide.md
    ├── rules/                  # agent 规则目录（session / intake / verify / git / review 等）
    ├── SKILL.md                # agent 协议适配层（三模式 + 规则目录索引）
    ├── __main__.py             # 零根文件启动入口
    ├── pyproject.toml / MANIFEST.in / LICENSE / .gitignore
    ├── shared/                 # 工作区骨架（宿主项目共享上下文）
    ├── proposals/              # 工作区骨架（提案目录）
    └── README.md               # 本说明
```

宿主项目根零额外文件——像 `.claude/` / `.cursor/` 一样无感。

---

## 许可

MIT License。详见 [LICENSE](LICENSE)。