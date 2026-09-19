"""gitops hook 域：pre-commit hook 安装/卸载（叶子模块，零同包依赖）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from orchd.gitops._run import _run_git, _shell_quote
from orchd.gitops.cleanup import _safe_delete


_HOOK_FILENAME = "pre-commit"
# hook 归属标记（owner-aware 判定）：生成物首部注释行。宿主自有（husky / lint-staged
# / 手写）的 pre-commit 不含该标记，引擎据此只覆盖 / 删除自己生成的 hook，
# 绝不改写宿主文件（R2-3）。
_HOOK_MARKER = "# orchd L3 pre-commit hook"
# 生成物 shebang（自检项之一）：hook 由 git 交给 shell 解释器直接执行，首行缺失
# shebang 会让 git 无法运行该 hook（事故形态：生成模板首行被误改 / 截断）。
_HOOK_SHEBANG = "#!/bin/sh"
# 归属标记只扫首部 N 行（生成物标记在第 2 行）：避免把正文里偶然提到标记串的
# 宿主 hook 误判为本引擎生成物。
_HOOK_MARKER_SCAN_LINES = 5
# 仓库自带 hooks 目录（随 clone 检出，内含 pre-push 发版同步保护等仓库级 hook）。
# 只有 core.hooksPath 指向它才会生效——该配置是**本地仓库配置**，不随 clone 传播。
_REPO_HOOKS_DIR = ".githooks"


# 运行时动态解析任务定义的内嵌 python（task-precommit-hook-multitask）。
# 由 hook 以 heredoc 方式调用：argv = [git_common_dir, task_id]，stdout 输出该任务
# 的 files_to_edit ∪ exempt_files（每行一个）；失败写 stderr 并返回非零。
# 用 % 格式化（而非 f-string），避免插入 hook 模板 f-string 时花括号被二次解析。
_HOOK_PY_RESOLVER = """\
import json
import os
import sys

# 强制 UTF-8 输出：hook 其余部分（MSYS sh echo）产出 UTF-8，若此处按 Windows
# locale(gbk) 写中文诊断会造成混合编码，git/调用方按 UTF-8 解码时失败。
# newline="\\n"（CRLF 回归）：Windows 文本模式会把 print 的 \\n 翻成 \\r\\n，shell
# 侧 while read 得到的路径带尾 \\r，与 staged 路径精确/前缀判定永不命中——只有
# 最后一行因命令替换吞掉尾部 CRLF 而幸免，表现为「允许列表里明明有却判越界」。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", newline="\\n")
    except Exception:
        pass

_common = (sys.argv[1] or ".git").replace("\\\\", "/")
_task = sys.argv[2]
if _common == ".git":
    _root = "."
elif _common.endswith("/.git"):
    _root = _common[:-5] or "/"
else:
    _root = os.path.dirname(_common) or "."
_master = os.path.join(_root, ".orchd", "_master.json")
try:
    with open(_master, encoding="utf-8") as _fh:
        _data = json.load(_fh)
except Exception as _exc:
    print("[orchd E020] 动态解析失败：无法读取 %s：%s" % (_master, _exc), file=sys.stderr)
    sys.exit(2)
_match = [t for t in (_data.get("tasks") or []) if t.get("id") == _task]
if not _match:
    print("[orchd E020] 动态解析失败：%s 中无任务 %s" % (_master, _task), file=sys.stderr)
    sys.exit(3)
for _p in list(_match[0].get("files_to_edit") or []) + list(_match[0].get("exempt_files") or []):
    if _p:
        print(_p)
