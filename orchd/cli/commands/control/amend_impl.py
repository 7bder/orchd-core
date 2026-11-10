"""amend 编排器（_amend_impl），调用四段；体内 2 条函数级 import 原样保留。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

from .amend_admission import _amend_admission
from .amend_patch_fields import _amend_patch_fields
from .amend_persist_commit import _amend_persist_commit
from .amend_register_gate import _amend_register_gate


# ── 终态共享面声明裁剪通道（code review 修复，task-shared-surface-admission）──
# revive 拒绝旧 fragments/generated 声明时给出的迁移命令必须**可执行**：终态卡走
# 常规 --remove-files-to-edit 会被终态矩阵拦（声明规范化「删的须不存在」）或撞
# amend_decl_empty。本通道只放行「唯一一种声明变更 = 移除已注册 fragments/generated
# 面路径」；任何其它字段变化/新增/非共享面路径仍走原矩阵（E007 行为不变）。
_PRUNE_EXCLUDED_FLAGS = (
    "files_to_edit", "exempt_files", "remove_exempt_files", "depends_on",
    "verify_command", "verify_command_file", "reviewers", "files_to_read",
    "acceptance_criteria", "brief", "name", "verify_timeout_seconds",
    "additional_sources", "revise_terminal", "register", "allow_shared_write",
    "refresh_surface", "remove_refresh_surface",
)
_PRUNE_SURFACE_MODES = frozenset({"fragments", "generated"})


def _surface_prune_plan(
    args, *, master, store, project_root: Path,
) -> dict[str, Any] | None:
    """终态卡共享面声明裁剪检测：命中返回计划；任何不匹配 → None（走原矩阵）。"""
    task_id = getattr(args, "task", None)
    removed = [str(p) for p in (getattr(args, "remove_files_to_edit", None) or ())]
    if not task_id or not removed:
        return None
    for name in _PRUNE_EXCLUDED_FLAGS:
        if getattr(args, name, None):
            return None
    target = next(
        (t for t in (master.tasks or [])
         if isinstance(t, dict) and t.get("id") == task_id),
        None,
    )
    if target is None:
        return None
    try:
        state = store.replay()
    except Exception:
        return None
    status = getattr(state.get(task_id), "status", "") if state else ""
    from orchd.contracts.events import TERMINAL_STATUSES

    if status not in TERMINAL_STATUSES:
        return None
    from orchd import surfaces as _surfaces
    from .amend_register_gate import _normalize_decl_path, _surface_hit

    specs = _surfaces.load_surfaces(project_root)
    declared = {
        _normalize_decl_path(p) for p in (target.get("files_to_edit") or [])
    }
    hits: list[dict[str, Any]] = []
    for raw in removed:
        key = _normalize_decl_path(raw)
        hit = _surface_hit(key, specs) if key else None
        if hit is None or hit[0].mode not in _PRUNE_SURFACE_MODES:
            return None
        spec = hit[0]
        hits.append({
            "declared": raw,
            "key": key,
            "surface": spec.name,
            "mode": spec.mode,
            "path": spec.path,
            "remove": key in declared,
        })
    return {"task_id": task_id, "target": target, "hits": hits}


def _sync_prune_snapshots(
    master, store_root: Path, touched: set[str],
) -> list[str]:
    """按 master 重写受影响的 mod-*/spec.json（与 split.amend 快照写入同形）。"""
    import json as _json

    from orchd.ledger import _write_text_atomic

    synced: list[str] = []
    tasks = master.tasks or []
    for module in master.modules or []:
        mod_id = str(module.get("id") or "") if isinstance(module, dict) else ""
        if not mod_id or mod_id not in touched:
            continue
        snapshot = {
            "module_id": mod_id,
            "module_name": module.get("name", ""),
            "module_role": module.get("role", ""),
            "tasks": [
                t for t in tasks
                if isinstance(t, dict) and t.get("module") == mod_id
            ],
        }
        spec_path = store_root / mod_id / "spec.json"
        if not spec_path.parent.is_dir():
            continue
        _write_text_atomic(
            spec_path,
            _json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        )
        synced.append(str(spec_path))
    return synced


def _amend_surface_prune(
    args, *, master, store, orchd_dir: Path, master_path: Path,
    project_root: Path, agent_id: str,
) -> dict[str, Any] | None:
    """终态共享面声明裁剪：写 master + 同步快照 + AMEND 审计（可执行迁移命令落点）。"""
    plan = _surface_prune_plan(
        args, master=master, store=store, project_root=project_root)
    if plan is None:
        return None
    reason = str(getattr(args, "reason", None) or "").strip()
    removed = [h["declared"] for h in plan["hits"] if h["remove"]]
    if not reason:
        raise OrchdError(
            ErrorCode.E007,
            "surface_prune_reason_missing: 终态共享面声明裁剪必须提供非空 --reason"
            "（写入 AMEND 审计事件 rationale）",
            [{
                "task_id": plan["task_id"],
                "reason": "surface_prune_reason_missing",
                "rule": "surface_declaration_prune",
                "channel": "amend",
                "removed": [h["declared"] for h in plan["hits"]],
                "declared": [h["path"] for h in plan["hits"]],
                "hint": "重跑并在 --reason 写明裁剪理由（如 revive 前置撤回旧声明）",
            }],
        )
    if not removed:
        return {
            "amended": True,
            "updated_tasks": [],
            "new_tasks": [],
            "unchanged_tasks_count": 0,
            "removed_tasks": [],
            "surface_prune": {
                "task_id": plan["task_id"],
                "channel": "amend",
                "rule": "surface_declaration_prune",
                "removed": [],
                "noop": True,
                "surfaces": plan["hits"],
            },
            "commit": {"performed": False, "reason": "no_changes"},
        }
    return _apply_surface_prune(
        plan, reason=reason, master=master, store=store, orchd_dir=orchd_dir,
        master_path=master_path, project_root=project_root, agent_id=agent_id,
        removed=removed,
    )


def _apply_surface_prune(
    plan, *, reason: str, master, store, orchd_dir: Path, master_path: Path,
    project_root: Path, agent_id: str, removed: list[str],
) -> dict[str, Any]:
    """裁剪落盘：持准入写锁写 master/快照/事件，再 best-effort 自动提交。"""
    import json as _json

    from orchd.contracts.events import EVENT_AMEND
    from orchd.contracts.task_schema import FILES_TO_EDIT_FIELD
    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.gitops_ops import make_event
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_store_dir,
        resolve_workspace_root,
    )
    from .amend_register_gate import _normalize_decl_path
    from .persist import _persist_master_durable

    keys = {h["key"] for h in plan["hits"]}
    target = plan["target"]
    before = [str(raw) for raw in (target.get(FILES_TO_EDIT_FIELD) or [])]
    target[FILES_TO_EDIT_FIELD] = [
        raw for raw in before if _normalize_decl_path(raw) not in keys
    ]
    payload = _json.dumps(master.raw, ensure_ascii=False, indent=2)
    try:
        if master_path.read_bytes().endswith(b"\n"):
            payload += "\n"
    except OSError:
        pass
    lock = intake_lock_acquire(orchd_dir, agent_id)
    synced: list[str] = []
    try:
        store.acquire_lock()
        try:
            _persist_master_durable(master_path, payload)
            synced = _sync_prune_snapshots(
                master, Path(resolve_store_dir(orchd_dir)),
                {str(target.get("module") or "")},
            )
            store.append_event(make_event(
                plan["task_id"], agent_id, EVENT_AMEND,
                reason="surface_declaration_prune",
                fields=[FILES_TO_EDIT_FIELD],
                removed=removed,
                rule="surface_declaration_prune",
                channel="amend",
                rationale=reason,
                hint="终态卡共享面声明裁剪（revive 迁移通道）：master 为唯一权威，"
                     "mod-*/spec.json 快照已同步",
            ))
            store.update_checkpoint(store.replay())
        finally:
            store.release_lock()
    finally:
        try:
            intake_lock_release(lock)
        except Exception:
            pass
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        commit: dict[str, Any] = {
            "performed": False, "reason": "not_on_main",
            "branch": current_branch,
        }
    else:
        ws_root = resolve_workspace_root(project_root)
        commit = ensure_committed(
            project_root,
            [str(master_path), str(ws_root / "IDEAS.md"),
             str(ws_root / "ROADMAP.md")],
            f"chore(intake): orchd amend — surface declaration prune: "
            f"{plan['task_id']}",
        )
    return {
        "amended": True,
        "updated_tasks": [plan["task_id"]],
        "new_tasks": [],
        "unchanged_tasks_count": 0,
        "removed_tasks": [],
        "surface_prune": {
            "task_id": plan["task_id"],
            "channel": "amend",
            "rule": "surface_declaration_prune",
            "removed": removed,
            "surfaces": plan["hits"],
            "snapshots_synced": synced,
        },
        "commit": commit,
    }



# ── generated 面受控刷新通道（task-surface-refresh-channel）──────────────
# 声明域之外的第二通道：--refresh-surface / --remove-refresh-surface 只改任务的
# surface_refresh 数组（内存 master），不碰 files_to_edit/exempt_files；准入矩阵
# 在 _amend_register_gate 的 _surface_refresh_gate 里统一复算。
_REFRESH_STATUSES = frozenset({"pending", "claimed"})


def _amend_surface_refresh(
    args, *, master, store, project_root: Path,
) -> None:
    """应用 --refresh-surface / --remove-refresh-surface（只改内存 master）。

    状态矩阵：pending/claimed 放行；in_review/completed 一律 E007（终态卡不得
    再刷新，避免审查后偷渡生成物）。同面重复声明 = 覆盖（更新 reason）。
    """
    adds = [str(n).strip() for n in
            (getattr(args, "refresh_surface", None) or ()) if str(n).strip()]
    removes = [str(n).strip() for n in
               (getattr(args, "remove_refresh_surface", None) or ())
               if str(n).strip()]
    if not adds and not removes:
        return
    task_id = getattr(args, "task", None)
    if not task_id:
        raise OrchdError(
            ErrorCode.E007,
            "amend_refresh_requires_task: --refresh-surface / "
            "--remove-refresh-surface 需配 --task <task_id>",
            [{
                "reason": "amend_refresh_requires_task",
                "hint": "刷新是任务级声明：--task <id> --refresh-surface <name> "
                        "--reason \"<为何刷新>\"",
            }],
        )
    clash = sorted(set(adds) & set(removes))
    if clash:
        raise OrchdError(
            ErrorCode.E007,
            "amend_refresh_conflict: 同一面不能同时声明刷新与撤回",
            [{
                "reason": "amend_refresh_conflict",
                "task_id": task_id,
                "surfaces": clash,
                "hint": "刷新与撤回互斥：确认该面应保留还是移除后，只带其中一个旗标",
            }],
        )
    target = next(
        (t for t in (master.tasks or [])
         if isinstance(t, dict) and t.get("id") == task_id),
        None,
    )
    if target is None:
        raise OrchdError(
            ErrorCode.E005,
            f"task '{task_id}' not found in master",
            [{
                "task_id": task_id,
                "hint": f"任务 {task_id} 在 _master.json 中不存在，检查 id 拼写或注册",
            }],
        )
    try:
        state = store.replay()
    except Exception:
        state = {}
    status = getattr(state.get(task_id), "status", "pending")
    if status not in _REFRESH_STATUSES:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: 任务 {task_id} 当前为 {status} 态，"
            "--refresh-surface 仅 pending/claimed 态放行",
            [{
                "reason": "refresh_status_invalid",
                "task_id": task_id,
                "status": status,
                "hint": ("刷新声明须在实现期定稿：in_review/completed 不得再改，"
                         "避免审查后偷渡生成物"),
            }],
        )
    from orchd import surfaces as _surfaces
    from .amend_register_gate import _declared_paths

    specs = _surfaces.load_surfaces(project_root)
    declared = [raw for raw, _key in _declared_paths(target)]
    entries = _surfaces.normalize_refresh_entries(
        specs, target.get(_surfaces.SURFACE_REFRESH_FIELD) or (),
        declared_paths=declared,
    )
    by_name = {entry["surface"]: entry for entry in entries}
    reason = getattr(args, "reason", None)
    for name in adds:
        by_name[name] = _surfaces.build_refresh_entry(
            specs, name, reason, declared_paths=declared)
    for name in removes:
        by_name.pop(name, None)
    if by_name:
        target[_surfaces.SURFACE_REFRESH_FIELD] = sorted(
            by_name.values(), key=lambda entry: entry["surface"])
    else:
        target.pop(_surfaces.SURFACE_REFRESH_FIELD, None)

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
    pruned = _amend_surface_prune(
        args=args, master=master, store=store, orchd_dir=orchd_dir,
        master_path=master_path, project_root=project_root, agent_id=agent_id,
    )
    if pruned is not None:
        return pruned
    _amend_surface_refresh(
        args, master=master, store=store, project_root=project_root)
    patch_task = _amend_patch_fields(
        args=args, master=master, store=store, project_root=project_root,
        revise_terminal=revise_terminal)
    (changed, dry_run_results, dry_run_skipped,
     register_warnings) = _amend_register_gate(
        orchd_dir=orchd_dir, master=master, store=store,
        project_root=project_root, revise_terminal=revise_terminal,
        scope_task=patch_task,
        allow_shared_write=getattr(args, "allow_shared_write", None),
        surface_reason=getattr(args, "reason", None))

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
            scope_task=patch_task,
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
            register_warnings=register_warnings,
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
