from __future__ import annotations

from orchd.errors import ErrorCode, OrchdError
from pathlib import Path
import re

_SAFE_MODULE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

def _validate_module_id(mod_id: str) -> str:
    """P2-3：module.id 仅允许 [A-Za-z0-9_-]，防止 ``../`` 或绝对路径越界写到 store 根之外。"""
    if not mod_id or not _SAFE_MODULE_ID_RE.fullmatch(mod_id):
        raise OrchdError(
            ErrorCode.E003,
            f"invalid_module_id: {mod_id!r}",
            [{
                "module_id": mod_id,
                "hint": "module.id 须为 [A-Za-z0-9_-] 组成的相对目录名，禁止路径分隔/..",
            }],
        )
    return mod_id

def _git_head_sha(project_root: Path | None) -> str | None:
    """best-effort 取 HEAD SHA；非 git / 异常 → None（调用方跳过 CAS）。

    供 amend 的乐观并发校验（task-concurrent-amend-lost-update）与调用方读时
    快照共用。空串按 None 处理。
    """
    if project_root is None:
        return None
    try:
        import subprocess as _sp

        proc = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (_sp.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None