"""


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


def _is_absolute_hooks_path(value: str) -> bool:
    """判断 core.hooksPath 配置值是否为绝对路径（跨平台）。

    POSIX 上无法用 Path.is_absolute() 识别 Windows 盘符形态（C:/...、C:\\...），
    单独用盘符正则兜底；保证在任意平台解析 hooksPath 均一致。
    """
    return Path(value).is_absolute() or bool(re.match(r"^[A-Za-z]:[/\\]", value))


def _get_hooks_dir(project_root: Path) -> Path:
    """解析实际 hooks 目录：git config core.hooksPath，缺省回退 .git/hooks。

    core.hooksPath 语义（git-config 文档）：
    - 未设置 / 设置为空 → 默认 .git/hooks
    - 相对路径 → 相对仓库根目录
    - 绝对路径 → 原样使用
    git 不可用或 config 读取失败 → 保守回退 .git/hooks（与旧版行为一致）。
    """
    try:
        proc = _run_git(project_root, ["config", "--get", "core.hooksPath"])
    except Exception:
        return project_root / ".git" / "hooks"
    if proc.returncode != 0:
        return project_root / ".git" / "hooks"
    value = proc.stdout.strip()
    if not value:
        return project_root / ".git" / "hooks"
    hooks = Path(value)
    if _is_absolute_hooks_path(value):
        return hooks
    return project_root / hooks


def _get_hook_path(project_root: Path) -> Path:
    """返回实际 hooks 目录下的 pre-commit 路径（适配 core.hooksPath）。"""
    return _get_hooks_dir(project_root) / _HOOK_FILENAME


def _read_hooks_path(project_root: Path) -> str | None:
    """读取 core.hooksPath 原始配置值（未设置 / 空 / git 不可用 → None）。"""
    try:
        proc = _run_git(project_root, ["config", "--get", "core.hooksPath"])
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _is_same_hooks_dir(project_root: Path, value: str, target: Path) -> bool:
    """core.hooksPath 配置值是否与目标目录等价。

    解析规则与 :func:`_get_hooks_dir` 一致（相对 → 相对仓库根；绝对 → 原样），
    比较用 ``resolve()`` 抹平分隔符与 ``..`` 差异。
    """
    resolved = Path(value) if _is_absolute_hooks_path(value) else project_root / value
    try:
        return resolved.resolve() == target.resolve()
    except OSError:
        return False


def _ensure_hooks_path(
    project_root: Path,
    hooks_dir: str = _REPO_HOOKS_DIR,
) -> dict[str, Any]:
    """确保仓库自带 hooks 目录被 git 启用（core.hooksPath），幂等且不覆盖用户配置。

    task-decl-hooks-autoset：``core.hooksPath`` 是**本地仓库配置**，不随
    ``git clone`` 传播——新 clone 检出后仓库内 ``.githooks/``（如 pre-push 发版
    同步保护）不会生效，质量门禁形同虚设。安装流程与本模块的 hook 安装期各调用
    一次，把它自动打开。

    决策表（永不抛异常）：

    - 非 git 仓库 → ``reason="not_a_git_repo"``，不动；
    - 仓库内无 ``hooks_dir`` 目录 → ``reason="hooks_dir_missing"``，不动；
    - ``core.hooksPath`` 未设置 / 空 → 设为 ``hooks_dir``（相对仓库根），
      ``reason="set"``；
    - 已指向同一目录（相对或等价绝对路径）→ 幂等不写，``reason="already_set"``；
    - 指向其他路径（用户显式自定义）→ **不改写**，``reason="custom_hooks_path"``
      并附 ``hint`` 提示手动改法。

    本函数只新增"写配置"动作；:func:`_get_hooks_dir` 的既有解析语义（相对/绝对/
    缺省回退 ``.git/hooks``）不变。

    Returns:
        ``{"configured": bool, "reason": str, ...}``；``configured=True`` 仅表示
        本次真的写入了配置。
    """
    root = Path(project_root)
    if not (root / ".git").exists():
        return {"configured": False, "reason": "not_a_git_repo"}
    target = root / hooks_dir
    if not target.is_dir():
        return {"configured": False, "reason": "hooks_dir_missing",
                "hooks_dir": hooks_dir}
    current = _read_hooks_path(root)
    if current is None:
        proc = _run_git(root, ["config", "core.hooksPath", hooks_dir])
        if proc.returncode != 0:
            return {"configured": False, "reason": "config_failed",
                    "error": proc.stderr.strip()}
        return {"configured": True, "reason": "set", "hooks_path": hooks_dir}
    if _is_same_hooks_dir(root, current, target):
        return {"configured": False, "reason": "already_set", "hooks_path": current}
    return {
        "configured": False,
        "reason": "custom_hooks_path",
        "hooks_path": current,
        "expected": hooks_dir,
        "hint": (
            f"core.hooksPath 已自定义为 {current}（非仓库自带 {hooks_dir}）：保持不动、"
            f"未改写；如需改为仓库自带 hooks，执行 git config core.hooksPath {hooks_dir}"
        ),
    }


def _is_orchd_hook(hook_path: Path) -> bool:
    """目标 pre-commit 是否由本引擎生成（首部含 orchd 归属标记，owner-aware）。

    只扫首部 ``_HOOK_MARKER_SCAN_LINES`` 行；读取失败（权限 / 二进制 / 编码）
    一律返回 False——保守方向是「不动宿主文件」，宁可跳过安装也不覆盖未知文件。

    Args:
        hook_path: pre-commit 文件路径。

    Returns:
        True = 本引擎生成物（可安全覆盖 / 删除）。
    """
    try:
        text = hook_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    head = "\n".join(text.splitlines()[:_HOOK_MARKER_SCAN_LINES])
    return _HOOK_MARKER in head


def _validate_hook_content(content: str) -> dict[str, Any]:
    """生成物完整性自检：写盘前拒绝 / 写盘后回读共用的**唯一形态判据**。

    事故背景（task-hook-staged-nul-rootfix）：f-string 内 ``tr '\\0' '\\n'`` 未转义
    → 生成物被写入**真实** NUL / 换行字符；而写入路径只强制 LF（``newline="\\n"``）、
    无内容自检，缺陷只能靠测试侧 ``bash -n`` 事后暴露。本函数把「生成物必须满足的
    形态约束」收敛为写盘前的单一判据：不通过即拒绝写盘，**不落半截生成物、不覆盖
    已有 hook**（宿主自有 hook 的完整性优先）。

    检查项（任一不满足即判失败）：

    - **NUL 控制字符**（``\\x00``：f-string 转义事故的典型产物）；
    - **CR**（``\\r``：hook 须纯 LF，CR 会污染 here-doc 终止符与路径比较）；
    - **shebang 首行**（缺失 → git 无法执行该 hook，等于拦截能力静默失效）；
    - **归属标记在首部**（owner-aware 判据，缺失会让引擎无法识别自己的生成物，
      卸载时不敢删、安装时被误判为宿主 hook）。

    Args:
        content: 待写入（或已写入后回读）的 hook 脚本文本。

    Returns:
        ``{"ok": True}`` 通过；``{"ok": False, "reason": <str>, "error": <str>}``
        拒绝，``reason`` 取值：``empty_content`` / ``nul_byte`` / ``cr_present`` /
        ``missing_shebang`` / ``missing_marker``。
    """
    if not content:
        return {"ok": False, "reason": "empty_content",
                "error": "生成物为空：拒绝写入 pre-commit hook"}
    if "\x00" in content:
        return {"ok": False, "reason": "nul_byte",
                "error": "生成物含 NUL 控制字符（典型为 f-string 内 \\0 被真实转义）"}
    if "\r" in content:
        return {"ok": False, "reason": "cr_present",
                "error": "生成物含 CR（hook 须纯 LF，CR 会污染 here-doc 终止符与路径比较）"}
    lines = content.splitlines()
    if not lines[0].startswith("#!"):
        return {"ok": False, "reason": "missing_shebang",
                "error": f"生成物首行缺失 shebang（须为 {_HOOK_SHEBANG}）"}
    if _HOOK_MARKER not in "\n".join(lines[:_HOOK_MARKER_SCAN_LINES]):
        return {
            "ok": False,
            "reason": "missing_marker",
            "error": (
                f"生成物首部 {_HOOK_MARKER_SCAN_LINES} 行内缺失归属标记"
                f"（{_HOOK_MARKER}）"
            ),
        }
    return {"ok": True}


def _write_hook_file(path: Path, content: str) -> None:
    """落盘 hook 脚本（统一 LF + 可执行位）。

    独立成函数的目的：① 写入动作是「自检通过后」的唯一出口，便于审计；
    ② 给落盘层留可替换的接缝（测试可据此模拟截断 / 污染写入，验证回读校验）。
    """
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _log_hook_skip(action: str, payload: dict[str, Any]) -> None:
    """把 hook 跳过 / 拒绝动作写 stderr 留痕（``orchd ▸ [hook]`` 前缀，best-effort）。

    与 ``worktree._log_recycle`` 同型：结构化 JSON 单行、任何异常静默不阻断主流程。
    差异：**不受 ORCHD_QUIET 抑制**——回收留痕是常规噪声，而「因宿主 hook 存在而
    跳过安装」是必须可追溯的安全决策（R2-3 的静默覆盖正是漏掉了这层留痕）；唯一
    调用方 ``onboard/claim.py::claim`` 目前丢弃 hook_install 返回值，故在此落一条
    stderr 审计行，避免跳过被静默吞掉。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass
    try:
        record = {"action": action, **payload}
        print(f"orchd ▸ [hook] {json.dumps(record, ensure_ascii=False)}", file=sys.stderr)
    except Exception:
        pass


