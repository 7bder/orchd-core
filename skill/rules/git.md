# git 纪律（引擎 best-effort 建分支/merge + done/amend 自动提交，从不 push）

> TL;DR: ① 禁手动 git 写操作（checkout/branch/reset/stash/merge/push 等），唯一豁免任务分支 git commit ② 禁破坏性 git 操作 ③ 声明文件必须随任务分支提交进 diff，否则 done/review 被拒 ④ worktree 全生命周期由引擎管理，agent 零操作

> 原 .orchd/SKILL.md「git 纪律」，外置自 task-skill-hub-refactor。

- **实现者**：实现过程中可自行多次提交（细粒度保留）；未提交的 `files_to_edit` 范围内改动由引擎在 `done`（verify 通过后）自动兜底提交，不重复提交、不 squash；verify 失败不影响已产生的提交，修复后追加提交再重试
- **merge 后分支自动清理（task-merge-auto-delete-branch，2026-08-15）**：code APPROVED merge 成功 / 自动化解成功后，引擎 best-effort 自动执行 `git branch -d task/{id}`（与 `_try_git_merge` 同语义）；删除失败（非零退出）静默降级，**不阻塞 completed 状态写入**；merge 冲突路径不删（任务停留 in_review 仍需分支）；merge 环境不支持（merge_result None：非 git / checkout 失败）跳过；仅删当前任务 `task/{id}`，不碰 main。agent 无需手动 `git branch -d` 清理，`status --audit-merge` 第三类告警仅作兜底（自动删除失败时仍可发现残留）
- **intake/amend**：只在 main 执行；amend 成功后引擎自动提交 `.orchd/_master.json` 与 IDEAS.md（避免脏 master 被 checkout -b 带进任务分支）；**ROADMAP.md 自 2026-08-25 起不纳入 git**（仅本地工作文档，引擎 ensure_committed 自动跳过被忽略路径）；`orchd intake` 命令可单独提交 IDEAS（不注册任务）。引擎兜底：若误在非 main 分支执行 amend，`commit` 响应 `not_on_main` 降级为不提交（注册不受影响）；**intake-commit-enforcement（2026-08-14）**：amend 前置"非摄入产物干净"守卫（摄入产物外的已跟踪改动 → E017 阻断）；commit 失败写入 `commit_warning` 可审计；`orchd status --audit-intake` 巡检未提交摄入产物。
- **claim 前提**：处于 main 且工作区干净（**"干净"= 无已跟踪文件改动；untracked 工具/配置文件不阻塞**）；引擎从当前 HEAD 建分支，上个任务未 merge 归还会导致 base 错误
- **审查者**：领取前确认处于对应 task 分支且工作区干净；审查对象是分支上的已提交 diff
- **本地提交自主执行**：任务分支上的 `git commit` 是协议动作，agent 直接执行、无需管理员确认（纪律红线唯一豁免的手动 git 命令）；只提交协议范围内（files_to_edit）改动，不 push
- **不 push**：远端推送不在 agent 职责内，由项目管理员负责
- **L3 pre-commit hook 生命周期**（2026-08-08 语义升级）：claim 时安装到真实仓库 `.git/hooks/pre-commit`，**任务活跃时任何分支**都校验 staged ⊆ files_to_edit ∪ exempt_files（堵住 main/幽灵分支越界提交实现内容）；任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行；`--no-verify` 可绕过。**固定资产豁免**：`.orchd/_master.json`、`IDEAS.md`（amend 自动提交路径，main 分支提交不被拦）。**exempt_files 豁免（2026-08-08 新增）**：任务定义可声明 `exempt_files`（必要连带文件，如新增错误码连带更新的 `tests/test_errors.py` 断言），claim 安装期即随 hook 生效（staged 文件 ∈ exempt_files 放行）；豁免文件**引擎 ensure_committed 不兜底提交**——实现者须自行 git commit，done 后 `require_clean` E017 兜底。done 执行 verify_command 前临时卸载、verify 后重装（避免 verify 期间真实仓库 git 操作被误伤），done 末尾 / retract 真正卸载
- **多 worktree 并行（1.4，multi-worktree-m-p1，2026-08-22）**：仓库开多个 worktree 并行时——**任务 worktree 全生命周期由引擎自动管理**（claim 自动创建 + 绑定 `session-worktrees.json`、终态自动回收、孤儿惰性清理），agent **零 worktree 管理操作**；任务 worktree **独立 checkout 各自 `task/{id}` 分支**实现（互不干扰）；**agent 不碰 main**——merge 由引擎在**主工作树**内执行（`main_worktree_root` 定位，专用 merge-wt 已废弃删除，见 gitops_ops.try_git_merge），任务 worktree **永不 checkout main**（规避 git 单分支单 worktree checkout 硬限制）；账本（container 默认 `<容器>/.orchd-runtime/`，可 `ORCHD_HOME` 重定向；flat 维持 `.orchd/` 零回归）**全局共享**，各 worktree 的 agent 状态一致，并发写由**统一排他文件锁原语（ExclusiveFileLock）**兜底——存储层 `.lock` 基于 flock（内核托管，进程退出自动释放），append/checkpoint 写原子化 + E011 任务级忙度锁（agent 一次一任务）；并发 merge 以主工作树锁串行；依赖链保持完成级串行（E008）；单 worktree（默认 flat）不建独立任务 worktree，行为与以往完全一致（零回归）
