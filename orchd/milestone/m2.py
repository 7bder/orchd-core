"""M2（产品闭环）十项判据：分线 / 同步 / 发版 / 制品 / 自检 / 行为 / 性能 / 命名 / 健壮。"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from .constants import (
    DRILL_ARTIFACT, DRILL_TEST_FILE, LINE_MODULE, LINE_SYNC_COMMAND, NAMING_PROBE_EXPECTED,
    NAMING_PROBE_TASK, PERF_BUDGET_CONST, PERF_BUDGET_TEST, PERF_SPAWN_BUDGET_MAX, RELEASE_SCRIPT,
)
from .context import MilestoneContext
from .m1 import _m1_b1_full_regression
from .probe import _cli_command_names, _module_spec, _perf_budget_value, _read_json


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
    # task-milestone-c1-fix：原 regex 含裸 ``--tag``，被脚本内 ``git describe --tags``
    # 子串误命中 → C1 假阳性（pass7 P2-8/C-3）。改为要求**显式线参数化 token**：
    # ``--line``（独立 token，非子串）或 ``ORCHD_LINE`` 环境变量。
    line_aware = bool(re.search(r"(?<![\w-])--line(?![\w-])", text)) or bool(
        re.search(r"(?<!\w)ORCHD_LINE(?!\w)", text)
    )
    if not line_aware:
        return False, f"{RELEASE_SCRIPT} 未按线参数化（无 --line / ORCHD_LINE）", (
            "发布脚本把 MAIN_DIR 与 push 目标写死：分线后第二线无发版通道，"
            "须显式支持 --line <name> 或 ORCHD_LINE 环境变量"
        )
    return True, "发布脚本支持按线（--line / ORCHD_LINE）", ""


def _attest_repo_head(project_root: Path, repo_head: object) -> str | None:
    """演练源锚见证（pass8 F4）：repo_head 须为 40-hex 且在当前仓库可达、
    为 HEAD 祖先；否则演练过期（仓库已推进）或产物系手写。

    Returns:
        None = 通过；否则为问题描述。git 不可用 / 非仓库一律判不通过
        （见证无从谈起，不静默放行）。
    """
    if not isinstance(repo_head, str) or not re.fullmatch(r"[0-9a-f]{40}", repo_head):
        return "演练缺有效的 repo_head 绑定（手写产物无源仓库锚点）"
    try:
        alimentare = subprocess.run(
            ["git", "cat-file", "-e", repo_head], cwd=str(project_root),
            capture_output=True, timeout=60,
        )
        if alimentare.returncode != 0:
            return f"演练源锚不可达：{repo_head} 在当前仓库无此对象"
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", repo_head, "HEAD"],
            cwd=str(project_root), capture_output=True, timeout=60,
        )
        if ancestor.returncode != 0:
            return f"演练已过期：源锚 {repo_head} 非当前 HEAD 祖先，请重跑演练"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"演练见证无法执行（{type(exc).__name__}）：请确认 git 可用后重跑演练"
    return None


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
    lifecycle = data.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get("completed") is not True:
        return False, "演练缺少第二线真实生命周期留痕", (
            "第二线须真实跑通 claim → done → review → merge（D 组 hint 口径），"
            "仅回归 + 回移不算达成；用 scripts/line_drill.py 重跑演练"
        )
    if not lifecycle.get("merge_commit"):
        return False, "演练生命周期缺合入提交（merge_commit 为空）", (
            "生命周期须以 merge 落码结束，否则 merge 语义未经演练；重跑演练"
        )
    if not data.get("drill_commit"):
        return False, "演练缺 drill_commit 绑定（手写产物无演练提交锚点）", (
            "用 scripts/line_drill.py 重跑演练（--no-lifecycle 产物判不通过）"
        )
    attest_issue = _attest_repo_head(ctx.project_root, data.get("repo_head"))
    if attest_issue is not None:
        return False, attest_issue, "用 scripts/line_drill.py 重跑演练"
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
            "line 能力落地后，单线模式任务分支名须保持 task/{id}（多线为 task/{line}/{id}）"
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
