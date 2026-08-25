"""跨平台 shell 命令执行（verify_command 等 POSIX 语法命令在 Windows 上的兼容层）。

verify_command 以 POSIX shell 语法书写（${TMPDIR:-/tmp}、ORCHD_SESSION_ID= 前缀、
test -f、bash -n、>/dev/null、| grep），Windows 的 cmd.exe（subprocess shell=True）
不识别这些构式 → done E014。本模块在 Windows 上优先用 Git Bash 执行命令串，
POSIX 平台保持 shell=True 原行为。
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


def find_bash() -> str | None:
    """定位 Windows 上的 Git Bash；POSIX 平台返回 None（用 shell=True 即可）。"""
    if os.name != "nt":
        return None
    for cand in _GIT_BASH_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    p = shutil.which("bash")
    if p and "system32" not in p.lower():
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
