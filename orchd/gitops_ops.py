"""Orchd git 写子域 + 任务生命周期共享辅助。

将 onboard.py 中与 git 写入相关的 best-effort 操作，以及 L1 分支守卫 /
L2 session 锁 / 事件构造等在 onboarding 与 review 路径间共享的辅助函数，
统一外置到此模块，保持 onboard.py 只保留生命周期主干。与 orchd.gitops 区别：

- orchd.gitops：git 基础设施（工作区检测、hook、session lock、ensure_committed
  等），偏低级、可被任意模块复用。
- orchd.gitops_ops（本模块）：任务生命周期特定的 git 写动作（task/{id} 分支、
  merge 前置化、冲突化解）与共享辅助（guard_write_command / make_event 等），
  偏流程层，仅被 onboarding/review 路径调用。

依赖方向：本模块不导入 onboard.py / review.py，二者各自单向依赖本模块，
杜绝循环依赖。

task-line-trunk：任务分支 fork / merge 目标 / 对账基线按**当前线**解析
（``orchd.line_ctx``：canonical master 的 ``project.lines`` + ``ORCHD_LINE``）；
单线（未配置多线）恒为 ``main`` / ``task/{id}``，与 v1.5.0 逐字一致（opt-in 零回归）。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
# task-14-git-policy-layer：判定类逻辑（guard / checkout_default_strict /
# ensure_session_lock / unmerged_paths）已收敛到 orchd.gitops（专用 git 判定模块，
# 单一入口）；此处 re-import 保持旧导入路径兼容。
# task-conflict-true-source-fix：冲突清单真源由 parse_conflicts（末词启发式，非权威）
# 换为 unmerged_paths（git 未合并路径，权威）。
from orchd.gitops import (
    checkout_default_strict,
    check_workspace_state,
    ensure_session_lock,
    guard_write_command,
    is_task_worktree,
    main_worktree_root,
    unmerged_paths,
)
from orchd.ledger import generate_event_id, resolve_session_identity
# task-line-trunk：任务生命周期 git 写动作的 trunk / 分支名按**当前线**解析
# （单线零回归：未配置 project.lines 时恒为 main / task/{id}）。
from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for


# ------------------------------------------------------------------
# 共享辅助（onboarding / review 路径共用）
# ------------------------------------------------------------------


# 进程内严格单调保证的状态（L-2）：同进程连续生成的时间戳严格递增，
# 使同一写入方的事件永不落入归并平局分支（见 event_sort_key 的第三元）。
_LAST_TIMESTAMP: datetime | None = None
_TS_LOCK = threading.Lock()


def now_iso() -> str:
    """返回当前时刻的 UTC ISO 8601 字符串（微秒精度，进程内严格单调）。

    历史实现为「本地时区 + 秒精度」（``datetime.now(timezone.utc).astimezone()
    .isoformat(timespec="seconds")``）。该字符串被 ``ledger_sync`` 当作**跨设备
    归并的排序键第一元**，而带本地 offset 的字符串按字典序比较会跨时区因果倒置
    （``-08:00`` 机器的 ``00:00:00+08:00`` 会被排到 ``17:00:00+09:00`` 之后），
    两机因此收敛到不同状态、「幂等收敛」契约失效（task-ledger-order-determinism
    L-2；用户裁决 A —— 按 AC 原样执行）。

    现口径：

    - **UTC 绝对时刻**：``2026-09-15T05:40:12.123456+00:00``（不再依赖本地时区，
      归并侧按 aware 绝对时刻比较，跨时区天然可比）；
    - **微秒精度**：同秒事件不再并列（旧秒精度下 48% 的历史事件与同写入方事件撞秒）；
    - **进程内严格单调**：同一进程内若当前时刻 <= 上次发出的时刻（同微秒连续调用、
      时间回拨），自动 +1µs 递增，保证同一写入方事件绝不共享时间戳 —— 这使归并
      排序键的平局分支只在跨写入方时生效，从而既满足跨设备确定性归并，又不破坏
      同写入方事件的语义顺序（CLAIMED→DONE→REVIEW_READY；2026-09-10 实踩防线）。

    兼容性：旧本地时区/秒精度事件**原样保留不重写**，归并侧按带偏移绝对时刻比较
    （见 ``orchd.ledger.parse_event_time``）。
    """
    global _LAST_TIMESTAMP
    with _TS_LOCK:
        ts = datetime.now(timezone.utc)
        if _LAST_TIMESTAMP is not None and ts <= _LAST_TIMESTAMP:
            ts = _LAST_TIMESTAMP + timedelta(microseconds=1)
        _LAST_TIMESTAMP = ts
    return ts.isoformat(timespec="microseconds")


def make_event(
    task_id: str, agent_id: str, etype: str, **extra: Any
) -> dict[str, Any]:
    """构造标准事件字典，用于追加到 ledger（与 onboard._make_event 行为一致）。

    事件 schema 字段：
        v          - 事件版本号（当前固定为 1），便于未来 schema 演进时做兼容判断。
        event_id   - 全局唯一事件 ID，由 generate_event_id() 生成。
        timestamp  - 事件发生的本地时区 ISO 8601 时间戳（精确到秒）。
        task_id    - 关联的任务 ID。
        agent_id   - 触发此事件的 agent 标识。
        type       - 事件类型（如 CLAIMED / DONE / REVIEW_SUBMITTED / FORCE_STATUS 等）。

    **extra 中的键值对会直接合并到事件字典，用于各类型事件的差异化字段
    （如 changes_description、verdict、target_status 等）。
    """
    session_identity = resolve_session_identity()
    ev = {
        "v": 1,
        "event_id": generate_event_id(),
        "timestamp": now_iso(),
        "task_id": task_id,
        "agent_id": agent_id,
        "type": etype,
        "session_id": session_identity["session_id"],
    }
    ev.update(extra)
    return ev


def decode_subprocess_output(raw: bytes) -> str:
    """稳健解码子进程输出：UTF-8 优先，GBK 回退（Windows 默认代码页），最后有损 UTF-8。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _truncate_by_lines(text: str, limit: int) -> str:
    """按行对齐截断：从尾部取完整行，超限时在行首加省略标记，不从词中间切开。"""
    lines = text.splitlines()
    if not lines:
        return ""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > limit and kept:
            break
        kept.insert(0, line)
        total += len(line) + 1
    result = "\n".join(kept)
    if len(kept) < len(lines):
        result = "…(前略)\n" + result
    return result


