"""共享入口改动的 verify 覆盖门禁（task-shared-entry-verify-gate）。

背景（2026-09-19 一夜三次同源事故）：任务改了「共享入口」——被**多个既有测试文件**
断言的公共文件 / 脚本（``orchd/cli/__init__.py``、``.githooks/pre-push``、
``tests/conftest.py``）——却只把自己新写的测试文件放进 verify_command，既有断言静默
变红、双审仍 APPROVED 入库，main 红只能靠事后全量发现（实测三次：prepush-tag-gate、
cli-json-envelope、canonical-root-dedup）。

本模块把「共享入口 → 必须出现在 verify_command 的既有测试文件」显式登记为机器可读
映射，供 done 前置门禁（E039）强制；判定为纯函数，便于测试正负控制。

维护约定：

- 新增 / 删除条目 = **引擎代码改动**，必须走任务管线（含审查），不得手改运行时文件；
- 登记值取「断言该入口行为的既有测试文件」，不是「碰巧 import 它的无关测试」——
  登记过宽会让每个触及者被迫跑大量用例，过窄则拦不住事故；
- 影响面**无法枚举**的全局共享文件（如 ``tests/conftest.py``：改它影响全量用例，没有
  有限的「断言它的测试文件」集合）不适用登记表形态，改用 :data:`GLOBAL_SHARED_FILES`
  的**形态 A**（verify 至少含一个基线既有测试文件），见下方。

**形态 A：全局共享文件（task-global-shared-file-verify-rule）**。形态选择依据是硬约束：
全量 pytest 禁入 verify_command（rules/verify.md，120s 上限），故无法要求「改 conftest 就
跑全量」；形态 A 退一步要求 pytest 目标中**至少有一个「基线既有」测试文件**（本任务动手前
就存在于基线分支，判据见 ``missing_baseline_test_coverage`` 的 ``is_baseline_test``
谓词）——让既有断言至少被真实执行一次，同时把「本任务新建的测试」排除在外（新建文件必然
不在基线）。兼容「修改既有测试文件」：只要该文件在基线上存在即计为基线既有。
基线分支**不硬编码**：由 ``orchd.onboard.lifecycle.core._resolve_baseline_ref`` 按
``origin/HEAD`` → ``main`` → ``master`` 次序解析（task-e039-escape-hatch-bypass-fix，pass6
Q-2：硬编码 ``main`` 在默认分支非 main 的宿主上会让形态 A 静默失效），全不可解析时记
E030 降级痕迹而非静默跳过。

**保证面（显式契约，task-shared-entry-gate-coverage-hardening）**：门禁覆盖
「**(已登记入口 ∪ 全局共享文件) × (分支已提交 diff ∪ 工作树未提交改动)**」——未提交改动
必须计入，因为引擎直到 ``done`` 靠后阶段（``_commit_and_verify_integrity`` →
``ensure_committed``）才 auto-commit，只看已提交 diff 会让「未提交的共享入口改动」整段
逃出视野（实测：同一仓库 ``task_branch_files=['other.py']`` 而
``list_tracked_changes=['orchd/cli/__init__.py']``）。

**「覆盖」的判定口径**（task-e039-gate-self-injury-fix，pass5 N-1/N-2；
task-e039-escape-hatch-bypass-fix，pass6 Q-1）：

1. **超集视为覆盖——但仅限「执行语义未被削减」**：pytest 段为目录级全收集（``tests/`` /
   ``tests``，且处于**目标位**）**且 pytest 自身参数全部落在良性白名单**内时，视为全覆盖
   （:func:`verify_command_covers_all_tests`）——否则「恰需宽域 verify」的任务会被自己的
   门禁卡死；
2. **白名单方向（未知即从严）**：判定不是「命中若干坏选项就否」，而是「只有确认为良性才
   认」。黑名单对抗 pytest 的开放选项集**构造性不完备**——pass6 Q-1 实测 8 个旁路
   （``--collect-only`` / ``--co`` / ``-m "not slow"`` / ``--lf`` / ``-x`` / ``--setup-plan``
   / 多段混合），其中 ``--collect-only`` 一条测试都没跑却被判「已覆盖」并**短路放行**（一次
   同时放掉登记表规则与形态 A）。白名单的失败模式是「多要一次显式声明」（安全），黑名单的
   失败模式是「静默放行」（事故类）；
3. **排除项不算覆盖**：``--ignore`` / ``--deselect`` / ``--ignore-glob`` 的**取值**不进入
   目标集合（被排除的登记测试判未覆盖）；
4. **目标集合**只取 pytest 段内、且非排除项取值的 ``tests/*.py`` 路径（非 pytest 段的
   静态检查目标不计入）。

已知**不**覆盖面（写在此处避免把门禁名当全覆盖）：

1. 形态 A 只约束「至少一个基线既有测试」，**不保证**该测试真的覆盖被改动行为（弱约束的
   固有代价；评估见 task-global-shared-file-verify-rule 的形态取舍）；
2. 判定取不到改动清单时按 fail-open 跳过（退化为用声明 ``files_to_edit`` 判定）——该故障
   场景由越界 / 声明门禁 fail-closed 兜底，既有锁定用例 =
   ``tests/test_done.py::TestDoneGuardFailClosed``（``_git_diff_names`` 抛异常 ⇒ E030
   阻断 done）；形态 A 的基线 ref 全不可解析时同样不阻断，但**不静默**：记
   ``degraded_guards``（E030 / not_applicable），可被 doctor 按码检索。
"""

