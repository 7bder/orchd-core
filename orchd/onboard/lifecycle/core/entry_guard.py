"""lifecycle core entry_guard 域 — 共享入口 verify 覆盖门禁（E039）。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

三条规则（registry / global_shared_file / subpackage_entry）同码不同 rule；
判定输入 = 分支已提交 diff ∪ 工作树未提交改动。依赖 ``baseline`` 域提供基线既有谓词。

第四个 rule ``drift_closure`` 由 ``_record_drift_closure``（B1，task-guard-drift-done-closure）
在 hard 档命中硬触发时使用：判定面在 :mod:`orchd.drift_closure`，本域只做档位分派与出口
（warn 走 ``degraded_guards``、hard 复用本门禁的 E039 通道，不新增错误码 / 通道 / 返回位）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import (
    GUARD_STATUS_FAILED,
    GUARD_STATUS_NOT_APPLICABLE,
    main_worktree_root,
    record_degraded_guard,
)
from orchd.repo_role import is_engine_repo
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

    三条判定规则，同一错误码（救济路径一致），由 details.rule 区分：

    - ``registry``：任务分支**实际改动** ∩ 登记表（:mod:`orchd.shared_entries` 的
      ``SHARED_ENTRY_TESTS``）非空时，登记表为该入口指定的既有测试文件必须全部出现在
      ``verify_command`` 的 pytest 目标里；
    - ``global_shared_file``（形态 A，task-global-shared-file-verify-rule）：改动
      ``GLOBAL_SHARED_FILES`` 里的**全局共享文件**（如 ``tests/conftest.py``，影响面无法
      枚举）时，pytest 目标中至少要有**一个基线既有**（本任务动手前已存在于基线分支，基线
      由 :func:`_resolve_baseline_ref` 解析）的测试文件——全量 pytest 禁入 verify_command，
      形态 A 是预算约束下可实现的弱约束替代。

    - ``subpackage_entry``（task-decl-subpackage-test-anchor）：拆包子包入口（非顶层
      ``orchd/**/__init__.py``）既无 E039 键（含尾斜杠包级通配）、不在``CLI_COMMAND_MODULE_EXEMPT``
      / ``SUBPACKAGE_ENTRY_EXEMPT``、子树无锚点、又非纯重导出时阻断——把 W6-C8 的「拆包后
      原单键失明」从人工补录变成机判。该规则与 ``verify_command`` **无关**（入口决策
      问题，不是覆盖问题），故判定在「空 verify early return」之前；只阻断本任务实际触及的
      入口，无关缺口记 ``degraded_guards`` 留痕不阻断（防误伤其它任务）。

    「实际改动」= **分支已提交 diff ∪ 工作树未提交改动**（task-shared-entry-gate-coverage-
    hardening）。只看已提交 diff 有实测漏检：引擎直到本链路靠后阶段
    （:func:`_commit_and_verify_integrity` → ``ensure_committed``）才 auto-commit，故
    「提交了无关文件、把共享入口留在工作树未提交」的任务会整段逃出视野（实测同仓库
    ``task_branch_files=['other.py']`` 而 ``list_tracked_changes=['orchd/cli/__init__.py']``）。

    **适用范围（task-host-guard-repo-scope）**：``registry`` 与 B1/B2（drift / 生成物）只对
    引擎仓评估（:func:`orchd.repo_role.is_engine_repo`）。宿主引擎在 ``.orchd/orchd``、登记
    测试与生成器不随包，硬套会 E039 误阻断并刷降级噪音；宿主布局直接跳过（不留痕）。
    ``global_shared_file`` 与 ``subpackage_entry`` 语义不变。

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
    # 仓角色（task-host-guard-repo-scope）：registry（E039）与 drift/generated 是引擎仓
    # 自研门禁（登记测试/生成器不随包）；宿主布局硬套只会误阻断或刷降级噪音。
    # project_root=None 保持既有行为（测试 / 无仓上下文），由 repo_role 单一真源判定。
    engine_repo = project_root is None or is_engine_repo(Path(project_root))
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
    # 宿主 guard 通用接线（task-host-guard-done-wiring）：配置属宿主面，**不受 engine_repo
    # 门控**——引擎仓无配置时本调用零痕迹；有配置时按 off/warn/hard 分派（warn 只写
    # degraded_guards，hard 复用本函数的 E039 通道 + details.rule=<guard 名>）。
    _record_host_guards(project_root, task_id, changed, verify_cmd, degraded_guards)
    # 改动集镜像闭环（B1，task-guard-drift-done-closure）：**只消费上面已算好的 changed**
    # （不重算 diff、不遍历目录树、不起子进程），按三处只读映射源算「改了真源却缺镜像」。
    # 三档由 ORCHD_DRIFT_CLOSURE 控制：warn（默认）走 degraded_guards 不改变 done 结果；
    # hard 复用本函数的 E039 通道 + details.rule="drift_closure"（不新增错误码 / 不改通道
    # 登记 / 不改 guide 词表）；off 完全不跑。与 E039 分工：registry 规则已判覆盖缺口的
    # 共享入口 key 进 skip_shared_keys，本模块不为同一 key 再出一条（同一事实不重复报）。
    # **仅在 verify_command 非空时**才算跳过集：空 verify 时 missing_shared_entry_tests 会把
    # 全部触及 key 都报成缺口（E039 此时并不判、只记 not_applicable），照抄会吞掉最需要提示的场景。
    # 仅引擎仓评估：宿主缺 design/scripts 映射源，降级=不适用，跳过且不留痕。
    if engine_repo:
        degraded = degraded_guards
        _record_drift_closure(project_root, task_id, changed, degraded, verify_cmd=verify_cmd)
        _record_generated_stale(project_root, task_id, degraded)
    # 拆包子包入口决策守卫（task-decl-subpackage-test-anchor）：拆包后原单文件键失明
    # （W6-C8 实测），入口既无 E039 键/豁免、子树无锚点、又非纯重导出时必须显式决策。
    # 审查返修（spec review Q2）：本守卫原先只在测试里被调用、无生产调用点，等于装饰；
    # 现挂到 done 的共享入口门禁路径，缺口即 E039 阻断。
    if project_root:
        from orchd.subpackage_guard import subpackage_entries_unaccounted

        try:
            unaccounted = subpackage_entries_unaccounted(Path(project_root))
        except OSError as exc:
            # 与同函数其它分支同纪律（best-effort 判定 + 留痕不静默 + 不阻断）：
            # 目录列举异常（Windows 共享冲突等）不应以裸异常冒泡成 done 失败——
            # 本门禁守的是「登记决策」，取不到扫描面时按降级处理并显式记 E030。
            record_degraded_guard(
                degraded_guards,
                guard_name="subpackage_entry_coverage",
                status=GUARD_STATUS_FAILED,
                reason="拆包子包入口扫描失败（orchd/ 目录不可读）",
                error=f"{type(exc).__name__}: {exc}",
                context={"task_id": task_id},
                hint="确认 orchd/ 目录可读后重试 done（该门禁降级不影响其它阻断）",
            )
            unaccounted = []
        # 审查返修（spec re-review Q2c）：只阻断**本任务触及**的未决策入口。旧写法对
        # 全仓缺口一律 raise，会把「与本任务无关的历史遗漏」变成所有任务的 done 阻塞。
        # 无关缺口改为 degraded 留痕（不静默、不误伤），由后续任务清理。
        changed_set = {str(path).replace("\\", "/") for path in changed}
        touched_entries = [path for path in unaccounted if path in changed_set]
        unrelated = [path for path in unaccounted if path not in changed_set]
        if unrelated:
            record_degraded_guard(
                degraded_guards,
                guard_name="subpackage_entry_coverage",
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason="仓库存在与本任务无关的未决策拆包子包入口",
                context={"task_id": task_id, "entries": unrelated},
                hint=(
                    "这些入口拆包后无人决策（E039 对其子树静默失明），"
                    "建议另开任务补 E039 键或 SUBPACKAGE_ENTRY_EXEMPT 豁免"
                ),
            )
        if touched_entries:
            entry_hint = (
                "拆包子包入口未做出决策（拆包后 E039 会对整棵子树静默失明）："
                "任选其一——入 SHARED_ENTRY_TESTS（支持尾斜杠包级通配）、"
                "入 SUBPACKAGE_ENTRY_EXEMPT 并写理由、让子树任一文件有键、"
                "或把它整理成纯重导出（顶层只有 import/字面量 __all__）；缺口："
                + ", ".join(touched_entries)
            )
            raise OrchdError(
                ErrorCode.E039,
                "subpackage_entry_unaccounted: 拆包子包入口既无登记键也未豁免",
                [{
                    "task_id": task_id,
                    "rule": "subpackage_entry",
                    "gaps": [
                        {"shared_entry": path, "required_test": None}
                        for path in touched_entries
                    ],
                    "hint": entry_hint,
                }],
            )

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

    gaps = missing_shared_entry_tests(changed, verify_cmd) if engine_repo else []
    rule = "registry"
    if not gaps:
        # 形态 A 需要读 git 基线（基线分支上是否存在目标文件）⇒ 无 project_root 时无法判定，
        # 按 fail-open 跳过（口径见 docstring）；基线 ref 不可解析时由谓词侧留痕。
        # **先判是否真的触及全局共享文件再构造谓词**：谓词构造会读 git 基线并可能留
        # E030 降级痕，未触及 conftest 时 eager 构造会给每个任务平白加一条噪音。
        changed_posix = {str(path).replace("\\", "/") for path in changed}
        touches_global = bool(changed_posix & set(GLOBAL_SHARED_FILES))
        baseline_gaps = (
            missing_baseline_test_coverage(
                changed,
                verify_cmd,
                _nogit_manifest_probe(Path(project_root), task_id, degraded_guards)
                or _baseline_test_predicate(Path(project_root), degraded_guards),
            )
            if project_root and touches_global
            else ([] if project_root else None)
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


def _drift_skip_keys(changed: list[str], verify_cmd: str) -> set[str]:
    """B1 应跳过的共享入口 key = E039 已判「改了入口但 verify_command 未覆盖登记测试」的那些。

    **空 ``verify_command`` 一律不跳过**：此时 ``missing_shared_entry_tests`` 会把全部触及 key 都
    报成缺口，而 E039 本身并不判（只记 not_applicable 后放行）——照抄跳过集会吞掉「没有 verify
    可依赖」这种最需要提示的场景（spec 一审打回项）。
    """
    if not verify_cmd:
        return set()
    return {
        str(gap.get("shared_entry") or "")
        for gap in missing_shared_entry_tests(changed, verify_cmd)
    }


def _drift_patch(project_root: Path | None, task_id: str) -> str:
    """本任务改动（分支已提交 ∪ 工作树未提交/暂存）的 unified diff 文本（best-effort）。

    ``done`` 期 ``changed`` 是路径级集合，无法区分「真源语义改动」与「纯注释/空白改动」；
    本函数取同一口径的 diff 文本，交给 :func:`orchd.drift_closure.semantic_paths_from_patch`
    做内容级过滤（u15，AC1 端到端）。无 git / 分支不可解析 / 超时 → 返回空串，调用方回落
    路径级判定（旧口径），不得因取 diff 失败卡死 done。
    """
    if project_root is None or not task_id:
        return ""
    try:
        from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for
        from orchd.worktree.layout import _GIT_TIMEOUT

        trunk = resolve_trunk_for(project_root)
        branch = resolve_task_branch_for(project_root, task_id)
        base = f"{trunk}...{branch}"
        parts: list[str] = []
        for args in (
            ["git", "diff", "--unified=0", base],  # 分支已提交改动
            ["git", "diff", "--unified=0", "HEAD"],  # 工作树未提交 / 已暂存改动
        ):
            proc = subprocess.run(
                args,
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
            )
            if proc.returncode == 0:
                parts.append(proc.stdout)
        return "".join(parts)
    except Exception:  # noqa: BLE001 - best-effort：取不到 diff 回落路径级判定
        return ""


def _record_drift_closure(
    project_root: Path | None,
    task_id: str,
    changed: list[str],
    degraded_guards: list[dict[str, Any]] | None,
    verify_cmd: str = "",
) -> None:
    """改动集镜像闭环（B1，task-guard-drift-done-closure）的接入壳。

    判定本身在 :mod:`orchd.drift_closure`（纯只读聚合，三处映射源：design §6「第二真源」列 /
    ``orchd/docsgen/generators.py`` 输出清单 / ``SHARED_ENTRY_TESTS``）；本函数只做**档位分派**
    与出口选择，保持「不新增错误码、不新增通道、不新增 ``_done_precheck`` 返回位」：

    - ``off``：不评估（真不跑：连 gap 计算都不发生），done 结果零变化；
    - ``warn``（默认）：全部 gap 写 ``degraded_guards``（E030 / not_applicable，与「空
      verify_command 时无从判定」同族留痕纪律），**不阻断**；
    - ``hard``：硬触发四类（schema / CLI 面 / 生成物 / census）有命中即抛 **E039**
      （``details.rule="drift_closure"``，E039 既有三 rule 之外的第四个同模式 rule），
      非硬类仍只进 degraded。

    评估故障（映射源解析异常）与「映射源解析出空」同纪律：记 ``degraded_guards``（E030 /
    failed）不阻断——本门禁守的是「记得不记得补镜像」，不得因映射源不可读而卡死 done；但也
    不得静默恒绿（那正是「看起来在守、实际守空」）。

    Args:
        project_root: 仓库根（映射源读取面）；``None`` 时只剩共享入口类判定。
        task_id: 任务 id（进 degraded / details 的上下文）。
        changed: **已算好**的改动集（分支 diff ∪ 工作树改动）。
        degraded_guards: 门禁降级登记表（done 响应统一挂载）。
        verify_cmd: done 的 verify_command；E039 已判覆盖缺口的共享入口 key 由本函数内部算出来跳过。
    """
    mode = "unknown"  # 兜底：import/档位解析失败时 except 里的 context 仍可用
    try:
        # import / 档位 / 跳过集都进 try：`off` 档必须真的「一点都不跑」（连 skip 集都不算），
        # 且任何一环异常都只能降级留痕，不得逃出去把 done 打成 E999。
        from orchd import drift_closure

        mode = drift_closure.resolve_mode()
        if mode == drift_closure.MODE_OFF:
            return
        skip_shared_keys = _drift_skip_keys(changed, verify_cmd)
        # 健康度与判定同批算：两者都可能读映射源，任一异常都按降级留痕处理（不裸崩 done）。
        problems = drift_closure.mapping_problems(project_root)
        # u15：把本任务 diff 文本一并注入，纯注释/空白改动不再算真源改动（取不到则回落路径级）。
        patch = _drift_patch(project_root, task_id)
        gaps = drift_closure.mirror_gaps(
            changed,
            project_root=project_root,
            skip_shared_keys=skip_shared_keys,
            patch=patch or None,
        )
    except Exception as exc:  # noqa: BLE001 - 映射源故障降级留痕，不阻断 done
        record_degraded_guard(
            degraded_guards,
            guard_name="drift_closure",
            status=GUARD_STATUS_FAILED,
            reason="改动集镜像闭环评估失败（映射源解析异常），本次未做判定",
            error=f"{type(exc).__name__}: {exc}",
            context={"task_id": task_id, "mode": mode},
            hint="确认 design §6 决策表与 orchd/docsgen/generators.py 可读后重试 done",
        )
        return
    if problems:
        record_degraded_guard(
            degraded_guards,
            guard_name="drift_closure_mapping",
            status=GUARD_STATUS_FAILED,
            reason="镜像闭环映射源不可用或解析为空：对应类别本次无法判定",
            context={"task_id": task_id, "mode": mode, "problems": problems},
            hint=(
                "确认 design §6 列数未变、docsgen/generators.py 的 GENERATORS 结构未变、"
                "tests 包可导入、体量普查基线存在；修复前该类别不报缺口（不等于通过）"
            ),
        )
    if not gaps:
        return
    payload = [gap.as_dict() for gap in gaps]
    hint = _drift_closure_hint(gaps)
    blocking = [gap for gap in gaps if gap.hard] if mode == drift_closure.MODE_HARD else []
    if blocking:
        raise OrchdError(
            ErrorCode.E039,
            "drift_closure_gap: 改动集缺少镜像（改了真源但未同批更新其镜像）",
            [{
                "task_id": task_id,
                "rule": "drift_closure",
                "mode": mode,
                "gaps": payload,
                "hint": hint,
            }],
        )
    record_degraded_guard(
        degraded_guards,
        guard_name="drift_closure",
        status=GUARD_STATUS_NOT_APPLICABLE,
        reason=f"改动集缺 {len(gaps)} 个镜像（{mode} 档：仅提示，未阻断 done）",
        context={"task_id": task_id, "mode": mode, "gaps": payload},
        hint=hint,
    )


#: degraded / details 里逐条列出的 gap 上限（防长响应；完整清单仍在 context.gaps）。
_DRIFT_HINT_LIMIT = 8
#: hint 总长度预算（与 guide 的 _truncate 同口径：超长截断并标注条数）。
_DRIFT_HINT_MAX = 600


def _drift_closure_hint(gaps: list[Any]) -> str:
    """逐条列出「哪个真源缺哪个镜像」并给出补法（不改 done 判定，纯提示文本）。"""
    lines = [
        f"[{gap.klass}] {gap.source} → 需同步 {', '.join(gap.mirrors)}"
        for gap in gaps[:_DRIFT_HINT_LIMIT]
    ]
    if len(gaps) > _DRIFT_HINT_LIMIT:
        lines.append(f"（另有 {len(gaps) - _DRIFT_HINT_LIMIT} 条见 gaps）")
    how = (
        "补法：生成物类 → python -m orchd.docsgen --write docs/_generated；"
        "census 类 → python scripts/file_census.py --write --force-raise（并写抵偿）"
        "或补 orchd/subdomain_census.py 登记；其余 → 补齐同域镜像并说明理由"
    )
    text = "；".join(lines) + "。" + how
    return text if len(text) <= _DRIFT_HINT_MAX else text[:_DRIFT_HINT_MAX] + "…（已截断）"


def _record_generated_stale(
    project_root: Path | None,
    task_id: str,
    degraded_guards: list[dict[str, Any]] | None,
) -> None:
    """生成物新鲜度正交判据（B2，task-guard-drift-b2-generated-stale-gate）的接入壳。

    判定在 :mod:`orchd.drift_closure::generated_stale_gaps`（逐产物比对 ``gen_*_md()``
    与磁盘内容，不看改动集）；本函数只做**档位分派**与出口选择，与
    :func:`_record_drift_closure` 同纪律：不新增错误码、不新增通道、不新增返回位——

    - ``off``：不评估（真不跑）；
    - ``warn``（默认）：命中与降级条目全部写 ``degraded_guards``（E030），**不阻断**；
    - ``hard``：命中即抛 **E039**（``details.rule="generated_stale"``，drift_closure 之外的
      第五个同模式 rule）。

    评估故障与成本超限一律降级留痕不阻断（fail-open + 不静默，同 drift_closure 家族纪律：
    本门禁守的是「产物新鲜不新鲜」，不得因生成器不可读而卡死 done，也不得静默恒绿）。

    Args:
        project_root: 仓库根（生成器导入与产物读取面）；``None`` 时零判定。
        task_id: 任务 id（进 degraded / details 的上下文）。
        degraded_guards: 门禁降级登记表（done 响应统一挂载）。
    """
    mode = "unknown"  # 兜底：import/档位解析失败时 except 里的 context 仍可用
    try:
        from orchd import drift_closure

        mode = drift_closure.resolve_mode()
        if mode == drift_closure.MODE_OFF:
            return
        gaps, degradates, _elapsed = drift_closure.generated_stale_gaps(project_root)
    except Exception as exc:  # noqa: BLE001 - 评估故障降级留痕，不阻断 done
        record_degraded_guard(
            degraded_guards,
            guard_name="generated_stale",
            status=GUARD_STATUS_FAILED,
            reason="生成物新鲜度评估失败，本次未做判定",
            error=f"{type(exc).__name__}: {exc}",
            context={"task_id": task_id, "mode": mode},
            hint="确认 orchd.docsgen 可导入后重试 done",
        )
        return
    for item in degradates:
        record_degraded_guard(
            degraded_guards,
            guard_name="generated_stale_degraded",
            status=GUARD_STATUS_FAILED,
            reason=str(item.get("reason") or "生成物新鲜度降级留痕"),
            context={"task_id": task_id, "mode": mode, **item},
            hint="生成物新鲜度判定未完整执行（见 reason）；不等于通过",
        )
    if not gaps:
        return
    payload = [gap.as_dict() for gap in gaps]
    stale_list = ", ".join(
        mirror for gap in payload for mirror in gap.get("mirrors", [])
    )
    if mode == drift_closure.MODE_HARD:
        raise OrchdError(
            ErrorCode.E039,
            "generated_stale: 磁盘产物与生成器输出不一致（未 regen 即 done）",
            [{
                "task_id": task_id,
                "rule": "generated_stale",
                "mode": mode,
                "gaps": payload,
                "hint": (
                    f"过期产物：{stale_list}；"
                    "补法：python -m orchd.docsgen --write docs/_generated "
                    "后重试 done"
                ),
            }],
        )
    record_degraded_guard(
        degraded_guards,
        guard_name="generated_stale",
        status=GUARD_STATUS_NOT_APPLICABLE,
        reason=f"{len(gaps)} 个生成物磁盘内容过期（{mode} 档：仅提示，未阻断 done）",
        context={"task_id": task_id, "mode": mode, "gaps": payload},
        hint="补法：python -m orchd.docsgen --write docs/_generated",
    )


#: 宿主 guard hard 档命中的统一 hint（动作导向，不写 amend 命令串）。
_HOST_GUARD_HINT = (
    "宿主 guard（.orchd/shared/guards.json）以 hard 档启用且命中；"
    "按 findings 整改后重试 done，或由宿主将该 guard 调为 warn/off"
)


def _record_host_guards(
    project_root: Path | None,
    task_id: str,
    changed: list[str],
    verify_cmd: str,
    degraded_guards: list[dict[str, Any]] | None,
) -> None:
    """done 期宿主 guard 通用接线（off / warn / hard）。

    单一分派点：读 ``.orchd/shared/guards.json``，对每个非 ``off`` 的 guard 调用
    注册表中的评估器（:mod:`orchd.guards`），按档位走既有通道——

    - ``off``：不调用评估器（真不跑），不改任何结果；
    - ``warn``：命中只写 ``degraded_guards``（E030 / ``not_applicable``），不改 done；
    - ``hard``：命中复用本函数的 E039 通道，``details.rule`` 为**具体 guard 名**。

    降级纪律（同 drift / generated 家族）：评估器异常、评估器缺失、配置非法都留
    E030 痕（``failed`` / ``not_applicable``），**不抛裸异常、不阻断 done**；无配置 /
    空配置时立即返回、零痕迹（无配置宿主仓 done 响应与接线前逐条一致）。

    Args:
        project_root: 仓库根；``None`` 时零判定（测试 / 无仓上下文）。
        task_id: 任务 id（进 degraded / details 的上下文）。
        changed: 本任务改动集（分支 diff ∪ 工作树改动 ∪ 声明回退）。
        verify_cmd: 任务 verify 命令（进评估上下文，供 guard 判据自用）。
        degraded_guards: 门禁降级登记表（done 响应统一挂载）。
    """
    if project_root is None:
        return
    try:
        from orchd.guards import (
            MODE_HARD,
            MODE_OFF,
            GuardEvalContext,
            GuardsConfigError,
            evaluate_guard,
            load_guards_config,
        )
    except Exception as exc:  # noqa: BLE001 - guard 层不可用只降级留痕
        record_degraded_guard(
            degraded_guards,
            guard_name="host_guards",
            status=GUARD_STATUS_FAILED,
            reason="宿主 guard 层不可用，本次未执行任何宿主 guard",
            error=f"{type(exc).__name__}: {exc}",
            context={"task_id": task_id},
            hint="确认 orchd.guards 可导入后重试 done",
        )
        return
    try:
        config = load_guards_config(Path(project_root))
    except GuardsConfigError as exc:
        record_degraded_guard(
            degraded_guards,
            guard_name="host_guards",
            status=GUARD_STATUS_FAILED,
            reason="宿主 guard 配置非法，本次未执行任何宿主 guard",
            error=f"{type(exc).__name__}: {exc}",
            context={"task_id": task_id, "errors": list(exc.errors[:8])},
            hint="修正 .orchd/shared/guards.json（schema 与档位语义见 docs/runtime-spec.md）",
        )
        return
    except Exception as exc:  # noqa: BLE001 - 读取故障降级留痕，不阻断
        record_degraded_guard(
            degraded_guards,
            guard_name="host_guards",
            status=GUARD_STATUS_FAILED,
            reason="宿主 guard 配置读取失败，本次未执行任何宿主 guard",
            error=f"{type(exc).__name__}: {exc}",
            context={"task_id": task_id},
            hint="确认 .orchd/shared/guards.json 可读后重试 done",
        )
        return
    if not config.configured:
        # 无配置 / 空配置 ⇒ 零行为变化：不评估、不留痕（AC3）。
        return
    enabled = [entry for entry in config.entries if entry.mode != MODE_OFF]
    if not enabled:
        return
    context = GuardEvalContext(
        project_root=Path(project_root),
        task_id=task_id,
        verify_cmd=verify_cmd,
        changed=tuple(str(path) for path in changed),
    )
    hard_details: list[dict[str, Any]] = []
    for entry in enabled:
        try:
            findings = evaluate_guard(entry.name, context)
            # 取结果同样纳入降级边界：评估器返回非 GuardFinding（如 dict）时
            # as_dict 会抛 AttributeError，**不得**裸冒泡阻断 done（AC4）。
            payload = [finding.as_dict() for finding in findings] if findings else []
        except Exception as exc:  # noqa: BLE001 - 单 guard 异常降级留痕，不阻断 done
            record_degraded_guard(
                degraded_guards,
                guard_name=entry.name,
                status=GUARD_STATUS_FAILED,
                reason=f"宿主 guard {entry.name} 评估异常，本次未做判定",
                error=f"{type(exc).__name__}: {exc}",
                context={"task_id": task_id, "mode": entry.mode},
                hint="评估器异常属实现缺陷；先按 warn/off 运行并上报，勿静默视为通过",
            )
            continue
        if findings is None:
            record_degraded_guard(
                degraded_guards,
                guard_name=entry.name,
                status=GUARD_STATUS_NOT_APPLICABLE,
                reason=f"宿主 guard {entry.name} 尚无评估器，配置档位 {entry.mode} 未生效",
                context={"task_id": task_id, "mode": entry.mode},
                hint="该 guard 判据由后续 host-guard-* 卡接入；当前配置不改变 done 行为",
            )
            continue
        if not payload:
            continue
        if entry.mode == MODE_HARD:
            hard_details.append({
                "task_id": task_id,
                "rule": entry.name,
                "mode": entry.mode,
                "findings": payload,
                "hint": _HOST_GUARD_HINT,
            })
            continue
        record_degraded_guard(
            degraded_guards,
            guard_name=entry.name,
            status=GUARD_STATUS_NOT_APPLICABLE,
            reason=f"宿主 guard {entry.name} warn 档命中 {len(payload)} 项（不阻断 done）",
            context={"task_id": task_id, "mode": entry.mode, "findings": payload},
            hint="按 findings 整改；如需阻断，将该 guard 调为 hard 档",
        )
    if hard_details:
        raise OrchdError(
            ErrorCode.E039,
            "host_guard_blocked: 宿主 guard hard 档命中",
            hard_details,
        )
