"""report — merge-ack 读写域（W6-C3 拆分，纯搬移）。

来源：``orchd/report.py``（1489 行，W6-C3 纯搬移）。

merge-ack 文件路径解析 / 读取 / 记录。

**叶子域**：零包内相对导入。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes, session_lock_check, session_lock_release
from orchd.ledger import Store, TaskState, resolve_workspace_root



# .orchd/merge-acks.json：merge_warning 残留的人工销账清单（task-merge-warning-ack）。
# gitignored（运行时文件，.gitignore 白名单天然忽略），格式：
#   {"<task_id>": {"acked_at": <ISO 时间>, "reason": <str>}}
_MERGE_ACKS_FILENAME = "merge-acks.json"



def merge_acks_path(project_root: Path) -> Path:
    """返回人工销账清单路径（落 canonical 账本根）。

    运行时状态文件须落在 ``resolve_store_dir`` 指向的 canonical 账本根：
    flat 布局下即 ``<project_root>/.orchd``；container / ORCHD_HOME 重定向
    布局下为 ``.orchd-runtime`` 等。落点须与 ``doctor.auto_clean`` 的保护
    口径一致（运行时文件位于 canonical 根时受保护，位于 legacy flat 的
    ``main/.orchd`` 时会被迁移）——否则 ``merge-ack`` 写入即被迁走、
    ``audit-merge`` 永远读不到，人工销账失效。
    """
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.ledger import resolve_store_dir

        return Path(resolve_store_dir(orchd_dir)) / _MERGE_ACKS_FILENAME
    except Exception:
        return orchd_dir / _MERGE_ACKS_FILENAME



def load_merge_acks(project_root: Path) -> dict[str, Any]:
    """读取 merge_warning 人工销账清单（task-merge-warning-ack）。

    文件不存在 / 解析失败 / 非 dict → 视为空清单（不报错，best-effort 降级）。

    Args:
        project_root: 仓库根目录。

    Returns:
        ``{"<task_id>": {"acked_at": ..., "reason": ...}}`` 字典；异常时空 dict。
    """
    path = merge_acks_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}



def merge_ack(project_root: Path, task_id: str, reason: str) -> dict[str, Any]:
    """登记 merge_warning 人工销账（追加/更新 .orchd/merge-acks.json，原子写）。

    与 resolve_sha 自动销账（task-merge-warning-resolve-sha）互补：人工路径
    兜底旧事件 / 无 resolve_sha 场景（08-22 task-fp-identity-single-source
    残留即此场景）。重复 ack 覆盖更新（acked_at 刷新为当前时间）。

    Args:
        project_root: 仓库根目录。
        task_id: 已人工确认的 task_id（非空）。
        reason: 确认原因（必填，非空）。

    Returns:
        ``{"acked": True, "task_id": ..., "acked_at": ..., "reason": ...}``。

    Raises:
        ValueError: task_id 或 reason 为空。
        OSError: 写入后回读不一致（原子写失败）。
    """
    if not task_id:
        raise ValueError("merge_ack: task_id 不能为空")
    if not reason:
        raise ValueError("merge_ack: reason 必填（人工确认原因）")
    path = merge_acks_path(project_root)
    acks = load_merge_acks(project_root)
    entry = {
        "acked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "reason": reason,
    }
    acks[task_id] = entry
    # 原子写：tmp + os.replace（写后回读确认，失败可审计）。
    # 耐久档（task-os-replace-hardening-io-batch 批次 1b）：tmp 写入 → flush +
    # fsync → ``ledger._atomic_replace``（WinError 5/32 有界重试，耗尽抛结构化
    # E007）→ ``_fsync_dir(父目录)``；写后回读防线原样保留且未被绕过。
    # 文本形态（indent=2 / ensure_ascii=False / 无尾换行）原样保留；惰性导入
    # 与本仓其余耐久站统一（替换入口在账本源头 spy）。
    from orchd.ledger import _atomic_replace, _fsync_dir

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(acks, ensure_ascii=False, indent=2))
        f.flush()
        os.fsync(f.fileno())
    _atomic_replace(tmp, path)
    _fsync_dir(path.parent)
    if load_merge_acks(project_root).get(task_id) != entry:
        raise OSError("merge-acks 写入回读不一致")
    return {"acked": True, "task_id": task_id, **entry}
