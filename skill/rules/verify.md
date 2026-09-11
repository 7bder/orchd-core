# 自检约定（verify_command）

> TL;DR: ① verify_command 120s 内完成（引擎硬上限）② **定向优先**：verify_command 只跑 files_to_edit 映射的定向测试（验收分级·定向档）③ **全量门禁**：全量 pytest 禁入任何 verify_command，只留在发版门禁点（验收分级·门禁档）④ 用 --basetemp 指向系统临时目录 ⑤ orchd 命令统一 python .orchd/__main__.py 形式。两档定义与选用规则见 [decomposition-guide §5.5 验收分级](../../docs/decomposition-guide.md)。

> 原 .orchd/SKILL.md「自检约定（verify_command）」，外置自 task-skill-hub-refactor。2026-09-11（task-test-tiering-policy）按验收分级规范重构为「定向优先 + 全量门禁」两档框架。

- **⏱ 120s 预算硬约束（2026-08-08 新增）**：引擎 verify 上限 `_VERIFY_TIMEOUT=120s`（onboard.py），**verify_command 必须在 120s 内完成**——写 verify_command 时先预算：模块定向 pytest（只跑相关文件，秒级）+ 轻量断言；**禁止** `python -m build` / `pip install` / `venv` / 全量 pytest（无 -k/-p 定向）等重命令段（重活留给 CI，不在 verify 跑）。2026-08-08 实踩两例：task-auto-claim 全量 pytest 210s 超时、task-release-pipeline build+venv 段 144.7s 超时 → done E014 卡死
- **定向档（日常验收，verify_command 唯一写法）**：verify_command 只跑 files_to_edit 映射的定向测试文件——`orchd/x.py` 对应 `tests/test_x.py` + 显式列出的 tests/ 文件：`python -m pytest tests/test_<涉及模块>.py [相关测试文件] -q --basetemp="${TMPDIR:-/tmp}/orchd-vf-$$" && python .orchd/__main__.py validate .orchd/_master.json`。历史教训（2026-08-06）：全量 `pytest tests/` 约 30s 且随测试膨胀线性恶化，累计 210s 超出引擎 verify 超时上限 → done 必卡死
- **门禁档（全量守门，禁入 verify_command）**：全量 pytest 不写入任何任务的 verify_command，也不写进验收标准（不写"全量 pytest 通过"）。触发点只有两个：① 发版前 `python .orchd/__main__.py full-regression` 手动触发（task-full-regression-gate-r2，2026-08-28）：跑全量 pytest 并通过后写 `.orchd/_full_regression.json`（`last_pass_commit` + `passed_at`，本地状态不入 git）；`scripts/sync_orchd_core.sh` 发版前检查 `last_pass_commit` 未覆盖当前 HEAD 的 `orchd/*.py` 改动时输出 stderr 警告（不阻断，退出码 0）② `done` 默认**不**自动跑全量（`_master.json` `config.full_regression_on_done` 缺省/显式 false → 跳过，响应无 `full_regression` 字段）；显式 true 才恢复 done 后全量冒烟（失败仅附加 warning，不阻断 done、不改任务状态）
- 触及高风险区域（状态机分支、CLI 契约、锁协议）追加第三环：`&& python .orchd/__main__.py status`
- **基线差分（验收"无新增失败"的工具，不靠人肉）**：`scripts/check_test_baseline.py` 落盘失败清单并与基线比对——`--record BASE.json [-- PYTEST_ARGS]` 落盘（测试挂也不影响退出码），`--diff BASE.json [-- 同样参数]` 仅新增失败时非零退出（基线已修好的失败只提示）。门禁点与返工验收用它代替"全量重跑三遍"：基线在全绿 commit 上 record，之后每次只看 diff 是否新增
- **文档 / 基础设施类**（files_to_edit 不含 .py/orchd/ 代码）：文件存在/内容断言（`python -c "..."`），**不跑 pytest 全量**；必须非空
- `orchd` 命令统一用 `python .orchd/__main__.py` 形式（bash PATH 无 orchd，避免 E014）
- **cmd 兼容**：verify_command 用纯 `cmd1 && cmd2` 链，**禁止** `;` 分隔与嵌套 `python -c "..."` 引号（JSON→cmd→shell 三层转义易失效 → SyntaxError，2026-08-08 实踩 task-release-pipeline）
