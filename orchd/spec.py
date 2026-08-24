"""Orchd _master.json 加载、结构校验与跨引用校验。

校验流程分为两个阶段：
1. 结构校验（validate_structure）—— 基于 JSON Schema（Draft 2020-12）对字段类型、
   必选/可选、枚举值等进行校验。
2. 引用校验（validate_references）—— 检查 ID 唯一性、跨引用存在性，并使用
   Kahn 拓扑排序算法检测依赖图中的环。

错误码登记（claim 路径引用）：
- E007: invalid_state（状态不合法，如 phase_mismatch / not_designated_reviewer）
- E008: task_not_ready（任务未就绪，如非 pending/in_review 状态）
- E009: already_claimed（任务已被其他 agent 认领）
- E010: file_conflict（文件冲突，与在握任务 files_to_edit 重叠）
- E011: agent_busy（agent 已持有其他任务）
- E016: self_review_blocked（实现者不得审查自己的实现，确保审查独立性）

依赖方向：spec.py → errors.py（不导入 ledger / pool / onboard）。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from orchd.errors import ErrorCode, OrchdError

# schema 相关文件的目录布局：
#   <项目根>/schema/                    ← _SCHEMA_DIR（默认 schema 根目录）
#   <项目根>/schema/_master.schema.json ← _DEFAULT_SCHEMA_PATH（无版本号时的回退 schema）
#   <项目根>/schema/v{N}/               ← 版本化 schema 子目录（按 schema_version 加载）
_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
_DEFAULT_SCHEMA_PATH = _SCHEMA_DIR / "_master.schema.json"


def _resolve_schema_path(version: int) -> Path:
    """根据版本号加载对应版本的 schema 文件。

    优先查找 schema/v{version}/_master.schema.json，
    如果不存在则回退到 schema/_master.schema.json。
    """
    version_path = _SCHEMA_DIR / f"v{version}" / "_master.schema.json"
    if version_path.exists():
        return version_path
    return _DEFAULT_SCHEMA_PATH


@lru_cache(maxsize=8)
def _load_schema(version: int) -> dict[str, Any]:
    """按版本缓存 schema 内容（P2b：避免每次 validate_structure 重复 json.loads）。"""
    schema_path = _resolve_schema_path(version)
    return json.loads(schema_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _build_validator(version: int) -> jsonschema.Draft202012Validator:
    """按版本缓存 Draft202012Validator（P2b：避免重复构建校验器）。"""
    return jsonschema.Draft202012Validator(_load_schema(version))


@dataclass
class ValidationError:
    """单条校验错误。

    Attributes:
        code: 错误码，对应 errors.ErrorCode 中的枚举值（E003/E004/E005/E006）。
        path: 错误定位路径，采用 JSON Path 风格（如 ``$.tasks[0].depends_on[1]``），
              方便前端或日志系统直接定位到 _master.json 中的问题字段。
        message: 人类可读的错误描述。
    """

    code: ErrorCode
    path: str  # JSON Path 风格，如 "$.tasks[0].depends_on"
    message: str


@dataclass
class Master:
    """_master.json 解析后的数据对象。

    封装了从磁盘加载并 JSON 解析后的原始字典（``raw``）以及来源文件路径
    （``source_path``）。各子结构（project / modules / tasks / shared）通过
    ``@property`` 提供延迟访问，避免在不需要时产生额外拷贝。
    """

    raw: dict[str, Any]
    source_path: Path

    @property
    def project(self) -> dict[str, Any]:
        return self.raw.get("project", {})

    @property
    def modules(self) -> list[dict[str, Any]]:
        return self.raw.get("modules", [])

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return self.raw.get("tasks", [])

    @property
    def shared(self) -> dict[str, Any] | None:
        return self.raw.get("shared")

    @property
    def config(self) -> dict[str, Any]:
        """_master.json 顶层 config 段（引擎行为配置，1.1 起支持）。

        当前支持键：
        - ``importance``: derive_importance 阈值覆盖
          （critical/high/normal 三个下界，缺省键回退默认值）。
        """
        return self.raw.get("config", {})


def load_master(path: Path | str) -> Master:
    """加载 _master.json。

    文件必须以 UTF-8 编码读取；若编码不兼容将触发 E002 错误。

    Raises:
        OrchdError E001: 文件不存在。
        OrchdError E002: JSON 解析失败（含编码错误）。
    """
    path = Path(path)
    if not path.exists():
        raise OrchdError(
            ErrorCode.E001,
            f"file not found: {path}",
            [{"path": str(path), "message": "目标文件不存在"}],
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OrchdError(
            ErrorCode.E002,
            f"invalid JSON in {path}: {exc}",
            [{"path": str(path), "message": str(exc)}],
        ) from exc
    return Master(raw=raw, source_path=path)


def validate_structure(master: Master) -> list[ValidationError]:
    """基于 JSON Schema（Draft 2020-12）做结构校验（字段类型、必选/可选、枚举值）。

    根据 _master.json 中的 ``schema_version`` 字段决定加载哪个版本的 schema：
    优先查找 ``schema/v{version}/_master.schema.json``，不存在时回退到默认 schema。
    返回 E003 错误列表；合法时返回空列表。
    """
    version = master.raw.get("schema_version", 1)
    validator = _build_validator(version)
    errors: list[ValidationError] = []
    for err in sorted(validator.iter_errors(master.raw), key=lambda e: list(e.absolute_path)):
        json_path = "$" + "".join(
            f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path
        )
        errors.append(
            ValidationError(
                code=ErrorCode.E003,
                path=json_path,
                message=err.message,
            )
        )
    return errors


def validate_references(master: Master) -> list[ValidationError]:
    """跨引用完整性校验：ID 唯一性、引用存在性、DAG 无环。

    校验顺序：
    1. E006 —— task / module 的 ID 唯一性检查。
    2. E005 —— task.module 和 task.depends_on 的引用目标是否存在；
       以及 shared 文件的存在性（仅当 master 位于 .orchd/ 目录时检查）。
    3. E004 —— 使用 Kahn 拓扑排序算法检测 task 依赖图中的环：
       逐步移除入度为零的节点，若最终仍有剩余节点则说明存在环。

    错误采用累积策略，不会因单个错误而提前终止，以便一次性返回所有问题。
    返回 E004/E005/E006 错误列表；合法时返回空列表。
    """
    errors: list[ValidationError] = []
    tasks = master.tasks
    modules = master.modules

    # --- E006: ID 唯一性 ---
    task_ids: list[str] = []
    module_ids: list[str] = []

    for i, t in enumerate(tasks):
        tid = t.get("id", "")
        if tid in task_ids:
            errors.append(
                ValidationError(
                    code=ErrorCode.E006,
                    path=f"$.tasks[{i}].id",
                    message=f"duplicate task_id: '{tid}'",
                )
            )
        else:
            task_ids.append(tid)

    for i, m in enumerate(modules):
        mid = m.get("id", "")
        if mid in module_ids:
            errors.append(
                ValidationError(
                    code=ErrorCode.E006,
                    path=f"$.modules[{i}].id",
                    message=f"duplicate module_id: '{mid}'",
                )
            )
        else:
            module_ids.append(mid)

    task_id_set = set(task_ids)
    module_id_set = set(module_ids)

    # --- E005: 引用存在性 ---
    for i, t in enumerate(tasks):
        # module 引用
        mod = t.get("module", "")
        if mod and mod not in module_id_set:
            errors.append(
                ValidationError(
                    code=ErrorCode.E005,
                    path=f"$.tasks[{i}].module",
                    message=f"module '{mod}' not found in modules[]",
                )
            )
        # depends_on 引用
        for j, dep in enumerate(t.get("depends_on", [])):
            if dep not in task_id_set:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E005,
                        path=f"$.tasks[{i}].depends_on[{j}]",
                        message=f"depends_on references unknown task_id: '{dep}'",
                    )
                )

    # --- E005: shared 文件存在性 ---
    # shared 中声明的文件由 BOOTSTRAP 阶段负责写入，路径为相对于项目根的相对路径。
    # 只有当 _master.json 位于标准的 .orchd/ 目录下时，才能通过 parent.parent 可靠
    # 推算出项目根目录；对于从任意路径加载的 master（如临时校验场景），无法对文件
    # 系统布局做假设，因此跳过此检查。
    shared = master.shared
    if shared and master.source_path.parent.name == ".orchd":
        project_root = master.source_path.parent.parent
        for key, rel in shared.items():
            if not (project_root / rel).exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E005,
                        path=f"$.shared.{key}",
                        message=(
                            f"shared file not found: '{rel}'"
                            f"（BOOTSTRAP 声明了 shared.{key} 但未写入该文件）"
                        ),
                    )
                )

    # --- E004: DAG 环检测（Kahn 拓扑排序） ---
    # 构建邻接表与入度表（仅使用已存在的 ID，避免 E005 干扰）
    in_degree: dict[str, int] = {tid: 0 for tid in task_id_set}
    dependents: dict[str, list[str]] = {tid: [] for tid in task_id_set}

    for t in tasks:
        tid = t.get("id", "")
        if tid not in task_id_set:
            continue
        for dep in t.get("depends_on", []):
            if dep in task_id_set:
                in_degree[tid] += 1
                dependents[dep].append(tid)

    queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
    visited_count = 0

    while queue:
        node = queue.popleft()
        visited_count += 1
        for child in dependents[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if visited_count < len(task_id_set):
        # 找出参与环的节点
        cycle_nodes = [tid for tid, deg in in_degree.items() if deg > 0]
        errors.append(
            ValidationError(
                code=ErrorCode.E004,
                path="$.tasks",
                message=f"dependency cycle detected involving: {', '.join(sorted(cycle_nodes))}",
            )
        )

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# 质量校验（弱 LLM 兜底）
# ─────────────────────────────────────────────────────────────────────────────

# 模糊词白名单：这些词虽然看似模糊，但在特定上下文中是可验证的
_VAGUE_WORDS_WHITELIST = {
    "自检通过", "测试通过", "验证通过", "正常输出", "正常运行",
    "无报错", "无异常", "无错误", "符合预期", "满足要求",
    # task-e023-vague-whitelist：可验证连词（精确子串，不可能误伤裸"正常"）
    "正常仓库", "健康仓库", "正常执行", "正常报", "main 正常",
}

# 模糊词检测列表
_VAGUE_WORDS = [
    "应该能", "合理地", "适当", "正常", "充分", "足够",
    "良好的", "优雅的", "健壮的", "高效的",
]

# 跨平台 basetemp 模板（task-cross-platform-release / task-cross-platform-validation）
# 项目自 Windows 迁至 macOS，专用于跨平台校验提示文案。
_CROSS_PLATFORM_BASETEMP = '--basetemp="${TMPDIR:-/tmp}/orchd-vf-$$"'

# 文档后缀白名单（与 onboard.py _DOC_SINGLE_STAGE_SUFFIXES 对齐）：
# files_to_edit 全部命中这些后缀 → 视为文档/基础设施类任务（R5 维持 warning）。
# 未声明 files_to_edit 或含任意代码文件 → 视为代码类（R5 缺 verify_command 升级阻断）。
_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt")

# R4 粒度锚点（与 SKILL.md 任务拆解粒度启发式一致）
_GRANULARITY_MAX_FILES = 5
_GRANULARITY_MAX_HOURS = 8
_GRANULARITY_MAX_AC = 6


def _is_doc_task(t: dict) -> bool:
    """判定任务是否为文档/基础设施类（files_to_edit 全部为文档后缀）。

    与 onboard._is_doc_single_stage 不同的是：此处不校验 blocked 约定文件集合，
    仅按 files_to_edit 后缀白名单判定——空 files_to_edit 视为非文档（代码类），
    避免漏校验。
    """
    files_edit = [f for f in (t.get("files_to_edit") or []) if isinstance(f, str)]
    if not files_edit:
        return False
    return all(f.lower().endswith(_DOC_SUFFIXES) for f in files_edit)


def is_code_task(t: dict) -> bool:
    """判定任务是否为代码类（非文档/基础设施类）。

    R5（task-constraint-quality-checks）：代码类任务缺 verify_command 在注册点阻断，
    文档/基础设施类维持 warning。供 split.py amend 阻断判据复用，避免后缀白名单漂移。
    """
    return not _is_doc_task(t)


def validate_quality(master: Master) -> list[ValidationError]:
    """任务定义质量校验（弱 LLM 兜底）。

    加法式校验：不改变 validate_structure / validate_references 的既有行为，
    仅在它们之后追加质量层面的告警。

    校验项：
    1. E022 —— verify_command 缺失（warning；代码类任务在 amend 注册点按
       ``_is_doc_task`` 判定阻断，文档/基础设施类仅 warning）。
    2. E023 —— acceptance_criteria 含模糊词（warning），白名单豁免常见可验证语义。

    Returns:
        ValidationError 列表（E022/E023/E024/E026/E027…）；合法时返回空列表。
    """
    errors: list[ValidationError] = []
    tasks = master.tasks

    for i, t in enumerate(tasks):
        tid = t.get("id", "")

        # E022: verify_command 必填
        # R5（task-constraint-quality-checks）：缺 verify_command 是否阻断，取决于任务类型。
        #   代码类（files_to_edit 含非文档文件）→ amend 注册点阻断；
        #   文档/基础设施类（全部文档后缀）→ 维持 warning。
        verify_cmd = t.get("verify_command")
        if not verify_cmd or (isinstance(verify_cmd, str) and not verify_cmd.strip()):
            is_code = not _is_doc_task(t)
            errors.append(
                ValidationError(
                    code=ErrorCode.E022,
                    path=f"$.tasks[{i}].verify_command",
                    message=(
                        f"task '{tid}' missing verify_command (required for automated validation)"
                        + ("" if not is_code else "；代码类任务缺 verify_command，注册被阻断")
                    ),
                )
            )

        # R4（task-constraint-quality-checks）：任务拆解粒度启发式越界（warning 级）。
        # 越界即提示拆分，不做注册阻断（触碰 §9.2 内容域，硬阻断留待人工决策）。
        files_edit = [f for f in (t.get("files_to_edit") or []) if isinstance(f, str)]
        if len(files_edit) > _GRANULARITY_MAX_FILES:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].files_to_edit",
                    message=(
                        f"task '{tid}' files_to_edit 数量 {len(files_edit)} 超过粒度锚点 "
                        f"{_GRANULARITY_MAX_FILES}（建议拆分，warning 不阻断）"
                    ),
                )
            )
        est_hours = t.get("estimated_hours")
        if isinstance(est_hours, (int, float)) and est_hours > _GRANULARITY_MAX_HOURS:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].estimated_hours",
                    message=(
                        f"task '{tid}' estimated_hours {est_hours} 超过粒度锚点 "
                        f"{_GRANULARITY_MAX_HOURS}（建议拆分，warning 不阻断）"
                    ),
                )
            )
        ac_list2 = [a for a in (t.get("acceptance_criteria") or []) if isinstance(a, str)]
        if len(ac_list2) > _GRANULARITY_MAX_AC:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].acceptance_criteria",
                    message=(
                        f"task '{tid}' acceptance_criteria 数量 {len(ac_list2)} 超过粒度锚点 "
                        f"{_GRANULARITY_MAX_AC}（建议拆分，warning 不阻断）"
                    ),
                )
            )

        # E023: acceptance_criteria 模糊词检测
        ac_list = t.get("acceptance_criteria", [])
        for j, ac in enumerate(ac_list):
            if not isinstance(ac, str):
                continue
            # 检查白名单豁免
            if any(phrase in ac for phrase in _VAGUE_WORDS_WHITELIST):
                continue
            # 检查模糊词
            for vague in _VAGUE_WORDS:
                if vague in ac:
                    errors.append(
                        ValidationError(
                            code=ErrorCode.E023,
                            path=f"$.tasks[{i}].acceptance_criteria[{j}]",
                            message=f"task '{tid}' acceptance_criteria[{j}] contains vague term '{vague}' (use quantifiable criteria)",
                        )
                    )
                    break  # 一条 AC 只报一次

        # E024: verify_command 含 pytest 但缺 --basetemp（沙箱坑，warning）
        # 2026-08-06 实踩 3 例：pytest 默认落 C:\Temp 触发 SAFE_DELETE_BULK_CONFIRM_REQUIRED → E014
        # 2026-08-08 精确化：仅匹配"真正执行 pytest 子进程"的命令段
        # （python -m pytest / pytest 命令行），python -c 内容断言（字符串含
        # pytest 字样但不跑 pytest）不再命中。
        if verify_cmd and _runs_pytest(verify_cmd) and "--basetemp" not in verify_cmd:
            errors.append(
                ValidationError(
                    code=ErrorCode.E024,
                    path=f"$.tasks[{i}].verify_command",
                    message=(
                        f"task '{tid}' verify_command 含 pytest 但缺 --basetemp"
                        "（pytest 默认落系统 Temp 触发沙箱拦截 → done E014；"
                        "按 SKILL.md 自检约定加 --basetemp=\"${TMPDIR:-/tmp}/orchd-vf-$$\"）"
                    ),
                )
            )

        # E027: verify_command 不安全/不兼容（warning，amend 注册点阻断）
        # 2026-08-08 实踩 task-release-pipeline 三类：
        #   a) cmd 不兼容分隔符（; 或 2>&1;）——Windows cmd shell=True 下 ; 非分隔符
        #   b) 重命令段（python -m build / pip install / venv / 全量 pytest 无 -k/-p）
        #      ——引擎 verify 上限 120s，build+venv 段实测 144.7s 超时
        #   c) 嵌套 python -c "..."——JSON→cmd→shell 三层转义易失效 SyntaxError
        if verify_cmd:
            unsafe_reasons = _verify_unsafe_reasons(verify_cmd)
            # 2026-08-12（task-cross-platform-validation）：--basetemp 路径平台性校验。
            # 与 E027 同源（不安全/不兼容），计入 unsafe_reasons 一并上报。
            unsafe_reasons += _basetemp_platform_issues(verify_cmd)
            if unsafe_reasons:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E027,
                        path=f"$.tasks[{i}].verify_command",
                        message=(
                            f"task '{tid}' verify_command 含不安全/不兼容段"
                            f"（{'；'.join(unsafe_reasons)}）"
                        ),
                    )
                )

        # E026: 引擎源码变更但对应测试未声明（warning，intake 期预警）
        # 2026-08-08 实踩：errors.py 新增错误码必然连带 tests/test_errors.py 计数断言，
        # 但该文件不在 files_to_edit 被 E020 拦截——声明 exempt_files 或加入 files_to_edit 即消除。
        files_edit = [f for f in (t.get("files_to_edit") or []) if isinstance(f, str)]
        exempts = [f for f in (t.get("exempt_files") or []) if isinstance(f, str)]
        for fe in files_edit:
            # 匹配 orchd/X.py → 对应 tests/test_X.py
            if fe.startswith("orchd/") and fe.endswith(".py"):
                stem = fe[len("orchd/"):-3]
                expect_test = f"tests/test_{stem}.py"
                if (
                    expect_test not in files_edit
                    and expect_test not in exempts
                    and any(f.startswith("tests/") for f in files_edit)
                ):
                    errors.append(
                        ValidationError(
                            code=ErrorCode.E026,
                            path=f"$.tasks[{i}].exempt_files",
                            message=(
                                f"task '{tid}' 修改 {fe} 但对应测试 {expect_test} 未在 "
                                "files_to_edit 或 exempt_files 声明（必要连带文件须声明，"
                                "否则 E020 hook 会拦截）"
                            ),
                        )
                    )

    return errors


def validate_source(
    master: Master,
    project_root: Path | None = None,
) -> list[ValidationError]:
    """source 字段溯源校验（E025，加法式：不改变 validate_structure/references 行为）。

    task 可选 ``source`` 字段（``^(idea|roadmap):[a-z0-9-]+$``）声明任务来源：
    - ``idea:<id>``：引用 IDEAS.md 中 ``- id: <id>`` **精确匹配**的条目，且该条目
      ``status: pending``（已 taskified/完成/dropped 的条目不可作为新任务来源；
      日期词/标题词 ref 因无对应条目 id 必然被拒）；
    - ``roadmap:<id>``：引用 ROADMAP.md 中 ``## 版本`` 章节头包含的规划 id。

    校验规则：
    - 无 source 字段的任务直接通过（向后兼容存量）。
    - source 格式非法（不匹配正则）→ E025。
    - 对应文件（IDEAS.md / ROADMAP.md）缺失 → E025（文件缺失即无法溯源）。
    - idea 引用条目不存在或 status 非 pending → E025。
    - roadmap 引用章节头不包含 id → E025。

    Args:
        master: 已加载的 Master 对象。
        project_root: 项目根目录（默认取 master.source_path 的上级上级，
            即 .orchd/ 的父目录）。为 None 时基于 source_path 推导。

    Returns:
        ValidationError 列表（E025）；合法时返回空列表。
    """
    errors: list[ValidationError] = []
    tasks = master.tasks

    if project_root is None:
        project_root = master.source_path.parent.parent
    project_root = Path(project_root)

    # AC3（task-12-engine-path-abstraction）：IDEAS.md / ROADMAP.md 走统一工作区根
    # helper（默认 .orchd/，兼容旧根路径）。spec.py 依赖方向为 errors.py，此处
    # 采用函数内惰性导入 ledger 的纯路径 helper（无循环依赖：ledger 不导入 spec）。
    from orchd.ledger import resolve_workspace_root
    workspace_root = resolve_workspace_root(project_root)

    # P2-2（2026-08-19 审查）：对终态任务（completed/cancelled）豁免 source 校验。
    # ideas-archive 归档机制会把已完结的 IDEAS 条目移入 IDEAS-archive.md，终态任务的
    # source 条目必然已被归档，全量调用（未来巡检/接入）不应报 E025 误报。
    # 惰性加载 ledger 状态；拿不到（无 ledger / replay 异常）时保持全量校验（不豁免）。
    terminal_ids: set[str] = set()
    try:
        from orchd.ledger import Store
        store = Store(workspace_root)
        for _tid, _ts in store.replay().items():
            if _ts.status in ("completed", "cancelled"):
                terminal_ids.add(_tid)
    except Exception:
        terminal_ids = set()

    for i, t in enumerate(tasks):
        tid = t.get("id", "")
        source = t.get("source")
        if not source or not isinstance(source, str):
            continue
        if tid in terminal_ids:
            # P2-2：终态任务（completed/cancelled）已关闭，来源条目归档属正常生命周期
            continue

        prefix, _, ref_id = source.partition(":")
        ref_id = ref_id.strip()

        # 格式校验（正则已在 schema 层，但 validate_source 独立可调用时也要保证）
        import re as _re
        if not _re.fullmatch(r"(idea|roadmap):[a-z0-9-]+", source):
            errors.append(
                ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].source",
                    message=(
                        f"task '{tid}' source '{source}' 格式非法"
                        "（须 ^(idea|roadmap):[a-z0-9-]+$）"
                    ),
                )
            )
            continue

        if prefix == "idea":
            ideas_path = workspace_root / "IDEAS.md"
            if not ideas_path.exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E025,
                        path=f"$.tasks[{i}].source",
                        message=f"task '{tid}' 引用 IDEAS.md 但文件缺失（无法溯源）",
                    )
                )
                continue
            errors.extend(_check_idea_reference(tid, i, ref_id, ideas_path))
        elif prefix == "roadmap":
            roadmap_path = workspace_root / "ROADMAP.md"
            if not roadmap_path.exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E025,
                        path=f"$.tasks[{i}].source",
                        message=f"task '{tid}' 引用 ROADMAP.md 但文件缺失（无法溯源）",
                    )
                )
                continue
            errors.extend(_check_roadmap_reference(tid, i, ref_id, roadmap_path))

    return errors


def _exact_ref_match(ref_id: str, title: str) -> bool:
    """ref_id 是否为 title 中的一个完整词（子串/前缀误命中防护，P3 2026-08-13）。

    匹配规则：ref_id 在标题中出现，且前后边界均为「非字母数字/连字符/下划线」
    （开头/结尾视为合法边界），避免 ``2026-08-1`` 误命中 ``2026-08-10``。
    支持 ref_id 出现在标题任意位置（roadmap id 形如 ``id: snapshotstore-m-p0``）。
    """
    start = 0
    while True:
        pos = title.find(ref_id, start)
        if pos == -1:
            return False
        before_ok = pos == 0 or (
            not title[pos - 1].isalnum() and title[pos - 1] not in ("-", "_")
        )
        after = pos + len(ref_id)
        after_ok = after == len(title) or (
            not title[after].isalnum() and title[after] not in ("-", "_")
        )
        if before_ok and after_ok:
            return True
        start = pos + 1


def _check_idea_reference(
    tid: str, task_idx: int, ref_id: str, ideas_path: Path
) -> list[ValidationError]:
    """核对 IDEAS.md：存在 ``- id: <ref_id>`` 精确匹配的条目且 status 为 pending。

    ideas-archive-exact-match（2026-08-22）：idea ref 只匹配条目 ``- id: == ref``
    （精确相等），废除标题完整词匹配——日期词 ref（如 ``idea:2026-08-22``）因无
    对应条目 id 必然被拒，从数据模型杜绝同日条目标题词误命中。

    idea-write-gate（2026-08-15）：status 仅 ``pending`` 可作为任务来源；``study``
    （论证中，idea propose 写入）不可作为任务来源——须先 confirm 升 pending 才能引用。
    """
    errors: list[ValidationError] = []
    text = ideas_path.read_text(encoding="utf-8")
    # 解析条目：## 标题行 + 后续行中的 status / id 字段（支持列表与裸两种格式）
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current:
                entries.append(current)
            current = {"title": stripped[3:].strip(), "status": "", "id": ""}
        elif current is not None:
            # 匹配 "- status: pending" 或 "status: pending"
            for marker in ("- status:", "status:"):
                if stripped.startswith(marker):
                    current["status"] = stripped[len(marker):].strip()
                    break
            # 匹配 "- id: <slug>" 或 "id: <slug>"（显式 id 强约束锚点）
            for marker in ("- id:", "id:"):
                if stripped.startswith(marker):
                    current["id"] = stripped[len(marker):].strip()
                    break
    if current:
        entries.append(current)

    # ideas-archive-exact-match：只按条目 `- id:` 精确匹配（废除标题完整词匹配）
    matched = next(
        (e for e in entries if (e.get("id") or "").strip() == ref_id),
        None,
    )
    if matched is None:
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=f"task '{tid}' 引用 idea '{ref_id}' 但 IDEAS.md 中无匹配条目（- id: == {ref_id}）",
            )
        )
        return errors
    if matched["status"] != "pending":
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=(
                    f"task '{tid}' 引用 idea '{ref_id}' 但该条目 status='{matched['status']}'"
                    "（须为 pending 才能作为新任务来源）"
                ),
            )
        )
    return errors


def _check_roadmap_reference(
    tid: str, task_idx: int, ref_id: str, roadmap_path: Path
) -> list[ValidationError]:
    """核对 ROADMAP.md：存在 ``## 版本`` 章节头且包含引用 id。"""
    errors: list[ValidationError] = []
    text = roadmap_path.read_text(encoding="utf-8")
    section_headers = [
        line.strip()[3:].strip()
        for line in text.splitlines()
        if line.strip().startswith("## ")
    ]
    # P3（2026-08-13 full-audit-v2）：精确匹配（完整词），避免前缀误命中
    matched = any(_exact_ref_match(ref_id, header) for header in section_headers)
    if not matched:
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=(
                    f"task '{tid}' 引用 roadmap '{ref_id}' 但 ROADMAP.md 的"
                    "## 版本 章节头均不包含该 id"
                ),
            )
        )
    return errors


