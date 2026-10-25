"""``orchd.ledger.Store`` 骨架（file-split-program W1-L8，2026-10-16）。

Store 类本体退化为骨架：类声明（四个 mixin 基类）+ 类 docstring + ``__init__`` + 三个路径
property。其余方法按职责分置于 ``store_read`` / ``store_checks`` / ``store_write`` /
``store_apply`` 四段 mixin，类型与常量在 ``store_types``。全部方法体与 L7 基线**逐字一致**
（等价性由 AST 归一化 dump 逐方法比对），故这是物理组织变化而非逻辑变更。

本模块同时 re-export ``store_types`` 的 8 个符号（``orchd.ledger.store.<符号>`` 属性路径不变）
与四个 mixin 类。

依赖方向：标准库 + ``orchd.ledger.backend``（``_select_backend``）/ ``orchd.storage``
（``StorageBackend``）+ 本包 ``store_types`` 与四个 mixin 模块。**不得**导入 ``orchd.ledger``
聚合入口（加载期环）。
"""
from __future__ import annotations

from orchd.ledger.backend import _select_backend
from orchd.storage import StorageBackend
from pathlib import Path
from typing import Any

# 四段 mixin（基类）。
from orchd.ledger.store_read import _StoreReadMixin
from orchd.ledger.store_checks import _StoreChecksMixin
from orchd.ledger.store_write import _StoreWriteMixin
from orchd.ledger.store_apply import _StoreApplyMixin
# store_types 的 re-export：orchd.ledger.store.<符号> 属性路径保持不变。
from orchd.ledger.store_types import (
    _CHECKPOINT_SCHEMA_VERSION, _REVIEW_STALE_DEFAULT_S, TaskDerived, TaskState,
    generate_event_id, review_claim_age_s, review_stale_timeout_s,
    stale_review_claims,
)

# 本文件不含代码级环境变量读取：下文 docstring 里的 ``ORCHD_STORAGE_BACKEND`` /
# ``ORCHD_HOME`` 只是给读者的说明；变量名真源见 ``orchd.constants`` 的 ``*_ENV``。

class Store(_StoreReadMixin, _StoreChecksMixin, _StoreWriteMixin, _StoreApplyMixin):
    """事件存储引擎，封装 ledger / checkpoint / lock 的全部 I/O。

    通过 :class:`StorageBackend` 访问底层存储，默认使用 SqliteBackend
    （B-max 切默认；显式 ``ORCHD_STORAGE_BACKEND=file`` 切回文件后端）。
    可注入自定义后端以支持 ORCHD_HOME 重定向、并行化 / 远程化等场景。
    """

    def __init__(self, orchd_dir: Path, backend: StorageBackend | None = None,
                 *, readonly: bool = False) -> None:
        """初始化 Store，默认使用 SqliteBackend（经 :func:`_select_backend`）。

        Args:
            orchd_dir: ``.orchd`` 根目录（master 目录，含 ``_master.json`` +
                ``shared/``，走 git）。账本根由此目录经 :func:`resolve_store_dir`
                解析（ORCHD_HOME 设置时重定向到外部目录）。
            backend: 可选的存储后端；缺省时按 ``ORCHD_STORAGE_BACKEND``
                选择（缺省 sqlite，显式 file 回滚）。
            readonly: 只读模式（task-readonly-no-migrate）——缺省后端构造
                不建库、不迁移（转给 :func:`_select_backend`）；显式注入的
                ``backend`` 保持原样（调用方自备只读后端）。doctor 等只读
                入口经此读取已迁移部署，目录零副作用。

        派生路径（委托给 backend，账本根 = ORCHD_HOME 或 orchd_dir）：
            - ``ledger_path``:     ``<账本根>/_ledger.jsonl``，事件追加日志。
            - ``checkpoint_path``: ``<账本根>/_checkpoint.json``，状态快照。
            - ``lock_path``:       ``<账本根>/.lock``，排他文件锁。

        ``_file_lock`` 为 ExclusiveFileLock 原语实例，未持锁时 ``_lock_fd`` 为 None。
        """
        self.orchd_dir = orchd_dir
        self.backend = backend or _select_backend(orchd_dir, readonly=readonly)
        # H4（2026-08-13）：ledger 行数内存计数器。None = 未校准（惰性，
        # 首次 _current_line_count() 时以实际文件行数为准）；append 成功后
        # 已校准则 +1。避免每次写 checkpoint 全文件数行（O(L) → O(1)）。
        # 注：orchd 命令均为短进程，写路径（append → update_checkpoint）
        # 在文件锁内完成，校准必发生在 append 之后，故内存计数准确。
        self._line_count: int | None = None
        # replay 违规收集列表：显式注解，避免 mypy 从首次空列表赋值推断出空类型。
        self._replay_violations: list[dict[str, Any]] = []

    # 路径属性 / 锁 fd 转发到 backend（保持既有调用方与测试兼容）
    @property
    def ledger_path(self) -> Path:
        return self.backend.ledger_path  # type: ignore[attr-defined]

    @property
    def checkpoint_path(self) -> Path:
        return self.backend.checkpoint_path  # type: ignore[attr-defined]

    @property
    def lock_path(self) -> Path:
        return self.backend.lock_path  # type: ignore[attr-defined]
