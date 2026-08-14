# orchd-core

跨 AI agent 平台的任务编排 CLI 核心套件。本仓库是 **orchd 引擎的最小可移植核心**，可复制到任意新项目中，为该项目托管「多 agent 协作的任务领取 / 提交 / 审查」基础设施。

orchd 不做需求推理、不内置 LLM 调用。它只为一个项目里的多个 AI agent 提供**可靠**的协作基础设施：

- **事件溯源账本**（append-only JSONL + 原子 checkpoint + 增量 replay）
- **文件锁并发控制**（跨平台排他锁 + session 工作区锁）
- **DAG 就绪池**（依赖感知、能力过滤、文件冲突串行化）
- **两阶段审查**（spec → code，merge 前置化）

任何平台的 agent（Claude Code / Qoder / Codex / …）只通过 `orchd` CLI 与编排系统交互，人作为最终调度器确认候选任务。

---

## 核心概念

### 六状态机

任务生命周期由 6 种状态驱动：

```
pending → claimed → done → in_review → completed
                              ↘ CHANGES_REQUESTED → pending（打回返工）
cancelled（强制取消）
```

- `claimed`：已被某 agent 认领，执行中。
- `done`：agent 提交完成，附带 `verify_command` 自动验证。
- `in_review`：两阶段审查（spec → code）。
- `completed`：code 审查通过（先 merge 成功才写完成事件）。
- 事件溯源：所有状态变化都是 append-only 事件，可重放、可撤回（`retract`）。

### 关键机制

| 机制 | 说明 |
|------|------|
| 事件溯源 | 每次变更追加一条事件到 `_ledger.jsonl`，状态由 checkpoint + 增量 replay 重建，可审计 |
| 文件锁 | `.lock` 排他锁防并发写损坏；`.session.lock` 防多个 agent 同时写工作区（E019） |
| DAG 就绪池 | 仅当全部 `depends_on` 完成才进入候选池；支持能力（`requires`）过滤与文件冲突检测（E010） |
| 两阶段审查 | 纯文档任务单阶段 code 终审；涉及代码/约定的任务走 spec→code 双阶段 |
| 越界保护 | L3 pre-commit hook 拦截提交 `files_to_edit` 之外的文件（E020） |
| 质量校验 | E022 缺 verify_command、E023 模糊验收标准、E024 缺 basetemp、E025 source 溯源、E026/E027/E028 等注册点硬校验 |

---

## 安装

要求 Python >= 3.10，依赖仅 `jsonschema`。

```bash
# 开发安装（套件根目录）
pip install -e .
orchd --version
```

---

## 快速开始：为你的新项目启动 orchd 托管

1. 把本套件复制到你的项目根目录（或作为子模块引入），确保目录结构：

```
你的项目/
├── orchd/                 # 引擎（本套件）
├── schema/_master.schema.json
├── templates/             # architect / implementer / reviewer prompt
├── pyproject.toml
├── SKILL.md               # agent 协议适配层
└── docs/decomposition-guide.md
```

2. 安装并让 agent 生成任务分解：

```bash
pip install -e .
orchd bootstrap            # 输出 schema + architect prompt + 分解指南
```

3. 编写（或由 LLM 生成）任务清单 `.orchd/_master.json`，然后校验并初始化：

```bash
orchd validate .orchd/_master.json   # 结构 + 引用 + 质量校验
orchd init                           # 生成 mod-*/spec.json 快照 + 空 ledger + checkpoint
```

4. 写项目共享上下文 `.orchd/shared/architecture.md` 与 `.orchd/shared/conventions.md`。

5. agent 进入工作流：`orchd request → claim → 实现 → done → review`（见下方命令）。

---

## CLI 命令一览

| 命令 | 作用 |
|------|------|
| `orchd bootstrap` | 输出分解套件（schema + architect prompt + guide） |
| `orchd validate <master>` | 校验任务清单（结构/引用/质量/source） |
| `orchd init` | 初始化快照 + 空 ledger + checkpoint |
| `orchd amend` | 增量更新快照（按状态约束矩阵过滤） |
| `orchd request` | 获取下一个候选任务（`--role reviewer` 领审查） |
| `orchd pool` | 列出就绪池（`--all` 含非就绪） |
| `orchd claim` | 认领任务（自动建 `task/{id}` 分支） |
| `orchd done` | 报告完成（跑 verify_command → 自动进入审查） |
| `orchd review` | 提交审查结论（APPROVED / CHANGES_REQUESTED） |
| `orchd retract` | 撤回事件（级联） |
| `orchd force-status` | 强制设置状态（受"允许从"矩阵约束） |
| `orchd status` | 全局状态快照 / 单任务详情 |
| `orchd watchdog` | 僵死任务巡检 |
| `orchd doctor` | git 仓库完整性只读检测 |

所有命令统一输出 JSON（UTF-8），便于脚本/管道消费。

---

## 目录结构

```
orchd/                     # 引擎包（13 个模块）
  cli.py                   # CLI 路由 + 统一 JSON 输出
  ledger.py                # 事件溯源存储 + 文件锁
  onboard.py               # 状态机生命周期（claim/done/review/retract/force）
  pool.py                  # 就绪池 / DAG / 冲突检测
  split.py                 # init/amend 快照
  spec.py                  # master 校验（结构/引用/质量/source）
  gitops.py                # git 分支/hook/session 锁
  report.py                # status/watchdog
  ideas.py                 # IDEAS.md 自动归档
  doctor.py                # git 完整性检测
  errors.py                # 错误码 E001–E028
schema/_master.schema.json # 任务清单 JSON Schema
templates/                 # architect / implementer / spec-reviewer / code-reviewer prompt
SKILL.md                   # agent 协议适配层（双模式）
docs/decomposition-guide.md# 任务拆解方法论
```

---

## 许可

MIT License。详见 [LICENSE](LICENSE)。
