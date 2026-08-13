<!--
  拆解指南 — 教 BOOTSTRAP agent 将需求文档分解为合法的 _master.json。
  读者：执行 BOOTSTRAP 的外部 AI agent。变更频率：低。
  关联文档：system-design.md §3（入口链路）、schema/_master.schema.json（结构约束）、
           implementation-design.md §8（完整示例）。
  消费方式：orchd bootstrap 内联输出，agent 无需额外读取本文件。
-->

# 项目拆解指南

本指南规定将需求文档分解为 `_master.json` 的方法论和质量标准。遵循本指南产出的 JSON 必须同时通过 `orchd validate` 的结构校验和本文规定的语义约束。

---

## 1. 总则

### 1.1 分解目标

将一份需求文档转化为一组**可由单个 agent 在单个 session 内独立完成**的任务集合。每个任务必须具备：

- 明确的输入（files_to_read）和输出（files_to_edit）
- 可客观判定的验收标准（acceptance_criteria）
- 可执行的完成定义（Definition of Done）

### 1.2 核心约束

| 约束 | 来源 | 含义 |
|------|------|------|
| 单任务 session | system-design §1 | agent 做完一个任务即退出，不做跨任务工作 |
| 文件冲突检测 | runtime-spec §3.4 | 同时 claimed 的任务不可编辑同一文件 |
| 串行审查 | system-design §4.8 | spec review → code review，验收标准是 spec review 的唯一判定依据 |
| 依赖放行 | runtime-spec §3.1 | 下游任务需等上游 completed 或 cancelled 才进入就绪池 |

### 1.3 分解流程（推荐顺序）

```
1. 通读需求文档，识别功能边界 → 划分模块
2. 在每个模块内识别独立工作单元 → 定义任务
3. 分析任务间的数据/接口依赖 → 建立 depends_on
4. 为每个任务编写验收标准 → 确保可量化
5. 标注文件映射 → files_to_read / files_to_edit
6. 自检 → 对照 §8 清单逐条验证
```

---

## 2. 模块划分

### 2.1 划分原则

模块是任务的逻辑分组容器，不是执行单元。划分依据：

- **职责内聚**：同一模块内的任务围绕同一子系统或同一技术关注点
- **接口隔离**：模块间通过明确定义的接口交互，内部实现对外不可见
- **独立可测**：模块的核心功能可以独立于其他模块验证

### 2.2 粒度标准

| 信号 | 判断 |
|------|------|
| 模块内任务数 < 2 | 考虑合并到相邻模块（除非职责确实独立） |
| 模块内任务数 > 8 | 考虑拆分为子模块 |
| 两个模块的任务频繁编辑同一文件 | 模块边界可能错误，考虑合并或重新划分 |

### 2.3 必填字段

```json
{
  "id": "mod-{kebab-case-name}",
  "name": "人类可读名称",
  "role": "一句话说明该模块在整个系统中的定位和职责边界"
}
```

`role` 的写法要求：说明"做什么"和"不做什么"。例如："数据录制、崩溃恢复、许可证管理。不含 UI 渲染和网络通信。"

---

## 3. 任务粒度

### 3.1 单任务 session 约束

一个任务 = 一个 agent 的一次完整工作 session。这意味着：

- 任务范围必须小到 agent 无需"记住"前一个 session 的上下文
- 任务的所有必要信息必须能通过 files_to_read + acceptance_criteria + shared/ 文件完整传达
- 任务完成后产出可独立审查的增量变更

### 3.2 粒度判定规则

**太粗的信号**（必须拆分）：

- 任务需要修改 3 个以上不相关的文件组
- 任务包含"并且"/"同时"/"以及"连接的多个独立功能
- 任务预估工时 > 6 小时
- 任务无法用一句话描述其交付物

**太细的信号**（应当合并）：

- 任务仅修改单个函数的单个参数
- 任务预估工时 < 0.5 小时
- 任务与另一任务必须修改同一文件的同一区域，且逻辑不可分离
- 任务单独完成后无法通过任何有意义的验收测试

**合适粒度的特征**：

- 可用一句话描述交付物："实现 X 功能"/"修复 Y 问题"/"重构 Z 模块"
- 修改文件数 1-4 个
- 预估工时 1-4 小时
- 验收标准 2-5 条，每条可独立判定

### 3.3 拆分策略

