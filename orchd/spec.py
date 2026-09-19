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
import re
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

# ------------------------------------------------------------------
# 连带文件自动登记（task-decl-concession-autoregister）：白名单单一来源
# ------------------------------------------------------------------
# E026 推导（engine → 对应测试）与 done 越界分诊（guards.py）共用本函数——
# 禁止在 guards.py 另写一份推导，防止双写漂移。白名单必须窄且单一来源
# （红线 #3「禁改范围外文件」护栏不下降）：仅两类自动登记——
#   ① 同名测试：tests/test_<stem>.py（对应 files_to_edit 中 orchd/<stem>.py）
#   ② docs/*.md（文档类连带）
# 其余越界文件一律高风险类，仍 E010 拒绝、需显式确认。


def derive_related_test_file(fe: str,
                             tests_root: str | Path | None = None
                             ) -> str | None:
    """从引擎源码文件推导对应测试文件（单一来源，E026 与 done 分诊共用）。

    ``orchd/<stem>.py`` → ``tests/test_<stem>.py``；嵌套路径（``orchd/a/b/x.py``）
    按仓库实际命名约定拍平推导——候选为 ``tests/test_<一级目录>_<stem>.py`` 与
    ``tests/test_<stem>.py``（如 ``orchd/cli/commands/session.py`` →
    ``tests/test_cli_session.py``，``orchd/onboard/claim.py`` → ``tests/test_claim.py``）。
    非 orchd/ 源码或无对应测试返回 ``None``。传入 ``tests_root`` 时做 **tests/
    实际存在性兜底**：候选文件在 tests/ 下实际存在才返回，全部不存在返回
    ``None``（E026 对不存在的派生测试文件不产生无法满足的预警）；未传时保持
    纯字符串推导（guards.py 的 done 越界分诊以字符串比较复用本函数）。
    E026 预警与 guards.py 的 done 越界分诊**必须**调用本函数取得同名测试路径，
    禁止各自实现推导（双写漂移检测见 tests）。
    """
    if not (fe.startswith("orchd/") and fe.endswith(".py")):
        return None
    rel = fe[len("orchd/"):-3]  # 如 "errors" / "cli/commands/session"
    parts = rel.split("/")
    stem = parts[-1]
    if len(parts) == 1:
        rel_candidates = [f"test_{stem}.py"]
    else:
        rel_candidates = [
            f"test_{parts[0]}_{stem}.py",
            f"test_{stem}.py",
        ]
    if tests_root is None:
        return f"tests/{rel_candidates[0]}"
    root = Path(tests_root)
    for rel_cand in rel_candidates:
        if (root / rel_cand).is_file():
            return f"tests/{rel_cand}"
    return None


def is_concession_file(file: str, files_to_edit: list[str]) -> bool:
    """done 越界分诊白名单判定（单一来源，task-decl-concession-autoregister）。

    返回 ``True`` = 连带类（同名测试 / docs/*.md）→ 引擎自动登记、done 不阻断；
    ``False`` = 高风险类 → 仍 E010 拒绝并需显式确认。白名单判定规则：
      - 同名测试：``tests/test_<stem>.py`` 且 ``orchd/<stem>.py`` 在 files_to_edit
        中（经 :func:`derive_related_test_file`，与 E026 同一推导）；
      - 文档：``docs/*.md``。
    引擎核心 ``orchd/`` 既有文件、约定文件（``.orchd/SKILL.md`` /
    ``.orchd/shared/conventions.md``）、``.orchd/_master.json``、他人声明或
    在途文件一律不在此列（高风险类）。
    """
    if file.startswith("docs/") and file.endswith(".md"):
        return True
    for fe in files_to_edit:
        if derive_related_test_file(fe) == file:
            return True
    return False


def is_path_covered(declared: str, target: str) -> bool:
    """目录式声明覆盖判定（task-decl-dir-notation-guard AC2/AC5）。

    目录式声明（``orchd/cli/``）覆盖其下所有文件（``orchd/cli/foo.py``），
    但**不误覆盖同前缀兄弟目录**（``orchd/cli/`` 不覆盖 ``orchd/cli_extra.py``）。

    判定逻辑：尾斜杠归一后，精确相等直接命中；目录式声明要求 target 以
    ``declared + "/"`` 为前缀（归一后的 declared 不含尾斜杠，加 ``/`` 确保
    目录边界，避免 ``orchd/cli`` 前缀误命中 ``orchd/cli_extra.py``）。
    非目录式声明仅精确相等命中。

    Args:
        declared: 声明路径（如 ``orchd/cli/`` / ``orchd/spec.py``）。
        target: 待判定的目标文件路径（如 ``orchd/cli/foo.py``）。

    Returns:
        True 表示 target 被 declared 覆盖。
    """
    if not isinstance(declared, str) or not isinstance(target, str):
        return False
    d = declared.rstrip("/")
    t = target.rstrip("/")
    if d == t:
        return True
    # 目录式声明：declared 原本以 / 结尾，或归一后 target 以 declared/ 开头
    if declared.endswith("/") and t.startswith(d + "/"):
        return True
    return False


