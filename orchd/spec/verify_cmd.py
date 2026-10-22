"""orchd/spec/verify_cmd.py — verify_command 安全与跨平台判定域（W4-S3 拆分，纯搬移）。

来源：``orchd/spec/__init__.py``（起点 204aaa5a）的 L1264-1446（6 函数）+
_CROSS_PLATFORM_BASETEMP 常量块 L107-111（唯一消费者在本模块）
拆分记录见 design/file-split-program-20261015.md §4.5.3。

依赖方向：verify_cmd.py →（无 orchd 内部依赖；re 为函数级惰性导入）。
"""



# 跨平台 basetemp 模板（task-cross-platform-release / task-cross-platform-validation）
# 项目自 Windows 迁至 macOS，专用于跨平台校验提示文案。
# 执行模型（task-cross-platform-verify）：verify_command 在 Windows 上经 Git Bash
# （orchd.subproc.run_shell）执行，${TMPDIR:-/tmp} 由 bash 展开 → 真正跨平台。
_CROSS_PLATFORM_BASETEMP = '--basetemp="${TMPDIR:-/tmp}/orchd-vf-$$"'

def _runs_pytest(verify_cmd: str) -> bool:
    """判定 verify_command 是否真正执行 pytest 子进程。

    匹配 `python -m pytest` 或独立 `pytest` 命令行（非 python -c 内容断言）：
    - "python -m pytest tests/" → True
    - 'python -c "import pytest; ..."'（字符串含 pytest 但不跑 pytest）→ False
    """
    import re as _re
    # 排除 python -c "..." 内容断言（内容里含 pytest 字样不视为执行 pytest）
    if _re.search(r"python\s+-c\s+[\"']", verify_cmd):
        return False
    return bool(_re.search(r"(?:python\s+-m\s+)?pytest\b", verify_cmd))


def _verify_unsafe_reasons(verify_cmd: str) -> list[str]:
    """检测 verify_command 的不安全/不兼容段（E027，2026-08-08 实踩）。

    Returns:
        命中原因列表；无命中返回空列表。
    """
    import re as _re
    reasons: list[str] = []

    # a) cmd 不兼容分隔符：半角 ;（Windows cmd shell=True 下非命令分隔符）。
    #    排除引号内的 ;（如 python -c "a; b" 是合法 Python 语句）。
    stripped_cmd = _re.sub(r"([\"'])(.*?)\1", "", verify_cmd, flags=_re.DOTALL)
    if _re.search(r";\s*$", stripped_cmd) or _re.search(r"2>&1;", stripped_cmd) \
            or _re.search(r"(?:^|[^&|;])\s*;\s", stripped_cmd):
        reasons.append("含 cmd 不兼容分隔符 ;（Windows shell=True 下 ; 非命令分隔符）")

    # b) 重命令段：python -m build / pip install / venv / 全量 pytest 无 -k/-p 定向
    if _re.search(r"python\s+-m\s+build\b", verify_cmd) \
            or "pip install" in verify_cmd or "venv" in verify_cmd:
        reasons.append("含重命令段（build/pip install/venv），引擎 verify 120s 上限易超时")
    if _runs_pytest(verify_cmd) and not _re.search(r"-[kp]\b", verify_cmd) \
            and "tests/" in verify_cmd and not _re.search(r"tests/test_\w+\.py", verify_cmd):
        reasons.append("全量 pytest 无 -k/-p 定向，累计耗时超 120s 引擎上限")

    # c) 嵌套 python -c "..."：JSON→cmd→shell 三层转义易失效。
    #    仅匹配"内容再含引号"的多层嵌套（如 python -c "... python -c ..."），
    #    简单断言 python -c "exit(0)" 是合法用法不命中。
    m = _re.search(r"python\s+-c\s+([\"'])(.*?)\1", verify_cmd, _re.DOTALL)
    if m and _re.search(r"[\"']", m.group(2)):
        reasons.append("含嵌套 python -c 引号（JSON→cmd→shell 三层转义易失效）")

    # d) P1-4 安全加固：shell 注入构式（命令替换/管道/命令链/重定向/危险命令）
    reasons += _dangerous_shell_reasons(verify_cmd)

    return reasons


