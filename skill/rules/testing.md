---
guide:
  claim_review: 2
  rework_first: 3
  request_impl: 3
  done: 2
  claimed_impl: 4
---
# 测试纪律（防复制式测试）

> TL;DR: ① 复用 tests/conftest.py 的 make_task / orchd_dir ② 参数化，禁止在测试文件内另造副本 ③ 一个测试文件一个域，新域新建文件不塞泛用文件

> 原 .orchd/SKILL.md「测试纪律」，外置自 task-skill-hub-refactor。

- **必须复用 `tests/conftest.py` 共享 fixture/helper**：`orchd_dir` / `store` / `make_task` 等已在 `tests/conftest.py` 收敛，新增测试一律从 conftest 导入复用；**禁止在测试文件内另造副本**（复制式回归将由 `scripts/check_test_dedup.py` 硬检查拦截，已接入 CI）
- **同族测试变体必须参数化**：同一逻辑的不同取值用 `@pytest.mark.parametrize` 展开，**禁止复制函数**制造 N 份近似用例（复制式测试是测试数量线性膨胀的主因）
- 新增测试前先确认所需 fixture/helper 是否已存在于 `tests/conftest.py`（`make_task` / `store` / `orchd_dir`），存在即复用，不得重写

## 分片纪律：「一个测试文件一个域」（2026-09-12）

> 与 `git rerere.enabled=true`（已启用）配套：rerere 负责**自动复用同型冲突解法**，本纪律负责**降低冲突面**。

- **文件名即域锚点**：每个测试文件对应**单一域**（一个模块 / 一个职责，如 `tests/test_pool.py` 只放任务池域）。
- **新域新建文件**：新增用例所属域尚无对应测试文件时，**新建** `tests/test_<domain>.py`；**禁止**把跨域用例塞进泛用收纳文件（典型反例 `tests/test_cli_misc.py`）——泛用文件会同时成为多任务修改热点。
- **迁移就近归位**：文件拆分 / 包拆分 / 删除迁移测试时，用例只搬进**同域**测试文件（`cli/` 拆包 → `tests/test_cli_<域>.py`）；**禁止**「集中收纳」到同一个文件。
- **同域内聚合**：同一域的不同取值走参数化，不为变体新开文件。
- **理由（实测）**：2026-09-11 `task-test-cli-split-domains` 一次 modify/delete 冲突使两轮审查全部作废（13:59 → 22:03，**4h15m / 1 个文件**）。根因即「测试迁移跨域集中收纳」在并发下反复制造同型冲突。

## per-op 外部进程纪律（2026-09-22，task-suite-slow-guard）

> 反例：`reference-transaction` 强制层 hook 被 claim 装进每个测试仓库后，
> git commit 93ms→1728ms、git branch 783ms，全量涨回 ~10min——而既有
> `tests/test_perf_budget.py` 只数子进程**次数**，对**单价**上涨免疫，无门禁可拦。

- **新增 git hook / 命令包装器 / 解释器启动必须附 before/after 计数实测**：
  计数 = 新增的外部进程调用次数（单次操作 × 触发面），不是墙钟——墙钟跨机抖动，
  禁止用墙钟硬断言（否则制造新的 flaky 源）。实测附在提交信息或
  `docs/perf-call-census.md`。
- **同步更新 `tests/test_perf_budget.py` 的计数上限**：上限只收紧（次数下降才合入），
  不放宽；放宽须另立任务说明理由。
- **默认不进测试临时仓库**：生产安全机制（hook / 强制拦截）在测试会话默认跳过安装，
  专门用例 opt-in（见 `tests/conftest.py` 会话补丁注释与 `tests/test_ref_tx_hook.py`
  的恢复夹具）。判定标准：代表性流程跑完后，测试仓库内该机制制品计数为 0。

## 热点测试文件禁止尾部追加（2026-09-13，task-test-hotspot-sharding）

> 与「一个测试文件一个域」配套：前者管**新建时的域归属**，本条管**已有热点文件的修改方式**。

- **禁止向既有热点测试文件尾部追加新测试**：`tests/test_pool.py` / `tests/test_worktree.py` / `tests/test_guide.py` / `tests/test_review.py` / `tests/test_done.py` 等被多任务共享的热点文件，**不得**作为新测试的追加点。
- **新域/新职责必须新建独立测试文件**：新增测试所属域若尚无对应文件，新建 `tests/test_<domain>.py`；即便同域已有热点文件，新增**独立子域**（如 `pool` 下的 `decl_dir_match`）也应新建 `tests/test_<domain>_<subdomain>.py`，避免 hotspot 尾部堆叠。
- **理由（实证）**：2026-09-13 `task-decl-dir-match-conflict` 与 `task-inflight-conflict-visibility` 并行时**同时**向 `tests/test_pool.py` / `tests/test_worktree.py` 尾部追加测试 → 必然 modify/modify 冲突。根因：同一热点文件被多个并发异步任务共享作为追加点，引擎无法干预 append 冲突。约定层根治：降低冲突面，不新增引擎校验。
- **例外**：修复既有测试文件中的 bug / 调整既有测试用例（非新增类/方法）不受此限；新增测试类必须新建文件。
