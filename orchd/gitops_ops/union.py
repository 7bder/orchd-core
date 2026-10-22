"""gitops_ops union 域 — tests/ 冲突的三方 union 化解与结果验证。

来源：``orchd/gitops_ops.py（1220 行）``（W6-C6 纯搬移）。

仅当冲突文件全部落在 ``tests/test_*.py`` 时启用；union 后必须重跑 verify，
verify 非零即 abort 并降级 E015。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchd.gitops import main_worktree_root, unmerged_paths
from orchd.ledger import _write_bytes_atomic
from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for


def _read_task_verify_command(
    project_root: Path, task_id: str,
    degraded: list[dict[str, Any]] | None = None,
) -> str | None:
    """从 _master.json 读取任务的 verify_command（best-effort，失败返回 None）。

    task-master-path-resolver-convergence：master **路径解析**统一走
    :func:`orchd.worktree.resolve_master_path_from_dir`。此前裸拼
    ``project_root/.orchd/_master.json``，而调用点已把 workdir 经
    :func:`main_worktree_root` canonical 化（口径不一）——container 布局下
    project_root 可能是任务 worktree（副本被抑制）→ 取不到 verify_command，
    union 合并静默降级为逐文件 pytest。

    task-degraded-trace-visibility：读失败不再纯静默 ``pass``——经
    :func:`orchd.gitops.guard.record_degraded_guard`（``guard="verify_command_read"``，
    E030 / warning）写入 ``degraded`` 供响应携带（``degraded_guards``）。
    降级语义不变：仍返回 None 由调用方按既有 fallback 逐文件验证
    （不升级为阻断，也不改成跳过）。
    """
    try:
        from orchd.worktree import resolve_master_path_from_dir

        master_path = resolve_master_path_from_dir(Path(project_root) / ".orchd")
        if not master_path.exists():
            if degraded is not None:
                from orchd.gitops.guard import (
                    GUARD_STATUS_NOT_APPLICABLE,
                    record_degraded_guard,
                )

                record_degraded_guard(
                    degraded,
                    guard_name="verify_command_read",
                    status=GUARD_STATUS_NOT_APPLICABLE,
                    reason=(
                        f"master 不存在（{master_path}）：union verify_command "
                        "无从读取，已降级为逐文件验证"
                    ),
                    context={"task_id": str(task_id)},
                    hint=(
                        "确认主工作树 _master.json 存在且任务已注册；"
                        "本次已按既有 fallback 逐文件验证，不阻断"
                    ),
                )
            return None
        import json
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)
        for t in master.get("tasks", []):
            if t.get("id") == task_id:
                return t.get("verify_command")
    except Exception as exc:
        if degraded is not None:
            from orchd.gitops.guard import GUARD_STATUS_FAILED, record_degraded_guard

            err = f"{type(exc).__name__}: {exc}".strip()[:300]
            record_degraded_guard(
                degraded,
                guard_name="verify_command_read",
                status=GUARD_STATUS_FAILED,
                reason=f"verify_command 读取失败，已降级为逐文件验证：{err}",
                context={"task_id": str(task_id)},
                error=err,
                hint=(
                    "master 解析/读取异常，本次已按既有 fallback 逐文件验证；"
                    "确认 _master.json 可读后重试"
                ),
            )
    return None


def _is_tests_only_file(path: str) -> bool:
    """判断冲突文件是否仅限 tests/ 目录下的 test_* 文件（union 白名单）。

    task-merge-tests-union-and-reviewer-fallback [union]：仅当冲突文件全部
    落在 tests/ 且以 test_ 前缀命名时，才允许 union 自动合并。orchd 本体
    及非 tests 路径永不自动 union。
    """
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if "tests" not in parts:
        return False
    filename = parts[-1]
    return filename.startswith("test_") and filename.endswith(".py")


def _try_union_merge_conflicts(
    workdir: Path, conflict_files: list[str], verify_command: str | None = None
) -> bool:
    """对冲突文件执行 git merge-file --union 三方合并并提交，提交后跑 verify。

    仅在 _is_tests_only_file 全部通过后由调用方触发。逐文件取
    base(:1:)/ours(:2:)/theirs(:3:) 三态，union 合并后写回工作树并 git add；
    全部成功后 git commit 完成 merge。

    task-merge-tests-union-and-reviewer-fallback（AC2）：提交后必须重跑
    verify——git merge-file --union 对同一文件两侧真实行冲突/重复 import 会
    拼接出语法或语义损坏的测试，无验证即合入 main 是承重安全网缺失。verify
    非零则 git merge --abort 并返回 False（调用方降级 E015）。

    Args:
        workdir: git 工作树目录（当前在 task 分支上）。
        conflict_files: 冲突文件相对路径列表。
        verify_command: 任务 spec 中的 verify_command（可选）。为 None 时
            降级为对冲突文件逐个跑 ``python -m pytest <file>``。

    Returns:
        True 表示 union 合并、提交、verify 均成功；False 表示任一步失败
        （已 abort，调用方降级 E015）。
    """
    import tempfile

    merged_paths: list[str] = []
    for rel in conflict_files:
        # 取三态内容
        base_p = subprocess.run(
            ["git", "-C", str(workdir), "show", f":1:{rel}"],
            capture_output=True,
        )
        ours_p = subprocess.run(
            ["git", "-C", str(workdir), "show", f":2:{rel}"],
            capture_output=True,
        )
        theirs_p = subprocess.run(
            ["git", "-C", str(workdir), "show", f":3:{rel}"],
            capture_output=True,
        )
        if base_p.returncode != 0 or ours_p.returncode != 0 or theirs_p.returncode != 0:
            return False
        # 写入临时文件
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".base") as bf:
                bf.write(base_p.stdout)
                base_path = bf.name
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ours") as of:
                of.write(ours_p.stdout)
                ours_path = of.name
            with tempfile.NamedTemporaryFile(delete=False, suffix=".theirs") as tf:
                tf.write(theirs_p.stdout)
                theirs_path = tf.name
        except OSError:
            return False
        try:
            # git merge-file --union ours base theirs → 结果写回 ours_path
            mf = subprocess.run(
                ["git", "merge-file", "--union", ours_path, base_path, theirs_path],
                capture_output=True,
            )
            if mf.returncode != 0:
                return False
            # 读回合并结果，写回工作树
            with open(ours_path, "rb") as rf:
                merged_content = rf.read()
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes_atomic(target, merged_content)
        finally:
            for p in (base_path, ours_path, theirs_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        add = subprocess.run(
            ["git", "-C", str(workdir), "add", rel],
            capture_output=True,
        )
        if add.returncode != 0:
            return False
        merged_paths.append(rel)
    # AC2：union 合并后、提交前重跑 verify，防止拼接出语法/语义损坏的测试。
    # verify 非零 → git merge --abort（此时 merge 尚未提交，abort 有效）回退，
    # 调用方降级 E015。verify 通过才提交。
    verify_ok = _run_union_verify(workdir, conflict_files, verify_command)
    if not verify_ok:
        subprocess.run(
            ["git", "-C", str(workdir), "merge", "--abort"],
            capture_output=True, timeout=10,
        )
        return False
    # 提交 union merge（保留双方测试新增）
    commit = subprocess.run(
        ["git", "-C", str(workdir), "commit", "-q",
         "-m", "chore(tests): auto union merge — both sides preserved"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return commit.returncode == 0


def _run_union_verify(
    workdir: Path, conflict_files: list[str], verify_command: str | None
) -> bool:
    """union 合并后执行 verify，返回是否通过。

    优先使用任务 spec 的 verify_command（替换 ``${TMPDIR:-/tmp}`` 和 ``$$``
    等 shell 变量为实际临时目录）；无 verify_command 时降级为对冲突文件
    逐个跑 ``python -m pytest <file>``。任一非零即失败。
    """
    import tempfile

    if verify_command:
        # F2（pass9 评审，task-pass9-union-verify-gate）：接入与 done/amend 路径
        # 同源的危险命令门禁——本路径把任务提供的 verify_command 以 shell=True
        # 在主工作区执行，此前完全绕过 verify_command_dangerous_reasons（同一
        # 字符串走 done 被拦 E027、走 union 被执行）。对原始串（变量替换前）
        # 同口径扫描：非零 reasons 即拒绝执行，返回 verify 失败 → 既有
        # merge --abort → E015 链路；stderr 留痕供审计。
        from orchd.spec import verify_command_dangerous_reasons

        _dangerous = verify_command_dangerous_reasons(verify_command)
        if _dangerous:
            print(
                "[orchd union-verify] verify_command 含 shell 注入风险，拒绝执行："
                f"{_dangerous}",
                file=sys.stderr,
            )
            return False
        # 替换 shell 变量为实际值，避免 Windows 下不兼容
        tmpdir = tempfile.gettempdir()
        cmd = verify_command.replace("${TMPDIR:-/tmp}", tmpdir)
        cmd = cmd.replace("$$", str(os.getpid()))
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=str(workdir),
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=120,
            )
            return proc.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False
    # 降级：对每个冲突测试文件做语法编译检查（union 最可能引入语法错误）。
    # 不用 pytest：孤立测试仓库无 conftest/配置时 pytest 可能因收集环境问题
    # 返回非零，造成误杀；py_compile 精准检测语法损坏。
    for rel in conflict_files:
        try:
            proc = subprocess.run(
                ["python", "-m", "py_compile", rel],
                cwd=str(workdir),
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            if proc.returncode != 0:
                return False
        except (subprocess.SubprocessError, OSError):
            return False
    return True


def try_auto_resolve_conflict(
    project_root: Path, task_id: str,
    degraded: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """L3：merge 冲突自动化解——恢复 main → 分支 merge main 预演 → 自动合并或返回清单。

    全部 git 操作在**主工作树**内以 ``git -C`` 执行（``git rev-parse --git-common-dir``
    定位；flat 单会话即 project_root，零回归），任务 worktree 永不 checkout main。
    merge-wt 已废弃。

    task-degraded-trace-visibility：union 前取 verify_command 失败经 ``degraded``
    留痕（``guard="verify_command_read"``，见 :func:`_read_task_verify_command`），
    降级语义不变（仍按既有 fallback 逐文件验证）。

    Returns:
        ``{"resolved": True}``：自动化解成功（main 已含任务分支实现）。
        ``{"resolved": False, "conflict_files": [...], "action": "..."}``：仍需人工解决。
        ``None``：git 环境异常（best-effort 降级）。
    """

    def run(workdir: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None

    trunk = resolve_trunk_for(project_root)
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        workdir = main_worktree_root(project_root)
        run(workdir, "merge", "--abort")
        co = run(workdir, "checkout", branch)
        if co is None or co.returncode != 0:
            return None
        pre = run(workdir, "merge", trunk)
        if pre is None:
            return None
        if pre.returncode != 0:
            # 权威真源必须先于 abort 取（abort 清索引 → 之后只会得到空，
            # task-conflict-true-source-fix AC4）。
            paths = unmerged_paths(workdir)
            files = paths or []
            # task-merge-tests-union-and-reviewer-fallback [union]：冲突文件
            # 全部在 tests/（test_*.py）时尝试 union 三方合并，保留双方新增。
            # 含非 tests 文件或 union 失败 → 立即 abort 降级 E015。
            union_ok = False
            if files and all(_is_tests_only_file(f) for f in files):
                # AC2：读取任务 spec 的 verify_command 供 union 后验证
                _verify_cmd = _read_task_verify_command(project_root, task_id, degraded)
                union_ok = _try_union_merge_conflicts(workdir, files, _verify_cmd)
            if not union_ok:
                run(workdir, "merge", "--abort")
                return {
                    "resolved": False,
                    "conflict_files": files,
                    "action": (
                        f"分支 {branch} 与 {trunk} 合并冲突：请在 task 分支上执行 "
                        "orchd git merge main（受管通道）解决冲突并提交"
                        f"（{len(files) or '若干'} 个文件），"
                        f"然后由同一 reviewer 重试 code APPROVED"
                    ),
                }
            # union 成功：task 分支已含 main 的 union 合并结果，继续后续
            # checkout main → merge task（此时应可快进或无冲突合并）。
        co2 = run(workdir, "checkout", trunk)
        if co2 is None or co2.returncode != 0:
            return None
        final = run(workdir, "merge", branch)
        if final is None:
            return None
        if final.returncode != 0:
            # 此处未 abort（索引仍为未合并态）→ 直接取权威真源。
            files = unmerged_paths(workdir) or []
            return {
                "resolved": False,
                "conflict_files": files,
                "action": (
                    f"main 与任务分支合并仍冲突（{len(files) or '若干'} 个文件），"
                    f"请人工处理"
                ),
            }
        return {"resolved": True}
    except Exception:
        return None
