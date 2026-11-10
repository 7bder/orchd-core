"""amend 注册闸门段（_amend_register_gate），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd import deps as _deps
from orchd.errors import ErrorCode, OrchdError
from orchd.guards import MODE_HARD
from orchd.guards.deps import (
    GATE_HARD,
    GATE_WARN,
    REASON_GRAPH_UNAVAILABLE,
    decide_gate,
)
from orchd.ledger import structured_error
from .declarations import (
    _declared_paths,
    _normalize_decl_path,
    _serial_depends_on,
    _surface_admission_gate,
    _surface_hit,
    _surface_refresh_gate,
)


_MIGRATION_MARKERS = (
    "conventions.md", "contracts/", "schema/",
)
_MIGRATION_AC_KEYWORD = "消费方断言同步"

# E028 dry-run 失败类的专属文案（task-feedback-verify-win-shell / -dryrun-cmd-internal-error）。
# 抽为模块级常量：_amend_register_gate 保持 <=300 行预算，新增失败类只扩表不涨函数体。
_E028_MESSAGE_BY_CLASS = {
    "assertion_mismatch": (
        "dry-run 断言不匹配（assertion_mismatch）：verify_command 引用"
        "现有文件但断言失败，注册已阻断（E028）"
    ),
    "usage_error": (
        "dry-run 参数/插件错误（usage_error）：verify_command 含无法"
        "识别的参数或缺少测试插件，注册已阻断（E028）"
    ),
    "invalid_shell": (
        "dry-run shell 语法错误（invalid_shell）：verify_command 被截断"
        "或引号不平衡，注册已阻断（E028）"
    ),
    "internal_error": (
        "dry-run 引擎内部错误（internal_error）：pytest INTERNALERROR"
        "（常见于 cmd 通道截断 --basetemp），注册已阻断（E028）"
    ),
}
_E028_HINT_BY_CLASS = {
    "assertion_mismatch": (
        "核对断言/引用文件；确认是待创建文件请补 files_to_edit"
    ),
    "usage_error": (
        "verify_command 含无法识别的参数或缺少测试插件（如 "
        "pytest-xdist）：先 `python -m pytest --help` 核对参数并"
        "安装插件；Windows 下可改用 --verify-command-file"
    ),
    "invalid_shell": (
        "verify_command 有 shell 语法错误或被截断（常见于 "
        "PowerShell 吃掉双引号）：检查引号平衡，Windows 下改用 "
        "--verify-command-file 传参"
    ),
    "internal_error": (
        "pytest 报 INTERNALERROR（常见于 cmd 通道截断 --basetemp）："
        "核对命令是否完整、临时目录是否可创建；Windows 下改用 "
        "--verify-command-file 传参"
    ),
}


def _register_precheck_warnings(
    master, changed: list[str], project_root: Path,
) -> list[dict[str, Any]]:
    """注册期依赖边硬门（E042）：未登记边阻断注册；迁移类 AC 缺同步仅告警。

    task-register-precheck-guidance 的 warning 语义在 task-deps-hard-gate 收紧：
    删去「已登记模块新增边 continue 吞边」后，任何不在 deps.json hard/delayed 的
    边（含存量模块的新增边）都阻断注册（此时尚未写 snapshot/commit）。逃生口：
    ① 任务 files_to_edit 声明 ``.orchd/shared/deps.json`` —— 登记意图随该任务
    提交，注册期放行并留 deferred 告警；② 文件级 exempt_files 不参与扫描。
    宿主缺 deps.json 跳过、坏图按仓角色与档位分流；档位判定与 done 门禁 / 宿主
    guard 共用 ``orchd.guards.deps.decide_gate``（引擎仓恒 ``hard``，宿主取
    ``guards.json`` 的 ``deps.mode``）：引擎仓坏图硬阻断（reason
    ``deps_graph_unavailable``），宿主坏图 ``warn`` 只告警、``hard`` 阻断、
    ``off`` 跳过。扫描面（``deps`` / ``root`` / ``package``）与 ``orchd deps``
    子命令同一解析器。
    """
    decision = decide_gate(project_root)
    policy = decision.policy
    mode = decision.mode
    graph: _deps.DepsGraph | None = decision.graph
    warnings: list[dict[str, Any]] = []
    if decision.status == GATE_HARD:
        # 引擎仓坏图 fail-closed：不再以 warning 放行（旧实现的最大缺口）。
        raise OrchdError(
            ErrorCode.E007,
            f"deps_graph_unavailable: {decision.message}",
            [{
                "code": ErrorCode.E007.name,
                "reason": REASON_GRAPH_UNAVAILABLE,
                "path": str(project_root / policy.deps_rel),
                "mode": mode,
                "is_engine": decision.is_engine,
                "message": decision.message,
                "hint": (
                    "修复或删除坏 deps.json 后重试：引擎仓坏图必须 fail-closed；"
                    "宿主可修复图，或把 guards.deps 调为 warn/off"
                ),
            }],
        )
    if decision.status == GATE_WARN:
        warnings.append({
            "type": REASON_GRAPH_UNAVAILABLE,
            "path": str(project_root / policy.deps_rel),
            "mode": mode,
            "message": (
                f"{decision.message}；本次跳过未登记边预检，注册不受影响"
            ),
        })
    root_prefix = Path(policy.root).as_posix().rstrip("/") + "/"
    task_map = {t.get("id", ""): t for t in master.tasks}
    blocking: list[dict[str, Any]] = []
    for tid in changed:
        task = task_map.get(tid) or {}
        files = [str(f) for f in (task.get("files_to_edit") or [])]
        exempt = [str(f) for f in (task.get("exempt_files") or [])]
        new_modules = [
            f for f in files
            if Path(f).as_posix().startswith(root_prefix) and f.endswith(".py")
            and (project_root / f).is_file()
        ]
        if new_modules and graph is not None:
            missing = _deps.undeclared_edges_for_files(
                new_modules, project_root=project_root, graph=graph,
                exclude=exempt, root=policy.root, package=policy.package,
            )
            deferred = any(
                Path(f).as_posix() == policy.deps_rel for f in files
            )
            for owner, edges in sorted(missing.items()):
                edge_list = [f"{owner} -> {edge}" for edge in edges]
                if deferred:
                    warnings.append({
                        "type": "deps_edge_deferred_to_task",
                        "task_id": tid,
                        "module": owner,
                        "count": len(edges),
                        "edges": edge_list,
                        "message": (
                            f"任务 {tid} 声明了 {policy.deps_rel}："
                            f"{owner} 的 {len(edges)} 条边由本任务提交登记，注册期放行"
                        ),
                    })
                    continue
                if mode != MODE_HARD:
                    warnings.append({
                        "type": "deps_edge_undeclared",
                        "task_id": tid,
                        "module": owner,
                        "count": len(edges),
                        "edges": edge_list,
                        "message": (
                            f"模块 {owner} 有 {len(edges)} 条依赖边未登记"
                            f"（宿主 deps guard 为 {mode} 档）：注册放行，请整改"
                        ),
                    })
                    continue
                blocking.append({
                    "code": ErrorCode.E042.name,
                    "task_id": tid,
                    "module": owner,
                    "count": len(edges),
                    "edges": edge_list,
                    "message": (
                        f"模块 {owner} 有 {len(edges)} 条依赖边未登记："
                        f"{owner} -> {edges[0]} 等；注册已阻断（未写入 snapshot）"
                    ),
                    "hint": (
                        f"在 {policy.deps_rel} 的 hard/delayed 登记这些边"
                        "（顶层 import 归 hard、函数内延迟 import 归 delayed），"
                        "并把该文件声明进本任务 files_to_edit；"
                        "或把不属本任务的路径加入 exempt_files"
                    ),
                })
        migration = [
            f for f in files
            if any(marker in Path(f).as_posix() for marker in _MIGRATION_MARKERS)
        ]
        ac_text = chr(10).join(
            str(item) for item in (task.get("acceptance_criteria") or []))
        if migration and _MIGRATION_AC_KEYWORD not in ac_text:
            warnings.append({
                "type": "migration_ac_missing",
                "task_id": tid,
                "files": migration,
                "message": (
                    "迁移类真源变更（" + ", ".join(migration)
                    + "）的验收标准未含「消费方断言同步」：请补充消费方断言同步要求"
                ),
            })
    if blocking:
        raise OrchdError(
            ErrorCode.E042,
            "deps_edge_undeclared: 注册期检测到未登记依赖边"
            "（注册已阻断，未写入 snapshot）",
            blocking,
        )
    return warnings




def _amend_register_gate(
    *, orchd_dir: Path, master, store, project_root: Path,
    revise_terminal: str | None, scope_task: str | None = None,
    allow_shared_write: list[str] | None = None,
    surface_reason: str | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """amend 注册门禁段：变更集预计算 + 无快照 fail-fast + dry-run + 质量门禁（原 L823-1036）。

    预计算将变更任务（store_root/mod-*/spec.json 存量定义 vs master）；无快照且账本
    非空 → E007 missing_snapshot 阻断；纯文本修订任务剔出 dry-run 与存量告警集合；
    逐任务 dry-run verify_command（E028：assertion_mismatch / usage_error /
    invalid_shell 阻断，expected_pending 仅提示）；E024/E027 仅对 changed 任务阻断
    注册（grandfather：存量命中不阻断）。
    返回 changed / dry_run_results / dry_run_skipped / register_warnings。
    """
    import json as _json
    import subprocess

    from orchd.ledger import resolve_store_dir
    from orchd.split import is_text_only_spec_revision

    # task-e028-dryrun-exit4-priority：dry-run 前置到写入/提交之前。
    # 原实现先 amend()（写 snapshot + amend 自动 commit）再 dry-run，E028 阻断时
    # snapshot/commit 已实际完成，错误文案「注册已阻断」与真实副作用不符（实测
    # ae2f9a2 已提交入库）。现改为：预计算将变更任务 → 先 dry-run 校验 → 阻断则
    # raise（此时未写入）；通过后再 amend() 写入 + 自动提交。
    from orchd.spec import validate_quality, verify_command_dangerous_reasons
    from orchd.spec.verify_preview import (
        BLOCKING_FAILURE_CLASSES,
        preview_verify_command,
    )

    # 预计算将变更任务（与 split.amend 内部 existing_tasks 加载同源）：
    # store_root/mod-*/spec.json 的存量定义 vs master，值差异即视为将变更。
    store_root = Path(resolve_store_dir(orchd_dir))
    existing_tasks: dict[str, dict[str, Any]] = {}
    for spec_path in sorted(store_root.glob("mod-*/spec.json")):
        snapshot = _json.loads(spec_path.read_text(encoding="utf-8"))
        for t in snapshot.get("tasks", []):
            existing_tasks[t.get("id", "")] = t
    changed = [
        t.get("id", "") for t in master.tasks
        if t.get("id", "") not in existing_tasks
        or existing_tasks.get(t.get("id", "")) != t
    ]
    # task-feedback-amend-scope：--task 补丁路径只对目标任务做 dry-run /
    # E024/E027 质量门。scope 外任务的 snapshot 漂移不应触发它们的 verify
    # （否则无关终态卡的坏 verify_command 仍会阻断本次补登）。
    if scope_task is not None:
        changed = [tid for tid in changed if tid == scope_task]

    # N1（task-latent-f7f8n1）：无快照 amend fail-fast。快照（mod-*/spec.json）
    # 是 gitignored 派生数据，不随 clone 走；无快照时全量任务被判新并逐个
    # dry-run（580 任务 × 30s = 马拉松，演练实证）。此时直接 E007 阻断并指引，
    # 不进入 dry-run；空 master（无任务）不受影响。
    if master.tasks and not list(store_root.glob("mod-*/spec.json")):
        _ledger_lines = 0
        try:
            if store.ledger_exists():
                _ledger_lines = store.ledger_line_count()
        except Exception:
            _ledger_lines = 0
        if _ledger_lines > 0:
            raise OrchdError(
                ErrorCode.E007,
                "missing_snapshot: 未找到任何 mod-*/spec.json 快照，但账本非空 "
                "（快照疑似被手动删除，违反红线 #9 不得手改运行时文件），"
                "amend 已阻断，未写入任何内容",
                [{
                    "reason": "missing_snapshot",
                    "store_root": str(store_root),
                    "ledger_lines": _ledger_lines,
                    "hint": ("快照为派生数据但账本有状态，不可 init 覆盖；"
                             "请按 rules/recovery.md 上报处置（doctor 诊断），"
                             "不要手动重建快照"),
                }],
            )
        raise OrchdError(
            ErrorCode.E007,
            "missing_snapshot: 未找到任何 mod-*/spec.json 快照，无法判定变更集 "
            "（快照不入库，clone 后须先 init 重建，否则全量任务会被误判为新任务 "
            "并逐个 dry-run），amend 已阻断，未写入任何内容",
            [{
                "reason": "missing_snapshot",
                "store_root": str(store_root),
                "task_count": len(master.tasks),
                "hint": ("请先执行 python .orchd/__main__.py init 重建快照后重试 "
                         "amend"),
            }],
        )

    surface_warnings = _surface_admission_gate(
        master=master, existing_tasks=existing_tasks,
        project_root=project_root, store=store,
        allow_shared_write=allow_shared_write,
        surface_reason=surface_reason, scope_task=scope_task,
        # 注册期 exempt_files 是「越界提交豁免」而写集声明只在 files_to_edit：
        # serial_order 信封必须原样透传 optional 字段，不能被当成整份片段面声明。
        exempt_policy="amend_only",
    )
    surface_warnings += _surface_refresh_gate(
        master=master, project_root=project_root, scope_task=scope_task)

    # task-terminal-spec-revision-channel：纯文本修订不改 verify_command → 重跑 dry-run
    # 无信息量；且目标任务的**存量** E024/E027（历史定义缺 --basetemp / 含不安全段）
    # 会把这次文本修订整体阻断，使通道对历史终态任务不可用。故把"仅文本修订"的目标
    # 任务同时剔出 dry-run 与存量告警阻断集合（只影响开关指向的那一个任务）。
    dry_run_skipped: list[str] = []
    if revise_terminal is not None:
        old_def = existing_tasks.get(revise_terminal)
        new_def = next(
            (t for t in master.tasks if t.get("id") == revise_terminal),
            None,
        )
        if (old_def is not None and new_def is not None
                and is_text_only_spec_revision(old_def, new_def)):
            dry_run_skipped.append(revise_terminal)
    dry_run_changed = [tid for tid in changed if tid not in dry_run_skipped]

    # dry-run 试跑将变更任务的 verify_command（与 done 相同 shell 执行、同 cwd、
    # 限时 30s；2026-08-08 升级：assertion_mismatch 类失败阻断注册（E028），
    # E024/E027（缺 basetemp / 不安全段）阻断注册；expected_pending 仅提示）
    task_map = {t.get("id", ""): t for t in master.tasks}
    dry_run_results: list[dict[str, Any]] = []
    blocking_errors: list[dict[str, Any]] = []
    for tid in dry_run_changed:
        verify_cmd = task_map.get(tid, {}).get("verify_command")
        if not verify_cmd:
            continue
        _dangerous = verify_command_dangerous_reasons(verify_cmd)
        if _dangerous:
            blocking_errors.append({
                "code":
                ErrorCode.E027.name,
                "task_id":
                tid,
                "verify_command":
                verify_cmd,
                "reasons":
                _dangerous,
                "message": ("verify_command 含 shell 注入风险，dry-run 拒绝执行（E027）"),
            })
            continue
        # task-engine-defect-fix-unified-u7 AC1/AC3：预演收敛到单一实现
        # （orchd.spec.verify_preview）。TimeoutExpired 不再被 except 静默吞掉——
        # 记 classification/reason=timeout 并保留在结果列表中，任务不会从
        # verify_dry_run 里消失；不安全命令已在上方 E027 分支提前 continue。
        _preview = preview_verify_command(
            verify_cmd,
            project_root,
            files_to_edit=task_map.get(tid, {}).get("files_to_edit") or [],
        )
        failure_class = _preview.get("classification")
        if _preview.get("ok"):
            failure_class = None
        if failure_class in BLOCKING_FAILURE_CLASSES:
            # task-feedback-verify-win-shell：rc=4 纯 usage error 与
            # bash syntax error 不再伪装成「断言不匹配」，各自给专属文案。
            _msg28 = _E028_MESSAGE_BY_CLASS[failure_class]
            _hint28 = _E028_HINT_BY_CLASS[failure_class]
            _details28 = [{
                "task_id":
                tid,
                "verify_command":
                verify_cmd,
                "exit_code":
                _preview.get("exit_code"),
                "failure_class":
                failure_class,
                "reason":
                f"dry_run_{failure_class}",
                "hint":
                _hint28,
                "project_root":
                str(project_root),
                "cwd":
                str(Path.cwd()),
                "stderr":
                _preview.get("stderr", ""),
            }]
            _resp28 = structured_error("E028", _msg28, _details28,
                                       project_root)
            _err28 = _resp28.get("error", {})
            _guid28 = _resp28.get("guidance")
            blocking_errors.append({
                "code":
                _err28.get("code", "E028"),
                "task_id":
                tid,
                "verify_command":
                verify_cmd,
                "exit_code":
                _preview.get("exit_code"),
                "stderr":
                _preview.get("stderr", ""),
                "failure_class":
                failure_class,
                "reason":
                f"dry_run_{failure_class}",
                "hint":
                _hint28,
                "message":
                _err28.get("message", _msg28),
                "details":
                _err28.get("details", _details28),
                "guidance":
                _guid28,
                "severity":
                _err28.get("severity", "error"),
            })
        dry_run_results.append({
            "task_id":
            tid,
            "ok":
            bool(_preview.get("ok")),
            "exit_code":
            _preview.get("exit_code"),
            "failure_class":
            failure_class,
            "reason":
            _preview.get("reason"),
            "stderr":
            _preview.get("stderr", ""),
            # task-debug-dryrun-classify-order：stdout 必须与 stderr 同批回显
            # ——pytest 的 "no tests ran in 0.00s" 只出现在 stdout，缺它则
            # failure_class 的判定依据对 agent 不可见（现象：exit 4 + 缺失路径
            # 却报 no_tests_collected，看不出为什么）。
            "stdout":
            _preview.get("stdout", ""),
            "hint": (_preview.get("hint") or
                     "dry-run 仅提示不阻断注册：实现未完成时失败属预期（可忽略）；"
                     "若断言应匹配现有文件而失败，则 verify_command 定义可能有误，建议核对"),
        })

    # E024/E027 阻断：amend 注册点对新增/变更任务的 verify_command 质量校验
    # （E024 缺 basetemp / E027 不安全段 → 阻断注册，不再等 done 期 E014）
    # AC4 grandfather：仅收集 changed 任务（或新注册任务）的 E024/E027，
    # 存量任务命中仅 warning 不阻断——避免存量 E027/E024 阻塞任何后续 amend。
    qerrors = validate_quality(master)
    changed_set = set(dry_run_changed)
    for qe in qerrors:
        if qe.code in (ErrorCode.E024, ErrorCode.E027):
            # 从 path（$.tasks[i].verify_command）解析任务 index → 定位 task id
            import re as _re
            m_idx = _re.search(r"\$\.tasks\[(\d+)\]", qe.path)
            tid = None
            if m_idx:
                idx = int(m_idx.group(1))
                if 0 <= idx < len(master.tasks):
                    tid = master.tasks[idx].get("id")
            # grandfather：仅 changed 任务阻断，存量（未变更）命中跳过
            if tid is not None and tid in changed_set:
                blocking_errors.append({
                    "code":
                    qe.code.name,
                    "path":
                    qe.path,
                    "message":
                    f"task '{tid}': {qe.message}",
                })

    if blocking_errors:
        # dry-run 前置：此时 amend 尚未执行，snapshot/commit 均未写入，
        # 「阻断注册」与真实副作用一致（task-e028-dryrun-exit4-priority）。
        raise OrchdError(
            ErrorCode.E028 if any(
                e.get("code") == "E028"
                for e in blocking_errors) else ErrorCode.E027,
            "amend_blocked: verify_command 校验未通过（注册已阻断，未写入 snapshot）",
            blocking_errors,
        )

    register_warnings = surface_warnings + _register_precheck_warnings(
        master, changed, project_root)
    return changed, dry_run_results, dry_run_skipped, register_warnings
