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
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_current_branch, get_default_branch, list_tracked_changes
from orchd.ledger import Store, resolve_store_dir
from orchd.spec import (
    Master,
    is_code_task,
    validate_quality,
    validate_references,
    validate_source,
    validate_structure,
)

_SAFE_MODULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
_INTAKE_PRODUCT_FILES = frozenset({
    ".orchd/_master.json",
    "IDEAS.md",
    ".orchd/IDEAS.md",
    "ROADMAP.md",
    ".orchd/ROADMAP.md",
})


# M-2（2026-08-12 全面审计）：三处状态（claimed / done / in_review / 终态附加）
# 共用同一"附加字段"白名单，避免 exempt_files / verify_timeout_seconds 在部分
# 阶段被误拦导致语义不连贯。
_AMEND_ATTACHABLE_FIELDS = frozenset({
    "exempt_files", "verify_command", "verify_timeout_seconds", "reviewers",
    # Bug #20c（2026-08-27）：files_to_edit / files_to_read 加入白名单，
    # claimed 状态可修正无效路径（无需 force-status 回退 pending）。
    "files_to_edit", "files_to_read",
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


def amend(orchd_dir: Path, master: Master, store: Store) -> dict[str, Any]:
    """增量更新 snapshot：按状态约束矩阵过滤变更。

    约束矩阵：
    - pending：可改全部字段
    - claimed：仅允许修改 verify_command / reviewers 白名单字段，其余拒绝（E007）
    - done / in_review（review 组）：仅允许修改 reviewers
    - completed / cancelled：拒绝

    review 阶段归一化比对：对于 done / in_review 状态的任务，先将新定义的
    reviewers 字段还原为旧值，再与旧定义做全量比对；若相等则说明仅有
    reviewers 发生了变更，允许通过；否则拒绝（提示"仅允许修改 reviewers"）。

    新增任务始终允许。删除任务不阻止，但在摘要中报告 removed_tasks。

    红线 7（task-redline7-amend-refuse，roadmap:constraint-hardening）：
    amend 只在 default（main）分支执行。在非 main 分支上调用 amend 直接
    拒绝注册（抛出 E007），而非降级为"仅不提交"——否则任务分支内仍会
    注册任务并改写 master，污染 master 注册来源。分支判定仅在 amend 阶段
    强制：init / validate / status 等只读或冷启动命令不受限。

    Returns:
        变更摘要。
    """
    orchd_dir = Path(orchd_dir)
    tasks = master.tasks
    modules = master.modules

    # 红线 7：非 main 分支拒绝 amend 注册（best-effort 降级已移除）
    project_root = orchd_dir.parent
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_branch: amend 仅在 default（main）分支执行，"
            f"当前分支 {current_branch} 拒绝注册（红线 7）",
            [{"branch": current_branch, "default": default_branch}],
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

    # task-intake-file-lock（AC1/AC3）：准入写锁 + 提交前 HEAD 推进检测。
    # 准入写（改 _master.json / IDEAS.md / ROADMAP.md）受独立 .intake.lock 串行，
    # 不复用账本锁（避免一次 amend 阻塞并行 claim/done）；HEAD 漂移检测发现
    # base 被并行推进则拒绝注册（git 层 TOCTOU）。两者 best-effort。
    intake_lock: dict[str, Any] | None = None
    drift_checked = False
    try:
        from orchd.gitops import head_drift_check
        from orchd.ledger import intake_lock_acquire, intake_lock_release

        if orchd_dir is not None:
            from orchd.ledger import resolve_agent_id

            intake_lock = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
        # 提交前 HEAD 推进检测：main 被并行推进则拒绝（AC3）
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
        drift_checked = True
    except OrchdError:
        raise
    except Exception:
        # 准入锁/HEAD 检测属 best-effort：失败不阻断 amend 本身
        drift_checked = False

    store.acquire_lock()
    try:
        state = store.replay()

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
                source = task.get("source")
                if not source or not isinstance(source, str) or not source.strip():
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
                    updated_tasks.append(tid)
                    whitelisted_updates.append({
                        "task_id": tid,
                        "fields": sorted(changed_fields),
                    })
                else:
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            "claimed task only allows whitelist fields "
                            f"{sorted(_CLAIMED_WHITELIST_FIELDS)}, "
                            f"got {sorted(changed_fields)}"
                        ),
                    })
            elif status in ("completed", "cancelled"):
                # task-amend-terminal-drift-repair：终态任务 master≠snapshot 时，
                # 区分"合法附加字段增量"与"核心字段变更"。附加字段白名单 → 自动
                # 以 master 为准同步 snapshot（不报 E007）；含核心字段 → 仍 E007。
                changed_fields = {
                    key
                    for key in set(task) | set(old_task)
                    if task.get(key) != old_task.get(key)
                }
                if changed_fields and changed_fields <= _TERMINAL_ATTACHABLE_FIELDS:
                    updated_tasks.append(tid)
                    attachable_sync.append({
                        "task_id": tid,
                        "status": status,
                        "fields": sorted(changed_fields),
                    })
                else:
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": f"{status} 为终态，不可修改",
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
                    errors.append({
                        "task_id": tid,
                        "status": status,
                        "message": (
                            "review 阶段仅允许修改附加字段 "
                            "(reviewers / verify_command / exempt_files / "
                            "verify_timeout_seconds)；检测到其他字段同时被修改"
                        ),
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
                m = re.match(r"\$\.tasks\[(\d+)\]\.source$", v.path)
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

        # Bug #20a（2026-08-27）：files_to_edit / exempt_files 路径存在性校验。
        # 摄入时检测声明了但不存在的路径，写入 conflict_warnings 供人工核对。
        # 不硬阻断（路径可能是待创建的新文件），仅告警。
        for task in tasks:
            tid = task.get("id", "")
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
    finally:
        store.release_lock()

    # task-intake-file-lock：finally 释放准入写锁（异常路径也释放，防残留卡死后续准入）
    if intake_lock is not None:
        try:
            intake_lock_release(intake_lock)
        except Exception:
            pass

    return {
        "amended": True,
        "new_tasks": new_tasks,
        "updated_tasks": updated_tasks,
        "whitelisted_updates": whitelisted_updates,
        "attachable_sync": attachable_sync,
        "unchanged_tasks": unchanged_tasks,
        "removed_tasks": removed_tasks,
        "quality_warnings": _annotate_if_needed(quality_warnings, orchd_dir),
        "conflict_warnings": conflict_warnings,
        "sources_missing": sources_missing,
        "sources_invalid": sources_invalid,
    }


def classify_dry_run_failure(
    verify_cmd: str,
    exit_code: int,
    stderr: str,
    stdout: str = "",
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
      （引用不存在文件/模块）→ 若为 pytest 收集错误 → assertion_mismatch
    - 其余（如测试运行但断言失败、实现未完成）→ expected_pending

    Args:
        verify_cmd: verify_command 定义。
        exit_code: dry-run 子进程退出码。
        stderr: 子进程 stderr。
        stdout: 子进程 stdout（可空）。

    Returns:
        "assertion_mismatch" 或 "expected_pending"。
    """
    stderr_l = (stderr or "").lower()

    # pytest usage error（cmd 语法错误）——定义本身有问题，阻断
    if exit_code == 4:
        return "assertion_mismatch"

    # 引用不存在文件/模块（收集错误）——定义引用了不存在的路径
    if any(k in stderr_l for k in (
        "filenotfounderror", "nosuchfile", "no such file",
        "file not found", "modulenotfounderror", "cannot import",
    )):
        return "assertion_mismatch"

    # pytest 收集阶段错误（ERROR at setup/collection，指向现有测试文件）
    if "error" in stderr_l and ("collect" in stderr_l or "setup" in stderr_l):
        return "assertion_mismatch"

    # 其余失败（测试断言失败、实现未完成等）→ 预期失败，不阻断
    return "expected_pending"

# task-errexit-weak-polish-batch: E007 hint polish placeholder
