<!--

> **所属线**：共用内核（跨线）（2026-09-21 两条线分线规划归位，见 ROADMAP「## 里程碑」/「## 契约演进记录」）。
> 跨线文档**不得**含版本号与排期（一律指 ROADMAP）；本文件描述的是两条线共享的内核语义与契约。
  运行时规格 — Orchestra 的动态行为：CLI 命令、Agent 工作流、并发模型。
  读者：开发者、Agent 集成者、监管者。变更频率：中高。
  关联文档：system-design.md（数据模型与架构）、PROGRESS.md（进度与决策）。
-->

## 1. CLI 命令集

**规范源声明（task-docs-single-source）**：本文件为运行时语义的唯一规范源；
`implementation-design.md` / `system-design.md` / `user-manual.md` / `.orchd/rules/`
与本文件互斥处，以本文件 + 引擎代码常量为准（amend 白名单真源 = `schema/_master.schema.json` 的 `x-amendable` 注解，
机读推导见 `orchd/schema_policy.py::amendable_fields`；错误码全表见 `docs/_generated/error-codes.md`（由 `orchd/errors.py` 生成））。

**输出约定**：所有命令默认输出 JSON，供 agent 解析。`orchd status` 支持 `--text` 输出人类可读表格（仅改变展示层，不改变 JSON 语义）。错误统一使用 `{"error": {...}}` 结构（错误码见 `docs/_generated/error-codes.md`）。

<!-- BEGIN GENERATED: commands-runtime-spec -->
| 命令 | 参数 | 读写 | 锁 | 用途 |
|------|------|------|----|------|
| `validate` | - | 只读 | 否 | 校验 _master.json |
| `bootstrap` | - | 只读 | 否 | 输出分解套件 JSON |
| `init` | - | 写 | 是 | 初始化 .orchd/ 并生成 snapshot |
| `amend` | --task --files-to-edit --exempt-files | 写 | 是 | 增量更新 snapshot / 声明域补登 |
| `retract` | --event --reason | 写 | 是 | 撤回事件 |
| `force-status` | --task --status | 写 | 是 | 强制设置任务状态 |
| `merge-ack` | --task | 写 | 是 | merge_warning 人工销账 |
| `restore` | --path | 写 | 是 | 受管工作树还原 |
| `request` | --sort --phase | 只读 | 否 | 获取下一个候选任务 |
| `claim` | --task --type --confirm | 写 | 是 | 认领任务 |
| `done` | --task --changes | 写 | 是 | 报告任务完成 |
| `review` | --task --type --verdict | 写 | 是 | 提交审查结果 |
| `pool` | - | 只读 | 否 | 列出就绪池 |
| `status` | --text --audit-task | 只读 | 否 | 全局状态快照 |
| `show` | <task-id> | 只读 | 否 | 单任务卡面详情 |
| `watchdog` | --timeout --takeover --full | 写 | 否 | 僵死任务巡检 / 接管 |
| `doctor` | --path --fix --dry-run --backup-dir | 写 | 否 | git 仓库完整性检测 / 残留清理 |
| `milestone-check` | <milestone> --master | 只读 | 否 | 里程碑判据门禁 |
| `layout-migrate` | - | 写 | 是 | flat → container 布局迁移 |
| `full-regression` | - | 写 | 是 | 全量 pytest 并记录基线 |
| `ci-record` | --path | 写 | 是 | 记录远端 CI 结论 |
| `intake` | - | 写 | 是 | 提交摄入产物 |
| `roadmap-land` | <version> | 写 | 是 | ROADMAP 规划落地为 IDEAS |
| `context-digest` | - | 只读 | 否 | 必读面内容哈希 |
| `ledger-compact` | --dry-run | 写 | 是 | 终态事件归档 |
| `migrate` | --dry-run | 写 | 是 | schema 版本前移 |
| `master-archive` | - | 写 | 是 | 终态任务定义归档 |
| `storage-rollback` | - | 写 | 是 | sqlite→file 回滚 |
| `storage-recutover` | --dry-run | 写 | 是 | 回滚残留并账 |
| `git` | merge commit | 写 | 是 | git 写操作代理 |
| `ideas-archive` | - | 写 | 是 | 归档已完结 IDEAS |
| `idea` | append close confirm propose | 写 | 是 | 灵感写入门禁 |
| `ideas` | list | 只读 | 否 | IDEAS 台账只读盘点 |
| `session` | start current end | 写 | 是 | 会话生命周期 |
| `lesson` | add archive list report resolve review show stage | 写 | 是 | 经验回灌 |
| `sync` | --push --pull --compact --status --remote | 写 | 是 | 账本跨设备同步 |
| `line-sync` | - | 写 | 是 | 跨线回移通道 |
| `check` | - | 只读 | 否 | 静态门禁 ruff + mypy |
| `release` | plan run status | 写 | 是 | 发版受管通道 |
| `guards` | list check init | 写 | 否 | 宿主 guard 配置与检查 |
| `deps` | init check sync | 写 | 否 | 依赖图真源 |
| `docs` | list check write sync | 写 | 是 | 契约文档产物清单与生成 |
| `surfaces` | list validate check assemble | 写 | 是 | 落地面登记与汇总：surfaces list / validate / check / assemble |
<!-- END GENERATED: commands-runtime-spec -->

> `status` 选项语义（总览块不逐项承载）：默认仅活跃任务（pending/claimed/done/in_review）；`--all` 回退全量（含终态 completed/cancelled）；跟 task-id 输出单任务详情（与 `show` 等价）；summary 含 active + total + 六状态计数。