from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from orchd.errors import ErrorCode, OrchdError

# 共享入口（仓库相对 POSIX 路径）→ 必须出现在 verify_command 的既有测试文件。
SHARED_ENTRY_TESTS: dict[str, tuple[str, ...]] = {
    # 2026-09-19 task-cli-json-envelope：改 main() 的 argparse 错误出口后只跑了
    # tests/test_cli_contract.py，漏了断言同一 CLI 行为的 test_cli_workflow.py。
    "orchd/cli/__init__.py": (
        "tests/test_cli_workflow.py",
        "tests/test_cli_contract.py",
    ),
    # 2026-09-19 task-prepush-tag-gate：改门禁条件后只跑了新文件
    # tests/test_prepush_gate.py，漏了同样断言该 hook 的 test_gate_ci_wiring.py。
    ".githooks/pre-push": (
        "tests/test_gate_ci_wiring.py",
        "tests/test_prepush_gate.py",
    ),
    # 2026-09-19 task-shared-entry-gate-coverage-hardening（自举）：规则文件自身即被
    # 多个既有测试断言的共享入口——两个文件名带「drift」的护栏读它并断言已登记内容，
    # 改规则文件而不跑它们 = 护栏静默失效。
    ".orchd/rules/verify.md": (
        "tests/test_docs_drift_batch.py",
        "tests/test_gate_ci_wiring.py",
    ),
    # 2026-09-19 task-lint-scope-and-registry-hygiene：review.md 内容同样被 drift 断言
    # 锁定（自审口径 / 门禁必查清单等章节），改它而不跑护栏 = 规则静默漂移。
    ".orchd/rules/review.md": (
        "tests/test_docs_drift_batch.py",
        "tests/test_gate_ci_wiring.py",
    ),
    # 2026-09-24 task-shared-entry-init-mapping：改 validate 退出码后只跑了新文件，
    # 漏了同样断言 init/validate 行为的 test_cli_init.py（2 处旧断言在主分支上
    # 变红入库）。init/validate 行为由三文件直接断言，改 commands/init.py 须全跑。
    "orchd/cli/commands/init.py": (
        "tests/test_cli_init.py",
        "tests/test_cli_query.py",
        "tests/test_nogit_init_layout.py",
    ),
    # 2026-09-27 task-pass9-shared-entry-registry（pass9 实测）：hook 生成物形态
    # （case 模式 / scope-check 行）被 test_decl_dir_match_guards.py 的断言钉住，
    # 改生成层而不跑它 = 断言漂移只能靠全量回归兜底（实测漏 3 条红）。生成行为
    # 由三文件直接断言，改 hook.py 须全跑。
    # W6-C8：`orchd/gitops/hook.py` 拆为子包 ⇒ 原单文件键不再命中任何改动（E039 对该域
    # 静默失明），改为逐子域登记（精确匹配语义不变，保守地都要求三文件全跑）。
    # 审查返修：拆包后**包入口**是最易碎的面（重导出面 / `__all__` 都在这），漏掉它等于
    # 该入口改动不触发 E039；同样补上 `cli/_util/` 四域（拆前亦无键，一并收口）。
    "orchd/gitops/hook/__init__.py": (
        "tests/test_gitops.py",
        "tests/test_hook_template_external.py",
    ),
    "orchd/gitops/hook/paths.py": (
        "tests/test_gitops.py",
        "tests/test_decl_dir_match_guards.py",
        "tests/test_hook_path_injection.py",
    ),
    "orchd/gitops/hook/content.py": (
        "tests/test_gitops.py",
        "tests/test_decl_dir_match_guards.py",
        "tests/test_hook_path_injection.py",
        "tests/test_hook_template_external.py",
    ),
    "orchd/gitops/hook/install.py": (
        "tests/test_gitops.py",
        "tests/test_decl_dir_match_guards.py",
        "tests/test_hook_path_injection.py",
    ),
    "orchd/gitops/hook/templates.py": (
        "tests/test_hook_template_external.py",
        "tests/test_gitops.py",
    ),
    "orchd/gitops/hook/precommit_template.py": (
        "tests/test_hook_template_external.py",
        "tests/test_gitops.py",
    ),
    "orchd/cli/_util/__init__.py": (
        "tests/test_cli_workflow.py",
        "tests/test_cli_ideas.py",
    ),
    "orchd/cli/_util/paths.py": (
        "tests/test_cli_workflow.py",
        "tests/test_cli_ideas.py",
    ),
    "orchd/cli/_util/archive.py": (
        "tests/test_cli_workflow.py",
        "tests/test_cli_ideas.py",
    ),
    "orchd/cli/_util/fallback.py": (
        "tests/test_cli_workflow.py",
    ),
    # 2026-09-27 task-pass9-shared-entry-registry（pass9 实测）：session 锁回收/
    # 探活链路被 test_concurrency.py 的 W-17 断言钉住；锁协议属高风险区（verify.md
    # 第三环），改 session_lock.py 须全跑四个同域测试文件。
    # W8-5：`orchd/gitops/session_lock.py` 拆为子包 ⇒ 单文件键替换为逐子模块键（照 hook 先例）。
    "orchd/gitops/session_lock/__init__.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/constants.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/degrade.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/state.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/gate.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/paths.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/payload.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/write.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/probe.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/reclaim.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/acquire.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/check.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/release.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    "orchd/gitops/session_lock/ensure.py": (
        "tests/test_concurrency.py",
        "tests/test_session_lock_observe.py",
        "tests/test_session_lock_acquire_respect.py",
        "tests/test_session_lock_unlink_race.py",
    ),
    # task-caliber-residue-and-surface-gate：CLI 注册面与命令模块入表。此前
    # 「改子命令却忘把对拍测试放进 verify」只能靠 verify.md 的散文约束——E039 不会拦，
    # 因为这两处根本不在表里（`idea archive` 上线时手册行 help 与 parser 漂移就是这么漏的）。
    "orchd/cli/parser.py": (
        "tests/test_docs_single_source.py",
        "tests/test_cli_contract.py",
    ),
    "orchd/cli/commands/ideas.py": (
        "tests/test_cli_ideas.py",
        "tests/test_docs.py",
    ),
}

