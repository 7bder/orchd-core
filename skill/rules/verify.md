# 自检约定（verify_command）

> TL;DR: ① verify_command 120s 内完成（引擎硬上限）② **定向优先**：verify_command 只跑 files_to_edit 映射的定向测试（验收分级·定向档）③ **全量门禁**：全量 pytest 禁入任何 verify_command，只留在发版门禁点（验收分级·门禁档）④ 用 --basetemp 指向系统临时目录 ⑤ orchd 命令统一 python .orchd/__main__.py 形式。两档定义与选用规则见 [decomposition-guide §5.5 验收分级](../../docs/decomposition-guide.md)。

> 原 .orchd/SKILL.md「自检约定（verify_command）」，外置自 task-skill-hub-refactor。2026-09-11（task-test-tiering-policy）按验收分级规范重构为「定向优先 + 全量门禁」两档框架。

- **⏱ 120s 预算硬约束（2026-08-08 新增）**：引擎 verify 上限 `_VERIFY_TIMEOUT=120s`（定义于 `orchd/onboard/lifecycle/core.py`（done 链路）/ `orchd/onboard/lifecycle/control.py`），**verify_command 必须在 120s 内完成**——写 verify_command 时先预算：模块定向 pytest（只跑相关文件，秒级）+ 轻量断言；**禁止** `python -m build` / `pip install` / `venv` / 全量 pytest（无 -k/-p 定向）等重命令段（重活留给 CI，不在 verify 跑）。2026-08-08 实踩两例：task-auto-claim 全量 pytest 210s 超时、task-release-pipeline build+venv 段 144.7s 超时 → done E014 卡死
- **⏱ 预算例外通道（默认 120s **不变**，2026-09-17 新增 task-verify-timeout-amend-channel）**：`python .orchd/__main__.py amend --task <id> --verify-timeout-seconds <N>`——N 为正整数 **1..600**（非法值 E007 且**不落盘**；`--verify-command` 可同一次调用组合；claimed / done / in_review 状态均可 patch，白名单单一事实源为 `orchd/split.py::_AMEND_ATTACHABLE_FIELDS`）。
  - **为什么会需要**：verify 经 `orchd/subproc.py::run_shell` 执行时，**MSYS 祖先链**会放大
    其下 MSYS 子进程的创建成本（机制口径见本文件门禁档「成本实测 → 修法」，易错点是把它记成
    「Git Bash 慢」）。涉及 git hook / POSIX 外部工具的用例尤其明显。本轮实测（task-verify-timeout-amend-channel）：`tests/test_gitops.py` 全 81 用例 **Git Bash 142.2s vs PowerShell ~13s**、单例 0.8s → 10~11s；更早 task-roadmap-contract-guards-realign 实测 ~13x，其 verify 首跑 131.3s 触发 E014。此类 verify_command 打爆默认预算后，此前唯一办法是**人为收窄验证面**（= 牺牲覆盖度，慢用例改由全量门禁兜）。
  - **2026-09-18 起的大幅缓解（task-subproc-native-fastpath）**：`run_shell` 已按命令形态选
    执行器——不含 POSIX-only 构造的命令走**原生 cmd 快速通道**（祖先链被切断，与前台执行同量级）；
    只有命中 POSIX-only 构造的命令（含 `grep` / `test` / `rm` 等外部工具、分号、单引号、命令替换）
    才回落 Git Bash，此时仍会踩到上述放大 ⇒ 打爆预算依旧走本通道。
  - **使用纪律**：① 优先按「定向档」精简 verify_command（能 `-k` 选子集就不整体跑）；② 只有在「必须保留慢用例」时才提高预算，且**必须在任务 notes / 交付说明写明实测耗时与理由**（引用上列数据）；③ 上界 600s 为防呆硬顶（>10 分钟会让 done 在单点阻塞过久），不提供「无限预算」；④ 提高预算 ≠ 放宽「全量 pytest 禁入 verify_command」——全量仍只留在门禁档三触发点。
