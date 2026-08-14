"""零根文件启动入口（task-12-zero-root-launcher）。

宿主项目根零额外文件时，可用 ``python .orchd/__main__.py`` 运行全部 orchd 命令。
复用 ``orchd.cli.main`` 保持 CLI 契约完全一致。

sys.path 双态兼容（task-12-engine-path-abstraction 铺垫）：
- 开发态（根布局）：引擎 ``orchd/`` 在项目根 → 将项目根加入 ``sys.path``。
- 发布态（自包含 ``.orchd``）：引擎 vendored 于 ``.orchd/orchd/`` → 将
  ``.orchd/`` 加入 ``sys.path``（``orchd`` 包随 ``.orchd`` 子目录解析）。

实现：脚本所在目录（``.orchd/``）与项目根（``.orchd`` 的父目录）都尝试加入
``sys.path``，再导入 ``orchd.cli``——无论引擎在哪种布局都能命中。
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent  # .orchd/
_PROJECT_ROOT = _SCRIPT_DIR.parent  # 宿主项目根

# 双态：发布态引擎在 .orchd/orchd/（.orchd/ 入 path 即可解析 orchd 包）；
# 开发态引擎在项目根 orchd/（项目根入 path）。都加入不影响正确性。
for _p in (_SCRIPT_DIR, _PROJECT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from orchd.cli import main  # noqa: E402  (sys.path 调整后导入)

if __name__ == "__main__":
    main()
