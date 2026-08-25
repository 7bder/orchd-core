# orchd-core

> orchd 引擎的源码发行版 · 跨 AI agent 平台的任务编排 CLI 核心套件

**orchd-core 让多个 AI agent 在一个项目里可靠协作。** 宿主项目通过**安装器**把引擎与资源组装进自身 `.orchd/`，形成单一自包含工作空间，宿主项目根零额外文件。

orchd 不推理需求、不内置 LLM 调用。它只为一个项目里的多个 AI agent 提供可靠的协作基础设施：事件溯源账本、文件锁并发控制、DAG 就绪池、两阶段审查（spec → code）。

任何平台的 agent（Claude Code / Qoder / Codex / …）只通过 `orchd` CLI 与编排系统交互，人作为最终调度器确认候选任务。

**通用前置依赖**：git（克隆 orchd-core、agent 在项目内建任务分支）、Python >= 3.10（运行安装器与引擎；`jsonschema` 依赖由安装器自动装入 `.orchd/`）、网络可访问 GitHub 或 Gitee。

## 快速开始

### 方式一：给 agent 一句话（推荐）

把「[接入方式](#接入方式)」里的指令直接发给任意 agent，它会自动完成 `git clone` → 安装 → 引导 → 项目就绪，全程无感。

### 方式二：手动安装

```bash
git clone <GitHub 或 Gitee 仓库>        # 见「接入方式」
python <仓库>/install.py . --agent
python .orchd/__main__.py --version
```

安装后 `python .orchd/__main__.py <子命令> ...` 即完整命令入口（无需安装、不依赖 PATH）。

## 接入方式

两种仓库接入，二选一。把对应代码块内容直接发给 agent，agent 收到后即可自动安装引导。

### GitHub 接入（科学上网）

```text
用这些命令把 orchd 接入当前项目并引导：
git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent
装好后读 .orchd/SKILL.md 进入工作流。
```

### Gitee 接入（国内镜像）

```text
用这些命令把 orchd 接入当前项目并引导：
git clone https://gitee.com/QQ7bder/orchestra.git && python orchestra/install.py . --agent
装好后读 .orchd/SKILL.md 进入工作流。
```

> **给项目配长期入口**：把下面这段放在宿主项目 `README.md` 顶部，团队任何 agent 进场都会自动检测并安装：
>
> ```markdown
> > 🤖 AI agents: 用 orchd 管理本项目。如果 .orchd/ 不存在，请自动安装：
> > git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent
> > 然后读 .orchd/SKILL.md 按 guidance 导航。
> ```

安装器每次运行都会在宿主根确保 `AGENTS.md`（无则新建、有则追加，幂等），内容指向 `.orchd/SKILL.md`——不扫隐藏目录、无 orchd skill 的 agent 在宿主根即可发现引擎入口。

## 核心概念

- **六状态机**：pending → claimed → done → in_review → completed（+ cancelled）。claimed 认领执行中、done 提交完成（verify 通过）、in_review 两阶段审查、completed 先 merge 成功才写入。
- **事件溯源**：所有状态变化 append-only 写入 `_ledger.jsonl`，可重放、可撤回（`retract`）。
- **文件锁**：`.lock` 排他锁防并发写损坏；`.session.lock` 防多个 agent 同时写工作区。
- **DAG 就绪池**：仅当全部 `depends_on` 完成才进入候选池；支持能力（`requires`）过滤与文件冲突检测。
- **两阶段审查**：纯文档任务单阶段 code 终审；涉及代码/约定的任务走 spec → code 双阶段。
- **越界保护**：L3 pre-commit hook 拦截提交 `files_to_edit` 之外的文件。
- **零根入口**：安装器在宿主根维护 `AGENTS.md`（指向 `.orchd/SKILL.md`），不扫隐藏目录的 agent 也能发现引擎入口。

## CLI 命令一览

所有命令统一 JSON（UTF-8）输出，统一用 `python .orchd/__main__.py <命令>` 调用。

| 命令 | 作用 |
|------|------|
| `bootstrap` | 输出分解套件（schema + architect prompt + 拆解指南） |
| `validate <master>` | 校验任务清单（结构/引用/质量/source） |
| `init` | 初始化快照 + 空 ledger + checkpoint |
| `request` | 获取下一个候选任务（有 in_review 任务时优先返回审查候选） |
| `claim` | 认领任务（自动建 `task/{id}` 分支） |
| `done` | 报告完成（跑 verify_command → 自动进入审查） |
| `review` | 提交审查结论（APPROVED / CHANGES_REQUESTED） |
| `status` | 全局状态快照 / 单任务详情 |
| `watchdog` | 僵死任务巡检 |
| `doctor` | git 仓库完整性只读检测 |

人为控制面：`amend` / `force-status` / `retract`。

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

## 许可

MIT License。详见 [LICENSE](LICENSE)。