- **定向档（日常验收，verify_command 唯一写法）**：verify_command 只跑 files_to_edit 映射的定向测试文件——`orchd/x.py` 对应 `tests/test_x.py` + 显式列出的 tests/ 文件：`python -m pytest tests/test_<涉及模块>.py [相关测试文件] -q --basetemp="${TMPDIR:-/tmp}/orchd-vf-$$" && python .orchd/__main__.py validate .orchd/_master.json`。历史教训（2026-08-06）：全量 `pytest tests/` 约 30s 且随测试膨胀线性恶化，累计 210s 超出引擎 verify 超时上限 → done 必卡死
- **门禁档（全量守门，禁入 verify_command）**：全量 pytest 不写入任何任务的 verify_command，也不写进验收标准（不写"全量 pytest 通过"）。触发点三个：
  ① 发版前 `python .orchd/__main__.py full-regression` 手动触发（task-full-regression-gate-r2，2026-08-28）：跑全量 pytest 并通过后写 `.orchd/_full_regression.json`（`last_pass_commit` + `passed_at`，本地状态不入 git）；`scripts/sync_orchd_core.sh` 发版前检查 `last_pass_commit` 是否覆盖当前 HEAD 的 `orchd/*.py` 改动——**未覆盖 / 记录缺失 / 记录无效一律非零退出阻断发版**（task-release-gate-blocking，2026-09-19：此前只打印 WARNING 后照常同步，等于发版门禁名义化；可用 `--check-coverage-only` 单独校验）；
  ② `done` 默认**不**自动跑全量（`_master.json` `config.full_regression_on_done` 缺省/显式 false → 跳过，响应无 `full_regression` 字段）；显式 true 才恢复 done 后全量冒烟（失败仅附加 warning，不阻断 done、不改任务状态）；
  ③ **push 前门禁**（task-prepush-full-suite-gate，2026-09-17；task-prepush-tag-gate，2026-09-19）：`.githooks/pre-push` 在推送 `refs/heads/*` **或 tag（`refs/tags/*`，发版）** 时跑全量 pytest（`tests/ -n auto`）+ **基线差分**，只拦「相对基线新增的失败」——存量红不阻塞，避免门禁一上线即不可用。该条件的断言**单一真源** = `tests/conftest.py` 的 `assert_prepush_full_suite_gate`（task-prepush-gate-assertion-drift，2026-09-19：此前 hook + 两个测试文件三处硬编码同一条件，条件一扩展即漏改一处）。
     - 基线文件：`.orchd/test_baseline.json`（`.orchd/*` 已被 gitignore，属本地状态，不入库）；
     - 刷新时机：在全绿提交上 `python scripts/check_test_baseline.py --record .orchd/test_baseline.json -- tests/ -n auto`；只跑只看用 `--diff`（退出码 1 = 有新增失败，0 = 无新增，已修好的只报 info）；
     - 基线缺失时门禁退化为「要求全量全绿」（不静默放行）；
     - 被拒时的三步修复入口与 `git push --no-verify` 紧急绕过由 hook 自身打印（绕过须在任务 notes 留痕）；
     - 背景实踩：verify 定向档管不到「全仓不新增红」，本地唯一门禁曾只有 ruff+mypy → 9 条确定性红一路入库、唯一发现点是 CI（当时本地已 ahead 190 提交）。
     - **成本实测（2026-09-18，task-prepush-gate-cost-measurement，MSYS 根）**：`core.hooksPath`
       = `c:/VibeCoding/orchestra/main/.githooks`，hook 首行 `#!/bin/sh` ⇒ git 用 **MSYS sh**
       拉起门禁，故 **MSYS 根**是门禁的默认通道。**放大源不是「由谁拉起」，而是 MSYS
       祖先链**——祖先链上的 MSYS 进程会让其下 MSYS 子进程的**创建**走慢路径（综述见本节
       末尾「修法」；易错口径纠正：把原因记成「hook 由 MSYS 拉起」会导向「换个拉起方式即可」的
       无效改法，实测只有**切断祖先链**才回落）。实测方法：
       **后台启动 + 轮询**读日志（前台单命令会被上层超时中止，本诊断实踩 3 次），计时脚本与
       原始日志外置系统临时目录；顺序是先单独计时 ruff / mypy（hook 前两段，秒级），再计时
       整条 hook 链，扣除即 pytest 段——同一批次只跑一次全量。两通道同 worktree、同 PATH、
       同脚本，**仅根进程通道不同**（基线 `.orchd/test_baseline.json` 不在任务 worktree ⇒
       走「基线缺失 ⇒ 要求全量全绿」分支，同 CI 新克隆形态）：

       | 测量项 | MSYS 根（Git Bash 5.3.15 MINGW64 → sh） | 原生根（PowerShell 5.1 → python.exe） | 倍数 |
       |---|---|---|---|
       | pytest 全量 `tests/ -n auto` | **310.77s**，15 failed / 2524 passed / 3 skipped，rc=1 | **237.55s**，2539 passed / 3 skipped，rc=0 | 1.31x（**结论相反**） |
       | 整条 pre-push 全链 | **312.9s**（ruff 0.44s + mypy 3.5s + pytest 310.8s），rc=1 拒推 | 242.3s（三段合计），rc=0 | 1.29x |
       | 上述 15 条失败用例复跑（`-n0`） | **180.9s**，2 failed / 13 passed，rc=1 | **68.5s**，15 passed，rc=0 | **2.64x（结论相反）** |

       - **假红比慢更严重**：基线 `.orchd/test_baseline.json`（commit `38f4fbb`）为 **2507
         passed / 0 failed** 全绿，同 HEAD `3a04df0` 在 MSYS 根却报 15 条失败
         （2524 passed + 15 failed = 2539，与原生计数**逐项吻合**），基线缺失分支要求全绿
         ⇒ **rc=1 拒推**。其中 13 条属 xdist 并发 flaky（`-n0` 同序复跑通过）；**2 条在
         MSYS 根下稳定失败**（`-n0` 仍红且同为 `E017 dirty_workspace`）——`tests/test_review.py::
         TestMergeMainWorktree::test_flat_review_branch_deleted_regression`、
         `tests/test_e2e.py::TestNewProjectInitSmoke::test_fresh_init_claim_done`：MSYS 祖先链下
         引擎兜底提交未生效，残留改动被 done 前置守卫判脏。**这是可用性缺陷，不是性能缺陷**。
       - **机制旁证**（本机实测链，同任务前序诊断）：同一真实 hook 脚本，原生根 224~320ms vs
         经 bash 12.7~16.5s（**50~70x**）；`tests/test_gitops.py -k TestPreCommitHook` 直接
         20~22s vs 经 bash 155.7s（**7x**）；仅插入一层 cmd 屏障切断 MSYS 祖先链即回落
         **238ms** —— 根因是 MSYS 祖先链下的子进程创建成本，与脚本内容无关。
       - **是否可接受：不可接受，须待 `task-subproc-native-fastpath` 落地后才可用**。
         可接受阈值 = 「单次 push 附加等待 ≤ 60s」（日常 git 操作体感上限）；
         实测全链 **312.9s ≈ 5.2 分钟 ⇒ 超出阈值 5.2 倍**，
         且叠加 2 条稳定假红 ⇒ 会把正常提交判不合格而拒推。
       - **过渡做法**：需 push 时用 `git push --no-verify` 绕过（**须在任务 notes 留痕**），或在
         原生根手工跑 `python -m pytest tests/ -n auto` 自检。如遇 verify 预算问题仍走任务级
         `verify_timeout_seconds` 通道（见上方「预算例外通道」）；10 分钟级实测**禁止**写入
         verify_command（本任务 verify 仅内容断言 + validate）。
       - **修法（task-subproc-native-fastpath，2026-09-18 落地）**：三组实测指向同一机制 ⇒
         修法是**切断祖先链**，不是「优化脚本」或「换个拉起方式」：
         ① `orchd/subproc.py::plan_execution` 在 Windows 上按命令形态选执行器——不含
         POSIX-only 构造的命令改走**原生 `cmd /d /c` 快速通道**，只对 `${TMPDIR:-/tmp}` /
         `/dev/null` / `$$` 三类白名单 token 做有限可审计翻译（→ 系统临时目录 / `NUL` /
         PID+序号），表外构造（反引号 / `$(...)` / `$VAR` / `;` / 单引号 / 反斜杠 / POSIX
         外部工具如 grep·test·rm）一律**回落 bash**，语义零放宽、书写契约仍为 POSIX；
         ② `.githooks/pre-push` 全量段插**原生屏障**（MSYS 下 `NATIVE_BARRIER="cmd //c"`，
         非 MSYS 为空串 ⇒ 行为零变化），使 pytest 子树不再有 MSYS 祖先。
         支撑实测：同一 hook 脚本 原生根 224~320ms vs MSYS 祖先下 12.7~16.5s（**50~70x**），
         插入 cmd 屏障后回落 **238ms**；hook 密集 pytest 文件 7x（`tests/test_gitops.py`
         直接 20~22s vs 经 bash 155.7s）。
       - **判定更新（复测回填）**：本条最初结论为「不可接受，须待修复任务落地」。修法就位后，
         **同一机器、同一命令、仅 hook 多一层屏障**的独立复测跑了两轮：
         ① 280.3s / 2590 passed / 1 failed（单跑复现为绿的偶发项）；
         ② **275.4s / 2591 passed / 0 failed / rc=0**。修前那 15 条失败——含 2 条 `-n0` 下
         稳定的 `E017 dirty_workspace` 通道性假红——**全部消失**，端到端耗时
         312.9s → 275.4s（**-12%**）。结论改为「**修法已就位且可用**」，
         `git push --no-verify` 不再作为常规逃生口。
       - **收益归因（防误读）**：端到端改善主要来自**屏障把 pytest 子树移出 MSYS 祖先链**；
         `run_shell` 自身的快速通道收益随负载形态而异——子进程创建密集负载
         cmd 1879ms vs bash 2643ms（**1.41x**），pytest 型负载两者基本持平（0.97x，
         耗时主体是 pytest 本身）。**勿**把 50~70x 当通用加速倍数：那一数据来自
         「根进程本身就是 MSYS」的 hook 脚本场景，而非任意命令。