def _extract_json_objects(text: str) -> list[dict]:
    """从文本中提取所有可解析的顶层 JSON 对象（括号匹配，容忍嵌套）。"""
    import json
    objects: list[dict] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            in_string = False
            escape = False
            for j in range(i, n):
                c = text[j]
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == "\"":
                    in_string = not in_string
                elif not in_string:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = json.loads(text[i:j + 1])
                                if isinstance(obj, dict):
                                    objects.append(obj)
                            except Exception:
                                pass
                            i = j + 1
                            break
            else:
                i += 1
        else:
            i += 1
    return objects


def _extract_validate_summary(text: str) -> str | None:
    """从 verify 输出中提取 validate 的 valid/errors/warnings 计数（降噪）。"""
    for obj in _extract_json_objects(text):
        if "valid" in obj and "errors" in obj and "warnings" in obj:
            errors = obj.get("errors", [])
            warnings = obj.get("warnings", [])
            return (
                f"valid={obj.get('valid')}, "
                f"errors={len(errors) if isinstance(errors, list) else '?'}, "
                f"warnings={len(warnings) if isinstance(warnings, list) else '?'}"
            )
    return None


def verify_output_summary(stdout: bytes, stderr: bytes, limit: int = 400) -> str:
    """从 verify_command 子进程输出提取可读摘要。

    task-verify-summary-structured：
    - 逐命令结论行聚合（ruff / pytest / validate），不再只反映最后一条命令
    - 按行对齐截断，不从词中间切开，超限时有省略标记
    - validate 存量 warning 降级为计数摘要，原文不进入 output_summary
    - 无已知工具结论时回退到按行截断的原始输出（兼容简单命令）
    """
    import re
    out = decode_subprocess_output(stdout)
    err = decode_subprocess_output(stderr)
    parts: list[str] = []

    if out.strip():
        conclusions: list[str] = []
        # 1) 逐行扫描 ruff / pytest 结论行
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            # ruff: All checks passed! / Found N errors
            if "All checks passed" in s:
                conclusions.append(f"ruff: {s}")
            elif re.search(r"Found \d+ error", s):
                conclusions.append(f"ruff: {s}")
            # pytest: N passed / N passed, M skipped / N failed
            elif re.search(r"\d+ passed", s) or re.search(r"\d+ failed", s):
                conclusions.append(f"pytest: {s}")
        # 2) validate 降噪：提取计数摘要
        validate_summary = _extract_validate_summary(out)
        if validate_summary:
            conclusions.append(f"validate: {validate_summary}")
        # 3) 有结论行则聚合；否则回退按行截断
        if conclusions:
            parts.append(" | ".join(conclusions))
        else:
            parts.append(_truncate_by_lines(out.rstrip(), limit))

    # 4) stderr 同样按行截断
    if err.strip():
        parts.append("stderr: " + _truncate_by_lines(err.strip(), 200))

    return " | ".join(parts)