def _parse_roadmap_sections(text: str) -> list[dict[str, Any]]:
    """解析 ROADMAP.md 的 ``## 版本 · 标题（id: xxx）`` 章节头（roadmap-land / validate 兜底复用）。

    Returns:
        [{version, header, id, historical}]：version 为章节头首个词（如 ``1.3``）；
        id 为 ``id: xxx`` 内的 id（无则 None）；historical 为标题含"历史"。
    """
    import re as _re

    sections: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        header = stripped[3:].strip()
        tokens = header.split()
        version = tokens[0] if tokens else ""
        id_m = _re.search(r"id:\s*([\w-]+)", header)
        sections.append({
            "version": version,
            "header": header,
            "id": id_m.group(1) if id_m else None,
            "historical": "历史" in header,
        })
    return sections


def _find_workspace_file(orchd_dir: Path, name: str) -> Path | None:
    """在 .orchd 布局先、根布局次的顺序定位 IDEAS/ROADMAP（不依赖 ledger，spec 保持零依赖）。"""
    in_orchd = orchd_dir / name
    if in_orchd.is_file():
        return in_orchd
    root = orchd_dir.parent / name
    return root if root.is_file() else None


def roadmap_landing_warnings(orchd_dir: Path) -> list[dict[str, Any]]:
    """validate 落地兜底（intake-dual-path）：带 id 且非历史的规划章节须有 IDEAS 落地条目。

    IDEAS 落地判据：IDEAS.md 存在引用该章节的条目（detail 含 ``§版本``）。缺失 → warning
    （不判 invalid，对齐 E022/E023/E024 质量告警语义）。ROADMAP.md 缺失时返回空（跳过）。
    """
    roadmap = _find_workspace_file(orchd_dir, "ROADMAP.md")
    if roadmap is None:
        return []
    ideas = _find_workspace_file(orchd_dir, "IDEAS.md")
    ideas_text = ideas.read_text(encoding="utf-8") if ideas is not None else ""
    warnings: list[dict[str, Any]] = []
    for sec in _parse_roadmap_sections(roadmap.read_text(encoding="utf-8")):
        if sec["historical"] or not sec["id"]:
            continue
        if f"§{sec['version']}" in ideas_text:
            continue
        warnings.append({
            "code": "E031",
            "path": f"roadmap §{sec['version']}",
            "message": (
                f"规划章节 ROADMAP §{sec['version']}（id: {sec['id']}）尚无 IDEAS 落地条目："
                "IDEAS.md 缺引用该章节的 detail；可运行 `orchd roadmap-land <版本>` 落地"
            ),
        })
    return warnings


