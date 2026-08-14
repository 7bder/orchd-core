"""Orchd 引擎 best-effort git 提交能力（叶子模块，零 orchd 内部依赖）。

职责：在 ``done`` / ``amend`` 成功后，把实现者漏提交的改动兜底提交到
当前分支，把"提交纪律"从约定层升级为引擎兜底。与 ``onboard._try_git_branch`` /
``_try_git_merge`` 保持同一 best-effort 语义：任何失败（非 git 仓库、
git 不可用、提交失败）都不抛异常，仅返回结构化结果，由调用方放入
响应字段（对齐 ``review_submit`` 的 ``merged:false`` 契约）。

安全约束：
- 只对调用方显式声明的路径执行 ``git add``，且 ``git commit -- <paths>``
  同样限定路径——即使实现者预先 staged 了范围外文件，也不会被本模块提交；
- 绝不执行 ``git push``（远端推送归管理员）；
- 绝不新增 ledger 事件（事件格式与状态机零改动）。

依赖方向：gitops.py → 标准库（shutil / subprocess / pathlib）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

# 单条 git 命令超时（秒），与 onboard.py 的 git 辅助保持一致量级
_GIT_TIMEOUT = 10

# git 输出统一按 UTF-8 解码（git 内部以 UTF-8 处理 commit message 等），
# 解码失败按替换符处理——避免中文 Windows 默认 GBK 代码页解码崩溃。
_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"


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
    """shell 单引号转义：' 替换为 '\\''（单引号闭合-转义-重开）。

    用于把文件名安全嵌入 shell 脚本字面量，防 shell 注入
    （hook 模板中文件名来自任务定义，属可信输入，但仍按防御性处理）。
    """
    return "'" + value.replace("'", "'\\''") + "'"


def get_current_branch(project_root: Path) -> str | None:
    """获取当前 git 分支名。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。
    """
    try:
        result = _run_git(project_root, ["branch", "--show-current"])
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_head_commit(project_root: Path) -> str | None:
    """获取当前 HEAD 的 commit SHA（用于 review baseline 追踪）。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。
    """
    try:
        result = _run_git(project_root, ["rev-parse", "HEAD"])
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_default_branch(project_root: Path) -> str | None:
    """检测仓库的默认分支名（best-effort）。

    优先级：
    1. ``git config init.defaultBranch``（用户显式配置）
    2. 本地存在 ``main`` 分支
    3. 本地存在 ``master`` 分支
    4. 都没有返回 None

    非 git 仓库、git 不可用或任何异常返回 None。
    """
    try:
        # 1. 显式配置优先
        cfg = _run_git(project_root, ["config", "--get", "init.defaultBranch"])
        if cfg.returncode == 0:
            name = cfg.stdout.strip()
            if name:
                return name
        # 2. 探测本地常见默认分支名
        for candidate in ("main", "master"):
            check = _run_git(project_root, ["rev-parse", "--verify", "--quiet", candidate])
            if check.returncode == 0:
                return candidate
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def check_workspace_state(project_root: Path) -> dict[str, Any]:
    """检查当前 git 工作区状态：分支名 + 干净度（best-effort）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"available": False}`` 非 git 仓库 / git 不可用 / 异常
              （调用方应静默降级，沿用 best-effort 契约）。
            - ``{"available": True, "branch": <str|None>, "clean": <bool>}``
              branch 为当前分支名（detached HEAD 时为 None）；
              clean 表示无已跟踪文件改动（untracked 文件不视为脏，
              与"工作区干净 = 无已跟踪文件改动"的约定一致）。
    """
    if shutil.which("git") is None:
        return {"available": False}
    try:
        check = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {"available": False}
        branch = get_current_branch(project_root)
        # 已跟踪文件改动（不含 untracked）：--porcelain 输出非空即脏
        status = _run_git(project_root, ["status", "--porcelain", "--untracked-files=no"])
        clean = status.returncode == 0 and not status.stdout.strip()
        return {"available": True, "branch": branch, "clean": clean}
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return {"available": False}


def ensure_committed(
    project_root: Path,
    paths: list[str],
    message: str,
) -> dict[str, Any]:
    """best-effort 将 ``paths`` 范围内的未提交改动提交到当前分支。

    Args:
        project_root: 仓库根目录（git 命令的 cwd）。
        paths: 允许提交的路径列表（相对 project_root 或绝对路径）。
            调用方须传入任务声明的 ``files_to_edit`` 或固定资产路径。
        message: 提交消息。

    Returns:
        结构化结果，永不抛异常：
            - ``{"performed": True, "reason": "committed", "message": message}``
              引擎实际创建了一个提交。
            - ``{"performed": False, "reason": "no_changes", "message": message}``
              范围内无未提交改动（实现者已自行提交、路径不存在或本就无改动），
              无需提交。
            - ``{"performed": False, "reason": "not_a_git_repo"}``
              当前目录不是 git 工作树。
            - ``{"performed": False, "reason": "git_unavailable"}``
              git 可执行文件不在 PATH。
            - ``{"performed": False, "reason": "commit_failed", "message": <stderr>}``
              git diff / commit 失败（如 user 未配置、模拟异常）。

    Raises:
        永不抛异常；所有 git 失败均降级为结构化结果。
    """
    if not paths:
        return {"performed": False, "reason": "no_paths"}

    if shutil.which("git") is None:
        return {"performed": False, "reason": "git_unavailable"}

    try:
        check = _run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if check.returncode != 0:
            return {"performed": False, "reason": "not_a_git_repo"}
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "not_a_git_repo"}

    # 只 add 声明范围。add 失败（如路径不存在）不阻断：
    # 是否真的"无改动"由下一步 diff 精确判断（diff 也限定 paths）。
    try:
        _run_git(project_root, ["add", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # 范围内是否有 staged 改动：0=无，1=有，其余为 git 错误
    try:
        diff = _run_git(project_root, ["diff", "--cached", "--quiet", "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git diff failed"}
    if diff.returncode == 0:
        return {"performed": False, "reason": "no_changes", "message": message}
    if diff.returncode != 1:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (diff.stderr or diff.stdout).strip()[:300] or "git diff --cached failed",
        }

    # commit 同样限定 paths：不提交声明范围外的 staged 内容，不 push
    try:
        commit = _run_git(project_root, ["commit", "-m", message, "--", *paths])
    except (subprocess.SubprocessError, FileNotFoundError):
        return {"performed": False, "reason": "commit_failed", "message": "git commit failed"}
    if commit.returncode != 0:
        return {
            "performed": False,
            "reason": "commit_failed",
            "message": (commit.stderr or commit.stdout).strip()[:300] or "git commit failed",
        }
    return {"performed": True, "reason": "committed", "message": message}


# ─────────────────────────────────────────────────────────────────────────────
# L3 pre-commit hook 越界提交拦截
# ─────────────────────────────────────────────────────────────────────────────

_HOOK_FILENAME = "pre-commit"


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
    """
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
    # .orchd 布局）——amend 在 main 分支提交它们，若不豁免会被本 hook 拦截
    # （引擎自动提交零改动）。
    case "$FILE" in
        .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md)
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
    echo "To bypass: git commit --no-verify"
    exit 1