# ------------------------------------------------------------------
# git 写子域（任务生命周期特定）
# ------------------------------------------------------------------


def _git_error_summary(proc: "subprocess.CompletedProcess[str]") -> str:
    """从 git checkout 子进程提取可读错误摘要：stderr 优先，回退 stdout，截断 200 字符。"""
    text = (proc.stderr or proc.stdout or "").strip()
    return text[:200] + ("…" if len(text) > 200 else "")


def try_git_branch(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """best-effort 切换到任务分支 task/{task_id}，并显式上报 git 命令失败。

    返工场景分支已存在则 checkout 复用（task-master-single-copy：不再同步分支
    worktree 的 .orchd/_master.json 与 main——container 副本已由 sparse-checkout
    抑制、flat 布局单副本无需同步）。
    首次 claim 才 checkout -b 新建——**显式从默认分支(main/master) fork**，
    避免游离 HEAD / 孤儿分支（2026-08-28 仓库损坏根因修复）。

    返回值契约（task-git-branch-fail-report，替代原先的静默吞错）：
      - 成功创建/切换：``{"state": "ok"}``；
      - git 命令失败（真实仓库内 checkout / checkout -b 返回非零）：
        ``{"state": "failed", "step": "create" | "checkout", "error": <stderr 摘要>}``；
      - 环境异常（非 git 仓库 / git 不可用 / 子进程超时）：``None``
        ——静默降级、不抛异常（既有契约 test_non_git_dir_no_error 锁定）。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        # 环境探测：区分「非 git 仓库等环境异常」（返回 None）与
        # 「真实仓库内 git 命令失败」（返回 failed 状态字典）。
        env = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if env.returncode != 0:
            return None
        check = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if check.returncode == 0:
            checkout = subprocess.run(
                ["git", "checkout", branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if checkout.returncode == 0:
                return {"state": "ok"}
            return {
                "state": "failed",
                "step": "checkout",
                "error": _git_error_summary(checkout),
            }
        default = resolve_trunk_for(project_root)
        create = subprocess.run(
            ["git", "checkout", "-b", branch, default],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if create.returncode == 0:
            return {"state": "ok"}
        return {
            "state": "failed",
            "step": "create",
            "error": _git_error_summary(create),
        }
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _clean_stale_index_lock(workdir: Path) -> bool:
    """清理主工作树 .git/index.lock 残留锁文件。

    当 git 进程异常中断时（如 shell 被强制关闭、超时被杀），index.lock 会残留，
    阻塞后续所有 git 操作（checkout/merge/commit 等均报 ``Unable to create
    '.../index.lock': File exists``）。通过文件年龄判断锁是否已过期：
    - 存在时间 > 5 分钟 → 视为 stale，删除后返回 True。
    - 不存在或 < 5 分钟 → 不动，返回 False（可能是活跃 git 操作，不应干预）。

    best-effort：任何异常（路径不存在、权限不足等）静默降级，不阻断调用方。
    """
    try:
        lock = workdir / ".git" / "index.lock"
        if not lock.is_file():
            return False
        age = datetime.now(timezone.utc).timestamp() - lock.stat().st_mtime
        if age > 300:  # 5 分钟
            lock.unlink()
            return True
    except Exception:
        pass
    return False


def _classify_merge_failure(stderr: str) -> dict[str, Any]:
    """merge 非冲突失败分类（task-merge-failure-diagnostic）。

    ``stderr`` 为 ``git merge`` 的原始错误文本。返回
    ``{"kind", "files", "hint"}``：
    - ``untracked_collision``：untracked 同路径碰撞（附文件清单；处置为文件系统
      移动/删除，**非 git 写操作**，不违反红线；切勿 ``git add`` 它）；
    - ``local_changes``：主工作树已跟踪脏写阻断（提交/还原归属，主分支无手动提交
      豁免，须报告归属）；
    - ``unknown``：其余（含空 stderr），附原文摘录供人工判定。
    """
    text = stderr or ""
    m = re.search(
        r"untracked working tree files would be overwritten by merge:\s*\n"
        r"((?:[ \t]+.*\n?)+)",
        text,
    )
    if m:
        files = [
            ln.strip().strip('"').strip("'")
            for ln in m.group(1).splitlines()
            if ln.strip()
        ]
        files = [f for f in files if f]
        listing = "、".join(files) if files else "（未能解析）"
        return {
            "kind": "untracked_collision",
            "files": files,
            "hint": (
                f"主工作树有未跟踪文件阻挡合并：{listing}。"
                "用文件系统移走或删除它们（Remove-Item / 移出工作树，非 git 写操作，"
                "不违反红线；切勿 git add）后，由同一 reviewer 重试 code APPROVED。"
            ),
        }
    if "would be overwritten by merge" in text:
        return {
            "kind": "local_changes",
            "files": [],
            "hint": (
                "主工作树已跟踪改动阻挡合并：请先确认改动归属并提交/还原（主分支无"
                "手动提交豁免，归属不明请报告），后由同一 reviewer 重试 code APPROVED。"
            ),
        }
    excerpt = text.strip()[:300]
    return {
        "kind": "unknown",
        "files": [],
        "hint": (
            "merge 失败但无冲突、无可分类原因"
            + (f"（git 原文：{excerpt}）" if excerpt else "（git 无输出）")
            + "：确认 git 可用且主工作树干净后，由同一 reviewer 重试 code APPROVED。"
        ),
    }


def try_git_merge(project_root: Path, task_id: str) -> dict[str, Any] | None:
    """best-effort 将任务分支合并到主工作树的 main（task-14-merge-main-tree）。

    始终在**主工作树**内执行（``git rev-parse --git-common-dir`` 定位；flat 单会话
    即 project_root，零回归），任务 worktree 永不 checkout main。merge-wt 已废弃。

    - 成功：``{"conflict": False}``
    - 内容冲突：``{"conflict": True, "files": [...]}``（files 取权威真源
      :func:`orchd.gitops.unmerged_paths`，不再用末词启发式）
    - 非冲突失败：``{"conflict": False, "merge_diagnostic": {...}}``
      （task-merge-failure-diagnostic：stage/stderr 摘录/分类，不再裸 ``None``——
      裸 ``None`` 使 review 只能报无诊断的 ``merge_env_error`` 黑洞）
    - 环境异常（checkout 失败 / git 不可用 / 抛异常）：``None``（调用方按
      best-effort 降级，行为不变）。
    """
    trunk = resolve_trunk_for(project_root)
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        workdir = main_worktree_root(project_root)
        _clean_stale_index_lock(workdir)
        checkout = subprocess.run(
            ["git", "-C", str(workdir), "checkout", trunk],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if checkout.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "-C", str(workdir), "merge", branch],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            # 权威真源：索引中的未合并路径。必须在 merge --abort **之前**取——abort 会
            # 清掉索引未合并态，之后再查必然为空（task-conflict-true-source-fix）。
            paths = unmerged_paths(workdir)
            # 三态：非空 → 冲突；空 → 非冲突失败（透出诊断，见上）；None（测不到）→
            # 保守按冲突上报且不崩。
            if paths or paths is None:
                # P2-7：冲突后立即 abort 清理 MERGE_HEAD，避免残留中间态阻塞后续 git 操作。
                # try_auto_resolve_conflict 开头会再次 abort（幂等），此处先清理无副作用。
                _abort_merge(workdir)
                return {"conflict": True, "files": paths or []}
            err = (result.stderr or "").strip()
            return {
                "conflict": False,
                "merge_diagnostic": {
                    "stage": "merge",
                    "stderr_excerpt": err[:500],
                    **_classify_merge_failure(err),
                },
            }
        return {"conflict": False}
    except subprocess.TimeoutExpired:
        # B2（task-gate-cleanup-batch）：merge 超时同样残留 MERGE_HEAD（与冲突
        # 同源）——先 abort 再返回可诊断的 None（调用方 review 报 merge_env_error
        # 附超时诊断，原裸 None 与真环境异常不可区分）。
        _abort_merge(workdir)
        return None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _abort_merge(workdir: Path) -> None:
    """merge 中止清理（B2，task-gate-cleanup-batch）：冲突/超时共用。

    best-effort：失败静默（调用方已有降级路径），10s 有界。
    """
    try:
        subprocess.run(
            ["git", "-C", str(workdir), "merge", "--abort"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass


def try_ff_merge_to_main(
    project_root: Path, task_id: str
) -> dict[str, Any] | None:
    """W-5：仅当任务分支领先 main 时以 ``--ff-only`` 快速并入 main。

    逃生舱（force-status completed）"完成 = 状态终态 + 代码落 main" 的落码环节。
    与 ``try_git_merge``（普通 merge，可产生 merge commit/自动化解）不同：**仅接受
    快进**——任务与 main 分叉时拒绝自动合并，返回 ``diverged`` 交人工处理，防止
    静默并入错误代码。

    Returns:
        - ``{"state": "merged"}``：ff 合并成功，main 前进到任务分支。
        - ``{"state": "already_in_main"}``：任务分支不领先 main（已并入 / 无独立分支）。
        - ``{"state": "diverged", "branch": <task_branch>}``：与 main 分叉，拒绝自动合并。
        - ``None``：环境异常，调用方按 best-effort 降级。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    trunk = resolve_trunk_for(project_root)

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        workdir = main_worktree_root(project_root)
        return subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )

    try:
        _ = _git("rev-parse", "--git-dir")  # 非 git 仓库探测（返回 None 降级）
        # 任务分支不存在 → 无待落码（无独立分支即视为已并入/无实现）
        if _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            return {"state": "already_in_main"}
        trunk_is_ancestor = _git("merge-base", "--is-ancestor", trunk, branch).returncode == 0
        task_is_ancestor = _git("merge-base", "--is-ancestor", branch, trunk).returncode == 0
        if trunk_is_ancestor and task_is_ancestor:
            # trunk 与任务分支同 commit → 无待落码（已并入）
            return {"state": "already_in_main"}
        if trunk_is_ancestor:
            # 任务领先 trunk → 可快进。先确认主工作树落到 trunk（flat 布局下主工作树
            # 可能正 checkout 任务分支），再 --ff-only 快进。
            if _git("checkout", trunk).returncode != 0:
                return {"state": "diverged", "branch": branch}
            if _git("merge", "--ff-only", branch).returncode == 0:
                return {"state": "merged"}
            # 快进被拒（并发/脏工作区）→ 交人工，防止静默完成却不落码
            return {"state": "diverged", "branch": branch}
        if task_is_ancestor:
            return {"state": "already_in_main"}
        # 两者互为祖先均不成立 → 分叉，拒绝自动合并（仅接受快进，防止静默并入错误代码）
        return {"state": "diverged", "branch": branch}
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def try_delete_task_branch(project_root: Path, task_id: str) -> bool:
    """best-effort 删除任务分支 task/{task_id}（merge 成功后调用）。

    分支删除以**主工作树**为稳定 cwd（git -C）：容器布局下 project_root 是任务
    worktree，task/{id} 曾由该 worktree checkout，git 拒绝删除被占用分支；worktree
    已回收后（remove_task_wt 或分支已删）分支不再被占用，-d 成功或报分支不存在。
    flat 布局 main_worktree_root 回退 project_root，cwd 即主工作树，零回归。

    Returns:
        True：删除成功，或分支已不存在（幂等视为成功，best-effort 不抛异常）。
        False：删除失败或环境不支持。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    try:
        workdir = main_worktree_root(project_root)
        result = subprocess.run(
            ["git", "-C", str(workdir), "branch", "-d", branch],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return True
        # 分支已不存在（如 remove_task_wt 的 -D 已删）→ 幂等视为成功
        err = (result.stderr or "").lower()
        if (
            "not found" in err
            or "doesn't exist" in err
            or "does not exist" in err
        ):
            return True
        return False
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _read_task_verify_command(project_root: Path, task_id: str) -> str | None:
    """从 _master.json 读取任务的 verify_command（best-effort，失败返回 None）。

    task-master-path-resolver-convergence：master **路径解析**统一走
    :func:`orchd.worktree.resolve_master_path_from_dir`。此前裸拼
    ``project_root/.orchd/_master.json``，而调用点已把 workdir 经
    :func:`main_worktree_root` canonical 化（口径不一）——container 布局下
    project_root 可能是任务 worktree（副本被抑制）→ 取不到 verify_command，
    union 合并静默降级为逐文件 pytest。
    """
    try:
        from orchd.worktree import resolve_master_path_from_dir

        master_path = resolve_master_path_from_dir(Path(project_root) / ".orchd")
        if not master_path.exists():
            return None
        import json
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)
        for t in master.get("tasks", []):
            if t.get("id") == task_id:
                return t.get("verify_command")
    except Exception:
        pass
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
            target.write_bytes(merged_content)
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
    project_root: Path, task_id: str
) -> dict[str, Any] | None:
    """L3：merge 冲突自动化解——恢复 main → 分支 merge main 预演 → 自动合并或返回清单。

    全部 git 操作在**主工作树**内以 ``git -C`` 执行（``git rev-parse --git-common-dir``
    定位；flat 单会话即 project_root，零回归），任务 worktree 永不 checkout main。
    merge-wt 已废弃。

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
                _verify_cmd = _read_task_verify_command(project_root, task_id)
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