def layout_marker_warnings(project_root: Path) -> list[dict[str, Any]]:
    """validate 布局标记校验（task-14-worktree-layout，AC2）。

    布局标记（``.orchd/.layout.json``）缺失或主工作树不一致时返回告警
    （不判 invalid，对齐 E031 告警语义），并附自动探测结果——不静默跑错目录。
    标记存在且有效 → 空列表。
    """
    from orchd.worktree import detect_layout

    project_root = Path(project_root)
    layout = detect_layout(project_root)
    warnings: list[dict[str, Any]] = []
    for msg in layout.get("warnings", []):
        warnings.append({
            "code": "LAYOUT",
            "path": ".orchd/.layout.json",
            "message": msg,
        })
    return warnings


def _runs_pytest(verify_cmd: str) -> bool:
    """判定 verify_command 是否真正执行 pytest 子进程。

    匹配 `python -m pytest` 或独立 `pytest` 命令行（非 python -c 内容断言）：
    - "python -m pytest tests/" → True
    - 'python -c "import pytest; ..."'（字符串含 pytest 但不跑 pytest）→ False
    """
    import re as _re
    # 排除 python -c "..." 内容断言（内容里含 pytest 字样不视为执行 pytest）
    if _re.search(r"python\s+-c\s+[\"']", verify_cmd):
        return False
    return bool(_re.search(r"(?:python\s+-m\s+)?pytest\b", verify_cmd))