def detect_dir_or_glob_declarations(
        task: dict[str, Any]) -> list[dict[str, str]]:
    """检出任务声明中的目录式/通配符路径（task-decl-dir-notation-guard AC1/AC3）。

    扫描 ``files_to_edit`` 与 ``exempt_files``，返回命中清单。判定：
    - **目录式**：路径以 ``/`` 结尾，或对应路径在项目中实际为目录（``Path.is_dir()``）；
    - **通配符**：路径含 ``*`` 或 ``?``。

    与 :func:`validate_quality` 同源不双写——本函数是唯一检出原语，amend 注册
    门禁与消费点前缀匹配均须复用，禁止各自实现判定。

    Args:
        task: 任务定义 dict。

    Returns:
        ``[{"field", "path", "kind"}]``，kind 为 ``"directory"`` 或 ``"glob"``；
        无命中返回空列表。
    """
    hits: list[dict[str, str]] = []
    for decl_field in ("files_to_edit", "exempt_files"):
        for fp in task.get(decl_field, []) or []:
            if not isinstance(fp, str):
                continue
            if "*" in fp or "?" in fp:
                hits.append({"field": decl_field, "path": fp, "kind": "glob"})
                continue
            if fp.endswith("/"):
                hits.append({
                    "field": decl_field,
                    "path": fp,
                    "kind": "directory"
                })
                continue
            # 实际为目录（如 orchd/cli 无尾斜杠但对应目录存在）
            try:
                if Path(fp).is_dir():
                    hits.append({
                        "field": decl_field,
                        "path": fp,
                        "kind": "directory"
                    })
            except OSError:
                pass
    return hits


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
            [{
                "path": str(path),
                "message": "目标文件不存在"
            }],
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OrchdError(
            ErrorCode.E002,
            f"invalid JSON in {path}: {exc}",
            [{
                "path": str(path),
                "message": str(exc)
            }],
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
    for err in sorted(validator.iter_errors(master.raw),
                      key=lambda e: list(e.absolute_path)):
        json_path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                                  for p in err.absolute_path)
        # 从 $.tasks[i] 路径解析 task id，增强诊断信息
        tid = None
        for i, p in enumerate(err.absolute_path):
            if isinstance(p, int) and i + 1 < len(err.absolute_path):
                next_p = err.absolute_path[i + 1]
                if isinstance(next_p, str) and p < len(master.tasks):
                    tid = master.tasks[p].get("id")
                    break
        message = f"task '{tid}': {err.message}" if tid else err.message
        errors.append(
            ValidationError(
                code=ErrorCode.E003,
                path=json_path,
                message=message,
            ))
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
                ))
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
                ))
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
                ))
        # depends_on 引用
        for j, dep in enumerate(t.get("depends_on", [])):
            if dep not in task_id_set:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E005,
                        path=f"$.tasks[{i}].depends_on[{j}]",
                        message=
                        f"depends_on references unknown task_id: '{dep}'",
                    ))

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
                        message=(f"shared file not found: '{rel}'"
                                 f"（BOOTSTRAP 声明了 shared.{key} 但未写入该文件）"),
                    ))

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

    queue: deque[str] = deque(tid for tid, deg in in_degree.items()
                              if deg == 0)
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
                message=
                f"dependency cycle detected involving: {', '.join(sorted(cycle_nodes))}",
            ))

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# 质量校验（弱 LLM 兜底）
# ─────────────────────────────────────────────────────────────────────────────

# 模糊词白名单：这些词虽然看似模糊，但在特定上下文中是可验证的
_VAGUE_WORDS_WHITELIST = {
    "自检通过",
    "测试通过",
    "验证通过",
    "正常输出",
    "正常运行",
    "无报错",
    "无异常",
    "无错误",
    "符合预期",
    "满足要求",
    # task-e023-vague-whitelist：可验证连词（精确子串，不可能误伤裸"正常"）
    "正常仓库",
    "健康仓库",
    "正常执行",
    "正常报",
    "main 正常",
}

# 模糊词检测列表
_VAGUE_WORDS = [
    "应该能",
    "合理地",
    "适当",
    "正常",
    "充分",
    "足够",
    "良好的",
    "优雅的",
    "健壮的",
    "高效的",
]

# 跨平台 basetemp 模板（task-cross-platform-release / task-cross-platform-validation）
# 项目自 Windows 迁至 macOS，专用于跨平台校验提示文案。
# 执行模型（task-cross-platform-verify）：verify_command 在 Windows 上经 Git Bash
# （orchd.subproc.run_shell）执行，${TMPDIR:-/tmp} 由 bash 展开 → 真正跨平台。
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
    files_edit = [
        f for f in (t.get("files_to_edit") or []) if isinstance(f, str)
    ]
    if not files_edit:
        return False
    return all(f.lower().endswith(_DOC_SUFFIXES) for f in files_edit)


def is_code_task(t: dict) -> bool:
    """判定任务是否为代码类（非文档/基础设施类）。

    R5（task-constraint-quality-checks）：代码类任务缺 verify_command 在注册点阻断，
    文档/基础设施类维持 warning。供 split.py amend 阻断判据复用，避免后缀白名单漂移。
    """
    return not _is_doc_task(t)


def _tests_root_from_master(master: Master) -> Path | None:
    """从 _master.json 所在位置推导 tests/ 目录，不存在返回 None（E026 存在性兜底）。"""
    tests = _project_root_from_master(master) / "tests"
    return tests if tests.is_dir() else None


