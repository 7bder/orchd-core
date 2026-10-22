"""完成指引与 reviewer rerun 命令（_build_completion_guidance/build_reviewer_rerun_command）。"""

from __future__ import annotations

from typing import Any

from orchd.guide import read_for


def _build_completion_guidance(
    task_id: str, worktree_recycled: dict[str, Any] | None,
    main_worktree: str | None = None,
) -> dict[str, Any]:
    """构造 code APPROVED + merge 成功后的完成态 guidance（AC1/AC2）。

    AC1：明确下一步在主工作树执行 status --audit-merge（rules/review.md 硬要求）。
    AC2：回收未成功（worktree_recycled.removed=false）时额外给出处置指引，
    文案与 residual 实际结局一致、不承诺已清理。
    task-merge-audit-inline：内联审计已在 code APPROVED 内自动执行（响应
    merge_audit 字段），此处复核命令保留作人工二次确认。
    task-recycle-noblock：目录残留不再打回审查时，此处附延迟回收指引
    （journal 保留 + doctor --fix）。
    """
    from orchd.guide import _ENTRY_CMD

    hint = (
        f"任务 {task_id} 已审查通过并合并（completed）。内联 merge 审计已执行"
        f"（见响应 merge_audit 字段）；请在**主工作树**执行 "
        f"{_ENTRY_CMD} status --audit-merge 复核确认 merge_audit.warnings 为空"
        "（rules/review.md 硬要求）。"
    )
    wr = worktree_recycled or {}
    if wr.get("removed") is False:
        residual = wr.get("residual") or {}
        reason = residual.get("reason") or "worktree 回收未成功"
        hint += (
            f" 注意：任务 worktree 回收未成功（{reason}），"
            "请先切出该目录后重试回收，或运行 doctor --fix 清理残留；"
            "不要在已失效的 worktree 目录内执行命令。"
            " 回收 journal 已保留（延迟回收）：引擎下次回收该任务时按 journal "
            "重放收敛，无需重审。"
        )
    return {
        "step": "audit_merge",
        "command": f"{_ENTRY_CMD} status --audit-merge",
        "hint": hint,
        "read": read_for("audit_merge"),
        **({"main_worktree": main_worktree} if main_worktree else {}),
    }


def build_reviewer_rerun_command(task_def: dict[str, Any]) -> str | None:
    """从任务 verify_command 提取 reviewer 可粘贴的定向重跑命令（AC3，单一事实源）。

    解析 verify_command（``ruff && pytest && validate`` 链式），提取含 ``pytest``
    的命令段作为 reviewer 定向重跑模板——reviewer 只需重跑测试，不需重跑 ruff
    或 validate。跨平台 --basetemp 形式直接继承 verify_command 中的写法
    （``${TMPDIR:-/tmp}/orchd-vf-$$``），不手写字符串。verify_command 无
    pytest 段时返回 None（不编造命令）。
    """
    verify_cmd = task_def.get("verify_command")
    if not isinstance(verify_cmd, str) or not verify_cmd.strip():
        return None
    for segment in verify_cmd.split("&&"):
        seg = segment.strip()
        if "pytest" in seg:
            return seg
    return None
