---
guide:
  done: 4
  claimed_impl: 2
  done_submitted: 2
---
# git 纪律（引擎 best-effort 建分支/merge + done/amend 自动提交，从不 push）

> TL;DR: ① 禁手动 git 写操作（checkout/branch/reset/stash/merge/push 等），豁免 = 任务分支 git commit + 任务分支 `orchd git merge main` 精确形态（受管出口） ② 禁破坏性 git 操作 ③ 声明文件必须随任务分支提交进 diff，否则 done/review 被拒 ④ worktree 全生命周期由引擎管理，agent 零操作

> 原 .orchd/SKILL.md「git 纪律」，外置自 task-skill-hub-refactor。

- **实现者**：实现过程中可自行多次提交（细粒度保留）；未提交的 `files_to_edit` 范围内改动由引擎在 `done`（verify 通过后）自动兜底提交，不重复提交、不 squash；verify 失败不影响已产生的提交，修复后追加提交再重试
- **工作树还原受管通道（task-restore-channel）**：误写文件需干净工作区时（如 claim 被 E017 拒绝），用 `orchd restore --path <文件>` 回到 HEAD，不碰历史；仅已跟踪且有 HEAD 版本的文件，目录 / 仓库外 / 未跟踪新建一律拒绝（先验后做，一个不合规整体不执行）
- **merge 后分支自动清理（task-merge-auto-delete-branch，2026-08-15）**：code APPROVED merge 成功 / 自动化解成功后，引擎 best-effort 自动执行 `git branch -d task/{id}`（与 `_try_git_merge` 同语义）；删除失败（非零退出）静默降级，**不阻塞 completed 状态写入**；merge 冲突路径不删（任务停留 in_review 仍需分支）；merge 环境不支持（merge_result None：非 git / checkout 失败）跳过；仅删当前任务 `task/{id}`，不碰 main。agent 无需手动 `git branch -d` 清理，`status --audit-merge` 第三类告警仅作兜底（自动删除失败时仍可发现残留）
- **intake/amend**：只在 main 执行；amend 成功后引擎自动提交 `.orchd/_master.json` 与 IDEAS.md（避免脏 master 被 checkout -b 带进任务分支）；**ROADMAP.md 唯一源 = 宿主项目根（flat=仓库根，container=`<容器>/main/`）且纳入 git**——随同次 amend/intake 提交，引擎 ensure_committed 对「被 gitignore 忽略的路径」剔除、根版不被忽略故正常入库（`orchd/ledger.py::resolve_roadmap_path`）；`.orchd/ROADMAP.md` 为旧布局残留（引擎不读），不在提交范围；`orchd intake` 命令可单独提交 IDEAS（不注册任务）。引擎兜底：若误在非 main 分支执行 amend，`commit` 响应 `not_on_main` 降级为不提交（注册不受影响）；**intake-commit-enforcement（2026-08-14）**：amend 前置"非摄入产物干净"守卫（摄入产物外的已跟踪改动 → E017 阻断）；commit 失败写入 `commit_warning` 可审计；`orchd status --audit-intake` 巡检未提交摄入产物。
- **声明域补登通道（task-amend-decl-patch-channel）**：claim 后发现 files_to_edit / exempt_files 遗漏，不手改 `_master.json`——回主工作树执行 `python .orchd/__main__.py amend --task <id> --files-to-edit <f>... --exempt-files <f>... --verify-command "<cmd>"`。列表类为并集追加（只增不删，删改走 E007 矩阵）；`--verify-command` 为覆写；终态任务补登仍 E007。`--task` 不带补丁字段时拒绝执行。**brief/name 文本旗标（task-amend-text-field-patch-channel）**：`python .orchd/__main__.py amend --task <id> --brief "<对齐后的 brief>"`（`--name` 同型）是 E038 brief 口径对齐与纯文本规格修订的 pending 合法出口——单值整体覆写、空值拒绝；仅 pending 态直接放行，claimed/done/in_review 一律 E007（`_ac_field_routing_hint` 指引回 pending 或终态通道），终态配 `--revise-terminal <id> --reason "<理由>"` 走纯文本修订；两字段不进 schema `x-amendable`（否则 claimed 白名单连带放行打穿 E007 矩阵）。逃生文案（guide E022/E024/E027/E028、hook E020 echo）中的补登命令由 `guide.amend_patch_cmd` 单一生成，禁止手写（`tests/test_control.py::TestAmendPatchCmdSingleSource` 锁死同源）
- **终态规格文本修订通道（`amend --revise-terminal`，task-terminal-spec-revision-channel，2026-09-14）**：任务已进入终态（completed / cancelled）时常规 amend 一律 E007（补登声明域同样 E007），若需把规格**纯文本**对齐已裁定/已实现的现实（如 AC 表述漂移），用 `python .orchd/__main__.py amend --revise-terminal <task_id> --reason "<修订理由>"`：
  - 放行字段仅四类：`name` / `brief` / `acceptance_criteria` / `deliverables`（展示与文本字段；`acceptance_criteria` 的引擎消费者只有 E023 模糊词 / E029 条数两条 warning，`deliverables` 在 `orchd/` 内零消费者）；
  - 护栏是**结构性断言**而非字段名自觉：剥离上述文本字段后，其余变更仍须逐项落入既有通道（终态可附加字段 / 声明路径规范化），用文本字段夹带执行字段（`verify_command` / `files_to_edit` 等）一律 E007；
  - `--reason` **必填且非空白**（否则 E007），理由写入 AMEND 审计事件（`reason="terminal_spec_revision"`，复用既有事件类型，不新增事件类型、不改 `_apply_event` 语义）；
  - `--revise-terminal` 指向的 task 必须已存在于 `_master.json` 且为终态；非终态任务用本通道 → E007；
  - 与 amend 同受红线约束：只在**主工作树**（default 分支）执行，任务分支调用拒绝注册
