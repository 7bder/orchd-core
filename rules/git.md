# git 纪律（引擎 best-effort 建分支/merge + done/amend 自动提交，从不 push）

> 原 .orchd/SKILL.md「git 纪律」，外置自 task-skill-hub-refactor。

- **实现者**：实现过程中可自行多次提交（细粒度保留）；未提交的 `files_to_edit` 范围内改动由引擎在 `done`（verify 通过后）自动兜底提交，不重复提交、不 squash；verify 失败不影响已产生的提交，修复后追加提交再重试
- **merge 后分支自动清理（task-merge-auto-delete-branch，2026-08-15）**：code APPROVED merge 成功 / 自动化解成功后，引擎 best-effort 自动执行 `git branch -d task/{id}`（与 `_try_git_merge` 同语义）；删除失败（非零退出）静默降级，**不阻塞 completed 状态写入**；merge 冲突路径不删（任务停留 in_review 仍需分支）；merge 环境不支持（merge_result None：非 git / checkout 失败）跳过；仅删当前任务 `task/{id}`，不碰 main。agent 无需手动 `git branch -d` 清理，`status --audit-merge` 第三类告警仅作兜底（自动删除失败时仍可发现残留）
- **intake/amend**：只在 main 执行；amend 成功后引擎自动提交 `.orchd/_master.json` 与 IDEAS.md、ROADMAP.md（避免脏 master 被 checkout -b 带进任务分支）；`orchd intake` 命令可单独提交 IDEAS/ROADMAP（不注册任务）。引擎兜底：若误在非 main 分支执行 amend，`commit` 响应 `not_on_main` 降级为不提交（注册不受影响）；**intake-commit-enforcement（2026-08-14）**：amend 前置"非摄入产物干净"守卫（摄入产物外的已跟踪改动 → E017 阻断）；commit 失败写入 `commit_warning` 可审计；`orchd status --audit-intake` 巡检未提交摄入产物。
- **claim 前提**：处于 main 且工作区干净（**"干净"= 无已跟踪文件改动；untracked 工具/配置文件不阻塞**）；引擎从当前 HEAD 建分支，上个任务未 merge 归还会导致 base 错误
- **审查者**：领取前确认处于对应 task 分支且工作区干净；审查对象是分支上的已提交 diff
- **本地提交自主执行**：任务分支上的 `git commit` 是协议动作，agent 直接执行、无需管理员确认（纪律红线唯一豁免的手动 git 命令）；只提交协议范围内（files_to_edit）改动，不 push
- **不 push**：远端推送不在 agent 职责内，由项目管理员负责
- **L3 pre-commit hook 生命周期**（2026-08-08 语义升级）：claim 时安装到真实仓库 `.git/hooks/pre-commit`，**任务活跃时任何分支**都校验 staged ⊆ files_to_edit ∪ exempt_files（堵住 main/幽灵分支越界提交实现内容）；任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行；`--no-verify` 可绕过。**固定资产豁免**：`.orchd/_master.json`、`IDEAS.md`（amend 自动提交路径，main 分支提交不被拦）。**exempt_files 豁免（2026-08-08 新增）**：任务定义可声明 `exempt_files`（必要连带文件，如新增错误码连带更新的 `tests/test_errors.py` 断言），claim 安装期即随 hook 生效（staged 文件 ∈ exempt_files 放行）；豁免文件**引擎 ensure_committed 不兜底提交**——实现者须自行 git commit，done 后 `require_clean` E017 兜底。done 执行 verify_command 前临时卸载、verify 后重装（避免 verify 期间真实仓库 git 操作被误伤），done 末尾 / retract 真正卸载