def _verify_unsafe_reasons(verify_cmd: str) -> list[str]:
    """检测 verify_command 的不安全/不兼容段（E027，2026-08-08 实踩）。

    Returns:
        命中原因列表；无命中返回空列表。
    """
    import re as _re
    reasons: list[str] = []

    # a) cmd 不兼容分隔符：半角 ;（Windows cmd shell=True 下非命令分隔符）。
    #    排除引号内的 ;（如 python -c "a; b" 是合法 Python 语句）。
    stripped_cmd = _re.sub(r"([\"'])(.*?)\1", "", verify_cmd, flags=_re.DOTALL)
    if _re.search(r";\s*$", stripped_cmd) or _re.search(r"2>&1;", stripped_cmd) \
            or _re.search(r"(?:^|[^&|;])\s*;\s", stripped_cmd):
        reasons.append("含 cmd 不兼容分隔符 ;（Windows shell=True 下 ; 非命令分隔符）")

    # b) 重命令段：python -m build / pip install / venv / 全量 pytest 无 -k/-p 定向
    if _re.search(r"python\s+-m\s+build\b", verify_cmd) \
            or "pip install" in verify_cmd or "venv" in verify_cmd:
        reasons.append("含重命令段（build/pip install/venv），引擎 verify 120s 上限易超时")
    if _runs_pytest(verify_cmd) and not _re.search(r"-[kp]\b", verify_cmd) \
            and "tests/" in verify_cmd and not _re.search(r"tests/test_\w+\.py", verify_cmd):
        reasons.append("全量 pytest 无 -k/-p 定向，累计耗时超 120s 引擎上限")

    # c) 嵌套 python -c "..."：JSON→cmd→shell 三层转义易失效。
    #    仅匹配"内容再含引号"的多层嵌套（如 python -c "... python -c ..."），
    #    简单断言 python -c "exit(0)" 是合法用法不命中。
    m = _re.search(r"python\s+-c\s+([\"'])(.*?)\1", verify_cmd, _re.DOTALL)
    if m and _re.search(r"[\"']", m.group(2)):
        reasons.append("含嵌套 python -c 引号（JSON→cmd→shell 三层转义易失效）")

    # d) P1-4 安全加固：shell 注入构式（命令替换/管道/命令链/重定向/危险命令）
    reasons += _dangerous_shell_reasons(verify_cmd)

    return reasons


