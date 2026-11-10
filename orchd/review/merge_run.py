"""review merge 执行段（_review_submit_merge_run），依赖 branch+resolve_markers。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import subprocess

from orchd.gitops import main_worktree_root
from orchd.gitops.guard.restore import restore_worktree_paths
from orchd.line_ctx import resolve_task_branch_for
from orchd.line_ctx import resolve_trunk_for
from orchd.gitops_ops import try_auto_resolve_conflict
from orchd.gitops_ops import try_git_merge
from .branch import _wt_dir_name
from .resolve_markers import _resolve_review_resolve_markers


def _generate_in_main_worktree(root: Path, artifact: str) -> str:
    """在主工作树上下文里生成单份产物内容（避免用任务 worktree 的包根求值）。

    生成器的 ``ROOT`` 由被导入的 ``orchd`` 包位置推导；本进程内直接调用会锚定到
    任务 worktree（其行数/文件集与 main 不同，导致 merge 后 docs check 仍漂移）。
    故以 ``cwd=root`` + ``PYTHONPATH`` 指向主工作树的子进程生成到临时目录，
    再只读取目标产物。
    """
    import os
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(prefix="orchd-merge-docsgen-") as tmp:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "orchd.docsgen", "--write", tmp],
            cwd=str(root), env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"docsgen 生成失败（rc={proc.returncode}）：{(proc.stderr or '')[-300:]}"
            )
        return (Path(tmp) / Path(artifact).name).read_text(encoding="utf-8")


def _rollback_generated_artifact(root: Path, artifact: str) -> None:
    """把生成物恢复到 HEAD；HEAD 中不存在则从索引与工作树移除。

    merge 后刷新是独立提交：失败时不能把 staged 生成物留在主工作树，否则
    当前活跃任务的 hook 会持续拦截后续无关提交。tracked 文件走受管还原
    （``git checkout HEAD --``）；首次生成的新文件没有 HEAD 版本，改为从
    索引移除并删除工作树文件。
    """
    target = Path(root) / artifact
    probe = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"HEAD:{artifact}"],
        capture_output=True, timeout=30,
    )
    if probe.returncode == 0:
        restore_worktree_paths(Path(root), [artifact])
        return
    subprocess.run(
        ["git", "-C", str(root), "rm", "--cached", "--ignore-unmatch", "--", artifact],
        check=True, capture_output=True, timeout=30,
    )
    target.unlink(missing_ok=True)


def _refresh_dependency_graph_after_merge(
    project_root: Path,
    task_id: str,
    degraded: list[dict[str, Any]],
) -> None:
    """merge 成功后刷新 dependency-graph 并提交（失败回滚生成物，不回滚 merge）。

    仅在引擎仓执行：以主工作树为 cwd 重生成 ``docs/_generated/dependency-graph.md``；
    内容有变化才写盘并做一次引擎提交（``chore(docs): ...``）。宿主仓 / 非引擎仓
    直接跳过，避免误触 engine-only 生成器；无 git 单目录同样跳过（无提交通道）。
    """
    from orchd.repo_role import is_engine_repo

    try:
        root = main_worktree_root(project_root)
    except Exception as exc:  # noqa: BLE001 - 定位主工作树失败只降级
        degraded.append({
            "guard": "dependency_graph_refresh",
            "task_id": task_id,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "merge 已完成；dependency-graph 未刷新，"
                "可在主工作树手工执行 docs sync --point merge"
            ),
        })
        return
    if not is_engine_repo(root):
        return
    try:
        from orchd.nogit import git_available

        if not git_available(Path(root)):
            return  # 无 git 单目录：add/commit 必失败，跳过刷新避免误导性 degraded
    except Exception:  # noqa: BLE001 - 探测失败按原路径继续，由 git 步骤自行降级
        pass
    artifact = ""
    wrote = False
    try:
        from orchd import docs_manifest

        entry = docs_manifest.by_id("dependency-graph")
        if entry is None:
            raise RuntimeError("dependency-graph 产物未登记")
        artifact = entry.artifact
        expected = _generate_in_main_worktree(Path(root), entry.artifact)
        target = Path(root) / entry.artifact
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current == expected:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        wrote = True
        target.write_text(expected, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root), "add", entry.artifact],
            check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m",
             f"chore(docs): merge 后刷新 {entry.artifact}（{task_id}）",
             "--", entry.artifact],
            check=True, capture_output=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - 刷新失败不阻断已完成的 merge
        rollback_error = None
        if wrote and artifact:
            try:
                _rollback_generated_artifact(Path(root), artifact)
            except Exception as rexc:  # noqa: BLE001 - 回滚失败要单独留痕
                rollback_error = f"{type(rexc).__name__}: {rexc}"
        stderr = ""
        if isinstance(exc, subprocess.CalledProcessError):
            raw = exc.stderr or exc.stdout or b""
            stderr = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        hint = (
            "merge 已完成；刷新失败且自动回滚失败，请检查主工作树 "
            "git status，并用 orchd restore --path <artifact> 恢复"
            if rollback_error else
            "merge 已完成；本次刷新已回滚；修复 hook 后可重跑 "
            "docs sync --point merge 并提交"
        )
        degraded.append({
            "guard": "dependency_graph_refresh",
            "task_id": task_id,
            "artifact": artifact,
            "error": f"{type(exc).__name__}: {exc}",
            "stderr": stderr[-500:],
            "rollback_error": rollback_error,
            "hint": hint,
        })


def _review_submit_merge_run(
    *,
    project_root: Path | None,
    task_id: str,
    result: dict[str, Any],
    degraded: list[dict[str, Any]],
    authorize_reviewer_resolve: bool,
) -> tuple[dict[str, Any] | None, bool, bool]:
    """merge 执行段（原嵌套 try 体 L955-1049，6 条语句）。

    git 可用性探测 → try_git_merge → 冲突处理（自动化解 / 审查者授权解冲突兜底）→
    merge_env_error 与 merge_diagnostic 分支；冲突失败路径写 result 的 error/guidance/
    action 并保持 in_review。返回 (merge_result, auto_resolved, _nogit_single_dir)。
    """
    merge_result = None
    _nogit_single_dir = False
    if project_root:
        from orchd.nogit import git_available as _git_avail

        if not _git_avail(Path(project_root)):
            # 单目录无 git（task-nogit-single-dir-pivot）：无合并动作
            # （工作已在主目录），直接走完成路径（与 merge 成功同后续）。
            _nogit_single_dir = True
        else:
            merge_result = try_git_merge(project_root, task_id)

    auto_resolved = False
    conflict_files: list[str] = []
    if merge_result is not None and merge_result.get("conflict"):
        # task-degraded-trace-visibility：union 取 verify_command 失败经
        # degraded 透到响应；2 参测试替身向后兼容（TypeError 回退 2 参）。
        if project_root:
            try:
                auto = try_auto_resolve_conflict(project_root, task_id, degraded)
            except TypeError as _te:
                if "positional" in str(_te) or "takes" in str(_te):
                    auto = try_auto_resolve_conflict(project_root, task_id)
                else:
                    raise
        else:
            auto = None
        if auto and auto.get("resolved"):
            auto_resolved = True
        else:
            conflict_files = (auto or {}).get("conflict_files") or merge_result.get("files", [])
            # task-merge-tests-union-and-reviewer-fallback [fallback]：
            # 审查者显式授权解冲突兜底。仅在 authorize_reviewer_resolve=True
            # 时尝试解 REVIEW-RESOLVE 标记段，保留双方内容；只提交冲突文件。
            # 无授权或解冲突失败 → 回退 E015（默认仍要求实现者解）。
            reviewer_resolved = False
            if authorize_reviewer_resolve and project_root and conflict_files:
                _rr_workdir = main_worktree_root(project_root)
                _rr_trunk = resolve_trunk_for(project_root)
                _rr_branch = resolve_task_branch_for(project_root, task_id)
                # 重新触发 merge 以获得冲突工作树状态（try_git_merge 已 abort）
                subprocess.run(["git", "-C", str(_rr_workdir), "merge", "--abort"],
                               capture_output=True, timeout=10)
                subprocess.run(["git", "-C", str(_rr_workdir), "checkout", _rr_trunk],
                               capture_output=True, timeout=10)
                _rr_merge = subprocess.run(
                    ["git", "-C", str(_rr_workdir), "merge", _rr_branch],
                    capture_output=True, timeout=30,
                )
                if _rr_merge.returncode != 0:
                    reviewer_resolved = _resolve_review_resolve_markers(
                        _rr_workdir, conflict_files
                    )
            if reviewer_resolved:
                auto_resolved = True
                result["reviewer_resolve"] = "Both sides preserved"
            else:
                result["merged"] = False
                result["reason"] = "merge_conflict"
                result["conflict_files"] = conflict_files
                result["task_status"] = "in_review"
            # 通道 D：E015 merge_conflict 指引挂载（structured_error 收敛）
            # 加法式：保留原 reason/conflict_files/action，新增 error 字段
            # 使 guide.py E015 静态表指引(recovery/command/exit_type=git-diagnose)可达
            try:
                from orchd.ledger import structured_error
                _cf_text = ", ".join(conflict_files) if conflict_files else "未知"
                _e015_resp = structured_error(
                    "E015",
                    f"merge 冲突：{_cf_text}（main 已恢复，需人工裁决）",
                    [{
                        "conflict_files": conflict_files,
                        "hint": (
                            "进入任务 worktree 执行 orchd git merge main，解决冲突后 commit，"
                            "再由同一 reviewer 重试 code APPROVED"
                        ),
                    }],
                    project_root,
                )
                result["error"] = _e015_resp.get("error")
                if "guidance" in _e015_resp:
                    result["guidance"] = _e015_resp["guidance"]
            except Exception:
                pass  # best-effort：指引挂接失败不阻断冲突上报
            # P0-18：增强冲突指引——worktree 位置、文件清单、重试路径、回退命令
            _auto_action = (auto or {}).get("action")
            if _auto_action:
                result["action"] = _auto_action
            else:
                _cf_list = ", ".join(conflict_files) if conflict_files else "未知"
                result["action"] = (
                    f"merge 冲突（main 已恢复）。冲突文件：{_cf_list}。\n"
                    f"【解决步骤】\n"
                    f"  1. 进入任务 worktree 目录：cd ../{_wt_dir_name(task_id)}/\n"
                    f"     （container 布局下主工作树内无法 checkout task/{task_id} 分支）\n"
                    f"  2. 执行 orchd git merge main（受管通道，任务分支放行），"
                    f"解决冲突后 git commit\n"
                    f"  3. 由同一 reviewer 重试 code APPROVED\n"
                    f"【放弃本次审查】\n"
                    f"  orchd retract --task {task_id} --type REVIEW_CLAIMED "
                    f"--reason 'merge冲突放弃'\n"
                    f"  orchd force-status --task {task_id} --status pending "
                    f"--reason 'merge冲突回退'"
                )

    # 段体保持基线 6 条语句（段体拼接守卫）：merge 成功判定 + 依赖图刷新由尾随
    # return 表达式承载——见 `_finish_merge_run`，不得写回段体。
    return _finish_merge_run(
        merge_result=merge_result,
        auto_resolved=auto_resolved,
        nogit_single_dir=_nogit_single_dir,
        project_root=project_root,
        task_id=task_id,
        degraded=degraded,
    )


def _finish_merge_run(
    *,
    merge_result: dict[str, Any] | None,
    auto_resolved: bool,
    nogit_single_dir: bool,
    project_root: Path | None,
    task_id: str,
    degraded: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool, bool]:
    """merge 成功判定 + 成功后依赖图刷新（原段体尾部两条语句的承载点）。

    刷新失败只记 degraded（见 :func:`_refresh_dependency_graph_after_merge`），
    不回滚已完成 merge、不改变审查结论。
    """
    merged_ok = (
        nogit_single_dir
        or auto_resolved
        or (
            merge_result is not None
            and not merge_result.get("conflict")
            and not merge_result.get("merge_diagnostic")
        )
    )
    if project_root and merged_ok:
        # merge 已落 main；刷新失败只记 degraded，不回滚 merge。
        _refresh_dependency_graph_after_merge(Path(project_root), task_id, degraded)
    return merge_result, auto_resolved, nogit_single_dir
