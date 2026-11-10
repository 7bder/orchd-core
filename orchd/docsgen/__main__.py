"""``python -m orchd.docsgen --write <out_dir>`` 入口（生成器随引擎包发布）。"""

from __future__ import annotations

import sys

from . import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
