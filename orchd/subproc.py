"""跨平台 shell 命令执行（verify_command 等 POSIX 语法命令在 Windows 上的兼容层）。

verify_command 以 POSIX shell 语法书写（${TMPDIR:-/tmp}、ORCHD_SESSION_ID= 前缀、
test -f、bash -n、>/dev/null、| grep），Windows 的 cmd.exe（subprocess shell=True）
不识别这些构式 → done E014。本模块在 Windows 上优先用 Git Bash 执行命令串，
POSIX 平台保持 shell=True 原行为。

与 guide 层零根入口的分工：guide.py 下发的 command 已收敛为零根入口
``python .orchd/__main__.py``（纯 Python 进程，不依赖 shell 语法，PowerShell/
cmd/Git Bash 下均可直接执行，无需本模块兜底）；本模块只负责引擎侧 verify_command
这类 POSIX 语法命令串的 Git Bash 兜底，两层职责不同、互不干扰。
"""
from __future__ import annotations

import os
import shutil
import subprocess

# Git Bash 常见安装路径（优先于 PATH 中的 bash——后者可能命中 WSL bash，
# 路径语义不同且依赖 WSL 环境，实测 bash -n 报 rc=127）
_GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
)


def _is_git_for_windows_bash(bash_path: str) -> bool:
    """校验 PATH 中的 bash 是否来自 Git for Windows（排除 WSL 等其它发行版）。

    依据：Git for Windows 的 bash.exe 与 git.exe 同装——bash 所在目录
    （Git\\bin）或 Git 根下的 bin/ 目录存在 git.exe。WSL 的 bash
    （System32\\bash.exe）与独立 MSYS 均不满足该同现条件，无需再依赖
    脆弱的 "system32" 字符串排除。
    """
    d = os.path.dirname(bash_path)
    if not d:
        return False
    if os.path.isfile(os.path.join(d, "git.exe")):
        return True
    # Git\usr\bin\bash.exe → Git 根 = dirname(dirname(d))，检查 <root>\bin\git.exe
    git_root = os.path.dirname(os.path.dirname(d))
    return os.path.isfile(os.path.join(git_root, "bin", "git.exe"))


def find_bash() -> str | None:
    """定位 Windows 上的 Git Bash；POSIX 平台返回 None（用 shell=True 即可）。"""
    if os.name != "nt":
        return None
    for cand in _GIT_BASH_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    p = shutil.which("bash")
    if p and _is_git_for_windows_bash(p):
        return p
    return None


def run_shell(cmd: str, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    """跨平台执行 shell 命令串：Windows 有 Git Bash → bash -c；否则 shell=True。

    返回 CompletedProcess（capture_output=True，bytes stdout/stderr），
    与调用方现有 _decode_subprocess_output / _verify_output_summary 契约一致。
    """
    if os.name == "nt":
        bash = find_bash()
        if bash:
            return subprocess.run(
                [bash, "-c", cmd], cwd=cwd, capture_output=True, timeout=timeout
            )
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=timeout)
