"""``python -m orchd`` 入口：与 ``orchd`` 脚本一致，退出码透传 main()。"""
import sys

# 最低支持 Python 守卫（task-python311-floor-convergence-fix，L3）：引擎代码可能使用
# 3.11+ 语法，须在 import orchd 之前拦截，避免低版本宿主拿到深层 SyntaxError。
if sys.version_info < (3, 11):
    sys.stderr.write(
        "orchd 需要 Python >= 3.11，"
        f"当前为 {sys.version_info.major}.{sys.version_info.minor}。"
        "请升级 Python 后重试（引擎要求 >= 3.11）。\n"
    )
    sys.exit(2)

from orchd.cli import main

if __name__ == "__main__":
    sys.exit(main())