`amend` 语法：`orchd amend`，读取 `.orchd/_master.json`。
仅更新 snapshot 文件，不重置 ledger。
返回变更摘要：`amended` / `new_tasks` / `updated_tasks` / `unchanged_tasks_count` / `removed_tasks`（unchanged 只回计数不回列表，task-pass9-amend-response-ux）。
新增或变更的任务若声明了 `verify_command`，附加 `verify_dry_run` 字段（30s 限时试跑，仅提示不阻断注册）：
```json
{
  "verify_dry_run": [
    {"task_id": "task-x", "ok": false, "exit_code": 1, "stderr": "...", "hint": "dry-run 仅提示不阻断注册..."}
  ]
}
```
claimed 任务的白名单字段变更记录在 `whitelisted_updates` 中：
```json
{
  "whitelisted_updates": [
    {"task_id": "task-1", "fields": ["verify_command", "reviewers"]}
  ]
}
```
amend 成功后引擎 best-effort 自动提交 `.orchd/_master.json` 与 IDEAS.md（分支守卫：仅在 main 分支上执行 auto-commit）：
```json
{
  "commit": {
    "performed": true,
    "reason": "committed",
    "message": "chore(intake): orchd amend — 1 new / 0 updated / 0 removed"
  }
}
```
- `performed=false, reason=no_changes` — 范围内无未提交改动
- `performed=false, reason=not_on_main, branch=task/xxx` — 在任务分支上运行 amend（**注意：task-master-single-copy 后此路径已被 E007 拒绝注册取代**——split.amend 在非 default 分支直接 raise E007，不再走到提交降级；本分支判定为防御冗余，仅当 amend 未走红线 7 拦截时的兜底）
- `performed=false, reason=not_a_git_repo` — 非 git 仓库，降级跳过
```

### 1.1 amend 修改约束矩阵

基于任务当前状态（from checkpoint + replay）。逐字段可改性的单一真源 = schema 注解：`x-amendable`（amend 声明域）与 `x-terminal-attachable`（终态可附加），机读推导 `orchd/schema_policy.py::amendable_fields` / `terminal_attachable_fields`；逐字段取值的可读投影 = 生成物 `docs/_generated/fields.md` 的对应列（勿手改，重新生成命令见文件头）：

- `pending`：所有字段可改
- `claimed` / `done` / `in_review`：附加白名单 = `x-amendable: true` 字段集（其中 `files_to_edit` 只增不删）；其余字段需先 `force-status` 回 `pending` 才能改
- `completed` / `cancelled`：终态附加 = `x-terminal-attachable: true` 字段集（另有 `--revise-terminal` 纯文本修订通道，须 `--reason`）；核心字段终态锁定（`files_to_edit` / `files_to_read` 终态不可改）

AC 修改窗口（E-03 收口）：`acceptance_criteria` 仅 `pending` 可直接改；
`claimed/done/in_review` 锁死（回 `pending` 改）；终态走 `--revise-terminal`
且条数不变——完整窗口表见 `.orchd/rules/git.md`「AC 字段编辑窗口」。

验收分层（E-04 收口，三档各司其职，非双真源）：E029（条数/文件/工时上限）
为注册期 warning；`--revise-terminal` 条数硬门禁（E007）守的是“终态验收面
不变”；E039（共享入口登记测试覆盖）为 done 期硬阻断——三者判据与时机不同，
不互相替代

同域补充（2026-09-28 task-pass10-counts-pointers 登记，此前 rules 与本文件零登记）：
E037（validate_quality 阻断级——verify_command 引用的仓库内路径既未声明又不存在，
amend 注册点阻断）与 E038（brief「files_to_edit 控 N」与实际声明数不一致 →
warning，非拆分建议）同为 validate 质量域，定义见 `.orchd/rules/verify.md`「verify 域错误码」，处置口径见 `.orchd/rules/recovery.md`。

amend 对依赖链的影响：通过 amend 新增任务 X 且 `depends_on` 包含已完成任务 A — 合法，X 直接进入就绪池。修改未开始任务 B 的 `acceptance_criteria` 而 B 处于 review 阶段 — 不允许（review 状态仅可改 reviewers 或单独 verify_command）。若需修改，先 force-status 回 pending。

## 2. Agent Session 工作流

### 2.0 初始化阶段（首次 agent 进入新项目）

Agent 检测 `.orchd/` 不存在时进入此流程。初始化阶段为特殊 session，不受"单任务 session"约束（它本身不做开发任务，只做项目初始化）。

**建议**：初始化阶段完成后 agent 退出。下一个 agent session 进入日常阶段无缝衔接（检查 `.orchd/` 已存在 -> 跳过初始化阶段 -> 直接 request）。避免单 session 承载分解推理 + 首个任务实现，token 消耗过大。

```
1. orchd bootstrap
     CLI: 输出 JSON {schema, prompt, guide, next_step}
     （schema/prompt/guide 均为内联文本，无需 agent 读额外文件。
     合计约 2.6 万字符（以 decomposition-guide 为主要体量，中文约 1 万+ tokens），
     仅首次初始化阶段消耗一次）
2. Agent 读取 requirements.md + bootstrap 输出 -> 推理分解 -> 写 .orchd/_master.json
3. orchd validate
     - exit 0 -> 继续
     - exit 1 -> 修复错误后重试
4. orchd init
     CLI: 读取 .orchd/_master.json -> 生成 snapshot + 空 ledger
5. 初始化阶段完成。Agent 退出。下一个 session 自动进入日常阶段。
```

### 2.1 实现者（单任务 session）

```
步骤 1：索取候选任务

orchd request [--capabilities "python csharp"] [--exclude "task-a task-b"] [--sort importance|downstream|hours]

CLI 行为：
  1. 从 checkpoint + 增量 ledger replay 计算全任务状态
  2. 构建就绪池：status=pending 且 depends_on 全部 completed 或 cancelled
  3. 如有 --capabilities -> 过滤 task.requires subset-of capabilities
  4. 如无 --capabilities -> 不过滤，全池可用
  5. 排除 --exclude 列表中的任务
  6. 从池中选一个候选 -> 返回 JSON
  7. downstream_blocked 基于当前 ledger 状态实时计算，非静态值

返回 JSON 结构：
{
  "candidate": {
    "task_id": "task-ringbuffer",
    "name": "...",
    "brief": "...",
    "module": "mod-core",
    "difficulty": "medium",
    "estimated_hours": 3,
    "importance": "critical",
    "depends_on": [],
    "downstream_blocked": ["task-checkpoint", "task-gpu-heatmap"],
    "source": "idea:...",
    "conflict_with": [{"task_id": "...", "files": ["..."], "claimed_by": "..."}],
    "review_comments_count": 0
  },
  "pool_size": 4,
  "prompt": "确认将此任务分配给 {agent_id}？",
  "warnings": ["若 attempt_count >= max_attempts，附加：exceeded_max_attempts 警告"],
  "excluded_conflicts": [],
  "guidance": {"step": "request_impl", "command": "...", "hint": "..."}
}

若无就绪任务，返回空候选：
{
  "candidate": null,
  "message": "当前无就绪候选：...",
  "next_action": "exit",
  "pool_size": 0,
  "blocked_count": 3,
  "reason": "none_ready",
  "mismatched": [],
  "excluded_conflicts": [],
  "cooldown_excluded": [{"task_id": "task-x", "retracted_at": "...", "seconds_ago": 120}],
  "guidance": {"step": "stop_wait", "command": "...", "hint": "..."}
}
- next_action: "exit" — agent 应退出 session
- blocked_count — 因依赖未满足而被阻塞的任务数（供人判断进度）
- reason — 空池原因："none_ready"（无就绪任务）或 "capability_mismatch"（存在就绪任务但因能力不匹配被过滤）
- mismatched — 当 reason=capability_mismatch 时，列出就绪但能力不匹配的任务：[{task_id, requires}]
- excluded_conflicts — 因文件冲突被排除的候选任务列表
- cooldown_excluded — 因 retract 认领冷却期（默认 300s）被暂时排除的任务：[{task_id, retracted_at, seconds_ago}]；message 中会包含冷却归因说明
- guidance — 空池时附加 stop_wait 引导，明确停止等待用户指令

审查者模式额外分流（存在 in_review 任务时 `request` 自动优先返回审查候选，或经 `review_priority` 提示）：
- `reviewers` 名单仅作向后兼容门禁：字段存在且非空时，不在名单内 → `candidate: null, reason: "not_in_reviewer_list"`，
  message 提示"有 N 个待审查任务但你不在名单内"，并列出 `tasks`（含各任务 reviewers 名单）。
  Agent 应将 message 呈现给人，由人确认是否 amend 更新 reviewers。
