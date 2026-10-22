"""沙箱安全删除单文件（_delete_file），叶域。"""

from __future__ import annotations

from pathlib import Path


def _delete_file(path: Path, project_root: Path) -> None:
    """沙箱安全删除单个文件（DR-10：doctor 文件删除的唯一入口）。

    裸 ``Path.unlink`` 在沙箱下被劫持为「移入回收站」，回收站不可用时 FAIL_CLOSED
    抛 OSError → 清理静默失败（残留永远清不掉）。统一走
    ``gitops.cleanup._safe_delete``：底层 OS 直接删除优先 → ``unlink`` → 降级重命名
    到系统 temp（语义等效删除）。路径不存在时直接返回（幂等）。
    """
    from orchd.gitops.cleanup import _safe_delete

    if not path.exists():
        return
    _safe_delete(path, Path(project_root))