# ------------------------------------------------------------------
# 对账（reconcile）：任务分支 ↔ main 可合并性左移（task-done-reconcile-main）
# ------------------------------------------------------------------

# ``merge-tree --write-tree`` 需 git >= 2.38；低版本降级为 merge 预演。
_MERGE_TREE_MIN_VERSION: tuple[int, int] = (2, 38)
_merge_tree_supported: bool | None = None


def _git_version_tuple() -> tuple[int, ...]:
    """探测 git 版本（best-effort；不可用 / 解析失败返回空元组）。"""
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            return ()
        match = re.search(r"(\d+)\.(\d+)", proc.stdout or "")
        return tuple(int(x) for x in match.groups()) if match else ()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ()


def merge_tree_supported() -> bool:
    """是否支持 ``git merge-tree --write-tree``（git >= 2.38，进程级缓存）。

    低版本（< 2.38）走 :func:`reconcile_with_main` 的 merge 预演降级路径。
    """
    global _merge_tree_supported
    if _merge_tree_supported is None:
        _merge_tree_supported = _git_version_tuple() >= _MERGE_TREE_MIN_VERSION
    return _merge_tree_supported


def _split_nonempty_lines(text: str | None) -> list[str]:
    """按行切分并丢弃空行（git ``--name-only`` 输出的通用解析）。"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def resolve_task_worktree(
    project_root: Path | None,
    task_id: str,
    *,
    strict: bool = False,
) -> Path | None:
    """解析对账 / 诊断 / done 所用任务 worktree（单一来源）。

    container 布局下 ``project_root`` 若是主工作树，需解析到
    ``<task_wt_root>/task-<id>``（分支实际 checkout 处）；本身即任务 worktree
    则原样返回；flat / 解析不到回退 ``project_root``（维持既有降级语义）。
    原先该逻辑内联于 ``orchd.onboard.claim._resolve_review_worktree``，此处收敛为
    单一实现，避免对账与诊断两处漂移（task-done-reconcile-main）。

    task-done-root-resolution AC2：新增 ``strict`` 参数。当 ``strict=True`` 且
    container 布局下解析不到任务 worktree（目录缺失 / 非 git）时，抛
    ``OrchdError`` 阻断并给出可执行指引，不回退主工作树、不静默写 DONE。
    flat 布局与本身即任务 worktree 时不受 strict 影响（原样返回）。
    """
    if project_root is None:
        return None
    try:
        if is_task_worktree(project_root):
            return project_root
        from orchd.worktree import _task_wt_name, detect_layout

        layout = detect_layout(Path(project_root))
        if layout.get("layout") == "container":
            cand = Path(layout["task_wt_root"]) / _task_wt_name(task_id)
            # A0b（单目录收敛后）：无 git 即无任务 worktree 概念，仅 git 登记生效。
            if (cand / ".git").exists():
                return cand
            # AC2: strict 模式下解析不到任务 worktree 时抛错阻断
            if strict:
                raise OrchdError(
                    ErrorCode.E018,
                    f"done 执行位置解析失败：container 布局下任务 worktree 不存在 "
                    f"({cand})，无法从主工作树静默 done",
                    [{
                        "task_id": task_id,
                        "expected_worktree": str(cand),
                        "hint": (
                            f"请进入任务 worktree 执行 done：cd '{cand}'; "
                            f"python .orchd/__main__.py done --task {task_id} --changes '<描述>'；"
                            f"或先重建 worktree：git worktree add {cand} "
                            f"{resolve_task_branch_for(project_root, task_id)}"
                        ),
                    }],
                )
    except Exception as e:
        # strict 模式下的 OrchdError 直接抛出，不捕获
        if strict and isinstance(e, OrchdError):
            raise
        return Path(project_root)
    return Path(project_root)


def reconcile_with_main(
    project_root: Path | None,
    task_id: str,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """任务分支 ↔ main 对账（**单一来源**，done 前置 + reviewer 认领前置共用）。

    根因（2026-09-11~12 并发冲突复盘）：容器布局下任务分支基线在 claim 时冻结，
    main 在任务生命周期内持续推进，直到 code APPROVED 的 merge 才首次对账 →
    冲突在「两轮审查之后」爆发，恢复路径粗暴（retract + force-status + 重实现 +
    全量重审）。实测 task-test-cli-split-domains：基线 3539ea8(13:48) → 对方
    3cfcc16 于 14:00:52 落 main（晚于 done 84 秒）→ 17:48:33 撞 modify/delete →
    22:03:16 才 completed（4h15m / 1 个文件）。

    两个挂载点**共用本函数与同一触发条件**（防双写漂移）：

    - ① ``done`` 前置（``apply=True``）：覆盖「实现期间 main 推进」；
    - ② reviewer 认领前置（``apply=False``）：覆盖「done 之后、merge 之前」残余
      窗口——实测事故正落在该窗口。

    触发条件（精确，避免分支刷满 merge commit）：**仅当** main 自 merge-base
    以来的改动与本任务分支的实际改动**有交集**时才探测；main 未推进或无交集时
    零成本跳过（不产生任何 git 写操作）。

    探测优先用零副作用的 ``git merge-tree --write-tree --name-only --no-messages``
    （本机 git 2.55.0 实测：不切分支、不动工作区、无 ``MERGE_HEAD``；输出首行为
    tree OID、其余行即真实冲突路径；配 ``-c core.quotePath=false`` 避免非 ASCII
    路径被八进制转义）；git < 2.38 降级为 ``merge --no-commit --no-ff`` 预演。

    Args:
        project_root: 主工作树根（container 下经 :func:`resolve_task_worktree`
            解析到任务 worktree）。``None`` → 不探测。
        task_id: 任务 id（分支名 ``task/{task_id}``）。
        apply: ``True``（done 挂载点）时冲突**不 abort**——在任务 worktree 内保留
            合并现场（``MERGE_HEAD``）供实现者直接解决；``False``（reviewer 认领
            挂载点）时纯探测、不落地，保证审查期工作区不被触碰（不触碰 E017）。

    Returns:
        ``{"checked", "clean", "files", "reason", "worktree", "action"}``：
        ``checked`` 是否真正探测；``clean`` 探测结果（未探测 ``None``，未触发
        ``True``）；``files`` 冲突文件（真实路径，权威）；``reason`` ∈
        ``no_branch`` / ``no_main_advance`` / ``no_overlap`` / ``clean`` /
        ``conflict`` / ``git_unavailable``；``action`` 冲突时可执行指引。

    本函数**不抛异常**（best-effort）：git 异常一律降级为
    ``reason="git_unavailable"``，调用方据 ``checked`` 决定是否采信。
    """
    branch = resolve_task_branch_for(project_root, task_id)
    trunk = resolve_trunk_for(project_root)
    workdir = resolve_task_worktree(project_root, task_id)
    result: dict[str, Any] = {
        "checked": False,
        "clean": None,
        "files": [],
        "reason": "git_unavailable",
        "worktree": str(workdir) if workdir else None,
        "action": None,
    }
    if workdir is None:
        return result

    def _run(
        args: list[str], timeout: int = 30
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return None

    # 0) 任务分支存在性（不存在 → 无对账对象，静默降级）
    probe = _run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    if probe is None:
        return result
    if probe.returncode != 0:
        result["reason"] = "no_branch"
        return result

    # 1) merge-base：任务分支冻结基线与 trunk 的分叉点
    mb = _run(["merge-base", trunk, branch])
    if mb is None or mb.returncode != 0 or not (mb.stdout or "").strip():
        return result
    base = mb.stdout.strip()

    # 2) trunk 自 merge-base 以来的改动（空 → trunk 未推进 → 零成本跳过）
    trunk_diff = _run(["diff", "--name-only", base, trunk])
    if trunk_diff is None or trunk_diff.returncode != 0:
        return result
    trunk_files = _split_nonempty_lines(trunk_diff.stdout)
    if not trunk_files:
        result.update(reason="no_main_advance", clean=True)
        return result

    # 3) 本任务分支的实际改动；与 trunk 推进文件无交集 → 零成本跳过
    task_diff = _run(["diff", "--name-only", base, branch])
    if task_diff is None or task_diff.returncode != 0:
        return result
    if not (set(trunk_files) & set(_split_nonempty_lines(task_diff.stdout))):
        result.update(reason="no_overlap", clean=True)
        return result

    # 4) 触发：探测可合并性
    result["checked"] = True
    clean: bool | None = None
    files: list[str] | None = None
    staged_merge = False  # 降级路径下是否已处于（保留现场的）冲突合并态

    if merge_tree_supported():
        mt = _run(
            [
                "-c", "core.quotePath=false",
                "merge-tree", "--write-tree", "--name-only", "--no-messages",
                trunk, branch,
            ]
        )
        if mt is not None and mt.returncode in (0, 1):
            if mt.returncode == 0:
                clean = True
            else:
                clean = False
                # 首行为 tree OID，其余行为冲突路径（--no-messages 已去掉信息段）
                files = _split_nonempty_lines(mt.stdout)[1:]

    if clean is None:
        # 降级（git < 2.38 或 merge-tree 执行异常）：真合并预演
        merged = _run(["merge", "--no-commit", "--no-ff", trunk])
        if merged is None:
            result["checked"] = False
            return result
        if merged.returncode == 0:
            clean = True
            _run(["merge", "--abort"])  # 撤销预演痕迹，保持分支零残留
        else:
            paths = unmerged_paths(workdir)
            if not paths:
                # merge 失败但索引无未合并条目（脏工作区等）或测不到 → 非对账结论，
                # 降级放行（不得把非冲突误报为冲突，零回归）
                result["checked"] = False
                return result
            clean = False
            files = paths
            if apply:
                staged_merge = True  # 现场已保留，供实现者解决
            else:
                _run(["merge", "--abort"])  # 纯探测：不留现场

    if clean:
        result.update(reason="clean", clean=True)
        return result

    # 冲突：apply=True 时在任务 worktree 内落地合并、保留现场并取权威清单
    conflict_files = list(files or [])
    if apply and not staged_merge:
        _run(["merge", trunk])
        authoritative = unmerged_paths(workdir)
        if authoritative:  # 非空才采信（空 = 未落地成功，保留探测清单）
            conflict_files = authoritative
    result.update(
        reason="conflict",
        clean=False,
        files=conflict_files,
        action=(
            f"任务分支 {branch} 与 {trunk} 对账发现冲突"
            f"（{len(conflict_files) or '若干'} 个文件）："
            + ("合并现场已保留在任务 worktree（MERGE_HEAD 存在），" if apply else "")
            + "请解决冲突 → git add → git commit，然后重新 done"
        ),
    )
    return result
