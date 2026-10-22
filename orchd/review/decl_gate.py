"""review 提交声明门禁段（_review_submit_decl_gate）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.errors import ErrorCode
from orchd.errors import OrchdError


def _review_submit_decl_gate(
    *,
    project_root: Path | None,
    tasks: list[dict[str, Any]],
    task_id: str,
    degraded: list[dict[str, Any]],
) -> None:
    """code APPROVED 前声明文件 diff 门禁（原 L878-927，单条 If）。

    声明文件未进入任务分支 diff 即 E010 拒绝 merge；校验异常按 best-effort 降级但经
    record_degraded_guard 留痕（E030 / degraded_guards），不升级为阻断。
    """
    # task-engine-review-merge-diff-gate：code APPROVED 前校验声明文件已全部
    # 进入任务分支 diff；缺失则拒绝 merge，避免“实现/测试未进分支”被终审放行。
    # task-master-single-copy：与 done 门禁同源改用 diagnose_missing_branch_files
    # （区分“漏提交”与“声明但未改动”），声明未改动的冗余文件不再误拦 merge。
    if project_root:
        try:
            # task-flat-decl-authority：声明经 resolve_declaration_source 解析
            # （flat 下从 main blob 读权威声明，防任务分支本地副本陈旧）。
            from orchd.worktree import resolve_declaration_source
            _tasks = resolve_declaration_source(project_root, tasks, degraded)[0]
            task_map = {t.get("id", ""): t for t in _tasks}
            task_def = task_map.get(task_id) or {}
            from orchd.worktree import diagnose_missing_branch_files

            missing = [
                d["file"] for d in diagnose_missing_branch_files(
                    project_root, task_id, task_def.get("files_to_edit", [])
                )
            ]
            if missing:
                raise OrchdError(
                    ErrorCode.E010,
                    "file_conflict: 声明文件未进入任务分支 diff，拒绝 merge",
                    [{
                        "task_id": task_id,
                        "missing_declared_files": missing,
                        "hint": (
                            "请回任务分支确认这些文件已提交；若无需修改，"
                            "请从 files_to_edit 移除或补充说明"
                        ),
                    }],
                )
        except OrchdError:
            raise
        except Exception as exc:
            # task-degraded-trace-visibility：声明完整性校验未生效不再纯静默
            # pass——经 record_degraded_guard（guard="review_declared_diff"，
            # E030 / warning）写入 degraded 供响应携带（degraded_guards）。
            # 降级语义不变：仍跳过该门禁继续 merge（不升级为阻断）。
            from orchd.gitops.guard import GUARD_STATUS_FAILED, record_degraded_guard

            _err = f"{type(exc).__name__}: {exc}".strip()[:300]
            record_degraded_guard(
                degraded,
                guard_name="review_declared_diff",
                status=GUARD_STATUS_FAILED,
                reason=f"声明文件 diff 校验未生效，已降级跳过：{_err}",
                context={"task_id": str(task_id)},
                error=_err,
                hint=(
                    "声明完整性校验异常，本次 merge 未做声明 diff 门禁；"
                    "请人工确认声明文件已进任务分支后重试"
                ),
            )
