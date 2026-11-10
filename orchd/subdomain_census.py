"""orchd/subdomain_census.py — 子域完备性判据（W7-B，task-split2-w7-b-subdomain-census）。

背景：``shared_entries.py::cli_command_modules_unaccounted`` 只覆盖 ``orchd/cli/commands/``，
新增的 ``cli/_util/`` 等子包没有等价判据——在已声明子包里新开域文件而不登记、不写豁免理由时
**没有任何判据会喊红**。本模块把该判据推广到全部已声明子包，并补上「顶层单文件模块」
「任意深度的新包」两条同类缺口。

**决策单元（两级，边界必须写清）**：

- **作用域包**（:data:`DECLARED_SUBPACKAGES`）：**模块级**决策——包内每个 ``.py`` 必须登记进
  ``SHARED_ENTRY_TESTS`` 或写进该包决策枚举（带理由），否则判红；
- **其它包**（:data:`OTHER_PACKAGE_REASONS`，任意深度）：**包级**决策——包内新增单文件模块视为
  已被该包理由覆盖（**这是显式边界，不是遗漏**）；但**新建一个包**必须补理由，否则判红；
- **顶层单文件模块**（:data:`TOP_LEVEL_MODULE_REASONS`）：``orchd/*.py`` 每个都要有理由
  （「第 6 个域」最常见的形态就是根目录新增一个文件）。

**五条判据（全部纯只读，零 git / 零写盘；返回值一律是仓库相对 POSIX 路径）**：

1. :func:`subdomain_unaccounted`：作用域包内未决策的模块；
2. :func:`undeclared_packages`：未决策的包（任意深度）；
3. :func:`undeclared_namespace_dirs`：含 ``.py`` 却无 ``__init__.py`` 的目录（须决策）；
4. :func:`undeclared_top_level_modules`：未决策的 ``orchd/*.py``；
5. :func:`stale_registry_entries`：登记面指向磁盘上不存在的对象（「看起来在守、实际守空」）。

**各表里的「理由」描述的是测试覆盖归属，不是门禁登记面**：只有 ``SHARED_ENTRY_TESTS`` 的条目才会
触发 E039；本模块三张表**不进** E039，其判据只在 pytest 档（含全量回归）生效。

**已知边界**：① 判据不缓存，五条扫描各自遍历（测试期可接受，不适合 done 热路径）；
② ``_repo_rel`` 对落在扫描面之外的路径（目录联接指向仓外）抛 ``ValueError``，
``resolve()`` / ``iterdir()`` 的 ``OSError`` 原样外泄——属「环境异常报错」而非「静默放过」。

维护约定：新增模块 / 包 = **做出决定**（登记入 ``SHARED_ENTRY_TESTS``，或补进对应表并写非空
理由）。不得靠「留空」通过。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orchd.shared_entries import CLI_COMMAND_MODULE_EXEMPT, SHARED_ENTRY_TESTS

_ORCHD_PREFIX = "orchd/"


@dataclass(frozen=True)
class CensusScope:
    """一次扫描的判定面（引擎默认 = 本模块常量表；宿主 guard 传入自己的表）。"""

    base: Path
    repo_root: Path
    prefix: str
    declared: Mapping[str, "SubdomainDecision"]
    other: Mapping[str, str]
    top_level: Mapping[str, str]
    shared: Mapping[str, tuple[str, ...]]


def _scope(value: "CensusScope | Path | str") -> "CensusScope":
    """``orchd/`` 目录（旧签名）→ 引擎默认 scope；已是 scope 则原样返回。"""
    if isinstance(value, CensusScope):
        return value
    base = Path(value)
    return CensusScope(base, base.resolve().parent, _ORCHD_PREFIX, DECLARED_SUBPACKAGES,
                       OTHER_PACKAGE_REASONS, TOP_LEVEL_MODULE_REASONS, SHARED_ENTRY_TESTS)


@dataclass(frozen=True)
class SubdomainDecision:
    """一个已声明子包的「决策」：该包内哪些模块已做出决定，以及为什么。

    Attributes:
        modules: 决策时刻该包（递归）内可放行的模块（仓库相对 POSIX 路径）。为空元组表示
            「该包全部模块已登记进 ``SHARED_ENTRY_TESTS``」（登记面另行强制）。
        reason: 非空理由——为什么这些模块不必逐个登记专属测试。
    """

    modules: tuple[str, ...]
    reason: str


# 作用域包清单（**模块级**决策单元，对应 W7-B 的 AC 清单）。
# `gitops/hook/` 与 `cli/_util/` 的每个模块都已登记 SHARED_ENTRY_TESTS（W6-C8 返修/active_gate），故
# modules 留空；`cli/commands/` 的枚举真源复用既有 CLI_COMMAND_MODULE_EXEMPT，不另立第二份。
DECLARED_SUBPACKAGES: dict[str, SubdomainDecision] = {
    "orchd/cli/commands": SubdomainDecision(
        modules=tuple(CLI_COMMAND_MODULE_EXEMPT)
        + (
            # W6-C7：`misc/` 子包以**入口**为决策单元（CLI_COMMAND_MODULE_EXEMPT 有该入口），
            # 四个实现域随入口一并放行；此处逐条列出，避免「入口已决策 ⇒ 子树整体免检」的
            # 隐含放行（新增第五个域仍须补进本元组）。
            "orchd/cli/commands/misc/commands.py",
            "orchd/cli/commands/misc/evidence.py",
            "orchd/cli/commands/misc/registration.py",
            "orchd/cli/commands/misc/storage.py",
            # W8-3：`control/` 子包以**入口**为决策单元，十三个实现域随入口一并放行。
            "orchd/cli/commands/control/constants.py",
            "orchd/cli/commands/control/declarations.py",
            "orchd/cli/commands/control/patch_validation.py",
            "orchd/cli/commands/control/proposals.py",
            "orchd/cli/commands/control/persist.py",
            "orchd/cli/commands/control/amend_admission.py",
            "orchd/cli/commands/control/amend_patch_fields.py",
            "orchd/cli/commands/control/amend_register_gate.py",
            "orchd/cli/commands/control/amend_persist_commit.py",
            "orchd/cli/commands/control/amend_impl.py",
            "orchd/cli/commands/control/roundtrip.py",
            "orchd/cli/commands/control/commands.py",
            "orchd/cli/commands/control/register.py",
        ),
        reason="命令模块的对外面由通用门（help AST + 名字集对拍）与 CLI 行为测试覆盖",
    ),
    "orchd/cli/_util": SubdomainDecision(
        modules=(),
        reason="四域全部登记进 SHARED_ENTRY_TESTS（W6-C8 返修），改它必须跑登记测试",
    ),
    "orchd/doctor/fix": SubdomainDecision(
        modules=(
            "orchd/doctor/fix/__init__.py",
            "orchd/doctor/fix/autoclean.py",
            "orchd/doctor/fix/constants.py",
            "orchd/doctor/fix/delete.py",
            "orchd/doctor/fix/dispose.py",
            "orchd/doctor/fix/fixrun.py",
            "orchd/doctor/fix/prune.py",
            "orchd/doctor/fix/report.py",
        ),
        reason=(
            "doctor 修复域（W8-6，task-split2-w8-6-doctor-fix-pkg 自单文件 fix.py 拆出）；"
            "七域逐条枚举，行为面由 tests/test_doctor*.py 覆盖，AST 指纹 / 定义块原文 / "
            "布局预算 / 域 DAG 由 tests/test_doctor_fix_pkg.py 守卫，跨域聚合面与补丁"
            "消费面由 tests/test_doctor_pkg.py 守卫"
        ),
    ),
    "orchd/split": SubdomainDecision(
        modules=(
            "orchd/split/__init__.py",
            "orchd/split/common.py",
            "orchd/split/log.py",
            "orchd/split/validate.py",
            "orchd/split/init.py",
            "orchd/split/amend_admission.py",
            "orchd/split/amend_precheck.py",
            "orchd/split/amend_route_tasks.py",
            "orchd/split/amend_route_state.py",
            "orchd/split/amend_persist.py",
            "orchd/split/amend.py",
            "orchd/split/dry_run.py",
        ),
        reason=(
            "快照生成（init）与增量更新（amend）纯搬移拆包（W8-2，task-split2-w8-2-split-pkg）；"
            "结构契约由 tests/test_pkg_contract.py 守卫（公开面 / 布局预算 / 域间 DAG），"
            "tests/test_split.py / tests/test_split_amend_segments.py / "
            "tests/test_split_guard_fallback.py / tests/test_terminal_spec_revision.py 行为覆盖"
        ),
    ),
    "orchd/gitops/hook": SubdomainDecision(
        modules=(),
        reason="七域全部登记进 SHARED_ENTRY_TESTS（W6-C8 返修 + active_gate），改它必须跑登记测试",
    ),
    "orchd/gitops/guard": SubdomainDecision(
        modules=(
            "orchd/gitops/guard/__init__.py",
            "orchd/gitops/guard/checkout.py",
            "orchd/gitops/guard/core.py",
            "orchd/gitops/guard/lifecycle.py",
            "orchd/gitops/guard/restore.py",
            "orchd/gitops/guard/write.py",
        ),
        reason="写操作红线守卫；行为面由 tests/test_gitops_guard.py 与 gitops 行为测试覆盖",
    ),
    "orchd/gitops/session_lock": SubdomainDecision(
        modules=(),
        reason="session lock/gate 协调域（W8-5 拆包），十三域全部登记进 SHARED_ENTRY_TESTS",
    ),
    "orchd/gitops/query": SubdomainDecision(
        modules=(
            "orchd/gitops/query/__init__.py",
            "orchd/gitops/query/cache.py",
            "orchd/gitops/query/landed.py",
            "orchd/gitops/query/probe.py",
            "orchd/gitops/query/refs.py",
            "orchd/gitops/query/workspace.py",
            "orchd/gitops/query/worktree.py",
        ),
        reason=(
            "git 只读查询域（W8-10，task-split2-w8-10-gitops-query-pkg 自单文件 query.py 拆出）；"
            "七域逐条枚举，行为面由 tests/test_gitops*.py / test_git_guards.py 覆盖，"
            "AST 指纹 / 定义块原文 / 布局预算 / 域 DAG 由 tests/test_gitops_query_pkg.py 守卫"
        ),
    ),
    "orchd/gitops_ops": SubdomainDecision(
        modules=(
            "orchd/gitops_ops/__init__.py",
            "orchd/gitops_ops/branch.py",
            "orchd/gitops_ops/events.py",
            "orchd/gitops_ops/gitver.py",
            "orchd/gitops_ops/output.py",
            "orchd/gitops_ops/reconcile.py",
            "orchd/gitops_ops/union.py",
        ),
        reason="git 写操作代理域；行为面由 tests/test_gitops*.py 与端到端流程覆盖",
    ),
    "orchd/onboard/lifecycle/core": SubdomainDecision(
        modules=(
            "orchd/onboard/lifecycle/core/__init__.py",
            "orchd/onboard/lifecycle/core/baseline.py",
            "orchd/onboard/lifecycle/core/entry_guard.py",
            "orchd/onboard/lifecycle/core/finish.py",
            "orchd/onboard/lifecycle/core/orchestrator.py",
            "orchd/onboard/lifecycle/core/reconcile.py",
            "orchd/onboard/lifecycle/core/stages.py",
            "orchd/onboard/lifecycle/core/verify.py",
        ),
        reason="done/verify 主流程域；行为面由 tests/test_lifecycle*.py 与 test_done.py 覆盖",
    ),
    "orchd/onboard/claim": SubdomainDecision(
        modules=(
            "orchd/onboard/claim/__init__.py",
            "orchd/onboard/claim/execute.py",
            "orchd/onboard/claim/precheck.py",
            "orchd/onboard/claim/review_face.py",
            "orchd/onboard/claim/run.py",
            "orchd/onboard/claim/scope.py",
        ),
        reason="claim 认领域（task-split2-w8-1-claim-pkg 自单文件拆出）；"
               "行为面由 tests/test_claim*.py 与 test_onboard.py 覆盖",
    ),
    "orchd/onboard/request": SubdomainDecision(
        modules=(
            "orchd/onboard/request/__init__.py",
            "orchd/onboard/request/conflicts.py",
            "orchd/onboard/request/constants.py",
            "orchd/onboard/request/entry.py",
            "orchd/onboard/request/gitinfo.py",
            "orchd/onboard/request/phase.py",
            "orchd/onboard/request/routing.py",
        ),
        reason=(
            "request 只读查询域（W8-7，task-split2-w8-7-onboard-request-pkg 自单文件拆出）；"
            "七域逐条枚举，行为面由 tests/test_request*.py 与 test_onboard.py 覆盖，"
            "AST 指纹 / 定义块原文 / 布局预算 / 域 DAG 由 tests/test_onboard_request_pkg.py 守卫"
        ),
    ),
    "orchd/intake": SubdomainDecision(
        modules=(
            "orchd/intake/__init__.py",
            "orchd/intake/commit.py",
            "orchd/intake/guard.py",
            "orchd/intake/idea.py",
            "orchd/intake/parse.py",
            "orchd/intake/roadmap.py",
        ),
        reason="摄入通道域；行为面由 tests/test_intake*.py 与 test_cli_ideas.py 覆盖",
    ),
    "orchd/worktree/layout": SubdomainDecision(
        modules=(
            "orchd/worktree/layout/__init__.py",
            "orchd/worktree/layout/bootstrap.py",
            "orchd/worktree/layout/detect.py",
            "orchd/worktree/layout/gitinfo.py",
            "orchd/worktree/layout/marker.py",
            "orchd/worktree/layout/migration.py",
            "orchd/worktree/layout/paths.py",
        ),
        reason="布局探测/迁移域；行为面由 tests/test_worktree_layout_pkg.py 与布局测试覆盖",
    ),
    "orchd/worktree/recycle": SubdomainDecision(
        modules=(
            "orchd/worktree/recycle/__init__.py",
            "orchd/worktree/recycle/cleanup.py",
            "orchd/worktree/recycle/journal.py",
            "orchd/worktree/recycle/remove.py",
            "orchd/worktree/recycle/steps.py",
        ),
        reason="工作树回收域；行为面由 tests/test_worktree_recycle_pkg.py 与回收测试覆盖",
    ),
    "orchd/ledger_sync": SubdomainDecision(
        modules=(
            "orchd/ledger_sync/__init__.py",
            "orchd/ledger_sync/inbound.py",
            "orchd/ledger_sync/local.py",
            "orchd/ledger_sync/merge.py",
            "orchd/ledger_sync/ops.py",
            "orchd/ledger_sync/refs.py",
        ),
        reason="账本跨设备同步域；行为面由 tests/test_ledger_sync_pkg.py 与同步测试覆盖",
    ),
}

# 其余包（**包级**决策单元，任意深度）：包内新增单文件模块随该包理由放行；新建包必须补理由。
# 缺一条 = 新建了一个包却无人决策 ⇒ undeclared_packages 判红。
OTHER_PACKAGE_REASONS: dict[str, str] = {
    "orchd/cli": "命令层根；子域 cli/commands、cli/_util 已按模块决策",
    "orchd/cli/commands/misc": "零散运维命令子包（W6-C7，入口纯重导出）；四域见 cli/commands",
    "orchd/cli/commands/control": "事件写入命令子包（W8-3，入口纯重导出）；十三域见 cli/commands",
    "orchd/contracts": "纯数据契约（事件/状态常量），无独立行为面",
    "orchd/doctor": "只读诊断域；行为面由 tests/test_doctor*.py 覆盖",
    # task-docsgen-relocate / task-docsgen-manifest：生成器随引擎包发布（宿主可
    # ``python -m orchd.docsgen``）；产物清单在 orchd/docs_manifest.py，生成器行为面由
    # tests/test_docs*.py 覆盖，产物新鲜度由 ``orchd docs check`` 门禁兜底。
    "orchd/docsgen": "契约文档生成器包（随引擎发布，实现集中 generators.py）；"
                     "行为面由 tests/test_docs*.py / test_docs_manifest.py 覆盖",
    "orchd/guards": "宿主 guard 配置层（task-host-guard-config，只读）；配置层行为面由"
                    " tests/test_host_guard_config.py 覆盖，guard 判据随 host-guard-* 卡接入",
    "orchd/gitops": "git 只读/锁域根；子域 hook、guard、proxy 各自决策，session_lock 已登记",
    "orchd/gitops/proxy": "只读代理命令域；行为面由 tests/test_gitops*.py 覆盖",
    "orchd/guide": "引导文本域；行为面由 tests/test_guide*.py 覆盖",
    "orchd/ideas": "IDEAS 台账域；行为面由 tests/test_cli_ideas.py 覆盖",
    "orchd/ledger": "事件账本内核；行为面由 tests/test_ledger*.py 覆盖",
    "orchd/lessons": "经验回灌域（W8-8 子包化，task-split2-w8-8-lessons-pkg）；八域行为面由"
                     " tests/test_lessons*.py 覆盖，结构契约由 tests/pkg_contract_data.py"
                     " + tests/test_pkg_contract.py 守卫",
    "orchd/milestone": "里程碑判据门禁（W8-9 子包化，task-split2-w8-9-milestone-pkg）；七域行为面由"
                      " tests/test_milestone*.py 覆盖，AST/块原文/布局/DAG 由"
                      " tests/test_milestone_pkg.py 守卫",
    "orchd/onboard": "任务生命周期层根；子域 lifecycle、lifecycle/core 各自决策",
    "orchd/onboard/lifecycle": "生命周期入口/钩子域；子域 core 已按模块决策",
    "orchd/report": "汇报渲染域；结构由 test_pkg_contract 守卫，行为面见 tests/test_report*.py",
    "orchd/review": "两阶段审查域（W8-4 子包化）；行为面由 tests/test_review*.py 覆盖",
    "orchd/spec": "校验域；行为面由 tests/test_spec_pkg.py 与 spec 测试覆盖",
    "orchd/storage": "存储后端域（file/sqlite）；行为面由 tests/test_storage*.py 覆盖",
    "orchd/worktree": "工作树域根；子域 layout、recycle、lifecycle 各自决策",
    "orchd/worktree/lifecycle": "工作树生命周期编排域；行为面由 tests/test_worktree*.py 覆盖",
}

# 顶层单文件模块（`orchd/*.py`）：每个都要有理由——「第 6 个域」最常见的形态就是根目录新增一个文件。
TOP_LEVEL_MODULE_REASONS: dict[str, str] = {
    "orchd/__init__.py": "包入口（版本号 + git 分支导出）",
    "orchd/__main__.py": "CLI 入口壳（与 .orchd/__main__.py 同职责）",
    # task-stale-red-guards：前序包新增的两个常量真源单文件（顶层模块登记补齐）。
    "orchd/constants.py": "全局常量真源（环境变量名/上限等）；由各域守卫覆盖",
    "orchd/names.py": "运行时资产名/目录名真源；由各域守卫覆盖",
    "orchd/definition_archive.py": "任务定义归档域；行为面由归档相关测试覆盖",
    "orchd/deps.py": "结构化依赖真源查询层（task-deps-graph-truth-source）；由 test_deps 覆盖",
    "orchd/drift_closure.py": "done 期改动集镜像闭环（task-guard-drift-done-closure，"
    "B1）；行为面由 tests/test_drift_closure.py 覆盖，接入面由 test_onboard 的 done 用例锁定",
    "orchd/errors.py": "错误码与异常类型（纯常量 + 单一类，扇入高但无职责交错）",
    "orchd/line.py": "分线模型；行为面由 tests/test_line*.py 覆盖",
    "orchd/line_ctx.py": "分线上下文注入；随 line 域测试覆盖",
    "orchd/line_sync.py": "跨线回移通道；行为面由 tests/test_line_sync*.py 覆盖",
    "orchd/lockfile.py": "文件锁原语；行为面由锁与并发用例覆盖",
    "orchd/migrate.py": "schema 迁移命令；行为面由迁移相关测试覆盖",
    "orchd/nogit.py": "无 git 模式降级；行为面由 tests/test_nogit*.py 覆盖",
    "orchd/parallel_face.py": "共享核心面与子域并行面；由 test_concurrency 覆盖",
    "orchd/pool.py": "就绪池/冲突检测（核心面已抽到 parallel_face.py）；由 test_pool 覆盖",
    "orchd/registry.py": "registry 读写；行为面由 registry 相关测试覆盖",
    "orchd/registry_file.py": "registry 文件后端；随 registry 域测试覆盖",
    "orchd/contract_docs.py": "审查契约文档判据（引擎/宿主双 profile，"
    "task-contract-docs-host-enforce）；由 tests/test_contract_docs.py 覆盖",
    "orchd/repo_role.py": "仓角色判据（引擎仓 vs 宿主，task-host-guard-repo-scope）；"
    "由 tests/test_repo_role.py 覆盖",
    "orchd/schema_policy.py": "schema 注解派生策略；与 split 手写集合的对拍由 contract 测试锁定",
    "orchd/shared_entries.py": "共享入口 verify 门禁（登记表真源）；由 shared_entry_gate 覆盖",
    # task-docsgen-manifest：产物清单真源（id/kind/产物/生成器/真源/同步点/门禁档/宿主策略），
    # 生成物注册面与 done 门禁同源；行为面由 tests/test_docs_manifest.py 与
    # tests/test_docs_gate.py 覆盖。
    "orchd/docs_manifest.py": "文档产物清单真源；由 test_docs_manifest / test_docs_gate 覆盖",
    # task-shared-surface-engine：落地面（fragments/generated/serial）注册表与片段协议真源；
    # 行为面由 tests/test_surfaces*.py / test_shared_surfaces_e2e.py 覆盖。
    "orchd/surfaces.py": "共享落地面注册表与片段协议真源；由 tests/test_surfaces*.py 覆盖",
    # 同上（S2a）：片段汇总只做装配、不碰 git；行为面由 tests/test_surface_assembly.py 覆盖。
    "orchd/surface_assembly.py": "片段汇总（S2a 装配层）；由 tests/test_surface_assembly.py 覆盖",
    "orchd/subdomain_census.py": "本模块（子域完备性判据）；由 test_subdomain_census 覆盖"
    "（未登记 SHARED_ENTRY_TESTS，判据只在 pytest / 全量档生效）",
    "orchd/subpackage_guard.py": "拆包子包入口登记守卫；由 test_subpackage_entry_guard 覆盖",
    "orchd/subproc.py": "子进程封装；行为面由各调用方测试覆盖",
}


def _pkg_dir(orchd_dir: Path, pkg_rel: str, prefix: str = _ORCHD_PREFIX) -> Path:
    """仓库相对包路径 → 绝对目录（只接受 ``prefix`` 前缀，越界即抛 ValueError）。"""
    if not pkg_rel.startswith(prefix):
        raise ValueError(f"包路径必须以 {prefix!r} 开头：{pkg_rel!r}")
    return Path(orchd_dir) / pkg_rel[len(prefix):]


def _repo_rel(scope_or_dir: "CensusScope | Path | str", path: Path) -> str:
    """模块文件 → 仓库相对 POSIX 路径（与 SHARED_ENTRY_TESTS 同口径）。"""
    return path.resolve().relative_to(_scope(scope_or_dir).repo_root.resolve()).as_posix()


def _pkg_modules(scope_or_dir: "CensusScope | Path | str", pkg_dir: Path) -> list[str]:
    """包目录下（递归）全部模块的仓库相对路径，已排序、排除 ``__pycache__``。"""
    return sorted(
        _repo_rel(scope_or_dir, p)
        for p in pkg_dir.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def subdomain_unaccounted(scope_or_dir: "CensusScope | Path | str") -> list[str]:
    """作用域包内既未登记、也不在所属包决策枚举内的模块（排序；空 = 通过）。"""
    scope = _scope(scope_or_dir)
    unaccounted: list[str] = []
    for pkg, decision in scope.declared.items():
        pkg_dir = _pkg_dir(scope.base, pkg, scope.prefix)
        if not pkg_dir.is_dir():
            # 包不存在属「死条目」，由 stale_registry_entries 专用判据负责，此处不重复报。
            continue
        decided = set(decision.modules) | {
            entry for entry in scope.shared if entry.startswith(pkg + "/")
        }
        unaccounted.extend(rel for rel in _pkg_modules(scope, pkg_dir) if rel not in decided)
    return sorted(unaccounted)


def undeclared_packages(scope_or_dir: "CensusScope | Path | str") -> list[str]:
    """未决策的包（任意深度；排序；空 = 通过）。

    包 = 含 ``__init__.py`` 的目录。新建包必须补进 ``declared``（模块级决策）或 ``other``
    （包级决策）；命名空间式目录（无 ``__init__.py``）由 :func:`undeclared_namespace_dirs` 判。
    """
    scope = _scope(scope_or_dir)
    flagged: list[str] = []
    for path in sorted(scope.base.rglob("*")):
        # 不显式排 `__pycache__`：它不是包（无 `__init__.py`），下面这条判定天然跳过它。
        if not path.is_dir() or not (path / "__init__.py").is_file():
            continue
        rel = _repo_rel(scope, path)
        if rel not in scope.declared and rel not in scope.other:
            flagged.append(rel)
    return sorted(flagged)


def undeclared_namespace_dirs(scope_or_dir: "CensusScope | Path | str") -> list[str]:
    """含 ``.py`` 但没有 ``__init__.py`` 的目录（排序；空 = 通过）。

    只判**直接**含 ``.py`` 文件的目录——空目录与纯数据目录不误报。
    """
    scope = _scope(scope_or_dir)
    flagged: list[str] = []
    for path in sorted(scope.base.rglob("*")):
        if not path.is_dir() or "__pycache__" in path.parts:
            continue
        if (path / "__init__.py").is_file():
            continue
        if any(p.suffix == ".py" for p in path.iterdir() if p.is_file()):
            flagged.append(_repo_rel(scope, path))
    return sorted(flagged)


def undeclared_top_level_modules(scope_or_dir: "CensusScope | Path | str") -> list[str]:
    """``<base>/*.py`` 中未决策的模块（排序；空 = 通过）。"""
    scope = _scope(scope_or_dir)
    flagged: list[str] = []
    for path in sorted(scope.base.glob("*.py")):
        rel = _repo_rel(scope, path)
        if rel not in scope.top_level:
            flagged.append(rel)
    return flagged


def stale_registry_entries(scope_or_dir: "CensusScope | Path | str") -> list[str]:
    """登记面里指向磁盘上不存在对象的条目（排序；空 = 通过）。

    覆盖：作用域包本身不存在、决策枚举里的模块路径不存在或越出所属包、其它包理由表与
    顶层单文件理由表指向不存在的对象。``shared`` 表自身的死条目不在本函数判（其真源由
    ``tests/test_shared_entry_gate.py`` 锁定，本函数只消费）。
    """
    scope = _scope(scope_or_dir)
    stale: list[str] = []
    for pkg, decision in scope.declared.items():
        pkg_dir = _pkg_dir(scope.base, pkg, scope.prefix)
        if not pkg_dir.is_dir():
            stale.append(pkg)
            continue
        for rel in decision.modules:
            if not rel.startswith(pkg + "/") or not (scope.repo_root / rel).is_file():
                stale.append(rel)
    for pkg in scope.other:
        if not (_pkg_dir(scope.base, pkg, scope.prefix) / "__init__.py").is_file():
            stale.append(pkg)
    for rel in scope.top_level:
        if not (scope.repo_root / rel).is_file():
            stale.append(rel)
    return sorted(stale)
