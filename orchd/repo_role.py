"""仓角色判定（task-host-guard-repo-scope）。

orchd 有两套运行形态，done 期门禁的**适用范围不同**：

- **引擎仓（自托管）**：仓库根就是 orchd 源码（``orchd/`` + ``tests/`` + ``scripts/``）。
  共享入口登记表（E039）、drift closure、生成物新鲜度这些「引擎自研门禁」在
  引擎仓内才有完整判定输入（登记测试、生成器、design 文档都在仓内）；
- **宿主仓**：引擎由安装器装在 ``.orchd/orchd/``，根目录没有 ``orchd/``，也没有
  ``tests/`` / ``scripts/``。上述门禁的登记测试与生成器**不随包**，硬套只会：

  1. E039 要求 verify_command 覆盖宿主根本不存在的测试文件（如
     ``tests/test_docs_drift_batch.py``）——宿主改 ``.orchd/rules/*.md`` 即被硬阻断，
     且补法不可执行；
  2. drift/generated 在缺 ``scripts/`` 与 design 文档时每次 done 刷 E030 降级噪音。

判据（单一真源）：根**同时**存在 ``orchd/shared_entries.py`` 与 ``tests/`` 目录 ⇒ 引擎仓。
两个条件缺一不可：只看 ``orchd/`` 会把「在根 vendor 一份引擎副本的普通工程」误认成
引擎仓；只看 ``tests/`` 更宽。宿主布局（引擎在 ``.orchd/orchd/``）恒为 False。
"""

from __future__ import annotations

from pathlib import Path

# 引擎仓的两个必要条件（相对仓库根）。
_ENGINE_REGISTRY_MODULE = Path("orchd") / "shared_entries.py"
_ENGINE_TESTS_DIR = Path("tests")


def is_engine_repo(project_root: Path | None) -> bool:
    """判断 ``project_root`` 是否为 orchd 引擎源码仓（含其任务 worktree）。

    Args:
        project_root: 仓库根；``None`` 表示无仓库上下文（测试 / 空仓），返回 False。

    Returns:
        True 仅当根同时存在 ``orchd/shared_entries.py`` 与 ``tests/`` 目录。
    """
    if project_root is None:
        return False
    root = Path(project_root)
    return (root / _ENGINE_REGISTRY_MODULE).is_file() and (root / _ENGINE_TESTS_DIR).is_dir()