# ------------------------------------------------------------------
# 声明口径一致性校验（task-intake-decl-consistency-gate，2026-09-17）
# ------------------------------------------------------------------
# why：撰稿人手写的 brief / verify_command 与机器声明、仓库实际三者之间此前无一致性
# 校验，漂移只能等执行期由 agent 撞上——实测三例：verify_command 引用
# tests/test_precommit.py 与 tests/test_intake.py（两者均不存在，只能 amend 现场修正）；
# files_to_edit 声明当时不存在的 tests/test_session.py（被迫新建文件以满足 E010）。
#
# 错误码归属（task-decl-consistency-error-code 已落地专用码）：
# 校验① → E037（verify_reference_drift，阻断级，终态豁免）
# 校验② → E038（brief_decl_count_mismatch，warning 级，终态豁免）
# E027 回归原语义（verify_command 不安全/不兼容段），E029 回归原语义（粒度越界/建议拆分）。
# 历史背景：task-intake-decl-consistency-gate 交付时因声明边界复用 E027/E029，
# 导致 guidance 文案串味（「建议拆分」用于数量口径不一致）、按码统计无法区分
# 「命令不安全」与「声明口径漂移」。本任务拆出专用码并同步 errors.py / guide.py /
# TERMINAL_EXEMPT_QUALITY_CODES，存量 387 条任务零新增误报（14 例历史命中均为终态豁免）。
_DECL_COUNT_RE = re.compile(r"files_to_edit\s*[）)」\"']?\s*控\s*(\d+)")
# verify_command 中「仓库内相对路径」识别后缀白名单：只认带明确文件后缀的 token，
# 宁可漏判不可误判（目录目标如 `ruff check orchd/`、选项 `-q` 均不参与判定）。
_VERIFY_PATH_SUFFIXES = (
    ".py",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".sh",
    ".ps1",
)
# 含以下字符的 token 不做路径解析：shell 元字符（$ ` | > < ( ) & ; * ? { } [ ]）、
# 反斜杠（Windows 路径形态）与冒号（env 展开 / 盘符）。
_VERIFY_PATH_SKIP_CHARS = frozenset("$`|><()&;*?{}[]\\:")


def _project_root_from_master(master: Master) -> Path:
    """从 _master.json 位置推导项目根（声明路径的存在性基准）。

    .orchd/ 布局（``<root>/.orchd/_master.json``）→ 项目根为 source_path.parent.parent；
    根布局（``<root>/_master.json``）→ 项目根为 source_path.parent。
    """
    src = master.source_path
    return src.parent.parent if src.parent.name == ".orchd" else src.parent


def _verify_command_path_tokens(verify_cmd: str) -> list[str]:
    """抽取 verify_command 中「按仓库根解析」的路径 token（保守判定）。

    跳过：以 ``-`` 开头（选项）、以 ``/`` 开头（绝对路径如 /dev/null）、无已知文件
    后缀（如 ruff 的目标目录 ``orchd/``）、含 shell 元字符 / 反斜杠 / 冒号的 token。
    """
    out: list[str] = []
    for raw in verify_cmd.split():
        tok = raw.strip("\"'")
        if not tok or tok.startswith("-") or tok.startswith("/"):
            continue
        if not tok.endswith(_VERIFY_PATH_SUFFIXES):
            continue
        if any(ch in _VERIFY_PATH_SKIP_CHARS for ch in tok):
            continue
        out.append(tok)
    return out