- **claim 前提**：处于 main 且工作区干净（**"干净"= 无已跟踪文件改动；untracked 工具/配置文件不阻塞**）；引擎从当前 HEAD 建分支，上个任务未 merge 归还会导致 base 错误
- **审查者**：领取前确认处于对应 task 分支且工作区干净；审查对象是分支上的已提交 diff
- **本地提交自主执行**：任务分支上的 `git commit` 是协议动作，agent 直接执行、无需管理员确认（纪律红线豁免之一的手动 git 命令；另一豁免为任务分支 `orchd git merge main` 受管出口，见 TL;DR）；只提交协议范围内（files_to_edit；done 的自动提交面另含 exempt_files，口径见下「exempt_files 豁免」）改动，不 push
- **不 push**：远端推送不在 agent 职责内，由项目管理员负责
- **L3 pre-commit hook 生命周期**（2026-08-08 语义升级）：claim 时安装到真实仓库 `.git/hooks/pre-commit`，**任务活跃时任何分支**都校验 staged ⊆ files_to_edit ∪ exempt_files（堵住 main/幽灵分支越界提交实现内容）；任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行；`--no-verify` 可绕过。**固定资产豁免（完整枚举）**：`.orchd/_master.json`、`IDEAS.md`、`.orchd/IDEAS.md`、`ROADMAP.md`（宿主根唯一源、纳入 git）。注：`.orchd/ROADMAP.md` 已非合法形态，**不在豁免表**——提交该路径会被 E020 拦（防残留副本被当固定资产放行；双向用例见 tests/test_gitops.py::test_hook_rejects_legacy_roadmap_in_orchd 与 ::test_hook_exempts_roadmap_at_host_root）。**exempt_files 豁免（2026-08-08 新增）**：任务定义可声明 `exempt_files`（必要连带文件，如新增错误码连带更新的 `tests/test_errors.py` 断言），claim 安装期即随 hook 生效（staged 文件 ∈ exempt_files 放行）；豁免文件**同样纳入 done 的自动提交范围**（真源 `orchd/onboard/lifecycle/core.py` 的 `commit_files = files_to_edit ∪ exempt_files`，task-done-exempt-files-commit）；**扩展仅限提交范围**——`declared_diff` / `zero_residual` / `out_of_scope` 三道门禁仍以 files_to_edit 为准，越界提交仍被 E020 拦；E017 不再因 exempt_files 未提交而阻断（该改动正是为消除误伤），实现者仍宜随手提交以保持分支 diff 可审。done 执行 verify_command 前临时卸载、verify 后重装（避免 verify 期间真实仓库 git 操作被误伤），done 末尾 / retract 真正卸载。**强制层生命周期（task-ref-tx-hook，2026-09-21）**：`reference-transaction` hook 与 pre-commit 同进退——claim 期安装，retract / force-status 卸载；任务正常完成（done→review→merge）后保持生效（无任务在跑时仓库仍处强制层，属承接既有语义，需知晓）；运行时产物（`.githooks/pre-commit`、`.githooks/reference-transaction`）在 `.gitignore` 忽略，git status 不显。
- **多 worktree 并行（1.4，multi-worktree-m-p1，2026-08-22）**：仓库开多个 worktree 并行时——**任务 worktree 全生命周期由引擎自动管理**（claim 自动创建 + 绑定 `session-worktrees.json`、终态自动回收、孤儿惰性清理），agent **零 worktree 管理操作**；任务 worktree **独立 checkout 各自 `task/{id}` 分支**实现（互不干扰）；**agent 不碰 main**——merge 由引擎在**主工作树**内执行（`main_worktree_root` 定位，专用 merge-wt 已废弃删除，见 gitops_ops.try_git_merge），任务 worktree **永不 checkout main**（规避 git 单分支单 worktree checkout 硬限制）；账本（container 默认 `<容器>/.orchd-runtime/`，可 `ORCHD_HOME` 重定向；flat 维持 `.orchd/` 零回归）**全局共享**，各 worktree 的 agent 状态一致，并发写由**统一排他文件锁原语（ExclusiveFileLock）**兜底——存储层 `.lock` 基于 flock（内核托管，进程退出自动释放），append/checkpoint 写原子化 + E011 任务级忙度锁（agent 一次一任务）；并发 merge 以主工作树锁串行；依赖链保持完成级串行（E008）；单 worktree（默认 flat）不建独立任务 worktree，行为与以往完全一致（零回归）

