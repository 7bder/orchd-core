"""doctor 修复域的随域常量（自动清理档位 / 备份保留期），叶域。"""

from __future__ import annotations


# 卫生门禁豁免档位（单一事实源，消费方 scripts/verify_project_hygiene.py）：
# 属这两档的残留由自动清理通道处置，不与清理器抢跑、不计入卫生失败判定。
AUTO_CLEAN_DISPOSITIONS: tuple[str, ...] = ("auto_clean", "legacy_move")


# A1（task-hostfix-pack-b）：备份保留期。`.doctor-backup/` 只增不减会长期积累
# （auto_clean 每次残留处置都新建时间戳桶）。策略：早于 30 天**且**不在各分类
# 最新 5 个之内的桶才删；非时间戳命名一律不动；best-effort（删失败静默跳过，
# 不阻断主流程）。只删自家备份目录，不碰其他内容。
_BACKUP_RETENTION_DAYS = 30


_BACKUP_RETENTION_KEEP_NEWEST = 5