- 字段缺失/为空（指纹身份模型，生产 `_master.json` 常态）→ 跳过名单门禁，仅按会话指纹去重；自审任务**不会成为候选**（`request` 一律排除，认领侧另有 E016 硬门）。
  `is_self_review` 字段仍落账到 `REVIEW_CLAIMED` / `REVIEW_SUBMITTED` 事件与派生状态
  `review_self_review`，但自审在 claim 阶段即被拒绝，故恒为 `false`。
```

```
步骤 2：人工确认

Agent 将 candidate + prompt 呈现给人。人作出判断：

（设计说明：人工确认是 v1 的信任模型选择——人作为最终调度器，防止 agent 误判能力匹配
或抢到不适合的任务。v1.1 的 `--auto-claim` 将支持绕过此步骤。）

  A. 执行（两段式确认闸门，v1.2）-> orchd claim --task {task_id}
     CLI（无 --confirm，预览阶段，只读不写事件不建分支）-> 返回 JSON:
     {
       "confirm_required": true,
       "claim_type": "implementer",          // 或 reviewer
       "hint": "预览模式：确认无误后请加 --confirm 真正执行认领（写事件 + 建分支）",
       "preview": {
         "task_id": "{task_id}",
         "name": "...",
         "brief": "...",
         "module": "mod-core",
         "depends_on": [...],
         "reviewers": ["reviewer-1"],
         "current_status": "pending",
         "review_phase": "spec",
         "git": {"available": true, "branch": "main", "clean": true},
         "actions": ["写 CLAIMED 事件到 ledger", "创建/切换 task/{task_id} 分支"],
         "expected_checks": [
           {"check": "任务处于 pending（可认领）", "expected_pass": true},
           ...
         ]
       }
     }
     -> 人/agent 核对预览无误后，带 --confirm 真正执行:
     CLI: 获取锁 -> 检查任务仍在池中 -> 文件冲突检测 -> append CLAIMED -> 释放锁
     返回 JSON（与既有契约一致）:
     {
       "claimed": true,
       "task": { 完整定义 },
       "event_id": "evt-xxx",
       "files_to_read": [
         {"path": "src/core.py", "priority": "must_read", "hint": "环形缓冲区实现"},
         {"path": ".orchd/shared/conventions.md", "priority": "reference", "hint": "编码规范（高风险领域自动附加）"}
       ],
       "files_to_edit": ["src/ringbuffer.py"],
       "review_comments": "如果有历史审查意见（含打回历史），内联在此",
       "previous_changes": "上一个实现者的 changes_description（如果是返工任务）"
     }

（设计说明：确认闸门（task-claim-confirm-gate，2026-08-14）把显式 claim 拆为
预览/确认两段，防止误领/误执行。reviewer 角色预览额外含 review_phase（spec/code）、
reviewers 名单、done_by 与自审阻断预期（E016：实现者 = 审查者，无条件阻断）。
`request --auto-claim` 为无人值守路径，内部直接执行 claim、跳过确认；预览项
全部仅作透明展示，真正的 E016 硬门在带 --confirm 的 claim() 锁内执行。

（设计说明：shared/ 按需附加（1.1）。默认不附加——仅高风险领域任务（mod-core 或
files_to_edit 含 orchd/ 引擎文件 / .orchd/_master.json）自动附 conventions.md；
architecture.md 仅任务 files_to_read 显式引用时提供。`--with-context` 显式附加全部。
若 shared 缺省，CLI 跳过共享上下文附加，files_to_read 仅含任务级条目，不报错。）

  B. 跳过 -> Agent 将此 task_id 加入排除列表 -> 回到步骤 1 带 --exclude

  C. 重新声明能力 -> 回到步骤 1，带新的 --capabilities
```

```
步骤 3：执行

Agent 读 files_to_read -> 实现 -> 修改 files_to_edit

  版本控制（git 仓库自动启用）：
  - claim 成功后 CLI 自动切换到任务分支 `task/{task_id}`：首次 claim 新建（`checkout -b`），返工 claim 复用已有分支（`checkout`）；best-effort，非 git 仓库静默跳过
  - 若非 git 仓库，输出警告并跳过，不做任何 git 操作
  - 若 review 打回（CHANGES_REQUESTED），新 agent 重新 claim 时复用原分支（继续在原 task 分支上工作）
  - 多任务并行时，各分支 base 为 claim 时刻的 main HEAD，互不影响
  - merge 由审查者在 `orchd review --verdict APPROVED` 时自动完成（见 §2.2 步骤 4）
```

```
步骤 4：报告完成

orchd done --task {task_id} --changes "改用 collections.deque 实现 O(1) 的 push/pop，移除了 RemoveAt(0)" [--concerns "备注"]

CLI 行为：
  若 task 定义了 verify_command：锁外执行该命令（超时 120s）:
    - exit 0 -> 继续
    - exit != 0 -> 返回失败 + stderr，不写 DONE。任务保持 CLAIMED 状态。
      agent 应修复问题后重新调用 `orchd done`；若无法修复，退出 session，
      由人运行 `orchd force-status --task {id} --status pending` 回收任务
    ⚠️ 安全提示：verify_command 在开发机上以当前用户权限执行任意 shell。
    _master.json 由初始化阶段 agent 生成并经人 validate 确认，属于可信输入。
    v1 不做沙箱隔离；v1.1 可考虑白名单限制或 --allow-exec 显式授权。
  -> 无论是否有 verify_command：
  -> 完整性硬门禁（task-engine-done-integrity-gate，2026-08-24）：
     a) 跨 worktree 脏写检测：主工作树存在与当前任务 files_to_edit 重叠的
        未提交改动 -> 拒绝 done，返回 E017 dirty_workspace + overlap_files，
        hint 提示"这些文件应在任务 worktree 内修改并由 done 提交；请先
        提交/还原主工作树改动后重试"
     b) 声明文件 diff 完整性：files_to_edit 中任一文件未进入任务分支 diff
        -> 拒绝 done，返回 missing_declared_files，hint 提示回任务分支补提交
     c) 提交零残留：files_to_edit 范围内仍有未提交跟踪改动 -> 拒绝 done，
        返回 E017 dirty_workspace（引擎 ensure_committed 只兜底已提交改动，
        exempt_files 豁免文件须实现者自行 git commit）
  -> 获取锁 -> replay 二次校验（任务仍 claimed 且 claimed_by 为本 agent）
  -> append DONE(含 changes_description) -> append REVIEW_READY(spec)
     （REVIEW_READY 的 review_type 由 project.review_mode 与 files_to_edit 决定，
      unified 项目不写该字段）
  -> 更新 checkpoint -> 释放锁
  -> best-effort 自动提交变更（见 gitops.md 安全约束）