## 账本同步（orchd sync，task-ledger-git-sync）

账本（`_ledger.jsonl` / `_checkpoint.json`）为本地运行时状态，不入 git。跨设备迁移后任务状态会丢失——**显式 `orchd sync`** 把账本进度经 git 共享，引擎热路径零改动。

**承载方式（可持续性红线：git 体积不随事件总数膨胀）**：

- 专用账本 ref `refs/heads/orchd/ledger`（本地 + 远端同名），**单提交**、`--force-with-lease` 推送——旧对象由 `git gc` 回收（机制描述，非执行指令），git 体积收敛到「当前内容」而非「全部历史」；
- ref tree 只含两个文件，尺寸均有界：
  - `state.json` — 紧凑任务状态表，**O(任务数)**：每任务 `status / review_phase / claimed_by / claimed_session / review_claimed_at / attempt_count / updated_event_id`；
  - `delta.jsonl` — 自上次 `sync` 以来未归档事件尾，**有界于同步间隔**，`--compact` 归并入 state 后清空；
- 本地 `_ledger.jsonl` 保留完整审计（本地增长属既有设计，归档/截断留待后续迭代）。

**命令用法**：

```bash
python .orchd/__main__.py sync --push      # 拉取远端增量合并 + 推送本地增量（pull-first）
python .orchd/__main__.py sync --pull      # 拉取远端 delta 合并进本地账本并重建 checkpoint（幂等）
python .orchd/__main__.py sync --compact   # delta 归并入 state.json 并清空（git 体积收敛）
python .orchd/__main__.py sync --status    # 只读查看远端 ref 进度摘要（不改本地）
python .orchd/__main__.py sync --remote <name>  # 指定远端名（默认 origin）
```