- **共享入口改动的验证面（task-shared-entry-verify-gate / task-shared-entry-gate-coverage-hardening，2026-09-19；**E039** 阻断）**：改「被多个既有测试文件断言」的共享入口（`.githooks/pre-push`、`orchd/cli/__init__.py`、**本规则文件自身**等，单一真源 = `orchd/shared_entries.py::SHARED_ENTRY_TESTS`）时，verify_command **必须包含该入口登记的既有测试文件**——只跑本任务新写的测试会让既有断言静默变红入库（2026-09-19 一夜三次同源事故：prepush-tag-gate / cli-json-envelope / canonical-root-dedup）。判定发生在 `done` 的 verify **之前**（缺口静态可判，先判先省一次昂贵 verify）：有缺口即 **E039** 阻断，缺口清单在 `details[0].gaps`；**逃生** `amend --task <id> --verify-command "<补入登记测试后的命令>"` 后重试（命令文本由引擎引导统一生成，勿手写命令串）。判定输入 = **分支已提交 diff ∪ 工作树未提交改动**——引擎 auto-commit 在 done 靠后阶段，故**「还没提交所以不算改过」不成立**；**「跑全量」只在执行语义未被削减时才算覆盖**（`--collect-only`/`--co` 零条执行，`-m`/`-k`/`--ignore`/`--deselect`/`--lf`/`-x`/`--setup-plan`/`-o` 等裁剪集合——一律**不算**，回落逐字面匹配；判定见 `orchd/shared_entries.py::verify_command_covers_all_tests` 的白名单口径，pass6 Q-1）；登记表新增条目属引擎代码改动，须走任务管线（含审查）。已知边界与**形态 A（全局共享文件）**：影响面无法枚举的全局共享文件（`tests/conftest.py` 类，单一真源 = `orchd/shared_entries.py::GLOBAL_SHARED_FILES`）不适用登记表形态，改用弱约束——改这类文件时，verify_command 的 pytest 目标中**至少要有一个「基线既有」测试文件**（判据 `git ls-tree --name-only <基线 ref> -- <path>`，基线 ref 按 `origin/HEAD` → `main` → `master` 解析，即该文件在本任务动手前就存在于基线分支；全不可解析时记 E030 降级痕而非静默跳过——pass6 Q-2；「修改既有测试文件」同样计入，排除的只有本任务**新建**文件）。理由：全量 pytest 禁入 verify_command（120s 上限），「改 conftest 就跑全量」在本预算下不可实现，故退一步保证既有断言至少被真实执行一次；代价是**不保证**该测试真覆盖被改动行为（弱约束的固有边界）。同一 E039 阻断，缺口性质由 `details[0].rule` 区分（`registry` / `global_shared_file`）。
- 触及高风险区域（状态机分支、CLI 契约、锁协议）追加第三环：`&& python .orchd/__main__.py status`
- **基线差分（验收"无新增失败"的工具，不靠人肉）**：`scripts/check_test_baseline.py` 落盘失败清单并与基线比对——`--record BASE.json [-- PYTEST_ARGS]` 落盘（测试挂也不影响退出码），`--diff BASE.json [-- 同样参数]` 仅新增失败时非零退出（基线已修好的失败只提示）。门禁点与返工验收用它代替"全量重跑三遍"：基线在全绿 commit 上 record，之后每次只看 diff 是否新增
- **文档 / 基础设施类**（files_to_edit 不含 .py/orchd/ 代码）：文件存在/内容断言（`python -c "..."`），**不跑 pytest 全量**；必须非空
- `orchd` 命令统一用 `python .orchd/__main__.py` 形式（bash PATH 无 orchd，避免 E014）
- **cmd 兼容**：verify_command 用纯 `cmd1 && cmd2` 链，**禁止** `;` 分隔与嵌套 `python -c "..."` 引号（JSON→cmd→shell 三层转义易失效 → SyntaxError，2026-08-08 实踩 task-release-pipeline）

