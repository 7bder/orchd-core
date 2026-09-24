"""跨平台 shell 命令执行（verify_command 等 POSIX 语法命令在 Windows 上的兼容层）。

verify_command 以 POSIX shell 语法书写（${TMPDIR:-/tmp}、ORCHD_SESSION_ID= 前缀、
test -f、bash -n、>/dev/null、| grep），Windows 的 cmd.exe（subprocess shell=True）
不识别这些构式 → done E014。本模块在 Windows 上按命令形态选执行器：

- **含 POSIX-only 构造** → 走 Git Bash（``bash -c``），语义与原行为一致；
- **cmd 兼容** → 走**原生快速通道**（``cmd /d /c``），只对白名单 token 做翻译。

两条健壮性约束（task-subproc-robustness）：

- **超时杀进程树**：超时后终止整棵进程树，而非只 kill 直接子进程——Windows 下孙进程
  继承 stdout/stderr 管道，只杀直接子进程会让 ``communicate()`` 一直阻塞到孙进程自然
  退出（实测 1s 预算被拖成 6.2s），门禁预算形同下界，且孤儿子进程会继续写复用目录
  污染下一次回归。
- **通道语义一致**：cmd 通道只承载「两通道可证明同语义」的命令——含 ``%`` 的命令
  （cmd 变量展开 vs bash 字面量）一律回落 bash；``${TMPDIR:-/tmp}`` 的注入值含不可
  中和字符（``"`` / ``%`` / 换行）时回落 bash，含空白或 cmd 元字符时按需加引号。

之所以区分（task-subproc-native-fastpath）：实测 **MSYS 祖先链会让其下的 MSYS 子进程
创建走慢路径**——同一负载在 MSYS 祖先下比原生进程慢 1~2 个数量级（hook 脚本类负载
50~70x，hook 密集 pytest 文件 7x）。执行器换成原生 cmd 即切断祖先链，快慢差值消失。
语义零放宽：翻译表外的构造一律回落到 Git Bash，命令书写契约仍为 POSIX。

与 guide 层零根入口的分工：guide.py 下发的 command 已收敛为零根入口
``python .orchd/__main__.py``（纯 Python 进程，不依赖 shell 语法，PowerShell/
cmd/Git Bash 下均可直接执行，无需本模块兜底）；本模块只负责引擎侧 verify_command
这类 POSIX 语法命令串的执行器选择，两层职责不同、互不干扰。
"""
from __future__ import annotations

import ctypes
import itertools
import os
import re
import shutil
import signal
import subprocess
import tempfile
from typing import Any

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


# ── 原生快速通道：白名单 token 翻译 + POSIX-only 判定 ──────────────────
# `${TMPDIR:-/tmp}` 的占位符：单点定义，翻译表与逐处注入判定共用（避免字面量漂移）。
_TMPDIR_PLACEHOLDER = "\x00T0\x00"

# 翻译表刻意保持**有限且可审计**：只有这三类 token 有确定的 cmd 等价物，
# 其余任何 POSIX 构造一律回落 Git Bash，不做猜测式翻译。
_TOKEN_TRANSLATIONS: tuple[tuple[str, str], ...] = (
    ("${TMPDIR:-/tmp}", _TMPDIR_PLACEHOLDER),
    ("/dev/null", "\x00T1\x00"),
    ("$$", "\x00T2\x00"),
)

# 出现任一 token 即判定为「POSIX-only」→ 回落 Git Bash。
# 判定在白名单替换**之后**进行（占位符保护），故翻译值本身不参与判定。
# 注意别用含尾空格的词（如 "test "）——它会误命中 "pytest " 这类正常命令；
# `test` 内建已由下方程序名表按**段首程序名**精确兜住。
_POSIX_ONLY_TOKENS: tuple[str, ...] = (
    "`",  # 反引号命令替换
    "$(",  # $(...) 命令替换
    "${",  # 表外参数展开
    "$",  # 表外变量引用
    ";",  # 分号命令分隔
    "\n",  # 换行分段：cmd /c 只执行首行 ⇒ 第二行起被丢弃（双通道 stdout 分叉）
    "'",  # 单引号（cmd 不作引号）
    "\\",  # 反斜杠转义 / 续行
)

# cmd 元字符：值里出现它们且未被双引号包裹时会被 cmd 拆词 / 当命令分隔符（`&` 尤其危险：
# 未加引号时会切成两条命令）。
_CMD_METACHARS = frozenset("&^|<>()")