def validate_quality(master: Master) -> list[ValidationError]:
    """任务定义质量校验（弱 LLM 兜底）。

    加法式校验：不改变 validate_structure / validate_references 的既有行为，
    仅在它们之后追加质量层面的告警。

    校验项：
    1. E022 —— verify_command 缺失（warning；代码类任务在 amend 注册点按
       ``_is_doc_task`` 判定阻断，文档/基础设施类仅 warning）。
    2. E023 —— acceptance_criteria 含模糊词（warning），白名单豁免常见可验证语义。
    3. E037 —— verify_command 引用的仓库内路径既未声明（files_to_edit /
       exempt_files）又不存在 → 阻断级：声明口径一致性（task-decl-consistency-error-code；
       终态任务由调用方按 TERMINAL_EXEMPT_QUALITY_CODES 豁免）。
    4. E038 —— brief 的「files_to_edit 控 N」与实际声明数不一致 → warning
       （同任务；**非拆分建议**，专用码从 E029 拆出）。
    5. E024 / E026 / E029 其余项 —— 见各代码块内注释（basetemp / 测试连带 / 粒度锚点）。

    Returns:
        ValidationError 列表（E022/E023/E024/E026/E027/E029/E037/E038…）；合法时返回空列表。
    """
    errors: list[ValidationError] = []
    tasks = master.tasks
    # 声明路径存在性的解析基准（校验①用）：与 E026 的 tests/ 推导同源，单一真源。
    project_root = _project_root_from_master(master)

    for i, t in enumerate(tasks):
        tid = t.get("id", "")

        # E022: verify_command 必填
        # R5（task-constraint-quality-checks）：缺 verify_command 是否阻断，取决于任务类型。
        #   代码类（files_to_edit 含非文档文件）→ amend 注册点阻断；
        #   文档/基础设施类（全部文档后缀）→ 维持 warning。
        verify_cmd = t.get("verify_command")
        if not verify_cmd or (isinstance(verify_cmd, str)
                              and not verify_cmd.strip()):
            is_code = not _is_doc_task(t)
            errors.append(
                ValidationError(
                    code=ErrorCode.E022,
                    path=f"$.tasks[{i}].verify_command",
                    message=
                    (f"task '{tid}' missing verify_command (required for automated validation)"
                     +
                     ("" if not is_code else "；代码类任务缺 verify_command，注册被阻断")),
                ))

        # R4（task-constraint-quality-checks）：任务拆解粒度启发式越界（warning 级）。
        # 越界即提示拆分，不做注册阻断（触碰 §9.2 内容域，硬阻断留待人工决策）。
        files_edit = [
            f for f in (t.get("files_to_edit") or []) if isinstance(f, str)
        ]
        if len(files_edit) > _GRANULARITY_MAX_FILES:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].files_to_edit",
                    message=
                    (f"task '{tid}' files_to_edit 数量 {len(files_edit)} 超过粒度锚点 "
                     f"{_GRANULARITY_MAX_FILES}（建议拆分，warning 不阻断）"),
                ))
        # E038（task-decl-consistency-error-code）：brief 的数量口径
        # 「files_to_edit 控 N」与实际声明数不一致 → warning（不阻断，**非拆分建议**）。
        # 专用码从 E029 拆出，避免与「粒度越界/建议拆分」语义串味。
        brief_text = t.get("brief")
        if isinstance(brief_text, str) and brief_text:
            count_m = _DECL_COUNT_RE.search(brief_text)
            if count_m and int(count_m.group(1)) != len(files_edit):
                errors.append(
                    ValidationError(
                        code=ErrorCode.E038,
                        path=f"$.tasks[{i}].brief",
                        message=(
                            f"task '{tid}' brief 声明的 files_to_edit 数量口径 "
                            f"{count_m.group(1)} 与实际声明 {len(files_edit)} 不一致"
                            "（warning 不阻断；此处非拆分建议：请对齐 brief 文案与声明集合）"),
                    ))

        est_hours = t.get("estimated_hours")
        if isinstance(est_hours,
                      (int, float)) and est_hours > _GRANULARITY_MAX_HOURS:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].estimated_hours",
                    message=(
                        f"task '{tid}' estimated_hours {est_hours} 超过粒度锚点 "
                        f"{_GRANULARITY_MAX_HOURS}（建议拆分，warning 不阻断）"),
                ))
        ac_list2 = [
            a for a in (t.get("acceptance_criteria") or [])
            if isinstance(a, str)
        ]
        if len(ac_list2) > _GRANULARITY_MAX_AC:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].acceptance_criteria",
                    message=
                    (f"task '{tid}' acceptance_criteria 数量 {len(ac_list2)} 超过粒度锚点 "
                     f"{_GRANULARITY_MAX_AC}（建议拆分，warning 不阻断）"),
                ))

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
                            message=
                            f"task '{tid}' acceptance_criteria[{j}] contains vague term '{vague}' (use quantifiable criteria)",
                        ))
                    break  # 一条 AC 只报一次

        # E024: verify_command 含 pytest 但缺 --basetemp（沙箱坑，warning）
        # 2026-08-06 实踩 3 例：pytest 默认落 C:\Temp 触发 SAFE_DELETE_BULK_CONFIRM_REQUIRED → E014
        # 2026-08-08 精确化：仅匹配"真正执行 pytest 子进程"的命令段
        # （python -m pytest / pytest 命令行），python -c 内容断言（字符串含
        # pytest 字样但不跑 pytest）不再命中。
        if verify_cmd and _runs_pytest(
                verify_cmd) and "--basetemp" not in verify_cmd:
            errors.append(
                ValidationError(
                    code=ErrorCode.E024,
                    path=f"$.tasks[{i}].verify_command",
                    message=
                    (f"task '{tid}' verify_command 含 pytest 但缺 --basetemp"
                     "（pytest 默认落系统 Temp 触发沙箱拦截 → done E014；"
                     "按 SKILL.md 自检约定加 --basetemp=\"${TMPDIR:-/tmp}/orchd-vf-$$\"）"
                     ),
                ))

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
                        message=(f"task '{tid}' verify_command 含不安全/不兼容段"
                                 f"（{'；'.join(unsafe_reasons)}）"),
                    ))

            # E037（task-decl-consistency-error-code）：声明口径一致性——
            # verify_command 引用的仓库内路径必须 ∈ files_to_edit ∪ exempt_files，或
            # 磁盘已存在。两者皆不满足时 verify 在 done 期必然失败（引用已删除/拼错的
            # 文件），属**确定性错误**，注册点即拒。终态任务由调用方按
            # TERMINAL_EXEMPT_QUALITY_CODES 豁免。
            declared_paths = {
                f
                for f in (t.get("files_to_edit") or []) if isinstance(f, str)
            } | {
                f
                for f in (t.get("exempt_files") or []) if isinstance(f, str)
            }
            missing_refs = sorted({
                tok
                for tok in _verify_command_path_tokens(verify_cmd)
                if tok not in declared_paths and not (project_root /
                                                      tok).exists()
            })
            if missing_refs:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E037,
                        path=f"$.tasks[{i}].verify_command",
                        message=(
                            f"task '{tid}' verify_command 引用的路径既未声明、也不存在："
                            f"{missing_refs}（声明口径一致性：请将路径加入 files_to_edit / "
                            "exempt_files，或把 verify_command 改指向实际存在的文件）"),
                    ))

        # E026: 引擎源码变更但对应测试未声明（warning，intake 期预警）
        # 2026-08-08 实踩：errors.py 新增错误码必然连带 tests/test_errors.py 计数断言，
        # 但该文件不在 files_to_edit 被 E020 拦截——声明 exempt_files 或加入 files_to_edit 即消除。
        # task-decl-concession-autoregister：同名测试推导收敛到单一来源
        # derive_related_test_file（与 done 越界分诊 is_concession_file 共用，防双写漂移）。
        # task-roadmap-section-parse-fix（AC6）：存在性兜底——tests/ 下实际不存在的
        # 派生测试文件（如 orchd/gitops_ops.py → tests/test_gitops_ops.py 不存在）不产生
        # 无法满足的预警（E026 跳过）；tests/ 目录缺失时同样跳过（无从验证即不预警）。
        files_edit = [
            f for f in (t.get("files_to_edit") or []) if isinstance(f, str)
        ]
        exempts = [
            f for f in (t.get("exempt_files") or []) if isinstance(f, str)
        ]
        tests_root = _tests_root_from_master(master)
        for fe in files_edit:
            expect_test = derive_related_test_file(fe, tests_root)
            if (expect_test is not None and expect_test not in files_edit
                    and expect_test not in exempts
                    and any(f.startswith("tests/") for f in files_edit)):
                errors.append(
                    ValidationError(
                        code=ErrorCode.E026,
                        path=f"$.tasks[{i}].exempt_files",
                        message=(
                            f"task '{tid}' 修改 {fe} 但对应测试 {expect_test} 未在 "
                            "files_to_edit 或 exempt_files 声明（必要连带文件须声明，"
                            "否则 E020 hook 会拦截）"),
                    ))

    return errors


