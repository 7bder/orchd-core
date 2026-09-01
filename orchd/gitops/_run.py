"""git 命令执行底层（整块迁移，无逻辑改动）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchd.gitops._const import _GIT_ENCODING, _GIT_ERRORS, _GIT_TIMEOUT


def _run_git(
    project_root: Path,
    args: list[str],
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """以 UTF-8 解码运行 git 命令（cwd 限定 project_root）。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        encoding=_GIT_ENCODING,
        errors=_GIT_ERRORS,
        timeout=timeout,
    )


def _shell_quote(value: str) -> str:
    """shell 单引号转义：' 替换为 '\''（单引号闭合-转义-重开）。

    用于把文件名安全嵌入 shell 脚本字面量，防 shell 注入
    （hook 模板中文件名来自任务定义，属可信输入，但仍按防御性处理）。
    """
    return "'" + value.replace("'", "'\\''") + "'"