# 杀进程树后回收 stdout/stderr 管道的宽限预算（秒）。正常路径下 taskkill / killpg 已让
# 管道立刻关闭，此预算只兜「有进程逃出进程组仍持管道」的极小概率场景——绝不无限等待。
_KILL_GRACE_S = 3.0

# 常见于 verify_command 的 POSIX 外部工具：Git for Windows 自带它们，但只在 Git Bash
# 的 PATH 里（Windows PATH 通常不含 Git\usr\bin）⇒ cmd 下不可用或与 cmd 内建**同名
# 不同义**（如 sort / find / date / mkdir）。命中即回落 bash，不赌宿主的 PATH。
_CMD_INCOMPATIBLE_PROGRAMS = frozenset({
    "awk", "basename", "bash", "cat", "chmod", "cp", "cut", "date", "diff", "dirname",
    "du", "egrep", "env", "expr", "false", "find", "grep", "head", "id", "ln", "ls",
    "mkdir", "mktemp", "mv", "nproc", "patch", "printf", "pwd", "realpath", "rm",
    "rmdir", "sed", "sh", "sleep", "sort", "tail", "tee", "test", "time", "touch",
    "tr", "true", "uname", "uniq", "wc", "which", "xargs", "zsh",
})

# cmd 内建对**双引号**的处理与 bash 不同（如 echo 原样输出引号，bash 会 consume 掉），
# 故「内建 + 带引号参数」的组合回落 bash；给外部程序的引号由 CRT 解析，语义一致。
_CMD_BUILTINS_QUOTING = frozenset({"echo", "for", "if", "set", "type"})

_SEGMENT_SPLIT = re.compile(r"&&|\|\||\|")

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_BANG_NEGATION = re.compile(r"^\s*!\s")

_counter = itertools.count(1)