# ``orchd/cli/commands/`` 下**显式豁免**入表的模块（task-caliber-residue-and-surface-gate）：
# ``{仓库相对 POSIX 路径: 理由}``。豁免必须在此写明理由——新增命令文件若不决策（既不登记
# 也不豁免）即由 :func:`cli_command_modules_unaccounted` 判红。这些模块的对外面没有独立
# 行为测试，其 help 文案与命令名由 tests/test_docs.py 的 AST 守卫 + tests/test_cli_contract.py
# 的名字集对拍覆盖（通用门），故不逐个登记成"改它就要求跑某个专属测试"。
CLI_COMMAND_MODULE_EXEMPT: dict[str, str] = {
    "orchd/cli/commands/__init__.py": "仅注册顺序表（_REGISTER_ORDER），无独立行为面",
    "orchd/cli/commands/check.py": "只读体检命令，对外面由通用门（help AST + 名字集对拍）覆盖",
    "orchd/cli/commands/control/__init__.py": (
        "事件写入类命令（W8-3 子包化，入口纯重导出），"
        "行为面由 tests/test_cli_workflow.py 覆盖"
    ),
    "orchd/cli/commands/lessons.py": "经验回灌命令组，行为面由 tests/test_lessons*.py 覆盖",
    "orchd/cli/commands/line.py": "分线只读命令，对外面由通用门覆盖",
    "orchd/cli/commands/misc/__init__.py": "零散运维命令（W6-C7 子包化，入口纯重导出），"
                                           "对外面由通用门覆盖",
    "orchd/cli/commands/query.py": "只读查询命令，对外面由通用门覆盖",
    "orchd/cli/commands/session.py": "会话命令组，行为面由 tests/test_session*.py 覆盖",
    "orchd/cli/commands/sync.py": "账本同步命令，行为面由 tests/test_sync*.py 覆盖",
    "orchd/cli/commands/workflow.py": "任务流程命令组，行为面由 tests/test_cli_workflow.py 覆盖",
}