返回 JSON：
```json
{
  "done": true,
  "task_id": "task-1",
  "status": "done",
  "attempt_count": 1,
  "event_id": "evt-xxx",
  "review_created": {
    "type": "spec",
    "mode": "two_phase",
    "reason": "默认双阶段：先 spec 后 code"
  },
  "review_handoff": {
    "prompt": "你是一次独立审查的 subagent……（含 task_id/阶段/worktree/项目根/token 注入/claim&review 命令/模板/must_read & reference 清单/comments 首句/只读约束/JSON 输出契约）",
    "session_token": "tok-xxxxxxxx",
    "reviewer_session_id": "b1c2...",
    "parent_session_id": "a1b2...（本任务最后一次 DONE 的 session_id）",
    "implementer_fingerprint": "3f9c0a1b7d2e（DONE.agent_id，实现者 12 位指纹）",
    "reviewer_fingerprint": "8d4e2c5a9f10（审查会话 12 位指纹）",
    "worktree": "C:/.../task-task-1",
    "hint": "把 prompt 原样交给当前会话 spawn 的独立 subagent；无 subagent 能力请 retract 后停止并上报",
    "phase": "spec",
    "template": "templates/spec-reviewer.md",
    "files_to_review": [
      {"path": "src/a.py", "priority": "must_read"},
      {"path": ".orchd/shared/architecture.md", "priority": "reference"}
    ]
  },
  "commit": {
    "performed": true,
    "reason": "committed",
    "message": "..."
  }
}
```
- `commit.performed=false` 表示引擎提交降级（非 git 仓库、git 不可用、无改动或提交失败）
- `review_created` 契约（task-engine-defect-fix-unified-u13，2026-11-02）：
  三个键由单一真源 `orchd/onboard/_config.py::resolve_review_created` 推导，
  `type` 保留历史取值（`unified` / `code` / `spec`）供旧消费方零改动；新增
  `mode` 与 `reason` 两个正交维度——
  * `mode=unified`：项目 `project.review_mode=unified`（单阶段审查，一次 APPROVED 即 merge）；
  * `mode=two_phase_single_stage_doc`：双阶段项目下 `files_to_edit` 全为文档白名单
    且不触碰约定/状态文件 → 跳过 spec，直达 code 终审（此时 `type=code`）；
  * `mode=two_phase`：默认双阶段（`type=spec`），或上轮 code 范围打回后返工直达
    code 审查（`type=code`，`reason` 注明 `rework_scope=code`）。
    `type=code` 因此不再唯一标识原因；需要区分「文档单阶段」与「返工直达」时
    必须读 `mode`/`reason`。REVIEW_READY 事件的字段集不变。
- `review_handoff` 交接面（task-review-handoff-engine，2026-11-04）：done 成功后
  由引擎自动生成，实现方**无需**再拼 `session start` 命令。结构：纯函数
  `orchd/review/handoff.py::render_handoff_prompt` 渲染的 `prompt`（含 task_id、阶段、
  worktree + canonical 项目根、PowerShell/bash 两种 token 注入、`claim`/`review` 命令、
  对应模板、`must_read`/`reference` 清单、comments 首句、只读/不 push/不切分支/不 merge
  约束与 JSON 输出契约；**明确要求 subagent 不再执行 session start**）；`session_token` 与
  `reviewer_session_id` 由引擎 mint 的审查会话（`kind=reviewer`、`parent=本任务最后一次
  DONE 的 session_id`，即本响应的 `parent_session_id`）导出；审查清单 `files_to_review`
  与 `claim` 同源（单一 helper `orchd/review.handoff.files_to_review`）。comments 首句
  固定为「本审查由实现会话 `<implementer_fingerprint>` spawn 的独立 subagent 执行，
  审查指纹 `<reviewer_fingerprint>`」——两枚指纹均 12 位 hex，分别取自 DONE 事件
  `agent_id` 与 mint 返回的 `fingerprint`，prompt 内不留占位符。失败
  （DONE 缺 session_id / mint 失败 / worktree 或模板路由解析失败 / 指纹缺失）时
  `review_handoff` 退化为
  `{"error": {"code": "review_handoff_failed", "reason": ..., "message": ..., "hint": ...}}`，
  `reason` ∈ `done_session_id_missing` / `reviewer_session_start_failed` /
  `handoff_render_failed`（build 内可控失败），以及 CLI 兜底的 `unexpected_exception`
  （build 之外的未知异常，语义同为「done 已成功、交接失败」）；done 既有字段与成功语义
  不变，调用方据 `error` 字段 fail-closed。
  prompt 的判定口径（task-review-handoff-prompt-polish，2026-11-05）：AC 不复制进
  prompt，只声明 `acceptance_criteria` 来自 `claim` 响应，要求逐条「满足/不满足/需人工 +
  证据」；意见文件必须位于仓库外（PowerShell `%TEMP%` / bash `TMPDIR`，UTF-8），worktree
  的 `cd` 加引号；token 只注入 `ORCHD_SESSION_ID`，不写入意见/JSON/命令行/日志；
  `must_read`/`reference` 为空时省略空段，`project_root` 与 worktree 相同则省略，身份段
  压缩为两行；`claim` 因 E016/E033 失败时 reviewer 不自行取号、不换 token，停止并回报。

Session 结束。Agent 退出。
```


### 2.1.1 宿主 guard 的 done 期分派（off / warn / hard）

> 依据：task-host-guard-config（配置层）与 task-host-guard-done-wiring（分派接线，
> 2026-11-03）。配置真源：项目根下 `.orchd/shared/guards.json`（宿主自管，不入引擎
> 默认行为；无配置文件时全链 `off`，done 响应与接线前逐条一致）。

done 在算完改动集（分支 diff ∪ 工作树未提交改动，取不到时回退声明 files_to_edit）后、
进入镜像闭环与共享入口判定前，读取一次配置并对每个 `mode != off` 的 guard 调用其
评估器（注册表 `orchd.guards.GUARD_EVALUATORS`，由各 `host-guard-*` 卡在自己的模块里
显式注册）：

- `off`：不调用评估器，不改任何结果（真不跑）；
- `warn`：命中项只写 `degraded_guards`（`code=E030`、`status=not_applicable`，
  记录顶层 `guard_name` 为具体 guard、`context` 带 `task_id` / `mode` / `findings`），
  **不改变 done 结果**；
- `hard`：命中即复用共享入口门禁的 **E039** 通道——`details` 为逐 guard 一项：
  `{"task_id", "rule": "<guard 名>", "mode": "hard", "findings": [...], "hint"}`；
  `details.rule` 是**具体 guard 名**（如 `census` / `docs`），不是 `drift_closure` 这类内建名。

降级纪律（与 drift_closure / generated_stale 家族同）：配置非法、评估器缺失、评估器
抛异常（含返回非 `GuardFinding`、`as_dict` 失败）都按 `failed` / `not_applicable` 写
`degraded_guards` 留痕，**不抛裸异常、不阻断 done**；无配置 / 空配置 / 全 `off` 时
立即返回、零新增降级。`guards list` 与 `doctor`
的回显只读（不写盘、不执行评估器、零外部子进程）。


### 2.2 审查者（单任务 session）

```
步骤 1：索取候选审查

orchd request [--exclude "task-x"]

CLI 行为：
  从 checkpoint + 增量 replay -> 找 REVIEW_READY 事件（spec 优先于 code）
  -> 返回候选审查任务 JSON

审查顺序（串行）：
  spec review 先分配。只有 spec review APPROVED 后，code review 才进入就绪池。

