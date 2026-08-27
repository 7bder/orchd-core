# 自检约定（verify_command）

> TL;DR: ① verify_command 120s 内完成（引擎硬上限）② 代码类模块定向 pytest，禁全量 ③ 用 --basetemp 指向系统临时目录 ④ orchd 命令统一 python .orchd/__main__.py 形式

> 原 .orchd/SKILL.md「自检约定（verify_command）」，外置自 task-skill-hub-refactor。

- **⏱ 120s 预算硬约束（2026-08-08 新增）**：引擎 verify 上限 `_VERIFY_TIMEOUT=120s`（onboard.py），**verify_command 必须在 120s 内完成**——写 verify_command 时先预算：模块定向 pytest（只跑相关文件，秒级）+ 轻量断言；**禁止** `python -m build` / `pip install` / `venv` / 全量 pytest（无 -k/-p 定向）等重命令段（重活留给 CI，不在 verify 跑）。2026-08-08 实踩两例：task-auto-claim 全量 pytest 210s 超时、task-release-pipeline build+venv 段 144.7s 超时 → done E014 卡死
- **代码类：模块定向，禁全量 pytest**（2026-08-06 优化：全量 `pytest tests/` 约 30s，累计 210s 超出引擎 verify 超时上限 → done 必卡死；且随测试膨胀线性恶化）：`python -m pytest tests/test_<涉及模块>.py [相关测试文件] -q --basetemp="${TMPDIR:-/tmp}/orchd-vf-$$" && python .orchd/__main__.py validate .orchd/_master.json`——定向文件 = files_to_edit 中 `orchd/x.py` 对应 `tests/test_x.py` + 显式列出的 tests/ 文件
- 触及高风险区域（状态机分支、CLI 契约、锁协议）追加第三环：`&& python .orchd/__main__.py status`
- **文档 / 基础设施类**（files_to_edit 不含 .py/orchd/ 代码）：文件存在/内容断言（`python -c "..."`），**不跑 pytest 全量**；必须非空
- `orchd` 命令统一用 `python .orchd/__main__.py` 形式（bash PATH 无 orchd，避免 E014）
- **cmd 兼容**：verify_command 用纯 `cmd1 && cmd2` 链，**禁止** `;` 分隔与嵌套 `python -c "..."` 引号（JSON→cmd→shell 三层转义易失效 → SyntaxError，2026-08-08 实踩 task-release-pipeline）