fi

exit 0
"""

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = _get_hook_path(project_root)
        hook_path.write_text(hook_content, encoding="utf-8")
        # 设置可执行权限（POSIX）
        if hasattr(hook_path, "chmod"):
            hook_path.chmod(0o755)
        return {"installed": True, "path": str(hook_path)}
    except (OSError, IOError) as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}


def hook_uninstall(project_root: Path) -> dict[str, Any]:
    """卸载 pre-commit hook（幂等：hook 不存在时不报错）。

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


def _safe_delete(path: Path, base_dir: Path) -> None:
    """沙箱安全的文件删除（unlink 被劫持时降级为重命名移出）。

    部分沙箱把 ``Path.unlink`` 劫持为"移入回收站"（safe-delete），回收站不可用时
    FAIL_CLOSED 抛 OSError（2026-08-06 实踩：windows-sandbox-recycle-bin-unavailable，
    全量 pytest 稳定触发）。此时降级为把文件重命名移动到系统临时目录
    （重命名不经删除劫持），目标位置"消失"，语义等效删除；
    残留物在系统 temp（orchd-trash-*），可手动清理，不影响工作区。

    Args:
        path: 待删除文件。
        base_dir: 用于生成唯一残留名的基准目录名。

    Raises:
        OSError: unlink 与降级重命名均失败时向上抛（由调用方 best-effort 降级）。
    """
    try:
        path.unlink()
    except OSError:
        dest = Path(tempfile.gettempdir()) / (
            f"orchd-trash-{base_dir.name}-{uuid.uuid4().hex[:8]}-{path.name}"
        )
        os.replace(path, dest)


# ------------------------------------------------------------------
# L2 session 工作区锁（2026-08-06 task-l2-session-lock）
# ------------------------------------------------------------------

