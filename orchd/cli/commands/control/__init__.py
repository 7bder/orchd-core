"""Orchd CLI 路由：control 命令 handler（聚合入口）。

W8-3（task-split2-w8-3-control-pkg）：旧 orchd/cli/commands/control.py（1760 行）
纯搬移拆为本包十三域。本 __init__.py 纯重导出（零顶层函数/类定义），外部
``from orchd.cli.commands.control import X`` 与 ``orchd.cli.commands.control.X``
属性访问零改动。

依赖方向：constants/declarations/proposals/persist/amend_register_gate（叶）
<- patch_validation <- amend_patch_fields；proposals <- amend_admission；
proposals+persist <- amend_persist_commit；四段 <- amend_impl <- commands <- register；
roundtrip <- commands。
"""

from .constants import _VERIFY_TIMEOUT_MIN, _VERIFY_TIMEOUT_MAX
from .declarations import _log_decl_withdraw, _decl_withdraw_branch_overlap
from .patch_validation import _validate_verify_timeout_patch, _validate_additional_sources_patch
from .proposals import (
    _apply_register_proposals,
    _closeout_ids,
    _consume_registered_proposal,
    _expand_serial_order,
    _validate_closeout_coverage,
)
from .persist import _persist_master_durable, _fsync_file_durable
from .amend_admission import _amend_admission
from .amend_patch_fields import (
    _amend_patch_fields,
    _apply_depends_on_patch,
    _apply_text_field_patch,
)
from .amend_register_gate import _amend_register_gate
from .amend_persist_commit import _amend_persist_commit
from .amend_impl import _amend_impl
from .roundtrip import (
    _amend_roundtrip_applicable,
    _enter_amend_roundtrip,
    _emit_amend_roundtrip_event,
    _finish_amend_roundtrip,
    _rollback_amend_roundtrip,
)
from .commands import _cmd_amend, _cmd_retract, _cmd_force_status, _cmd_restore, _cmd_merge_ack
from .register import register
from .persist import _atomic_replace
from .persist import _fsync_dir
from .amend_register_gate import structured_error

__all__ = (
    '_VERIFY_TIMEOUT_MIN',
    '_VERIFY_TIMEOUT_MAX',
    '_log_decl_withdraw',
    '_decl_withdraw_branch_overlap',
    '_validate_verify_timeout_patch',
    '_validate_additional_sources_patch',
    '_consume_registered_proposal',
    '_apply_register_proposals',
    '_persist_master_durable',
    '_fsync_file_durable',
    '_amend_admission',
    '_amend_patch_fields',
    '_amend_register_gate',
    '_amend_persist_commit',
    '_amend_impl',
    '_amend_roundtrip_applicable',
    '_enter_amend_roundtrip',
    '_emit_amend_roundtrip_event',
    '_finish_amend_roundtrip',
    '_rollback_amend_roundtrip',
    '_cmd_amend',
    '_cmd_retract',
    '_cmd_force_status',
    '_cmd_restore',
    '_cmd_merge_ack',
    'register',
    '_atomic_replace',
    '_fsync_dir',
    'structured_error',
)
