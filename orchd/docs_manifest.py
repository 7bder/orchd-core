"""契约文档产物清单（单一真源，task-docsgen-manifest）。

每份生成产物登记：``id`` / ``kind``（``file`` / ``block``）/ ``artifact`` / 生成器函数名 /
``sources``（真源）/ ``sync``（同步点）/ ``gate``（门禁档位）/ ``host``（宿主策略）。
drift_closure、``orchd docs`` 命令与后续宿主文档通道共用本清单，不再各自维护手写映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable

__all__ = (
    "DOC_ARTIFACTS",
    "DocArtifact",
    "SYNC_POINTS",
    "artifacts",
    "by_id",
    "done_entries",
    "edges",
    "generator_for",
    "generators_map",
    "matches_changed",
    "matches_sources",
    "merge_entries",
    "resolve_manifest",
    "sync_point",
    "validate",
)

KINDS = frozenset({"file", "block"})
GATES = frozenset({"hard", "warn"})
HOSTS = frozenset({"engine-only", "host-optional", "host-required"})
#: 同步点（单一写入点）：``done`` = 实现完成时（done 门禁比对/声明）；``merge`` = 合并入口；
#: ``manual`` = 仅人工显式写入。每份产物**只允许一个**同步点，避免多写路径互相覆盖。
SYNC_POINTS = frozenset({"done", "merge", "manual"})

#: 生成块协议标记（``kind=block`` 时用于定位/替换产物中的生成区段）。
BLOCK_BEGIN = "<!-- BEGIN GENERATED: {id} -->"
BLOCK_END = "<!-- END GENERATED: {id} -->"


@dataclass(frozen=True)
class DocArtifact:
    """单份契约文档产物的登记项（不可变）。"""

    id: str
    kind: str
    artifact: str
    generator: str
    sources: tuple[str, ...]
    sync: tuple[str, ...]
    gate: str
    host: str


# 约定：file 类生成物必须位于 ``docs/_generated/`` 下；宿主布局镜像到
# ``.orchd/docs/_generated/``。pre-commit 的主干（bound）生成物豁免按目录匹配，
# 新增生成物目录须同步 ``orchd/gitops/hook/precommit_template.py`` 与契约测试。
DOC_ARTIFACTS: tuple[DocArtifact, ...] = (
    DocArtifact(
        "error-codes", "file", "docs/_generated/error-codes.md",
        "gen_error_codes_md", ("orchd/errors.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "commands", "file", "docs/_generated/commands.md",
        "gen_commands_md", ("orchd/cli/parser.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "fields", "file", "docs/_generated/fields.md",
        "gen_fields_md", ("schema/_master.schema.json",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "events", "file", "docs/_generated/events.md",
        "gen_events_md", ("orchd/contracts/events.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "statemachine", "file", "docs/_generated/statemachine.md",
        "gen_statemachine_md", ("orchd/contracts/events.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "dependency-graph", "file", "docs/_generated/dependency-graph.md",
        "gen_dependency_graph_md",
        (".orchd/shared/deps.json", "orchd/**/*.py"),
        ("merge",), "hard", "engine-only",
    ),
    # 正文生成块（migrate-1）：命令表由 orchd/cli/manifest.py 元数据单源生成，
    # 三份文档共用同一生成器；只替换块内区段，块外正文逐字节不动。
    DocArtifact(
        "commands-user-manual", "block", "docs/user-manual.md",
        "gen_command_table_md", ("orchd/cli/manifest.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "commands-runtime-spec", "block", "docs/runtime-spec.md",
        "gen_command_table_md", ("orchd/cli/manifest.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "commands-readme", "block", "README.md",
        "gen_command_table_md", ("orchd/cli/manifest.py",),
        ("done",), "hard", "host-optional",
    ),
    # migrate-2：契约表生成块（三表真源 = 本模块下方数据登记）与漂移矩阵整文件。
    DocArtifact(
        "compat-gates", "block", "docs/compat-policy.md",
        "gen_compat_gates_md", ("orchd/docs_manifest.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "kernel-invariants", "block", "docs/kernel-contract.md",
        "gen_invariants_md", ("orchd/docs_manifest.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "constraints-ledger", "block", "docs/constraint-hardening.md",
        "gen_constraints_md", ("orchd/docs_manifest.py",),
        ("done",), "hard", "host-optional",
    ),
    DocArtifact(
        "deps-conventions", "block", ".orchd/shared/conventions.md",
        "gen_deps_summary_md", (".orchd/shared/deps.json",),
        ("done",), "hard", "engine-only",
    ),
    DocArtifact(
        "drift-matrix", "file", "docs/contract-drift-matrix.md",
        "gen_drift_matrix_md", ("orchd/docs_manifest.py",),
        ("done",), "hard", "host-optional",
    ),
)


def artifacts() -> tuple[DocArtifact, ...]:
    """全部登记产物（顺序稳定）。"""
    return DOC_ARTIFACTS


def by_id(artifact_id: str) -> DocArtifact | None:
    """按 ``id`` 取登记项（未知返回 ``None``）。"""
    return next((item for item in DOC_ARTIFACTS if item.id == artifact_id), None)


def sync_point(entry: DocArtifact) -> str:
    """产物的同步点（单写路径；配置非法时返回空串，由 :func:`validate` 兜底报错）。"""
    return entry.sync[0] if entry.sync else ""


def done_entries() -> tuple[DocArtifact, ...]:
    """同步点 = ``done`` 的产物（done 期比对/声明面）。"""
    return tuple(entry for entry in DOC_ARTIFACTS if sync_point(entry) == "done")


def merge_entries() -> tuple[DocArtifact, ...]:
    """同步点 = ``merge`` 的产物（合并入口单写，done 不抢写）。"""
    return tuple(entry for entry in DOC_ARTIFACTS if sync_point(entry) == "merge")


def _normalized_paths(changed: Iterable[str]) -> set[str]:
    return {str(p).replace("\\", "/").strip().removeprefix("./") for p in changed}


def matches_sources(entry: DocArtifact, changed: Iterable[str]) -> bool:
    """``--changed`` 过滤（**只看真源**，产物自身命中不算真源变化）。"""
    normalized = _normalized_paths(changed)
    for source in entry.sources:
        if source in normalized:
            return True
        if ("*" in source or "?" in source) and any(
            _glob_match(source, path) for path in normalized
        ):
            return True
    return False


def _literal_sources(entry: DocArtifact) -> tuple[str, ...]:
    """可精确比对的真源路径（去掉 ``**`` 通配的宽面源）。"""
    return tuple(src for src in entry.sources if "*" not in src and "?" not in src)


def edges() -> list[tuple[str, str]]:
    """``(真源, 产物)`` 边（宽面源不参与改动集硬判定）。"""
    out: list[tuple[str, str]] = []
    for entry in DOC_ARTIFACTS:
        for source in _literal_sources(entry):
            if (source, entry.artifact) not in out:
                out.append((source, entry.artifact))
    return out


def generator_for(name: str) -> Callable[[], str] | None:
    """按函数名解析 ``orchd.docsgen`` 中的生成器（不可用返回 ``None``）。"""
    try:
        from orchd import docsgen
    except Exception:  # noqa: BLE001 - 生成器包不可读 ⇒ 调用方降级
        return None
    func = getattr(docsgen, name, None)
    return func if callable(func) else None


def generators_map() -> dict[str, Callable[[], str]]:
    """``产物文件名 → 生成器`` 视图（兼容 drift_closure 的 ``GENERATORS`` 形状）。

    只含 ``kind=file``：block 生成器输出的是**块内区段**而非整文件，混入会让
    ``write_docs`` / 生成物新鲜度把整份正文文档按块内容覆盖/比对。
    """
    out: dict[str, Callable[[], str]] = {}
    for entry in DOC_ARTIFACTS:
        if entry.kind != "file":
            continue
        if not entry.artifact.startswith("docs/_generated/"):
            # 生成物新鲜度判据只盯 docs/_generated/ 快照面；docs/ 根下的整文件
            # 生成物（drift-matrix）由 docs check / 契约测试覆盖，不入该视图。
            continue
        func = generator_for(entry.generator)
        if func is not None:
            out[Path(entry.artifact).name] = func
    return out


def as_namespace() -> SimpleNamespace:
    """``GENERATORS`` 命名空间（供 ``_load_generators`` 复用既有读取路径）。"""
    return SimpleNamespace(GENERATORS=generators_map())


def validate() -> list[str]:
    """清单配置校验：返回问题列表（空 = 健康）。"""
    problems: list[str] = []
    seen: set[str] = set()
    for entry in DOC_ARTIFACTS:
        if entry.id in seen:
            problems.append(f"重复的产物 id：{entry.id}")
        seen.add(entry.id)
        if entry.kind not in KINDS:
            problems.append(f"{entry.id}: kind 非法（{entry.kind}）")
        if entry.gate not in GATES:
            problems.append(f"{entry.id}: gate 非法（{entry.gate}）")
        if entry.host not in HOSTS:
            problems.append(f"{entry.id}: host 非法（{entry.host}）")
        path = Path(entry.artifact)
        if path.is_absolute() or ".." in path.parts:
            problems.append(f"{entry.id}: artifact 必须是项目内相对路径")
        if not entry.sources:
            problems.append(f"{entry.id}: sources 不能为空")
        if len(entry.sync) != 1:
            problems.append(f"{entry.id}: sync 必须是单一同步点（当前 {entry.sync}）")
        elif entry.sync[0] not in SYNC_POINTS:
            problems.append(f"{entry.id}: sync 非法（{entry.sync[0]}）")
        if not callable(generator_for(entry.generator)):
            problems.append(f"{entry.id}: 生成器不可用（{entry.generator}）")
    return problems


def _glob_match(pattern: str, path: str) -> bool:
    """POSIX glob 匹配：``**/`` 可匹配零层目录（``orchd/**/*.py`` 覆盖 ``orchd/errors.py``）。"""
    escaped = re.escape(pattern)
    regex = (
        escaped.replace(r"\*\*/", "(?:.*/)?")
        .replace(r"\*\*", ".*")
        .replace(r"\*", "[^/]*")
        .replace(r"\?", "[^/]")
    )
    try:
        return re.fullmatch(regex, path) is not None
    except re.error:
        return False


def matches_changed(entry: DocArtifact, changed: Iterable[str]) -> bool:
    """``--changed`` 过滤：产物自身或任一真源命中改动集。"""
    normalized = {str(p).replace("\\", "/").strip().removeprefix("./") for p in changed}
    if entry.artifact in normalized:
        return True
    for source in entry.sources:
        if source in normalized:
            return True
        if ("*" in source or "?" in source) and any(
            _glob_match(source, path) for path in normalized
        ):
            return True
    return False


def resolve_manifest() -> tuple[list[DocArtifact], list[str]]:
    """返回 ``(产物列表, 配置问题)``。"""
    return list(DOC_ARTIFACTS), validate()

# ── 正文生成块与生成文件数据登记（task-docsgen-migrate-2）──────────
# 三张契约表与漂移矩阵的机器登记本：文档侧只保留生成块，改表先改这里。
COMPAT_GATES: tuple[dict[str, str], ...] = (
    {"surface": 'schema/事件/flags 破坏性', "gate": '`scripts/check_contract_breaking.py`（基线快照比对）'},  # noqa: E501
    {"surface": '生成物过期', "gate": '`test_generated_docs.py` 新鲜度 + CI freshness 步骤'},
    {"surface": '依赖反向', "gate": '`test_dependency_direction.py` AST 门禁'},
    {"surface": 'rules 幽灵命令', "gate": '`test_rules_lint.py` 命令面 lint'},
    {"surface": '状态机语义', "gate": '`test_invariants_state_machine.py` 确定性种子模型'},
)

KERNEL_INVARIANTS: tuple[dict[str, str], ...] = (
    {"id": 'INV-1', "name": '**变更口径含 D/R**', "note": '变更文件集合必须含**删除(D) / 重命名(R)**，且提交层与 done 层**同一判定来源**（承 `task-decl-hook-delete-parity` / `task-e010-delete-parity-done-guards`）。**边界（已固定）**：无 git 侧按**精确内容同一性**折叠重命名；**重命名 + 改写**退化为 D+A（为 git 结果的超集 → 只更严、不漏检）；git 侧 `diff.renames=false` 时跨模式存在差异（已由测试固定）', "anchors": '`tests/test_gitops.py`、`tests/test_git_guards.py`；无 git 侧 `tests/test_nogit_changedet.py`（**A0a 已交付**，2026-09-21）'},  # noqa: E501
    {"id": 'INV-2', "name": '红线 #3 不因环境退化', "note": '越界改动检测在 git 与无 git 两种模式**判定一致**，不得降级为 `NotApplicable`', "anchors": '`tests/test_git_guards.py`；无 git 侧 `tests/test_nogit_lifecycle.py`（A0b）'},  # noqa: E501
    {"id": 'INV-3', "name": 'replay 幂等 / 确定性', "note": '同序列事件 replay 结果确定且幂等；账本 append 原子', "anchors": '`tests/test_ledger.py`、`tests/test_ledger_sync.py`'},  # noqa: E501
    {"id": 'INV-4', "name": '单任务单会话忙度', "note": '一个 agent 同时只持一个任务（E011）；审查期实现者冻结（R1-b / E017）', "anchors": '`tests/test_control.py`、`tests/test_git_guards.py`'},  # noqa: E501
    {"id": 'INV-5', "name": 'claim 近似互斥', "note": '超时阈值 + retract 冷却 + E034 归属守卫（v2 起由服务端租约 + fencing 取代）', "anchors": '`tests/test_control.py`'},  # noqa: E501
    {"id": 'INV-6', "name": '审查两阶段', "note": 'spec → code 两段 APPROVED（unified 为显式 opt-in）', "anchors": '`tests/test_docs.py`、审查相关用例'},  # noqa: E501
    {"id": 'INV-7', "name": '质量门不名义化', "note": '发版门禁（`full_regression` 覆盖 + 基线差分）与性能预算（`test_perf_budget.py` 上限不上调）为**阻断**语义', "anchors": '`tests/test_perf_budget.py`、`scripts/sync_orchd_core.sh`'},  # noqa: E501
    {"id": 'INV-8', "name": '**审查者独立性**', "note": '自审恒阻断（实现指纹 == 审查指纹时 claim 与 submit 均 `E016 self_review_blocked`，无 config/任务级开关、无降级档）；审查会话须 `kind=reviewer` 且 `parent_session_id` = 该任务最后一次 DONE 的会话 id，缺失/不符同样 E016（claim 与 submit 双检；旧账本 DONE 缺 `session_id` 时 fail-closed）。parent 为会话自声明，不可伪造性待服务端身份（见 system-design-online.md §11）', "anchors": '`tests/test_review_lineage.py`、`tests/test_review_self_review.py`、`tests/test_claim.py`、`tests/test_review.py`'},  # noqa: E501
)

SINK_CONSTRAINTS: tuple[dict[str, str], ...] = (
    {"constraint": '禁自审（E016）', "layer": '引擎', "sink": '已沉', "status": '已沉'},
    {"constraint": '分支守卫 claim/done/amend/review（E017/E018）', "layer": '引擎', "sink": '已沉', "status": '已沉。**注意（2026-08-14 intake-commit-enforcement）**：amend 此前仅沉「分支维度」（红线 7 拒绝非 main 注册），**干净度维度未沉**（脏工作区不影响注册）；本次补沉——amend 前置「非摄入产物干净」守卫（摄入产物 IDEAS/ROADMAP/master 允许未提交，其余已跟踪改动 → E017 阻断）'},  # noqa: E501
    {"constraint": '会话锁（E019）', "layer": '引擎', "sink": '已沉', "status": '已沉'},
    {"constraint": '越界提交拦截（L3 hook，E020）', "layer": '引擎', "sink": '已沉', "status": '已沉'},  # noqa: E501
    {"constraint": 'merge 前置化（completed 语义）', "layer": '引擎', "sink": '已沉', "status": '已沉'},  # noqa: E501
    {"constraint": 'claim 前置（分支 ∈ {main, task} + 工作区干净）', "layer": '引擎', "sink": '已沉', "status": '已沉'},  # noqa: E501
    {"constraint": '注册强制文件冲突串行化（depends_on）', "layer": '引擎', "sink": '已沉 → **2026-08-08 降级为 warning**（amend 不再拒绝注册，仅返回 `conflict_warnings`）', "status": '已沉 → 冲突硬边界移至 request 依赖感知强制过滤（`excluded_conflicts`）+ claim E010'},  # noqa: E501
    {"constraint": '不 push（引擎天然不执行）', "layer": '引擎', "sink": '已沉', "status": '已沉'},
    {"constraint": 'E024 缺 basetemp', "layer": '引擎', "sink": '有降级路径（warning）', "status": 'best-effort → **1.1.1 阻断升级已规划**'},  # noqa: E501
    {"constraint": 'E022 缺 verify_command', "layer": '引擎', "sink": '有降级路径（warning）', "status": '**R5 已沉（2026-08-13）**：代码类任务缺 verify_command 注册阻断；文档/基础设施类维持 warning'},  # noqa: E501
    {"constraint": 'E023 模糊验收标准', "layer": '引擎', "sink": '有降级路径（warning）', "status": '**R6 已评估（2026-08-13）**：维持 warning，注册时显著提示 + 人工确认记录'},  # noqa: E501
    {"constraint": 'E021 身份不一致', "layer": '引擎', "sink": '仅 warning，可被换 ID 绕过', "status": 'best-effort → 留待 1.5 权限模型'},  # noqa: E501
    {"constraint": '红线 7：任务分支执行 intake/amend', "layer": '约定层', "sink": '`not_on_main` 只是不提交，注册仍发生', "status": '**已沉（2026-09-11 task-master-single-copy）**：split.amend 非 default 分支直接 E007 拒绝注册（含 task/xxx），不再有 task 分支 amend 例外；hint 指向主工作树补声明路径'},  # noqa: E501
    {"constraint": '红线 10：任务悬空（claimed 超时未 done）', "layer": '约定层', "sink": 'watchdog 已检僵死锁，未检 claimed 超时', "status": '可沉-待立项（watchdog 扩展）'},  # noqa: E501
    {"constraint": '红线 8：手改 .orchd 运行时文件', "layer": '约定层', "sink": '引擎启动可校验 ledger/checkpoint 完整性', "status": '**R3 已沉（2026-08-13）**：Store.check_integrity() 校验 checkpoint 与 ledger 一致性，status 及写命令前置挂载，E030 告警不阻断'},  # noqa: E501
    {"constraint": '任务拆解粒度启发式', "layer": '约定层', "sink": '可做 schema 校验（warning 级，触碰内容域）', "status": '**R4 已沉（2026-08-13，warning 级）**：validate_quality 越界即提示拆分，不注册阻断'},  # noqa: E501
    {"constraint": '红线 1/2：手动 git / 破坏性 git', "layer": '约定层', "sink": '引擎无法拦截用户侧裸 git', "status": '不可沉（维持约定 + 现有守卫）'},  # noqa: E501
    {"constraint": '红线 3：修改范围外文件', "layer": '约定层', "sink": 'L3 hook 已挡提交路径；直接改文件不可观测', "status": '部分已沉（提交路径），直接改不可沉'},  # noqa: E501
    {"constraint": '红线 4：单 session 单 agent ID', "layer": '约定层', "sink": 'session 概念不可观测', "status": '不可沉'},  # noqa: E501
    {"constraint": '红线 5：未提交即中断', "layer": '约定层', "sink": '无法检测 session 结束', "status": '不可沉（整体）。**可沉子集已沉（2026-08-14 intake-commit-enforcement）**：引擎命令触发的提交强制——amend/intake 是引擎入口，提交范围（IDEAS/ROADMAP/master）可枚举 → amend 前置干净守卫 + commit 降级可审计化（commit_warning）+ `orchd intake` 命令 + `status --audit-intake` 巡检'},  # noqa: E501
    {"constraint": '红线 6：自动摄入', "layer": '约定层', "sink": '摄入时机无引擎信号', "status": '不可沉（摄入时机）；摄入产物提交已引擎化（`orchd intake`，2026-08-14）'},  # noqa: E501
    {"constraint": '红线 9：绕过 claim 手动建分支', "layer": '约定层', "sink": 'audit-merge 已检出残余分支', "status": '部分已沉（事后检出），事前不可沉'},  # noqa: E501
    {"constraint": '三条铁律（原文可追溯/推断标注/统一查重）', "layer": '约定层', "sink": '判断类，不可判定', "status": '不可沉'},  # noqa: E501
    {"constraint": '写前读 / 异常停止 / 核对引擎响应 / 三连检查', "layer": '约定层', "sink": '不可观测', "status": '不可沉'},  # noqa: E501
    {"constraint": '审查质量判断', "layer": '约定层', "sink": '判断类', "status": '不可沉'},
)

DRIFT_MATRIX: dict[str, object] = {
    "head_lines": ['# 契约漂移矩阵（Contract Drift Matrix）',
 '',
 '> P0 地图：漂移域 × 真源 × 断言 × 门禁。改真源必须同改断言；改断言必须有门禁运行点。',
 '> 单一真源登记：`tests/contract/contract_manifest.py`（P0-②落盘）。',
 ''],
    "sep": '|---|---|---|---|',
    "rows": (
        {"domain": '错误码表/文案', "source": '`orchd/errors.py::ErrorCode` + `orchd/guide.py::ERROR_GUIDANCE/ERROR_CODE_CHANNELS`', "assertions": '`tests/contract/_checkers.py` 错误码表类 + `tests/test_docs.py` 通道登记', "gates": 'pre-push 全量段 + CI test job'},  # noqa: E501
        {"domain": '命令面', "source": '`orchd/cli/parser.py` 注册面 + 子命令组注册函数', "assertions": '`tests/contract/_checkers.py` 命令面类 + `tests/test_docs.py::test_command_table_matches_argparse_help`', "gates": 'CI test job'},  # noqa: E501
        {"domain": '阈值/默认', "source": '代码常量（claim 600s / review 300s / retract 300s / conflict warn / enforce false）', "assertions": '`tests/test_docs.py::test_behavior_defaults_consistent_between_docs_and_engine` + `_checkers.py` 阈值类', "gates": 'CI test job'},  # noqa: E501
        {"domain": '状态表', "source": '`_apply_event` 状态机 + guidance step/next_action 词表', "assertions": '`_checkers.py` 状态表类 + rules/review.md 三查', "gates": 'spec+code 双阶段审查'},  # noqa: E501
        {"domain": '字段表', "source": '`schema/_master.schema.json` 任务属性的 `x-amendable` / `x-terminal-attachable` 注解，经 `orchd/schema_policy.py` 派生、`orchd/split/` 消费（W1-④ 已删手写副本集合）', "assertions": '`tests/contract/test_schema_annotations.py` 注解↔派生对拍 + `test_schema_cutover.py` 副本不得再定义 + `test_schema_policy_parity.py` 结构不变量 + `tests/test_docs_drift_batch.py` 幻影符号反向扫描（文档回指旧副本即红）', "gates": 'CI test job + amend E007'},  # noqa: E501
        {"domain": '事件表', "source": '`contracts/events.py`（W2-①②已落）', "assertions": '`tests/contract/test_event_contracts.py` + append fail-closed 校验 + replay 等价性', "gates": 'CI test job'},  # noqa: E501
        {"domain": '生成物新鲜度', "source": '`scripts/gen_contract_docs.py`（P1 已落）', "assertions": '`tests/contract/test_generated_docs.py` regen零差异 + CI freshness步骤（regen diff）', "gates": 'CI test job'},  # noqa: E501
        {"domain": '依赖方向', "source": '机读依赖图（P3-①已落：conventions.md 即真源）', "assertions": '`tests/contract/test_dependency_direction.py` AST 断言', "gates": 'CI test job'},  # noqa: E501
        {"domain": 'rules↔执法', "source": '`rules/*.md` 引用的命令/flag', "assertions": '`tests/contract/test_rules_lint.py` 命令面 + git 标注 lint（P3-②已落）', "gates": 'CI test job'},  # noqa: E501
        {"domain": '契约门禁接线', "source": '`.githooks/pre-push` 契约段', "assertions": '`tests/contract/test_prepush_contract_segment.py` 存在性 + CI test job 全量', "gates": 'pre-push 契约段（P2-①）'},  # noqa: E501
        {"domain": '脚本常驻性', "source": '`scripts/verify_*.py` 30 个', "assertions": '`tests/contract/test_verify_triage.py` triage 全覆盖（7 CI / 7 测试执行 / 16 历史引用）', "gates": 'CI test job（P2-②）'},  # noqa: E501
        {"domain": '状态机声明化', "source": '`contracts.TRANSITION_TABLE`（W2-③已落）', "assertions": '`tests/contract/test_statemachine_table.py` 表↔派生一致 + 生成表新鲜度', "gates": 'CI test job'},  # noqa: E501
        {"domain": '兼容快照', "source": '基线快照（P4-①已落）', "assertions": '`tests/contract/test_compat_gate.py` 加法过/破坏拦', "gates": 'CI test job'},  # noqa: E501
        {"domain": '状态机不变量', "source": '状态机语义（P4-②已落）', "assertions": '`tests/contract/test_invariants_state_machine.py` 确定性种子模型', "gates": 'CI test job'},  # noqa: E501
        {"domain": '治理', "source": 'ADR + 兼容策略（P5-①已落）', "assertions": '`tests/contract/test_governance_docs.py` 存在性互链', "gates": 'CI test job'},  # noqa: E501
        {"domain": '漂移巡检', "source": '生成物 + 登记表（P5-②已落）', "assertions": 'doctor `contract_drift` 节 + `test_doctor_drift.py` 三锁', "gates": 'doctor + CI test job'},  # noqa: E501
    ),
    "tail_lines": ['',
 '## 缺口（P0 已知，P1-W2 收敛）',
 '',
 '- `tests/contract/` 常驻套件缺失 → P0-②补骨架。',
 '- 错误码/命令/阈值/状态四类跨域断言缺失 → P0-③补 `_checkers.py`（difficulty 除外）。',
 '- 生成器已落（P1-①②③：生成器+生成物+freshness门禁）。',
 '- 门禁接线已落（P2-①②：pre-push契约段+verify脚本triage收敛）。',
 '- 依赖方向与规则执法已落（P3-①②：AST门禁+rules lint；附带修 `rules/git.md` 手动 gc 处方与红线的矛盾）。',  # noqa: E501
 '- 事件契约与状态机声明化已落（W2-①②③：契约表+append门禁+转移表驱动）。',
 '- 兼容快照与不变量已落（P4-①②）。',
 '- 治理已落（P5-①②：ADR+兼容策略+doctor漂移节）。'],
}