_SESSION_LOCK_FILENAME = ".session.lock"
# 默认锁超时（分钟），watchdog 超时自动释放僵死锁
_SESSION_LOCK_TIMEOUT_MIN = 60


def _get_session_lock_path(orchd_dir: Path) -> Path:
    """返回 session lock 文件路径。"""
    return orchd_dir / _SESSION_LOCK_FILENAME


def session_lock_acquire(
    orchd_dir: Path,
    agent_id: str,
    branch: str | None = None,
) -> dict[str, Any]:
    """写入 session lock 文件（agent_id + branch + timestamp）。

    Args:
        orchd_dir: .orchd 目录路径。
        agent_id: 当前 session 的 agent ID。
        branch: 当前 git 分支名（可选，None 表示非 git 或 detached HEAD）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"acquired": True, "path": <str>}`` 锁文件写入成功。
            - ``{"acquired": False, "reason": "io_error", "error": <str>}``
              写入失败（best-effort 降级，不阻塞状态机）。

    Note:
        调用方应先调用 ``session_lock_check`` 确认无其他 session 持有锁，
        再调用本函数。本函数不校验锁是否已存在（覆盖写入）。
    """
    import json
    from datetime import datetime, timezone

    lock_path = _get_session_lock_path(orchd_dir)
    lock_data = {
        "agent_id": agent_id,
        "branch": branch,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        lock_path.write_text(json.dumps(lock_data, ensure_ascii=False), encoding="utf-8")
        return {"acquired": True, "path": str(lock_path)}
    except (OSError, IOError) as exc:
        return {"acquired": False, "reason": "io_error", "error": str(exc)}


def session_lock_release(orchd_dir: Path) -> dict[str, Any]:
    """释放 session lock（幂等：锁文件不存在时不报错）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"released": True, "reason": "removed"}`` 锁文件已删除。
            - ``{"released": True, "reason": "not_exists"}`` 锁文件本就不存在（幂等）。
            - ``{"released": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    lock_path = _get_session_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"released": True, "reason": "not_exists"}
    try:
        _safe_delete(lock_path, orchd_dir)
        return {"released": True, "reason": "removed"}
    except (OSError, IOError) as exc:
        return {"released": False, "reason": "io_error", "error": str(exc)}


def session_lock_check(
    orchd_dir: Path,
    timeout_min: int = _SESSION_LOCK_TIMEOUT_MIN,
) -> dict[str, Any]:
    """检查 session lock 状态：是否存在、是否超时、内容是否合法。

    Args:
        orchd_dir: .orchd 目录路径。
        timeout_min: 超时分钟数（默认 60）。超时视为僵死锁，可覆盖。

    Returns:
        结构化结果，永不抛异常：
            - ``{"locked": False}`` 无锁文件 / 锁已超时 / 锁文件损坏（可覆盖）。
            - ``{"locked": True, "agent_id": <str>, "branch": <str|None>,
                 "timestamp": <str>, "age_min": <float>}``
               锁有效且未超时，调用方应拒绝写入（E019 workspace_busy）。

    Note:
        锁文件损坏（JSON 解析失败、缺少必要字段）视为可覆盖（容错），
        返回 ``{"locked": False, "reason": "corrupted"}``。
    """
    import json
    from datetime import datetime, timezone

    lock_path = _get_session_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"locked": False}

    try:
        content = lock_path.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, IOError, json.JSONDecodeError):
        # 锁文件损坏：视为可覆盖
        return {"locked": False, "reason": "corrupted"}

    # 校验必要字段
    agent_id = data.get("agent_id")
    timestamp_str = data.get("timestamp")
    if not agent_id or not timestamp_str:
        return {"locked": False, "reason": "corrupted"}

    # 解析时间戳
    try:
        lock_time = datetime.fromisoformat(timestamp_str)
        if lock_time.tzinfo is None:
            # 兼容无时区的时间戳（视为 UTC）
            lock_time = lock_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return {"locked": False, "reason": "corrupted"}

    # 计算锁年龄
    now = datetime.now(timezone.utc)
    age_seconds = (now - lock_time).total_seconds()
    age_min = age_seconds / 60.0

    # 超时视为僵死锁，可覆盖
    if age_min >= timeout_min:
        return {"locked": False, "reason": "timeout", "age_min": age_min}

    # 锁有效且未超时
    return {
        "locked": True,
        "agent_id": agent_id,
        "branch": data.get("branch"),
        "timestamp": timestamp_str,
        "age_min": age_min,
    }