当任务过大时，按以下优先级选择拆分维度：

1. **按层次拆**：接口定义 → 核心实现 → 集成适配（上游先完成，下游依赖上游）
2. **按功能拆**：每个独立功能一个任务（并行度高）
3. **按阶段拆**：骨架搭建 → 逻辑填充 → 边界处理（串行依赖）

拆分后必须检查：每个子任务是否仍满足 §3.2 的合适粒度特征。

---

## 4. 依赖设计

### 4.1 合法依赖

`depends_on` 表示**硬序约束**：下游任务在上游 completed/cancelled 前不进入就绪池。仅在以下情况建立依赖：

- 下游任务的实现**必须读取**上游任务的产出文件
- 下游任务的接口设计**必须基于**上游任务确定的数据结构
- 下游任务的验收**必须在上游功能可用**的前提下执行

### 4.2 反模式（禁止）

| 反模式 | 问题 | 修正 |
|--------|------|------|
| 循环依赖 | DAG 校验失败（E004） | 提取公共部分为独立上游任务 |
| 链式过长（A→B→C→D→E） | 串行瓶颈，并行度为零 | 识别可并行的分支，仅保留真正的硬序 |
| 依赖仅为"逻辑上相关" | 不必要地降低并行度 | 删除依赖，两者可并行执行 |
| 依赖仅为"想先做 A" | 偏好不是约束 | 用 importance 表达优先级，不用 depends_on |
| 依赖已 cancelled 的任务 | 语义混乱 | 若上游取消后下游仍可做，删除依赖；若下游也不做了，一并取消 |

### 4.3 依赖与 importance 的分工

| 机制 | 语义 | 效果 |
|------|------|------|
| `depends_on` | 硬序：B 在 A 完成前**不可能**执行 | 阻塞就绪池 |
| `importance` | 软序：A 比 B **更应该先做** | 影响排序，不阻塞 |

规则：如果去掉一条 depends_on 后两个任务仍然可以独立实现和审查，那它应该是 importance 差异，而非依赖。

### 4.4 依赖链长度建议

- 最长链 ≤ 4 层（超过说明任务粒度过细或模块耦合过深）
- 入度（被依赖数）> 5 的任务考虑拆分（它是瓶颈）
- 出度（依赖数）> 3 的任务考虑是否遗漏了中间层

---

## 5. 验收标准（核心章节）

### 5.1 定位

acceptance_criteria 是 **spec review 的唯一判定依据**。审查者逐条对照，每条只有"满足"或"不满足"两个状态。因此：

- 每条标准必须是**客观可判定**的——不依赖审查者的主观判断
- 每条标准必须是**原子**的——只验证一件事
- 标准集合必须是**充分**的——覆盖任务的全部交付承诺

### 5.2 可量化规则

每条验收标准必须属于以下类型之一：

| 类型 | 模板 | 示例 |
|------|------|------|
| **功能断言** | "当 {输入/操作} 时，{可观察结果}" | "当调用 Enqueue 超过容量时，最旧元素被自动丢弃" |
| **性能阈值** | "{操作} 在 {条件} 下耗时/资源 ≤ {数值}" | "导出 10000 行数据耗时 < 5 秒" |
| **结构约束** | "{产物} 满足 {可检查的结构规则}" | "CSV 文件编码为 UTF-8，列名与 docs/data-format.md 一致" |
| **边界行为** | "当 {边界条件} 时，{明确行为}" | "空文件输入时返回错误码 E_EMPTY，不抛异常" |
| **不变量** | "{系统属性} 在任何操作序列后保持为真" | "StopRecording 返回的帧数与实际录制帧数一致" |

### 5.3 禁止的写法

| 禁止 | 原因 | 改为 |
|------|------|------|
| "代码质量好" | 不可客观判定 | 删除（代码质量由 code review 对照 conventions.md 判定，不属于 spec review） |
| "性能优化" | 无阈值 | "单次查询响应时间 < 200ms（1000 条记录规模）" |
| "正确处理错误" | 未指定哪些错误、什么行为 | "文件不存在时抛出 FileNotFoundException，消息含路径" |
| "与现有系统兼容" | 未指定兼容判据 | "API 响应格式与 v1 文档 §3 定义的 JSON schema 一致" |
| "用户体验良好" | 主观 | 删除，或改为可测断言："加载页面在 3 秒内显示首屏内容" |
| "测试通过" | 循环定义（测试本身可能错） | 写明测试验证的具体行为 |

