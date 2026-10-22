"""gitops hook 域：pre-commit / reference-transaction hook 的路径、模板与安装。

域：content（W6-C8 从 orchd/gitops/hook.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orchd.gitops._run import _shell_quote

from .paths import _resolve_ledger_path
from .precommit_template import _PRE_COMMIT_HOOK_TEMPLATE
from .templates import (
    _HOOK_MARKER,
    _HOOK_PY_LEDGER_STATE,
    _HOOK_PY_RESOLVER,
    _HOOK_SHEBANG,
    _REF_TX_HOOK_MARKER,
)



def _e020_hook_escape_block() -> str:
    """生成 E020 越界提交的 hook 逃生文本（与 guide.py E020 recovery 同源）。

    单一生成函数：guide.py ERROR_GUIDANCE["E020"] 为权威来源，hook 文本
    从其 recovery/command/exit_type 派生，避免双写漂移。
    逃生步骤覆盖：查看被拦文件、移出暂存区、amend 补声明、红线命令警告。
    """
    try:
        from orchd.guide import ERROR_GUIDANCE, amend_patch_cmd
        e020 = ERROR_GUIDANCE.get("E020", {})
        recovery = e020.get("recovery", "范围外提交：只改 files_to_edit 声明文件")
        command = e020.get("command", "git status")
        exit_type = e020.get("exit_type", "git-diagnose")
        amend_files = amend_patch_cmd("<id>", files=["<file>"], entry="orchd")
        amend_exempt = amend_patch_cmd("<id>", exempt=["<file>"], entry="orchd")
    except Exception:
        recovery = "范围外提交：只改 files_to_edit 声明文件"
        command = "git status"
        exit_type = "git-diagnose"
        amend_files = "orchd amend --task <id> --files-to-edit <file>"
        amend_exempt = "orchd amend --task <id> --exempt-files <file>"

    lines = [
        f'    echo "E020 recovery: {recovery}"',
        f'    echo "E020 exit_type: {exit_type}"',
        f'    echo "E020 diagnostic: {command}"',
        '    echo ""',
        '    echo "=== 合规逃生步骤 ==="',
        '    echo "1. 查看被拦文件: git diff --cached --name-only"',
        '    echo "2. 移出暂存区(保留工作区): git restore --staged <file>"',
        f'    echo "3. 确属本任务: {amend_files}"',
        f'    echo "4. 豁免类(测试/文档): {amend_exempt}"',
        '    echo "5. 重新提交: git commit -m \"...\""',
        '    echo ""',
        '    echo "=== 红线命令(禁止,会丢失未提交工作) ==="',
        '    echo "  X git reset --hard  (丢弃工作区+暂存区)"',
        '    echo "  X git clean -fdx    (删除未跟踪文件,含.orchd运行时)"',
        '    echo "  X git checkout -- . (丢弃所有未提交改动)"',
        '    echo "  绕过仅限固定资产: git commit --no-verify"',
    ]
    return "\n".join(lines)



def _baked_hook_endpoints(project_root: Path) -> tuple[str | None, str | None]:
    """安装期固化 hook 的解释器与引擎源码根（task-ref-tx-hook-cost）。

    运行期钩子每次 checkout 都重走「解释器探测 + 嵌套 ``git rev-parse``」，
    在 MSYS 祖先链下被放大 35–90 倍。安装期 ``project_root`` 已知，一次性
    解析并烘焙进脚本，运行期零探测直达执行；固化路径失效时回退既有探针。

    Returns:
        ``(python_exe, src_dir)``；任一不可用即 ``(None, None)``（= 不烘焙，
        生成物与旧版逐字一致）。
    """
    try:
        py = sys.executable
        if not py:
            return None, None
        from orchd.gitops._run import _run_git

        top = _run_git(project_root, ["rev-parse", "--show-toplevel"])
        if top.returncode != 0 or not (top.stdout or "").strip():
            return None, None
        root = Path((top.stdout or "").strip())
        module_rel = "orchd/gitops/ref_tx_hook.py"
        if (root / ".orchd" / module_rel).is_file():
            return py, str(root / ".orchd")
        if (root / module_rel).is_file():
            return py, str(root)
    except Exception:
        pass
    return None, None



def _engine_source_root() -> Path:
    """引擎源码根（仓库根）：**与实现文件深度无关**的推导。

    W6-C8 修正：此处原本是 `_precommit_hook_params` 体内的
    ``Path(__file__).resolve().parent.parent.parent`` —— 它暗含「本文件在
    ``orchd/gitops/<file>.py``」这一深度假设。拆包后本文件变成
    ``orchd/gitops/hook/content.py``，同一表达式少算一层、把引擎根算成 ``<repo>/orchd``，
    于是 hook 运行期 ``import orchd`` 失败（``No module named 'orchd'``）、ledger 回退查询
    失效，实测把 DONE 任务误判为活跃并拦提交（E020）。改用 ``orchd`` 包自身的 ``__file__``
    （``<repo>/orchd/__init__.py`` 上两级 = 仓库根），函数内导入避免顶层循环。
    """
    from orchd import __file__ as _orchd_file

    return Path(_orchd_file).resolve().parent.parent

def _line_ref_tx_env(project_root: Path) -> tuple[str | None, str | None]:
    """多线下强制层需要的 trunk；**单线 → (None, None)**。

    task-line-ref-tx-per-line：单线不烘焙（生成物与历史逐字一致、零额外成本）。
    pass8 F1-A 单根命名空间后任务前缀恒为 ``refs/heads/task/``（全线通用，
    强制层常量 ``TASK_PREFIX`` 即此值），烘焙仅携带**当前线 trunk**，供强制层
    判定第二线 trunk 受管 merge。
    """
    try:
        from orchd.line import is_multi_line
        from orchd.line_ctx import _project_for, resolve_trunk_for

        if not is_multi_line(_project_for(project_root)):
            return None, None
        return "refs/heads/task/", resolve_trunk_for(project_root)
    except Exception:  # noqa: BLE001 - best-effort，失败退回单线口径
        return None, None



def ref_tx_hook_content(
    py_exe: str | None = None,
    src_dir: str | None = None,
    line_task_prefix: str | None = None,
    line_trunk: str | None = None,
) -> str:
    """生成强制层 hook 脚本（shell shim → ``python -m orchd.gitops.ref_tx_hook``）。

    与 pre-commit hook 同约定：纯 LF、含 shebang 与 orchd 归属标记（首部 5 行内）——
    故复用 :func:`_validate_hook_content` 的形态自检与 :func:`_is_orchd_hook` 的
    owner-aware 判定（宿主自有 reference-transaction hook 一律不覆盖 / 不删除）。

    引擎定位（宿主布局事实：宿主项目自带 ``.orchd/`` 引擎副本，`python .orchd/__main__.py`）：
    ① ``$ORCHD_SRC_DIR``（显式；CI / 外置安装）→ ② 安装期烘焙路径（解释器 +
    源码根已知，零探测直达；失效回退探针）→ ③ ``<git toplevel>/.orchd``（宿主自带）
    → ④ ``import orchd``（源码仓库内运行）。皆不可用时打印诊断并放行（**禁止静默**，
    且不阻断宿主 git 操作——与 pre-commit 的解释器缺失处置同族）。
    """
    # 注：py_code 内只用双引号字符串——外层 sh 以单引号包裹 -c 参数，
    # 单引号会提前闭合 sh 引用（task-ref-tx-hook-cost 实测回归）。
    py_code = (
        "import sys, importlib.util as _ilu; "
        '_p = sys.argv[1] + "/orchd/gitops/ref_tx_hook.py"; '
        '_s = _ilu.spec_from_file_location("orchd_ref_tx_hook_standalone", _p); '
        "_m = _ilu.module_from_spec(_s); _s.loader.exec_module(_m); "
        'sys.exit(_m.main(["reference-transaction"] + sys.argv[2:]))'
    )
    module_rel = "orchd/gitops/ref_tx_hook.py"
    py_probe = "python / python3 / py"
    no_py = f"[orchd ref-tx] 未找到可用 python 解释器（{py_probe}）"
    no_engine = (
        "[orchd ref-tx] 未定位到 orchd 引擎（ORCHD_SRC_DIR / 烘焙路径 / "
        "<toplevel>/.orchd / <toplevel> 均不含引擎源码）"
    )
    # 安装期烘焙快道（task-ref-tx-hook-cost）：解释器与源码根已知时，运行期
    # 零探测、零嵌套 git 直达执行；固化路径失效（升级/迁移）时落到下探针。
    baked = ""
    if py_exe and src_dir:
        baked = (
            "# 安装期固化快道：零探测直达；失效回退下探针。\n"
            f"ORCHD_BAKED_PY={_shell_quote(py_exe)}\n"
            f"ORCHD_BAKED_SRC={_shell_quote(src_dir)}\n"
            f'if [ -x "$ORCHD_BAKED_PY" ] && [ -f "$ORCHD_BAKED_SRC/{module_rel}" ]; then\n'
            f"    exec \"$ORCHD_BAKED_PY\" -c '{py_code}' \"$ORCHD_BAKED_SRC\" \"$@\"\n"
            "fi\n"
            "# 能执行到此处即固化快道未命中（exec 成功即替换进程）→ 留痕后回退探针。\n"
            'echo "[orchd ref-tx] 烘焙路径失效，回退解释器/引擎探针" >&2\n'
            "\n"
        )
    line_env = ""
    if line_task_prefix and line_trunk:
        line_env = (
            "# 多线（契约 v2）：安装期烘焙当前线命名空间前缀与 trunk"
            "（task-line-ref-tx-per-line）。\n"
            f"export ORCHD_LINE_TASK_PREFIX={_shell_quote(line_task_prefix)}\n"
            f"export ORCHD_LINE_TRUNK={_shell_quote(line_trunk)}\n"
        )
    return (
        f"{_HOOK_SHEBANG}\n"
        f"{_HOOK_MARKER}\n"
        f"{_REF_TX_HOOK_MARKER}\n"
        "# 红线 #1/#2 强制层：引用事务 prepared 阶段拦截（绕过代理直连 git 同样被拒）。\n"
        "# 模块 orchd.gitops.ref_tx_hook 按文件路径独立加载，不走包 __init__。\n"
        "# 逃生口：ORCHD_ALLOW_REF_TX=1（人工修复 / 仓库迁移）。\n"
        "set -u\n"
        "\n"
        + line_env +
        baked +
        'ORCHD_PY=""\n'
        "for _CAND in python python3 py; do\n"
        '    if command -v "$_CAND" >/dev/null 2>&1; then\n'
        '        ORCHD_PY="$_CAND"\n'
        "        break\n"
        "    fi\n"
        "done\n"
        'if [ -z "$ORCHD_PY" ]; then\n'
        f'    echo "{no_py} → 强制层未生效（禁止静默）" >&2\n'
        "    exit 0\n"
        "fi\n"
        "\n"
        "# 引擎定位（纯文件判定，不做 import 探针——探针成功但实际导入失败会导致\n"
        "# 「意外 fail-closed」：hook 报错退出，git 事务被中止，宿主提交被无故阻断）。\n"
        'ORCHD_SRC=""\n'
        f'if [ -n "${{ORCHD_SRC_DIR:-}}" ] && [ -f "$ORCHD_SRC_DIR/{module_rel}" ]; then\n'
        '    ORCHD_SRC="$ORCHD_SRC_DIR"\n'
        "else\n"
        '    _ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")\n'
        f'    if [ -n "$_ROOT" ] && [ -f "$_ROOT/.orchd/{module_rel}" ]; then\n'
        '        ORCHD_SRC="$_ROOT/.orchd"\n'
        f'    elif [ -n "$_ROOT" ] && [ -f "$_ROOT/{module_rel}" ]; then\n'
        '        ORCHD_SRC="$_ROOT"\n'
        "    fi\n"
        "fi\n"
        'if [ -n "$ORCHD_SRC" ]; then\n'
        f"    exec \"$ORCHD_PY\" -c '{py_code}' \"$ORCHD_SRC\" \"$@\"\n"
        "fi\n"
        f'echo "{no_engine} → 强制层未生效（禁止静默）" >&2\n'
        "exit 0\n"
    )



def _precommit_hook_content(
    *,
    task_id: str,
    files_list: str,
    ledger_q: str,
    bound_task: str,
    engine_q: str,
    static_precheck_body: str,
    static_hit_action: str,
    default_ref_q: str,
    files_check: str,
    exempts_check: str,
    static_allowed_echo: str,
    static_exempt_header: str,
    static_exempt_echo: str,
) -> str:
    """按 _PRE_COMMIT_HOOK_TEMPLATE 渲染 pre-commit hook 脚本（原 hook_install 体内 f-string 的等价替换）。

    只做模板插值：13 个随任务/仓库变化的字段由 :func:`_precommit_hook_params` 烘焙，
    4 个模块级/函数级值（归属标记 / 两段内联 python / E020 逃生指引）在此就地填充。
    渲染结果需与起点 blob 的 f-string 渲染**逐字节相等**（守卫
    ``tests/test_hook_template_external.py`` 以基线 AST 就地 eval 机判）。
    """
    return _PRE_COMMIT_HOOK_TEMPLATE.format(
        TASK_ID=task_id,
        FILES_LIST=files_list,
        LEDGER_Q=ledger_q,
        BOUND_TASK=bound_task,
        ENGINE_Q=engine_q,
        STATIC_PRECHECK_BODY=static_precheck_body,
        STATIC_HIT_ACTION=static_hit_action,
        DEFAULT_REF_Q=default_ref_q,
        FILES_CHECK=files_check,
        EXEMPTS_CHECK=exempts_check,
        STATIC_ALLOWED_ECHO=static_allowed_echo,
        STATIC_EXEMPT_HEADER=static_exempt_header,
        STATIC_EXEMPT_ECHO=static_exempt_echo,
        HOOK_MARKER=_HOOK_MARKER,
        PY_RESOLVER=_HOOK_PY_RESOLVER,
        PY_LEDGER_STATE=_HOOK_PY_LEDGER_STATE,
        E020_ESCAPE_BLOCK=_e020_hook_escape_block(),
    )



def _precommit_hook_params(
    project_root: Path,
    task_id: str,
    files_to_edit: list[str],
    exempt: list[str],
) -> dict[str, str]:
    """烘焙 pre-commit hook 模板的 13 个动态字段（原 hook_install 体 L845-929，17 条语句）。

    按行区间原样搬移：账本路径与 default 分支解析、注释展示转义、目录式声明感知的
    静态 scope 行、静态预检快路径（含换行文件名降级分支）、三类越界提示 echo、
    绑定任务与引擎根的 shell 转义。无 I/O 写、无新增外部进程（调用面与顺序不变）。
    返回 dict 供 :func:`_precommit_hook_content` 关键字插值。
    """
    # 账本路径在安装期按布局解析并烘入模板（task-hook-ledger-container-fix）
    ledger_abs = _resolve_ledger_path(project_root)
    ledger_q = _shell_quote(ledger_abs)
    # task-hook-flat-resolver：install 期解析 default 分支名并烘入 hook，供动态解析
    # 走 `git show <ref>:.orchd/_master.json`（与命令侧 resolve_declaration_source 同源，
    # 分支名经 get_default_branch 解析，缺失回退 "main"）。
    try:
        from orchd.gitops.query import get_default_branch
        default_ref = get_default_branch(project_root) or "main"
    except Exception:
        default_ref = "main"
    default_ref_q = _shell_quote(default_ref)

    # 生成 hook 脚本内容（shell 逻辑 + 运行时动态解析；回退用静态列表）
    # 顶部注释保留绑定任务与文件清单（可读性 / 既有断言兼容，不参与正常路径判定）。
    # 注释行展示转义（pass9 F1）：注释体除换行外皆惰性，声明含换行会把注释断行、
    # 断行后的内容成为可执行语句行——渲染为字面 \n 保住「每行都是注释」不变量
    # （声明层另由 validate_structure E003 fail-closed 拒绝控制字符路径）。
    def _comment_display(path: str) -> str:
        return path.replace("\n", "\\n")

    files_list = "\n".join(f"#   {_comment_display(f)}" for f in files_to_edit)
    if exempt:
        files_list += "\n# Exempt files:"
        files_list += "\n" + "\n".join(f"#   {_comment_display(f)}" for f in exempt)
    # 回退路径（无 task/<id> 分支）用的静态允许列表：文件名单引号转义（防 shell 注入）
    # 目录式声明感知：精确相等 + 目录前缀匹配（orchd/cli/ → orchd/cli/*）
    # 注意（pass9 F1）：case 模式中目录前缀部分同样单引号包裹、* 在引号外作通配符——
    # 单引号内 $ / 反引号 / " 均为字面量，双引号形态下它们会被展开（注入面）
    def _scope_check_lines(paths):
        lines = []
        for f in paths:
            q = _shell_quote(f)
            # 目录式声明：精确相等（目录名本身）或前缀匹配（目录下文件）
            lines.append(f'            if [ "$FILE" = {q} ]; then IN_SCOPE=yes; fi')
            if f.endswith("/"):
                # 单引号字面量前缀 + 引号外 * 通配（POSIX case：引号包裹的
                # 模式段按字面匹配，未包裹段保持通配）
                lines.append(f'            case "$FILE" in {q}*) IN_SCOPE=yes ;; esac')
        return "\n".join(lines)
    files_check = _scope_check_lines(files_to_edit)
    exempts_check = _scope_check_lines(exempt)
    # 静态预检快路径（task-hook-static-precheck）：命中只跳过 python resolver，
    # 判定仍走第 4 步动态循环（消费静态落盘的 ALLOWED_TMP）——动态循环是
    # 注入面守卫（content-guard）与历史负控制 instrument 的锚点，不可绕过。
    # 正确性依据（两条缺一不可）：① 分支任务 == 绑定任务（模板内
    # `[ "$TASK_ID" = "$BOUND_TASK" ]` 门控；不一致时静态列表与动态真值无关）；
    # ② 声明只增不删（并集追加），静态 ⊆ 动态真值，故"staged 全被静态覆盖 ⟹
    # 动态同样全覆盖"，快路径与全量动态结论一致。
    # 换行文件名 guard：静态落盘是行式消费，声明含换行时禁用快路径（全量动态，
    # 与修复前一致——动态对换行声明同样无能为力，不回归）。
    _static_paths = list(files_to_edit) + list(exempt)
    if any("\n" in p for p in _static_paths):
        _static_precheck_body = "        _SC_IN=no  # 静态声明含换行文件名：本项永不命中"
        _static_hit_action = '        : "静态声明含换行文件名：禁用快路径"'
    else:
        _static_precheck_body = _scope_check_lines(
            _static_paths).replace("$FILE", "$_SC_FILE").replace(
                "IN_SCOPE", "_SC_IN")
        _static_printf_args = " ".join(_shell_quote(p) for p in _static_paths)
        _static_hit_action = (
            "        # 命中：静态列表即本次判定真源，直接落盘供第 4 步动态分支消费\n"
            "        # （printf 固定格式 + 单引号逐参，无二次展开注入面）\n"
            "        if [ -z \"$_SC_MISS\" ]; then\n"
            f"            if printf '%s\\n' {_static_printf_args} > \"$ALLOWED_TMP\" 2>/dev/null; then\n"
            "                _SC_SKIP_RESOLVER=yes\n"
            "            fi\n"
            "        fi"
        )
    # 越界提示展示（pass9 F1）：printf 固定格式 + 单引号逐参——双引号内嵌路径
    # 会让 $ / 反引号在每次越界输出时执行（该段在 E020 拒绝分支对所有提交者运行）
    static_allowed_echo = "\n".join(
        f"        printf '  - %s\\n' {_shell_quote(f)}" for f in files_to_edit
    )
    # 无豁免时不输出 Exempt files 标题行（保持与无 exempt_files 行为一致）
    static_exempt_header = (
        '        echo "Exempt files for this task:"\n' if exempt else ""
    )
    static_exempt_echo = "\n".join(
        f"        printf '  - %s\\n' {_shell_quote(f)}" for f in exempt
    )
    bound_task = _shell_quote(task_id)
    # sqlite 回退查询的引擎根（task-hook-sqlite-python-fallback）：运行时 hook 的
    # python 内联把该目录插到 sys.path 后再 import orchd（hook cwd 未必含 orchd 包）；
    # 目录搬移/缺失则查询失败 → 保守分支，不抛错。
    engine_q = _shell_quote(str(_engine_source_root()))

    return {
        "task_id": task_id,
        "files_list": files_list,
        "ledger_q": ledger_q,
        "bound_task": bound_task,
        "engine_q": engine_q,
        "static_precheck_body": _static_precheck_body,
        "static_hit_action": _static_hit_action,
        "default_ref_q": default_ref_q,
        "files_check": files_check,
        "exempts_check": exempts_check,
        "static_allowed_echo": static_allowed_echo,
        "static_exempt_header": static_exempt_header,
        "static_exempt_echo": static_exempt_echo,
    }
