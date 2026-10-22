"""Orchd 两阶段审查与 merge 编排（聚合入口）。

W8-4（task-split2-w8-4-review-pkg）：旧 orchd/review.py（1528 行）纯搬移拆为本包
十三域。本 __init__.py 纯重导出（零顶层定义），外部 ``from orchd.review import X``
与 ``orchd.review.X`` 属性访问零改动。

依赖方向：identity/history/branch/residual/resolve_markers（叶）<- request/
precheck/merge_run/merge_finalize <- submit；guidance 支撑段。

monkeypatch 打点面（W8-4 迁移）：try_git_merge/try_auto_resolve_conflict->merge_run、
try_delete_task_branch->merge_finalize、get_head_commit->precheck、
guard_review_write->submit；_task_branch_tip 落 precheck+merge_finalize、
main_worktree_root 落 submit+merge_run（双 target）。
"""

from .identity import is_self_review_author
from .history import (
    _recent_transitions,
    extract_review_comments,
    extract_review_history,
    extract_last_done,
    find_last_done_event,
    extract_review_baseline,
)
from .branch import _task_branch_tip, _wt_dir_name
from .residual import _merge_diagnostic_action, _own_merge_residual, _own_residual_action
from .resolve_markers import _resolve_review_resolve_markers
from .request import request_reviewer
from .precheck import _review_submit_precheck
from .record import _review_submit_record
from .decl_gate import _review_submit_decl_gate
from .merge_run import _review_submit_merge_run
from .merge_finalize import _review_submit_merge_finalize
from .guidance import _build_completion_guidance, build_reviewer_rerun_command
from .submit import review_submit, _review_submit_impl
from .precheck import get_head_commit
from .merge_run import try_git_merge
from .submit import main_worktree_root

__all__ = (
    'is_self_review_author',
    '_recent_transitions',
    'extract_review_comments',
    'extract_review_history',
    'extract_last_done',
    'find_last_done_event',
    'extract_review_baseline',
    '_task_branch_tip',
    '_wt_dir_name',
    '_merge_diagnostic_action',
    '_own_merge_residual',
    '_own_residual_action',
    '_resolve_review_resolve_markers',
    'request_reviewer',
    '_review_submit_precheck',
    '_review_submit_record',
    '_review_submit_decl_gate',
    '_review_submit_merge_run',
    '_review_submit_merge_finalize',
    '_build_completion_guidance',
    'build_reviewer_rerun_command',
    'review_submit',
    '_review_submit_impl',
    'get_head_commit',
    'try_git_merge',
    'main_worktree_root',
)
