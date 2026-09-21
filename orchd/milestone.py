"""orchd/milestone.py — 里程碑判据门禁（**单一真源**）。

**M1 · 单机内核冻结**（id ``local-kernel-freeze``，出口 ``v1.5.0``）与
**M2 · 分线就绪**（id ``line-split-ready``，出口 ``v1.6.0``）的达成判据在本模块
**只定义一处**；``ROADMAP.md`` 的「## 里程碑」节只声明判据**组名**与出口版本，
不复述明细——避免"文档与实现双写必漂移"。判据变更只改本模块。

**为什么机制化**：M-FREEZE 若靠"人记得跑清单"，会重犯 F3 修
``status --audit-merge`` 时的同一个错（靠人记得 = 迟早名义化）。故判据收成
``orchd milestone-check <freeze|split-ready>``：输出 ``ready`` / 逐条 ``not_ready``
+ 修复指引，**退出码语义与 ``full-regression`` 一致**（0 = 达成，非 0 = 未达成）。

**复用既有门禁（不重复实现探测）**：B 质量组的四项直接调用既有实现——
``_full_regression.json``（``orchd full-regression`` 维护）/ ``test_baseline.json``
（``scripts/check_test_baseline.py`` 维护）/ ``validate``（``_cmd_validate``）/
``status --audit-merge``（``orchd.report.merge_audit``）。

**零影响**：本模块只被 ``orchd/cli/commands/misc.py`` 的 handler 惰性导入，
**不进入 claim / done / review 热路径**（AC6）。

**M2 的 D 组演练产物格式**（``<账本根>/line-drill.json``）：::

    {
      "lines": ["main", "line-drill"],
      "regressions": {
        "main": {"passed": 2900, "failed": 0},
        "line-drill": {"passed": 2880, "failed": 0}
      },
      "backport": [
        {"task_id": "...", "source_sha": "...",
         "target_line": "line-drill", "event": "..."}
      ],
      "created_at": "2026-09-21T00:00:00+00:00"
    }

``regressions`` 两线均 ``failed == 0`` 且 ``backport`` 非空（回移留痕）才算演练通过。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, NamedTuple

from orchd.errors import ErrorCode, OrchdError

# ------------------------------------------------------------------
# 单一真源常量（判据变更只改这里）
# ------------------------------------------------------------------

# 里程碑 → 出口版本
EXIT_VERSIONS: dict[str, str] = {
    "freeze": "v1.5.0",
    "split-ready": "v1.6.0",
}

# 里程碑 id（与 ROADMAP「## 里程碑」节声明一致）
MILESTONE_IDS: dict[str, str] = {
    "freeze": "local-kernel-freeze",
    "split-ready": "line-split-ready",
}

# 性能冻结口径：``tests/test_perf_budget.py`` 的 git 子进程上限**不得上调**
PERF_BUDGET_TEST = "tests/test_perf_budget.py"
PERF_BUDGET_CONST = "PERF_BASELINE_WATCHDOG_FLOW_GIT_SPAWNS"
PERF_SPAWN_BUDGET_MAX = 40

# M2 演练产物与命名口径
DRILL_ARTIFACT = "line-drill.json"
DRILL_TEST_FILE = "tests/test_line_drill.py"
NAMING_PROBE_TASK = "t1"
NAMING_PROBE_EXPECTED = "task/t1"
LINE_MODULE = "orchd.line"
LINE_SYNC_COMMAND = "line-sync"
RELEASE_SCRIPT = "scripts/sync_orchd_core.sh"

# 契约文档与关键锚点（文档缺失 / 锚点缺失即未达成）
KERNEL_CONTRACT_DOC = "docs/kernel-contract.md"
KERNEL_CONTRACT_ANCHORS = ("不变量", "端口")
ROADMAP_FILE = "ROADMAP.md"
ROADMAP_MILESTONE_ANCHOR = "## 里程碑"

_FULL_REGRESSION_FILE = "_full_regression.json"
_BASELINE_FILE = "test_baseline.json"


class MilestoneContext(NamedTuple):
    """判据求值上下文（只读）。"""

    project_root: Path          # 主工作树（HEAD 与文档基准）
    master_dir: Path            # canonical master 所在目录（.orchd）
    store: Any                  # orchd.ledger.Store（账本边界）
    tasks: list[dict[str, Any]]  # 任务定义
    state: dict[str, Any]       # store.replay() 结果（任务 → 状态）


class _Check(NamedTuple):
    id: str
    group: str
    title: str
    fn: Callable[[MilestoneContext], tuple[bool, str, str]]


# ------------------------------------------------------------------
# 通用探针（best-effort，绝不抛异常击穿门禁输出）
# ------------------------------------------------------------------


def _git_head_short(project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root), capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _status_counts(ctx: MilestoneContext) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ts in (ctx.state or {}).values():
        status = getattr(ts, "status", None) or "unknown"
        counts[str(status)] = counts.get(str(status), 0) + 1
    return counts


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _module_spec(name: str) -> Any:
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def _cli_command_names() -> set[str]:
    """已注册的 CLI 子命令名集合（走 parser 发现，与契约测试同口径）。"""
    try:
        from orchd.cli.parser import _build_parser

        parser = _build_parser()
    except Exception:
        return set()
    groups = [
        a for a in getattr(parser, "_subparsers", None)._group_actions  # type: ignore[union-attr]
        if hasattr(a, "choices") and a.choices
    ]
    if not groups:
        return set()
    return set(groups[0].choices)


def _perf_budget_value(root: Path) -> int | None:
    """解析 ``tests/test_perf_budget.py`` 的 git 子进程预算上限（冻结口径真源）。"""
    path = root / PERF_BUDGET_TEST
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(rf"^{PERF_BUDGET_CONST}\s*=\s*(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------
# M1 · 单机内核冻结（freeze）判据
# ------------------------------------------------------------------


def _m1_a1_active_clear(ctx: MilestoneContext) -> tuple[bool, str, str]:
    counts = _status_counts(ctx)
    active = {k: counts.get(k, 0) for k in ("in_review", "claimed", "done")}
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
        if getattr(ctx.state.get(str(t.get("id"))), "status", "pending") == "pending"
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
    terminal = {"completed", "cancelled"}
    offenders: list[str] = []
    for task in ctx.tasks:
        tid = str(task.get("id"))
        status = getattr(ctx.state.get(tid), "status", "pending")
        if status in terminal:
            continue
        for dep in task.get("depends_on") or []:
            dep_status = getattr(ctx.state.get(str(dep)), "status", "pending")
            if dep_status not in terminal:
                offenders.append(f"{tid} ← {dep}({dep_status})")
    if offenders:
        return False, f"依赖未闭合（非终态任务依赖非终态任务）：{offenders[:5]}", (
            "按依赖序完成上游任务（orchd request 会按 DAG 复合排序发牌）；依赖闭合是"
            "「池已收敛」的必要条件"
        )
    return True, "非终态任务的依赖全部为终态", ""


def _m1_b1_full_regression(ctx: MilestoneContext) -> tuple[bool, str, str]:
    path = ctx.master_dir / _FULL_REGRESSION_FILE
    data = _read_json(path)
    if data is None:
        return False, f"{path.name} 不存在或不可解析", (
            "跑 python .orchd/__main__.py full-regression 并全绿（发版门禁的覆盖检查会硬拦）"
        )
    head = _git_head_short(ctx.project_root)
    recorded = str(data.get("last_pass_commit") or "")
    if not head:
        return False, "无法解析当前 HEAD（git 探测失败）", "确认 git 可用后重试"
    if not recorded.startswith(head[:7]) and not head.startswith(recorded[:7]):
        return False, f"全量回归记录点 {recorded or '<空>'} ≠ HEAD {head}", (
            "HEAD 已推进：重跑 orchd full-regression 刷新 last_pass_commit"
        )
    return True, f"全量回归已覆盖 HEAD {head}（记录于 {recorded}）", ""


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


# ------------------------------------------------------------------
# M2 · 分线就绪（split-ready）判据
# ------------------------------------------------------------------


def _m2_a1_line_capability(ctx: MilestoneContext) -> tuple[bool, str, str]:
    spec = _module_spec(LINE_MODULE)
    if spec is None:
        return False, f"{LINE_MODULE} 不存在（引擎尚无 line 能力）", (
            "落地多主干 opt-in 能力：trunk 解析单一真源（显式配置）+ 变更基线 / merge / "
            "巡检 / 守卫 / 分支命名空间按当前线解析（task-line-split-refactor）"
        )
    try:
        import orchd.line as _line

        if not callable(getattr(_line, "resolve_trunk", None)):
            return False, f"{LINE_MODULE} 存在但未暴露 resolve_trunk()", (
                "line 能力的稳定入口是 resolve_trunk()（单线模式须返回 default trunk）"
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"{LINE_MODULE} 导入失败：{type(exc).__name__}: {exc}", "修导入错误"
    return True, "line 能力在册（resolve_trunk 可用）", ""


def _m2_b1_line_sync_channel(ctx: MilestoneContext) -> tuple[bool, str, str]:
    names = _cli_command_names()
    if LINE_SYNC_COMMAND not in names:
        return False, f"CLI 未注册 '{LINE_SYNC_COMMAND}'（回移通道缺失）", (
            "落地回移通道：定位源提交 → 目标线建回移任务（带源 sha 引用）→ 走目标线审查与回归"
            "→ 双线各留痕（禁止影子改动）"
        )
    return True, f"回移通道在册（orchd {LINE_SYNC_COMMAND}）", ""


def _m2_c1_release_per_line(ctx: MilestoneContext) -> tuple[bool, str, str]:
    path = ctx.project_root / RELEASE_SCRIPT
    if not path.is_file():
        return False, f"{RELEASE_SCRIPT} 不存在", "确认发布脚本位置（发版链入口）"
    text = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"--line|ORCHD_LINE|--tag", text):
        return False, f"{RELEASE_SCRIPT} 未支持按线 / 按 tag 归属发版", (
            "发布脚本当前把 MAIN_DIR 与 push main 写死：分线后第二线无发版通道，须先参数化"
        )
    return True, "发布脚本支持按线 / 按 tag 归属", ""


def _m2_d1_drill_artifact(ctx: MilestoneContext) -> tuple[bool, str, str]:
    try:
        from orchd.ledger import resolve_store_dir

        root = resolve_store_dir(ctx.master_dir)
    except Exception:  # noqa: BLE001
        root = ctx.master_dir
    path = Path(root) / DRILL_ARTIFACT
    data = _read_json(path)
    if data is None:
        return False, f"演练产物缺失：{path}", (
            "双线并行演练：开临时第二线，端到端跑通 claim → done → review → merge → 回移，"
            f"并落 {DRILL_ARTIFACT}（字段规范见 orchd/milestone.py 模块 docstring）"
        )
    regressions = data.get("regressions") or {}
    bad = [ln for ln, r in regressions.items() if (r or {}).get("failed")]
    if not regressions or bad:
        return False, f"演练回归未两线全绿（{bad or '无 regressions 记录'}）", (
            "两线各自回归必须全绿：演练通过的唯一可验证形态是「双线回归全绿」"
        )
    if not (data.get("backport") or []):
        return False, "演练缺少回移留痕（backport 为空）", (
            "回移必须留痕（源 sha + 目标线 + 事件），否则无法回答「这个修复在不在另一条线」"
        )
    return True, f"演练产物在册（两线全绿，回移 {len(data.get('backport') or [])} 条）", ""


def _m2_e1_gate_self(ctx: MilestoneContext) -> tuple[bool, str, str]:
    return True, "split-ready 判据清单单一真源于本模块；命令输出结构化结论与退出码", ""


def _m2_f1_behavior(ctx: MilestoneContext) -> tuple[bool, str, str]:
    return _m1_b1_full_regression(ctx)


def _m2_f2_perf(ctx: MilestoneContext) -> tuple[bool, str, str]:
    value = _perf_budget_value(ctx.project_root)
    if value is None:
        return False, f"无法解析 {PERF_BUDGET_TEST} 的 {PERF_BUDGET_CONST}", (
            "性能口径真源缺失：确认 perf 预算测试与常量名未被改名（判据要求上限不得上调）"
        )
    if value > PERF_SPAWN_BUDGET_MAX:
        return False, f"perf 预算上限被上调：{value} > 冻结值 {PERF_SPAWN_BUDGET_MAX}", (
            "单机零回归口径：spawn 调用次数不得增长，上限只可下调；优化须附次数下降证据"
        )
    return True, f"perf 预算上限 {value} ≤ 冻结值 {PERF_SPAWN_BUDGET_MAX}", ""


def _m2_f3_naming(ctx: MilestoneContext) -> tuple[bool, str, str]:
    if _module_spec(LINE_MODULE) is None:
        return False, f"{LINE_MODULE} 未落地：单线默认命名口径无法判定", (
            "line 能力落地后，单线模式任务分支名须保持 task/{id}（不得泄漏为 {line}/task/{id}）"
        )
    try:
        from orchd.line import resolve_task_branch_name

        got = resolve_task_branch_name(NAMING_PROBE_TASK)
    except Exception as exc:  # noqa: BLE001
        return False, f"命名探针失败：{type(exc).__name__}: {exc}", (
            "line 模块须提供 resolve_task_branch_name(task_id)；单线模式返回 task/{id}"
        )
    if got != NAMING_PROBE_EXPECTED:
        return False, f"单线默认命名漂移：{got} ≠ {NAMING_PROBE_EXPECTED}", (
            "分支命名空间变化会泄漏给单机用户；单线模式须保持 task/{id}（或提供显式迁移）"
        )
    return True, f"单线默认命名保持 {NAMING_PROBE_EXPECTED}", ""


def _m2_f4_robustness(ctx: MilestoneContext) -> tuple[bool, str, str]:
    if not (ctx.project_root / DRILL_TEST_FILE).is_file():
        return False, f"演练用例缺失：{DRILL_TEST_FILE}", (
            "分线改动的健壮性口径须有可重放断言：新增降级路径须幂等 / 可重放，"
            "不得引入新的半完成态"
        )
    return True, f"演练用例在册（{DRILL_TEST_FILE}）", ""


# ------------------------------------------------------------------
# 判据清单（唯一真源）
# ------------------------------------------------------------------

MILESTONES: dict[str, dict[str, Any]] = {
    "freeze": {
        "title": "M1 · 单机内核冻结",
        "groups": [
            ("A", "范围", [
                _Check("A1.active_clear", "A", "活跃任务清零",
                       _m1_a1_active_clear),
                _Check("A2.pool_priority_clear", "A", "池内无 P0/P1 待领项",
                       _m1_a2_pool_priority_clear),
                _Check("A3.dep_closure", "A", "依赖闭合", _m1_a3_dep_closure),
            ]),
            ("B", "质量（复用既有门禁）", [
                _Check("B1.full_regression_head", "B", "全量回归覆盖 HEAD",
                       _m1_b1_full_regression),
                _Check("B2.baseline_fresh", "B", "测试基线新鲜且零红",
                       _m1_b2_baseline_fresh),
                _Check("B3.validate_errors_zero", "B", "validate 零错误",
                       _m1_b3_validate),
                _Check("B4.merge_audit_clean", "B", "merge audit 零告警",
                       _m1_b4_merge_audit),
            ]),
            ("C", "契约", [
                _Check("C1.kernel_contract", "C", "冻结契约文档在册",
                       _m1_c1_kernel_contract),
                _Check("C2.roadmap_milestones", "C", "ROADMAP 里程碑节引用本命令",
                       _m1_c2_roadmap_milestones),
            ]),
            ("D", "机制", [
                _Check("D1.gate_self_check", "D", "判据机制化自检", _m1_d1_gate_self),
            ]),
        ],
    },
    "split-ready": {
        "title": "M2 · 分线就绪",
        "groups": [
            ("A", "引擎", [
                _Check("A1.line_capability", "A", "多主干 line 能力", _m2_a1_line_capability),
            ]),
            ("B", "回移", [
                _Check("B1.line_sync_channel", "B", "回移通道 + 留痕",
                       _m2_b1_line_sync_channel),
            ]),
            ("C", "治理", [
                _Check("C1.release_per_line", "C", "发布按线 / 按 tag 归属",
                       _m2_c1_release_per_line),
            ]),
            ("D", "演练", [
                _Check("D1.drill_artifact", "D", "双线并行演练通过",
                       _m2_d1_drill_artifact),
            ]),
            ("E", "机制", [
                _Check("E1.gate_self_check", "E", "判据机制化自检", _m2_e1_gate_self),
            ]),
            ("F", "单机零回归口径", [
                _Check("F1.behavior", "F", "行为：全量回归覆盖 HEAD", _m2_f1_behavior),
                _Check("F2.perf", "F", "性能：预算上限不得上调", _m2_f2_perf),
                _Check("F3.naming", "F", "命名：单线保持 task/{id}", _m2_f3_naming),
                _Check("F4.robustness", "F", "健壮性：演练用例可重放", _m2_f4_robustness),
            ]),
        ],
    },
}


def milestone_names() -> list[str]:
    """可用里程碑名（CLI choices 单一来源）。"""
    return sorted(MILESTONES)


def build_context(
    *,
    orchd_dir: Path,
    tasks: list[dict[str, Any]],
    store: Any,
) -> MilestoneContext:
    """构造判据上下文（master 目录 / 主工作树 / 账本状态）。"""
    orchd_dir = Path(orchd_dir)
    # task-master-path-residual-convergence：经单一真源解析（本地优先 →
    # canonical 回退；read_layout best-effort，损坏按缺失处理，故无需 try）。
    from orchd.worktree import resolve_master_path_from_dir

    master_path = resolve_master_path_from_dir(orchd_dir)
    try:
        state = store.replay()
    except Exception:  # noqa: BLE001 - 无账本时按空状态求值（A 组会如实报 not_ready）
        state = {}
    return MilestoneContext(
        project_root=master_path.parent.parent,
        master_dir=master_path.parent,
        store=store,
        tasks=tasks,
        state=state,
    )


def evaluate(milestone: str, ctx: MilestoneContext) -> dict[str, Any]:
    """求值一个里程碑的全部判据，返回结构化结论（**纯读**，不写任何状态）。

    Args:
        milestone: ``freeze`` / ``split-ready``（见 :func:`milestone_names`）。
        ctx: 判据上下文。

    Returns:
        ``{"milestone", "milestone_id", "title", "exit_version", "ready",
        "summary", "groups", "not_ready", "single_source"}``。
    """
    if milestone not in MILESTONES:
        raise OrchdError(
            ErrorCode.E007,
            f"unknown_milestone: 未知里程碑 '{milestone}'",
            [{
                "milestone": milestone,
                "available": milestone_names(),
                "hint": "用法：orchd milestone-check freeze | split-ready",
            }],
        )
    spec = MILESTONES[milestone]
    groups: list[dict[str, Any]] = []
    not_ready: list[dict[str, Any]] = []
    total = 0
    ready_count = 0
    for gid, gtitle, checks in spec["groups"]:
        items: list[dict[str, Any]] = []
        for check in checks:
            total += 1
            try:
                ok, detail, hint = check.fn(ctx)
            except Exception as exc:  # noqa: BLE001 - 判据故障按未达成并留痕，不吞
                ok, detail, hint = False, f"判据故障：{type(exc).__name__}: {exc}", (
                    "判据自身异常按未达成处理（fail-closed）；请修判据实现或环境后重试"
                )
            if ok:
                ready_count += 1
            else:
                not_ready.append({
                    "id": check.id, "group": gid, "title": check.title,
                    "detail": detail, "hint": hint,
                })
            items.append({"id": check.id, "title": check.title, "ready": ok,
                          "detail": detail, "hint": hint})
        groups.append({
            "id": gid,
            "title": gtitle,
            "ready": all(i["ready"] for i in items),
            "checks": items,
        })
    ready = ready_count == total
    return {
        "milestone": milestone,
        "milestone_id": MILESTONE_IDS[milestone],
        "title": spec["title"],
        "exit_version": EXIT_VERSIONS[milestone],
        "ready": ready,
        "summary": {"total": total, "ready": ready_count,
                    "not_ready": total - ready_count},
        "groups": groups,
        "not_ready": not_ready,
        "single_source": "orchd/milestone.py（判据清单只在此定义；ROADMAP 只引用不复述）",
    }