# 终态任务质量告警豁免口径（task-amend-quality-warning-terminal-filter，单一真源）：
# 质量类告警 E023/E026/E027/E029 对终态（completed/cancelled）任务一律豁免——
# 拆分/改写终态任务定义无意义（E007 终态保护无法改写）。validate 与 amend 共用；
# E022（代码类缺 verify_command 注册阻断）与 E024 不在豁免之列。
TERMINAL_EXEMPT_QUALITY_CODES = frozenset({"E023", "E026", "E027", "E029", "E037", "E038"})


def _quality_warning_code_name(w: Any) -> str:
    """质量告警条目的错误码名（兼容 ValidationError 与 dict 两种形态）。"""
    code = w.code if hasattr(w, "code") else w.get("code")
    name = code.name if hasattr(code, "name") else str(code)
    return name.split(".")[-1]


def _quality_warning_task_index(path: Any) -> int | None:
    """告警 path（$.tasks[i]…）的任务下标；非任务级路径返回 None。"""
    if not isinstance(path, str) or not path.startswith("$.tasks["):
        return None
    head, sep, _ = path[len("$.tasks["):].partition("]")
    return int(head) if sep and head.isdigit() else None


def filter_terminal_quality_warnings(
    warnings: list[Any],
    tasks: list[dict[str, Any]],
    terminal_ids: set[str] | None,
) -> tuple[list[Any], int]:
    """过滤终态任务的质量类告警（validate/amend 共用，task-amend-quality-warning-terminal-filter）。

    Args:
        warnings: ValidationError 列表或同形 dict 列表（{"code", "path"}，
            code 接受 "E026" 或 "ErrorCode.E026" 两种形态）。
        tasks: path 下标对应的任务定义列表（顺序须与判据产出一致，
            即 master.tasks 顺序）。
        terminal_ids: 终态任务 id 集合；为 None 时（ledger 不可用/replay 失败）
            不过滤（回退现行为）。

    Returns:
        (kept, exempted_count)：保留条目与本次豁免条数（调用方负责可见输出，
        不得静默吞掉计数）。
    """
    if terminal_ids is None:
        return list(warnings), 0
    kept: list[Any] = []
    exempted = 0
    for w in warnings:
        if _quality_warning_code_name(w) not in TERMINAL_EXEMPT_QUALITY_CODES:
            kept.append(w)
            continue
        path = w.path if hasattr(
            w, "path") else (w.get("path") if isinstance(w, dict) else None)
        idx = _quality_warning_task_index(path)
        tid = tasks[idx].get(
            "id") if idx is not None and 0 <= idx < len(tasks) else None
        if tid is not None and tid in terminal_ids:
            exempted += 1
            continue
        kept.append(w)
    return kept, exempted


