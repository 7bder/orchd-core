"""Orchd 快照生成（init）与增量更新（amend）。

- init：从 _master.json 读取项目定义，为每个 module 生成 spec.json 快照，
  同时创建空 ledger 与初始 checkpoint，完成项目冷启动。
- amend：在已有 ledger 的基础上做增量更新，依据六状态约束矩阵决定每个
  变更任务是否被允许（pending 可改全部、review 组仅改 reviewers、终态拒绝）。

依赖方向：split.py → spec.py / ledger.py（不导入 onboard / cli）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_current_branch, get_default_branch, list_tracked_changes
from orchd.ledger import Store, resolve_store_dir
from orchd.spec import (
    Master,
    detect_dir_or_glob_declarations,
    filter_terminal_quality_warnings,
    is_code_task,
    validate_quality,
    validate_references,
    validate_source,
    validate_structure,
)

_SAFE_MODULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _collapse(s: str) -> str:
    """折叠连续斜杠为单个（用于 Windows 转义路径与声明路径的归一化对齐）。"""
    return re.sub(r"/{2,}", "/", s)


def _annotate_if_needed(items: list[dict[str, Any]], orchd_dir: Path) -> list[dict[str, Any]]:
    """批量校验结果附加 guidance（best-effort，异常时原样返回）。"""
    if not items:
        return items
    try:
        from orchd.guide import annotate_validation_items
        return annotate_validation_items(items, orchd_dir)
    except Exception:
        return items


def _validate_module_id(mod_id: str) -> str:
    """P2-3：module.id 仅允许 [A-Za-z0-9_-]，防止 ``../`` 或绝对路径越界写到 store 根之外。"""
    if not mod_id or not _SAFE_MODULE_ID_RE.fullmatch(mod_id):
        raise OrchdError(
            ErrorCode.E003,
            f"invalid_module_id: {mod_id!r}",
            [{
                "module_id": mod_id,
                "hint": "module.id 须为 [A-Za-z0-9_-] 组成的相对目录名，禁止路径分隔/..",
            }],
        )
    return mod_id

# intake-commit-enforcement（2026-08-14）：摄入产物文件白名单（两种布局）。
# 摄入 → amend 的正当链路中，这些文件允许以未提交态进入 amend（引擎随后强制
# 提交）；其余任何已跟踪改动视为非摄入脏改动，amend / intake 前置阻断（E017）。
# task-inventory-honesty（F9）：以 orchd.intake._INTAKE_PRODUCT_FILES 为单一
# 真源（禁双写漂移；此前两处手写集合已分叉：split 独缺 IDEAS-archive.md）。
from orchd.intake import _INTAKE_PRODUCT_FILES


# M-2（2026-08-12 全面审计）：三处状态（claimed / done / in_review / 终态附加）
# 共用同一"附加字段"白名单，避免 exempt_files / verify_timeout_seconds 在部分
# 阶段被误拦导致语义不连贯。
_AMEND_ATTACHABLE_FIELDS = frozenset({
    "exempt_files", "verify_command", "verify_timeout_seconds", "reviewers",
    # Bug #20c（2026-08-27）：files_to_edit / files_to_read 加入白名单，
    # claimed 状态可修正无效路径（无需 force-status 回退 pending）。
    "files_to_edit", "files_to_read",
    # task-amend-additional-sources-field：additional_sources 属附加信息（溯源/
    # 归档匹配，不改变任务作用域），claimed/终态均可补登——存量孤儿条目补挂到
    # 已完成任务的合法 CLI 通道（此前直接编辑 _master.json 违反 no-direct-edit）。
    "additional_sources",
})
_CLAIMED_WHITELIST_FIELDS = tuple(_AMEND_ATTACHABLE_FIELDS)

# ── task-amend-terminal-drift-repair（2026-08-12）────────────────────────────
# 终态任务（completed/cancelled）允许自动同步的"合法附加字段增量"：这些字段不改变
# 任务语义/作用域，仅承载引擎/审查附加信息（e.g. 注册后补 exempt_files、跨平台化
# verify_command）。其余字段变更仍触发 E007 终态保护。
#
# 核心字段集合由"任务全部 schema 字段 - 附加字段"推导，避免硬编码漂移。
# Bug #20c（2026-08-27）：终态白名单排除 files_to_edit / files_to_read——
# 已完成/取消的任务不应再改文件声明（仅 claimed/done/in_review 允许修正路径）。
_TERMINAL_ATTACHABLE_FIELDS = _AMEND_ATTACHABLE_FIELDS - {
    "files_to_edit", "files_to_read",
}


def _derive_task_schema_fields() -> frozenset[str]:
    """从 schema/_master.schema.json 动态推导任务全部字段（P3.4 修复）。

    读取 ``tasks[].properties`` 的键集合作为任务 schema 字段全集，避免硬编码
    在 schema 演进（如新增字段）时静默漂移。schema 缺失 / 解析失败时回退到
    内置字段集合（保守默认，保证进程不因 schema 文件异常而崩溃）。
    """
    schema_path = Path(__file__).resolve().parent.parent / "schema" / "_master.schema.json"
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8"))
        props = (
            data.get("properties", {})
            .get("tasks", {})
            .get("items", {})
            .get("properties", {})
        )
        if isinstance(props, dict) and props:
            return frozenset(props.keys())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    # 回退：schema 演进前的内置字段集合
    return frozenset({
        "id", "name", "brief", "module", "depends_on", "estimated_hours",
        "importance", "difficulty", "requires", "acceptance_criteria",
        "files_to_read", "files_to_edit", "reviewers", "verify_command",
        "max_attempts", "deliverables", "exempt_files", "source",
        "verify_timeout_seconds",
    })


_TASK_SCHEMA_FIELDS = _derive_task_schema_fields()
# 核心字段 = 全部字段 - 附加字段（含任一核心字段变更 → E007）
_TERMINAL_CORE_FIELDS = _TASK_SCHEMA_FIELDS - _TERMINAL_ATTACHABLE_FIELDS

# ── task-terminal-spec-revision-channel（2026-09-14）────────────────────────
# 终态任务的**纯文本规格修订**通道：``amend --revise-terminal <task_id> --reason <文本>``。
#
# 动机（2026-09-13 实踩 df44a94）：终态任务的实际规格文本需对齐"已裁定 / 已实现的
# 现实"（如 AC 表述漂移）时，引擎无合规入口——常规 amend 一律 E007、手改
# mod-*/spec.json 是红线 #9、init 重建快照要求空账本，只能绕过引擎；且漂移一旦
# 成立，此后任何 amend 都因该任务的一条错误整体 abort。
#
# 放行字段（用户 2026-09-13 裁定四者均放行）：
# - ``name`` / ``brief``：展示类（无引擎消费者依赖其可执行语义）；
# - ``acceptance_criteria``：引擎消费者仅 E023（模糊词）/ E029（条数）两条 warning；
# - ``deliverables``：``orchd/`` 内零消费者。
# 其余字段一律不因本通道放行——护栏不靠"字段名枚举的自觉"，而是结构性断言：剥离
# 上述文本字段后，其余变更仍须逐项落入既有通道（terminal attachable / 声明路径
# 规范化），否则 E007。以此挡住"用文本字段夹带执行字段"的组合式规避。
_TERMINAL_TEXT_REVISABLE_FIELDS = frozenset({
    "acceptance_criteria", "brief", "name", "deliverables",
})

# 终态规格文本修订的审计事件 reason（复用既有 AMEND 事件类型，不新增事件类型、
# 不改 ledger._apply_event 语义）
_TERMINAL_REVISION_EVENT_REASON = "terminal_spec_revision"


def _ac_field_routing_hint(status: str, fields: set[str]) -> str:
    """AC 类字段被拒时的状态感知路由指引（task-spec-hygiene-flat-sweep AC2）。

    AC 类字段 = :data:`_TERMINAL_TEXT_REVISABLE_FIELDS`（acceptance_criteria /
    brief / name / deliverables）：pending 态可直接改；claimed/done/in_review
    锁死；终态走 ``amend --revise-terminal``。调用方在 E007 中附加本指引，
    使用户按状态找到合法通道，而不是只看到"不可修改"。
    """
    names = sorted(fields & _TERMINAL_TEXT_REVISABLE_FIELDS)
    if status == "claimed":
        return (
            f"字段 {names} 属 AC 类文本：claimed 态不可改；回 pending 改 "
            "（retract --disposition retry 免冷却），或等终态走 "
            "amend --revise-terminal <task_id> --reason \"<理由>\""
        )
    if status in ("done", "in_review"):
        return (
            f"字段 {names} 属 AC 类文本：review 阶段不可改；退回 pending 改，"
            "或等终态（completed/cancelled）走 "
            "amend --revise-terminal <task_id> --reason \"<理由>\""
        )
    return (
        f"字段 {names} 属 AC 类文本：pending 态可直接改；终态走 "
        "amend --revise-terminal <task_id> --reason \"<理由>\""
    )


def init(orchd_dir: Path, master: Master) -> dict[str, Any]:
    """从 _master.json 生成 mod-*/spec.json + 空 ledger + 初始 checkpoint。

    前置：master 通过 validate；ledger 不存在或为空（全新项目）。否则报错。

    目录命名规则：每个模块目录名直接取自 module_id（如 ``mod-foundation/``），
    不会再叠加 ``mod-`` 前缀，即 module_id 为 ``mod-foundation`` 时目录就是
    ``mod-foundation/``，而非 ``mod-mod-foundation/``。

    Returns:
        创建文件清单。

    Raises:
        OrchdError E003/E004/E005/E006: master 校验失败（不写任何文件）。
        OrchdError E007: ledger 非空，不可重复 init。
    """
    orchd_dir = Path(orchd_dir)

    # init 内置校验（load_master → validate → snapshot）
    errors = validate_structure(master) + validate_references(master)
    if errors:
        raise OrchdError(
            errors[0].code,
            f"master validation failed: {len(errors)} error(s), init aborted",
            [{"code": e.code.name, "path": e.path, "message": e.message} for e in errors],
        )

    orchd_dir.mkdir(parents=True, exist_ok=True)
    store = Store(orchd_dir)
    # B-2 修复：mod-*/spec.json 快照与 ledger 同根（ORCHD_HOME 重定向后同落
    # 外部账本根），对齐 ROADMAP 1.2「账本（ledger/checkpoint/lock/mod-*）由
    # ORCHD_HOME 重定向」设计；未设 ORCHD_HOME 时 store_root == orchd_dir。
    store_root = resolve_store_dir(orchd_dir)

    created_files: list[str] = []
    store.acquire_lock()
    try:
        # 幂等检查（锁内 check-then-act）
        if store.ledger_exists() and store.ledger_line_count() > 0:
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: ledger is not empty, cannot re-init (use amend instead)",
                [{"path": str(store.ledger_path)}],
            )

        # 为每个 module 生成 spec.json（目录名 = module_id，如 mod-foundation/）
        modules = master.modules
        tasks = master.tasks
        for module in modules:
            mod_id = _validate_module_id(module.get("id", ""))
            mod_tasks = [t for t in tasks if t.get("module") == mod_id]
            snapshot = {
                "module_id": mod_id,
                "module_name": module.get("name", ""),
                "module_role": module.get("role", ""),
                "tasks": mod_tasks,
            }
            mod_dir = store_root / mod_id
            mod_dir.mkdir(parents=True, exist_ok=True)
            spec_path = mod_dir / "spec.json"
            spec_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            created_files.append(spec_path.relative_to(store_root).as_posix())

        # 创建空 ledger（若不存在）
        if not store.ledger_exists():
            store.ledger_path.touch()
            created_files.append("_ledger.jsonl")

        # 写初始 checkpoint
        store.update_checkpoint({})
        created_files.append("_checkpoint.json")
    finally:
        store.release_lock()

    return {"initialized": True, "created_files": created_files}


def validate_terminal_revision(reason: str | None) -> str:
    """校验并归一化 ``--revise-terminal`` 的 ``--reason``（非空、非纯空白）。

    对齐"CHANGES_REQUESTED 必须附意见"的既有取向：修订理由会写入 AMEND 审计事件，
    缺失即无法回答"谁在何时以何理由改了什么"，故硬拒绝（E007）。CLI 与 amend 共用
    本函数（单一事实源，避免两处文案漂移）。

    Args:
        reason: 命令行 ``--reason`` 原文（可 None）。

    Returns:
        归一化后的理由（strip 后）。

    Raises:
        OrchdError(E007): 缺失 / 纯空白——不写事件、不改快照、不落任何副作用。
    """
    cleaned = reason.strip() if isinstance(reason, str) else ""
    if not cleaned:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_state: --revise-terminal 需附带非空 --reason（终态规格修订理由）",
            [{
                "option": "--revise-terminal",
                "hint": (
                    "修订理由会写入 AMEND 审计事件（reason="
                    f"{_TERMINAL_REVISION_EVENT_REASON}）供事后回查，纯空白视为缺失。"
                    "示例：amend --revise-terminal <task_id> --reason \"AC 文本对齐用户裁定\""
                ),
            }],
        )
    return cleaned


def is_text_only_spec_revision(
    old_task: dict[str, Any], new_task: dict[str, Any]
) -> bool:
    """变更是否仅落在终态规格文本字段（供调用方跳过与之无关的 verify dry-run）。

    文本修订不改变可执行验收面（``verify_command`` 未变），重跑其 dry-run 无信息量；
    且会让本通道被目标的**存量** E024/E027（缺 --basetemp / 不安全段）误伤——终态
    任务多为历史定义，往往命中存量告警，一旦计入阻断集合通道即不可用。

    Args:
        old_task: 快照（mod-*/spec.json）中的任务定义。
        new_task: master 中的任务定义。

    Returns:
        存在变更且变更字段全部落在 :data:`_TERMINAL_TEXT_REVISABLE_FIELDS`。
    """
    changed = {
        key
        for key in set(old_task) | set(new_task)
        if old_task.get(key) != new_task.get(key)
    }
    return bool(changed) and changed <= _TERMINAL_TEXT_REVISABLE_FIELDS


def _terminal_rejection_hint(task_id: str, fields: set[str], status: str) -> str:
    """终态字段被拒时的可执行指引（AC6："可自查"而非只报不可修改）。"""
    revisable = sorted(fields & _TERMINAL_TEXT_REVISABLE_FIELDS)
    if revisable:
        return (
            f"字段 {revisable} 属规格文本，可走修订通道（写 AMEND 审计事件、"
            "快照随 master 同步）："
            f"python .orchd/__main__.py amend --revise-terminal {task_id} "
            "--reason \"<修订理由>\""
        )
    return (
        f"{status} 任务的 {sorted(fields)} 属执行字段，不因文本修订通道放行；"
        f"确需变更请先经用户裁决走逃生口回退："
        f"python .orchd/__main__.py force-status --task {task_id} --status pending "
        "--reason \"<理由>\" --force（completed→pending 另需 --evidence-sha <提交>）"
    )


def _log_amend_guard_degrade(entry: dict[str, Any]) -> None:
    """amend 准入守卫降级留痕（``orchd ▸ [amend-guard]``，best-effort，R2-4）。

    stderr 是留痕通道（stdout 恒为 JSON 机器契约，见 conventions.md「命令输出通道
    契约」）；任何异常静默跳过，不阻断 amend 主流程。不被 ``ORCHD_QUIET`` 抑制：
    与 worktree ``[回收]`` 这类常规噪声不同，准入守卫失效属安全相关降级决策，
    必须可追溯（R2-4 的原缺陷正是零留痕）。

    Args:
        entry: 结构化降级条目（guard / severity / status / reason / error / hint）。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass
    try:
        print(
            f"orchd ▸ [amend-guard] {json.dumps(entry, ensure_ascii=False)}",
            file=sys.stderr,
        )
    except Exception:
        pass


