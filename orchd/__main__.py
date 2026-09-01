"""``python -m orchd`` 入口：与 ``orchd`` 脚本一致，退出码透传 main()。"""
import sys

from orchd.cli import main

if __name__ == "__main__":
    sys.exit(main())