def _validate_additional_sources(
    t: dict,
    i: int,
    tid: str,
    workspace_root,
    project_root,
) -> list["ValidationError"]:
    """additional_sources 独立遍历（task-additional-sources-standalone-validation）。

    与主 ``source`` 共享同一检查函数（``_check_idea_reference`` /
    ``_check_roadmap_reference``）与错误路径语义（``path`` 前缀仍为
    ``$.tasks[i].additional_sources[j]``、错误码 E025、消息口径与 source 一致）。

    独立性：由调用方置于主 source 短路（无 source / 非 str / 终态任务 /
    source 格式非法）**之前**执行，故四类短路路径下附加引用仍被逐条校验。
    本函数自身不做任何短路（含终态任务亦校验）。
    """
    import re as _re

    # spec.py 依赖方向为 errors.py，此处惰性导入 ledger 纯路径 helper
    # （与 validate_source 同模式，无循环依赖：ledger 不导入 spec）。
    from orchd.ledger import resolve_roadmap_path

    errors: list["ValidationError"] = []
    additional = t.get("additional_sources")
    if additional and isinstance(additional, list):
        for j, asrc in enumerate(additional):
            if not isinstance(asrc, str):
                errors.append(ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].additional_sources[{j}]",
                    message=f"task '{tid}' additional_sources[{j}] 非字符串",
                ))
                continue
            if not _re.fullmatch(r"(idea|roadmap|debug):[a-z0-9-]+", asrc):
                errors.append(ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].additional_sources[{j}]",
                    message=(f"task '{tid}' additional_sources[{j}] '{asrc}' 格式非法"
                             "（须 ^(idea|roadmap|debug):[a-z0-9-]+$）"),
                ))
                continue
            aprefix, _, aref = asrc.partition(":")
            aref = aref.strip()
            base_path = f"$.tasks[{i}].additional_sources[{j}]"
            if aprefix == "idea":
                ideas_path = workspace_root / "IDEAS.md"
                if not ideas_path.exists():
                    errors.append(ValidationError(
                        code=ErrorCode.E025,
                        path=base_path,
                        message=f"task '{tid}' additional_sources 引用 IDEAS.md 但文件缺失",
                    ))
                    continue
                for se in _check_idea_reference(tid, i, aref, ideas_path):
                    se.path = base_path
                    errors.append(se)
            elif aprefix == "roadmap":
                rpath = resolve_roadmap_path(project_root)
                if not rpath.exists():
                    errors.append(ValidationError(
                        code=ErrorCode.E025,
                        path=base_path,
                        message=f"task '{tid}' additional_sources 引用 ROADMAP.md 但文件缺失",
                    ))
                    continue
                for se in _check_roadmap_reference(tid, i, aref, rpath):
                    se.path = base_path
                    errors.append(se)
            # debug: 前缀无文件引用校验，与 source 一致
    return errors


def validate_source(
    master: Master,
    project_root: Path | None = None,
) -> list[ValidationError]:
    """source 字段溯源校验（E025，加法式：不改变 validate_structure/references 行为）。

    task 可选 ``source`` 字段（``^(idea|roadmap|debug):[a-z0-9-]+$``）声明任务来源：
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
    from orchd.ledger import resolve_roadmap_path, resolve_workspace_root
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
        # task-additional-sources-standalone-validation：附加引用独立遍历——
        # 先于主 source 的全部短路（无 source / 非 str / 终态 / 格式非法）执行，
        # 四类路径下仍逐条校验（E025 形同虚设的旁路消除）。
        errors.extend(
            _validate_additional_sources(t, i, tid, workspace_root, project_root))
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
        if not _re.fullmatch(r"(idea|roadmap|debug):[a-z0-9-]+", source):
            errors.append(
                ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].source",
                    message=(f"task '{tid}' source '{source}' 格式非法"
                             "（须 ^(idea|roadmap|debug):[a-z0-9-]+$）"),
                ))
            continue

        if prefix == "idea":
            ideas_path = workspace_root / "IDEAS.md"
            if not ideas_path.exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E025,
                        path=f"$.tasks[{i}].source",
                        message=f"task '{tid}' 引用 IDEAS.md 但文件缺失（无法溯源）",
                    ))
                continue
            errors.extend(_check_idea_reference(tid, i, ref_id, ideas_path))
        elif prefix == "roadmap":
            # task-roadmap-root-resolution：ROADMAP 走独立定位（宿主根优先、
            # .orchd/ 回退），与 roadmap-land / intake 同一份文件——此前由工作区
            # 文档根拼出（.orchd/ 下存在 IDEAS/SKILL 即锁定 .orchd/），与 validate
            # 系 _find_workspace_file 的根回退定位分裂。
            roadmap_path = resolve_roadmap_path(project_root)
            if not roadmap_path.exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E025,
                        path=f"$.tasks[{i}].source",
                        message=f"task '{tid}' 引用 ROADMAP.md 但文件缺失（无法溯源）",
                    ))
                continue
            errors.extend(
                _check_roadmap_reference(tid, i, ref_id, roadmap_path))
        # debug: 前缀标记外部来源手工注册的任务，不校验文件引用

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
        before_ok = pos == 0 or (not title[pos - 1].isalnum()
                                 and title[pos - 1] not in ("-", "_"))
        after = pos + len(ref_id)
        after_ok = after == len(title) or (not title[after].isalnum()
                                           and title[after] not in ("-", "_"))
        if before_ok and after_ok:
            return True
        start = pos + 1


def _check_idea_reference(tid: str, task_idx: int, ref_id: str,
                          ideas_path: Path) -> list[ValidationError]:
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
                message=
                f"task '{tid}' 引用 idea '{ref_id}' 但 IDEAS.md 中无匹配条目（- id: == {ref_id}）",
            ))
        return errors
    if matched["status"] != "pending":
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=
                (f"task '{tid}' 引用 idea '{ref_id}' 但该条目 status='{matched['status']}'"
                 "（须为 pending 才能作为新任务来源）"),
            ))
    return errors


def _roadmap_section_headers(text: str) -> list[str]:
    """提取 ROADMAP.md 章节头（单一来源，roadmap-ref 校验与 roadmap-land 定位共用）。

    识别 ``## `` 与 ``### `` 两级标题（ROADMAP 自结构重构后版本章节位于
    ``## 近期规划`` / ``## 远期规划`` / ``## 派生分支`` 之下、改用 ``### `` 层级，
    如 ``### 1.4.5 · 架构演进（id: arch-evolution-145）``）；header 剥掉全部
    前导 ``#`` 与空格。两份 ROADMAP 解析（_check_roadmap_reference /
    _parse_roadmap_sections）必须都经本函数取 header，禁止各自实现解析
    （对齐 shared/conventions.md:118「同一事实禁止两份解析」教训）。
    """
    headers: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            headers.append(stripped.lstrip("#").strip())
    return headers


def _check_roadmap_reference(tid: str, task_idx: int, ref_id: str,
                             roadmap_path: Path) -> list[ValidationError]:
    """核对 ROADMAP.md：存在 ``## / ### 版本`` 章节头且包含引用 id。"""
    errors: list[ValidationError] = []
    text = roadmap_path.read_text(encoding="utf-8")
    section_headers = _roadmap_section_headers(text)
    # P3（2026-08-13 full-audit-v2）：精确匹配（完整词），避免前缀误命中
    matched = any(
        _exact_ref_match(ref_id, header) for header in section_headers)
    if not matched:
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=(f"task '{tid}' 引用 roadmap '{ref_id}' 但 ROADMAP.md 的"
                         "## / ### 版本 章节头均不包含该 id"),
            ))
    return errors


