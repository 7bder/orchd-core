"""lifecycle core entry_guard 域 — 共享入口 verify 覆盖门禁（E039）。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

两条规则（registry / global_shared_file）同码不同 rule；判定输入 = 分支已提交 diff
∪ 工作树未提交改动。依赖 ``baseline`` 域提供基线既有谓词。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    GUARD_STATUS_NOT_APPLICABLE,
    main_worktree_root,
    record_degraded_guard,
)
from orchd.shared_entries import (
    GLOBAL_SHARED_FILES,
    SHARED_ENTRY_TESTS,
    missing_baseline_test_coverage,
    missing_shared_entry_tests,
)

from .baseline import _baseline_test_predicate, _nogit_manifest_probe, _uncommitted_changes


def _guard_shared_entry_coverage(
    project_root: Path | None,
    task_def: dict[str, Any],
    task_id: str,
    files_to_edit: list[str],
    degraded_guards: list[dict[str, Any]] | None = None,
) -> None:
    """共享入口改动的 verify 覆盖门禁（E039，task-shared-entry-verify-gate）。

    两条判定规则，同一错误码（救济路径一致：补 verify_command 后重试），由 details.rule 区分：

    - ``registry``：任务分支**实际改动** ∩ 登记表（:mod:`orchd.shared_entries` 的
      ``SHARED_ENTRY_TESTS``）非空时，登记表为该入口指定的既有测试文件必须全部出现在
      ``verify_command`` 的 pytest 目标里；
    - ``global_shared_file``（形态 A，task-global-shared-file-verify-rule）：改动
      ``GLOBAL_SHARED_FILES`` 里的**全局共享文件**（如 ``tests/conftest.py``，影响面无法
      枚举）时，pytest 目标中至少要有**一个基线既有**（本任务动手前已存在于基线分支，基线
      由 :func:`_resolve_baseline_ref` 解析）的测试文件——全量 pytest 禁入 verify_command，
      形态 A 是预算约束下可实现的弱约束替代。

    「实际改动」= **分支已提交 diff ∪ 工作树未提交改动**（task-shared-entry-gate-coverage-
    hardening）。只看已提交 diff 有实测漏检：引擎直到本链路靠后阶段
    （:func:`_commit_and_verify_integrity` → ``ensure_committed``）才 auto-commit，故
    「提交了无关文件、把共享入口留在工作树未提交」的任务会整段逃出视野（实测同仓库
    ``task_branch_files=['other.py']`` 而 ``list_tracked_changes=['orchd/cli/__init__.py']``）。

    为什么这里 fail-open（同族其它守卫多为 fail-closed）：本门禁防「漏跑既有测试」，而
    「未触及入口」与「改动清单取不到」在 best-effort 的
    :func:`orchd.worktree.task_branch_files` 下不可区分——取不到就阻断会把 flat
    布局等正常任务卡死。故取不到改动清单时退化为用声明的 ``files_to_edit`` 判定（声明
    通常覆盖实际改动），两者皆空则放行；形态 A 的基线 ref 全不可解析时同样跳过（fail-open），
    但**留痕不静默**（task-e039-escape-hatch-bypass-fix，pass6 Q-2）：记 ``degraded_guards``
    （E030 / not_applicable）。该 fail-open 的兜底前提=越界 / 声明门禁自身 fail-closed，锁定
    用例见 ``tests/test_done.py::TestDoneGuardFailClosed``。

    空 ``verify_command``（task-e039-gate-self-injury-fix，pass5 N-4）：不静默放行——改动
    命中受管入口时记 ``degraded_guards``（E030 / not_applicable），把「门禁没在守」变成可
    检索事实；仍不阻断（存量 / 纯文档任务可能本就没有 verify_command）。
    """
    verify_cmd = str(task_def.get("verify_command") or "")
    changed: list[str] = []
    if project_root:
        # E039 输入经端口 GitBackend 直连（与原 task_branch_files 同函数；
        # 无 git 时同返空列表→回退声明判定，行为零漂移。快照语义不外溢至此：
        # 本门禁的"已提交"即分支 diff，单目录另由声明/越界门禁覆盖）。
        from orchd.gitops.repo import GitBackend

        try:
            changed = list(GitBackend(Path(project_root)).changed_paths(task_id))
        except Exception:
            # diff 基建故障（如 git 子进程异常）⇒ 退化为声明判定（fail-open）：
            # 「取不到 diff」与「未触及入口」不可区分，且该故障场景下越界/声明门禁
            # 自身会 fail-closed 阻断 done（见 TestDoneGuardFailClosed），本门禁不得
            # 抢先抛出与门禁语义无关的裸异常。
            changed = []
        else:
            # 未提交改动同样计入（见 docstring 实测漏检）；取失败时不影响已提交部分。
            changed += _uncommitted_changes(Path(project_root))
    changed = changed or list(files_to_edit or [])
    if not verify_cmd:
        # 空 verify ⇒ 覆盖门禁无从判定。**不得静默放行**（pass5 N-4）：若改动确实命中受管
        # 入口（登记表 / 全局共享文件），记结构化降级（E030 / not_applicable）供审计与
        # doctor 按码检索；不阻断——存量与纯文档任务可能本就没有 verify_command，阻断会卡死
        # 正常流转（缺 verify_command 的代码类任务已在注册期被 validate 阻断）。
        managed = set(SHARED_ENTRY_TESTS) | set(GLOBAL_SHARED_FILES)
        touched = sorted({str(p).replace("\\", "/") for p in changed} & managed)
        if touched:
            record_degraded_guard(
                degraded_guards,
                guard_name="shared_entry_coverage",
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason="verify_command 为空 ⇒ 共享入口覆盖门禁无从判定",
                context={"task_id": task_id, "touched": touched},
                # 提示只描述动作，不写 amend 旗标串：补登命令的单一生成源是 guide.py
                # （禁止手写 amend 命令串，见 task-amend-decl-patch-channel）。
                hint="该任务缺少 verify_command，覆盖门禁无从判定；补齐自检命令后重试 done",
            )
        return

    gaps = missing_shared_entry_tests(changed, verify_cmd)
    rule = "registry"
    if not gaps:
        # 形态 A 需要读 git 基线（基线分支上是否存在目标文件）⇒ 无 project_root 时无法判定，
        # 按 fail-open 跳过（口径见 docstring）；基线 ref 不可解析时由谓词侧留痕。
        baseline_gaps = (
            missing_baseline_test_coverage(
                changed,
                verify_cmd,
                _nogit_manifest_probe(Path(project_root), task_id, degraded_guards)
                or _baseline_test_predicate(Path(project_root), degraded_guards),
            )
            if project_root
            else None
        )
        if baseline_gaps is None:
            return
        if not baseline_gaps:
            return
        gaps, rule = baseline_gaps, "global_shared_file"

    missing = []
    for gap in gaps:
        required = gap.get("required_test")
        token = (
            f"{gap['shared_entry']} → {required}"
            if required
            else f"{gap['shared_entry']} → 至少一个基线既有测试"
        )
        if token not in missing:
            missing.append(token)
    # hint 里的补登命令由 guide 单一生成（禁止手写 amend 命令串）；主工作树路径失败时
    # 退化为不带 cd 前缀的形态（amend 本就只能在主工作树执行，提示仍可读）。
    from orchd.guide import amend_mainwt_command, amend_patch_cmd

    placeholder = "<把缺失的登记测试加入 pytest 目标后的命令>"
    patch_cmd = amend_patch_cmd(task_id, verify=placeholder)
    if project_root:
        try:
            from orchd.gitops import main_worktree_root

            patch_cmd = amend_mainwt_command(
                task_id, main_worktree_root(Path(project_root)), verify=placeholder
            )
        except Exception:  # noqa: BLE001 - hint 构造 best-effort，不掩盖门禁的阻断
            patch_cmd = amend_patch_cmd(task_id, verify=placeholder)

    if rule == "registry":
        message = "shared_entry_verify_gap: verify_command 未覆盖共享入口的登记测试"
        hint = (
            "共享入口被改动时，其既有断言必须随任务一起跑（否则静默变红入库）；"
            f"缺失 {', '.join(missing)}；补登命令 {patch_cmd}"
        )
    else:
        message = "shared_entry_verify_gap: 全局共享文件改动未覆盖任一基线既有测试"
        hint = (
            "全局共享文件影响面无法枚举（改它会改变其它测试文件的行为），故要求在 pytest "
            "目标中至少包含一个基线既有（本任务动手前已存在于基线分支）的测试文件——"
            "全量 pytest 禁入 verify_command，形态 A 是预算约束下的弱约束替代；"
            f"缺失 {', '.join(missing)}；补登命令 {patch_cmd}"
        )
    raise OrchdError(
        ErrorCode.E039,
        message,
        [{
            "task_id": task_id,
            "rule": rule,
            "gaps": gaps,
            "verify_command": verify_cmd,
            "hint": hint,
        }],
    )