def _git_head_sha(project_root: Path | None) -> str | None:
    """best-effort 取 HEAD SHA；非 git / 异常 → None（调用方跳过 CAS）。

    供 amend 的乐观并发校验（task-concurrent-amend-lost-update）与调用方读时
    快照共用。空串按 None 处理。
    """
    if project_root is None:
        return None
    try:
        import subprocess as _sp

        proc = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (_sp.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def amend(
    orchd_dir: Path,
    master: Master,
    store: Store,
    revise_terminal: str | None = None,
    reason: str | None = None,
    expected_head: str | None = None,
    release_lock: bool = True,
) -> dict[str, Any]:
    """增量更新 snapshot：按状态约束矩阵过滤变更。

    约束矩阵：
    - pending：可改全部字段
    - claimed：仅允许修改 verify_command / reviewers 白名单字段，其余拒绝（E007）
    - done / in_review（review 组）：仅允许修改 reviewers
    - completed / cancelled：默认拒绝；``revise_terminal`` 指向该任务时，纯文本规格
      字段（acceptance_criteria / brief / name / deliverables）可修订并写 AMEND 审计
      事件，其余字段仍逐项走既有通道判定（task-terminal-spec-revision-channel）

    review 阶段归一化比对：对于 done / in_review 状态的任务，先将新定义的
    reviewers 字段还原为旧值，再与旧定义做全量比对；若相等则说明仅有
    reviewers 发生了变更，允许通过；否则拒绝（提示"仅允许修改 reviewers"）。

    新增任务始终允许。删除任务不阻止，但在摘要中报告 removed_tasks。

    红线 7（task-redline7-amend-refuse，roadmap:constraint-hardening）：
    amend 只在 default（main）分支执行。在非 main 分支上调用 amend 直接
    拒绝注册（抛出 E007），而非降级为"仅不提交"——否则任务分支内仍会
    注册任务并改写 master，污染 master 注册来源。分支判定仅在 amend 阶段
    强制：init / validate / status 等只读或冷启动命令不受限。

    Args:
        orchd_dir: ``.orchd/`` 目录（快照与账本根）。
        master: 待生效的 master 定义。
        store: 账本 Store（本函数内自持锁）。
        revise_terminal: 终态规格文本修订通道的开关——目标 task_id；仅该任务放行
            文本字段修订（须为终态任务，否则 E007）。
        reason: 修订理由（``revise_terminal`` 非空时必填、非空白，写入审计事件）。

    Returns:
        变更摘要（含 ``terminal_spec_revisions``：本次文本修订的 fields 与 rationale；
        以及条件字段 ``degraded_guards``：准入锁 / HEAD 漂移检测 best-effort 降级时
        非空，逐条给出 guard / reason / error / hint，供 agent 判断「本次未经准入锁
        保护」而非误读为「已确认无并发注册」）。
        ``release_lock=False`` 时另含 ``_intake_lock``（调用方提交后释放；仅内部
        通道使用，调用方须在返回响应前 pop 掉，不外泄）。

    并发语义（task-concurrent-amend-lost-update）：``expected_head`` 为调用方读
    master 时的 HEAD。持锁后比对，不一致 → E007 ``stale_base`` fail-closed（重试
    即重读最新 base，自然收敛）。读与 dry-run（数十秒）发生在锁外，仅靠准入锁
    串行化不够——两 amend 可先后读同一 base 再先后写，后写者静默覆盖（丢失更新）；
    ``head_drift_check`` 判的是工作区相对自分支，不覆盖此形，故独立比对。
    ``expected_head=None``（老调用 / 非 git）→ 跳过比对。
    """
    orchd_dir = Path(orchd_dir)
    tasks = master.tasks
    modules = master.modules

    # 红线 7：amend 只在 default（main）分支（= canonical 主工作树）执行。
    # task-master-single-copy：废除 task 分支 amend 例外——container 下任务
    # worktree 不再保留 .orchd/_master.json（sparse-checkout 抑制），唯一权威 =
    # 主工作树；任务分支上补声明改由"认领后、动手前在主工作树补 files_to_edit"
    # 流程承接。任务分支调用 amend 一律拒绝注册（E007，含 task/xxx），而非降级
    # 为"仅不提交"，避免污染 master 注册来源。分支判定仅在 amend 阶段强制：
    # init / validate / status 等只读或冷启动命令不受限。
    project_root = orchd_dir.parent
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_branch: amend 仅在 default（{default_branch}）分支执行（主工作树），"
            f"当前分支 {current_branch} 拒绝注册（红线 7）",
            [{
                "branch": current_branch,
                "default": default_branch,
                "hint": (
                    f"任务分支不再允许 amend；请在主工作树 {project_root} 上补充/注册 "
                    "files_to_edit 等声明后再执行"
                ),
            }],
        )

    # intake-commit-enforcement（2026-08-14）：amend 前置"非摄入产物干净"守卫。
    # 摄入产物（IDEAS.md / ROADMAP.md / _master.json，两种布局）允许未提交态进入
    # （摄入 → amend 的正当链路，引擎随后强制提交）；摄入产物之外的任何已跟踪
    # 改动 → E017 阻断注册——避免脏工作区被 checkout -b 带进任务分支，对齐
    # claim/done/review 的 require_clean 语义（lint：amend 是四条写命令中此前
    # 唯一无干净度守卫的）。
    dirty_files = list_tracked_changes(project_root)
    if dirty_files is not None:
        non_intake = [f for f in dirty_files if f not in _INTAKE_PRODUCT_FILES]
        if non_intake:
            raise OrchdError(
                ErrorCode.E017,
                "dirty_workspace: amend 要求除摄入产物（IDEAS.md / ROADMAP.md / "
                "_master.json）外工作区干净",
                [{
                    "command": "amend",
                    "dirty_files": non_intake,
                    "hint": (
                        "请先提交或还原摄入产物之外的文件改动"
                        "（untracked 工具/配置文件不阻塞）"
                    ),
                }],
            )

    errors: list[dict[str, Any]] = []
    updated_tasks: list[str] = []
    whitelisted_updates: list[dict[str, Any]] = []
    new_tasks: list[str] = []
    unchanged_tasks: list[str] = []
    sources_missing: list[str] = []
    sources_invalid: list[str] = []
    source_errors: list[dict[str, Any]] = []
    attachable_sync: list[dict[str, Any]] = []
    terminal_decl_sync: list[dict[str, Any]] = []
    # task-terminal-spec-revision-channel：本次终态规格文本修订明细（写审计事件 + 回响应）
    terminal_text_revisions: list[dict[str, Any]] = []

    # task-intake-file-lock（AC1/AC3）：准入写锁 + 提交前 HEAD 推进检测。
    # 准入写（改 _master.json / IDEAS.md / ROADMAP.md）受独立 .intake.lock 串行，
    # 不复用账本锁（避免一次 amend 阻塞并行 claim/done）；HEAD 漂移检测发现
    # base 被并行推进则拒绝注册（git 层 TOCTOU）。
    # R2-4（task-split-guard-no-silent-swallow）：两者仍是 best-effort（失败不阻断
    # amend），但**降级必须可见**——原 `except Exception: pass` 把「锁没拿到 / 检测
    # 本身坏了」与「确实无并发」混为一谈且零留痕。现在：异常被捕获时显式释放并把
    # intake_lock 置 None（明确降级为无锁准入），同时产出结构化留痕（stderr +
    # 响应 degraded_guards），与 session_lock.py 的 best-effort 降级语义对齐但可见。
    intake_lock: dict[str, Any] | None = None
    degraded_guards: list[dict[str, Any]] = []
    guard_stage = "intake_lock_acquire"
    try:
        from orchd.gitops import head_drift_check
        from orchd.ledger import intake_lock_acquire, intake_lock_release

        if orchd_dir is not None:
            from orchd.ledger import resolve_agent_id

            intake_lock = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
        # 提交前 HEAD 推进检测：main 被并行推进则拒绝（AC3）
        guard_stage = "head_drift_check"
        drift = head_drift_check(project_root, ref="HEAD", base_ref=default_branch)
        if drift.get("drift"):
            if intake_lock is not None:
                intake_lock_release(intake_lock)
                intake_lock = None
            raise OrchdError(
                ErrorCode.E007,
                f"stale_base: main 已被并行推进（base {drift.get('base_sha')[:7]}"
                " 与本地 HEAD 分叉），拒绝注册——请先更新工作区 main 后重试",
                [{"base_sha": drift.get("base_sha"), "head_sha": drift.get("head_sha")}],
            )
    except OrchdError:
        raise
    except Exception as exc:
        # best-effort 降级：不阻断 amend，但显式释放 + 置 None + 结构化留痕。
        guard_entry: dict[str, Any] = {
            "guard": guard_stage,
            "severity": "warning",
            "status": "degraded",
            "reason": "guard_exception",
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "准入锁 / HEAD 漂移检测未能执行（best-effort 降级）：本次 amend 在"
                "无准入锁保护下继续，不可据此断言无并发注册；请排查后重跑"
            ),
        }
        degraded_guards.append(guard_entry)
        if intake_lock is not None:
            try:
                from orchd.ledger import intake_lock_release as _release_intake_lock

                _release_intake_lock(intake_lock)
            except Exception:
                pass
            intake_lock = None
        _log_amend_guard_degrade(guard_entry)

    store.acquire_lock()
    try:
        state = store.replay()

        # task-concurrent-amend-lost-update：乐观并发（CAS）——持锁后第一件事。
        # expected_head 为调用方读 master 时的 HEAD；不一致说明读后有并行提交，
        # 此时写入必覆盖他人内容 → E007 stale_base 拒绝（未写任何内容，可直接重试）。
        if expected_head is not None and intake_lock is not None:
            _head_now = _git_head_sha(project_root)
            if _head_now is not None and _head_now != expected_head:
                raise OrchdError(
                    ErrorCode.E007,
                    "stale_base: main 已被并行推进，本次 amend 未写入任何内容——"
                    "请直接重试 amend（将重读最新 base 重放变更）",
                    [{"expected_head": expected_head, "head_now": _head_now}],
                )

        # task-terminal-spec-revision-channel：修订开关前置校验——先于任何写入，
        # 不合规立即 E007/E005（不写事件、不改快照、不落任何副作用）。
        revision_reason: str | None = None
        if revise_terminal is not None:
            revision_reason = validate_terminal_revision(reason)
            if not any(t.get("id") == revise_terminal for t in tasks):
                raise OrchdError(
                    ErrorCode.E005,
                    f"task '{revise_terminal}' not found in master",
                    [{
                        "task_id": revise_terminal,
                        "hint": "--revise-terminal 指定的任务须已存在于 _master.json",
                    }],
                )
            target_state = state.get(revise_terminal)
            target_status = target_state.status if target_state else "pending"
            if target_status not in ("completed", "cancelled"):
                raise OrchdError(
                    ErrorCode.E007,
                    "invalid_state: --revise-terminal 仅适用于终态任务"
                    f"（completed/cancelled），任务 {revise_terminal} 当前状态为 "
                    f"{target_status}",
                    [{
                        "task_id": revise_terminal,
                        "status": target_status,
                        "hint": (
                            "非终态任务的字段变更走常规 amend 矩阵（pending 可改全部 / "
                            "claimed 与 review 组仅附加字段白名单），无需本通道"
                        ),
                    }],
                )

        # L253：注册前结构校验——拦截非法字段入库（intake 期暴露，而非 done/validate 事后）
        # P2-4：并补跨引用校验（E006 重复 id / E005 未知 depends_on·module / E004 DAG 环），
        # 防止仅经 amend 注入坏引用；shared 文件存在性仅在 .orchd/ 目录时检查（无副作用）。
        structure_errors = validate_structure(master) + validate_references(master)
        if structure_errors:
            from orchd.guide import annotate_validation_items
            raw_details = [{"code": e.code.name, "path": e.path, "message": e.message}
                           for e in structure_errors]
            annotated_details = annotate_validation_items(raw_details, orchd_dir)
            raise OrchdError(
                ErrorCode.E003,
                f"schema_validation_failed: {len(structure_errors)} error(s), amend aborted",
                annotated_details,
            )

        # 加载现有 snapshot 中的任务定义（用于 diff；扫描全部模块目录）。
        # B-2 修复：快照与 ledger 同根（resolve_store_dir），与 init 写入路径一致。
        store_root = resolve_store_dir(orchd_dir)
        existing_tasks: dict[str, dict[str, Any]] = {}
        for spec_path in sorted(store_root.glob("mod-*/spec.json")):
            snapshot = json.loads(spec_path.read_text(encoding="utf-8"))
            for t in snapshot.get("tasks", []):
                existing_tasks[t.get("id", "")] = t

        new_task_ids = set()
        conflict_warnings: list[dict[str, Any]] = []
        for task in tasks:
            tid = task.get("id", "")
            new_task_ids.add(tid)
            ts = state.get(tid)
            status = ts.status if ts else "pending"

            if tid not in existing_tasks:
                # 新增任务强制 source 声明（2026-08-11 硬约束 + 存量豁免）。
                # 存量任务（snapshot 中存在）grandfather：不要求 source、不校验引用。
                # A4（task-amend-terminal-exempt）：快照漂移的终态卡不按新卡要求溯源。
                # existing_tasks 来自 snapshot，快照落后账本时终态任务会被误判为新卡，
                # 进而因归档 idea 恒 E025（执行者被迫回灌引擎自己的引用完整性）。账本
                # 终态为准：completed/cancelled 跳过 source 硬要求（与 validate_source
                # 的 P2-2 豁免同源）。
                _terminal_drifted = status in ("completed", "cancelled")
                source = task.get("source")
                if not _terminal_drifted and (
                        not source or not isinstance(source, str)
                        or not source.strip()):
                    sources_missing.append(tid)
                    source_errors.append({
                        "task_id": tid,
                        "status": "new",
                        "message": (
                            "新增任务缺 source 字段，拒绝注册（须声明来源 "
                            "idea:<ref> 或 roadmap:<ref>，存量任务豁免）"
                        ),
                    })
                # 新增任务：与在池 pending/claimed 任务（含同批次新任务）的
                # files_to_edit 冲突降级为 warning（不阻断注册）。冲突硬边界
                # 在 claim E010（活跃集合判定）+ request 依赖感知强制过滤
                # （与 claimed 冲突、与 pending 非依赖任务冲突均被过滤）。
                new_files = set(task.get("files_to_edit", []))
                if new_files:
                    conflicts: list[dict[str, Any]] = []
                    for other in tasks:
                        oid = other.get("id")
                        if oid == tid:
                            continue
                        ots = state.get(oid)
                        ostat = ots.status if ots else "pending"
                        if ostat not in ("pending", "claimed"):
                            continue
                        overlap = new_files & set(other.get("files_to_edit", []))
                        if overlap:
                            conflicts.append({
                                "task_id": oid,
                                "status": ostat,
                                "files": sorted(overlap),
                            })
                    if conflicts:
                        conflict_warnings.append({
                            "task_id": tid,
                            "status": "new",
                            "message": (
                                "新任务与在池任务存在文件冲突（warning，不阻断）："
                                f"{conflicts}。依赖链上共享文件为合法串行序列；"
                                "其余冲突将由 request 依赖感知强制过滤与 claim E010 拦截"
                            ),
                        })
                new_tasks.append(tid)
                continue

            old_task = existing_tasks[tid]
            if old_task == task:
                unchanged_tasks.append(tid)
                continue

            # 有变更，检查约束
            if status == "claimed":
                changed_fields = {
                    key
                    for key in set(task) | set(old_task)
                    if task.get(key) != old_task.get(key)
                }
                if changed_fields <= set(_CLAIMED_WHITELIST_FIELDS):
                    # task-amend-scope-add：claimed 任务 files_to_edit 只增不删
                    # 添加遗漏连带文件允许，删除已声明文件拒绝（E007）
                    if "files_to_edit" in changed_fields:
                        old_files = set(old_task.get("files_to_edit", []))
                        new_files = set(task.get("files_to_edit", []))
                        removed = old_files - new_files
                        if removed:
                            errors.append({
                                "task_id": tid,
                                "status": status,
                                "message": (
                                    "claimed task files_to_edit 只增不删："
                                    f"禁止删除已声明文件 {sorted(removed)}，"
                                    "仅允许添加遗漏连带文件"
                                ),
                                "hint": (
                                    "出路（task-amend-patch-write-after-validate）："
                                    "确需删声明请 retract --disposition retry（免认领冷却）"
                                    "退回 pending 后 amend，再 claim（无需 --force）"
                                ),
                            })
                            continue
                    updated_tasks.append(tid)
                    whitelisted_updates.append({
                        "task_id": tid,
                        "fields": sorted(changed_fields),
                    })
                else:
                    _rejected = set(changed_fields) - set(_CLAIMED_WHITELIST_FIELDS)
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            "claimed task only allows whitelist fields "
                            f"{sorted(_CLAIMED_WHITELIST_FIELDS)}, "
                            f"got {sorted(changed_fields)}"
                        ),
                        # task-spec-hygiene-flat-sweep AC2：AC 类字段被拒时附加
                        # 状态感知路由（pending 改 / 终态 revise-terminal）。
                        **({"hint": _ac_field_routing_hint(status, _rejected)}
                           if _rejected & _TERMINAL_TEXT_REVISABLE_FIELDS else {}),
                    })
            elif status in ("completed", "cancelled"):
                # task-amend-terminal-drift-repair：终态任务 master≠snapshot 时，
                # 区分"合法附加字段增量"与"核心字段变更"。附加字段白名单 → 自动
                # 以 master 为准同步 snapshot（不报 E007）；含核心字段 → 仍 E007。
                #
                # task-terminal-spec-revision-channel：开关指向本任务时，先剥离纯文本
                # 规格字段（有 AMEND 审计事件），**其余字段**仍须逐项落入既有两条通道
                # （terminal attachable / 声明路径规范化），否则 E007——结构性护栏，
                # 挡住"用文本字段夹带执行字段"的组合式规避。
                changed_fields = {
                    key
                    for key in set(task) | set(old_task)
                    if task.get(key) != old_task.get(key)
                }
                text_revised: set[str] = set()
                if revise_terminal == tid:
                    text_revised = changed_fields & _TERMINAL_TEXT_REVISABLE_FIELDS
                remaining = changed_fields - text_revised

                if not changed_fields:
                    # 值级无差异（e.g. master 以 null 占位、快照缺键）→ 视为未变更，
                    # 避免 key 存在性差异把空 diff 误判为"终态不可修改"（假阳性 E007）。
                    unchanged_tasks.append(tid)
                elif not remaining:
                    # 仅文本字段变更 → 走修订通道（审计事件在快照同步后写入）。
                    # task-spec-hygiene-flat-sweep AC4：AC 条数启发式——文本修订不得
                    # 改变 acceptance_criteria 条数（阈值 0，确定性）：条数增减会改变
                    # E029 粒度语义与验收面，属范围变更而非文本对齐。命中即 E007，
                    # 指引用户裁决后走逃生口（force-status 回 pending）。
                    if revise_terminal == tid and "acceptance_criteria" in text_revised:
                        _old_ac = old_task.get("acceptance_criteria") or []
                        _new_ac = task.get("acceptance_criteria") or []
                        if len(_old_ac) != len(_new_ac):
                            errors.append({
                                "task_id": tid,
                                "status": status,
                                "message": (
                                    f"{status} 任务 acceptance_criteria 条数变更"
                                    f"（{len(_old_ac)} → {len(_new_ac)}）："
                                    "文本修订通道只放行条数不变的文本对齐"
                                ),
                                "hint": (
                                    "AC 增删改变验收面，请先经用户裁决走逃生口回退："
                                    f"python .orchd/__main__.py force-status --task {tid} "
                                    "--status pending --reason \"<理由>\" --force"
                                    "（completed→pending 另需 --evidence-sha <提交>）"
                                ),
                            })
                            continue
                    updated_tasks.append(tid)
                elif remaining <= _TERMINAL_ATTACHABLE_FIELDS:
                    updated_tasks.append(tid)
                    attachable_sync.append({
                        "task_id": tid,
                        "status": status,
                        "fields": sorted(remaining),
                    })
                elif remaining <= {"files_to_edit", "exempt_files"}:
                    # task-terminal-decl-drift-channel：终态声明路径规范化通道。
                    # 仅当删的全不存在、增的全存在（相对 project_root）时放行并
                    # 同步 snapshot；否则仍 E007。防"借规范化之名篡改声明"。
                    sync_entries: list[dict[str, Any]] = []
                    normalized_ok = True
                    for field in ("files_to_edit", "exempt_files"):
                        if field not in remaining:
                            continue
                        old_files = old_task.get(field, []) or []
                        new_files = task.get(field, []) or []
                        removed = [p for p in old_files if p not in new_files]
                        added = [p for p in new_files if p not in old_files]
                        if any((project_root / p).exists() for p in removed):
                            normalized_ok = False
                            break
                        if any(not (project_root / p).exists() for p in added):
                            normalized_ok = False
                            break
                        sync_entries.append({
                            "task_id": tid,
                            "field": field,
                            "removed": sorted(removed),
                            "added": sorted(added),
                        })
                    if normalized_ok:
                        updated_tasks.append(tid)
                        terminal_decl_sync.extend(sync_entries)
                    else:
                        errors.append({
                            "task_id": tid,
                            "status": status,
                            "message": (
                                f"{status} 为终态，声明路径变更非规范化"
                                "（删的须不存在、增的须存在），不可修改"
                            ),
                            "changed_fields": sorted(remaining),
                            "diff": {
                                field: {
                                    "snapshot": old_task.get(field),
                                    "master": task.get(field),
                                }
                                for field in sorted(remaining)
                            },
                            "hint": _terminal_rejection_hint(tid, remaining, status),
                        })
                        continue
                else:
                    # AC6：列出差异字段两侧取值（snapshot vs master）+ 可执行指引，
                    # 使同类漂移可自查（此前仅一句"为终态，不可修改"）。
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            f"{status} 为终态，字段 {sorted(remaining)} 不可修改"
                            "（非附加字段 / 非文本修订白名单）"
                        ),
                        "changed_fields": sorted(remaining),
                        "diff": {
                            field: {
                                "snapshot": old_task.get(field),
                                "master": task.get(field),
                            }
                            for field in sorted(remaining)
                        },
                        "hint": _terminal_rejection_hint(tid, remaining, status),
                    })
                    continue

                if text_revised:
                    terminal_text_revisions.append({
                        "task_id": tid,
                        "status": status,
                        "fields": sorted(text_revised),
                        "rationale": revision_reason,
                    })
            elif status in ("done", "in_review"):
                # T2（2026-08-08）+ M-2（2026-08-12）+ Bug #20c（2026-08-27）：
                # 仅允许修改附加字段白名单（reviewers / verify_command /
                # exempt_files / verify_timeout_seconds / files_to_edit /
                # files_to_read）。把新定义的附加字段全部还原为旧值后与旧定义
                # 比对，相等才说明"只有白名单字段变了"。
                normalized = dict(task)
                for field in _AMEND_ATTACHABLE_FIELDS:
                    if field in old_task:
                        normalized[field] = old_task.get(field)
                    else:
                        # old_task（snapshot）无该键时从 new 定义中移除，与旧定义对齐
                        normalized.pop(field, None)
                if normalized == old_task:
                    updated_tasks.append(tid)
                    changed_fields = {
                        key
                        for key in set(task) | set(old_task)
                        if task.get(key) != old_task.get(key)
                    }
                    if changed_fields:
                        whitelisted_updates.append({
                            "task_id": tid,
                            "fields": sorted(changed_fields),
                        })
                else:
                    _normalized_changed = {
                        key
                        for key in set(task) | set(old_task)
                        if task.get(key) != old_task.get(key)
                    } - set(_AMEND_ATTACHABLE_FIELDS)
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            "review 阶段仅允许修改附加字段 "
                            "(reviewers / verify_command / exempt_files / "
                            "verify_timeout_seconds)；检测到其他字段同时被修改"
                        ),
                        # task-spec-hygiene-flat-sweep AC2：AC 类字段被拒时附加
                        # 状态感知路由（pending 改 / 终态 revise-terminal）。
                        **({"hint": _ac_field_routing_hint(status, _normalized_changed)}
                           if _normalized_changed & _TERMINAL_TEXT_REVISABLE_FIELDS else {}),
                    })
            else:
                # pending：可改全部
                updated_tasks.append(tid)

        # 被删除的任务（snapshot 中存在但 master 已移除）：不阻止，仅报告
        removed_tasks = sorted(set(existing_tasks) - new_task_ids)

        # E025：新增任务 source 引用硬校验（2026-08-11，task-source-amend-enforce）。
        # validate_source 对全量校验（含存量），但仅对新增任务硬阻断；存量任务
        # grandfather 豁免（不校验引用、不要求 source）。按 path 定位到 task id，
        # 仅收集新增任务命中。
        if new_tasks:
            new_task_set = set(new_tasks)
            violations = validate_source(master, project_root=orchd_dir.parent)
            for v in violations:
                # task-split-additional-sources-gate：主 source 与附加引用同等
                # 硬门（此前正则仅匹配 `.source` 结尾，新任务附加引用非法被静默
                # 跳过；validate 告警仍在，只丢 amend 硬阻断）。
                m = re.match(
                    r"\$\.tasks\[(\d+)\]\.(?:source|additional_sources\[\d+\])$",
                    v.path)
                if not m:
                    continue
                idx = int(m.group(1))
                if not (0 <= idx < len(master.tasks)):
                    continue
                tid = master.tasks[idx].get("id", "")
                if tid in new_task_set:
                    sources_invalid.append(tid)
                    source_errors.append({
                        "task_id": tid,
                        "status": "new",
                        "message": v.message,
                    })

        if source_errors:
            raise OrchdError(
                ErrorCode.E025,
                f"source_validation_failed: {len(source_errors)} new task(s) "
                "missing/invalid source, amend aborted。"
                " 提示：外部来源任务可用 debug:manual 标记。",
                source_errors,
            )

        if errors:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: {len(errors)} task(s) cannot be amended",
                errors,
            )

        # L262：注册前质量校验（E022/E023/E024/E029，warning 不阻断注册，附加到响应）。
        # R5（task-constraint-quality-checks）：代码类任务缺 verify_command 升级为阻断。
        raw_quality = validate_quality(master)
        quality_warnings: list[dict[str, Any]] = []
        for e in raw_quality:
            entry = {"code": str(e.code), "path": e.path, "message": e.message}
            # E022 代码类阻断：按 path 解析 task 下标，用 is_code_task 判定任务类型
            if e.code is ErrorCode.E022:
                m = re.match(r"\$\.tasks\[(\d+)\]\.verify_command$", e.path)
                is_code = False
                if m and m.group(1).isdigit():
                    idx = int(m.group(1))
                    if 0 <= idx < len(tasks):
                        is_code = is_code_task(tasks[idx])
                if is_code:
                    raise OrchdError(
                        ErrorCode.E022,
                        "注册前质量校验失败：代码类任务缺 verify_command，注册被阻断",
                        [entry | {"blocking": True}],
                    )
            quality_warnings.append(entry)

        # 终态任务质量告警豁免（task-amend-quality-warning-terminal-filter，单一真源）：
        # E022 阻断已在上方先行（过滤前生效，语义零变化）；此处只过滤质量类可见性
        # 告警（E023/E026/E027/E029），判据本身零变化。state 为本锁内 replay，
        # 新注册任务不在其中（非终态，不被过滤）。
        try:
            _terminal_ids = {
                tid for tid, ts in state.items()
                if ts.status in ("completed", "cancelled")
            }
        except Exception:
            _terminal_ids = None
        quality_warnings, _exempted_terminal_warnings = filter_terminal_quality_warnings(
            quality_warnings, master.tasks, _terminal_ids
        )

        # task-decl-dir-notation-guard（AC3）：声明形态门禁——对本次新增或声明
        # 变更的任务检出目录式/通配符声明 → E003 拒绝注册。存量未变更任务不触发
        # （grandfather 豁免，返工时即被拦自愈）。detect_dir_or_glob_declarations
        # 是唯一检出原语（spec.py），与消费点前缀匹配复用，禁双写。
        _changed_ids = set(new_tasks) | set(updated_tasks)
        _dir_glob_errors: list[dict[str, Any]] = []
        for task in tasks:
            tid = task.get("id", "")
            if tid not in _changed_ids:
                continue
            hits = detect_dir_or_glob_declarations(task)
            for h in hits:
                _dir_glob_errors.append({
                    "task_id": tid,
                    "field": h["field"],
                    "path": h["path"],
                    "kind": h["kind"],
                    "message": (
                        f"{h['field']} 声明的路径 '{h['path']}' 为"
                        f"{'目录式' if h['kind'] == 'directory' else '通配符'}声明，"
                        "注册被拒绝（intake.md step 4 禁目录式/通配符声明，"
                        "须展开为具体文件路径）"
                    ),
                })
        if _dir_glob_errors:
            raise OrchdError(
                ErrorCode.E003,
                "schema_validation_failed: 声明形态校验失败（目录式/通配符声明被拒）",
                _dir_glob_errors,
            )

        # Bug #20a（2026-08-27）：files_to_edit / exempt_files 路径存在性校验。
        # 摄入时检测声明了但不存在的路径，写入 conflict_warnings 供人工核对。
        # 不硬阻断（路径可能是待创建的新文件），仅告警。
        # task-decl-dir-notation-guard（AC4）：仅遍历本次新增/变更任务，
        # 存量任务不再产生 files_to_edit_path_not_found 告警（消除 121 个存量
        # 目录式声明每次 amend 刷屏）。
        for task in tasks:
            tid = task.get("id", "")
            if tid not in _changed_ids:
                continue
            for field in ("files_to_edit", "exempt_files"):
                for fp in task.get(field, []):
                    full = project_root / fp
                    if not full.exists():
                        conflict_warnings.append({
                            "task_id": tid,
                            "type": f"{field}_path_not_found",
                            "file": fp,
                            "message": (
                                f"{field} 声明的路径 '{fp}' 在项目中不存在"
                                f"。若为待创建新文件可忽略，否则请修正路径。"
                            ),
                        })

        # task-decl-withdraw-channel：与上方「声明了但不存在的路径」对称的反向提示——
        # E-16（task-hostfix-pack-b）：仅当豁免**真冗余**（同任务 files_to_edit 内
        # 也有该文件，豁免已无对象）才告警。旧口径“存在即失效”与 E026 主用例矛盾：
        # E026 正是要求把**既有**连带测试文件声明进 exempt_files，存在即告会把正确
        # 用法全量误报；且照告警去 remove 会立刻触发 E010 越界。只告警不阻断。
        for task in tasks:
            tid = task.get("id", "")
            if tid not in _changed_ids:
                continue
            _declared = set(task.get("files_to_edit", []) or [])
            for fp in task.get("exempt_files", []) or []:
                if (project_root / fp).exists() and fp in _declared:
                    conflict_warnings.append({
                        "task_id": tid,
                        "type": "exempt_files_path_exists",
                        "file": fp,
                        "message": (
                            f"exempt_files 声明的路径 '{fp}' 已存在于磁盘且同时在 "
                            f"files_to_edit 内：豁免已冗余。可执行 "
                            f"`orchd amend --task {tid} --remove-exempt-files {fp}` 撤回该声明。"
                        ),
                    })

        # 重新生成所有 snapshot（目录名 = module_id）
        for module in modules:
            mod_id = _validate_module_id(module.get("id", ""))
            mod_tasks = [t for t in tasks if t.get("module") == mod_id]
            snapshot = {
                "module_id": mod_id,
                "module_name": module.get("name", ""),
                "module_role": module.get("role", ""),
                "tasks": mod_tasks,
            }
            mod_dir = store_root / mod_id
            mod_dir.mkdir(parents=True, exist_ok=True)
            spec_path = mod_dir / "spec.json"
            spec_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        # task-terminal-spec-revision-channel：终态规格文本修订的 AMEND 审计事件。
        # 复用既有事件类型（不新增类型、不改 ledger._apply_event 语义——AMEND 为纯审计
        # 事件，_event_target_status 返回 None → 跳过状态机校验，不影响任务状态），
        # 携带 fields 与 rationale，经 ledger/status 即可回查"谁在何时以何理由改了什么"。
        # master 为唯一权威：快照已在上方按 master 重新生成，事件只作审计。
        if terminal_text_revisions:
            from orchd.gitops_ops import make_event
            from orchd.ledger import resolve_agent_id

            agent_id = resolve_agent_id(orchd_dir)
            for revision in terminal_text_revisions:
                store.append_event(make_event(
                    revision["task_id"], agent_id, "AMEND",
                    reason=_TERMINAL_REVISION_EVENT_REASON,
                    fields=revision["fields"],
                    rationale=revision["rationale"],
                    hint=(
                        "终态规格文本修订（amend --revise-terminal）：master 为唯一权威，"
                        "mod-*/spec.json 快照已同步"
                    ),
                ))
            store.update_checkpoint(store.replay())
    except Exception:
        # 调用方持有模式（release_lock=False）异常亦释放——调用方永无对象可放，
        # 不释放即进程内泄漏；默认模式沿旧行为（异常即泄漏，进程退出回收）。
        if intake_lock is not None and not release_lock:
            try:
                intake_lock_release(intake_lock)
            except Exception:
                pass
        raise
    finally:
        store.release_lock()

    # task-intake-file-lock：成功路径释放准入写锁；release_lock=False 时跳过释放
    # （所有权移交调用方，锁对象经下方 result["_intake_lock"] 透出，调用方提交后释放）。
    if intake_lock is not None and release_lock:
        try:
            intake_lock_release(intake_lock)
        except Exception:
            pass

    result: dict[str, Any] = {
        "amended": True,
        "new_tasks": new_tasks,
        "updated_tasks": updated_tasks,
        "whitelisted_updates": whitelisted_updates,
        "attachable_sync": attachable_sync,
        "terminal_decl_sync": terminal_decl_sync,
        "terminal_spec_revisions": terminal_text_revisions,
        "unchanged_tasks": unchanged_tasks,
        "removed_tasks": removed_tasks,
        "quality_warnings": _annotate_if_needed(quality_warnings, orchd_dir),
        "exempted_terminal_warnings": _exempted_terminal_warnings,
        "conflict_warnings": conflict_warnings,
        "sources_missing": sources_missing,
        "sources_invalid": sources_invalid,
    }
    # R2-4：准入守卫降级不静默——非空时并入响应（无降级则维持既有字段集合）
    if degraded_guards:
        result["degraded_guards"] = degraded_guards
    # task-concurrent-amend-lost-update：调用方持有模式透出锁对象（写+提交原子化）。
    if intake_lock is not None and not release_lock:
        result["_intake_lock"] = intake_lock
    return result


