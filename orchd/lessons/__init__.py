"""orchd.lessons — 经验回灌引擎（暂存/直落/上报/查询/审查/归档）聚合入口。

W8-8（task-split2-w8-8-lessons-pkg）：旧 orchd/lessons.py（815 行）纯搬移拆为本包八域。
本 __init__.py 纯重导出（零顶层定义，仅有 __all__），外部 ``from orchd.lessons import X`` /
``orchd.lessons.X`` 与 ``import orchd.lessons as lessons`` 的属性访问零改动。

依赖方向：constants / config（叶）<- storage <- text <- query/stage/hook <- review。

monkeypatch 打点面（W8-8 迁移，**易错点**）：修补 ``orchd.lessons.<符号>`` 只改本聚合入口的
重导出属性，对实际调用点无效。补丁必须打**消费该符号的域模块属性**——八域间是模块级
``from .storage import ...``，所以消费点读的是**本域全局名**：

- 打**定义域**（如 ``orchd.lessons.storage._append_line_unlocked``）**仅在它被本域内部调用时**
  生效；被其它域 from-import 消费时**同样无效**；
- 正例：``_append_line_unlocked`` / ``_write_rows_unlocked`` 被 hook 域消费 → 打
  ``orchd.lessons.hook.<名>``；
- 一个符号被多域消费时逐消费域打点；
- 例外：消费方用**函数级** ``from orchd.lessons import X``（调用时读属性）时，打聚合入口**有效**
  （已在 tests/test_lessons_pkg.py 的 ``_ALLOWED_AGGREGATE_PATCHES`` 登记）。

陷阱：``orchd.lessons.stage`` 等**同名函数/模块**——包属性被函数遮蔽，取子模块须
``importlib.import_module("orchd.lessons.stage")``。
"""

from .constants import (
    SYMPTOM_MAX, SOLUTION_MAX, SYMPTOM_TRUNC, SOLUTION_TRUNC, CASES_VERIFIED_CAP,
    CASES_PROPOSED_CAP, CASES_CHAR_BUDGET, LOCK_TIMEOUT_S, LESSON_ID_RE, _NEG_WORDS, _WARNING_CODES,
    _SUGGEST_REPORT_CODES,
)
from .storage import (
    _resolve_runtime, _lib_path, _staged_path, _lock_path, _read_jsonl, _append_line_unlocked,
    _write_rows_unlocked, _append_jsonl, _rewrite_jsonl,
)
from .config import load_lessons_config, is_lessons_enabled
from .text import (
    _validate_lengths, _next_lesson_id, _tokenize, _conflict_with, _version_relation, _to_case,
    _apply_char_budget,
)
from .query import lookup_lessons, _enrich_hits
from .stage import stage, add, report
from .hook import run_done_lesson_hook, _review_task_transaction
from .review import review_task, resolve_lesson, archive_lesson, list_lessons, show_lesson

__all__ = (
    'SYMPTOM_MAX',
    'SOLUTION_MAX',
    'SYMPTOM_TRUNC',
    'SOLUTION_TRUNC',
    'CASES_VERIFIED_CAP',
    'CASES_PROPOSED_CAP',
    'CASES_CHAR_BUDGET',
    'LOCK_TIMEOUT_S',
    'LESSON_ID_RE',
    '_NEG_WORDS',
    '_WARNING_CODES',
    '_SUGGEST_REPORT_CODES',
    '_resolve_runtime',
    '_lib_path',
    '_staged_path',
    '_lock_path',
    '_read_jsonl',
    '_append_line_unlocked',
    '_write_rows_unlocked',
    '_append_jsonl',
    '_rewrite_jsonl',
    'load_lessons_config',
    'is_lessons_enabled',
    '_validate_lengths',
    '_next_lesson_id',
    '_tokenize',
    '_conflict_with',
    '_version_relation',
    '_to_case',
    '_apply_char_budget',
    'lookup_lessons',
    '_enrich_hits',
    'stage',
    'add',
    'report',
    'run_done_lesson_hook',
    '_review_task_transaction',
    'review_task',
    'resolve_lesson',
    'archive_lesson',
    'list_lessons',
    'show_lesson',
)