# ``commands_dir`` 入参契约（W7-A）：形状必须是 ``<repo>/orchd/cli/commands``。
# 旧写法 ``commands_dir.resolve().parents[2]`` 按「本目录在第几层」反推仓库根——调用方
# 一旦换了层深（工程搬迁 / 传错目录），仓库根静默偏两级，完备性判据扫错面并「可信地」
# 返回空列表（与 C8 的 ``parent.parent.parent`` 同族：看起来在守，实际守空）。
_COMMANDS_DIR_NAME = "commands"
_CLI_DIR_NAME = "cli"

# 契约判别键（W7-A 审查返修 F11/F12）：错误码取 E007（invalid_state，阻断级）——E030 是
# ``runtime_file_integrity``（warning 级 + suggest_report + doctor 话术），把「调用方传错
# 目录」渲染成「引擎文件异常、建议上报」会同时误导严重度与补救方向；E007 是仓库既有的
# 「入参 / 状态校验失败」同族码（lessons 等模块已有先例）。details 两支共用同一键集并带
# 稳定判别键 ``contract`` / ``reason``，便于机器检索（不依赖 message 文本）。
_CONTRACT_ID = "commands_dir_shape"


def _contract_details(resolved: Path, reason: str) -> dict[str, Any]:
    """形状契约失败时的结构化 details（三分支键集相同，只有 ``reason`` 取值不同）。

    ``hint`` 是给 guidance 层的**可执行指引**（F17）：``attach_error_guidance`` 的模板覆盖
    只认 ``details[].hint``，缺它就会套用 E007 的状态机模板（「当前 <current> 不允许
    <event_type>→<target>」，占位符无处代入、方向错误导向 ``orchd status``）。
    """
    return {
        "contract": _CONTRACT_ID,
        "reason": reason,
        "commands_dir": str(resolved),
        "name": resolved.name,
        "parent_name": resolved.parent.name,
        "expected_name": _COMMANDS_DIR_NAME,
        "expected_parent_name": _CLI_DIR_NAME,
        "is_dir": resolved.is_dir(),
        "hint": (
            f"把 commands_dir 传成 <repo>/orchd/cli/commands（目录名 {_COMMANDS_DIR_NAME!r}、"
            f"父目录 {_CLI_DIR_NAME!r}）；不要按「本目录在第几层」反推仓库根"
        ),
    }


