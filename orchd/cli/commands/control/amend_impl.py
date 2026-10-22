"""amend 编排器（_amend_impl），调用四段；体内 2 条函数级 import 原样保留。"""

from __future__ import annotations

from typing import Any

from .amend_admission import _amend_admission
from .amend_patch_fields import _amend_patch_fields
from .amend_persist_commit import _amend_persist_commit
from .amend_register_gate import _amend_register_gate


def _amend_impl(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """增量更新 snapshot，依据状态约束矩阵过滤变更。

    CLI 参数: args.master — master 文件路径（默认 .orchd/_master.json）。
    返回: 变更摘要字典（new_tasks / updated_tasks / unchanged_tasks_count / removed_tasks），
    成功后附加 commit 字段（best-effort 自动提交 master 与 IDEAS.md，不阻塞）；
    新增/变更任务若声明 verify_command，附加 verify_dry_run 字段（试跑结果仅提示、不阻断注册）。
    """
    from orchd.split import amend
    from orchd.worktree import resolve_canonical_project_root

    (register_path, master, master_path, orchd_dir, store, project_root,
     read_head, revise_terminal) = _amend_admission(
        args, orchd_dir, master, store)
    patch_task = _amend_patch_fields(
        args=args, master=master, store=store, project_root=project_root,
        revise_terminal=revise_terminal)
    changed, dry_run_results, dry_run_skipped = _amend_register_gate(
        orchd_dir=orchd_dir, master=master, store=store,
        project_root=project_root, revise_terminal=revise_terminal)

    # 校验通过后执行 amend（写入 snapshot + checkpoint；透传终态文本修订通道）。
    # task-concurrent-amend-lost-update：release_lock=False 使准入写锁穿越
    # master 写盘 + 自动提交（写+提交原子化），提交后在 finally 释放；amend 内
    # CAS（expected_head=读时 HEAD）已在持锁后校验，未漂移才走到这里。
    # amend() 内抛异常 → 锁由其内部 except 释放，此处 held_lock 保持 None。
    held_lock: dict[str, Any] | None = None
    proposal_consumed: dict[str, Any] | None = None
    try:
        result = amend(
            orchd_dir,
            master,
            store,
            revise_terminal=revise_terminal,
            reason=getattr(args, "reason", None),
            expected_head=read_head,
            release_lock=False,
        )
        if isinstance(result, dict):
            held_lock = result.pop("_intake_lock", None)

        # task-amend-patch-write-after-validate：--task 补丁落盘点（--register 路径见
        # 热修复 1b5cdef）。amend() 成功返回才写 master：此前任何失败（E007/E025/E028）
        # 均在写盘前抛出，canonical 零修改；与 1b5cdef 同格式（无尾换行）。
        # （锁仍持有中：写盘纳入原子区。）
        # task-master-write-atomic → §11-13 续（task-master-write-hardening-...）：
        # 原子写只解决了「撕裂」，本路此前仍比账本低一档——写完 tmp 不 fsync、rename 不
        # 争用重试。现统一到 _persist_master_durable 的同一硬化序（含顺序与理由）。
        # 文本形态与现状一致（无尾换行），不新增幻影脏。
        result, proposal_consumed = _amend_persist_commit(
            result,
            proposal_consumed=proposal_consumed,
            master=master,
            master_path=master_path,
            patch_task=patch_task,
            register_path=register_path,
            changed=changed,
            project_root=project_root,
        )
    finally:
        # 写+提交原子区出口：无论提交成败释放准入写锁（amend 内异常路径已自释，
        # 此处 held_lock 为 None，不重复释放）。
        if held_lock is not None:
            try:
                from orchd.ledger import intake_lock_release as _release_intake_lock

                _release_intake_lock(held_lock)
            except Exception:
                pass

    if dry_run_results:
        result["verify_dry_run"] = dry_run_results
    if dry_run_skipped:
        # 透明化：本次跳过 dry-run 的任务（纯文本修订，verify_command 未变）
        result["verify_dry_run_skipped"] = dry_run_skipped

    # task-amend-register-channel：注册语义显式化——
    # ① --register：恒写 canonical，registered=true（一条龙注册）。
    # ② amend --master 指向非 canonical：仅写该 master 所在 orchd_dir，不落当前
    #    canonical → registered=false + 明确 hint（不再用 amended:true 暗示已注册）。
    if register_path is not None:
        result["registered"] = True
        # 消费留痕：consumed=False 属「注册已成功、草稿清理失败」，草稿仍在会被
        # 卫生门禁点名（可见而非静默），故不升级为注册失败；响应的 proposal_consumed
        # 字段是该状态的唯一出口。
        if proposal_consumed is not None:
            result["proposal_consumed"] = proposal_consumed
    else:
        # task-master-path-residual-convergence：经单一真源取 canonical master
        # （对 canonical orchd_dir 自身解析恒等，无裸拼；AST 门禁零新增）。
        from orchd.worktree import resolve_master_path_from_dir

        canonical_master = resolve_master_path_from_dir(
            resolve_canonical_project_root(project_root) / ".orchd")
        if master_path.resolve() != canonical_master.resolve():
            result["registered"] = False
            result["hint"] = (
                "amend --master 指向非 canonical master：本次仅写该 master 所在 orchd_dir，"
                "未写入当前 canonical。如需注册，请把任务并入 canonical .orchd/_master.json "
                "后重跑裸 amend，或用 `amend --register <proposal.json>` 显式注册。")
    return result