def hook_install(
    project_root: Path,
    task_id: str,
    files_to_edit: list[str],
    exempt_files: list[str] | None = None,
) -> dict[str, Any]:
    """安装 pre-commit hook（任务活跃期越界提交拦截，运行时动态识别当前任务）。

    task-precommit-hook-multitask：多任务并行时，``core.hooksPath`` 指向全仓共享目录
    （如 ``main/.githooks``），只有最后安装的那一份生效；旧实现的允许列表是**生成时
    静态烘死**的单任务快照，导致正在实现中的任务越界提交不被拦。本实现改为：

    - **正常路径（当前分支为 ``task/<id>``）**：hook 运行时由分支名解析任务 id，再从
      canonical 主工作树的 ``.orchd/_master.json`` 动态读取该任务的
      ``files_to_edit ∪ exempt_files``（**不再依赖生成时快照**）；
    - **无匹配分支**（非 ``task/<id>``，含非 git / detached HEAD）：回退到本 hook 安装
      时绑定的任务与静态列表（**既有 best-effort 语义，零回归**），并输出 stderr 留痕；
    - **动态读取失败**（master 缺失 / JSON 解析失败 / python 不可用）：输出 stderr 诊断
      并放行（**禁止静默放行**），不阻断非归属提交。

    保留既有三步语义不变：
    - ① 任务未活跃（该 task_id 最近事件为 CLAIMED / REVIEW_CLAIMED，且无后续
      DONE / RETRACT / REVIEW_SUBMITTED）→ 放行（exit 0）；
    - ② R1-b 审查期实现者冻结：任务分支上 REVIEW_CLAIMED 且无后续结论 → 拒绝（E017）；
    - ③ 越界文件 → 拒绝（E020）并输出逃生指引。
    固定资产豁免（``.orchd/_master.json`` / ``IDEAS.md`` / ``.orchd/IDEAS.md`` /
    ``ROADMAP.md``）语义不变；``--no-verify`` 可绕过。注：``.orchd/ROADMAP.md`` 已非
    合法形态（ROADMAP 唯一源为宿主项目根，见 ``orchd/ledger.py::resolve_roadmap_path``），
    故从豁免清单移除，避免把残留副本当合法固定资产放行。

    安装前先调用 :func:`_ensure_hooks_path` 确保仓库自带 ``.githooks/`` 已启用
    （``core.hooksPath`` 是本地配置，不随 clone 传播）：仓库内存在 ``.githooks/``
    且配置未设置时，pre-commit 会落到该目录而非回退 ``.git/hooks``；既有 hooksPath
    解析语义（相对 / 绝对 / 缺省回退）不变。

    owner-aware（task-hook-owner-aware-install）：``core.hooksPath`` 可能指向宿主
    自定义目录（``.husky`` / ``lint-staged`` 等）——此时 ``hooks_dir/pre-commit``
    是宿主自有文件。写入前用 :func:`_is_orchd_hook` 探测：非本引擎生成物一律跳过
    （不写不删）并返回结构化告警 + stderr 留痕，避免 claim 静默覆盖宿主 hook；
    本引擎生成物（含标记头）照旧覆盖安装（重装幂等语义不变）。

    Args:
        project_root: git 仓库根目录。
        task_id: 安装（绑定）时的任务 ID（无 task/<id> 分支时作为回退）。
        files_to_edit: 绑定任务允许修改的文件列表（回退用；正常路径走动态解析）。
        exempt_files: 绑定任务豁免文件列表（回退用）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"installed": True, "path": <str>, "hooks_path": <dict>}`` hook
              安装成功（``hooks_path`` 为 :func:`_ensure_hooks_path` 的处置结果，
              供审计：set / already_set / custom_hooks_path / hooks_dir_missing …）。
            - ``{"installed": False, "reason": "not_a_git_repo"}`` 非 git 仓库。
            - ``{"installed": False, "reason": "io_error", "error": <str>}``
              写入失败（best-effort 降级）。
            - ``{"installed": False, "reason": "unsafe_task_id", "error": <str>}``
              task_id 含 shell 元字符（注入面），拒绝写入 hook。
            - ``{"installed": False, "reason": "foreign_hook_present", "path": <str>,
              "hooks_path": <dict>, "hint": <str>}`` 目标 pre-commit 已存在且不含
              orchd 标记头（宿主自有 hook）→ 不覆盖不删除，附原因与目标路径。
            - ``{"installed": False, "reason": "invalid_generated_hook", "path": <str>,
              "error": <str>, "check": <dict>}`` 生成物自检未通过（含 NUL / CR /
              缺 shebang / 缺归属标记）→ **拒绝写入**：不落半截文件、不动已有 hook。
            - ``{"installed": False, "reason": "invalid_written_hook", "path": <str>,
              "error": <str>, "check": <dict>}`` 落盘后回读内容与生成物不一致
              （写入层污染 / 截断）→ 结构化报错（判据与写盘前自检同源）。
    """
    # P1-5 安全加固：task_id 会被裸插值进 shell hook（grep/echo），须严格白名单，
    # 否则单引号/`$(...)`/换行可逃逸出 shell 引号 → 任意命令注入。
    if not task_id or any(not (c.isalnum() or c in "-_") for c in task_id):
        return {
            "installed": False,
            "reason": "unsafe_task_id",
            "error": "task_id 含非 [A-Za-z0-9_-] 字符，拒绝写入 pre-commit hook（防 shell 注入）",
        }

    # 安装期确保仓库自带 hooks 目录已启用（core.hooksPath 不随 clone 传播）：
    # 先于 hooks_dir 解析执行，使本次安装即落到正确目录；best-effort，不影响主流程。
    hooks_path = _ensure_hooks_path(project_root)

    hooks_dir = _get_hooks_dir(project_root)
    if not (project_root / ".git").exists():
        return {"installed": False, "reason": "not_a_git_repo"}

    exempt = exempt_files or []

    # 生成 hook 脚本内容（shell 逻辑 + 运行时动态解析；回退用静态列表）
    # 顶部注释保留绑定任务与文件清单（可读性 / 既有断言兼容，不参与正常路径判定）。
    files_list = "\n".join(f"#   {f}" for f in files_to_edit)
    if exempt:
        files_list += "\n# Exempt files:"
        files_list += "\n" + "\n".join(f"#   {f}" for f in exempt)
    # 回退路径（无 task/<id> 分支）用的静态允许列表：文件名单引号转义（防 shell 注入）
    # 目录式声明感知：精确相等 + 目录前缀匹配（orchd/cli/ → orchd/cli/*）
    # 注意：case 模式中目录前缀部分用双引号包裹（防空格），* 在引号外作为通配符
    def _scope_check_lines(paths):
        lines = []
        for f in paths:
            q = _shell_quote(f)
            if f.endswith("/"):
                # 目录式声明：精确相等（目录名本身）或前缀匹配（目录下文件）
                lines.append(f'            if [ "$FILE" = {q} ]; then IN_SCOPE=yes; fi')
                # 双引号包裹路径前缀，* 在引号外作通配符
                dq = f.replace("'", "'\''")
                lines.append(f'            case "$FILE" in "{dq}"*) IN_SCOPE=yes ;; esac')
            else:
                lines.append(f'            if [ "$FILE" = {q} ]; then IN_SCOPE=yes; fi')
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
    static_allowed_echo = "\n".join(
        f'        echo "  - {f}"' for f in files_to_edit
    )
    # 无豁免时不输出 Exempt files 标题行（保持与无 exempt_files 行为一致）
    static_exempt_header = (
        '        echo "Exempt files for this task:"\n' if exempt else ""
    )
    static_exempt_echo = "\n".join(
        f'        echo "  - {f}"' for f in exempt
    )
    bound_task = _shell_quote(task_id)

    hook_content = f"""#!/bin/sh
{_HOOK_MARKER} (auto-generated, do not edit)
# 解释器与 staged 精确消费（task-hook-staged-nul-rootfix）：含换行的文件名需要
#   NUL 分隔 + `read -d ''` 才能不被拆——`read -d` 是 **bash 内建**，POSIX sh
#   （dash / ash）不支持。此处**保留 `#!/bin/sh` + 运行时分流**，而非把 shebang
#   改成 `#!/usr/bin/env bash`：
#     · 无 bash 的宿主（如 alpine 容器）下，改 shebang 会让 hook 无法执行 →
#       git 直接拒绝该次提交（把整个提交链路拦死，比降级更糟）；
#     · Windows 上 `env bash` 还会受 PATH 中其它 bash 发行版（WSL 等）遮蔽，
#       解析到非 msys bash 时路径语义（/c/ vs /mnt/c/）不兼容。
#   bash → NUL 精确消费（本任务根治目标）；非 bash → 退回按行消费并**留痕**
#   （仅保留「含换行文件名会被拆」这一已知限制，不静默、也不整体放行）。

# 运行时动态识别当前任务：当前分支 task/<id> → canonical 主工作树 .orchd/_master.json
#   动态解析该任务 files_to_edit ∪ exempt_files（不再依赖生成时快照）；
#   无 task/<id> 分支时回退绑定任务与静态列表（既有 best-effort 语义 + 留痕）。
# Bound task: {task_id}
# Bound allowed files:
{files_list}

LEDGER=".orchd/_ledger.jsonl"

# 0) 当前分支 → 任务 id（动态识别；无匹配分支回退绑定任务并留痕）
# BOUND_TASK：安装期绑定任务（静态列表的归属）。3a 快路径仅在
# “分支任务 == 绑定任务”时生效——不一致时静态列表与动态真值无关，
# 预检命中会误放行（fail-open，2026-09-18 全量回归实证）。
BOUND_TASK={bound_task}
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
case "$BRANCH" in
    task/*)
        TASK_ID="${{BRANCH#task/}}"
        SCOPE_MODE=dynamic
        ;;
    "")
        TASK_ID={bound_task}
        SCOPE_MODE=bound
        echo "[orchd E020] 非 git 环境 / 无法解析当前分支 → 回退绑定任务 {task_id}（best-effort）" >&2
        ;;
    *)
        TASK_ID={bound_task}
        SCOPE_MODE=bound
        echo "[orchd E020] 分支 '$BRANCH' 非 task/<id> → 回退绑定任务 {task_id}（best-effort）" >&2
        ;;
esac

# 1) 任务未活跃 → 放行：读 ledger 判该任务是否处于活跃状态
#    （最近事件为 CLAIMED / REVIEW_CLAIMED，且无后续 DONE / RETRACT /
#      REVIEW_SUBMITTED）——in_review 阶段任务同样活跃（审查中，实现者
#      仍可能补提交，需拦截越界）
if [ -f "$LEDGER" ]; then
    LAST_TASK=$(grep -F "\\"task_id\\":\\"$TASK_ID\\"" "$LEDGER" 2>/dev/null | grep -E '"type":"(CLAIMED|REVIEW_CLAIMED|DONE|RETRACT|REVIEW_SUBMITTED)"' | tail -1)
    case "$LAST_TASK" in
        *CLAIMED*|*REVIEW_CLAIMED*)
            # 任务活跃，继续校验
            ;;
        *)
            # 任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行
            exit 0
            ;;
    esac
else
    # 无 ledger（异常环境）→ 保守放行（best-effort）
    exit 0
fi

# 2) R1-b 审查期实现者冻结：任务分支上最后 review 事件是 REVIEW_CLAIMED
#    （审查进行中，无后续 REVIEW_SUBMITTED / RETRACT）→ 拒绝提交，保护审查基线。
if [ -f "$LEDGER" ]; then
    LAST_REVIEW=$(grep -F "\\"task_id\\":\\"$TASK_ID\\"" "$LEDGER" 2>/dev/null | grep -E '"type":"(REVIEW_CLAIMED|REVIEW_SUBMITTED|RETRACT)"' | tail -1)
    case "$LAST_REVIEW" in
        *REVIEW_CLAIMED*)
            echo "orchd E017: review in progress, commit blocked on $BRANCH"
            echo "任务正在审查中（REVIEW_CLAIMED）。请等待 reviewer 提交结论，或先执行 retract 撤回审查再提交。"
            echo "To bypass: git commit --no-verify"
            exit 1
            ;;
    esac
fi

# 3) 获取 staged 文件列表（相对路径）
# task-decl-hook-delete-parity：与 done 侧 (_guard_out_of_scope → _git_diff_names)
# 对齐 diff 口径——去掉 --diff-filter=ACM，使删除(D)/重命名/复制等改动状态的全部
# staged 路径都进校验集。此前仅 ACM 会导致「删除范围外文件」在提交层零拦截，
# 只能到 done 期才被 E010 拦下（红线 #3 提交层结构性缺口）。done 侧
# _git_diff_names 无 --diff-filter（含 D），--name-only 对 R 默认显示目标路径；
# 此处同义（无 --no-renames），两侧对同一 git 改动得到同一路径集合。
# R2-9a：`-z` 不可省略——不加时 core.quotePath（默认 true）把非 ASCII / 含特殊
# 字符路径转义为八进制引号串，再被 for 按空白拆词，合法 in-scope 文件被误拦 E020
# （对照 orchd/gitops/cleanup.py::unmerged_paths 的 -z 写法）。
# task-hook-staged-nul-rootfix：NUL 分隔集合**原样落临时文件**，下方按 NUL 逐项
# 消费——不再 `tr '\\0' '\\n'` 转行（转行后含换行的文件名会被拆成两项，in-scope
# 误拦 E020）。落文件而非管道：管道开子 shell，循环内累积的 OUT_OF_SCOPE 会随之
# 丢弃；重定向在当前 shell 内消费。临时文件用后即删（trap EXIT，含异常路径）。
STAGED_TMP="${{TMPDIR:-/tmp}}/orchd-hook-staged-$$"
ALLOWED_TMP="${{TMPDIR:-/tmp}}/orchd-hook-allowed-$$"
trap 'rm -f "$STAGED_TMP" "$ALLOWED_TMP"' EXIT
trap 'exit 130' INT TERM HUP
if ! git diff --cached --name-only -z > "$STAGED_TMP" 2>/dev/null; then
    echo "[orchd E020] staged 列表获取失败（git diff 异常）→ best-effort 放行（未校验范围，禁止静默）" >&2
    exit 0
fi

# 无 staged 文件 → 放行
if [ ! -s "$STAGED_TMP" ]; then
    exit 0
fi

# staged 集合装进位置参数（3a 预检自包含装载：与第 4 步装载逐字同构，
# 独立一份——test_hook_owner_aware._revert_staged_parsing 按锚点删除
# [trap,3b) 区间复原修复前形态，3a 整体落在该区间内、无需额外适配；
# 第 4 步装载保持原位不动，反形态断言零感知）。
# 以 **NUL** 为唯一分隔符——git -z 的输出即 NUL 分隔，只有 NUL 能无歧义表达
# 「含空格 / 含换行 / 非 ASCII」的路径（沿革见第 4 步注释：R2-9a 空白拆词修、
# task-hook-staged-nul-rootfix 换行拆分修）。不用 here-doc + read（文件名即
# 注入面：here-doc 体做参数/命令替换二次展开）；不用管道 + read（子 shell 丢
# 累积量）。非 bash（dash/ash 无 `read -d`）退回按行消费并留痕。
if [ -n "${{BASH_VERSION:-}}" ]; then
    set --
    while IFS= read -r -d '' _ORCHD_ITEM; do
        set -- "$@" "$_ORCHD_ITEM"
    done < "$STAGED_TMP"
else
    echo "[orchd E020] 当前解释器非 bash（不支持 read -d ''）：staged 集合按行消费（含换行的文件名会被拆分，已知限制）" >&2
    _ORCHD_OLD_IFS=$IFS
    IFS='
'
    set -f
    set -- $(tr '\\0' '\\n' < "$STAGED_TMP")
    set +f
    IFS=$_ORCHD_OLD_IFS
fi

# 3a) 静态预检快路径（task-hook-static-precheck）：仅 dynamic 模式**且分支任务
#     == 绑定任务**。staged 全被 bound 静态列表覆盖 → 跳过 python 动态解析
#     （省一次解释器启动），静态列表落盘供第 4 步动态分支消费（判定仍走动态
#     循环——注入面守卫与历史负控制的锚点不可绕过）。
#     任一 staged 未覆盖 → 保持 dynamic 全量解析（声明可能已 amend 补登，
#     动态为准）。分支任务 ≠ 绑定任务时禁用预检：静态列表归属绑定任务，与
#     当前分支的动态真值无关，命中即误放行（fail-open）。
#     固定资产同样先行覆盖（与第 4 步 case 同表），否则每个含固定资产的提交
#     都回退动态解析，快路径永不命中。
_SC_SKIP_RESOLVER=no
if [ "$SCOPE_MODE" = "dynamic" ] && [ "$TASK_ID" = "$BOUND_TASK" ]; then
    _SC_MISS=""
    for _SC_FILE in "$@"; do
        _SC_IN=no
        case "$_SC_FILE" in
            .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md)
                _SC_IN=yes
                ;;
        esac
{_static_precheck_body}
        if [ "$_SC_IN" != "yes" ]; then _SC_MISS=yes; fi
    done
{_static_hit_action}
fi

# 3b) 正常路径：动态解析任务允许列表（仅 task/<id> 分支）；失败不静默放行
ALLOWED=""
if [ "$SCOPE_MODE" = "dynamic" ] && [ "$_SC_SKIP_RESOLVER" != "yes" ]; then
    COMMON=$(git rev-parse --git-common-dir 2>/dev/null)
    # R2-9c：解释器按可用性解析（POSIX 常见 python3，Windows/MSYS 常见 python/py），
    # 不再硬编码 python——旧实现下解释器缺失即整段动态读取失败，只能降级放行。
    ORCHD_PY=""
    for _CAND in python python3 py; do
        if command -v "$_CAND" >/dev/null 2>&1; then
            ORCHD_PY="$_CAND"
            break
        fi
    done
    if [ -z "$ORCHD_PY" ]; then
        echo "[orchd E020] 动态读取任务定义失败（task=$TASK_ID）：未找到可用 python 解释器" >&2
        echo "[orchd E020] （python / python3 / py 均不可用）→ best-effort 放行（未校验范围，禁止静默）" >&2
        exit 0
    fi
    _ALLOWED_RAW=$("$ORCHD_PY" - "$COMMON" "$TASK_ID" <<'ORCHD_HOOK_PY'
{_HOOK_PY_RESOLVER}ORCHD_HOOK_PY
)
    if [ $? -ne 0 ]; then
        echo "[orchd E020] 动态读取任务定义失败（task=$TASK_ID）→ best-effort 放行（未校验范围，禁止静默）" >&2
        exit 0
    fi
    # CRLF 兜底（Windows）：resolver 已按 LF 输出，但若宿主解释器不支持
    # reconfigure(newline) 仍有 CR 残留——命令替换只吞行尾 CRLF、行内 CR 会保留，
    # 会让带尾 CR 的允许路径与 staged 路径永不相等（in-scope 提交被误拦 E020）。
    # 此处统一剔除 CR；退出码判定仍基于 resolver 本身（不改写失败语义）。
    ALLOWED=$(printf '%s\\n' "$_ALLOWED_RAW" | tr -d '\\r')
    # 允许列表落临时文件（**不展开**形态）：此前用未加引号的 here-doc（`done <<EOF`
    # + `$ALLOWED`）消费——该形态**实测不会**对展开结果二次求值（POSIX：here-doc 体只
    # 做一趟参数 / 命令替换，终止符在展开前匹配，bash 5.3 与 dash 双解释器已验证），
    # 但把「不做二次展开」押在「展开趟数」这一实现细节上很脆弱：一旦改成 eval 或未加
    # 引号的词拆分，文件名立刻变成注入面。改为 printf 写临时文件 + 重定向消费：内容
    # 按行原样抵达比较点，与 staged 侧同一形态（不开子 shell，OUT_OF_SCOPE 累积不丢）。
    if ! printf '%s\\n' "$ALLOWED" > "$ALLOWED_TMP" 2>/dev/null; then
        echo "[orchd E020] 允许列表落盘失败（task=$TASK_ID）→ best-effort 放行（未校验范围，禁止静默）" >&2
        exit 0
    fi
fi

# 4) 校验每个 staged 文件：固定资产豁免 或 任务范围（动态 / 回退静态）。
#    "$@" 由 STAGED_TMP 装载（NUL 精确消费：R2-9a 空白拆词修、
#    task-hook-staged-nul-rootfix 换行拆分修；不用 here-doc + read——文件名含
#    $() 会被执行（文件名即注入面）；不用管道 + read——子 shell 丢累积量。
#    非 bash（dash/ash 无 `read -d`）退回按行消费并留痕）。
if [ -n "${{BASH_VERSION:-}}" ]; then
    set --
    while IFS= read -r -d '' _ORCHD_ITEM; do
        set -- "$@" "$_ORCHD_ITEM"
    done < "$STAGED_TMP"
else
    echo "[orchd E020] 当前解释器非 bash（不支持 read -d ''）：staged 集合按行消费（含换行的文件名会被拆分，已知限制）" >&2
    _ORCHD_OLD_IFS=$IFS
    IFS='
'
    set -f
    set -- $(tr '\\0' '\\n' < "$STAGED_TMP")
    set +f
    IFS=$_ORCHD_OLD_IFS
fi
OUT_OF_SCOPE=""
for FILE in "$@"; do
    IN_SCOPE=no
    # 固定资产豁免（引擎自动提交路径，不在任务 files_to_edit 内）：
    # .orchd/_master.json、IDEAS.md（根布局）与 .orchd/IDEAS.md（发布态自包含
    # .orchd 布局）、ROADMAP.md（宿主项目根，唯一源）——
    # amend 在 main 分支提交它们，若不豁免会被本 hook 拦截（引擎自动提交零改动）。
    # 注：.orchd/ROADMAP.md 已非合法形态（ROADMAP 唯一源为宿主根），不列入豁免。
    case "$FILE" in
        .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md)
            IN_SCOPE=yes
            ;;
    esac
    if [ "$IN_SCOPE" != "yes" ]; then
        if [ "$SCOPE_MODE" = "dynamic" ]; then
            # 目录式声明感知：逐行判定，精确相等或目录前缀匹配
            # here-doc 的 EOF 必须在行首（无缩进），否则 shell 不识别
            while IFS= read -r AP; do
                if [ -n "$AP" ]; then
                    if [ "$FILE" = "$AP" ]; then IN_SCOPE=yes; break; fi
                    # R2-9b：仅目录式声明（以 / 结尾）做前缀匹配，文件声明只认
                    # 精确相等——与静态路径 case 模板、pool._is_path_covered 同边界，
                    # 堵住「声明 README.md 却放行 README.md.evil」的裸前缀误放行。
                    case "$AP" in
                        */)
                            case "$FILE" in "$AP"*) IN_SCOPE=yes; break ;; esac
                            ;;
                    esac
                fi
            done < "$ALLOWED_TMP"
        else
{files_check}
{exempts_check}
        fi
    fi
    if [ "$IN_SCOPE" != "yes" ]; then
        OUT_OF_SCOPE="$OUT_OF_SCOPE$FILE "
    fi
done

# 5) 有越界文件 → 拒绝提交
if [ -n "$OUT_OF_SCOPE" ]; then
    echo "orchd E020: out-of-scope commit blocked (task $TASK_ID active, mode=$SCOPE_MODE)"
    echo "Out-of-scope files:"
    for F in $OUT_OF_SCOPE; do
        echo "  - $F"
    done
    echo ""
    echo "Allowed files for this task:"
    if [ "$SCOPE_MODE" = "dynamic" ]; then
        printf '%s\\n' "$ALLOWED" | while IFS= read -r AF; do
            if [ -n "$AF" ]; then echo "  - $AF"; fi
        done
    else
{static_allowed_echo}
{static_exempt_header}{static_exempt_echo}
    fi
    echo ""
    echo ""
{_e020_hook_escape_block()}
    echo "To bypass: git commit --no-verify"
    exit 1
fi

exit 0
"""
    # 写入 hook 文件（newline="\n"：Windows 文本模式会把脚本写成 CRLF，虽多数
    # MSYS sh 容忍，但 here-doc 终止符 / 静态列表行同样被 CR 污染，属隐患——统一 LF，
    # 与 git 官方对 hook 脚本的可移植性建议一致）。
    hook_path = hooks_dir / _HOOK_FILENAME
    # R2-3 owner-aware：目标 pre-commit 已存在且非本引擎生成（无 orchd 标记头，
    # 典型为宿主自有的 husky / lint-staged hook，或 core.hooksPath 指向用户目录）
    # → 不覆盖、不删除，返回结构化告警；宿主 hook 的完整性与执行权归宿主。
    if hook_path.exists() and not _is_orchd_hook(hook_path):
        result = {
            "installed": False,
            "reason": "foreign_hook_present",
            "path": str(hook_path),
            "hooks_path": hooks_path,
            "hint": (
                f"{hook_path} 已存在且不含 orchd 标记头"
                f"（{_HOOK_MARKER}）——判定为宿主自有 hook，未覆盖；"
                "如需 orchd 越界拦截与之共存，请手动在该 hook 中调用 orchd 校验，"
                f"或先移除/改名该文件再执行 orchd claim"
            ),
        }
        _log_hook_skip("install_skipped_foreign_hook", result)
        return result
    # 生成物完整性自检（task-hook-content-guard）：写盘前统一校验 NUL / CR /
    # shebang / 归属标记，不通过即**拒绝写入**——不落半截生成物、不覆盖已有 hook。
    # 背景：此前写入路径只强制 LF（newline="\n"），缺陷生成物（如 f-string 内
    # `tr '\0' '\n'` 未转义产出的真实控制字符）会一路写到磁盘，只能靠测试侧
    # `bash -n` 事后暴露；宿主 hook 归属判定先于此检查（宿主文件完整性优先）。
    check = _validate_hook_content(hook_content)
    if not check["ok"]:
        result = {
            "installed": False,
            "reason": "invalid_generated_hook",
            "path": str(hook_path),
            "error": check.get("error", "生成物自检未通过"),
            "check": check,
        }
        _log_hook_skip("install_rejected_invalid_content", result)
        return result
    try:
        _write_hook_file(hook_path, hook_content)
    except (OSError, IOError) as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}
    # 写入后内容回读：此前落盘层只有「只读首部归属标记」的 _is_orchd_hook，无内容级
    # 回读——编码 / 换行转换类事故在落盘层即可被拦住，不必等到测试侧语法门禁。
    # read_bytes + decode（不走 read_text 的通用换行翻译）才能如实反映 CR 残留。
    try:
        written = hook_path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}
    written_check = _validate_hook_content(written)
    if not written_check["ok"]:
        result = {
            "installed": False,
            "reason": "invalid_written_hook",
            "path": str(hook_path),
            "error": written_check.get("error", "落盘内容与生成物不一致"),
            "check": written_check,
        }
        _log_hook_skip("install_rejected_written_content", result)
        return result
    return {"installed": True, "path": str(hook_path), "hooks_path": hooks_path}


