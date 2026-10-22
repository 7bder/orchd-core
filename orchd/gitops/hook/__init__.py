"""gitops hook 域聚合入口（W6-C8 从单文件 `orchd/gitops/hook.py` 拆为五域）。

**本入口不持有任何实现**：只重导出五域的全部 35 个顶层符号，维持 `orchd.gitops.hook.<名>`
与 `from orchd.gitops.hook import <名>` 两条既有导入路径可解析。

域划分（域内相对导入成 DAG，`paths` / `templates` / `precommit_template` 为叶子）：

- :mod:`.paths` —— hook 路径与 git config 解析（`core.hooksPath` 读写、ref-tx 路径）
- :mod:`.templates` —— marker / shebang / 内嵌脚本片段常量
- :mod:`.precommit_template` —— 369 行的 pre-commit 模板常量单列（避免任何代码文件被它撑过棘轮）
- :mod:`.content` —— hook 脚本内容生成（pre-commit 参数烘焙、ref-tx 内容）
- :mod:`.install` —— 安装 / 卸载 / 内容校验（`hook_install` / `hook_uninstall` 入口）

旧文件因模块级 import 而暴露的纯 stdlib 绑定（`json` / `re` / `sys` / `Path` / `Any`）
有意不再提供（全仓无消费者，守卫的公共面穷举对账会强制逐名登记）；跨模块绑定
`_run_git` / `_shell_quote` / `_safe_delete` 因**确有消费者**而保留重导出。
"""

from __future__ import annotations

# 旧单文件把这三个跨模块绑定暴露在 `orchd.gitops.hook.<名>` 上，且**确有消费者**
# （`tests/test_ref_tx_hook.py` 直接 `from orchd.gitops.hook import _shell_quote`）——
# 与「纯 stdlib import 绑定」不同，这三个必须保持可解析（面收窄只针对无消费者的绑定）。
from orchd.gitops._run import _run_git, _shell_quote
from orchd.gitops.cleanup import _safe_delete
from .content import (
    _baked_hook_endpoints,
    _e020_hook_escape_block,
    _line_ref_tx_env,
    _precommit_hook_content,
    _precommit_hook_params,
    ref_tx_hook_content,
)
from .install import (
    _install_ref_tx_hook,
    _is_orchd_hook,
    _log_hook_skip,
    _uninstall_ref_tx_hook,
    _validate_hook_content,
    _write_hook_file,
    _write_precommit_hook,
    hook_install,
    hook_uninstall,
)
from .paths import (
    _HOOK_FILENAME,
    _REF_TX_HOOK_FILENAME,
    _REPO_HOOKS_DIR,
    _absolute_hooks_value,
    _ensure_hooks_path,
    _get_hook_path,
    _get_hooks_dir,
    _get_ref_tx_hook_path,
    _is_absolute_hooks_path,
    _is_same_hooks_dir,
    _read_hooks_path,
    _resolve_ledger_path,
    _write_hooks_path,
)
from .precommit_template import _PRE_COMMIT_HOOK_TEMPLATE
from .templates import (
    _HOOK_MARKER,
    _HOOK_MARKER_SCAN_LINES,
    _HOOK_PY_LEDGER_STATE,
    _HOOK_PY_RESOLVER,
    _HOOK_SHEBANG,
    _REF_TX_HOOK_MARKER,
)

__all__ = [
    "_HOOK_FILENAME",
    "_HOOK_MARKER",
    "_HOOK_MARKER_SCAN_LINES",
    "_HOOK_PY_LEDGER_STATE",
    "_HOOK_PY_RESOLVER",
    "_HOOK_SHEBANG",
    "_PRE_COMMIT_HOOK_TEMPLATE",
    "_REF_TX_HOOK_FILENAME",
    "_REF_TX_HOOK_MARKER",
    "_REPO_HOOKS_DIR",
    "_absolute_hooks_value",
    "_baked_hook_endpoints",
    "_e020_hook_escape_block",
    "_ensure_hooks_path",
    "_get_hook_path",
    "_get_hooks_dir",
    "_get_ref_tx_hook_path",
    "_install_ref_tx_hook",
    "_is_absolute_hooks_path",
    "_is_orchd_hook",
    "_is_same_hooks_dir",
    "_line_ref_tx_env",
    "_log_hook_skip",
    "_precommit_hook_content",
    "_precommit_hook_params",
    "_read_hooks_path",
    "_resolve_ledger_path",
    "_run_git",
    "_safe_delete",
    "_shell_quote",
    "_uninstall_ref_tx_hook",
    "_validate_hook_content",
    "_write_hook_file",
    "_write_hooks_path",
    "_write_precommit_hook",
    "hook_install",
    "hook_uninstall",
    "ref_tx_hook_content",
]
