# 字段表（生成物，勿手改）

- 真源唯一 = `schema/_master.schema.json`；可达 property 60 处（properties / $defs / items 递归）
- `x-amendable: true` = amend 声明域白名单（`orchd/schema_policy.py::amendable_fields` 同源）；`x-terminal-attachable: true` = 终态可附加字段集
- 重新生成：`python scripts/gen_contract_docs.py --write docs/_generated`

| 字段路径 | 类型 | required | enum | default | 描述 | x-amendable | x-terminal-attachable |
|---|---|---|---|---|---|---|---|
| `schema_version` | enum | required | [1] | - | Schema 版本号。演进时递增，orchd validate 据此判断兼容性。当前仅 v1；新增版本须同步落地 schema/v{N}/ 目录。 | - | - |
| `project` | object | required | - | - | - | - | - |
| `project.name` | string | required | - | - | 项目名称。 | - | - |
| `project.brief` | string | required | - | - | 一句话项目描述。 | - | - |
| `project.review_mode` | enum | optional | ["unified","two_phase"] | - | 审查模式（review-unify-r2）：unified=单阶段一次 APPROVED 即 merge；two_phase=两阶段（spec→code）。缺省 two_phase（观察期兼容）。 | - | - |
| `project.default_line` | string | optional | - | - | 默认线名（可选，须为 lines 的键；缺省取 lines 首个键）。多主干 line 规划，见 design/line-split-design-20260923.md。 | - | - |
| `project.lines` | object | optional | - | - | 多主干（line）登记：line 名 → {trunk}。缺省 = 单线（仅 default，trunk=main）。trunk 拓扑属契约「明确不冻结」项（docs/kernel-contract.md §3）。 | - | - |
| `modules` | array | required | - | - | - | - | - |
| `modules[].id` | string | required | - | - | 模块 ID，格式 mod-{name}。 | - | - |
| `modules[].name` | string | required | - | - | 模块显示名称。 | - | - |
| `modules[].role` | string | required | - | - | 模块在整个项目中的定位。 | - | - |
| `modules[].estimated_hours` | number | optional | - | 0 | 模块预估总工时。 | - | - |
| `tasks` | array | required | - | - | - | - | - |
| `tasks[].id` | string | required | - | - | 任务 ID，格式 task-{name}。 | false | false |
| `tasks[].name` | string | required | - | - | 任务名称。 | false | false |
| `tasks[].brief` | string | required | - | - | 任务一句话描述。 | false | false |
| `tasks[].module` | string | required | - | - | 所属模块 ID，引用 modules[].id。 | false | false |
| `tasks[].phase` | string | optional | - | - | 任务所属执行阶段（阶段串行工作流用，非必填）。与评审阶段正交，也与 module / line 正交；缺省视为中立，不参与阶段推进判定。 | false | false |
| `tasks[].depends_on` | array | optional | - | [] | 上游任务 ID 列表，引用 tasks[].id。 | false | false |
| `tasks[].estimated_hours` | number | optional | - | 1 | 预估工时。 | false | false |
| `tasks[].importance` | enum | optional | ["critical","high","normal","low"] | - | 显式重要性。缺省时由 orchd 基于下游阻塞数自动推导。 | false | false |
| `tasks[].difficulty` | enum | optional | ["low","medium","high"] | - | 任务难度。 | false | false |
| `tasks[].requires` | array | optional | - | [] | 执行该任务所需的能力标签。 | false | false |
| `tasks[].acceptance_criteria` | array | required | - | - | 验收标准列表，spec review 逐条对照。 | false | false |
| `tasks[].files_to_read` | array | optional | - | [] | - | true | false |
| `tasks[].files_to_read[].path` | string | required | - | - | 相对 workspace 根目录的文件路径。 | - | - |
| `tasks[].files_to_read[].priority` | enum | required | ["must_read","reference"] | - | must_read：必须读；reference：按需参考。 | - | - |
| `tasks[].files_to_read[].hint` | string | optional | - | - | 一行摘要，帮助 agent 决定读取深度。 | - | - |
| `tasks[].files_to_edit` | array | required | - | - | 本任务将修改的文件列表，用于文件冲突检测。 | true | false |
| `tasks[].reviewers` | array | optional | - | - | 审查者 agent ID 列表。task-fp-schema-reviewers 起不再必填；新的身份模型由会话指纹 + 防自审判定，审查者不再预写（存量任务已清理，字段仅为向后兼容保留）。 | true | true |
| `tasks[].require_independent_review` | boolean | optional | - | false | 已废弃/忽略（deprecated、ignored）：保留该键仅为兼容旧 _master.json 的 schema 校验，引擎行为与本值无关——审查恒需独立会话（实现指纹 == 审查指纹时 claim 与 submit 一律 E016），request 也恒排除自审候选；任何取值都不会放宽该门禁（设 false 不放行、设 true 无额外作用）。不要用本字段表达审查策略，详见 .orchd/rules/review.md「审查者身份约定」。不触碰 required 字段与既有枚举语义。 | false | false |
| `tasks[].verify_command` | string | optional | - | - | 可选。done 时由 CLI 执行的客观验证命令，exit 0 才允许写 DONE。 | true | true |
| `tasks[].max_attempts` | integer | optional | - | 3 | 审查打回最大次数，超过后 request 返回警告。 | false | false |
| `tasks[].deliverables` | object | optional | - | - | 交付契约（可选）。定义任务的对外形状，供 spec review 逐项核对与 verify_command 测试锚点。 | false | false |
| `tasks[].deliverables.type` | enum | required | ["code_api","data_format"] | - | - | - | - |
| `tasks[].exempt_files` | array | optional | - | [] | 必要连带豁免文件列表（可选）。声明任务实现必需但不在 files_to_edit 内的文件（如新增错误码连带更新的测试断言文件），E020 hook 校验豁免，引擎 ensure_committed 不兜底提交（须实现者自行 commit）。 | true | true |
| `tasks[].surface_refresh` | array | optional | - | [] | generated 面受控刷新声明（可选）：条目 {surface,path,generator,reason}，path/generator 由共享面注册表解析，仅 mode=generated 可声明；引擎在 pre-commit 动态写集与 done 的 commit_files 中展开 path，使生成物刷新可审计地随任务提交（不进入 files_to_edit/exempt_files 写集）。 | true | false |
| `tasks[].surface_refresh[].surface` | string | required | - | - | - | - | - |
| `tasks[].surface_refresh[].path` | string | required | - | - | - | - | - |
| `tasks[].surface_refresh[].generator` | string | optional | - | - | - | - | - |
| `tasks[].surface_refresh[].reason` | string | required | - | - | - | - | - |
| `tasks[].source` | ["string","null"] | optional | - | - | 任务来源（可选）。格式 ^(idea\|roadmap\|debug):[a-z0-9-]+$——idea: 前缀引用 IDEAS.md 中 status 为 pending 的条目 id；roadmap: 前缀引用 ROADMAP.md 中 ## 版本章节头包含的规划 id；debug: 前缀标记外部来源手工注册的任务（无文件引用校验）。缺省无 source 时校验通过（向后兼容存量任务）。 | false | false |
| `tasks[].verify_timeout_seconds` | integer | optional | - | - | verify_command 执行超时（秒，可选）。缺省回退引擎默认 _VERIFY_TIMEOUT=120。全量 pytest 慢机器/沙箱拦截场景可调大（如 300）。 | true | true |
| `tasks[].additional_sources` | array | optional | - | [] | 附加来源列表（可选，task-idea-multi-source-attribution）。一个任务承接多个 idea 时，除主 source 外的次级引用列于此。格式与 source 相同，E025 按与 source 相同口径逐条校验；ideas-archive 归档匹配同时覆盖 source 与 additional_sources（任一命中即随任务完结归档）。加法式：不动既有单值 source 语义。 | true | true |
| `tasks[].backport` | object | optional | - | - | 跨线回移元数据（可选，task-line-sync）：记录该任务由 line-sync 从源线提交回移而来。source_sha 为完整 commit sha（git rev-parse 归一）；source_line 为源线名。与 line-drill.json 的 backport 字段同构（审计/演练留痕）。 | false | false |
| `tasks[].backport.source_sha` | string | required | - | - | 源提交完整 sha。 | - | - |
| `tasks[].backport.source_line` | string | required | - | - | 源线名（project.lines 的键）。 | - | - |
| `shared` | object | optional | - | - | 共享上下文文件路径。可选：缺省时 CLI claim 跳过共享上下文自动附加（不报错），适用于无架构/规范文档的简单项目。 | - | - |
| `shared.architecture` | string | optional | - | - | 架构文档相对路径（BOOTSTRAP 写入 .orchd/shared/architecture.md）。 | - | - |
| `shared.conventions` | string | optional | - | - | 编码规范相对路径（BOOTSTRAP 写入 .orchd/shared/conventions.md）。 | - | - |
| `config` | object | optional | - | - | 引擎行为配置（1.1）：importance 阈值覆盖等 | - | - |
| `config.importance` | object | optional | - | - | derive_importance 阈值下界覆盖 | - | - |
| `config.importance.critical` | integer | optional | - | - | - | - | - |
| `config.importance.high` | integer | optional | - | - | - | - | - |
| `config.importance.normal` | integer | optional | - | - | - | - | - |
| `config.doc_single_stage_blocked` | array | optional | - | - | 文档单阶段审查 blocked 文件集合（可选）。含这些文件的任务保持双阶段审查。缺省回退硬编码 {SKILL.md, .orchd/shared/conventions.md, .orchd/_master.json}。 | - | - |
| `config.conflict_policy` | string | optional | - | - | 在途冲突策略（可选，缺省 warn）。warn=「在途」任务（分支有未落 main 的提交：done / in_review / force-status 悬空态）与候选文件重叠时仅提示 + 降权排序，不阻断（缺省，保持流程通畅）；serialize=在途重叠候选被硬排除（响应含 blocked_by / 重叠文件 / 对方状态）；block=同 serialize 且需人工确认。故意不设 enum：非法值需可达以便引擎回退 warn 并在 degraded_guards 留痕（不得静默），而非在 schema 层直接拒绝。 | - | - |
| `config.allow_auto_claim` | boolean | optional | - | false | 是否允许无人值守自动认领（--auto-claim）。默认 false：未显式授权时 request --auto-claim 被拒绝（reason auto_claim_disabled）。仅当用户明确在 _master.json 配置为 true 时，agent 才可调用 --auto-claim 连续领任务。 | - | - |
| `config.guidance_stderr` | boolean | optional | - | true | 是否在 stderr 打印 guidance 人类可读提示块。默认 true：命令 JSON 响应后附带"下一步"提示块（不污染 stdout 纯 JSON 契约）。设为 false 时引擎跳过提示块打印，stdout 仍保持纯 JSON。 | - | - |
| `config.enforce_self_review_block` | boolean | optional | - | false | 已废弃/忽略（deprecated、ignored）：保留该键仅为兼容旧 config 的 schema 校验，引擎不再读取该值——自审恒被 E016 阻断（claim 与 submit 双侧），无 config 开关、无降级档；设为 false 不会放行自审，设为 true 也无额外作用。审查独立性由会话 lineage 硬门保证（kind=reviewer + parent_session_id=该任务最后一次 DONE 的会话 id）。 | - | - |
