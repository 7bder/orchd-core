from __future__ import annotations

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store, TaskState, resolve_store_dir
from orchd.spec import Master, validate_references, validate_structure
from pathlib import Path
from typing import Any
import json
from .common import _git_head_sha
from .validate import validate_terminal_revision

def _amend_admission(
    *,
    orchd_dir: Path,
    project_root: Path,
    tasks: list[dict[str, Any]],
    master: Master,
    store: Store,
    intake_lock: dict[str, Any] | None,
    revise_terminal: str | None,
    reason: str | None,
    expected_head: str | None,
) -> tuple[dict[str, TaskState], str | None, Path, dict[str, dict[str, Any]]]:
    """amend 账本准入段：持锁后 CAS / 修订开关前置校验 / 结构校验 / 载入存量（原 L538-609）。

    返回 (state, revision_reason, store_root, existing_tasks)。
    """
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
                [{"reason": "stale_base",
                  "expected_head": expected_head, "head_now": _head_now}],
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
        if target_status not in TERMINAL_STATUSES:
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: --revise-terminal 仅适用于终态任务"
                f"（completed/cancelled），任务 {revise_terminal} 当前状态为 "
                f"{target_status}",
                [{
                    "reason": "revise_terminal_not_terminal",
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
    return state, revision_reason, store_root, existing_tasks
