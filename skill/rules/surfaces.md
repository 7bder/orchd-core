# 共享面（shared surfaces）规则

> 适用：所有会改动「多任务共享的单文件」的任务。共享面的登记与操作由引擎统一收口，
> 不再把 `CHANGELOG.md` 等文件写进 `files_to_edit`。设计背景见 `docs/shared-surfaces.md`。

## 1. 判定：什么算共享面

运行 `python .orchd/__main__.py surfaces list` 查看注册表（mode / path / required / 来源）。
注册表 = 引擎默认（`orchd/surfaces.py`）+ 宿主覆盖（`.orchd/shared/surfaces.json`）；
旧 `guards.docs.changelog` 仅在显式配置时参与兼容（与宿主 overlay 冲突视为配置错误 E007）。

三种 mode：

| mode | 含义 | 任务侧动作 |
|---|---|---|
| `fragments` | 主文件按片段汇总（默认 `CHANGELOG.md` ← `changelog.d/`） | `done` 时交一条片段，禁止直接编辑主文件 |
| `generated` | 生成物（如 `file-census-baseline.json`、`docs/_generated/dependency-graph.md`） | 不手改、禁止写进声明；确需刷新用 `surface_refresh` 受控通道（§2.1） |
| `serial` | 必须串行的文件（`conventions.md` / `user-manual.md` / `SKILL.md`） | 加 `depends_on` 串行；确需并行才走逃生口 |

## 2. 硬规则：禁止把共享面写进 files_to_edit / exempt_files

- 新任务（`register` / `amend` 新增声明）出现共享面路径：
  - `fragments`：硬拒，错误给出面路径、mode 与 `done --surface-entry` 用法；
  - `generated`：硬拒，且**没有逃生口**（生成物只能由生成器改写）；
  - `serial`：默认只 warning 并列出建议 `depends_on`；确需并行的显式声明
    `--allow-shared-write <path> --reason "<并行理由>"`（无 reason 拒绝）。
- `fragments` 的 `--allow-shared-write` 视为串行例外，同样必须带 reason。
- 准入同时扫描 `files_to_edit` 与 `exempt_files` 两个声明域；放行参数中的路径同样做相对路径归一化
  （`..` 折叠；绝对路径与盘符路径直接拒绝）。
- `amend --remove-files-to-edit` 永远放行：旧卡迁移靠它，不会被自身旧声明卡住。

### 2.1 generated 面的受控刷新（唯一例外）

generated 面仍禁止写进声明；任务确实需要刷新生成物（如 `file-census-baseline.json`、
`docs/_generated/dependency-graph.md`）时，在实现期用 `surface_refresh` 把刷新意图交给引擎：

```bash
# 声明（仅 pending/claimed 任务；reason 单行、非空、≤200 字符）
python .orchd/__main__.py amend --task <id> --refresh-surface <面名> \
  --reason "<为何刷新：生成器、上游变化、复核范围>"
# 撤回（幂等；同一面不能同时声明与撤回）
python .orchd/__main__.py amend --task <id> --remove-refresh-surface <面名>
```

- 面必须存在、`mode=generated` 且已登记 generator；引擎把 `{surface,path,generator,reason}`
  写入任务的 `surface_refresh` 字段（一项一面，按面名排序）。
- path/generator 与注册表漂移、reason 空/多行/超长、与 `files_to_edit`/`exempt_files` 重复
  → E007 `surface_refresh_blocked`（reason 码 + 上下文，不落盘）。
- CLI 重复声明同一面 = 幂等覆盖（保留最新 reason）；只有 `_master.json`/register JSON 里
  已存的 `surface_refresh` 数组内出现同面重复，才会被 `normalize_refresh_entries` 拒绝。
- 它是**写集豁免**而非替任务执行生成器：实现期仍要自己跑生成器命令（`surfaces list` 可查）；
  `done` 自动提交与 pre-commit hook 动态作用域都会展开 `surface_refresh[].path`，越界守卫与
  revive 门禁同源放行。
- 刷新只覆盖本任务声明的那一个面/路径，不改变注册表中的 mode，也不触发自动汇总。

## 3. 写 CHANGELOG：done 交片段

```bash
# 推荐糖（默认 category=changed；summary 缺省回退 --changes 第一行）
python .orchd/__main__.py done --task <id> --changes "..." \
  --changelog-category fixed --changelog-summary "修复 done 预检的片段识别"

# 通用入口（第二个 fragments 面也用这个）
python .orchd/__main__.py done --task <id> --changes "..." \
  --surface-entry '{"surface":"changelog","category":"fixed","summary":"..."}'
```

