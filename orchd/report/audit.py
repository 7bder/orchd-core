"""report — merge 审计域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行）。``merge_audit`` / main 祖先判定；
落地面在册判据拆入 ``landing`` 并重导出（保持旧路径同一对象）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.line import TASK_BRANCH_ROOT
from orchd.ledger import Store
from .acks import load_merge_acks
from .landing import (
    LandingScan, changelog_landing_scan,
    surface_landing_scan, _changelog_landing_gaps,
)


# merge 审计严重度分级（task-engine-defect-fix-unified-u12）。
#
# 根因：审计项原是无类型字符串集合（只有 reason），``merge_finalize`` 在分档前把
# 自家残留全量并入 ``merge_audit.warnings``，``worktree_not_recycled``（环境性、
# 可延迟收敛）与 ``changelog_landed_task_missing``（记账缺口）同级——既无
# severity 可回看，弹回判据也只能按 reason 硬编码。此处把审计项类型化为
# ``{code, severity, deferred}``，成为 severity 的**单一真源**：
# ``merge_audit`` 自产项与 ``review/merge_finalize`` 的窄口径自家残留共用本表
# （后者以函数级 import 消费，避免 report↔review 顶层循环依赖）。
AUDIT_SEVERITY_BLOCKING = "blocking"
AUDIT_SEVERITY_ACCOUNTING = "accounting"
AUDIT_SEVERITY_ENV = "env"
AUDIT_SEVERITY_INFO = "info"
AUDIT_SEVERITIES = (
    AUDIT_SEVERITY_BLOCKING,
    AUDIT_SEVERITY_ACCOUNTING,
    AUDIT_SEVERITY_ENV,
    AUDIT_SEVERITY_INFO,
)
# code → severity。判据口径：
# - blocking：本任务一方的硬残留（实现/分支未落地、绕过 claim 的幽灵分支）——
#   唯一允许触发 in_review 弹回的一档；
# - accounting：记账类缺口（CHANGELOG 缺行 / 不可读、cancelled 分支遗留），须补记
#   或人工处置，但不等于「实现未落地」；
# - env：环境性残留（Windows 句柄占用导致 worktree 未回收），延迟回收可收敛；
# - info：卫生提示（已并入 main 但分支未删），不阻断亦不记账。
# 未登记 code 一律按 blocking（fail-closed：宁可多打回，不静默放行）。
AUDIT_SEVERITY_BY_CODE: dict[str, str] = {
    "unregistered_task_branch": AUDIT_SEVERITY_BLOCKING,
    "branch_not_merged_into_main": AUDIT_SEVERITY_BLOCKING,
    "branch_not_cleaned": AUDIT_SEVERITY_BLOCKING,
    "merge_warning_unresolved": AUDIT_SEVERITY_BLOCKING,
    "changelog_landed_task_missing": AUDIT_SEVERITY_ACCOUNTING,
    "changelog_unreadable": AUDIT_SEVERITY_ACCOUNTING,
    "cancelled_branch_not_cleaned": AUDIT_SEVERITY_ACCOUNTING,
    "worktree_not_recycled": AUDIT_SEVERITY_ENV,
    "merged_but_not_cleaned": AUDIT_SEVERITY_INFO,
}
_DEFAULT_AUDIT_SEVERITY = AUDIT_SEVERITY_BLOCKING


def audit_severity(code: str) -> str:
    """审计 code → severity（未登记 code 按 blocking，fail-closed）。

    Args:
        code: 审计项的 ``code``（取值口径同 ``reason``）。

    Returns:
        ``blocking`` / ``accounting`` / ``env`` / ``info`` 之一。
    """
    return AUDIT_SEVERITY_BY_CODE.get(str(code), _DEFAULT_AUDIT_SEVERITY)


def classify_audit_item(item: dict[str, Any]) -> dict[str, Any]:
    """把一条审计项类型化为 ``{..., code, severity, deferred}``（幂等，只增不改）。

    ``code`` 缺省取既有 ``reason``（既有消费方按 reason 过滤，保持零回归）；
    显式给出的 ``code`` / ``severity`` 优先（供上层覆写）。``deferred`` = 非
    blocking，语义为「只注解、不触发 in_review 弹回」，与
    ``review/merge_finalize`` 的分档判定同口径。

    Args:
        item: 原始审计项（至少含 ``reason`` 或 ``code``）。

    Returns:
        新 dict（不改入参），保留原键并补 ``code`` / ``severity`` / ``deferred``。
    """
    out = dict(item)
    code = str(out.get("code") or out.get("reason") or "")
    out["code"] = code
    out["severity"] = str(out.get("severity") or audit_severity(code))
    out["deferred"] = out["severity"] != AUDIT_SEVERITY_BLOCKING
    return out


def classify_audit_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """逐条类型化审计项（保持原顺序；见 :func:`classify_audit_item`）。"""
    return [classify_audit_item(item) for item in items]


def merge_audit(
    store: Store,
    tasks: list[dict[str, Any]],
    project_root: Path,
) -> dict[str, Any]:
    """只读 merge 巡检：task/* 分支与 main 的并入情况 + 未注册幽灵分支检测。

    扫描全部 task/* 分支（含未注册的幽灵分支）与 main 的差异：
    - 已注册任务的 completed 分支：main 不包含其 tip 时告警
      （branch_not_merged_into_main）；已并入 main 但分支未删告警
      （merged_but_not_cleaned）。
    - 已注册任务的 cancelled 分支：只要分支仍存在即告警
      （cancelled_branch_not_cleaned，含 ahead_count）——终态回收对未合并分支
      默认拒绝删除（W-3 红线：不得用 -D 销毁未合并提交），取消后的分支会留在
      仓库里；旧实现只巡 completed，使这类残留完全静默。可见化后由人决定人工
      处置或走 force-status --force-recycle（task-force-status-force-recycle-flag）。
    - 进行中（pending / claimed / done / in_review）任务分支不告警。
    - 未注册任务的幽灵分支（agent 绕过 claim 手动建的分支）：立即告警
      （unregistered_task_branch），含分支名与差异摘要。
    - 落地任务 CHANGELOG 在册（task-merge-audit-changelog-landing-gate，见
      :func:`_changelog_landing_gaps`）：自上个发布标签以来已落地但未在
      ``CHANGELOG.md`` 在册的任务 id → ``changelog_landed_task_missing``
      warning（含 id 列表与文件路径）。**只告警、不阻断**——合入侧拦截会造成
      「本卡自己的 CHANGELOG 行未写就合不进来」的死锁口。
    只读：不执行 merge/checkout/reset/commit/push。非 git 仓库、无 main
    分支或 git 不可用时返回 skipped 说明，不抛异常。

    Args:
        store: ledger Store（replay 取任务状态）。
        tasks: _master.json 的 tasks[] 定义列表。
        project_root: 仓库根目录（git 命令 cwd）。

    Returns:
        {"skipped": False, "warnings": [{"task_id"?, "branch", "reason",
        "code", "severity", "deferred", "ahead_count"}]} 或
        {"skipped": True, "reason": "..."}。
        幽灵分支告警无 task_id 字段（无注册任务）。每项经
        :func:`classify_audit_item` 类型化：``code`` 与 ``reason`` 同值（既有
        消费方零回归），``severity`` ∈ {blocking, accounting, env, info}，
        ``deferred`` 表示「只注解、不触发 in_review 弹回」。
    """
    import subprocess

    root = Path(project_root)

    # 单根命名空间（pass8 F1-A）：任务分支恒以 task/ 为根，全线通用
    from orchd.line import parse_task_branch
    from orchd.line_ctx import resolve_trunk_for

    trunk = resolve_trunk_for(root)
    branch_prefix = TASK_BRANCH_ROOT

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=str(root),
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )

    def _ahead(branch: str) -> int:
        revs = _git("rev-list", "--count", f"{trunk}..{branch}")
        if revs.returncode != 0:
            return 0
        return int(revs.stdout.strip() or "0")

    try:
        if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return {"skipped": True, "reason": "not_a_git_repo"}
        if _git("rev-parse", "--verify", trunk).returncode != 0:
            return {"skipped": True, "reason": "no_main_branch"}
    except (subprocess.SubprocessError, OSError):
        return {"skipped": True, "reason": "git_unavailable"}

    state = store.replay()
    registered_ids = {t.get("id", "") for t in tasks}
    warnings: list[dict[str, Any]] = []
    try:
        # for-each-ref 用裸前缀（无 *）：`*` 通配不跨 `/`（实证），裸前缀才含嵌套
        # task/{line}/{id}；与 request.py _inflight_files 同口径
        refs = _git(
            "for-each-ref", "--format=%(refname:short)", f"refs/heads/{branch_prefix}"
        )
    except (subprocess.SubprocessError, OSError):
        return {"skipped": False, "warnings": warnings}
    branch_names = [
        ln.strip() for ln in refs.stdout.splitlines() if ln.strip()
    ] if refs.returncode == 0 else []

    # 已并入 main 的分支集合（git branch --merged main 判定）：tip 为 main 祖先
    merged_out = _git("branch", "--merged", trunk)
    merged_branches: set[str] = set()
    if merged_out.returncode == 0:
        for ln in merged_out.stdout.splitlines():
            name = ln.strip().lstrip("*").strip()
            if name:
                merged_branches.add(name)

    for branch in branch_names:
        # 单一真源（task-branch-parse-single-source）：经 line.parse_task_branch
        # 反解（task/{id} -> id；task/{line}/{id} -> id）；不可解析回退旧切片
        # （逐字节零回归；将来 parse 改实现此处自动跟随）。
        _parsed = parse_task_branch(branch)
        if _parsed is not None:
            tid = _parsed[1]
        else:
            tid = (
                branch[len(TASK_BRANCH_ROOT):]
                if branch.startswith(TASK_BRANCH_ROOT) else branch
            )
        if tid not in registered_ids:
            # 未注册幽灵分支：agent 绕过 claim 手动建的分支，立即告警
            warnings.append({
                "branch": branch,
                "reason": "unregistered_task_branch",
                "ahead_count": _ahead(branch),
            })
            continue
        ts = state.get(tid)
        if not ts or ts.status not in TERMINAL_STATUSES:
            continue  # 进行中任务的分支不告警
        ahead = _ahead(branch)
        if ts.status == "cancelled":
            # 终态回收对未合并分支默认拒绝删除（W-3 红线），取消后的任务分支会留在
            # 仓库里；旧实现只巡 completed → 这类残留完全静默。此处显式可见化。
            warnings.append({
                "task_id": tid,
                "branch": branch,
                "reason": "cancelled_branch_not_cleaned",
                "ahead_count": ahead,
            })
            continue
        if ahead > 0:
            warnings.append({
                "task_id": tid,
                "branch": branch,
                "reason": "branch_not_merged_into_main",
                "ahead_count": ahead,
            })
        elif branch in merged_branches:
            # 已并入 main 但分支未删（卫生）：只读告警，不自动清理
            warnings.append({
                "task_id": tid,
                "branch": branch,
                "reason": "merged_but_not_cleaned",
            })

    # B1（2026-08-13 full-audit-v2）：merge 降级盲区——completed 任务含
    # merge_warning 但 task 分支已删（merge 未落地 + audit 失明）→ 单独告警。
    # 分支仍存在时由上循环的 branch_not_merged / merged_but_not_cleaned 覆盖。
    # 自动销账（task-merge-warning-resolve-sha，2026-08-24）：merge_warning 事件
    # 附加 resolve_sha（task 分支 tip）；main 已包含该 sha（git merge-base
    # --is-ancestor 为真，即手工补 merge 已落地）→ 不再告警（resolve_sha 无效
    # 或缺失的旧事件维持告警，人工核对兜底）。
    # 同上经 parse_task_branch 反解为任务 id 集合（不可解析回退旧切片，
    # 非分线输出与现状逐字节一致）。
    branch_ids = set()
    for _b in branch_names:
        _p = parse_task_branch(_b)
        if _p is not None:
            branch_ids.add(_p[1])
        elif _b.startswith(TASK_BRANCH_ROOT):
            branch_ids.add(_b[len(TASK_BRANCH_ROOT):])
        else:
            branch_ids.add(_b)
    resolve_sha_by_task: dict[str, str] = {}
    try:
        for ev in store.backend.read_events():
            if ev.get("type") == "REVIEW_SUBMITTED" and ev.get("resolve_sha"):
                resolve_sha_by_task.setdefault(ev.get("task_id", ""), ev["resolve_sha"])
    except Exception:
        resolve_sha_by_task = {}
    # 人工销账清单（task-merge-warning-ack）：resolve_sha 自动判定未覆盖的
    # 旧事件 / 无 sha 场景，由人工确认后跳过告警。
    merge_acks = load_merge_acks(root)
    for tid in sorted(registered_ids):
        ts = state.get(tid)
        if not ts or ts.status != "completed" or not ts.merge_warning:
            continue
        if tid not in branch_ids:
            sha = resolve_sha_by_task.get(tid)
            if sha and _is_ancestor_of_main(root, sha):
                continue  # main 已含实现（resolve_sha 为 main 祖先）→ 自动销账
            if tid in merge_acks:
                continue  # 人工已确认（merge-acks 清单）→ 人工销账
            warnings.append({
                "task_id": tid,
                "reason": "merge_warning_unresolved",
                "message": ts.merge_warning,
                "hint": "该任务标记完成但 git merge 未执行（best-effort 降级），"
                        "且 task 分支已删除，无法从分支差异核对；"
                        "请人工确认 main 是否含实现",
            })

    # 落地任务须在 CHANGELOG 在册（task-merge-audit-changelog-landing-gate）：
    # 判据真源 = gitops.query 的两个真源函数（不自建 git 遍历、不复制判据），
    # 命中只并入 warnings（注解层）——不进窄口径拦截分支：拦截会让「本卡自己的
    # CHANGELOG 行尚未写」变成「合不进来」的死锁口。
    warnings.extend(_changelog_landing_gaps(root))
    # 出口统一类型化（task-engine-defect-fix-unified-u12）：逐项补
    # code/severity/deferred，使 report/audit（status --audit-merge）与 review
    # 响应的 merge_audit 回看都带分档信息。只增字段，reason 不变。
    return {"skipped": False, "warnings": classify_audit_items(warnings)}


def _is_ancestor_of_main(project_root: Path, sha: str) -> bool:
    """当前线 trunk 是否已包含 sha（``git merge-base --is-ancestor <sha> <trunk>``）。

    task-line-audit-guide-wiring：trunk 按当前线解析（单线恒 main）。

    Args:
        project_root: 仓库根目录（git 命令 cwd，与 merge_audit 一致）。
        sha: 待判定的 commit SHA（resolve_sha）。

    Returns:
        True：sha 是 trunk 的祖先（实现已并入主干）；False：未包含 / git 不可用。
    """
    import subprocess

    from orchd.line_ctx import resolve_trunk_for

    trunk = resolve_trunk_for(project_root)
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, trunk],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False