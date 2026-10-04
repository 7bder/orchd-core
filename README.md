# orchd-core

> orchd 引擎的源码发行版・跨 AI agent 平台的任务编排 CLI 核心套件

**orchd-core 让多个 AI agent 在一个项目里可靠协作。** 宿主项目通过**安装器**把引擎与资源组装进自身 `.orchd/`，形成单一自包含工作空间，宿主项目根零额外文件。

零安装・模型无关・单一 `.orchd/` 自包含：orchd 不推理需求、不内置 LLM 调用，只提供可靠的协作基础设施 —— 事件溯源账本、文件锁并发控制、DAG 就绪池、两阶段审查（spec → code）。任何平台的 agent（Claude Code / Qoder / Codex / …）只通过 `orchd` CLI 与编排系统交互，人作为最终调度器确认候选任务。

## 它解决什么问题



| 场景             | 痛点                        | orchd 的做法                |
| -------------- | ------------------------- | ------------------------ |
| **换 agent 接续** | agent A 做一半换 agent B，进度断档 | 进度在账本里，新 agent 进场读状态即续   |
| **多 agent 并行** | 同时开工互相踩，文件冲突、状态错乱         | 文件锁 + DAG 就绪池，并发不冲突      |
| **质量闭环**       | 谁改的、改了什么、合没合入，说不清         | 两阶段审查 + 引擎自动 merge，全程可审计 |

## 快速开始

### 方式一：给 agent 一句话（推荐）