### 5.4 数量与覆盖

- 每个任务 2-5 条验收标准
- 覆盖维度：正常路径（至少 1 条）+ 边界/异常（至少 1 条）+ 性能/约束（若任务涉及）
- 每条验收标准必须有对应的验证手段：verify_command 中的测试用例（首选）或 criteria 本身的可检查描述（豁免场景）
- acceptance_criteria 不应重复 verify_command 已覆盖的低层内容（如"代码编译通过"），而应描述行为正确性

### 5.5 测试交付（核心约束）

**原则：每个任务必须交付可执行的验证手段。** 验收不能仅依赖审查 agent 的主观判断——交付物本身必须携带客观证据。

#### 判定机制分层

| 层级 | 机制 | 判定者 | 判定时机 | 作用 |
|------|------|--------|---------|------|
| 第一层 | `verify_command` | CLI 自动执行 | DONE 时（锁外） | 机器判底线：测试通过 = 功能可验证 |
| 第二层 | `acceptance_criteria` | 审查 agent（spec review） | DONE 后 | 判充分性：测试是否真正覆盖了所有标准 |
| 第三层 | `conventions.md` | 审查 agent（code review） | spec 通过后 | 判质量：代码风格、架构合规 |

#### verify_command 填写规则

**原则必填**：除非满足 §5.5.4 豁免条件，否则每个任务必须设置 verify_command。

verify_command 必须执行**覆盖该任务全部 acceptance_criteria 的测试**。具体要求：

1. **测试代码是交付物的一部分**：实现者不仅交付功能代码，还必须交付测试代码。测试文件列入 files_to_edit。
2. **每条 acceptance_criteria 至少有一个测试用例覆盖**：实现者在自检时逐条对照（见 §7.1）。
3. **测试必须是可自动执行的**：verify_command 是一条 shell 命令，exit 0 = 全部通过。不依赖人工交互、GUI 操作或外部服务。
4. **测试必须是有意义的**：验证行为正确性，而非仅验证"代码能跑"。空测试、永真断言（`assert True`）视为未交付。
5. **环境自包含**：verify_command 由 `orchd done` 以 `shell=True` 在 orchd 进程的环境中执行，**不继承实现者会话的虚拟环境**。因此命令不得依赖特定会话的 PATH/venv：
   - 依赖应安装到 verify 实际解析到的解释器，或在命令中显式激活项目内 venv（如 `.\venv\Scripts\activate && pytest ...`）；
   - 环境不确定时优先写绝对解释器路径（如 `C:\...\python.exe -m pytest tests -q`）；
   - 分解者在写下 verify_command 时应自问：换一个干净 shell 执行它还能通过吗？

#### verify_command 示例

| 语言/场景 | verify_command |
|-----------|---------------|
| C# / Unity | `dotnet test Tests/CoreTests.csproj --filter "Category=task-ringbuffer"` |
| Python | `python -m pytest tests/test_parser.py -v --tb=short` |
| Node.js | `npx jest --testPathPattern=exporter --ci` |
| 编译 + lint | `cargo build && cargo clippy -- -D warnings` |
| 多步组合 | `dotnet build && dotnet test --filter "RingBuffer"` |

#### 测试与 acceptance_criteria 的对应关系

分解任务时，应为每条 acceptance_criteria 预想对应的测试策略：

```
acceptance_criteria:                    对应测试策略:
"录制 10 分钟数据无丢帧"         →     集成测试：模拟 10 分钟数据流，断言帧数一致
"Enqueue/Dequeue 均为 O(1)"     →     性能测试：10 万次操作耗时 < 阈值
"空文件输入时返回 E_EMPTY"       →     单元测试：传入空文件，断言错误码
```

实现者交付时，测试代码中应能通过测试命名或注释追溯到对应的 acceptance_criteria。

#### spec reviewer 的审查对象变更

当任务携带测试交付时，spec review 的判定逻辑变为：

```
旧：审查者读代码 → 主观判断"代码是否满足标准 X"
新：审查者读测试 → 客观判断"测试是否真正覆盖了标准 X"
```

审查者检查：
- 每条 acceptance_criteria 是否有对应测试（覆盖性）
- 测试断言是否真正验证了标准描述的行为（有效性）
- 边界条件是否被测试覆盖（充分性）

