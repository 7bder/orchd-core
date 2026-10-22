"""gitops guard — 工作树路径恢复域（W6-C1 拆分，纯搬移）。

来源：``orchd/gitops/guard.py``（1652 行，W6-C1 纯搬移）。

``restore_worktree_paths`` / ``_count_git_processes`` /
``_restore_refusal_reason``。

依赖 ``core``（``_clear_timeout_index_lock``，顶层导入）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

from orchd.gitops._const import (
    _GIT_CHECKOUT_TIMEOUT,
    _GIT_ENCODING,
    _GIT_ERRORS,
    _GIT_TIMEOUT,
)

from .core import _clear_timeout_index_lock

def _count_git_processes() -> int | None:
    """当前系统 git 进程数（task-ref-tx-hook-cost；best-effort）。

    checkout 超时多由并发 git 负载（回归测试夹具 / 并行会话）引起，
    而失败信息此前无任何环境维度，agent 只能从零自查。失败返回 None
    （不阻断），成功返回非负整数。只读探测，不触碰任何进程。
    """
    import sys as _sys

    try:
        if _sys.platform == "win32":
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=10,
            )
            if proc.returncode != 0:
                return None
            out = (proc.stdout or b"").decode("utf-8", errors="replace")
            return sum(
                1 for ln in out.splitlines()
                if ln.strip().strip('"').lower().startswith("git.exe")
            )
        proc = subprocess.run(
            ["pgrep", "-c", "-x", "git"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return int((proc.stdout or b"").decode("utf-8", errors="replace").strip() or 0)
    except Exception:
        return None

def restore_worktree_paths(
    project_root: Path, paths: list[str]
) -> dict[str, Any]:
    """受管工作树还原（task-restore-channel）：指定路径回到 HEAD，不碰历史。

    红线 #1 受管出口（与 commit / merge-main 精确形态同族）：agent 把"写文件"与
    "需要干净工作区的命令"排进同一批导致脏工作区拒绝（E017）时，用本通道逐字
    还原，而不必走原生 ``git checkout --``（红线禁止，需审批）。

    逐路径门禁（任一失败即整体 E007，一个也不执行——先验后做）：
    - 路径逃逸仓库根 → 拒绝（路径穿越）；
    - 目录 → 拒绝（只接受文件，防 ``--path .`` 级误伤）；
    - 无 HEAD 版本（未跟踪新建 / 已暂存新文件）→ 拒绝（无可还原目标；
      未跟踪文件的删除不在本通道内，请走审批后手动处置）。

    执行：单次 ``git checkout HEAD -- <paths>``（原子语义）；失败 → E007
    （fail-closed，可安全重试）。

    Args:
        project_root: 仓库根目录（git 命令 cwd）。
        paths: 仓库根相对路径列表（非空）。

    Returns:
        ``{"restored": [...], "project_root": str}``。

    Raises:
        OrchdError(E007): 空路径表 / 非 git 仓库 / 任一路径被拒 / 执行失败。
    """
    root = Path(project_root).resolve()
    # 路径按 posix 归一化（git 的 HEAD:path 形态要求正斜杠；Windows 反斜杠亦可传入）。
    # 绝对路径一律拒绝（必须相对仓库根，避免歧义）。
    raw = [str(p).replace("\\", "/").strip() for p in (paths or [])]
    if any(Path(r).is_absolute() for r in raw if r):
        raise OrchdError(
            ErrorCode.E007,
            "restore_refused: 路径须相对仓库根，拒绝绝对路径",
            [{"hint": "用法：orchd restore --path <相对仓库根的文件路径>..."}],
        )
    rels = [r.strip("/") for r in raw]
    rels = [r for r in rels if r and r != "."]
    if not rels:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_usage: restore 需要至少一个 --path 文件路径",
            [{"hint": "用法：orchd restore --path <相对仓库根的文件路径>..."}],
        )
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True, encoding=_GIT_ENCODING, errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_unavailable: git 不可用：{exc}",
            [{"hint": "确认目录是有效 git 仓库后重试"}],
        ) from exc
    if inside.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            "restore_unavailable: 非 git 仓库，无 HEAD 可还原",
            [{"project_root": str(root)}],
        )
    refused: list[dict[str, str]] = []
    ok: list[str] = []
    for rel in rels:
        reason = _restore_refusal_reason(root, rel)
        if reason is None:
            ok.append(rel)
        else:
            refused.append({"path": rel, "reason": reason})
    if refused:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_refused: {len(refused)} 个路径不可还原（一个也未执行）",
            [{
                "refused": refused,
                "hint": (
                    "仅已跟踪且有 HEAD 版本的文件可还原；目录、仓库外路径、"
                    "未跟踪新建文件一律拒绝（后者无可还原目标，删除请走审批后"
                    "手动处置）"
                ),
            }],
        )
    try:
        proc = subprocess.run(
            ["git", "checkout", "HEAD", "--", *ok],
            cwd=str(root),
            capture_output=True, encoding=_GIT_ENCODING, errors=_GIT_ERRORS,
            timeout=_GIT_CHECKOUT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _clear_timeout_index_lock(root)
        raise OrchdError(
            ErrorCode.E007,
            "restore_timeout: git checkout 超时，未确认是否生效，请先核对后重试",
            [{"paths": ok}],
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_failed: git 执行失败：{exc}",
            [{"paths": ok}],
        ) from exc
    if proc.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            f"restore_failed: git checkout HEAD 失败：{(proc.stderr or '').strip()[:300]}",
            [{"paths": ok}],
        )
    return {"restored": ok, "project_root": str(root)}

def _restore_refusal_reason(root: Path, rel: str) -> str | None:
    """单路径还原准入判定（None = 放行，否则为拒绝原因码）。"""
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (OSError, ValueError):
        # ValueError = 逃逸仓库根（路径穿越）；OSError = 不可解析
        return "path_traversal"
    if target.is_dir():
        return "is_directory"
    # 有 HEAD 版本才可还原（未跟踪新建 / 已暂存新文件无还原目标）。
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{rel}"],
            capture_output=True, timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "git_unavailable"
    if probe.returncode != 0:
        return "no_head_version"
    return None