**同步语义**：

- **pull-first + event_id 去重合并**：pull/compact 先 fetch 远端 ref，远端 `delta.jsonl` 中本地缺失事件按 `(timestamp)` 确定性稳定排序合并进本地账本，再重建 checkpoint；重复 pull/sync 幂等；
- **并发仲裁**：两机对同一任务并发写入时，以合并后事件的确定性全局序为准（时间戳稳定排序 + event_id 去重），不丢他端事件；push 用 `--force-with-lease` CAS——远端被并行推进则拒绝，pull-first 重试兜底；
- **首次 push 保守全量**：本地 marker（`.ledger_sync_marker.json`）缺失时全量推送，push 成功后记录末位事件 id，后续 push 仅推增量；
- **gc 说明**：`orchd sync` 本身不触发 `git gc`；体积收敛依赖 git 自动回收旧对象（默认 expiration 策略），不改变远端 hook 行为。**禁止手动执行 `git gc --prune=now`**（红线：禁破坏性 git，无豁免）。

**触发纪律**：sync 为**显式**命令，agent 或人在需要跨设备共享进度时手动触发；不进入引擎热路径（claim/done/review 等不隐式调用）。

## 持任务 amend 补登（task-amend-guidance-mainwt）

持任务（claimed 状态）期间发现 `files_to_edit` / `exempt_files` 遗漏或 `verify_command` 需调整时，**禁止手改 `_master.json`**，走以下三步流程：

### 三步流程

1. **定位主工作树**：任务 worktree 内无 `_master.json`，amend 必须回主工作树执行。
   - 方法 A：`git worktree list` — 带 `(main)` 标记的路径即主工作树；
   - 方法 B：从任务 worktree 路径上溯到项目根下的 `main/` 目录（container 布局）；
   - 方法 C：引擎 guidance 已自动注入 `main_worktree` 字段（E010 / scope_warning / amend 分支拒绝均带此字段）。

2. **执行 amend 补登**：在主工作树根目录执行（命令由 `guide.amend_mainwt_command` 单一生成，禁止手写）：

   ```bash
   cd "<主工作树绝对路径>"; python .orchd/__main__.py amend --task <task_id> --files-to-edit <file1> <file2> ...
   ```

   - `--files-to-edit` / `--exempt-files`：并集追加（只增不删，删改走 E007 矩阵）；
   - `--verify-command "<cmd>"`：覆写 verify_command（白名单内不阻断）；
   - `--reviewers <id...>` / `--files-to-read <path...>`：整体覆写（空值即清空；
     files_to_read 条目按 reference 登记；task-spec-hygiene-flat-sweep 起）；
   - `--task` 需至少携带一个补丁字段，否则 E007 拒绝；
   - 终态任务（completed/cancelled）补登仍 E007 拒绝。

   **AC 字段编辑窗口（task-amend-ac-pending-channel）**：`acceptance_criteria`
   整体覆写的合法通道是本主工作树命令（与 `--reviewers` / `--files-to-read`
   一致的整体替换语义）：

   ```bash
   python .orchd/__main__.py amend --task <id> --acceptance-criteria "<ac>" ...
   ```

   - 空值拒绝而非清空（AC 必填下限 1 条）；超过 6 条 E029 拒绝注册；
     模糊词不阻断但 `validate` 照常报 E023；
   - 状态分流：仅 pending 态放行；claimed / done / in_review 改 AC 一律 E007
     （附回 pending / 终态通道指引）；终态（completed/cancelled）仅
     `amend --revise-terminal <id> --reason` 且条数不变（AC 增删属范围变更，
     另走用户裁决逃生口）。
   - 与 `:17`（声明域补登“不手改 `_master.json`”）和 `:64`（持任务 amend 补登
     “禁止手改 `_master.json`”）的关系：三处禁的是同一件事——任何字段（含 AC）
     都不得绕过引擎直接改 `_master.json` 再跑裸 `amend`；pending 改 AC 也不例外，
     一律走上面这条 `--acceptance-criteria` 旗标通道，由引擎一条龙写盘提交。
     本段即全部手续，无需再跨段拼凑。

