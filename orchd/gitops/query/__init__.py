"""orchd.gitops.query — git 只读查询域（refs / workspace / landed / worktree）聚合入口。

W8-10（task-split2-w8-10-gitops-query-pkg）：旧 orchd/gitops/query.py（651 行）纯搬移拆为
本包六域。本 __init__.py 纯重导出（零顶层定义，仅有 __all__），外部
``from orchd.gitops.query import X`` / ``orchd.gitops.query.X``（含 ``orchd/gitops/__init__.py``
的 ``from .query import ...``）零改动。

依赖方向：cache / probe（叶）<- landed / refs / worktree <- workspace。

monkeypatch 打点面（W8-10 迁移）：修补 ``orchd.gitops.query.<符号>`` 只改本聚合入口的重导出
属性，对实际调用点无效；补丁须打**消费该符号的域模块属性**（如
``orchd.gitops.query.refs.get_current_branch``）。注意 ``_gitops_pkg`` 是 ``orchd.gitops``
模块对象别名——打在 ``orchd.gitops.<名>`` 上仍对属性访问式消费点生效。
"""

from .cache import _PROBE_CACHE, clear_git_probe_cache, _probe_cache_key
from .landed import (
    _LANDED_MERGE_RE, _LANDED_IMPL_RE, _LANDED_PAREN_RE, landed_task_ids_since,
    previous_release_tag,
)
from .refs import (
    get_current_branch, get_head_commit, upstream_state, get_default_branch, branch_exists,
)
from .workspace import (
    check_workspace_state, _has_content_diff, _content_changed_paths, list_tracked_changes,
)
from .worktree import _has_linked_worktrees, main_worktree_root, is_task_worktree
from .probe import _probe_git_repo_ready

__all__ = (
    '_PROBE_CACHE',
    'clear_git_probe_cache',
    '_probe_cache_key',
    '_LANDED_MERGE_RE',
    '_LANDED_IMPL_RE',
    '_LANDED_PAREN_RE',
    'landed_task_ids_since',
    'previous_release_tag',
    'get_current_branch',
    'get_head_commit',
    'upstream_state',
    'get_default_branch',
    'branch_exists',
    'check_workspace_state',
    '_has_content_diff',
    '_content_changed_paths',
    'list_tracked_changes',
    '_has_linked_worktrees',
    'main_worktree_root',
    'is_task_worktree',
    '_probe_git_repo_ready',
)