def _resolve_repo_root(commands_dir: Path) -> Path:
    """从 ``commands_dir`` 反推仓库根；形状不符即 E007 结构化报错（不静默退层级）。

    契约（三条都满足才返回仓库根）：解析后是**目录**、目录名 == ``commands``、其父目录名
    == ``cli``。拒绝「按层数硬退」：那会把形状错误变成静默的空扫描结果，而不是可检索的
    故障。

    覆盖边界（宣称与能力对齐，审查返修 F13）：本条只判「名字 + 是否目录」；**仓库根是否
    真是仓库**（其下有 ``orchd/__init__.py``）不在此判——由调用方与守卫的落点判据另判
    （``tests/test_w7_depth_paths.py`` 的等值判据）。
    """
    resolved = commands_dir.resolve()
    if not resolved.is_dir():
        raise OrchdError(
            ErrorCode.E007,
            "shared_entry_path_contract: commands_dir 必须是已存在的目录"
            f"（实际 {resolved}，is_dir=False），拒绝按层级反推仓库根",
            [_contract_details(resolved, "not_a_dir")],
        )
    if resolved.name != _COMMANDS_DIR_NAME:
        raise OrchdError(
            ErrorCode.E007,
            "shared_entry_path_contract: commands_dir 目录名必须是 "
            f"{_COMMANDS_DIR_NAME!r}（实际 {resolved.name!r}），拒绝按层级反推仓库根",
            [_contract_details(resolved, "dir_name")],
        )
    if resolved.parent.name != _CLI_DIR_NAME:
        raise OrchdError(
            ErrorCode.E007,
            "shared_entry_path_contract: commands_dir 的父目录必须是 "
            f"{_CLI_DIR_NAME!r}（实际 {resolved.parent.name!r}），拒绝按层级反推仓库根",
            [_contract_details(resolved, "parent_dir_name")],
        )
    return resolved.parent.parent.parent


def cli_command_modules_unaccounted(commands_dir: Path) -> list[str]:
    """``commands_dir`` 下既未登记进 ``SHARED_ENTRY_TESTS`` 也不在豁免集内的模块。

    完备性判据（task-caliber-residue-and-surface-gate AC4）：新命令文件必须**做出决定**
    ——登记入表（则改它就必须把登记测试放进 verify_command，否则 E039 拦）或写进
    :data:`CLI_COMMAND_MODULE_EXEMPT` 并给出理由。两者都不做即非空列表 ⇒ 判红。
    返回值用仓库相对 POSIX 路径（与登记表键同口径）。
    """
    unaccounted: list[str] = []
    repo_root = _resolve_repo_root(commands_dir)
    decided = set(SHARED_ENTRY_TESTS) | set(CLI_COMMAND_MODULE_EXEMPT)
    for path in sorted(commands_dir.iterdir()):
        if path.is_dir():
            # W6-C7：命令模块子包化后，`glob("*.py")` 永远扫不到目录 ⇒ 整棵子树
            # （含承载实现的各域）掉出「必须登记或豁免」的完备性判据（覆盖 1→0 的
            # fail-open）。包目录以**包入口**为决策单元：入口在表内即视为已决策。
            if not (path / "__init__.py").is_file():
                continue
            rel = (path / "__init__.py").resolve().relative_to(repo_root).as_posix()
            if rel in decided:
                continue
            unaccounted.append(rel)
            continue
        if path.suffix != ".py":
            continue
        rel = path.resolve().relative_to(repo_root).as_posix()
        if rel in decided:
            continue
        unaccounted.append(rel)
    return unaccounted

# 全局共享文件（形态 A）：影响面无法枚举的共享入口——改它影响**全量**用例，因此不存在
# 「断言它的有限测试文件集合」可登记。对这些文件，门禁退一步要求 verify_command 的 pytest
# 目标中至少有一个「基线既有」测试文件（见 missing_baseline_test_coverage）。
# 维护约定同登记表：增删 = 引擎代码改动（须走任务管线）；入库门槛 = 「改它会改变其它测试
# 文件的行为」且「其影响面无法枚举」，二者缺一不可（否则应进 SHARED_ENTRY_TESTS）。
GLOBAL_SHARED_FILES: frozenset[str] = frozenset({
    # 全仓测试共享 fixture/helper（session 级模板、store、git_repo 等）——改它同时影响
    # 所有测试文件的行为，2026-09-19 前有 8 个任务声明改过它且无任何覆盖。
    "tests/conftest.py",
})

# 基线判定谓词：True=基线既有 / False=非基线（本任务新增）/ None=不可判定（git 不可用等，
# 调用方按 fail-open 跳过）。
BaselineTestProbe = Callable[[str], "bool | None"]