3. **回到任务 worktree 继续**：amend 成功后引擎自动提交 `_master.json`，任务 worktree 下次 claim / done 时自动读取最新声明。无需手动同步或重建 worktree。

### 与 guidance 的互链

- 触发点 1（done E010 越界改动）：`_guard_out_of_scope` 抛 E010 时，details 含 `main_worktree` 字段，hint 含 `amend_mainwt_command` 生成的可执行命令；
- 触发点 2（claim 预览 scope_warning）：`build_scope_warning` 返回含 `main_worktree` 字段，hint 含可执行命令 + verify_command 白名单说明；
- 触发点 3（任务分支 amend 被拒）：`_cmd_amend` 分支守卫抛 E007 时，details 含 `main_worktree` 字段，hint 含定位方法 + 可执行命令；
- 所有命令字符串来自 `guide.amend_patch_cmd` / `guide.amend_mainwt_command`，测试 `tests/test_guide.py` 锁死同源，禁止手写漂移。

## driver 触发面与中和边界（task-git-proxy-driver-residual-boundary）

仓库所有者配的 `filter.*.clean|smudge` 与 `diff.*.textconv` 是**外部命令**，git 在物化文件内容
时默认执行它们。代理对这条面只做了一件能做的事，其余是**登记**而非处置。口径分三层：

1. **只读通道（已中和）**：`orchd git diff|log|show|blame|…` 在执行层注入 `--no-textconv
   --no-ext-diff`。白名单逐子命令实测建立，判据条件与全部排除族见
   `orchd/gitops/proxy.py` 的 `_DRIVER_NEUTRALIZE_SUBCOMMANDS` 注释；证据由 CI 复现，不留
   scratch 文件引用——`tests/test_git_proxy.py::TestDriverNeutralization`（四形经代理零标记 +
   直连正对照）与 `::TestReadFormDriverSurface`（43 条 read 形态逐条裸跑不 spawn + 注入 rc
   分档 129 / 假接受 / 挤位失败）。
   两个旗标**缺一不可**：仓库配了 `diff.external` 时，**裸** `git diff HEAD~1` 就会执行那个
   外部命令（argv 里没有任何危险旗标），而 external 路径反过来**不**调 textconv——单注入
   `--no-textconv` 只会把外部命令留在原地（实测标记集合 `{EXT, F}` ↔ `{F, TC}`，
   `::TestDeniedFlagExecutionSurface::test_ext_diff_runs_the_configured_external_command`）。
   被拒旗标自身的执行面证据（`--text` / `--ext-diff` / `--filters` / grep 的 `-O` 与前缀缩写
   与短旗标簇 / `--output` / commit 正文来源 → `core.editor`）在同一条 class，注释里的
   `::用例名` 锚不得悬空——由 `::TestCommentCiAnchorsResolve` 校验。
