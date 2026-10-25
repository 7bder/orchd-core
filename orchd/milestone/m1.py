"""M1（内核稳定）九项判据：活跃面 / 池优先 / 闭包 / 回归 / 基线 / validate / 审计 / 契约 / 自检。"""

from __future__ import annotations

import argparse

from orchd.contracts.events import (
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
from .constants import (
    KERNEL_CONTRACT_ANCHORS, KERNEL_CONTRACT_DOC, ROADMAP_FILE, ROADMAP_MILESTONE_ANCHOR,
    _BASELINE_FILE, _FULL_REGRESSION_FILE,
)
from .context import MilestoneContext
from .probe import _git_head_short, _read_json, _sha_covers, _status_counts


def _m1_a1_active_clear(ctx: MilestoneContext) -> tuple[bool, str, str]:
    counts = _status_counts(ctx)
    active = {
        k: counts.get(k, 0)
        for k in (STATUS_IN_REVIEW, STATUS_CLAIMED, STATUS_DONE)
    }
    bad = {k: v for k, v in active.items() if v}
    if bad:
        return False, f"仍有活跃任务：{bad}", (
            "先清审查积压（in_review）与在握认领（claimed），再领实现（见 rules/session.md"
            "「工作优先级」）；活跃项清零才具备冻结资格"
        )
    return True, "活跃任务为空（in_review / claimed / done 均为 0）", ""


def _m1_a2_pool_priority_clear(ctx: MilestoneContext) -> tuple[bool, str, str]:
    pending = [
        t for t in ctx.tasks
        if (getattr(ctx.state.get(str(t.get("id"))), "status", STATUS_PENDING)
                == STATUS_PENDING)
        and str(t.get("importance") or "normal") in ("high", "critical")
    ]
    if pending:
        ids = sorted(str(t.get("id")) for t in pending)
        return False, f"就绪池仍有 P0/P1（high/critical）待领项：{ids}", (
            "M1 要求 1.4.6 / 1.4.7 全部 P0/P1 收口：先领完这些任务（或经 amend 降级 / "
            "cancelled 显式出清），再判冻结"
        )
    return True, "池内无 high/critical 待领项", ""


def _m1_a3_dep_closure(ctx: MilestoneContext) -> tuple[bool, str, str]:
    terminal = TERMINAL_STATUSES
    offenders: list[str] = []
    for task in ctx.tasks:
        tid = str(task.get("id"))
        status = getattr(ctx.state.get(tid), "status", STATUS_PENDING)
        if status in terminal:
            continue
        for dep in task.get("depends_on") or []:
            dep_status = getattr(ctx.state.get(str(dep)), "status", STATUS_PENDING)
            if dep_status not in terminal:
                offenders.append(f"{tid} ← {dep}({dep_status})")
    if offenders:
        return False, f"依赖未闭合（非终态任务依赖非终态任务）：{offenders[:5]}", (
            "按依赖序完成上游任务（orchd request 会按 DAG 复合排序发牌）；依赖闭合是"
            "「池已收敛」的必要条件"
        )
    return True, "非终态任务的依赖全部为终态", ""


def _m1_b1_full_regression(ctx: MilestoneContext) -> tuple[bool, str, str]:
    """F1 / M1-B1：全量回归证据覆盖 HEAD——本地记录 + 已推送 + 远端 CI 三重齐备。

    卡 A（task-ci-evidence-authority）：此前只认本地 ``last_pass_commit``，于是
    「本地绿 + 若干提交未推 + CI 停在旧 HEAD」也能盖章（2026-10-12 实测：本地领先 67
    提交、CI 最近 6 条 run 全红，而本判据照绿）。证据须与实况同源，故三重都落在 HEAD 上：
    ① 本地记录点（本机跑过的全量回归）；② 推送状态（``gitops.query.upstream_state`` 单一真源）；
    ③ 远端 CI 结论（``orchd ci-record`` 落库，可离线复算）。任一项缺失即 not_ready——
    显式失败而非静默放行（无 gh / 无网络 / 记录未落 = 缺字段 = 不通过）。
    """
    path = ctx.master_dir / _FULL_REGRESSION_FILE
    data = _read_json(path)
    if data is None:
        return False, f"{path.name} 不存在或不可解析", (
            "跑 python .orchd/__main__.py full-regression 并全绿（发版门禁的覆盖检查会硬拦）"
        )
    head = _git_head_short(ctx.project_root)
    if not head:
        return False, "无法解析当前 HEAD（git 探测失败）", "确认 git 可用后重试"
    recorded = str(data.get("last_pass_commit") or "")
    if not _sha_covers(head, recorded):
        return False, f"全量回归记录点 {recorded or '<空>'} ≠ HEAD {head}", (
            "HEAD 已推进：重跑 orchd full-regression 刷新 last_pass_commit"
        )
    from orchd.gitops.query import upstream_state

    up = upstream_state(ctx.project_root)
    if up["state"] != "pushed":
        return False, f"HEAD {head} 未证明已推送（{up['detail']}）", (
            "证据须来自远端已验证的历史：commit → push → CI 绿 → "
            "python .orchd/__main__.py ci-record"
        )
    ci_sha = str(data.get("ci_head_sha") or "")
    if not ci_sha:
        return False, f"缺远端 CI 记录（{path.name} 无 ci_head_sha）", (
            "推送后跑 python .orchd/__main__.py ci-record 记录该 HEAD 的 CI 结论"
        )
    if not _sha_covers(head, ci_sha):
        return False, f"远端 CI 记录点 {ci_sha} ≠ HEAD {head}", (
            "CI 记录对应的是旧提交：推送后重跑 ci-record"
        )
    conclusion = str(data.get("ci_conclusion") or "")
    if conclusion != "success":
        return False, f"远端 CI 结论非 success：{conclusion or '<空>'}", (
            "CI 红或仍在跑：修红重推，待该 HEAD 的 run 完成后再 ci-record"
        )
    return True, (
        f"全量回归 + 远端 CI 均已覆盖 HEAD {head}"
        f"（记录于 {recorded}，CI run {data.get('ci_run_id')}）"
    ), ""


def _m1_b2_baseline_fresh(ctx: MilestoneContext) -> tuple[bool, str, str]:
    path = ctx.master_dir / _BASELINE_FILE
    data = _read_json(path)
    if data is None:
        return False, f"{path.name} 不存在或不可解析", (
            "在全绿提交上记录基线：python scripts/check_test_baseline.py --record "
            ".orchd/test_baseline.json"
        )
    head = _git_head_short(ctx.project_root)
    recorded = str(data.get("commit") or "")
    if not recorded or (head and recorded != head):
        return False, f"基线提交 {recorded or '<空>'} ≠ HEAD {head or '<未知>'}", (
            "基线陈旧会让 pre-push 的「新增失败」判据失真：重跑 --record 刷新基线"
        )
    failed = data.get("failed") or []
    errors = data.get("errors") or []
    if failed or errors:
        return False, f"基线非零红：failed={len(failed)} errors={len(errors)}", (
            "先修红或显式登记已知失败，再刷新基线"
        )
    return True, f"基线新鲜且零红（commit={recorded}, passed={data.get('passed')}）", ""


def _m1_b3_validate(ctx: MilestoneContext) -> tuple[bool, str, str]:
    try:
        from orchd.cli.commands.init import _cmd_validate
        from orchd.worktree import resolve_master_path_from_dir

        result = _cmd_validate(argparse.Namespace(
            # task-master-path-residual-convergence：master_dir 即 canonical
            # .orchd，经单一真源解析（与 build_context 同源，零裸拼）。
            path=str(resolve_master_path_from_dir(ctx.master_dir)),
            include_terminal=False,
            full=False,
        ))
    except Exception as exc:  # noqa: BLE001 - 门禁自身故障按未达成处理并留痕
        return False, f"validate 复用失败：{type(exc).__name__}: {exc}", (
            "确认 .orchd/_master.json 可读；validate 是本组判据的既有真源"
        )
    # _cmd_validate 在结构错误时返回 (dict, exit_code) 元组（task-cli-exit-honesty）；
    # 此处只关心 payload，取出元组首元素后再读 valid / errors_summary。
    if isinstance(result, tuple):
        result = result[0]
    if not result.get("valid"):
        total = (result.get("errors_summary") or {}).get("total")
        return False, f"validate errors 非空（valid=False, total={total}）", (
            "修掉结构 / 引用错误：python .orchd/__main__.py validate .orchd/_master.json"
        )
    return True, "validate valid=True（结构 / 引用零错误）", ""


def _m1_b4_merge_audit(ctx: MilestoneContext) -> tuple[bool, str, str]:
    try:
        from orchd.report import merge_audit

        result = merge_audit(ctx.store, ctx.tasks, ctx.project_root)
    except Exception as exc:  # noqa: BLE001
        return False, f"merge_audit 复用失败：{type(exc).__name__}: {exc}", (
            "确认 git 可用且仓库可读；audit 是本组判据的既有真源"
        )
    warnings = result.get("warnings") or []
    if warnings:
        return False, f"merge audit 告警 {len(warnings)} 条（悬空 / 未并入分支）", (
            "清理未并入 main 的任务分支（或补 merge-ack）；零告警是冻结硬要求"
        )
    if result.get("skipped"):
        return False, f"merge audit 未执行（skipped，reason={result.get('reason')}）", (
            "巡检被跳过不等于零告警：确认 git 环境后重试"
        )
    return True, "merge audit 零告警", ""


def _m1_c1_kernel_contract(ctx: MilestoneContext) -> tuple[bool, str, str]:
    path = ctx.project_root / KERNEL_CONTRACT_DOC
    if not path.is_file():
        return False, f"{KERNEL_CONTRACT_DOC} 不存在", (
            "冻结的物证是「端口契约文档」：补齐端口清单 + 不变量 + 每端口测试锚点"
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [a for a in KERNEL_CONTRACT_ANCHORS if a not in text]
    if missing:
        return False, f"{KERNEL_CONTRACT_DOC} 缺少锚点：{missing}", (
            "契约文档须含端口清单与不变量清单（冻结面 = 语义不变量 + 端口契约）"
        )
    return True, f"{KERNEL_CONTRACT_DOC} 在册且含端口 / 不变量锚点", ""


def _m1_c2_roadmap_milestones(ctx: MilestoneContext) -> tuple[bool, str, str]:
    path = ctx.project_root / ROADMAP_FILE
    if not path.is_file():
        return False, f"{ROADMAP_FILE} 不存在", "补齐 ROADMAP 并声明里程碑节"
    text = path.read_text(encoding="utf-8", errors="replace")
    if ROADMAP_MILESTONE_ANCHOR not in text:
        return False, f"{ROADMAP_FILE} 缺少 '{ROADMAP_MILESTONE_ANCHOR}' 节", (
            "在 ROADMAP 新增「## 里程碑」节：只声明判据组名与出口版本，引用本命令（不复述明细）"
        )
    if "milestone-check" not in text:
        return False, f"{ROADMAP_FILE} 未引用 milestone-check（单一真源接线缺失）", (
            "里程碑节须指向 orchd milestone-check（判据清单只在本模块定义）"
        )
    return True, "ROADMAP 里程碑节在册且引用 milestone-check", ""


def _m1_d1_gate_self(ctx: MilestoneContext) -> tuple[bool, str, str]:
    return True, (
        "判据清单单一真源于 orchd/milestone.py；命令可输出 ready / 逐条 not_ready + 修复指引，"
        "退出码语义同 full-regression"
    ), ""