# verify_command 里的测试目标（``tests/**/*.py``）。前后边界防误吞形如 ``x/tests/a.py``
# 的其它路径。
_TEST_PATH_RE = re.compile(r"(?<![\w/.-])(tests/[\w./-]+\.py)")

# ``--basetemp`` 的取值形态：``--basetemp=<值>`` / ``--basetemp <值>``，值可能是
# ``"${TMPDIR:-/tmp}/orchd-vf-$$"``（带引号）或裸 token。必须在扫描前剥离——否则
# 模板里的路径会被当测试目标，门禁形同虚设 / 误报。
_BASETEMP_RE = re.compile(r"--basetemp(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|\S+)")

# pytest 程序 token（取 basename 比对，兼容 venv 绝对路径与 ``py.test`` 别名）。
_PYTEST_PROGRAM_TOKENS = frozenset({"pytest", "pytest.exe", "py.test"})

# 良性选项白名单：**不改变「跑哪些测试 / 跑多少」**的选项（报告、并发、临时目录、插件禁用）。
# 判定方向是「只有确认为良性才认全量」——未知 / 削减类一律从严回落逐字面匹配
# （task-e039-escape-hatch-bypass-fix，pass6 Q-1：此前的 4 条黑名单对 pytest 开放选项集
# 构造性不完备，``--collect-only`` 零执行亦被判「已覆盖」）。
_BENIGN_OPTION_TOKENS = frozenset({
    "-q", "-v", "-vv", "-s", "-n",
    "--basetemp", "--tb", "--max-worker-restart", "--durations",
    "--color", "--no-header", "--import-mode",
    "-p",   # 仅放行 ``-p no:...``；加载插件可改变集合，见 _has_only_benign_options
})

# 目录级目标的合法 token 形态（须处于**目标位**，判定见 _is_directory_target_in_segment）。
_DIR_TARGET_TOKENS = frozenset({"tests", "tests/"})


def _is_directory_target_in_segment(segment: str) -> bool:
    """pytest 段内是否存在**处于目标位**的目录级目标（``tests/`` / ``tests``）。

    只认 token 形态不够——``--rootdir tests/`` / ``--cov tests/`` / ``--cov=tests/`` 里的
    ``tests/`` 是**选项取值**而非「跑哪些测试」的目标：按形态识别会把「只跑单个文件」误判为
    「跑全量」，逃逸口反成旁路（rework R-1，2026-09-19 审查打回项）。故按位置判定：

    1. token 自身不含 ``=`` ⇒ 排除 ``--opt=value`` 形态；
    2. 前一 token 不以 ``-`` 开头 ⇒ 排除「选项 取值」形态；
    3. 去掉包裹引号后必须精确等于 ``tests`` / ``tests/``（``./tests/`` 之类保守不认）。
    """
    tokens = segment.split()
    for index, token in enumerate(tokens):
        if token.strip("\"'") not in _DIR_TARGET_TOKENS:
            continue
        if "=" in token:
            continue
        if index and tokens[index - 1].startswith("-"):
            continue
        return True
    return False


def _pytest_own_args(segment: str) -> list[str] | None:
    """取 pytest 段里 **pytest 自身**的参数；找不到 pytest 程序 token ⇒ ``None``。

    ``python -m pytest`` 的 ``-m`` 属 **python**（模块开关），不是 pytest 的 marker 选择器。
    若整段扫选项，现网 419/441 条该形态的 verify_command 会被白名单误判为「非全量」
    （task-e039-escape-hatch-bypass-fix 实施期实测）。故先定位 pytest 程序 token（basename
    比对，兼容 venv 绝对路径与 ``py.test``），只取其后的参数。
    """
    tokens = segment.split()
    for index, token in enumerate(tokens):
        program = token.strip("\"'").replace("\\", "/").rsplit("/", 1)[-1]
        if program in _PYTEST_PROGRAM_TOKENS:
            return tokens[index + 1:]
    return None


