from __future__ import annotations

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store, TaskState, _write_text_atomic
from orchd.spec import (
    Master,
    detect_dir_or_glob_declarations,
    filter_terminal_quality_warnings,
    is_code_task,
    validate_quality,
)
from pathlib import Path
from typing import Any
import json
import re
from .common import _SAFE_MODULE_ID_RE, _validate_module_id
from .validate import _TERMINAL_REVISION_EVENT_REASON

def _amend_persist(
    *,
    orchd_dir: Path,
    tasks: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    master: Master,
    store: Store,
    state: dict[str, TaskState],
    store_root: Path,
    project_root: Path,
    new_tasks: list[str],
    updated_tasks: list[str],
    terminal_text_revisions: list[dict[str, Any]],
    conflict_warnings: list[dict[str, Any]],
    pruned_modules: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """amend 落盘段：质量校验与声明门禁 + 告警 + 快照重写 + 审计（原 L963-1208）。

    返回 (quality_warnings, _exempted_terminal_warnings)。
    """
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
            blocking_tid = ""
            if m and m.group(1).isdigit():
                idx = int(m.group(1))
                if 0 <= idx < len(tasks):
                    is_code = is_code_task(tasks[idx])
                    blocking_tid = str(tasks[idx].get("id") or "")
            if is_code:
                # 场景化 hint（task-guide-placeholder-literal-scan AC3）：阻断
                # 响应必须带真值 task_id 与可执行命令——命令走 guide.amend_patch_cmd
                # 单一生成源（rules/git.md:17，禁手写第二份命令模板），占位词表
                # 的静态 recovery 退为尖括号形态仅作兜底。
                from orchd.guide import amend_patch_cmd

                raise OrchdError(
                    ErrorCode.E022,
                    "注册前质量校验失败：代码类任务缺 verify_command，注册被阻断",
                    [entry | {
                        "blocking": True,
                        "hint": (
                            f"代码类任务 {blocking_tid} 缺少 verify_command：运行 "
                            f"{amend_patch_cmd(blocking_tid, verify='<cmd --basetemp=...>')} "
                            "补齐后重试"
                        ),
                    }],
                )
        quality_warnings.append(entry)

    # 终态任务质量告警豁免（task-amend-quality-warning-terminal-filter，单一真源）：
    # E022 阻断已在上方先行（过滤前生效，语义零变化）；此处只过滤质量类可见性
    # 告警（E023/E026/E027/E029），判据本身零变化。state 为本锁内 replay，
    # 新注册任务不在其中（非终态，不被过滤）。
    try:
        _terminal_ids = {
            tid for tid, ts in state.items()
            if ts.status in TERMINAL_STATUSES
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
    # task-pass9-amend-response-ux（pass9 UX-2）：按形态分流强提示——
    # tests/test_*.py 形态大概率是待创建的新测试文件（可忽略）；其余路径
    # 不存在且不像新测试文件 → 独立 typo_suspect 类（防笔误：实测
    # task-pass9-union-verify-gate 注册期路径笔误直到 E020 提交被拦才暴露）。
    # 不做硬阻断：合法新源文件声明与笔误结构上不可区分。
    for task in tasks:
        tid = task.get("id", "")
        if tid not in _changed_ids:
            continue
        for field in ("files_to_edit", "exempt_files"):
            for fp in task.get(field, []):
                full = project_root / fp
                if full.exists():
                    continue
                is_test_like = _is_new_test_file_path(fp)
                conflict_warnings.append({
                    "task_id": tid,
                    "type": (
                        f"{field}_new_test_file_pending" if is_test_like
                        else f"{field}_path_typo_suspect"
                    ),
                    "file": fp,
                    "message": (
                        f"{field} 声明的路径 '{fp}' 在项目中不存在。"
                        + (
                            "新测试文件形态（tests/test_*.py）：实现时创建即可，可忽略。"
                            if is_test_like else
                            "路径不存在且不像新测试文件——请核对是否笔误"
                            "（若确为待创建的新源文件可忽略）。"
                        )
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

    # task-pass9-verify-path-guard（pass9 实测）：verify_command 内引用的 .py
    # 路径不存在且不在本任务声明集 → typo_suspect 强提示。背景：addopts 强制
    # xdist（-n auto）下，pytest/ruff 参数含不存在路径时 usage error 被整吞
    # （exit 5 "no tests ran"，-n0 对照 exit 4 可见），笔误只能靠 done 失败
    # 反推且报因不可读——注册期静态核对是唯一提前点。已声明路径（待创建的
    # 新文件）不告警；不做硬阻断：合法新源文件与笔误结构上不可区分。
    for task in tasks:
        tid = task.get("id", "")
        if tid not in _changed_ids:
            continue
        _verify_cmd = task.get("verify_command") or ""
        if not _verify_cmd:
            continue
        _declared_paths = (
            set(task.get("files_to_edit", []) or [])
            | set(task.get("exempt_files", []) or [])
        )
        for _tok in _verify_cmd.split():
            if not _tok.endswith(".py"):
                continue
            if "$" in _tok or "=" in _tok or _tok.startswith("-"):
                continue  # 变量替换 / 内联旗标 / 选项不是文件路径
            _ref = _tok.strip("\"'")
            if (project_root / _ref).exists() or _ref in _declared_paths:
                continue
            conflict_warnings.append({
                "task_id": tid,
                "type": "verify_command_path_typo_suspect",
                "file": _ref,
                "message": (
                    f"verify_command 引用的路径 '{_ref}' 在项目中不存在且不在"
                    "本任务声明中——请核对是否笔误（addopts 强制 xdist 下该"
                    "错误会被整吞为 'no tests ran'；若确为待创建的新文件可忽略）。"
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
        _write_text_atomic(
            spec_path,
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        )

    # task-amend-orphan-prune：清除已删 module 的孤儿 mod-*/spec.json 快照目录。
    # 根因：快照重写只写当前 modules，历史已删 module 的目录从不清理；其 tasks
    # 仍被下一次 amend 的 existing_tasks（mod-*/spec.json 全量 glob）读入，
    # removed_tasks 每次重复误报（非幂等，如 init 默认的 task-sample 孤儿）。
    # 范围：仅 store_root 直接子级、目录名匹配 mod-* 且不在当前 master modules。
    # 方式：仅删派生 spec.json，目录仅在其为空时 rmdir（非空留痕保留，下次重试）；
    # 无 spec.json 的 mod-* 目录不入列（不误删用户数据）。
    master_module_ids = {str(m.get("id", "")) for m in modules}
    for orphan_spec in sorted(store_root.glob("mod-*/spec.json")):
        orphan_dir = orphan_spec.parent
        orphan_id = orphan_dir.name
        if orphan_dir.parent != store_root or orphan_id in master_module_ids:
            continue
        if not _SAFE_MODULE_ID_RE.fullmatch(orphan_id):
            continue
        try:
            orphan_spec.unlink()
        except OSError:
            continue
        try:
            orphan_dir.rmdir()  # 仅空目录（R-16：失败禁止升级 rmtree）
        except OSError:
            pass
        pruned_modules.append(orphan_id)

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
    return quality_warnings, _exempted_terminal_warnings

def _is_new_test_file_path(fp: str) -> bool:
    """声明路径是否为「待创建新测试文件」形态（task-pass9-amend-response-ux）。

    tests/test_*.py 形态的缺失路径大概率是实现期才创建的新测试文件（惯例
    「一个测试文件一个域」），存在性告警归为可忽略；其余形态走 typo_suspect
    强提示。仅做形态判断，不做盘上校验。
    """
    normalized = fp.replace("\\", "/")
    return normalized.startswith("tests/test_") and normalized.endswith(".py")
