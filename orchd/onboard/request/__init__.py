"""orchd.onboard.request — request 只读查询域（候选池/阶段闸门/冲突过滤）聚合入口。

W8-7（task-split2-w8-7-onboard-request-pkg）：旧 orchd/onboard/request.py（817 行）纯搬移
拆为本包六域。本 __init__.py 纯重导出（零顶层定义，仅有 __all__），保证：

- **18 个域符号**（15 函数 + 3 常量）经 ``from orchd.onboard.request import X`` /
  ``orchd.onboard.request.X`` 零改动，且均为声明域的原对象；
- ``orchd/onboard/__init__.py`` 的既有导入（5 个符号）零改动。

**命名空间按域收窄（显式决定，非遗漏）**：旧单文件顶层 import 时绑定（``check_workspace_state`` /
``build_pool`` / ``subprocess`` 等 30 个实现依赖）不再从本入口透出——它们是实现细节而非模块
公共面；消费方一律从原模块取，原先打在旧入口上的补丁已随卡改指 ``request/<域>``
（清单一见 ``tests/test_onboard_request_pkg.py::_OLD_IMPORT_BINDINGS``）。

依赖方向：constants / gitinfo / phase（叶）<- conflicts / routing <- entry 编排器。

monkeypatch 打点面（W8-7 迁移）：修补 ``orchd.onboard.request.<符号>`` 只改本聚合入口的
重导出属性，**对实际调用点无效**；补丁须打 ``orchd.onboard.request.<域>.<符号>``
（一个符号被多域消费时要打多个 target）。
"""

from .constants import CONFLICT_POLICY_DEFAULT, CONFLICT_POLICIES, _RETRACT_COOLDOWN_S
from .gitinfo import _git_lines, _default_branch, _inflight_files
from .phase import active_task_phase, _phase_has_open_task, _apply_phase_gate
from .conflicts import _resolve_conflict_policy, _filter_conflicts
from .routing import (
    _find_review_priority_tasks, _build_candidates, _none_ready_message, _route_by_role,
    _filter_cooldown_tasks, _held_review_claim,
)
from .entry import request

__all__ = (
    'CONFLICT_POLICY_DEFAULT',
    'CONFLICT_POLICIES',
    '_RETRACT_COOLDOWN_S',
    '_git_lines',
    '_default_branch',
    '_inflight_files',
    'active_task_phase',
    '_phase_has_open_task',
    '_apply_phase_gate',
    '_resolve_conflict_policy',
    '_filter_conflicts',
    '_find_review_priority_tasks',
    '_build_candidates',
    '_none_ready_message',
    '_route_by_role',
    '_filter_cooldown_tasks',
    '_held_review_claim',
    'request',
)