def _strip_single_quoted_segments(verify_cmd: str) -> str:
    """按 shell 引号状态机剥离**最外层**单引号字面段，返回供扫描的「活跃文本」。

    为什么必须是状态机（task-verify-danger-quote-state-machine）：单引号只有在
    **不在双引号内**时才是定界符。正则剥除法 `re.sub(r"'[^']*'", "", cmd)`
    无法表达该状态——它会把 `echo "'$(curl … | sh)'"` 里那段「双引号内、被单引号
    包着」的文本当作字面量整体剥掉，而 bash 语义下双引号内的 `'` 只是普通字符、
    其中的 `$(…)` 照旧展开 → 判定 `danger=[]` 放行，实跑却执行替换（已实证绕过）。

    状态与保守口径（沿用 .orchd/rules/verify.md 的保守拦截纪律）：
    - 不在双引号内的 `'…'`：shell 字面量，整段剥离（无替换 / 执行语义）；
    - 双引号段：原样保留（其间 `$(…)` / 反引号 / `$var` 照旧展开，剥离即漏报）；
      双引号内的 `'` 不构成定界符，按普通字符保留（故嵌套形态同样被检出）；
    - 未闭合的单引号不剥离（其内容照旧参与判定——宁可多报，不可放过）；
    - 不做转义还原、不做引号以外的语法解释（判定只服务保守拦截）。

    Args:
        verify_cmd: 原始 verify_command 串。

    Returns:
        移除最外层单引号字面段后的串，供构式扫描使用（执行点仍用原始串）。
    """
    out: list[str] = []
    i = 0
    n = len(verify_cmd)
    in_double = False
    while i < n:
        ch = verify_cmd[i]
        if in_double:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                # 双引号内的转义序列原样保留（不解释），仅避免把 \" 误判为闭合引号
                out.append(verify_cmd[i + 1])
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            close = verify_cmd.find("'", i + 1)
            if close == -1:
                # 未闭合：保守——不剥离，原样参与判定
                out.append(ch)
                i += 1
                continue
            i = close + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _dangerous_shell_reasons(verify_cmd: str) -> list[str]:
    """检测 verify_command 中可用于 shell 注入的构式（P1-4）。

    合法 verify 只应含 pytest / python -c / exit 等；命中以下构式即视为不可信：
    命令替换、管道、命令链、重定向、危险外部命令。注册期 E027 warning，
    执行期（done / amend dry-run）硬阻断。

    判定输入为**原始串**，仅按引号状态机剥离**最外层**单引号字面段（见
    :func:`_strip_single_quoted_segments`）：执行点 orchd/onboard/lifecycle/core.py
    ::_run_verify 用原始 verify_cmd 经 shell=True 执行，双引号内 `$(...)` / 反引号 /
    `$var` 同样被 shell 展开，故不得把引号内容整体剥离后再判定（剥离会与执行点语义
    脱钩，形成绕过）；双引号内的单引号不构成定界符，同样不得据此豁免。

    Args:
        verify_cmd: 原始 verify_command 串。

    Returns:
        命中原因列表；无命中返回空列表。
    """
    import re as _re
    reasons: list[str] = []
    scanned = _strip_single_quoted_segments(verify_cmd)
    # 命令替换 / 反引号：任意代码执行，合法 verify 从不使用（双引号内同样生效）
    if _re.search(r"\$\(|`", scanned):
        reasons.append("含命令替换 $(...) 或反引号")
    # sh/bash -c：执行任意命令串。不拦 bash -n（语法检查）与 .sh 后缀（合法）
    if _re.search(r"\b(?:sh|bash)\s+-c\b", scanned):
        reasons.append("含 sh/bash -c 任意命令执行")
    # 危险外部命令：任意系统副作用 / 网络外联
    # 注：不拦截管道 | 与重定向 >/< —— 现有 master 合法使用（>/dev/null、| grep），
    # 其后的恶意命令由本清单（curl/wget/rm 等）覆盖。
    for bad in ("rm", "curl", "wget", "nc", "chmod", "chown", "reboot",
                "shutdown", "mkfs", "dd"):
        if _re.search(rf"\b{_re.escape(bad)}\b", scanned, _re.IGNORECASE):
            reasons.append(f"含危险命令 {bad}")
            break
    return reasons


def verify_command_dangerous_reasons(verify_cmd: str) -> list[str]:
    """公开入口（P1-4）：返回 verify_command 的 shell 注入风险原因，供执行点硬阻断。"""
    return _dangerous_shell_reasons(verify_cmd)


def _basetemp_platform_issues(verify_cmd: str) -> list[str]:
    """检测 verify_command 中 --basetemp 路径的平台性（E027，2026-08-12 实踩）。

    跨平台 basetemp 应使用 `${TMPDIR:-/tmp}/orchd-vf-$$`——verify_command 在
    Windows 上经 Git Bash（orchd.subproc.run_shell）执行，由 bash 展开，真正跨平台。
    命中两类平台专用片段即判定为非跨平台：
    - Windows 专用：`%LOCALAPPDATA%` / `%TEMP%` / `%RANDOM%` / 反斜杠路径（`\\Temp` 式）
    - POSIX 专用：`${TMPDIR`（无 `:-` 回退） / `$(mktemp`

    Returns:
        命中原因列表；无 basetemp 或路径跨平台时返回空列表。
    """
    import re as _re
    basetemp_m = _re.search(r'--basetemp\s*=\s*"?([^"\s]+)', verify_cmd)
    if not basetemp_m:
        return []
    basetemp = basetemp_m.group(1)

    reasons: list[str] = []
    # Windows 专用片段
    windows_pat = r"%LOCALAPPDATA%|%TEMP%|%RANDOM%|\\\\|\\Temp"
    if _re.search(windows_pat, basetemp):
        reasons.append("basetemp 路径含 Windows 专用片段"
                       f"（{basetemp}），非跨平台——应改 {_CROSS_PLATFORM_BASETEMP}")
    # POSIX 专用：${TMPDIR 无 :- 回退（跨平台模板 ${TMPDIR:-/tmp} 是针对的例外）
    if _re.search(r"\$\{TMPDIR(?![^}]*:-)",
                  basetemp) or "$(mktemp" in basetemp:
        reasons.append("basetemp 路径含 POSIX 专用片段"
                       f"（{basetemp}），非跨平台——应改 {_CROSS_PLATFORM_BASETEMP}")
    return reasons