- 合法分类（默认 changelog 面）：`added` / `changed` / `deprecated` / `removed` / `fixed` / `security`。
- 片段落盘路径：`<dir>/<task_id>.<category>.md`（默认 `changelog.d/<task_id>.<category>.md`，宿主可改 `dir`）；
  内容由引擎渲染为单行 `- <task_id>：<summary>`，`summary` 不得含中文分号 `；`、渲染后整行 ≤150 字符。
- 一个任务在同一个 fragments 面只能有一条片段：重复交同内容幂等（`created=false`），
  换分类或换内容直接 E007；不要手写、手改、手删片段文件。
- 片段在 `_done_precheck` 之前写入，因此落地预检（E039）能看见本任务片段；
  done 失败会回滚本次新建的片段（同一分支上已有片段保留）。自托管仓库（引擎仓库自身）的
  pre-commit scope 不认片段路径时，唯一逃生口是在主工作树执行
  `amend --task <id> --exempt-files <片段路径> --allow-shared-write <片段路径> --reason "<自托管豁免理由>"`
  把该片段登记为动态作用域豁免（审计会留 `surface_declaration_override` warning）；不得手改 hook、
  不得 `--no-verify` 绕门。

## 4. 汇总与发行

```bash
python .orchd/__main__.py surfaces check [name] [--require-assembled]   # 只读
python .orchd/__main__.py surfaces assemble [name] [--dry-run] [--keep-fragments]
```

- `check` 校验注册表、片段格式、重复片段与 generated/serial 状态；`--require-assembled`
  存在未汇总片段即非零退出并给出 `surfaces assemble` 提示。
- `assemble` 只在默认分支、干净工作树、共享面未被在途任务声明时执行：先全部规划再写，
  写失败按字节回滚；只在缺失位置插入条目（按 categories 顺序、同分类按 task_id 排序），
  保留原 EOL/BOM，成功后删除已汇总片段；`--dry-run` 不写盘，`--keep-fragments` 保留片段。
- 发版：`orchd release run` 在 full-regression 之前有只读 `surface-check`
  （`surfaces check --require-assembled`），未汇总片段会硬失败并停在该步；宿主没有
  `scripts/` 时该步由引擎完成，`release-ready` 视脚本缺失为 `not_applicable`。

## 5. 旧卡迁移与复活

- 迁移只针对 `pending` 卡（`claimed` / `in_review` / `completed` 不动）：
  `python .orchd/__main__.py amend --task <id> --remove-files-to-edit CHANGELOG.md`
  （可多次，一次一个路径；删除后不得留下空 `files_to_edit`）。
- 复活（`force-status` 从终态回 `pending`）会按新规则重检声明：仍声明 fragments / generated 面
  直接拒绝（E007 `revive_blocked`），并给出按字段分流的精确迁移命令：
  `amend --task <id> --remove-files-to-edit <path> --reason "<迁移理由>"`
  （或声明在 `exempt_files` 时用 `--remove-exempt-files`）。终态卡的声明裁剪由引擎显式通道完成
  （仅允许移除已注册的 fragments/generated 面路径，其它声明变更仍按终态矩阵拒绝），
  不得静默改写声明；无 git 的宿主保持 fail-open。
- 任务 worktree 内执行 `force-status` 也会解析到 canonical 主工作树的 master 做同一套检查；
  解析到主工作树但 master 缺失视为错误（E007 `revive_master_unavailable`），不回退为放行。

## 6. 违规后果

| 场景 | 引擎行为 |
|---|---|
| 新任务声明 fragments / generated 面 | register / amend 硬拒 E007（带面路径、mode、正确用法） |
| 任务凭 `surface_refresh` 刷新生成物 | done 自动提交该生成物；`in_review`/`completed` 声明 E007 |
| `surface_refresh` 与注册表漂移 / reason 空 / 与写集重复 | E007 `surface_refresh_blocked`（不落盘） |
| 声明 serial 面且无 `--allow-shared-write` | warning + 建议 `depends_on` |
| 落地任务未在共享面留痕（文本或片段） | done 预检 E039 `landing_precheck` |
| 分支上有未声明路径（含手写片段） | pre-commit scope 拦截 E020 |
| 发版时仍有未汇总片段 | release `surface-check` 失败并停在该步 |

## 7. 相关文档

- 设计：`docs/shared-surfaces.md`（模式判定准则、§7 单点扫描契约、§8 汇总算法；generated 受控刷新见 §10.4）。
- 命令：`docs/user-manual.md` §3.1 / §3.5（`surfaces`）与 `python .orchd/__main__.py surfaces --help`。
- 宿主覆盖：`.orchd/shared/surfaces.json`（`schema_version: 1`，升级保留）。