def _parse_roadmap_sections(text: str) -> list[dict[str, Any]]:
    """解析 ROADMAP.md 的 ``## / ### 版本 · 标题（id: xxx）`` 章节头（roadmap-land / validate 兜底复用）。

    Returns:
        [{version, header, id, historical}]：version 为章节头首个词（如 ``1.3``）；
        id 为 ``id: xxx`` 内的 id（无则 None）；historical 为标题含"历史"。
    """
    import re as _re

    sections: list[dict[str, Any]] = []
    for header in _roadmap_section_headers(text):
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

    IDEAS 落地判据：IDEAS.md **或 IDEAS-archive.md** 存在引用该章节的条目
    （detail 含 ``§版本``）——规划章节必须曾进入执行层，或被显式标记历史；否则提醒。缺失 → warning
    （不判 invalid，对齐 E022/E023/E024 质量告警语义）。ROADMAP.md 缺失时返回空（跳过）。
    """
    # task-roadmap-root-resolution：ROADMAP 定位与其余工作区文档解耦——统一走
    # resolve_roadmap_path（宿主根优先、.orchd/ 回退），与 roadmap-land / E025
    # 溯源定位到**同一份文件**（IDEAS / IDEAS-archive 仍走 _find_workspace_file）。
    # 入参形态兼容：orchd_dir 名为 .orchd 时，项目根为其父级。
    from orchd.ledger import resolve_roadmap_path

    project_root = orchd_dir.parent if orchd_dir.name == ".orchd" else orchd_dir
    roadmap_candidate = resolve_roadmap_path(project_root)
    roadmap = roadmap_candidate if roadmap_candidate.is_file() else None
    if roadmap is None:
        return []
    ideas = _find_workspace_file(orchd_dir, "IDEAS.md")
    ideas_text = ideas.read_text(encoding="utf-8") if ideas is not None else ""
    # 判据扩展（task-roadmap-section-parse-fix）：IDEAS.md 或 IDEAS-archive.md 含 §版本
    # 均视为「已落地」——archive_resolved_ideas 会在条目全部终态后把条目移入
    # IDEAS-archive.md（先写归档、再删主文件），已落地且已实现的章节不得「回弹告警」。
    archive = _find_workspace_file(orchd_dir, "IDEAS-archive.md")
    archive_text = archive.read_text(
        encoding="utf-8") if archive is not None else ""
    warnings: list[dict[str, Any]] = []
    for sec in _parse_roadmap_sections(roadmap.read_text(encoding="utf-8")):
        if sec["historical"] or not sec["id"]:
            continue
        if f"§{sec['version']}" in ideas_text or f"§{sec['version']}" in archive_text:
            continue
        warnings.append(_e031_warning(sec))
    return warnings


def _e031_warning(sec: dict[str, Any]) -> dict[str, Any]:
    """构造单条 E031 落地告警（通道 C：经 structured_error 挂 details+guidance）。

    保留 ``{code, path, message}`` 既有键（消费方断言面不变），加法式附加
    ``details``（list 契约）与 ``guidance``（按码指引，command 指向 roadmap-land）。
    """
    message = (f"规划章节 ROADMAP §{sec['version']}（id: {sec['id']}）尚无 IDEAS 落地条目："
               "IDEAS.md 与 IDEAS-archive.md 均缺引用该章节的 detail；处置二选一——"
               "① 运行 `orchd roadmap-land <版本>` 落地为 IDEAS pending；"
               "② 若该版本已发布或已放弃，标记历史或移出 ROADMAP")
    try:
        from orchd.ledger import structured_error

        resp = structured_error(
            "E031",
            message,
            [{
                "path":
                f"roadmap §{sec['version']}",
                "hint":
                (f"处置二选一：① 运行 `orchd roadmap-land {sec['version']}` 为该规划章节"
                 "生成 IDEAS pending 落地条目（摄入协议：先落地再注册任务）；"
                 "② 若该版本已发布或已放弃，在 ROADMAP.md 将章节标题标记「历史」或移出"),
            }],
            None,
        )
        err = resp.get("error", {})
        return {
            "code": err.get("code", "E031"),
            "path": f"roadmap §{sec['version']}",
            "message": message,
            "details": err.get("details", []),
            "severity": err.get("severity", "warning"),
            "guidance": resp.get("guidance"),
        }
    except Exception:
        # 通道 C best-effort：指引挂接失败不击穿 validate 告警链
        return {
            "code": "E031",
            "path": f"roadmap §{sec['version']}",
            "message": message,
        }


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


def _strip_single_quoted_segments(verify_cmd: str) -> str:
    """按 shell 引号状态机剥离**最外层**单引号字面段，返回供扫描的「活跃文本」。

    为什么必须是状态机（task-verify-danger-quote-state-machine）：单引号只有在
    **不在双引号内**时才是定界符。正则剥除法 `re.sub(r"'[^']*'", "", cmd)`
    无法表达该状态——它会把 `echo "'$(curl … | sh)'"` 里那段「双引号内、被单引号
    包着」的文本当作字面量整体剥掉，而 bash 语义下双引号内的 `'` 只是普通字符、
    其中的 `$(…)` 照旧展开 → 判定 `danger=[]` 放行，实跑却执行替换（已实证绕过）。

    状态与保守口径（沿用 .orchd/rules/verify.md 的保守拦截纪律）：
    - 不在双引号内的 `'…'`：shell 字面量，整段剥离（无替换 / 执行语义）；
    - 双引号段：原样保留（其间 `$(…)` / 反引号 / `$var` 照旧展开，剥离即漏报）；
      双引号内的 `'` 不构成定界符，按普通字符保留（故嵌套形态同样被检出）；
    - 未闭合的单引号不剥离（其内容照旧参与判定——宁可多报，不可放过）；
    - 不做转义还原、不做引号以外的语法解释（判定只服务保守拦截）。

    Args:
        verify_cmd: 原始 verify_command 串。

    Returns:
        移除最外层单引号字面段后的串，供构式扫描使用（执行点仍用原始串）。
    """
    out: list[str] = []
    i = 0
    n = len(verify_cmd)
    in_double = False
    while i < n:
        ch = verify_cmd[i]
        if in_double:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                # 双引号内的转义序列原样保留（不解释），仅避免把 \" 误判为闭合引号
                out.append(verify_cmd[i + 1])
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            close = verify_cmd.find("'", i + 1)
            if close == -1:
                # 未闭合：保守——不剥离，原样参与判定
                out.append(ch)
                i += 1
                continue
            i = close + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _dangerous_shell_reasons(verify_cmd: str) -> list[str]:
    """检测 verify_command 中可用于 shell 注入的构式（P1-4）。

    合法 verify 只应含 pytest / python -c / exit 等；命中以下构式即视为不可信：
    命令替换、管道、命令链、重定向、危险外部命令。注册期 E027 warning，
    执行期（done / amend dry-run）硬阻断。

    判定输入为**原始串**，仅按引号状态机剥离**最外层**单引号字面段（见
    :func:`_strip_single_quoted_segments`）：执行点 orchd/onboard/lifecycle/core.py
    ::_run_verify 用原始 verify_cmd 经 shell=True 执行，双引号内 `$(...)` / 反引号 /
    `$var` 同样被 shell 展开，故不得把引号内容整体剥离后再判定（剥离会与执行点语义
    脱钩，形成绕过）；双引号内的单引号不构成定界符，同样不得据此豁免。

    Args:
        verify_cmd: 原始 verify_command 串。

    Returns:
        命中原因列表；无命中返回空列表。
    """
    import re as _re
    reasons: list[str] = []
    scanned = _strip_single_quoted_segments(verify_cmd)
    # 命令替换 / 反引号：任意代码执行，合法 verify 从不使用（双引号内同样生效）
    if _re.search(r"\$\(|`", scanned):
        reasons.append("含命令替换 $(...) 或反引号")
    # sh/bash -c：执行任意命令串。不拦 bash -n（语法检查）与 .sh 后缀（合法）
    if _re.search(r"\b(?:sh|bash)\s+-c\b", scanned):
        reasons.append("含 sh/bash -c 任意命令执行")
    # 危险外部命令：任意系统副作用 / 网络外联
    # 注：不拦截管道 | 与重定向 >/< —— 现有 master 合法使用（>/dev/null、| grep），
    # 其后的恶意命令由本清单（curl/wget/rm 等）覆盖。
    for bad in ("rm", "curl", "wget", "nc", "chmod", "chown", "reboot",
                "shutdown", "mkfs", "dd"):
        if _re.search(rf"\b{_re.escape(bad)}\b", scanned, _re.IGNORECASE):
            reasons.append(f"含危险命令 {bad}")
            break
    return reasons


def verify_command_dangerous_reasons(verify_cmd: str) -> list[str]:
    """公开入口（P1-4）：返回 verify_command 的 shell 注入风险原因，供执行点硬阻断。"""
    return _dangerous_shell_reasons(verify_cmd)


def _basetemp_platform_issues(verify_cmd: str) -> list[str]:
    """检测 verify_command 中 --basetemp 路径的平台性（E027，2026-08-12 实踩）。

    跨平台 basetemp 应使用 `${TMPDIR:-/tmp}/orchd-vf-$$`——verify_command 在
    Windows 上经 Git Bash（orchd.subproc.run_shell）执行，由 bash 展开，真正跨平台。
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
        reasons.append("basetemp 路径含 Windows 专用片段"
                       f"（{basetemp}），非跨平台——应改 {_CROSS_PLATFORM_BASETEMP}")
    # POSIX 专用：${TMPDIR 无 :- 回退（跨平台模板 ${TMPDIR:-/tmp} 是针对的例外）
    if _re.search(r"\$\{TMPDIR(?![^}]*:-)",
                  basetemp) or "$(mktemp" in basetemp:
        reasons.append("basetemp 路径含 POSIX 专用片段"
                       f"（{basetemp}），非跨平台——应改 {_CROSS_PLATFORM_BASETEMP}")
    return reasons