审查模式（`mode` / `review_phase` 单一真源；`review_created.mode` 与 claim/submit 的 mode 判定同源）：
  - mode=unified（单阶段）：只做一轮审查，一次 APPROVED 即 merge；
  - mode=two_phase_single_stage_doc（文档单阶段）：跳过 spec，直达 code 终审；
  - mode=two_phase（默认双阶段）：先 spec 后 code；上轮 code 范围打回后的返工直达
    code 审查（reason 注明 rework_scope=code）。
  认领审查用 `claim --task {task_id} --type spec|code`（取值来自任务的 review_phase）；
  mode=unified 时省略 `--type`（`type` 仅为旧消费方兼容字段，不唯一标识原因）。

  若无待审查任务，返回空候选：
  ```json
  { "candidate": null, "message": "当前无待审查任务", "next_action": "exit", "pool_size": 0 }
  ```
  - `next_action: "exit"` — agent 应退出 session
  - `next_action: "retry_later"` — agent 可等待后重试（后续版本支持）
  - `next_action: "switch_role"` — agent 可切换为 implementer 角色继续（后续版本支持）
```

```
步骤 2：人工确认

同实现者流程（执行 / 跳过 / 重新声明能力）

确认后：orchd claim --task {task_id}
  -> 校验：`reviewers` 名单存在且非空时，agent_id 必须在该名单内，否则报 E007
     （会话指纹形态 agent_id（12 位 hex）豁免名单校验；名单空/缺失时不触发 E007，
       details 附名单与提示：改用名单内 ID，或先 orchd amend 修改 reviewers）
  -> append REVIEW_CLAIMED -> 返回审查详情 + files_to_review + acceptance_criteria + changes_description
  -> 自动附加 shared/ 文件：spec review 附 architecture.md（reference），code review 必附 conventions.md（must_read）
     （shared 缺省时跳过此步，与实现者流程一致）
```

审查认领前与提交结论前均执行 **lineage 硬门**（单一真源
`orchd/ledger/session.py::require_review_lineage`；claim 与 submit 双检）：

- 审查会话必须 `kind=reviewer`，且 `parent_session_id` 等于该任务**最后一次 DONE 事件**的
  会话 id（两者由引擎在 `done` 成功时 mint，随 `review_handoff` 交接；实现会话把
  `review_handoff.prompt` 原样交给 subagent 即可）；
- 不满足一律抛 `E016 self_review_blocked`，`details.reason` 区分
  `lineage_missing`（未声明 kind/parent）/ `kind_not_reviewer` /
  `lineage_mismatch`（parent 与实现会话不一致，含「孙代 subagent」链）；
- 旧账本 DONE 事件缺 `session_id` 时 **fail-closed**：判 `lineage_missing` 并带
  `details.missing_evidence="done_session_id"`；
- 成功认领的响应回显 `review_lineage`（`{kind, parent_session_id}`）供审计；
  `REVIEW_CLAIMED` 事件字段集保持不变（lineage 只活在响应面）；
- parent 仍是**会话自声明**：引擎只校验其与 DONE 会话 id 的一致性，不能证明宿主未伪造；
  不可伪造需服务端身份（见 [system-design-online.md](system-design-online.md) §11）。

```json
{
  "review_lineage": {"kind": "reviewer", "parent_session_id": "3f2a1c8d（与最后一次 DONE 的 session_id 相同）"}
}
```

- 无引擎身份的直连 Python API 调用（`session_id` 为空）不进入 lineage 判定；
  CLI 写命令在更早的 `E033` 会话门已被拦截，故该豁免不构成绕过面。

失败示例（未声明 lineage，认领即被拦截；错误信封与引擎统一响应一致，见 `orchd/errors.py::to_json_response`）：

```json
{
  "error": {
    "code": "E016",
    "message": "review_lineage_gate[lineage_missing]: 审查会话 lineage 未通过硬门（stage=claim）",
    "details": [
      {
        "task_id": "task-...",
        "reason": "lineage_missing",
        "stage": "claim",
        "kind": "",
        "parent_session_id": "",
        "expected_parent_session_id": "<该任务最后一次 DONE 事件的 session_id>",
        "missing": ["kind", "parent_session_id"],
        "hint": "审查会话须使用 done 响应 review_handoff 交接的身份（kind=reviewer、parent=该任务最后一次 DONE 的 session_id）再认领"
      }
    ],
    "severity": "error",
    "suggest_report": false
  }
}
```

```
步骤 3：审查

Agent 读 files_to_review -> 对照判定标准评估：
  - spec review: 逐条对照 acceptance_criteria；若 task 定义了 deliverables，逐项核对契约一致性（API 签名 / 数据格式）
  - code review: 对照 shared/conventions.md 编码规范
```

```
步骤 4：提交审查

orchd review --task {task_id} --type spec|code --verdict APPROVED|CHANGES_REQUESTED [--comments "意见"]

  APPROVED + spec review:
    -> append REVIEW_SUBMITTED(spec, APPROVED)
    -> 自动 append REVIEW_READY(code)
    -> 更新 checkpoint

  APPROVED + code review（受管 merge 收口）:
    -> 锁内暂存 REVIEW_SUBMITTED 事件但不立即写入
    -> 释放锁后 diff 完整性校验（task-engine-review-merge-diff-gate,
       2026-08-24）：校验声明文件已全部进入任务分支 diff；缺失则
       拒绝 merge（E010 file_conflict，返回 missing_declared_files，
       hint 提示回任务分支确认已提交、或从 files_to_edit 移除/补充说明），
       任务停留 in_review，不写完成事件
    -> 审查纪律：提交 code APPROVED 前跑 `status --audit-merge`（只读巡检），
       确认 merge_audit.warnings 为空；有 blocking 告警先按指引处置，不改仓库
    -> 校验通过后由引擎在主工作树执行受管 merge（try_git_merge；merge-wt 已废弃，
       任务 worktree 永不 checkout main；并发 merge 以主工作树锁串行）
    -> 内容冲突：先自动解冲突（try_auto_resolve_conflict）；
       自动失败且审查者显式授权（authorize_reviewer_resolve）→ 解 REVIEW-RESOLVE
       标记段（保留双方内容）后继续；无授权/解不了 → 回退 E015，要求实现者在
       task 分支 `orchd git merge main` 解决冲突后提交；冲突现场用受管
       `orchd git merge --continue` 收口（close_merge_in_progress，无 pathspec
       提交，禁止手工 `git commit` 绕过），随后重试 code APPROVED。任何冲突失败都不写完成事件：
       任务停留 in_review，返回：
       {"merged": false, "reason": "merge_conflict", "conflict_files": [...],
        "action": "..."}（`"task_status"` 保持 in_review）
    -> 非冲突失败（git 不可用 / 非 git 仓库 / 主工作树脏写阻断 / untracked 碰撞 /
       merge 超时等）：返回 {"merged": false, "reason": "merge_env_error",
       "merge_diagnostic": {...}}，任务停留 in_review，action 给可执行处置
       （untracked 碰撞属文件系统移动/删除，不是 git 写操作）
    -> merge 成功：
       重新获取锁 -> 二次校验（任务仍 in_review 且 review_claimed_by 为本 reviewer）
       -> 校验通过：append REVIEW_SUBMITTED -> checkpoint -> completed；
          同一收口内回收任务 worktree/分支、内联 merge 审计（自残留分档：
          blocking 弹回 in_review，目录残留仅注解）
       -> 校验失败（merge 窗口内状态被改）：返回 reason=state_changed_during_merge
    -> 单目录无 git（nogit 布局，工作已在主目录）：跳过 merge，直接走完成路径

  CHANGES_REQUESTED:
    -> append REVIEW_SUBMITTED(type, CHANGES_REQUESTED, comments)
    -> 任务回到就绪池
    -> 更新 checkpoint
    -> 下次实现者 request 时，review_comments + previous_changes 内联返回
    -> 返工后无论哪一轮被打回，DONE 后均重新进入 spec review -> code review 两轮完整审查
       （v1 策略：简单且安全；不记录"已通过的审查阶段"）

