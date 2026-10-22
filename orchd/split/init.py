from __future__ import annotations

from orchd.errors import ErrorCode, OrchdError
from orchd.ledger import Store, _write_text_atomic, resolve_store_dir
from orchd.spec import Master, validate_references, validate_structure
from pathlib import Path
from typing import Any
import json
from .common import _validate_module_id

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
            _write_text_atomic(
                spec_path,
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            )
            created_files.append(spec_path.relative_to(store_root).as_posix())

        # 创建空 ledger（若不存在）——走后端端口，避免在 sqlite 后端下产出幽灵 jsonl
        if not store.ledger_exists():
            created = store.backend.create_empty_ledger()
            if created:
                created_files.append(created)

        # 写初始 checkpoint
        store.update_checkpoint({})
        created_files.append("_checkpoint.json")
    finally:
        store.release_lock()

    return {"initialized": True, "created_files": created_files}