def classify_dry_run_failure(
    verify_cmd: str,
    exit_code: int,
    stderr: str,
    stdout: str = "",
    to_be_created: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
) -> str:
    """dry-run 失败分类（L3：注册通道校验，2026-08-08）。

    区分两类 dry-run 失败：
    - ``assertion_mismatch``：断言应匹配现有文件而失败（如 pytest 收集到
      现有测试文件但断言失败、exit 4 语法错误、引用不存在文件）→ 阻断注册
      （E028，verify_command 定义可能有误）。
    - ``expected_pending``：依赖实现产物、预期失败（如测试文件尚未由实现者
      创建、断言引用的实现文件不存在）→ 仅提示不阻断。

    启发式判定（简单、可测）：
    - exit_code == 4（pytest usage error，cmd 语法错误）→ assertion_mismatch
    - stderr 含 "ERROR"/"error:" 指向现有文件（tests/ 下的收集错误）→ assertion_mismatch
    - stderr 含 "file not found"/"No such file"/"ModuleNotFoundError"
      （引用不存在文件/模块）→ 缺失路径 ∈ to_be_created（本任务即将创建的
      files_to_edit）→ expected_pending；否则 → assertion_mismatch
    - 其余（如测试运行但断言失败、实现未完成）→ expected_pending

    Args:
        verify_cmd: verify_command 定义。
        exit_code: dry-run 子进程退出码。
        stderr: 子进程 stderr。
        stdout: 子进程 stdout（可空）。
        to_be_created: 本任务 files_to_edit 声明（即将创建的路径集合，可空；
            为空时保持存量行为，向后兼容）。

    Returns:
        "assertion_mismatch" 或 "expected_pending"。
    """
    stderr_l = (stderr or "").lower()

    # 缺失路径/模块信号（pytest 收集错误）。pytest 引用不存在测试文件时真实
    # 输出为 "ERROR: file or directory not found"（且 exit_code==4），"file not
    # found" 子串不匹配它，须单独列出（task-e028-dryrun-exit4-priority）。
    _MISSING_REF_SIGNALS = (
        "filenotfounderror", "nosuchfile", "no such file",
        "file not found", "file or directory not found",
        "can't open file", "modulenotfounderror", "cannot import",
    )

    def _missing_ref() -> bool:
        return any(k in stderr_l for k in _MISSING_REF_SIGNALS)

    def _hit_to_be_created() -> bool:
        """缺失路径 ∈ to_be_created（本任务即将创建的 files_to_edit）判定。

        与旧缺失信号分支共用同一套归一化对齐：小写、反斜杠→斜杠、折叠连续
        分隔符 + 模块名变体（tests/test_x.py ↔ tests.test_x）。
        """
        if not to_be_created:
            return False
        declared: set[str] = {
            _collapse(re.sub(r"/{2,}", "/", str(p).lower().replace("\\", "/")))
            for p in to_be_created
        }
        variants = set(declared)
        for p in list(declared):
            if p.endswith(".py"):
                variants.add(p[:-3].replace("/", "."))
            elif "." in p and "/" not in p:
                variants.add(p.replace(".", "/") + ".py")
        hay = _collapse(
            (stderr_l + " " + (stdout or "").lower()).replace("\\", "/")
        )
        return any(v in hay for v in variants)

    # pytest usage error（cmd 语法错误）——先做 to_be_created 豁免：
    # pytest 引用『本任务将创建的测试文件』时恒返回 exit 4（ERROR: file or
    # directory not found: tests/test_x.py），属预期失败（expected_pending）
    # 而非断言不匹配；纯语法错误或缺失路径不属待创建时仍阻断（assertion_mismatch）。
    if exit_code == 4:
        if _missing_ref() and _hit_to_be_created():
            return "expected_pending"
        return "assertion_mismatch"

    # 引用不存在文件/模块（收集错误）——缺失路径若属于本任务即将创建的
    # files_to_edit，则为 expected_pending（新增测试文件 + verify 引用它），
    # 否则维持 assertion_mismatch（定义引用了本不该缺失的路径）
    if _missing_ref():
        if _hit_to_be_created():
            return "expected_pending"
        return "assertion_mismatch"

    # pytest 收集阶段错误（ERROR at setup/collection，指向现有测试文件）
    if "error" in stderr_l and ("collect" in stderr_l or "setup" in stderr_l):
        return "assertion_mismatch"

    # 其余失败（测试断言失败、实现未完成等）→ 预期失败，不阻断
    return "expected_pending"

# task-errexit-weak-polish-batch: E007 hint polish placeholder