def _leading_program(segment: str) -> str:
    """取命令段的程序名：剥离 ``VAR=value`` 环境变量前缀与两侧引号。"""
    tokens = segment.strip().split()
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN.match(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return ""
    program = tokens[idx].strip("\"'").replace("\\", "/")
    return program.rsplit("/", 1)[-1]


def _has_incompatible_program(staged: str) -> bool:
    """命令任一段的首个程序名属于 POSIX 工具表 ⇒ 不得走 cmd 通道。"""
    for segment in _SEGMENT_SPLIT.split(staged):
        if _leading_program(segment) in _CMD_INCOMPATIBLE_PROGRAMS:
            return True
    return False


def _has_quoting_builtin(staged: str) -> bool:
    """某段以 cmd 内建开头且该段带双引号 ⇒ 引号语义与 bash 不同，回落。"""
    for segment in _SEGMENT_SPLIT.split(staged):
        if '"' in segment and _leading_program(segment) in _CMD_BUILTINS_QUOTING:
            return True
    return False


def _has_env_assignment_prefix(staged: str) -> bool:
    """命令任一段以 ``VAR=value`` 前缀开头 ⇒ 不得走 cmd 通道。

    cmd.exe 不认 POSIX 的一次性环境变量赋值语法（``VAR=1 prog`` 会把 ``VAR=1``
    当命令名）。改写成 ``set VAR=1 && prog`` 会改变错误传播与作用域语义，
    故选择回落 Git Bash 而非翻译。
    """
    for segment in _SEGMENT_SPLIT.split(staged):
        tokens = segment.strip().split()
        if tokens and _ENV_ASSIGN.match(tokens[0]):
            return True
    return False


def _has_bang_negation(staged: str) -> bool:
    """命令任一段以 bash 取反内建 ``!`` 开头 ⇒ 不得走 cmd 通道。

    段首 ``!`` 既不在 POSIX 工具表也不在 token 表——``! grep`` 的段首程序名被
    判定为 ``!``，逃过 :func:`_has_incompatible_program`，cmd 下报
    「'!' 不是内部或外部命令」（实踩：verify_command ``! grep`` 误走快速通道 → done E014）。
    按段首精确判定而非加 ``"! "`` token，避免非段首 ``!``（如 ``echo hi!``）误伤。
    """
    return any(_BANG_NEGATION.match(seg) for seg in _SEGMENT_SPLIT.split(staged))


def _unique_suffix() -> str:
    """cmd 通道下替代 bash ``$$``：PID + 进程内序号（同进程多次调用不撞车）。"""
    return f"{os.getpid()}-{next(_counter)}"


def _tmpdir_value() -> str:
    """复刻 bash ``${TMPDIR:-/tmp}`` 语义：TMPDIR 优先，否则系统临时目录。"""
    return os.environ.get("TMPDIR") or tempfile.gettempdir()


def _cmd_value_embeddable(value: str) -> bool:
    """翻译值能否安全嵌入 cmd 命令行。

    含 ``"``（引号无法在 cmd 内层转义）、``%``（会被当变量引用）、换行 / NUL 时
    无法中和 ⇒ 调用方必须回落 bash（参考实现），不赌 cmd 解析。
    """
    return not any(ch in value for ch in ('"', "%", "\n", "\r", "\x00"))


def _cmd_value_needs_quoting(value: str) -> bool:
    """值含空白或 cmd 元字符 ⇒ 不加双引号会被拆词 / 当命令分隔符（``&``）。"""
    return any(ch.isspace() or ch in _CMD_METACHARS for ch in value)


def _cmd_expands_percent(staged: str) -> bool:
    """命令是否含 ``%``（cmd 变量展开面）⇒ 两通道语义必然分叉，拒绝 cmd 通道。

    cmd 下 ``%VAR%`` 会被展开、孤立 ``%`` 会被吞掉，bash 下 ``%`` 恒为字面量
    （实测 ``echo %TMP%``：cmd→``C:\\Temp``，bash→字面 ``%TMP%``）。原实现只拦
    「未成对的 %」，成对 ``%VAR%`` 仍走 cmd ⇒ 通道语义分叉。现一律回落 bash
    （参考实现），代价是该命令不走快速通道。
    """
    return "%" in staged


def _inject_tmpdir(staged: str, value: str) -> str:
    """把 ``\\x00T0\\x00`` 的**每一处**出现替换为 ``value``，逐处判定引号包裹状态。

    为什么必须逐处判定（task-subproc-newline-and-multislot-fix，pass5 N-6）：作者的引号
    分布可以逐处不同（``a=${TMPDIR:-/tmp} "b=${TMPDIR:-/tmp}"``），原实现按**首个**占位符
    的位置决定是否给替换值补引号、却对全串 replace ⇒ 第二处被塞进已有双引号内形成
    ``"b="C:\\tmp dir"/y"``，cmd 解析后参数被拆开（实测 argv 变成 ``b=C:\\tmp`` +
    ``dir/y``）。现按字符串扫描维护引号开合状态，对每处出现独立决定加不加引号。

    引号判定与 :func:`_cmd_value_needs_quoting` 一致：已被作者双引号包裹时插入裸值（避免
    嵌套引号），未包裹且值含空白 / 元字符时才补引号——安全值保持原样。
    """
    parts: list[str] = []
    quote_open = False
    index = 0
    while index < len(staged):
        if staged.startswith(_TMPDIR_PLACEHOLDER, index):
            token = value
            if _cmd_value_needs_quoting(value) and not quote_open:
                token = f'"{value}"'
            parts.append(token)
            index += len(_TMPDIR_PLACEHOLDER)
            continue
        char = staged[index]
        if char == '"':
            quote_open = not quote_open
        parts.append(char)
        index += 1
    return "".join(parts)


def plan_execution(cmd: str) -> tuple[str, str]:
    """决定命令走哪个执行器并返回 ``(channel, actual_cmd)``。

    Args:
        cmd: POSIX 语法书写的命令串（契约不变）。

    Returns:
        二元组 ``(channel, actual_cmd)``：

        - ``("cmd", translated)`` —— cmd 兼容，走原生快速通道（``actual_cmd``
          已替换白名单 token）；
        - ``("bash", original)`` —— 含 POSIX-only 构造、或非 Windows、或本机无
          Git Bash（此时由 :func:`run_shell` 落到 ``shell=True``），``actual_cmd``
          为原命令，翻译从未发生。
    """
    if os.name != "nt" or find_bash() is None:
        return ("bash", cmd)

    staged = cmd
    for source, placeholder in _TOKEN_TRANSLATIONS:
        staged = staged.replace(source, placeholder)

    if any(token in staged for token in _POSIX_ONLY_TOKENS):
        return ("bash", cmd)
    if (
        _has_incompatible_program(staged)
        or _has_env_assignment_prefix(staged)
        or _has_quoting_builtin(staged)
        or _has_bang_negation(staged)
    ):
        return ("bash", cmd)

    cooked = staged
    if _TMPDIR_PLACEHOLDER in cooked:
        tmpdir = _tmpdir_value()
        # 注入值本身含不可中和字符 ⇒ 回落 bash（bash 自己展开 ${TMPDIR:-/tmp}，语义正确）
        if not _cmd_value_embeddable(tmpdir):
            return ("bash", cmd)
        # 逐处判定引号包裹状态（见 _inject_tmpdir：多占位符时不得只按首个判定）。
        cooked = _inject_tmpdir(cooked, tmpdir)
    cooked = cooked.replace("\x00T1\x00", "NUL").replace("\x00T2\x00", _unique_suffix())

    # %：cmd 会展开 %VAR%（并吞掉孤立 %），bash 一律按字面量 ⇒ 两通道必然分叉。拒绝
    # cmd 通道（判定抽出成函数，测试方可正负控制在同一真源上做开关）。
    if _cmd_expands_percent(cooked):
        return ("bash", cmd)
    return ("cmd", cooked)


# ── Windows 进程树终止（Job Object）────────────────────────────────────────
# 为什么不用 taskkill /T 作主通道：实测它**漏** MSYS 的后台孙进程——`sleep.exe` 不在
# taskkill 枚举出的父子链上，杀完仍存活并继续持有 stdout/stderr 管道（超时预算 1s 被
# 拖成 12.2s）。Job Object 的成员表由内核维护、子进程自动入 Job，TerminateJobObject
# 才能保证整棵树终止。刻意**不设** KILL_ON_JOB_CLOSE：正常返回时关闭句柄不得杀任何
# 进程（保持既有语义），只有超时路径才显式终止。
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

_KERNEL32: Any = None
_KERNEL32_READY = False


def _kernel32() -> Any:
    """惰性取 kernel32 并声明签名（非 Windows / 无 ``ctypes.WinDLL`` 时返回 None）。

    ``restype`` 必须显式声明为 ``c_void_p``：ctypes 默认 ``c_int`` 会截断 64 位句柄。
    """
    global _KERNEL32, _KERNEL32_READY
    if not _KERNEL32_READY:
        _KERNEL32_READY = True
        win_dll = getattr(ctypes, "WinDLL", None)
        if os.name == "nt" and win_dll is not None:
            try:
                dll = win_dll("kernel32", use_last_error=True)
                dll.CreateJobObjectW.restype = ctypes.c_void_p
                dll.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
                dll.OpenProcess.restype = ctypes.c_void_p
                dll.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
                dll.AssignProcessToJobObject.restype = ctypes.c_int
                dll.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
                dll.TerminateJobObject.restype = ctypes.c_int
                dll.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                dll.CloseHandle.restype = ctypes.c_int
                dll.CloseHandle.argtypes = (ctypes.c_void_p,)
                _KERNEL32 = dll
            except (OSError, AttributeError):
                _KERNEL32 = None
    return _KERNEL32


def _attach_job(pid: int) -> int | None:
    """把 ``pid`` 挂进新建的匿名 Job Object，返回句柄（调用方负责关闭）。

    任一步失败（非 Windows / 无 kernel32 / 进程已退出 / 权限不足）返回 ``None``，
    调用方降级 ``taskkill``——能力探测失败不得阻断命令执行。
    """
    kernel32 = _kernel32()
    if kernel32 is None:
        return None
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            kernel32.CloseHandle(job)
            return None
        assigned = kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _close_job(job: int | None) -> None:
    """关闭 Job 句柄（未设 KILL_ON_JOB_CLOSE ⇒ 关闭本身不杀任何进程）。"""
    kernel32 = _kernel32()
    if job is None or kernel32 is None:
        return
    try:
        kernel32.CloseHandle(job)
    except (OSError, AttributeError, TypeError, ValueError):
        pass


def _terminate_job(job: int | None) -> None:
    """终止 Job 全部成员（内核枚举，覆盖 taskkill /T 漏掉的 MSYS 孙进程）。"""
    kernel32 = _kernel32()
    if job is None or kernel32 is None:
        return
    try:
        kernel32.TerminateJobObject(job, 1)
    except (OSError, AttributeError, TypeError, ValueError):
        pass


def _taskkill_tree(pid: int) -> None:
    """降级通道：``taskkill /F /T``（已知会漏 MSYS 后台孙进程，仅作兜底）。"""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _terminate_tree(proc: subprocess.Popen, *, job: int | None, own_group: bool) -> None:
    """超时后终止**整棵进程树**（只 kill 直接子进程不够，见模块 docstring）。

    优先级：Job Object（内核枚举成员）→ ``taskkill /F /T``（Windows）或 ``killpg``
    （POSIX 自建进程组）→ 单进程 kill。
    """
    if proc.poll() is not None:
        return
    if job is not None:
        _terminate_job(job)
    if proc.poll() is None:
        if os.name == "nt":
            _taskkill_tree(proc.pid)
        elif own_group:
            # getattr 而非直接属性访问：killpg / getpgid / SIGKILL 仅在 POSIX 存在，
            # 直接访问会让 Windows 上的类型检查（mypy 按宿主平台解析 stdlib）报错。
            killpg = getattr(os, "killpg", None)
            getpgid = getattr(os, "getpgid", None)
            if killpg is not None and getpgid is not None:
                try:
                    killpg(getpgid(proc.pid), getattr(signal, "SIGKILL", 9))
                except OSError:
                    pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _reap_after_kill(proc: subprocess.Popen, exc: subprocess.TimeoutExpired) -> tuple[bytes, bytes]:
    """杀树后回收管道；仍被占住时返回已读部分，绝不超出「预算 + 回收宽限」。

    刻意**不关闭** ``proc.stdout`` / ``proc.stderr``：CPython 的 ``communicate`` 在
    读线程阻塞时持有缓冲区锁，``close()`` 会一直等到读返回（实测多等 6.7s，把 3s
    宽限拖成 9.8s）。读线程是 daemon，句柄交由 GC 回收即可。
    """
    try:
        return proc.communicate(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        return (exc.stdout or b"", exc.stderr or b"")


def run_shell(cmd: str, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    """跨平台执行 shell 命令串：cmd 兼容走原生 cmd /c；否则 Git Bash / shell=True。

    返回 CompletedProcess（capture_output=True，bytes stdout/stderr），
    与调用方现有 _decode_subprocess_output / _verify_output_summary 契约一致。

    超时语义：抛 ``subprocess.TimeoutExpired``（``stdout`` 携带已读到的部分输出），
    但**先终止整棵进程树**再回收管道——否则持管道的孙进程会把超时拖成「子进程自然
    退出时刻」（实测 1s 预算被拖成 6.2s），超时预算形同下界（task-subproc-robustness）。
    树终止在 Windows 上经 Job Object（见 :func:`_attach_job`），POSIX 经
    ``start_new_session`` + ``killpg``。
    """
    channel, actual = plan_execution(cmd)
    spawn: dict[str, Any] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    own_group = False
    if channel == "cmd":
        # 刻意用**命令行字符串**而非 argv list：Windows 的 list2cmdline 会给含空格的
        # 参数补外层引号并把内部 " 转义成 \"，而 cmd.exe 不认反斜杠转义 ⇒ 引号语义被
        # 破坏（实测 python -c "print(1+1)" 经 argv list 传参 stdout 为空、exit(3) 得
        # rc=1）。字符串形式的行为与 subprocess shell=True 完全一致。
        # /d 跳过 cmd AutoRun，避免宿主自定义脚本污染执行结果。
        spawn["args"] = f"cmd /d /c {actual}"
    else:
        bash = find_bash() if os.name == "nt" else None
        if bash:
            spawn["args"] = [bash, "-c", actual]
        else:
            spawn["args"] = actual
            spawn["shell"] = True
        if os.name != "nt":
            # POSIX 自建会话 / 进程组：超时可整组终止，与 Windows 杀树语义对齐。
            # 注意组内进程的自杀风险——只有 `start_new_session=True` 时本进程才不在该组，
            # 故 `own_group` 与之一一对应。
            spawn["start_new_session"] = True
            own_group = True

    proc = subprocess.Popen(**spawn)
    # 挂进 Job Object：必须在进程启动后立刻做（子进程启动期 fork 出的后代仍会入 Job，
    # 因为入 Job 的子进程其后代自动继承成员身份）。
    job = _attach_job(proc.pid) if os.name == "nt" else None
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_tree(proc, job=job, own_group=own_group)
        out, err = _reap_after_kill(proc, exc)
        raise subprocess.TimeoutExpired(
            exc.cmd, exc.timeout, output=out, stderr=err
        ) from None
    finally:
        _close_job(job)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
