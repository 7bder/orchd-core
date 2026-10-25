"""amend 落盘提交段（_amend_persist_commit），依赖 proposals+persist。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .persist import _persist_master_durable
from .proposals import _consume_registered_proposal


def _amend_persist_commit(
    result: dict[str, Any],
    *,
    proposal_consumed: dict[str, Any] | None,
    master,
    master_path: Path,
    patch_task: str | None,
    register_path: str | None,
    changed: list[str],
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """amend 落盘段：master 持久化 + 提案消费 + 自动提交与告警（原 L1065-1134）。

    --task 补丁路径与 --register 注册路径均在 amend() 成功返回后落盘（此前任何失败
    路径零修改），同一硬化序 _persist_master_durable；注册通道落盘之后消费提案草稿
    （失败路径不消费，草稿原样保留可重试）；成功后 best-effort 提交 master +
    IDEAS.md + ROADMAP.md（非 default 分支降级 not_on_main；commit 未执行且非
    no_changes 时写 commit_warning 可审计）。返回 (result, proposal_consumed)。
    """
    import json as _json

    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.ledger import resolve_workspace_root

    def _payload_preserving_trailing_newline() -> str:
        # D3（debug-pack D）：保留**原始**尾换行形态——仅当既有文件以换行结尾才补，
        # 否则维持「无尾换行」既有契约（tests/test_control_amend_atomic 硬断言）。
        text = _json.dumps(master.raw, ensure_ascii=False, indent=2)
        try:
            if master_path.read_bytes().endswith(bytes([10])):
                text += chr(10)
        except OSError:
            pass
        return text

    if patch_task is not None:
        _persist_master_durable(master_path, _payload_preserving_trailing_newline())

    # task-amend-register-channel-hotfix（2026-09-21）：--register 路径内存追加后落盘。
    # split.amend() 只重写快照不写 master（写盘仅 --task 分支 577 行），缺此行则注册
    # 仅存于内存/快照、canonical master 丢失（新任务幽灵化；下次快照重生成连快照痕迹
    # 一并消失，响应却报 amended:true）。位置：blocking_errors/dry-run 阻断之后、
    # amend() 成功之后——失败路径不落盘；与 577 行同格式（无尾换行）。
    # task-master-write-atomic：同上，--register 路径走同一硬化序（§11-13 续）。
    if register_path is not None:
        _persist_master_durable(master_path, _payload_preserving_trailing_newline())
        # 注册即消费提案（task-hygiene-gate-executor-and-proposal-lifecycle AC1，
        # 根因修复）：位置必须在本行之后——此前 _apply_register_proposals 只把提案
        # append 进内存 master，落盘由此处完成，而**整段从不删除草稿**，于是
        # .orchd/proposals 长期堆积已注册任务的历史草稿（实测 main 上 161 行
        # [proposals] FAIL），卫生门禁 check 3 恒红。放在落盘之后 → 任何失败路径
        # （提案校验 E007/E002、amend 的 E007/E025/E028、os.replace 自身抛错）
        # 都不消费，草稿原样保留可重试；锁仍持有（写+提交原子区内）。
        proposal_consumed = _consume_registered_proposal(
            Path(register_path), project_root)

    # 成功后 best-effort 自动提交（锁内、不阻塞状态机，语义对齐 merged:false）
    summary = ", ".join(changed) if changed else "snapshot refresh"

    # 分支校验：intake/amend 约定只在 main 执行，非 main 时降级为不提交，
    # 避免 master+IDEAS.md 被误提交进任务分支（污染待 merge 内容）
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        result["commit"] = {
            "performed": False,
            "reason": "not_on_main",
            "branch": current_branch,
        }
    else:
        # AC3（task-12-engine-path-abstraction）：IDEAS.md 走统一工作区根 helper
        # （默认 .orchd/，兼容旧根路径）；commit 路径与 ensure_committed 期望一致。
        ws_root = resolve_workspace_root(project_root)
        # intake-commit-enforcement（2026-08-14）：提交范围含 ROADMAP.md——
        # roadmap 摄入改的 ROADMAP.md 此前不在范围，必然残留未提交改动
        commit = ensure_committed(
            project_root,
            [
                str(master_path),
                str(ws_root / "IDEAS.md"),
                str(ws_root / "ROADMAP.md")
            ],
            f"chore(intake): orchd amend — {summary}",
        )
        result["commit"] = commit
        # intake-commit-enforcement（2026-08-14）：commit 降级可审计化（对齐
        # merge_warning 先例）——注册成功后 commit 未执行（非 no_changes）不再
        # 静默：写入 commit_warning 供 status --audit-intake 巡检。git 环境不可用
        # （not_a_git_repo / git_unavailable）保留 best-effort 降级（判据 3）；
        # git 可用但提交失败（commit_failed）同样告警——"注册成功但改动未入库"
        # 违背"强制提交"语义，须人工核对。
        if commit.get("performed") is False and commit.get(
                "reason") != "no_changes":
            result["commit_warning"] = {
                "reason":
                commit.get("reason"),
                "message": (f"amend 注册成功但 commit 未执行（{commit.get('reason')}）："
                            "摄入产物改动可能未入库"),
                "hint": ("若为 git 环境异常，可运行 'orchd status --audit-intake' "
                         "巡检未提交摄入产物，或运行 'orchd intake' 手动提交"),
            }

    return result, proposal_consumed