打回理由变得具体："第 3 条标准'空文件返回 E_EMPTY'没有对应测试用例"，而非"我觉得边界处理不够"。

#### 5.5.4 豁免条件

以下类型的任务可不设 verify_command，但必须在 acceptance_criteria 中提供替代验证手段：

| 任务类型 | 豁免理由 | 替代验证 |
|---------|---------|---------|
| 纯文档（README、API 文档） | 无代码可测 | 结构约束类标准："文档包含 X/Y/Z 章节" |
| UI 布局 / 视觉调整 | 难以自动化断言 | 边界行为类标准 + 人工截图对比（在 criteria 中注明） |
| 配置变更（CI、部署脚本） | 执行环境不可复现 | 结构约束类标准："配置文件通过 yamllint / shellcheck" |
| 探索性原型 | 目标不确定 | 至少设置编译/lint 级 verify_command |

豁免不是默认选项。分解者必须主动判断是否满足豁免条件，而非"忘了写"。

### 5.6 写法示例

**好的验收标准**（来自 implementation-design §8 示例）：

```json
"acceptance_criteria": [
  "录制 10 分钟数据无丢帧",
  "Enqueue/Dequeue 均为 O(1)",
  "StopRecording 返回与录制帧数一致"
]
```

分析：第 1 条是功能断言（可观察）；第 2 条是结构约束（复杂度可验证）；第 3 条是不变量（数值一致）。三条互相独立，各自可判定。

**差的验收标准**：

```json
"acceptance_criteria": [
  "实现环形缓冲区",
  "代码质量高",
  "与其他模块兼容"
]
```

分析：第 1 条是任务描述的重复，不是验收标准；第 2 条不可客观判定；第 3 条无具体判据。

### 5.7 交付契约（deliverables）

**定位**：deliverables 定义任务的**对外形状**（API 签名 / 数据格式），acceptance_criteria 定义**行为正确性**。两者并列，spec review 同时校验。

| 维度 | acceptance_criteria | deliverables |
|------|--------------------|--------------| 
| 回答的问题 | "功能对不对？" | "接口/格式是不是约定的形状？" |
| 判定方式 | 审查者逐条对照行为 | 审查者逐项核对签名/字段一致性 |
| 适用场景 | 所有任务 | 仅对明确对外接口/格式的任务 |

**何时使用**：

- 任务产出的 API 会被其他模块/任务调用 → 用 `code_api` 锁定签名
- 任务产出的数据文件会被下游消费 → 用 `data_format` 锁定格式
- 任务是纯内部逻辑（无对外接口）→ 不使用 deliverables

**code_api 填写规则**：

```json
"deliverables": {
  "type": "code_api",
  "api": {
    "language": "csharp",
    "signatures": [
      {"name": "Enqueue", "params": [{"name": "item", "type": "T"}], "returns": "void"},
      {"name": "Dequeue", "params": [], "returns": "T"}
    ]
  }
}
```

- `language`：实现语言（与 requires 标签对应）
- `signatures[]`：列出所有公开 API 的签名。每个签名含 name、params（参数名+类型）、returns
- 只列对外公开的接口，内部辅助函数不列

**data_format 填写规则**：

```json
"deliverables": {
  "type": "data_format",
  "schema": {
    "file": "output/result.csv",
    "format": "csv",
    "fields": [
      {"name": "timestamp", "type": "datetime", "unit": "ms"},
      {"name": "x", "type": "float", "unit": "px"},
      {"name": "y", "type": "float", "unit": "px"}
    ]
  }
}
```

- `file`：产出文件的相对路径（或路径模式）
- `format`：文件格式（csv / json / parquet 等）
- `fields[]`：字段名 + 类型 + 可选单位

**与 verify_command 的配合**：deliverables 定义了"形状应该是什么"，verify_command 中的测试应包含契约一致性断言（如：调用 API 验证签名存在、读取输出文件验证列名匹配）。

---

## 6. 文件映射

### 6.1 files_to_edit

- 列出本任务**将修改或新建**的所有文件路径
- 路径相对于 workspace 根目录
- 此列表用于文件冲突检测：两个同时 claimed 的任务不可有交集
- 不要列出"可能顺便看看"的文件（那是 files_to_read 的职责）