def _has_only_benign_options(args: Sequence[str]) -> bool:
    """pytest 自身参数是否**全部**为良性白名单项（``--`` 之后是测试参数，停止扫描）。

    ``-p`` 只看取值：``-p no:<plugin>`` 放行（禁用插件不改变集合），``-p <plugin>`` 拒绝
    （加载插件可能改变集合，如注入 ``addopts``）。
    """
    for index, token in enumerate(args):
        if token == "--":
            break
        name, has_eq, value = token.partition("=")
        if not token.startswith("-") or token == "-":
            continue
        if name not in _BENIGN_OPTION_TOKENS:
            return False
        if name == "-p":
            inline = value if has_eq else (args[index + 1] if index + 1 < len(args) else "")
            if not inline.strip("\"'").startswith("no:"):
                return False
    return True

# 收窄选项的**取值**（可能是路径）：扫描目标前剥离，否则「被 --ignore 排除掉的登记测试」
# 会因字面出现而被当成「已覆盖的目标」（pass5 N-2 旁路）。
_NARROWING_VALUE_RE = re.compile(
    r"(--ignore|--deselect|--ignore-glob)(?:=|\s+)(?:\"[^\"]*\"|'[^']*'|\S+)"
)


def verify_command_test_targets(verify_command: str) -> tuple[str, ...]:
    """提取 verify_command 中 **pytest 段**的目标测试文件（去重、保序）。

    只扫描含 ``pytest`` 的命令段（按 ``&&`` / ``;`` 切分）。**非 pytest 段里的
    ``tests/*.py`` 路径不算测试目标**——否则 ``python -m ruff check tests/conftest.py``
    这类「只静态检查、一次测试都没跑」的命令也会满足覆盖判定（假通过；task-global-
    shared-file-verify-rule 实施期实测到该路径，登记表规则同样受影响）。

    Args:
        verify_command: 任务定义里的验证命令（单命令串，可含 ``&&`` 链）。

    Returns:
        形如 ``("tests/a.py", "tests/b.py")`` 的目标文件元组；``--basetemp`` 取值
        已剥离，不会出现在结果中。
    """
    seen: dict[str, None] = {}
    for segment in re.split(r"&&|;", verify_command or ""):
        if "pytest" not in segment:
            continue
        # 顺序要紧：先剥离收窄选项取值（排除项不是目标），再剥离 basetemp 取值。
        scrubbed = _NARROWING_VALUE_RE.sub(" ", segment)
        scrubbed = _BASETEMP_RE.sub(" ", scrubbed)
        for match in _TEST_PATH_RE.finditer(scrubbed):
            seen.setdefault(match.group(1), None)
    return tuple(seen)


def verify_command_covers_all_tests(verify_command: str) -> bool:
    """verify 是否「跑全量测试」——目录级全收集且**执行语义未被削减**（超集逃逸口）。

    用途：登记表规则与形态 A 的**超集逃逸口**（task-e039-gate-self-injury-fix，pass5 N-1）。
    全量 / 超集覆盖必然包含登记测试与基线既有测试，若仍判缺口，会让「恰需宽域 verify」的
    任务被卡死——而 verify 红线一边禁全量入 verify_command、一边不认全量为合法覆盖，属自相
    矛盾。

    判定 = 下列三条件同时成立（任一不成立即回落逐字面匹配，从严）：

    1. pytest 段内存在**处于目标位**的目录级目标（``tests/`` / ``tests``；``--rootdir tests/``
       之类选项取值位不算，rework R-1）；
    2. 能定位 pytest 程序 token，且取其**自身参数**（``python -m pytest`` 的 ``-m`` 属 python，
       不计——现网 419/441 条命令是该形态）；
    3. 这些参数**全部**落在良性白名单内（:data:`_BENIGN_OPTION_TOKENS`）。

    方向说明（task-e039-escape-hatch-bypass-fix，pass6 Q-1）：此处原为 4 条黑名单（``-k`` /
    ``--ignore`` / ``--deselect`` / ``--ignore-glob``），对 pytest 的开放选项集构造性不完备
    ——实测 ``--collect-only``（只收集、**零条执行**）、``--co``、``-m "not slow"``、``--lf``、
    ``-x``、``--setup-plan`` 与多段混合均被判「全量」并**短路放行**（一次同时放掉登记表规则与
    形态 A）。故改为「确认为良性才算」：未知选项只会让门禁多要一次显式声明，不会静默放行。

    只识别显式目录目标（``tests/`` / ``tests``）；``pytest .`` 这类仓库根目标不在识别范围
    （保守方向：宁可多判缺口，不误判为已覆盖）。
    """
    for segment in re.split(r"&&|;", verify_command or ""):
        if "pytest" not in segment:
            continue
        scrubbed = _BASETEMP_RE.sub(" ", segment)
        if not _is_directory_target_in_segment(scrubbed):
            continue
        args = _pytest_own_args(scrubbed)
        if args is None or not _has_only_benign_options(args):
            continue
        return True
    return False