把「[接入方式](#接入方式)」里的指令直接发给任意 agent，它会自动完成 `git clone` → 安装 → 引导 → 项目就绪，全程无感。

### 方式二：手动安装



```
git clone <GitHub 或 Gitee 仓库>        # 见「接入方式」
python <仓库>/install.py . --agent --cleanup
python .orchd/__main__.py --version
```

安装后 `python .orchd/__main__.py <子命令> ...` 即完整命令入口（无需安装、不依赖 PATH；`--cleanup` 安装成功后自动删除克隆源，宿主根零残留）。

## 接入方式

两种仓库接入，二选一。把对应代码块内容直接发给 agent，agent 收到后即可自动安装引导。

### GitHub 接入（科学上网）



```
用这些命令把 orchd 接入当前项目并引导：
git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent --cleanup
装好后读 .orchd/SKILL.md 进入工作流。
```

### Gitee 接入（国内镜像）



```
用这些命令把 orchd 接入当前项目并引导：
git clone https://gitee.com/QQ7bder/orchd-core.git && python orchd-core/install.py . --agent --cleanup
装好后读 .orchd/SKILL.md 进入工作流。
```

> **给项目配长期入口**
>
> ：把下面这段放在宿主项目 
>
> `README.md`
>
>  顶部，团队任何 agent 进场都会自动检测并安装：
>

```
> 🤖 AI agents: 用 orchd 管理本项目。如果 .orchd/ 不存在，请自动安装：
> git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent --cleanup
> 然后读 .orchd/SKILL.md 按 guidance 导航。
```

安装器每次运行都会在宿主根确保 `AGENTS.md`（无则新建、有则追加，幂等），内容指向 `.orchd/SKILL.md`—— 不扫隐藏目录、无 orchd skill 的 agent 在宿主根即可发现引擎入口。

## 快速教程：5 分钟跑通第一个任务

两种方式任选其一：**对话版**把一段话发给 agent 自动跑完（推荐）；**手动版**自己敲命令，适合理解原理。

### 方式 A：Agent 对话版（推荐）

把安装、初始化、第一个任务全流程交给 agent 自己完成。

① **放需求**：准备一份需求文档，或用一句话口头描述；

② **发指令**：把下面这段发给任意 agent（Claude Code / Qoder / Codex / …）：



```
把 orchd 接入当前项目并引导：
git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent --cleanup
装好后读 .orchd/SKILL.md 进入工作流。初始化项目：bootstrap 输出分解套件，
按 architect 模板把需求拆成 .orchd/_master.json，validate 通过后 init。
然后 request 领第一个任务，claim 实现，done 报告完成。
需求文档在：<path/to/requirements.md>
```

③ **只做两件事**：



* 确认候选任务：agent 领任务前先展示预览（两段式 `claim --confirm`），你点头它才开工；

* 裁决审查结论：实现完进入审查，由你（或你指定的审查者）提交结论，APPROVED 后引擎自动 merge 入 main。

④ **换 agent 接续**：任何时刻把同一段话发给新 agent，它读 SKILL + `status` 找到断点继续，进度不丢。

### 方式 B：手动版（自己动手）

从安装到第一个任务合入，整条路径可复制。核心口诀：**每一步看命令响应的&#x20;**`guidance`**&#x20;提示**—— 引擎会告诉你下一步做什么，不用记命令。

#### 第 0 步：安装

按「[快速开始](#快速开始)」装好，验证：



```
python .orchd/__main__.py --version
```

#### 第 1 步：初始化项目（BOOTSTRAP）

人放一份需求文档；agent 负责拆解（`bootstrap` 会输出 schema + architect 模板 + 拆解指南）：



```
python .orchd/__main__.py bootstrap                      # 输出分解套件
# 人：放需求文档；agent：按 architect 模板拆成 .orchd/_master.json
python .orchd/__main__.py validate .orchd/_master.json  # 校验任务清单（通过才继续）
python .orchd/__main__.py init                          # 初始化快照 + 账本 → 项目就绪
```

#### 第 2 步：跑第一个任务



```
python .orchd/__main__.py request                       # 获取候选任务（返回 task id）
python .orchd/__main__.py claim --task <id> --confirm   # 认领：自动建 task/<id> 分支
# 在任务分支实现 → 提交
python .orchd/__main__.py done                          # 报告完成：自动跑 verify → 进入审查
```

> 注意：
>
> `claim`
>
>  是两段式 —— 先不带 
>
> `--confirm`
>
>  看预览，确认无误再加 
>
> `--confirm`
>
>  真正执行。

#### 第 3 步：审查与合入



```
python .orchd/__main__.py review --task <id> ...        # 提交审查结论（APPROVED / CHANGES_REQUESTED）
# 代码/约定任务走 spec → code 两阶段；纯文档任务单阶段
# code APPROVED → 引擎自动 merge 入 main → completed
```

#### 第 4 步：换人接续

任何时刻换一个 agent（甚至换平台、换 LLM）接着做：



```
# 新 agent 进场：
# ① 读 .orchd/SKILL.md
# ② python .orchd/__main__.py status    # 看全局进度，找到断点
# ③ 从断点继续（claim / done / review 由 guidance 导航）
```

#### 卡住时



* 命令报错 → `python .orchd/__main__.py doctor`（git 仓库完整性只读检测）

* 项目没有 git 仓库？→ 无 git 模式：有目录即可（v1.5.0 起）

## 核心概念



* **六状态机**：pending → claimed → done → in\_review → completed（+ cancelled）。claimed 认领执行中、done 提交完成（verify 通过）、in\_review 两阶段审查、completed 先 merge 成功才写入。

* **事件溯源**：所有状态变化 append-only 写入 `_ledger.jsonl`，可重放、可撤回（`retract`）。

* **文件锁**：`.lock` 排他锁防并发写损坏；`.session.lock` 防多个 agent 同时写工作区。

* **DAG 就绪池**：仅当全部 `depends_on` 完成才进入候选池；支持能力（`requires`）过滤与文件冲突检测。

* **两阶段审查**：纯文档任务单阶段 code 终审；涉及代码 / 约定的任务走 spec → code 双阶段。

* **guidance 导航**：每步命令响应自动携带下一步提示（read → template → command），agent 跟着走即可。

* **会话指纹身份**：会话级 12 位 hex 指纹（`ORCHD_SESSION_ID` 派生），一会话一身份，自审分级管理。

* **越界保护**：L3 pre-commit hook 拦截提交 `files_to_edit` 之外的文件。

* **零根入口**：安装器在宿主根维护 `AGENTS.md`（指向 `.orchd/SKILL.md`），不扫隐藏目录的 agent 也能发现引擎入口。

## CLI 命令一览

所有命令统一 JSON（UTF-8）输出，统一用 `python .orchd/__main__.py <命令>` 调用。按用途分组（34 个命令全量）：



* **接入与初始化**：`bootstrap` 输出分解套件・`validate` 校验任务清单・`init` 初始化快照 + 账本・`session` 会话身份管理

* **任务闭环**：`request` 获取候选（有审查先给审查）・`claim` 认领（自动建分支，两段式 `--confirm`）· `done` 报告完成（跑 verify 进审查）・`pool` 就绪池・`restore` 受管工作树还原

* **审查与门禁**：`review` 提交审查结论・`merge-ack` 合并落地确认・`check` 静态门禁（ruff+mypy）・`full-regression` 全量回归・`milestone-check` 里程碑判据门禁・`line-sync` 跨线回移

* **诊断与维护**：`status` 全局状态・`show` 单任务卡面・`watchdog` 僵死巡检・`doctor` 仓库完整性检测・`sync` 账本跨设备同步・`ledger-compact` 账本归档压实・`context-digest` 读取指纹・`git` git 写操作代理

* **想法与摄入**：`idea` 想法摄入・`ideas` 台账盘点・`ideas-archive` 归档・`intake` 提交摄入产物・`roadmap-land` 版本规划落地・`layout-migrate` 布局迁移・`migrate` schema 迁移

* **经验回灌**：`lesson` 自愈经验打点与审核

* **人为控制面**：`amend` 声明补登（含 `--revise-terminal` 终态规格修订）・`force-status` 状态裁决・`retract` 撤回认领

## 前置依赖与系统要求



* **git**：克隆 orchd-core、agent 在项目内建任务分支（v1.5.0 起支持无 git 模式：有目录即可）

* **Python >= 3.11**：运行安装器与引擎；`jsonschema` 依赖由安装器自动装入 `.orchd/`

* **网络**：可访问 GitHub 或 Gitee

## 安装后宿主长什么样



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

宿主项目根零额外文件 —— 像 `.claude/` / `.cursor/` 一样无感。

## 文档导航

安装后，宿主项目的 `docs/` 提供完整文档（随 orchd-core 发布）：`user-manual.md`（使用手册）・`system-design.md`（架构总览）・`runtime-spec.md`（运行时规格）・`implementation-design.md`（实现层设计）・`decomposition-guide.md`（拆解方法论）。

## 许可

MIT License。详见 [LICENSE](LICENSE)。