"""Orchd 快照生成（init）与增量更新（amend）。

纯重导出包：旧 orchd/split.py 的 18 个顶层函数 + 3 个模块级常量经聚合入口可达，
外部 ``from orchd.split import X`` 与 ``orchd.split.X`` 属性访问零改动（另重导出
intake 的 _INTAKE_PRODUCT_FILES 以保 orchd.split 命名空间兼容）。

依赖方向：split/ -> spec/ ledger/ gitops/ guide/ intake/ schema_policy/ contracts 包
（不导入 onboard / cli）。
"""

from .common import _SAFE_MODULE_ID_RE
from .common import _validate_module_id
from .common import _git_head_sha
from .log import _log_amend_guard_degrade
from .validate import _TERMINAL_TEXT_REVISABLE_FIELDS
from .validate import _TERMINAL_REVISION_EVENT_REASON
from .validate import validate_terminal_revision
from .validate import is_text_only_spec_revision
from .init import init
from .amend_admission import _amend_admission
from .amend_precheck import _amend_precheck
from .amend_route_tasks import _amend_route_tasks
from .amend_route_state import _amend_route_state_matrix
from .amend_route_state import _ac_field_routing_hint
from .amend_route_state import _terminal_rejection_hint
from .amend_persist import _amend_persist
from .amend_persist import _is_new_test_file_path
from .amend import amend
from .amend import _annotate_if_needed
from .dry_run import _collapse
from .dry_run import classify_dry_run_failure
from .amend_precheck import _INTAKE_PRODUCT_FILES

__all__ = (
    '_SAFE_MODULE_ID_RE',
    '_validate_module_id',
    '_git_head_sha',
    '_log_amend_guard_degrade',
    '_TERMINAL_TEXT_REVISABLE_FIELDS',
    '_TERMINAL_REVISION_EVENT_REASON',
    'validate_terminal_revision',
    'is_text_only_spec_revision',
    'init',
    '_amend_admission',
    '_amend_precheck',
    '_amend_route_tasks',
    '_amend_route_state_matrix',
    '_ac_field_routing_hint',
    '_terminal_rejection_hint',
    '_amend_persist',
    '_is_new_test_file_path',
    'amend',
    '_annotate_if_needed',
    '_collapse',
    'classify_dry_run_failure',
    '_INTAKE_PRODUCT_FILES',
)
