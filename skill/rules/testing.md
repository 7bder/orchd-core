# 测试纪律（防复制式测试）

> TL;DR: ① 复用 tests/conftest.py 的 make_task / orchd_dir ② 参数化，禁止在测试文件内另造副本

> 原 .orchd/SKILL.md「测试纪律」，外置自 task-skill-hub-refactor。

- **必须复用 `tests/conftest.py` 共享 fixture/helper**：`orchd_dir` / `store` / `make_task` 等已在 `tests/conftest.py` 收敛，新增测试一律从 conftest 导入复用；**禁止在测试文件内另造副本**（复制式回归将由 `scripts/check_test_dedup.py` 硬检查拦截，已接入 CI）
- **同族测试变体必须参数化**：同一逻辑的不同取值用 `@pytest.mark.parametrize` 展开，**禁止复制函数**制造 N 份近似用例（复制式测试是测试数量线性膨胀的主因）
- 新增测试前先确认所需 fixture/helper 是否已存在于 `tests/conftest.py`（`make_task` / `store` / `orchd_dir`），存在即复用，不得重写
