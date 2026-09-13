"""gitops hook 域：pre-commit hook 安装/卸载（叶子模块，零同包依赖）。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orchd.gitops._run import _run_git, _shell_quote
from orchd.gitops.cleanup import _safe_delete


_HOOK_FILENAME = "pre-commit"
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
    ``ROADMAP.md`` / ``.orchd/ROADMAP.md``）语义不变；``--no-verify`` 可绕过。

    安装前先调用 :func:`_ensure_hooks_path` 确保仓库自带 ``.githooks/`` 已启用
    （``core.hooksPath`` 是本地配置，不随 clone 传播）：仓库内存在 ``.githooks/``
    且配置未设置时，pre-commit 会落到该目录而非回退 ``.git/hooks``；既有 hooksPath
    解析语义（相对 / 绝对 / 缺省回退）不变。

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
# orchd L3 pre-commit hook (auto-generated, do not edit)
# 运行时动态识别当前任务：当前分支 task/<id> → canonical 主工作树 .orchd/_master.json
#   动态解析该任务 files_to_edit ∪ exempt_files（不再依赖生成时快照）；
#   无 task/<id> 分支时回退绑定任务与静态列表（既有 best-effort 语义 + 留痕）。
# Bound task: {task_id}
# Bound allowed files:
{files_list}

LEDGER=".orchd/_ledger.jsonl"

# 0) 当前分支 → 任务 id（动态识别；无匹配分支回退绑定任务并留痕）
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
STAGED=$(git diff --cached --name-only)

# 无 staged 文件 → 放行
if [ -z "$STAGED" ]; then
    exit 0
fi

# 3b) 正常路径：动态解析任务允许列表（仅 task/<id> 分支）；失败不静默放行
ALLOWED=""
if [ "$SCOPE_MODE" = "dynamic" ]; then
    COMMON=$(git rev-parse --git-common-dir 2>/dev/null)
    _ALLOWED_RAW=$(python - "$COMMON" "$TASK_ID" <<'ORCHD_HOOK_PY'
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
fi

# 4) 校验每个 staged 文件：固定资产豁免 或 任务范围（动态 / 回退静态）
OUT_OF_SCOPE=""
for FILE in $STAGED; do
    IN_SCOPE=no
    # 固定资产豁免（引擎自动提交路径，不在任务 files_to_edit 内）：
    # .orchd/_master.json、IDEAS.md（根布局）与 .orchd/IDEAS.md（发布态自包含
    # .orchd 布局）、ROADMAP.md（根布局）与 .orchd/ROADMAP.md（发布态）——
    # amend 在 main 分支提交它们，若不豁免会被本 hook 拦截（引擎自动提交零改动）。
    case "$FILE" in
        .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md|.orchd/ROADMAP.md)
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
                    case "$FILE" in "$AP"*) IN_SCOPE=yes; break ;; esac
                fi
            done <<EOF
$ALLOWED
EOF
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
    try:
        hook_path.write_text(hook_content, encoding="utf-8", newline="\n")
        hook_path.chmod(0o755)
        return {"installed": True, "path": str(hook_path), "hooks_path": hooks_path}
    except (OSError, IOError) as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}


def hook_uninstall(project_root: Path) -> dict[str, Any]:
    """删除 pre-commit hook（best-effort）。

    Args:
        project_root: git 仓库根目录。

    Returns:
        结构化结果，永不抛异常：
            - ``{"uninstalled": True, "reason": "removed"}`` hook 已删除。
            - ``{"uninstalled": True, "reason": "not_exists"}`` hook 本就不存在（幂等）。
            - ``{"uninstalled": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    hook_path = _get_hook_path(project_root)
    if not hook_path.exists():
        return {"uninstalled": True, "reason": "not_exists"}
    try:
        _safe_delete(hook_path, project_root)
        return {"uninstalled": True, "reason": "removed"}
    except (OSError, IOError) as exc:
        return {"uninstalled": False, "reason": "io_error", "error": str(exc)}
