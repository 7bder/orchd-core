---
# request_impl 理由：引擎改动触碰 §9.1 停服边界时，领实现任务先确认
guide:
  request_impl: 4
---
# 安全边界（详见 docs/self-hosting-design-merged.md §9）

> TL;DR: ① 改引擎触碰 §9.1 停服边界先确认

> 原 .orchd/SKILL.md「安全边界」，外置自 task-skill-hub-refactor。

- 禁止走自托管的"停服升级"（人工执行）：schema required 字段与既有枚举语义、事件格式与 `_apply_event` 语义、spec.py 校验逻辑主干
- 任何把内容域假设写死进引擎的改动同样归入停服边界
- 高风险但可走管线的改动（状态机分支 / CLI 契约 / 锁协议）：三连自检 + files_to_read 含相关设计文档章节 + 审查者对照本规范逐条核对

## 清理面与证据面三条边界（task-review-retention-and-draft-bundle-judgment）

> TL;DR: ① 证据必须内联进提交产物，不得寄存在可被整体清扫的目录 ② 可再生产物不入
> 门禁判据 ③ 判据不得写死命名形态假设

- **决定性证据必须内联进提交产物**。审查报告正文落在 `review/`，而该目录整条被
  `.gitignore` 忽略（实测 main@6536418：846 KiB / 27 文件 / `git ls-files` 零命中），
  等于**账本里几十条结论的唯一可追溯正文寄存在一个既不入库、又随时可被整体清空的目录**。
  本 session 已两次实踩：`review/` 下的 `.log` 证据活不过别人一次清理。⇒ 结论：任何要
  被后人复核的数字/堆栈/命令输出，必须写进**提交里的**报告正文或用例注释，不能只留在
  `review/` 或系统临时目录的 scratch 里（`C:/Temp` 同样会被清扫）。`review/` 正文的
  入库归属**已裁定：不入库**（用户，2026-10-07）：甲案（反忽略 `*.md`）与
  乙案（迁 `docs/reviews/`）都不取，`review/` 维持整目录被 `.gitignore` 忽略。裁定不
  等于零工作量——正因正文不入库、目录随时可被整体清扫，才更要求把决定性证据内联进提交
  产物。该现状由 `tests/test_hygiene_gate_wiring.py` 的双断言钉住
  （`git ls-files review/` 恒零命中 + `.gitignore` 的 `/review/` 规则仍在），防有人
  反忽略或删规则致报告静默入库。
- **可再生产物不入门禁判据**（与 `_ROOT_JUNK` / `_CACHE_DIRS` 既有口径同源，此处补
  一条本轮实测否决）：曾拟给 `review/` 加「不得存在 `.pyc` / `__pycache__`」判据，实测
  否决——main 下 `.pyc` 共 844 个（`tests/__pycache__` 653 等），`git ls-files '*.pyc'`
  恒 0，全部由 `.gitignore` 的 `*.py[cod]` 规则忽略；断言其不存在会让门禁随每次 import
  反复回红，属不可持续判据。判据只收**本仓流程产生且不会自愈**的失守形态（注册后未
  清理的草稿、执行后未删除的一次性脚本）。
- **判据不得写死命名形态假设**（上一红线「内容域假设写死进引擎」的同类失效）。实测
  案例一：`.orchd/proposals/` 的过程产物判据原写作 glob `_*.py`，隐含「一次性脚本必带
  前导下划线」；同目录实际存在 `build-proposal.py`（其自身 docstring 就写「一次性注册
  脚本……执行后删除」，且对应任务确已注册完成）⇒ 恒漏判。改判「后缀非 `.json` 即过程
  产物」（超集）并挂到既有受管清理档，才不依赖命名习惯。**凡按形态枚举的判据，都要
  反问一句：不符合该形态的真物去哪了**——它不会报错，只会静默判绿。
  案例二（同一任务第 1 轮 spec review 打回的自伤，比案例一更隐蔽）：把 `_*.py` 换成
  「后缀非 `.json`」时，草案列写的是 `glob("*.json")`、产物列写的是
  `suffix != ".json"`——前者在大小写不敏感文件系统上连 `.JSON` 一起命中，后者恒敏感，
  于是同一份 `Draft.JSON` **被两门各点一次且理由互斥**（既说它是「已注册未清理的草案」，
  又说它是「非草案的过程产物」），后果还不对称：内容判据认得出它是草案，挡不住产物判据
  经 `move_to_backup` 把用户仍在用的草案搬进备份桶。同一轮探针实测到第二种溢出：
  `glob` 会返回**目录**，名为 `task-1.json` 的目录被当成陈旧草案点名，而 `shutil.move`
  对目录是整树搬移。⇒ 修法不是给某一侧补 `.lower()`，而是**两侧共用同一谓词的正反面**
  （`orchd/doctor.py::_is_draft_name`），让「草案域」与「产物域」在定义上互补。**形态
  假设不只藏在字面量里，也藏在「两个看起来等价的比较写法」之间**：同一事实的两种写法
  必然随宿主文件系统漂移，而在本机（Windows）跑绿的用例看不见 Linux CI 的那一半，反之亦然。
  目录 / 断链符号链接等**非普通文件**条目的处置通道属另一档语义：已由
  `task-proposals-dir-entry-disposition` 落地——`orchd/doctor.py::proposals_dir_entries`
  按**条目类型**分派、恒手动（不自动搬整树），卫生门单列 `[proposals-dir]` 标签；硬约束
  「非普通文件条目绝不被整树搬走」由
  `tests/test_hygiene_gate_wiring.py::TestDraftNamePredicateIsOneSource::test_directory_entry_is_pointed_out_but_never_tree_moved`
  与 `::test_broken_symlink_entry_is_pointed_out` 钉住，不停在散文里。
