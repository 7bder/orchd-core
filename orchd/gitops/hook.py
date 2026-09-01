"""gitops hook 域：pre-commit hook 安装/卸载（叶子模块，零同包依赖）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.gitops._run import _shell_quote
from orchd.gitops.cleanup import _safe_delete


_HOOK_FILENAME = "pre-commit"


def _e020_hook_escape_block() -> str:
    """生成 E020 越界提交的 hook 逃生文本（与 guide.py E020 recovery 同源）。

    单一生成函数：guide.py ERROR_GUIDANCE["E020"] 为权威来源，hook 文本
    从其 recovery/command/exit_type 派生，避免双写漂移。
    逃生步骤覆盖：查看被拦文件、移出暂存区、amend 补声明、红线命令警告。
    """
    try:
        from orchd.guide import ERROR_GUIDANCE
        e020 = ERROR_GUIDANCE.get("E020", {})
        recovery = e020.get("recovery", "范围外提交：只改 files_to_edit 声明文件")
        command = e020.get("command", "git status")
        exit_type = e020.get("exit_type", "git-diagnose")
    except Exception:
        recovery = "范围外提交：只改 files_to_edit 声明文件"
        command = "git status"
        exit_type = "git-diagnose"

    lines = [
        f'    echo "E020 recovery: {recovery}"',
        f'    echo "E020 exit_type: {exit_type}"',
        f'    echo "E020 diagnostic: {command}"',
        '    echo ""',
        '    echo "=== 合规逃生步骤 ==="',
        '    echo "1. 查看被拦文件: git diff --cached --name-only"',
        '    echo "2. 移出暂存区(保留工作区): git restore --staged <file>"',
        '    echo "3. 确属本任务: orchd amend --task <id> --files-to-edit <file>"',
        '    echo "4. 豁免类(测试/文档): orchd amend --task <id> --exempt-files <file>"',
        '    echo "5. 重新提交: git commit -m \"...\""',
        '    echo ""',
        '    echo "=== 红线命令(禁止,会丢失未提交工作) ==="',
        '    echo "  X git reset --hard  (丢弃工作区+暂存区)"',
        '    echo "  X git clean -fdx    (删除未跟踪文件,含.orchd运行时)"',
        '    echo "  X git checkout -- . (丢弃所有未提交改动)"',
        '    echo "  绕过仅限固定资产: git commit --no-verify"',
    ]
    return "\n".join(lines)


def _get_hook_path(project_root: Path) -> Path:
    """返回 .git/hooks/pre-commit 路径。"""
    return project_root / ".git" / "hooks" / _HOOK_FILENAME


def hook_install(
    project_root: Path,
    task_id: str,
    files_to_edit: list[str],
    exempt_files: list[str] | None = None,
) -> dict[str, Any]:
    """安装 pre-commit hook（任务活跃期越界提交拦截）。

    Hook 逻辑（2026-08-08 增强：由"任务分支校验"升级为"任务活跃校验"）：
    - 读 ledger 判断任务是否活跃（该 task_id 最近事件为 CLAIMED，且无后续
      DONE / RETRACT / REVIEW_SUBMITTED）→ 任务未活跃 → 放行（exit 0）
    - 任务活跃 → **任何分支**都校验 staged 文件 ⊆ files_to_edit
      （堵住任务活跃期间在 main / 幽灵分支越界提交实现内容的事故：
      b3c2e84 直接在 main 提交、task/task-1 幽灵分支）
    - 固定资产豁免：.orchd/_master.json、IDEAS.md 与 .orchd/IDEAS.md
      （amend 自动提交的路径，cli.py ensure_committed 在 main 执行，
      若不豁免会被自身 hook 拦截）
    - R1-b 审查期实现者冻结：任务分支上 REVIEW_CLAIMED 且无后续结论 → 拒绝
    - 越界 → 拒绝提交（exit 1），打印越界文件清单
    - --no-verify 可绕过（git 原生行为，hook 无需特殊处理）

    Args:
        project_root: git 仓库根目录。
        task_id: 当前任务 ID（绑定到 hook 内容）。
        files_to_edit: 允许修改的文件列表（相对路径）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"installed": True, "path": <str>}`` hook 安装成功。
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

    hooks_dir = project_root / ".git" / "hooks"
    if not hooks_dir.parent.exists():
        return {"installed": False, "reason": "not_a_git_repo"}

    exempt = exempt_files or []

    # 生成 hook 脚本内容（使用纯 shell 逻辑，避免 JSON 解析复杂性）
    files_list = "\n".join(f"#   {f}" for f in files_to_edit)
    if exempt:
        files_list += "\n# Exempt files:"
        files_list += "\n" + "\n".join(f"#   {f}" for f in exempt)
    # 文件名用单引号转义（_shell_quote，防 shell 注入；
    # 既有双引号注入面一并收敛）
    files_check = "\n".join(
        f'    if [ "$FILE" = {_shell_quote(f)} ]; then IN_SCOPE=yes; fi'
        for f in files_to_edit
    )
    exempts_check = "\n".join(
        f'    if [ "$FILE" = {_shell_quote(f)} ]; then IN_SCOPE=yes; fi'
        for f in exempt
    )
    # 无豁免时不输出 Exempt files 标题行（保持与无 exempt_files 行为一致）
    exempt_header = (
        '    echo "Exempt files for this task:"\n' if exempt else ""
    )

    hook_content = f"""#!/bin/sh
# orchd L3 pre-commit hook (auto-generated, do not edit)
# Task: {task_id}
# Allowed files:
{files_list}

LEDGER=".orchd/_ledger.jsonl"

# 1) 任务未活跃 → 放行：读 ledger 判该任务是否处于活跃状态
#    （最近事件为 CLAIMED / REVIEW_CLAIMED，且无后续 DONE / RETRACT /
#      REVIEW_SUBMITTED）——in_review 阶段任务同样活跃（审查中，实现者
#      仍可能补提交，需拦截越界）
if [ -f "$LEDGER" ]; then
    LAST_TASK=$(grep -F '"task_id":"{task_id}"' "$LEDGER" 2>/dev/null | grep -E '"type":"(CLAIMED|REVIEW_CLAIMED|DONE|RETRACT|REVIEW_SUBMITTED)"' | tail -1)
    case "$LAST_TASK" in
        *CLAIMED*|*REVIEW_CLAIMED*)
            # 任务活跃，继续校验（任何分支）
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
    LAST_REVIEW=$(grep -F '"task_id":"{task_id}"' "$LEDGER" 2>/dev/null | grep -E '"type":"(REVIEW_CLAIMED|REVIEW_SUBMITTED|RETRACT)"' | tail -1)
    case "$LAST_REVIEW" in
        *REVIEW_CLAIMED*)
            echo "orchd E017: review in progress, commit blocked on task/{task_id}"
            echo "任务正在审查中（REVIEW_CLAIMED）。请等待 reviewer 提交结论，或先执行 retract 撤回审查再提交。"
            echo "To bypass: git commit --no-verify"
            exit 1
            ;;
    esac
fi

# 3) 获取 staged 文件列表（相对路径）
STAGED=$(git diff --cached --name-only --diff-filter=ACM)

# 无 staged 文件 → 放行
if [ -z "$STAGED" ]; then
    exit 0
fi

# 4) 校验每个 staged 文件：固定资产豁免 或 在允许列表内
    OUT_OF_SCOPE=""
    for FILE in $STAGED; do
        IN_SCOPE=no
        # 固定资产豁免（引擎自动提交路径，不在任务 files_to_edit 内）：
        # .orchd/_master.json、IDEAS.md（根布局）与 .orchd/IDEAS.md（发布态自包含
        # .orchd 布局）、ROADMAP.md（根布局）与 .orchd/ROADMAP.md（发布态）——
        # amend 在 main 分支提交它们，若不豁免会被本 hook 拦截
        # （引擎自动提交零改动）。
        case "$FILE" in
            .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md|.orchd/ROADMAP.md)
                IN_SCOPE=yes
                ;;
        esac
{files_check}
{exempts_check}
    if [ "$IN_SCOPE" != "yes" ]; then
        OUT_OF_SCOPE="$OUT_OF_SCOPE$FILE "
    fi
done

# 5) 有越界文件 → 拒绝提交
if [ -n "$OUT_OF_SCOPE" ]; then
    echo "orchd E020: out-of-scope commit blocked (task {task_id} active)"
    echo "Out-of-scope files:"
    for F in $OUT_OF_SCOPE; do
        echo "  - $F"
    done
    echo ""
    echo "Allowed files for this task:"
{chr(10).join(f'    echo "  - {f}"' for f in files_to_edit)}
{exempt_header}{chr(10).join(f'    echo "  - {f}"' for f in exempt)}
    echo ""
    echo ""
{_e020_hook_escape_block()}
    echo "To bypass: git commit --no-verify"
    exit 1
fi

exit 0
"""
    # 写入 hook 文件
    hook_path = hooks_dir / _HOOK_FILENAME
    try:
        hook_path.write_text(hook_content, encoding="utf-8")
        hook_path.chmod(0o755)
        return {"installed": True, "path": str(hook_path)}
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