def missing_shared_entry_tests(
    changed_files: Iterable[str],
    verify_command: str,
    registry: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, str]]:
    """返回「改了共享入口但 verify_command 未覆盖其登记测试」的缺口清单。

    Args:
        changed_files: 本次任务分支实际改动的文件（仓库相对路径，``/`` 或 ``\\`` 皆可）。
        verify_command: 任务定义里的验证命令。
        registry: 登记表覆盖项（默认 :data:`SHARED_ENTRY_TESTS`）；测试可注入以做负控制。

    Returns:
        缺口列表 ``[{"shared_entry": ..., "required_test": ...}, ...]``；空列表 = 通过。
    """
    entries = SHARED_ENTRY_TESTS if registry is None else registry
    changed = {str(path).replace("\\", "/") for path in changed_files}
    if verify_command_covers_all_tests(verify_command):
        # 超集逃逸口：跑全量 ⇒ 每个登记测试都被执行（无论是否逐个列出）。
        return []
    targets = set(verify_command_test_targets(verify_command))
    gaps: list[dict[str, str]] = []
    for entry in sorted(entries):
        if entry not in changed:
            continue
        for test in entries[entry]:
            if test not in targets:
                gaps.append({"shared_entry": entry, "required_test": test})
    return gaps


def missing_baseline_test_coverage(
    changed_files: Iterable[str],
    verify_command: str,
    is_baseline_test: BaselineTestProbe,
    registry: Iterable[str] | None = None,
) -> list[dict[str, Any]] | None:
    """形态 A 判定：改了全局共享文件时，verify 是否至少含一个**基线既有**测试。

    Args:
        changed_files: 本次任务实际改动的文件（仓库相对路径，``/`` 或 ``\\`` 皆可）。
        verify_command: 任务定义里的验证命令。
        is_baseline_test: 基线判定谓词（``main`` 上是否存在该文件）；返回 ``None`` 表示
            不可判定（git 不可用 / ref 不可解析）。
        registry: 全局共享文件集合覆盖项（默认 :data:`GLOBAL_SHARED_FILES`）。

    Returns:
        - ``[]``：通过（未触及全局共享文件，或 verify 目标中已有基线既有测试）；
        - ``[{...}]``：缺口（逐文件一条，``required_test`` 为 ``None``——形态 A 不指定
          具体文件，只要求「至少一个基线既有」）；
        - ``None``：**不可判定**（任一目标文件的基线属性未知）——调用方应按 fail-open
          跳过，口径见模块 docstring。
    """
    files = GLOBAL_SHARED_FILES if registry is None else registry
    changed = {str(path).replace("\\", "/") for path in changed_files}
    touched = sorted(set(files) & changed)
    if not touched:
        return []
    if verify_command_covers_all_tests(verify_command):
        # 超集逃逸口：跑全量 ⇒ 必然含基线既有测试（谓词无需介入，也避免无谓 git 调用）。
        return []

    targets = verify_command_test_targets(verify_command)
    verdicts = {target: is_baseline_test(target) for target in targets}
    # 判定可用性先于结论：任一目标「未知」，就无法排除它是基线既有文件 ⇒ 不阻断（fail-open）。
    if any(verdict is None for verdict in verdicts.values()):
        return None
    if any(verdict is True for verdict in verdicts.values()):
        return []
    # 走到这里：要么根本没有 pytest 目标，要么目标全是本任务新建文件 ⇒ 既有断言一次没跑。
    return [{
        "shared_entry": path,
        "required_test": None,
        "reason": "pytest 目标中无任一基线既有（本任务动手前已存在于基线分支）测试文件",
        "verify_targets": list(targets),
    } for path in touched]