- **verify_command 危险构式判定（保守口径 + 引号状态机，2026-09-17）**：引擎对 verify_command 做危险构式静态扫描，判定口径为**保守拦截**——宁可误拦合法命令，不可放过可执行注入：
  - **双引号内出现 `$(…)` / 反引号 / 危险命令词即拦**：即便该构式不构成实际执行（如出现在注释或字符串字面量中），只要双引号包裹范围内出现命令替换符（`$()`、`` ` ``）或危险命令词（`rm`、`curl`、`wget`、`eval`、`exec` 等），即判定为危险并拒绝执行。
  - **引号按状态机判定，不做正则剥除**（task-verify-danger-quote-state-machine）：只有**不在双引号内**的 `'…'` 才是 shell 字面量、可豁免并整段剥离；**双引号内的 `'` 不是定界符**（bash 语义下只是普通字符，其间 `$(…)` / 反引号照旧展开），一律保留参与判定。实测教训：正则剥除法 `re.sub(r"'[^']*'", "", cmd)` 把 `echo "'$(curl … | sh)'"` 整段当字面量剥掉后放行，而 bash 实跑会执行替换（`danger=[]` 放行 + 实跑执行，已实证绕过）；该形态与负控制固定在 `tests/test_spec_command_danger.py`（`nested_*` 用例 + `test_negative_control_regex_stripping_would_miss`）。
  - **不做转义还原，未闭合单引号不豁免**：扫描不解释转义序列（双引号内 `\"` 只用于避免误判闭合，不改变判定方向），未闭合的单引号不会被当作「安全字符串」豁免，其范围内的危险构式同样拦截。
  - **合法但被拦时的处置**：若 verify_command 本身合法但被保守口径误拦，**应改写 verify_command**（如将含 `$(...)` 的路径计算改为提前计算后传入、将危险词拆分为非触发形式、去掉嵌套引号写法），而非放宽判定规则或申请白名单豁免。判定逻辑是全局安全基线，不因单任务需要而降级。
