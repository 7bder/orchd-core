"""orchd.doctor.fix — doctor 修复域（--fix / 自动清理）聚合入口。

W8-6（task-split2-w8-6-doctor-fix-pkg）：旧 orchd/doctor/fix.py（882 行）纯搬移拆为
本包七域。本 __init__.py 纯重导出（零顶层定义），外部 ``from orchd.doctor.fix import X`` /
``orchd.doctor.fix.X`` 与 ``orchd.doctor/__init__.py`` 的既有导入零改动。

依赖方向：constants / delete（叶）<- dispose / prune / report <- fixrun / autoclean 编排器。

monkeypatch 打点面（W8-6 迁移）：修补 ``orchd.doctor.fix.<符号>`` 现在只改本聚合入口的
重导出属性，**对实际调用点无效**；补丁须打 ``orchd.doctor.fix.<域>.<符号>``（一个符号被
多域消费时要打多个 target），实测消费面见 tests/test_doctor_pkg.py 的消费表。
"""

from .constants import (
    AUTO_CLEAN_DISPOSITIONS, _BACKUP_RETENTION_DAYS, _BACKUP_RETENTION_KEEP_NEWEST,
)
from .delete import _delete_file
from .dispose import _dispose_residual_dir, _dispose_file_to_backup
from .prune import _prune_stale_backups
from .report import doctor
from .fixrun import doctor_fix, _doctor_fix_impl
from .autoclean import _auto_clean_item, _auto_move_legacy, auto_clean, _auto_clean_impl

__all__ = (
    'AUTO_CLEAN_DISPOSITIONS',
    '_BACKUP_RETENTION_DAYS',
    '_BACKUP_RETENTION_KEEP_NEWEST',
    '_delete_file',
    '_dispose_residual_dir',
    '_dispose_file_to_backup',
    '_prune_stale_backups',
    'doctor',
    'doctor_fix',
    '_doctor_fix_impl',
    '_auto_clean_item',
    '_auto_move_legacy',
    'auto_clean',
    '_auto_clean_impl',
)
