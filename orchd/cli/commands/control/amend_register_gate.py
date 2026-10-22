"""amend 注册闸门段（_amend_register_gate），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import structured_error


def _amend_register_gate(
    *, orchd_dir: Path, master, store, project_root: Path,
    revise_terminal: str | None,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """amend 注册门禁段：变更集预计算 + 无快照 fail-fast + dry-run + 质量门禁（原 L823-1036）。

    预计算将变更任务（store_root/mod-*/spec.json 存量定义 vs master）；无快照且账本
    非空 → E007 missing_snapshot 阻断；纯文本修订任务剔出 dry-run 与存量告警集合；
    逐任务 dry-run verify_command（E028 assertion_mismatch 阻断，expected_pending 仅
    提示）；E024/E027 仅对 changed 任务阻断注册（grandfather：存量命中不阻断）。
    返回 changed / dry_run_results / dry_run_skipped。
    """
    import json as _json
    import subprocess

    from orchd.ledger import resolve_store_dir
    from orchd.onboard import _decode_subprocess_output
    from orchd.split import is_text_only_spec_revision

    # task-e028-dryrun-exit4-priority：dry-run 前置到写入/提交之前。
    # 原实现先 amend()（写 snapshot + amend 自动 commit）再 dry-run，E028 阻断时
    # snapshot/commit 已实际完成，错误文案「注册已阻断」与真实副作用不符（实测
    # ae2f9a2 已提交入库）。现改为：预计算将变更任务 → 先 dry-run 校验 → 阻断则
    # raise（此时未写入）；通过后再 amend() 写入 + 自动提交。
    from orchd.split import classify_dry_run_failure
    from orchd.spec import validate_quality, verify_command_dangerous_reasons
    from orchd.subproc import run_shell

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
        try:
            proc = run_shell(verify_cmd, str(project_root), 30)
            failure_class = None
            if proc.returncode != 0:
                # E028 误判修复（task-fix-e028-dryrun-created-file）：缺失路径若属于
                # 本任务 files_to_edit（即将创建）→ expected_pending 仅提示不阻断
                _to_be_created = set(
                    task_map.get(tid, {}).get("files_to_edit", []) or [])
                failure_class = classify_dry_run_failure(
                    verify_cmd,
                    proc.returncode,
                    _decode_subprocess_output(proc.stderr)[:500],
                    _decode_subprocess_output(proc.stdout)[:300],
                    _to_be_created,
                )
                if failure_class == "assertion_mismatch":
                    _msg28 = (
                        "dry-run 断言不匹配（assertion_mismatch）：verify_command 引用现有文件但"
                        "断言失败/语法错误，注册已阻断（E028）"
                    )
                    _details28 = [{
                        "task_id":
                        tid,
                        "verify_command":
                        verify_cmd,
                        "exit_code":
                        proc.returncode,
                        "project_root":
                        str(project_root),
                        "cwd":
                        str(Path.cwd()),
                        "stderr":
                        _decode_subprocess_output(proc.stderr)[:500]
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
                        proc.returncode,
                        "stderr":
                        _decode_subprocess_output(proc.stderr)[:500],
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
                proc.returncode == 0,
                "exit_code":
                proc.returncode,
                "failure_class":
                failure_class,
                "stderr":
                _decode_subprocess_output(proc.stderr)[:500],
                "hint": ("dry-run 仅提示不阻断注册：实现未完成时失败属预期（可忽略）；"
                         "若断言应匹配现有文件而失败，则 verify_command 定义可能有误，建议核对"),
            })
        except (subprocess.SubprocessError, OSError):
            # 运行环境异常：静默跳过，不影响 amend 主流程
            continue

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

    return changed, dry_run_results, dry_run_skipped