**冲突预防规则**：如果两个任务逻辑上需要修改同一文件，要么建立 depends_on（串行），要么将共享修改提取为独立的上游任务。

> **2026-08-08 语义更新**：`amend` 不再拒绝冲突注册（仅返回 `conflict_warnings` warning）。冲突硬边界在 `request` 依赖感知强制过滤——与 pending/claimed **非依赖**任务冲突的候选会被硬排除（`excluded_conflicts`），依赖链（祖先/子孙）上共享文件的任务放行（`conflict_with`）。因此本规则的意图由引擎在领取阶段自动执行：未按 depends_on 串行化的共享文件任务对，第二个将无法被领取（直至冲突解除）。


### 6.2 files_to_read

```json
{"path": "相对路径", "priority": "must_read|reference", "hint": "一行说明"}
```

| priority | 含义 | agent 行为 |
|----------|------|-----------|
| `must_read` | 不读此文件无法完成任务 | 必须完整阅读 |
| `reference` | 提供上下文但非必需 | 按需查阅 |

**hint 写法**：说明"这个文件里什么内容与本任务相关"，而非重复文件名。例如："当前 List 实现，需替换的 RemoveAt(0) 在第 47 行"。

### 6.3 数量控制

- files_to_read：must_read ≤ 5 个（超过说明任务范围过大或上下文未充分共享）
- files_to_edit：≤ 4 个（超过考虑拆分任务）
- 共享上下文文件（architecture.md / conventions.md）由 CLI 自动附加，不要手动列入

---

## 7. 交付标准（Definition of Done）

一个任务被视为"完成"（可调用 `orchd done`）当且仅当：

1. **files_to_edit 中所有文件已修改/创建**，且变更内容与任务 brief 一致
2. **所有 acceptance_criteria 均已满足**——实现者应逐条自检
3. **测试代码已交付且 verify_command 执行通过**——测试文件在 files_to_edit 中，CLI 在 done 时自动执行 verify_command
4. **每条 acceptance_criteria 有对应测试用例**——测试命名或注释可追溯到具体标准
5. **变更可独立编译/运行**——不依赖其他未完成任务的产出
6. **changes_description 准确描述做了什么**——供审查者理解变更意图

豁免 verify_command 的任务（见 §5.5.4）：第 3、4 条替换为"acceptance_criteria 中每条标准有明确的可检查判据"。

### 7.1 实现者自检流程

```
□ 逐条对照 acceptance_criteria，确认每条已满足
□ 确认每条 acceptance_criteria 有对应测试用例（测试命名/注释可追溯）
□ 运行 verify_command，确认 exit 0（全部测试通过）
□ 检查 files_to_edit 列表，确认功能代码和测试代码均已列入
□ 确认变更不引入对其他未完成任务的隐式依赖
□ 准备 changes_description：一句话说清"改了什么、为什么这样改"
```

### 7.2 审查判定标准

| 审查阶段 | 判定依据 | 通过条件 |
|---------|---------|---------|
| Spec Review | acceptance_criteria + 测试覆盖性 | 每条标准有对应测试且测试有效 → APPROVED |
| Code Review | shared/conventions.md + architecture.md | 符合编码规范和架构约束 → APPROVED |

Spec reviewer 的核心问题不再是"代码对不对"，而是"测试有没有真正验证标准描述的行为"。具体检查：
- 覆盖性：每条 criteria 是否有对应测试
- 有效性：测试断言是否验证了正确的行为（而非永真/无意义断言）
- 充分性：边界条件和异常路径是否被测试覆盖

实现者无需关心 code review 标准（那是审查者的职责），但应确保代码风格与 conventions.md 一致以减少返工。

---

## 8. 质量自检清单

完成 `_master.json` 编写后、运行 `orchd validate` 前，逐条检查：

### 结构完整性

- [ ] 每个 task 的 module 指向已定义的 module id
- [ ] 每个 task 有至少 1 条 acceptance_criteria
- [ ] 每个 task 有至少 1 个 files_to_edit
- [ ] 每个 task 有至少 1 个 reviewers
- [ ] 所有 depends_on 引用的 task_id 存在
- [ ] 无循环依赖

### 粒度与可执行性

- [ ] 每个任务可用一句话描述交付物
- [ ] 每个任务预估工时在 0.5-6 小时范围内
- [ ] 每个任务修改文件数 ≤ 4
- [ ] 无两个独立任务编辑同一文件（除非有 depends_on 串行）