def _dangerous_shell_reasons(verify_cmd: str) -> list[str]:
    """检测 verify_command 中可用于 shell 注入的构式（P1-4）。

    合法 verify 只应含 pytest / python -c / exit 等；命中以下构式即视为不可信：
    命令替换、管道、命令链、重定向、危险外部命令。注册期 E027 warning，
    执行期（done / amend dry-run）硬阻断。

    Returns:
        命中原因列表；无命中返回空列表。
    """
    import re as _re
    reasons: list[str] = []
    # 剥离引号内容后再查（合法 python -c "..." 内容里的 ; 等不参与构式判定；
    # 但 $()/` 在引号外出现仍属 shell 语义）
    stripped = _re.sub(r"([\"'])(.*?)\1", "", verify_cmd, flags=_re.DOTALL)
    # 命令替换 / 反引号：任意代码执行，合法 verify 从不使用
    if _re.search(r"\$\(|`", stripped):
        reasons.append("含命令替换 $(...) 或反引号")
    # sh/bash -c：执行任意命令串。不拦 bash -n（语法检查）与 .sh 后缀（合法）
    if _re.search(r"\b(?:sh|bash)\s+-c\b", stripped):
        reasons.append("含 sh/bash -c 任意命令执行")
    # 危险外部命令：任意系统副作用 / 网络外联
    # 注：不拦截管道 | 与重定向 >/< —— 现有 master 合法使用（>/dev/null、| grep），
    # 其后的恶意命令由本清单（curl/wget/rm 等）覆盖。
    for bad in ("rm", "curl", "wget", "nc", "chmod", "chown", "reboot", "shutdown", "mkfs", "dd"):
        if _re.search(rf"\b{_re.escape(bad)}\b", stripped, _re.IGNORECASE):
            reasons.append(f"含危险命令 {bad}")
            break
    return reasons


