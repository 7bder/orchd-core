"""备份保留期剪枝（_prune_stale_backups）。"""

from __future__ import annotations

from pathlib import Path

from .constants import _BACKUP_RETENTION_DAYS, _BACKUP_RETENTION_KEEP_NEWEST


def _prune_stale_backups(backup_base: Path) -> list[str]:
    """清理过期备份桶，返回已删路径（相对名，供留痕）。"""
    pruned: list[str] = []
    try:
        import time as _time

        base = Path(backup_base)
        if not base.is_dir():
            return pruned
        now = _time.time()
        # 顶层时间戳桶 + residual/legacy 分类桶逐个收敛。
        buckets: list[Path] = []
        for child in sorted(base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                buckets.append(child)
            elif child.is_dir() and child.name in ("residual", "legacy"):
                buckets.extend(
                    sorted(p for p in child.iterdir()
                           if p.is_dir() and p.name.isdigit()))
        if not buckets:
            return pruned
        by_parent: dict[str, list[Path]] = {}
        for b in buckets:
            by_parent.setdefault(str(b.parent), []).append(b)
        for siblings in by_parent.values():
            ordered = sorted(siblings, key=lambda p: int(p.name), reverse=True)
            for b in ordered[_BACKUP_RETENTION_KEEP_NEWEST:]:
                try:
                    age_days = (now - int(b.name)) / 86400
                except ValueError:
                    continue
                if age_days < _BACKUP_RETENTION_DAYS:
                    continue
                try:
                    import shutil as _shutil

                    _shutil.rmtree(str(b), ignore_errors=False)
                    if not b.exists():
                        pruned.append(str(b))
                except OSError:
                    pass
    except Exception:
        pass
    return pruned