def hook_uninstall(project_root: Path) -> dict[str, Any]:
    """删除 pre-commit hook（best-effort，owner-aware：只删本引擎生成物）。

    Args:
        project_root: git 仓库根目录。

    Returns:
        结构化结果，永不抛异常：
            - ``{"uninstalled": True, "reason": "removed"}`` hook 已删除。
            - ``{"uninstalled": True, "reason": "not_exists"}`` hook 本就不存在（幂等）。
            - ``{"uninstalled": False, "reason": "foreign_hook_present", "path": <str>,
              "hint": <str>}`` 目标 pre-commit 不含 orchd 标记头（宿主自有 hook）
              → **不删除**，附不删原因（保护宿主文件）。
            - ``{"uninstalled": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    hook_path = _get_hook_path(project_root)
    if not hook_path.exists():
        return {"uninstalled": True, "reason": "not_exists"}
    # R2-3 owner-aware：只删除本引擎生成物（含 orchd 标记头）。宿主自有 hook
    # （husky / lint-staged / 手写）一律不动，返回不删原因。
    if not _is_orchd_hook(hook_path):
        result = {
            "uninstalled": False,
            "reason": "foreign_hook_present",
            "path": str(hook_path),
            "hint": (
                f"{hook_path} 不含 orchd 标记头（{_HOOK_MARKER}）——判定为宿主自有 "
                "hook，未删除（保护宿主文件）"
            ),
        }
        return result
    try:
        _safe_delete(hook_path, project_root)
        return {"uninstalled": True, "reason": "removed"}
    except (OSError, IOError) as exc:
        return {"uninstalled": False, "reason": "io_error", "error": str(exc)}