def verify_command_dangerous_reasons(verify_cmd: str) -> list[str]:
    """公开入口（P1-4）：返回 verify_command 的 shell 注入风险原因，供执行点硬阻断。"""
    return _dangerous_shell_reasons(verify_cmd)


def _basetemp_platform_issues(verify_cmd: str) -> list[str]:
    """检测 verify_command 中 --basetemp 路径的平台性（E027，2026-08-12 实踩）。

    跨平台 basetemp 应使用 `${TMPDIR:-/tmp}/orchd-vf-$$`（POSIX 展开 + 回退）。
    命中两类平台专用片段即判定为非跨平台：
    - Windows 专用：`%LOCALAPPDATA%` / `%TEMP%` / `%RANDOM%` / 反斜杠路径（`\\Temp` 式）
    - POSIX 专用：`${TMPDIR`（无 `:-` 回退） / `$(mktemp`

    Returns:
        命中原因列表；无 basetemp 或路径跨平台时返回空列表。
    """
    import re as _re
    basetemp_m = _re.search(r'--basetemp\s*=\s*"?([^"\s]+)', verify_cmd)
    if not basetemp_m:
        return []
    basetemp = basetemp_m.group(1)

    reasons: list[str] = []
    # Windows 专用片段
    windows_pat = r"%LOCALAPPDATA%|%TEMP%|%RANDOM%|\\\\|\\Temp"
    if _re.search(windows_pat, basetemp):
        reasons.append(
            "basetemp 路径含 Windows 专用片段"
            f"（{basetemp}），非跨平台——应改 {_CROSS_PLATFORM_BASETEMP}"
        )
    # POSIX 专用：${TMPDIR 无 :- 回退（跨平台模板 ${TMPDIR:-/tmp} 是针对的例外）
    if _re.search(r"\$\{TMPDIR(?![^}]*:-)", basetemp) or "$(mktemp" in basetemp:
        reasons.append(
            "basetemp 路径含 POSIX 专用片段"
            f"（{basetemp}），非跨平台——应改 {_CROSS_PLATFORM_BASETEMP}"
        )
    return reasons