2. **merge 通道（刻意不中和，代理 == 直连）**：`orchd git merge main`（红线 #1 豁免二）必然
   spawn `filter` driver，且两条中和路径实测都不可用——否定旗标注入到 merge 上 **rc=129**
   `error: unknown option`（合并根本不执行）；`-c filter.mk.smudge=` 或
   `GIT_CONFIG_COUNT` 置空则 **rc=0 但静默改掉合并出来的字节**（恒等型 smudge 看不出来，
   变换型 `touch M && tr a-z A-Z` 下直连得大写、置空得原文）。故本通道不追求「更干净」，只
   锁「与直连逐字节一致」：`tests/test_git_proxy.py::TestMergeDriverBoundary`（ff / no-ff ×
   恒等 / 变换 四组合的 rc + 合并文件 sha256 + 标记集合全等，外加两条反例把「不采用中和」的
   依据本身钉住）。风险承接前提：agent 本就拥有该仓库，driver 由仓库所有者配置，与引擎
   内部自动执行的 `git add` / `git checkout` 同语义（引擎动作，非 agent 处方）；且
   **agent 经代理写不到 driver 配置**——`config <key> <value>` / `--global` / `--unset` 三种写
   形态在分类层一律判写；其中两条本地形态经代理真敲一次，实测在**执行前**即 E007 拒且
   `.git/config` 一字未改（`--global` 只在纯函数层验，避免用例失手时脏宿主机全局配置），
   只有 `--list` / `--get` 放行
   （`::TestMergeDriverBoundary::test_driver_config_is_not_agent_writable_via_proxy`）。
   即 agent 能触发**已配好**的 driver，不能自己装一个。
3. **引擎内部触发面（登记，不处置）**：下表是引擎自动执行的 git 动作里会 spawn driver 的站点。
   它们**不是 agent 可达面**——表内登记的**站点形态**（`add -- <paths>` / `checkout <branch>` /
   `worktree add …` / 引擎侧 `merge <branch>`）在分类层一律判写，代理在执行前直接拒。
   注意口径是「站点形态」而非「动词整体」：同一动词可能有可达的兄弟形态，例如
   `worktree list` 判 read 并放行——它不物化文件，实测裸跑不落任何 driver 标记（列在
   `::TestReadFormDriverSurface` 的 43 形态表内），所以它不属于本表的触发面。可达性判据由
   `tests/test_git_proxy.py::TestEngineInternalDriverSurfaceRegistered` 校验（表内 argv 片段必须
   在源码中逐字存在，且「登记不处置」行的**站点形态**仍必须判写；agent 若能经新通道调用它们，
   本测试红 ⇒ 该站点升级为需处置项，另开任务）。

| 站点 | git 动作 | argv 片段（判据锚，须在源码中逐字存在） | 实测 spawn | 处置 |
|---|---|---|---|---|
| `orchd/gitops/commit.py` | add | `["add", "--", *paths]` | `clean` | 登记不处置 |
| `orchd/gitops/guard.py` | checkout | `["git", "checkout", branch_name]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops/guard.py` | checkout | `["git", "checkout", "HEAD", "--", *ok]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops/guard.py` | checkout | `["git", "checkout", default]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops_ops.py` | checkout | `["git", "checkout", branch]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops_ops.py` | checkout | `["git", "checkout", "-b", branch, default]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops_ops.py` | checkout | `["git", "-C", str(workdir), "checkout", trunk]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops_ops.py` | checkout | `_git("checkout", trunk)` | `smudge`+`clean` | 登记不处置 |
| `orchd/worktree/lifecycle.py` | worktree | `["git", "worktree", "add", "-b", branch, str(wt_path)]` | `smudge`（落在**新工作树根**，不在源仓库） | 登记不处置 |
| `orchd/gitops_ops.py` | merge | `["git", "-C", str(workdir), "merge", branch]` | `smudge`+`clean` | 登记不处置 |
| `orchd/gitops/proxy.py` | merge | `cls["kind"] == "merge"` | `smudge`+`clean` | 已锁代理==直连 |

> 夹具纪律（写新用例前必读）：配 driver 的 scratch 仓库，smudge 必须写成
> `touch MARKER && cat`。裸 `touch` 吃掉 stdin 会把重写出来的文件截成 0 字节，于是任何
> 「字节相同 / 中和无害」比较的基线本身已被 driver 破坏——反证见
> `test_truncating_smudge_is_a_real_fixture_trap`。