Session 结束。
```

### 2.3 浏览路径（人不确定分配时）

```
orchd pool [--capabilities "python"] [--all]

返回整个就绪池（基于当前 ledger 状态实时计算）：
```json
{
  "pool": [
    {"task_id": "task-1", "name": "...", "brief": "...", "module": "mod-a", "difficulty": "low", "importance": "normal", "blocked_downstream_count": 0}
  ],
  "pool_size": 1,
  "all": [
    {"task_id": "task-2", "name": "...", "module": "...", "status": "claimed", "difficulty": "low"}
  ]
}
```
- `pool` — 就绪任务列表（pending + 依赖满足），含 `difficulty` 字段
- `pool_size` — 就绪任务数量
- `all` — `--all` 开关时返回全部任务，每项附加 `status` 和 `difficulty` 字段

人浏览后告诉 agent 尝试 request。
```

### 2.4 误操作恢复

```
orchd retract --event {event_id} --reason "误标记 DONE，实际未完成"

CLI: 获取锁 -> append RETRACT(引用原 event_id) -> 级联撤回后续依赖事件 -> 重新计算状态 -> 更新 checkpoint -> 释放锁
```

RETRACT 级联规则详见 system-design.md §4.5。核心原则：撤回一个事件时，其后所有逻辑依赖该事件的事件自动失效（CLI 逐条标记，审计保留）。不允许 RETRACT FORCE_STATUS 事件。

```
orchd force-status --task {task_id} --status {target} --reason "说明" [--assignee {agent_id}]
```

**force-status 合法目标状态与副作用**：

| 目标状态 | 允许从 | 副作用 |
|---------|-------|--------|
| `pending` | claimed / done / in_review（默认矩阵；completed→pending 须 `--force + --evidence-sha` 复活门禁，cancelled→pending 须 `--force` 逃生口） | 重置 attempt_count = 0；释放文件占用；若在 review 阶段，审查者占用解除 |
| `claimed` | pending | 需同时指定 --assignee {agent_id}；等效于手动 claim（跳过冲突检测） |
| `completed` | in_review（默认）/ claimed（须 `--force` 逃生口） | 跳过剩余审查；引擎 `--ff-only` 快进落码（分支领先 main 时自动落码并透出 `merge` 字段，分叉则拒绝） |
| `cancelled` | 任意非 completed（completed→cancelled 非法，`--force` 亦不放行） | 任务永久移出就绪池，不参与依赖计算（下游视为已满足） |

不允许的目标：force 到 `done`（必须通过正常 DONE 流程）、force 到 `in_review`（必须通过 REVIEW_READY 触发）。

CLI: 获取锁 -> append FORCE_STATUS(含 target_status + reason) -> 更新 checkpoint -> 释放锁

### 2.5 hook 路径探测三态（fail-closed）

安装器、卸载器、门禁与残留检测共用同一探测点 `orchd.gitops.hook.paths._get_hooks_dir`
（解析 `core.hooksPath`：相对值按仓库根解析、绝对值原样）。它把「判定」与「回退」
拆成三态，调用方不得把「没查成」当成「未配置」：

| status | 含义 | 返回值 | 消费方处置 |
|--------|------|--------|-----------|
| `ok` | git 正常且 `core.hooksPath` 已配置 | 解析后的 hooks 目录 | 正常安装/卸载/检测 |
| `not_configured` | git 正常回答「未设置/空值」（rc=0 空值或 rc=1 键不存在） | `<repo>/.git/hooks` | 该位置没有 hook 是真相，可据此告警 |
| `git_unavailable` | git 无法回答（可执行缺失/权限拒绝/超时/非仓库/rc 非 0,1） | `<repo>/.git/hooks` 仅为兼容旧调用面的回退值 | **禁止消费回退值**：改走 `_resolve_hooks_dir_checked`，它返回结构化 fail-closed 结果（不写、不删），由调用方显式报错 |

`_get_hooks_dir(project_root, probe={...})` 回填 `{"status", "hooks_dir", "raw", "source", "error"}`
（`raw` 仅 `ok` 时非空）；门禁与清理路径（installer / uninstaller / residue / doctor
hook guard）一律走 `_resolve_hooks_dir_checked`，不得直接消费 `_get_hooks_dir` 的回退
路径——探测失败期间写/删 hook 会把门禁落到错误目录并静默失效
（u11 `task-engine-defect-fix-unified-u11-hook-path-failclosed`）。

## 3. 就绪池与并发

### 3.1 就绪池定义

全局池，不按 module 限缩。筛选条件：
- status = pending（via checkpoint + 增量 replay）
- depends_on 全部 completed 或 cancelled（审查通过或作废均视为依赖满足，避免在上游可能打回的基础上工作）
- 如果 agent 声明了 capabilities：task.requires subset-of capabilities
- 排除 --exclude 列表中的任务
- downstream_blocked 实时计算，不依赖静态 snapshot
- 候选选择排序：`importance desc -> blocked_downstream_count desc -> estimated_hours asc`（request 支持 `--sort` 覆盖）

### 3.2 task.requires

`_master.json` 中 task 的可选字段。例如 `"requires": ["python", "opencv"]`。

Agent 未声明 capabilities -> 不过滤 -> 全池可用。
Agent 声明了 capabilities -> 只返回 `task.requires ⊆ agent.capabilities` 的任务。
即任务的每个 require 标签都必须在 agent 的能力列表中，agent 可以有额外的能力。

### 3.3 并发控制

两个 agent 同时 `orchd request`：
- `request` 只读，不需要锁。两个 agent 可能看到同一个候选。

两个 agent 同时 `orchd claim` 同一个任务：
- `claim` 通过 `ExclusiveFileLock` 获取排他文件锁（flock 为唯一互斥权威，无 check-then-act 窗口）-> 从 checkpoint + 增量 replay -> 检查任务仍在池中（未被别人 CLAIMED）
  - 如果已被抢占 -> 返回抢占失败（E012）-> agent 回到 `request`
  - 如果仍在池中 -> 文件冲突检测 -> append CLAIMED -> 更新 checkpoint -> 释放锁 -> 返回成功
- 只有一个 agent 抢到

**多 worktree 并行（1.4，multi-worktree-m-p1，2026-08-22）**：仓库开多个 worktree 并行推进时，各 worktree 共享同一账本（container 默认 `<容器>/.orchd-runtime/`，或 `ORCHD_HOME` 重定向；flat 维持 `.orchd/` 零回归），并发写由**统一排他文件锁原语（ExclusiveFileLock）**兜底——存储层 `.lock` 基于 flock（内核托管，进程退出自动释放，无僵死锁），append/checkpoint 写原子化，跨 worktree 互斥；辅以 E011 任务级忙度锁（agent 一次一任务/审查，防同一 agent 多 worktree 重复领任务）。**任务 worktree 全生命周期引擎自动**：claim 自动创建 + 绑定（`session-worktrees.json`，响应返回 `worktree_path`）、终态自动回收、孤儿惰性清理（status/watchdog）——agent 零 worktree 管理操作。**merge 在主工作树内由引擎执行**（`main_worktree_root` 定位，专用 merge-wt 已废弃），任务 worktree **永不 checkout main**；并发 merge 以主工作树锁串行。**依赖完成级串行（E008）**：并行仅限无依赖就绪任务（request 已按依赖闭包放行 `conflict_with`），依赖链保持完成级串行——上游 completed/cancelled 后下游才可领取，杜绝并行撞车。

### 3.4 文件级写冲突检测

`claim` 执行时额外检查：目标任务的 `files_to_edit` 是否与所有当前 `CLAIMED`（未 DONE / 未 RETRACT）任务的 `files_to_edit` 有交集。

有交集 -> claim 失败，返回统一错误格式（冲突列表在 details 中）：
```json
{
  "error": {
    "code": "E010",
    "message": "file_conflict: ...",
    "details": [
      {"task_id": "task-other", "files": ["src/core.py"], "claimed_by": "agent-ui"}
    ]
  }
}
```

无交集 -> claim 成功。

**request 层前置过滤（2026-08-08 重构，依赖感知强制过滤）**：`request` 在返回候选前
即执行一轮文件冲突预检，与 E010 互为双保险：
- 候选与 **claimed 活跃任务**冲突 -> 硬排除（领取必撞 E010，提前拦截），记入响应 `excluded_conflicts`
- 候选与 **pending 非依赖任务**冲突 -> **软提示**（2026-09-27 对齐实现，task-pass9-conflict-docs-align）：
  候选保留在可领取集合，响应以 `candidate_conflicts` 注解 + 降权排序，串行化意图靠拆解期
  depends_on 规划与拆解者知情决策（实现见 `request.py` 的 `pending_soft` 分支）
- 候选与 **pending 依赖任务**冲突（依赖闭包内，祖先或子孙）-> 放行，标注 `conflict_with`（依赖链串行正确，不会并行领取）
- 候选与**在途任务**（分支有未落 main 改动）重叠 -> 按 `config.conflict_policy` 分流：缺省 `warn`
  仅注解 + 降权，`serialize` / `block` 才硬排除
- 无 `--force` 绕行通道；E010 仍是 claim 的最终不可绕过的边界

注册阶段（`amend`）的冲突校验同时从"拒绝注册"降级为"warning 收集"（`conflict_warnings`），
冲突任务仍可注册，由上述 request 强制过滤与 claim E010 兜底。


### 3.5 Agent ID 单任务约束

`claim` 执行时检查：同一 `agent_id` 是否已持有另一个未完成（CLAIMED 但未 DONE / 未 RETRACT）的任务。如果是 -> claim 失败。

### 3.6 Watchdog 自动巡检（受 Gas Town Deacon 启发）

Orchestra v1 不做 daemon，但提供 `watchdog` 命令供人手动或 cron 调用，检测僵死任务。

```bash
orchd watchdog [--timeout 60]
```

行为：
- replay ledger -> 检测两类僵死：
  1. `claimed` 状态且超过 `--timeout` 分钟未更新（实现者僵死）
  2. `in_review` 且 `review_claimed_by` 存在且超过 `--timeout` 分钟未更新（审查者僵死）
- 输出报警 JSON（不自动 force-status，仅报告）；默认 `stuck_tasks` 为**聚合视图**（`top` 默认保留 5 条、按 `stuck_minutes` 降序；下例两条已完整列出，故 `truncated=false`）：
```json
{
  "stuck_tasks": {
    "total": 2,
    "truncated": false,
    "top": [
      {"task_id": "task-checkpoint", "review_claimed_by": "agent-reviewer-1", "stuck_minutes": 70, "stuck_kind": "reviewer", "action": "建议运行: orchd force-status --task task-checkpoint --status pending"},
      {"task_id": "task-ringbuffer", "claimed_by": "agent-core", "stuck_minutes": 45, "stuck_kind": "implementer", "action": "建议运行: orchd force-status --task task-ringbuffer --status pending"}
    ],
    "by_type": {"reviewer": 1, "implementer": 1}
  },
  "stuck_count": 2,
  "summary": "2 个任务可能僵死"
}
```

**聚合契约（task-diagnostics-aggregate-budget）**：默认 `stuck_tasks` / `stale_claims` / `stale_sessions` 均为聚合视图 `{total, truncated, top, by_type}`（`top` 默认保留 5 条；`stuck_tasks` 的 `top` 按 `stuck_minutes` 降序，`stale_claims` / `stale_sessions` 保持调用方保证的确定输入序；`truncated=true` 表示明细被截断）；`stuck_tasks_summary` / `stale_claims_summary` / `stale_sessions_summary` **恒为聚合视图**，不受 `--full` 影响；`stuck_count` 恒为全量条数（非 `top` 长度）。

**`--full` 逃生舱**：`orchd watchdog --full` 时上述三个明细字段回退为**全量数组**（`stuck_tasks` 按 `task_id` 升序输出以保证字节稳定），聚合 `*_summary` 字段同时保留。

- exit code：0 = 无僵死任务，1 = 发现僵死任务（可用于 CI/监控集成）

**与 manual 模式的关系**：watchdog 不自动回收任务（避免误杀正在工作的 agent），只做检测和报告。实际回收仍需人评估后执行 `force-status`。后续 v1.1 可增加 `--auto-recover` 选项。

**session 锁巡检（L2）**：watchdog 同时巡检 `.orchd/.session.lock`——新式 flock 活性锁（JSON 含 `flock_active: true`）由 `session_lock_check` 先做**非阻塞 OS 探活**：持锁进程已死（内核已释放 flock）即判定 stale 并自动清理（响应 `session_lock.status="released"`，`reason="stale_os_probe"`），活锁（进程存活持有 flock）即使超过 `--timeout` 也不仅凭 timeout 判死，仅报告（`status="active"`）；旧纯 JSON 锁保持按 `--timeout` 判定兼容，不探活不误清。仅释放僵死锁，不回收任务状态。

**任务完整性巡检（task-engine-audit-coverage，2026-08-24）**：watchdog 结果附加 `task_integrity` 字段（只读 best-effort，非 git / 无 project_root 时返回 skipped 结构，不影响既有字段），巡检活跃任务（claimed / done / in_review）的 `files_to_edit` 声明文件：
- `main_dirty_overlap`：main 工作树存在同名未提交改动（跨 worktree 脏写：实现者在任务 worktree 提交后 main 上仍有残留，疑似漏提交/错 worktree）；
- `missing_declared_files`：任务分支相对 main 的实际 diff 缺失声明文件（改了未声明文件却漏改/漏提交声明文件，branch diff 兜底核对）。

响应结构：
```json
{
  "task_integrity": {
    "skipped": false,
    "scope": "active",
    "issues": [
      {"task_id": "task-x", "status": "claimed", "main_dirty_overlap": ["src/x.py"], "missing_declared_files": ["docs/x.md"]}
    ],
    "summary": "1 个活跃任务存在完整性告警"
  }
}
```

### 3.7 status --audit-merge 只读 merge 巡检

```
orchd status --audit-merge
```

在常规 status JSON 上附加 `merge_audit` 字段：

```json
{
  "merge_audit": {
    "skipped": false,
    "warnings": [
      {"task_id": "task-xxx", "status": "completed", "branch": "task/task-xxx", "hint": "分支未 merge 入 main"}
    ]
  }
}
```

巡检规则：
- 遍历 completed 状态的所有任务，检查对应 `task/{task_id}` 分支是否存在且其 HEAD 是否已并入 main
- 分支不存在 → 视为已并入（非告警）
- 分支存在但 HEAD 未被 main 的祖先链包含 → 输出警告
- 非 git 仓库 / git 不可用 → `skipped: true`
- 纯只读操作（git rev-parse / merge-base --is-ancestor），不执行 checkout / merge / push

**流程闭环要求**：任务 completed 后，应运行 `orchd status --audit-merge` 实证告警清零，确保 merge 实际发生。如 `pipeline-runner.md` 有独立要求则优先遵循。

### 3.8 审查循环终止

`attempt_count` 超过 `max_attempts`（默认 3）：
- request 返回 `"warning": "exceeded_max_attempts"` 标记
- 提示人介入：force-status 回 pending 并重置计数，或 amend 修改 acceptance_criteria

### 3.9 status --audit-task 只读任务完整性巡检

```
orchd status --audit-task
```

在常规 status JSON 上附加 `audit_task` 字段（task-engine-audit-coverage，2026-08-24），巡检 **completed（merged）任务**的声明文件历史缺失/残留：

```json
{
  "audit_task": {
    "skipped": false,
    "scope": "merged",
    "issues": [
      {
        "task_id": "task-xxx",
        "status": "completed",
        "main_residual": ["docs/x.md"],
        "historical_missing": ["src/x.py"]
      }
    ],
    "summary": "1 个merged任务存在完整性告警"
  }
}
```

巡检规则：
- 遍历 completed 状态的所有任务，检查对应 `files_to_edit` 声明文件：
  - `main_residual`：main 工作树仍残留该任务声明文件的未提交改动（合并后未提交的残留，无论巡检入口在 main 还是任务 worktree 都能发现）
  - `historical_missing`：任务分支 diff 缺失声明文件（历史缺失：merge 时声明文件未随分支进入 main）
- 非 git 仓库 / git 不可用 / 无 project_root → `skipped: true`，不抛异常（best-effort）
- 纯只读操作（git diff / status 类查询），不修改工作树、不建分支、不提交

**流程闭环要求**：completed 任务应运行 `orchd status --audit-task` 实证告警清零，确保声明文件全部随任务分支进入 main 且主工作树无残留（与 §3.7 audit-merge 配套）。


### 3.10 锁维度全景（task-audit-lock-residue-reclaim AC1）

Orchd 运行时存在四类独立锁文件，均落于**共享账本根**（container 布局 `<容器>/.orchd-runtime/`，或 `ORCHD_HOME` 重定向根；flat 布局回退 `.orchd/`），由 `ExclusiveFileLock` 原语基于 OS flock（内核托管，进程退出自动释放）提供互斥。各锁路径、用途、生命周期与回收策略如下：

| 锁文件 | 路径模板 | 用途 | 生命周期 | 残留/孤儿回收 |
|---|---|---|---|---|
| **账本锁** `.lock` | `<store>/.lock` | 保护 ledger append / checkpoint 读写，是所有状态机写操作的底层互斥权威 | `store.acquire_lock()` → 写操作 → `store.release_lock()`；进程崩溃内核自动释放 flock | 无孤儿问题（flock 内核托管，文件残留不影响判定） |
| **摄入准入锁** `.intake.lock` | `<store>/.intake.lock` | 串行化 `amend` / `intake` 对 `_master.json` / `IDEAS.md` / `ROADMAP.md` 的全局写入，独立于账本锁（避免一次准入阻塞并行 claim/done） | `intake_lock_acquire`（阻塞等待上限 60s + flock + 写诊断标记）→ `intake_lock_release`（引用计数归零释放 flock，**不 unlink**，文件保留） | 无 live flock 时 `intake_lock_check` 判未持有；超时残留标记（>120s）由 `intake_lock_check` 自动清除并返回 `timeout_cleaned`；flat→container 迁移后旧路径 `<main>/.orchd/.intake.lock` 由 `reclaim_orphan_intake_locks` 在 intake 路径 best-effort 回收 |
| **会话锁** `.session-{wt}.lock` | `<store>/.session-<worktree>.lock`（主 worktree 回退 `.session.lock`） | worktree 维度操作互斥：同一 worktree 同时仅一个 session 可执行 claim/done 等写操作；锁文件持 OS flock fd 作进程活性探针 | `session_lock_acquire`（flock + JSON 标记含 agent_id/session_id/nonce/branch/timestamp）→ `session_lock_release`（关闭 fd，内核释放 flock）；同 session 刷新覆盖写（幂等） | worktree 删除后锁文件成为孤儿：`reclaim_orphan_session_locks` 在 `session_lock_acquire` 时 best-effort 扫描，对 worktree 已不存在且无 live flock 的锁文件删除（主 worktree 锁不触碰） |
| **会话门锁** `.session-gate-{wt}.lock` | `<store>/.session-gate-<worktree>.lock`（主 worktree 回退 `.session.gate.lock`） | 串行化 `ensure_session_lock` 的"检查+写入"全过程，消除 check-then-act 竞态（两个并发 session 同时通过检查并覆盖写锁标记）；与会话锁标记文件分离，避免"标记被删导致门锁失效" | `ExclusiveFileLock` acquire（阻塞 10s）→ 临界区 → release；best-effort（获取失败静默降级为仅凭当前标记判定） | 同会话锁孤儿回收策略：worktree 已不存在且无 live flock 时由 `reclaim_orphan_session_locks` 一并清理 |

**路径解析规则**：所有锁路径经 `resolve_store_dir` / `_resolve_store_root` 解析到共享账本根，确保多 worktree 共享同一锁命名空间；worktree 维度后缀由 `_git_worktree_name` 解析 git linked worktree 名称得到，解析失败回退无后缀的全局文件名。

**活性判定**：新式锁（含 `flock_active` 标记）优先以非阻塞 OS flock 探活判定是否 stale；缺失标记的旧纯 JSON 锁按 timeout / no_active_task 兼容判定，不误清。

**.gitignore**：全部运行时锁形态（`.session-*.lock` / `.session.lock` / `.session.gate.lock` / `.intake.lock` / `.lock`）已在 `.gitignore` 覆盖；误提交（如 504e39c 历史教训）需从 git 索引移除并保留本地文件。
