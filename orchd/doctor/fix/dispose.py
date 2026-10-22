"""残留处置：目录移入备份区、文件移入备份区（_dispose_residual_dir/_dispose_file_to_backup）。"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import shutil
import time

from orchd.doctor.residue import _classify_residual_dir


def _dispose_residual_dir(path: Path,
                          backup_root: Path) -> tuple[bool, dict[str, Any]]:
    """处置残留任务目录：先移入备份区（可回滚），含真实内容则拒绝处置（DR-3）。

    顺序：① :func:`_classify_residual_dir` 分级——``has_content`` 直接拒绝（不删也
    不移），由调用方归入人工处置档；② ``empty`` / ``junk_only`` 先 ``shutil.move``
    到 ``backup_root/<目录名>``（可回滚，且完全避开递归删除）。

    NEW-DR3（task-doctor-fail-open-blindspots）：搬移失败（跨设备 copy+rm 中途
    失败等）→ 显式标记失败并保留源，**绝不**降级为 ``_rmtree_force`` 强删——
    否则源已删、备份残缺，宣称的「可回滚」失真。Windows 句柄占用等搬移失败
    同样走失败标记（留待下轮/人工），不静默强删。

    Args:
        path: 待处置的残留目录。
        backup_root: 备份落点目录（调用方给出，按时间戳分桶）。

    Returns:
        ``(ok, record)``：``ok=True`` 时 record 含 ``path`` / ``disposition``
        （``moved_to_backup``）与 ``backup``；``ok=False`` 时 record 含
        ``path`` / ``error`` 与原因（``move_failed=True`` + ``source_preserved``
        标记源是否仍存在）。不抛异常（调用方按处置档归类）。
    """
    content = _classify_residual_dir(path)
    if content == "has_content":
        return False, {
            "path":
            str(path),
            "content":
            content,
            "error": ("含非杂项内容（疑似真实工作残留）——拒绝自动删除；"
                      "请人工确认后处置（确认无用可手工移入 .doctor-backup 或删除）"),
        }
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / path.name
        if dest.exists():
            dest = backup_root / f"{path.name}-{int(time.time())}"
        shutil.move(str(path), str(dest))
        return True, {
            "path": str(path),
            "content": content,
            "backup": str(dest),
            "disposition": "moved_to_backup",
        }
    except OSError as exc:
        return False, {
            "path": str(path),
            "content": content,
            "error": (f"备份迁移失败（{type(exc).__name__}: {exc}）——源已保留，"
                      "未执行强删；请人工确认后处置"),
            "move_failed": True,
            "source_preserved": path.exists(),
        }


def _dispose_file_to_backup(path: Path, backup_root: Path,
                            project_root: Path) -> tuple[bool, dict[str, Any]]:
    """单文件整份搬入备份桶（``action=move_to_backup`` 的执行体，可回滚）。

    与通用 ``delete`` 档（:func:`_doctor_fix_impl` 内 copy2 后删除）的差别是**副本
    份数**：move 全程只有一份内容存在（先在桶内、后源消失），copy2 则先落两份。
    陈旧提案草稿属准入产物、注册前含独立信息量（草稿正文尚未入库，``.orchd/*`` 被
    gitignore），不得出现「备份未写成而源已删」的窗口，也不得落到 :func:`_safe_delete`
    的降级重命名（那会把内容推进系统 temp 的 ``orchd-trash-*``，脱离 ``.doctor-backup``
    既有保留期——本桶由 :func:`_prune_stale_backups` 收敛，不新造无界目录）。

    落点保持相对 ``project_root`` 的路径结构（同名冲突追加时间戳）。搬移失败
    （OSError，含跨设备 copy+rm 中途失败）→ 显式失败 + ``source_preserved``，
    绝不降级强删（NEW-DR3 同语义）。

    Args:
        path: 待搬移文件。
        backup_root: 桶内基准目录（调用方按时间戳分桶给出）。
        project_root: 相对路径基准。

    Returns:
        ``(ok, record)``：``ok=True`` 时 record 含 ``path`` / ``backup`` /
        ``disposition=moved_to_backup``；``ok=False`` 时含 ``error`` +
        ``move_failed`` + ``source_preserved``。不抛异常。
    """
    try:
        try:
            rel = path.relative_to(project_root)
        except ValueError:
            rel = Path(path.name)
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / rel
        if dest.exists():
            dest = dest.with_name(f"{dest.name}-{int(time.time())}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return True, {
            "path": str(path),
            "backup": str(dest),
            "disposition": "moved_to_backup",
        }
    except OSError as exc:
        return False, {
            "path": str(path),
            "error": (f"备份迁移失败（{type(exc).__name__}: {exc}）——源已保留，"
                      "未执行强删；请人工确认后处置"),
            "move_failed": True,
            "source_preserved": path.exists(),
        }
