"""任务 worktree 诊断（声明/diff/冲突判定）。

依赖方向：同包 layout 原语 + 懒导入（gitops/line_ctx/pool）。
"""


from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型标注：叶子模块，运行时不依赖 ledger（防循环）
    from orchd.ledger import Store

from orchd.worktree.layout import _GIT_TIMEOUT

# task-pass9-rev-slug-guard：task_id 中央层白名单（与 split.py module.id 同口径）。
# rev 由 task_id 拼装（main...task/<id>），intake 之后 claim/done 边界不再复验；
# 非法值直接返回空列表（不拼 rev、不调 git），defense-in-depth。
_TASK_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")


def _git_diff_names(project_root: Path, task_id: str, *,
                    no_renames: bool = False) -> list[str]:
    """git diff --name-only main...task/<id>（best-effort，E010 增强用）。

    返回任务分支相对 main 实际改动的文件路径列表；分支不存在 / 非 git /
    git 不可用返回空列表。

    task-rename-declaration-deadlock-fix：``no_renames=True`` 时加
    ``--no-renames``，重命名展开为删（旧路径）+ 增（新路径)，与 delete-parity
    口径一致（旧路径删除态走“提交删除态后保持声明”，新路径按新增判定），
    杜绝折叠后旧路径 path_not_found 死锁；E020 hook staged 侧同口径。
    缺省 False（调用方显式 opt-in，零回归）。
    """
    if not _TASK_SLUG_RE.fullmatch(task_id or ""):
        return []
    try:
        from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

        base = (f"{resolve_trunk_for(project_root)}..."
                f"{resolve_task_branch_for(project_root, task_id)}")
        args = ["git", "diff", "--name-only"]
        if no_renames:
            args.append("--no-renames")
        args.append(base)
        proc = subprocess.run(
            args,
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return []


def task_branch_files(project_root: Path, task_id: str, *,
                      no_renames: bool = False) -> list[str]:
    """返回任务分支相对 main 的实际改动文件清单（best-effort）。

    ``no_renames`` 透传 :func:`_git_diff_names`（重命名展开口径，见该函数）。
    """
    return _git_diff_names(project_root, task_id, no_renames=no_renames)


def main_worktree_dirty_overlap(
    project_root: Path,
    declared_files: list[str] | set[str],
) -> list[str]:
    """检测主工作树中与声明文件重叠的已跟踪脏文件（跨 worktree 漏写防护）。

    flat 单 worktree（project_root == main_worktree_root）不适用，返回空列表。
    """
    try:
        from orchd.gitops import list_tracked_changes, main_worktree_root

        main_root = main_worktree_root(project_root)
        if main_root.resolve() == Path(project_root).resolve():
            return []
        dirty = list_tracked_changes(main_root)
        if dirty is None:
            return []
        from orchd.pool import _prefix_overlap
        return _prefix_overlap(dirty, declared_files)
    except Exception:
        return []


def _is_flat_task_branch(project_root: Path, task_id: str) -> bool:
    """当前检出分支是否为 ``task/<id>``（task-flat-guard-parity，flat 等价门）。

    container 降级（主工作树检出任务分支）同理适用——后续探针（status /
    check-ignore）均以 project_root 为 cwd，与独立任务 worktree 语义一致。
    best-effort：探测失败一律 False（调用方回落跳过，不扩大阻断面）。
    """
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(project_root),
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    from orchd.line_ctx import resolve_task_branch_for

    expected = resolve_task_branch_for(project_root, task_id)
    return proc.returncode == 0 and (proc.stdout or "").strip() == expected


def is_current_task_branch(project_root: Path, task_id: str) -> bool:
    """当前工作区是否即该任务的分支检出（flat 等价门对外口径，守卫层共用）。

    独立任务 worktree 恒为真（其 HEAD 即任务分支）；flat / 容器降级下以后续
    ``_is_flat_task_branch`` 判定为准。best-effort，异常 False。
    """
    try:
        from orchd.gitops import is_task_worktree

        if is_task_worktree(Path(project_root)):
            return True
    except Exception:
        pass
    return _is_flat_task_branch(Path(project_root), task_id)


def missing_declared_branch_files(
    project_root: Path,
    task_id: str,
    declared_files: list[str] | set[str],
) -> list[str]:
    """返回任务分支 diff 中缺失的声明文件（best-effort）。

    flat / 非任务 worktree 场景跳过；仅 container 独立任务 worktree 才对比。
    """
    try:
        from orchd.gitops import is_task_worktree

        if not is_task_worktree(Path(project_root)):
            return []
        # task-rename-declaration-deadlock-fix：重命名展开口径（删+增），旧路径
        # 删除态走 path_not_found + deleted 标记（提交删除态后保持声明），不死锁
        branch_files = set(task_branch_files(Path(project_root), task_id, no_renames=True))
        if not branch_files:
            # 无实际任务分支改动（测试/flat/未实现）不强制比对，避免误伤
            return []
        from orchd.pool import _is_path_covered as is_path_covered
        # 目录式声明感知差集（task-decl-dir-match-conflict）：声明路径若被
        # branch_files 中任一文件覆盖（即目录下有改动），则视为已覆盖。
        declared = list(declared_files)
        missing: list[str] = []
        for dp in declared:
            if dp in branch_files:
                continue
            if any(is_path_covered(dp, bf) for bf in branch_files):
                continue
            missing.append(dp)
        return sorted(missing)
    except Exception:
        return []


def diagnose_missing_branch_files(
    project_root: Path,
    task_id: str,
    declared_files: list[str] | set[str],
) -> list[dict[str, str]]:
    """返回缺失声明文件的结构化诊断（Bug #20b，2026-08-27）。

    对每个缺失文件做三路判定：
    - path_not_found：文件在磁盘不存在
    - gitignored：文件存在但被 .gitignore 忽略（附命中规则）
    - not_committed：文件存在且未被忽略，但未进入任务分支 diff（漏提交）

    task-flat-guard-parity：独立任务 worktree 之外，flat / 容器降级下当前检出
    分支即任务分支时同样执行（分支 diff + 本 worktree 探针，语义一致）；其余
    （main 上等）返回空列表。
    """
    try:
        pr = Path(project_root)
        if not is_current_task_branch(pr, task_id):
            return []
        # task-rename-declaration-deadlock-fix：同上展开口径
        branch_files = set(task_branch_files(pr, task_id, no_renames=True))
        if not branch_files:
            return []
        from orchd.pool import _is_path_covered as is_path_covered
        # 目录式声明感知差集：声明路径若被 branch_files 中任一文件覆盖（即目录下有改动），
        # 则视为已覆盖，不报 missing；精确文件仍用差集判定。
        declared = list(declared_files)
        missing: list[str] = []
        for dp in declared:
            if dp in branch_files:
                continue
            if any(is_path_covered(dp, bf) for bf in branch_files):
                continue
            missing.append(dp)
        missing = sorted(missing)
        if not missing:
            return []

        results: list[dict[str, str]] = []
        for fp in missing:
            full = pr / fp
            if not full.exists():
                # task-e010-delete-parity-done-guards：区分「删除态未提交」与幽灵
                # 路径。两者 reason 均保持 path_not_found（未提交的声明内删除仍报
                # path_not_found，防「删了不提交」漏网），但 hint 引导不同——删除态
                # 应「提交删除态后保持声明」（与 _guard_out_of_scope 的 D 感知口径
                # 对齐；勿移除声明，否则删除变声明外改动触发 out_of_scope E010）；
                # 幽灵路径才是「修正路径或从声明中移除」。deleted 标记仅供
                # _guard_declared_diff 组装 hint 使用，不进事件 details。
                deleted_flag = "false"
                try:
                    st = subprocess.run(
                        ["git", "status", "--short", "--", fp],
                        cwd=str(pr),
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                    )
                    # 删除态：status 短格式的 XY 位含 D（staged 'D ' / unstaged ' D'）；
                    # XY 与路径间有空格分隔，strip 后首位即 X/Y 状态位，不会误匹配路径名。
                    if st.returncode == 0 and any(
                        ln.strip().startswith("D")
                        for ln in st.stdout.splitlines() if ln.strip()
                    ):
                        deleted_flag = "true"
                except (subprocess.SubprocessError, FileNotFoundError, OSError):
                    pass  # 探测失败按非删除态处理（维持原 hint，不放大阻断面）
                detail = (
                    f"路径 {fp} 在磁盘不存在（检测到未提交的删除态）"
                    if deleted_flag == "true"
                    else f"路径 {fp} 在磁盘不存在"
                )
                results.append({
                    "file": fp,
                    "reason": "path_not_found",
                    "detail": detail,
                    "deleted": deleted_flag,
                })
                continue
            # git check-ignore：退出码 0 = 被忽略，1 = 未被忽略
            try:
                proc = subprocess.run(
                    ["git", "check-ignore", "-v", fp],
                    cwd=str(pr),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    results.append({
                        "file": fp,
                        "reason": "gitignored",
                        "detail": proc.stdout.strip(),
                    })
                    continue
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                pass
            # task-master-single-copy：区分"漏提交"与"声明未改动"。未进分支
            # diff 的文件若在工作树/暂存区有改动 → 真漏提交（not_committed，
            # 阻断）；完全无改动 → 声明冗余（E020 预防性声明），不阻断。
            try:
                st = subprocess.run(
                    ["git", "status", "--short", "--", fp],
                    cwd=str(pr),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                has_changes = st.returncode == 0 and bool(st.stdout.strip())
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                has_changes = True  # 无法判定时保持 fail-closed 语义
            if has_changes:
                results.append({
                    "file": fp,
                    "reason": "not_committed",
                    "detail": "文件存在且未被 .gitignore 忽略，但未出现在任务分支 diff 中"
                              "（工作树/暂存区有改动未提交）",
                })
        return results
    except Exception:
        return []


def actual_changes_conflict(
    project_root: Path | None,
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    target_task: dict[str, Any],
) -> list[dict[str, Any]]:
    """E010 增强（task-14-worktree-lifecycle AC7）：声明 ∪ 实际改动文件冲突。

    claim 期额外比对活跃（claimed）任务的**分支实际改动**（``git diff --name-only
    main...task/<id>``）与候选任务 ``files_to_edit`` 的重叠——未声明文件的重叠
    编辑提前到 claim 期拦截（比 merge 期返工更早）。

    Args:
        project_root: 主工作树根（None / 非 git → 空结果，best-effort）。
        state: Store.replay() 结果。
        tasks: 全部任务定义。
        target_task: 候选目标任务。

    Returns:
        实际改动冲突列表：``[{"task_id", "files", "claimed_by", "source": "actual"}]``。
    """
    if project_root is None:
        return []
    try:
        from orchd.pool import _build_claimed_files
    except Exception:
        return []
    claimed_files = _build_claimed_files(state, tasks)
    target_files = set(target_task.get("files_to_edit", []))
    if not target_files:
        return []
    conflicts: list[dict[str, Any]] = []
    target_id = target_task.get("id", "")
    for tid, (_, claimed_by) in claimed_files.items():
        if tid == target_id:
            continue
        # task-rename-declaration-deadlock-fix：在途冲突按展开口径（删+增），
        # 重命名两端皆可参与冲突判定（折叠只见新路径会漏旧路径冲突）
        actual = _git_diff_names(project_root, tid, no_renames=True)
        from orchd.pool import _prefix_overlap
        overlap = _prefix_overlap(target_files, actual)
        if overlap:
            conflicts.append({
                "task_id": tid,
                "files": overlap,
                "claimed_by": claimed_by,
                "source": "actual",
            })
    return conflicts