### 验收标准质量

- [ ] 每条标准属于 §5.2 五种类型之一
- [ ] 无主观判断词（"好"/"合理"/"适当"/"优雅"）
- [ ] 每条标准只验证一件事（无"并且"连接）
- [ ] 正常路径和边界条件均有覆盖
- [ ] 与 verify_command 无冗余重复

### 测试交付

- [ ] 每个任务设置了 verify_command（或满足 §5.5.4 豁免条件）
- [ ] 每条 acceptance_criteria 有对应测试用例
- [ ] 测试文件已列入 files_to_edit
- [ ] verify_command 可自动执行（无人工交互、无外部依赖）
- [ ] 测试验证行为正确性（非空测试、非永真断言）

### 交付契约

- [ ] 若定义了 deliverables，确认 type 为 code_api 或 data_format 且对应属性完整（见 §5.7）
- [ ] 产出 API 被其他任务调用的任务已设置 code_api 契约
- [ ] 产出数据文件被下游消费的任务已设置 data_format 契约

### 依赖合理性

- [ ] 每条 depends_on 满足 §4.1 的合法依赖条件
- [ ] 无"偏好型依赖"（应改用 importance）
- [ ] 最长依赖链 ≤ 4 层（例外：多任务修改同一文件时，文件冲突约束强制串行，此时链深可超出 4 层，但应在 module.role 中注明串行原因）
- [ ] 无入度 > 5 的瓶颈任务

### 文件映射

- [ ] files_to_read 的 must_read ≤ 5 个
- [ ] 每个 files_to_read 条目有 hint
- [ ] files_to_edit 中无共享上下文文件（CLI 自动附加）
- [ ] 路径格式正确（相对路径，无 .. 前缀）

---

## 9. 常见分解模式

### 9.1 新功能开发

```
task-接口定义（数据结构 + API 签名）
  → task-核心实现（算法/逻辑）
  → task-集成适配（接入现有系统）
  → task-边界处理（错误处理 + 极端输入）
```

### 9.2 Bug 修复

```
task-复现与定位（写失败测试 + 定位根因）
  → task-修复实现（最小变更修复）
```

若修复涉及多文件联动，按文件职责拆分为并行任务（无依赖）。

### 9.3 重构

```
task-提取接口（定义新抽象，不改行为）
  → task-迁移实现（逐模块切换到新接口）
  → task-清理旧代码（删除废弃路径）
```

### 9.4 数据管道

```
task-数据加载（输入解析 + 校验）
task-核心处理（转换/计算）     ← 可与加载并行（若接口已定）
  → task-输出导出（格式化 + 写入）
```

---

## 10. 字段填写速查

| 字段 | 必填 | 填写规则 |
|------|------|---------|
| `id` | 是 | `task-{kebab-case}`，全局唯一 |
| `name` | 是 | 人类可读，≤ 20 字 |
| `brief` | 是 | 一句话，说明"做什么"（不是"为什么"） |
| `module` | 是 | 引用 modules[].id |
| `depends_on` | 否 | 仅硬序约束，默认 [] |
| `estimated_hours` | 否 | 0.5-6，默认 1 |
| `importance` | 否 | 缺省时 CLI 自动推导；显式设置用于覆盖 |
| `difficulty` | 否 | low/medium/high，供人判断分配 |
| `requires` | 否 | 能力标签（如 ["python", "opencv"]），agent 据此匹配 |
| `acceptance_criteria` | 是 | 2-5 条，遵循 §5 规则 |
| `files_to_read` | 否 | must_read ≤ 5，每条含 hint |
| `files_to_edit` | 是 | 1-4 个，用于冲突检测 |
| `reviewers` | 是 | ≥ 1 个 agent ID（自声明字符串） |
| `verify_command` | 原则必填 | 执行覆盖全部 acceptance_criteria 的测试命令（见 §5.5）；豁免需满足 §5.5.4 |
| `max_attempts` | 否 | 默认 3，高难度任务可放宽 |
| `deliverables` | 否 | 定义对外契约（见 §5.7）。type 为 `code_api`（含 api.language + api.signatures）或 `data_format`（含 schema.file + schema.format + schema.fields）。仅对明确对外接口/格式的任务使用 |
