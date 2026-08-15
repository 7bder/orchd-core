"""Orchd 事件存储引擎 —— 基于 Event Sourcing 模式的持久化层。

本模块实现了 append-only 的事件日志（Event Ledger），通过「检查点 + 增量事件」
的算法高效重建系统状态，而非每次从头遍历全部事件。

涉及三种文件：
- JSONL Ledger（``_ledger.jsonl``）：每行一条 JSON 事件记录，仅追加、不可修改，
  是系统唯一的 "source of truth"。
- JSON Checkpoint（``_checkpoint.json``）：定期快照，记录截至某一 ledger 行号的
  全部任务状态和已撤回事件集合，用于加速 replay。
- Lock 文件（``.lock``）：跨平台排他文件锁（Windows 使用 msvcrt，POSIX 使用 fcntl），
  防止多进程并发写入导致数据损坏。

Replay 算法：
  1. 加载 checkpoint（若存在且合法），得到 ``ledger_line`` 快照行号。
  2. 从 ``ledger_line + 1`` 起读取增量事件并逐条应用。
  3. 若 checkpoint 缺失或解析失败，自动回退到全量 replay（性能降级但保证正确性）。

依赖方向：ledger.py → errors.py（不导入 spec / pool / onboard）。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from itertools import count as _count
from pathlib import Path
from typing import Any

# L5 身份审计：进程内单调计数器——Windows 时钟粒度（~15.6ms）下连续两次
# time.time_ns() 可能相同，计数器保证同进程内指纹必不同；hostname/pid/时间戳
# 保证跨会话（重启、换机）可辨识。
_FINGERPRINT_SEQ = _count()

from orchd.errors import ErrorCode, OrchdError

# 跨平台文件锁
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_LOCK_RETRIES_FLAT = [0.05, 0.1, 0.2]  # 50ms, 100ms, 200ms


@dataclass
class TaskState:
    """单个任务的当前状态（由事件流 replay 得到的派生状态）。

    任务生命周期的 6 种状态：
        - ``pending``      : 等待被认领，可被任意 agent 领取。
        - ``claimed``      : 已被某 agent 认领，正在执行中。
        - ``done``         : agent 提交完成，等待进入审核流程。
        - ``in_review``    : 正在接受审核（spec review 或 code review）。
        - ``completed``    : code review 通过，任务彻底完成。
        - ``cancelled``    : 被强制取消，不再参与调度。

    字段说明：
        status:             当前状态字符串，对应上述 6 种之一。
        claimed_by:         认领该任务的 agent ID（仅 ``claimed`` 状态有值）。
        attempt_count:      累计尝试次数，每次 DONE 事件递增；FORCE_STATUS(pending) 时重置为 0。
        review_phase:       当前审核阶段类型（如 ``"spec"`` 或 ``"code"``），无审核时为 None。
        review_claimed_by:  认领该审核的 reviewer agent ID，未认领时为 None。
        merge_warning:      代码审查通过后 git merge 未执行（环境异常/best-effort 降级），
                            标记完成但 merge 未落地，audit-merge 需告警。仅 completed 有值。
    """

    status: str = "pending"
    claimed_by: str | None = None
    attempt_count: int = 0
    review_phase: str | None = None
    review_claimed_by: str | None = None
    merge_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将任务状态序列化为字典，用于写入 checkpoint JSON。

        为保持 checkpoint 紧凑，值为 ``None`` 的可选字段（``claimed_by``、
        ``review_phase``、``review_claimed_by``、``merge_warning``）会被省略。
        始终包含 ``status`` 和 ``attempt_count``。
        """
        d: dict[str, Any] = {"status": self.status, "attempt_count": self.attempt_count}
        if self.claimed_by:
            d["claimed_by"] = self.claimed_by
        if self.review_phase:
            d["review_phase"] = self.review_phase
        if self.review_claimed_by:
            d["review_claimed_by"] = self.review_claimed_by
        if self.merge_warning:
            d["merge_warning"] = self.merge_warning
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskState:
        """从 checkpoint 字典反序列化为 TaskState 实例。

        与 :meth:`to_dict` 互为逆操作；对缺失的可选字段使用安全默认值
        （``status`` 默认 ``"pending"``，其余可选字段默认 ``None``）。
        """
        return cls(
            status=d.get("status", "pending"),
            claimed_by=d.get("claimed_by"),
            attempt_count=d.get("attempt_count", 0),
            review_phase=d.get("review_phase"),
            review_claimed_by=d.get("review_claimed_by"),
            merge_warning=d.get("merge_warning"),
        )


def generate_event_id() -> str:
    """生成事件 ID：evt-{uuid4-hex-8}。"""
    return f"evt-{uuid.uuid4().hex[:8]}"


@dataclass
class TaskDerived:
    """从 ledger 单次扫描得到的 per-task 派生信息（H2，2026-08-13 性能审核）。

    供 request / claim / done / review_submit 复用，消除「每个查询都从头
    全扫 ledger」的重复 O(L) 扫描（原实现单命令内可能触发 2-4 次全扫）。
    三类信息均与 ``_extract_*`` 辅助函数的语义一一对应：
    - ``last_done``      : task_id → 最近 DONE 事件 dict（正序遍历后者覆盖）。
    - ``review_comments``: task_id → 全部 REVIEW_SUBMITTED 的 comments（正序）。
    - ``review_baselines``: (task_id, agent_id) → 最近 REVIEW_CLAIMED 的
      ``baseline_sha``（正序遍历后者覆盖 = 最近一次）。

    与 replay 同源（同一份 ledger、同一 :meth:`Store._read_ledger_lines`
    容错语义：末行损坏跳过、中间行损坏 E002），保证「读到的辅助信息」与
    「派生状态」一致。
    """

    last_done: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_comments: dict[str, list[str]] = field(default_factory=dict)
    review_baselines: dict[tuple[str, str], str] = field(default_factory=dict)


def generate_session_fingerprint() -> str:
    """生成 session 指纹：hostname + pid + timestamp 的 SHA-256 短哈希（前 12 位）。

    用于事件溯源的身份审计（ROADMAP 1.1 L5）：区分同一 agent_id 的不同 session，
    跨会话（hostname / pid 变化）可辨识。旧事件缺失该字段时 replay 兼容
    （``_apply_event`` 只读已知键，未知键自然忽略），视为 None。
    """
    import hashlib
    import os
    import socket

    raw = f"{socket.gethostname()}|{os.getpid()}|{time.time_ns()}|{next(_FINGERPRINT_SEQ)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def resolve_store_dir(orchd_dir: Path) -> Path:
    """解析账本根目录（task-orchd-home-redirect，roadmap:snapshotstore-m-p0）。

    账本（ledger / checkpoint / lock / mod-*）由环境变量 ``ORCHD_HOME`` 重定向到
    外部目录；未设置时回退到传入的 ``orchd_dir``（默认 ``<cwd 向上找的 .orchd/>``）。

    注意：``orchd_dir`` 语义是「master 目录」（含 ``_master.json`` + ``shared/``，
    走 git，不入 backend）；返回的账本根仅用于 FilesystemBackend 派生账本文件路径。
    """
    home = os.environ.get("ORCHD_HOME")
    if home:
        return Path(home)
    return orchd_dir


_WORKSPACE_DOCS = ("IDEAS.md", "ROADMAP.md", "SKILL.md")


def resolve_workspace_root(project_root: Path) -> Path:
    """解析工作区文档根目录（IDEAS.md / ROADMAP.md / SKILL.md 所在目录）。

    task-12-engine-path-abstraction 双态兼容：
    - 根布局（开发态）：工作区文档在项目根 → 返回 ``project_root``。
    - 发布态布局（自包含 ``.orchd/``）：工作区文档归置 ``.orchd/`` → 返回
      ``project_root / ".orchd"``。

    判定：``.orchd/`` 下已存在任一工作区文档 → 发布态；否则若项目根存在 → 开发态；
    两者都无 → 默认返回 ``.orchd/``（发布态默认，AC3）。

    调用方：``validate_source``（spec.py）、``archive_resolved_ideas``（ideas.py）、
    ``amend``/``ideas_archive``（cli.py）——统一经本 helper 解析 IDEAS/ROADMAP/SKILL 路径。
    """
    project_root = Path(project_root)
    orchd_dir = project_root / ".orchd"
    if any((orchd_dir / name).exists() for name in _WORKSPACE_DOCS):
        return orchd_dir
    if any((project_root / name).exists() for name in _WORKSPACE_DOCS):
        return project_root
    return orchd_dir


class StorageBackend(ABC):
    """存储后端抽象：Store 通过本接口访问 ledger / checkpoint / lock 的全部 I/O。

    引入目的（task-storage-backend-interface，roadmap:snapshotstore-m-p0）：
    将 Store 与底层存储位置解耦，为 ORCHD_HOME 账本根重定向、并行化 / 远程化
    打地基。默认实现 :class:`FilesystemBackend` 保持当前行为与路径不变。

    接口暴露七个方法：append_event / read_events / event_count /
    load_checkpoint / save_checkpoint / acquire_lock / release_lock。
    """

    @abstractmethod
    def append_event(self, event: dict[str, Any]) -> None:
        """以 append 模式写一条事件到 ledger（追加 + fsync）。"""

    @abstractmethod
    def read_events(self, from_line: int = 1) -> list[dict[str, Any]]:
        """读取 ledger 事件（从 ``from_line`` 起，1-based 行号）。

        容错语义与既有实现一致：末行损坏跳过 + warning，中间行损坏抛 E002。
        ``from_line`` 必须在文件层先跳过前 ``from_line-1`` 行再解析——保证
        checkpoint 之前（已被快照覆盖）的损坏行不会被解析（B-1 修复，
        恢复增量 replay 的容错语义）。
        """

    @abstractmethod
    def event_count(self) -> int:
        """返回 ledger 事件总数（行数）。"""

    @abstractmethod
    def load_checkpoint(self) -> dict[str, Any] | None:
        """读取 checkpoint。文件不存在或解析失败返回 None。"""

    @abstractmethod
    def save_checkpoint(self, data: dict[str, Any]) -> None:
        """原子写入 checkpoint（write-tmp + os.replace）。"""

    @abstractmethod
    def acquire_lock(self) -> None:
        """获取排他文件锁，重试 50/100/200ms，全部失败抛 E012。"""

    @abstractmethod
    def release_lock(self) -> None:
        """释放文件锁。"""


class FilesystemBackend(StorageBackend):
    """默认文件系统存储后端：行为与路径与改造前完全一致。

    持有三个文件路径（ledger / checkpoint / lock）与锁文件描述符；Store 的
    ``ledger_path`` / ``checkpoint_path`` / ``lock_path`` / ``_lock_fd``
    属性转发到本后端，保证既有测试与调用方兼容。
    """

    def __init__(self, orchd_dir: Path) -> None:
        self.orchd_dir = orchd_dir
        self.ledger_path = orchd_dir / "_ledger.jsonl"
        self.checkpoint_path = orchd_dir / "_checkpoint.json"
        self.lock_path = orchd_dir / ".lock"
        self._lock_fd: int | None = None

    def append_event(self, event: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(str(self.ledger_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def read_events(self, from_line: int = 1) -> list[dict[str, Any]]:
        """读取 ledger 事件（从 ``from_line`` 起，1-based 行号）。

        ``from_line`` 语义：在文件层先跳过前 ``from_line-1`` 行，再解析剩余行。
        这样 checkpoint 之前（已被快照覆盖）的损坏行不会被解析——B-1 修复，
        恢复增量 replay 的容错语义（重构前 ``_read_ledger_lines`` 直接在文件层
        跳过，不解析被跳过的行）。

        容错规则：
        - 最后一行 JSON 解析失败 → 跳过 + warning（可能写入未完成）
        - 中间行解析失败 → E002
        """
        if not self.ledger_path.exists():
            return []
        events: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
        # 文件层跳过前 from_line-1 行：被跳过的行不参与解析（含损坏行）
        start = max(0, from_line - 1)
        for i in range(start, len(raw_lines)):
            stripped = raw_lines[i].strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError:
                # 末行（含末尾空行）损坏 → 可能写入未完成，仅 warning 跳过；
                # 中间行损坏 → 数据损坏，抛 E002 中断。
                is_last = i == len(raw_lines) - 1 or all(
                    not raw_lines[j].strip() for j in range(i + 1, len(raw_lines))
                )
                if is_last:
                    warnings.warn(
                        f"ledger 最后一行解析失败，已跳过: {stripped[:80]}",
                        stacklevel=2,
                    )
                else:
                    raise OrchdError(
                        ErrorCode.E002,
                        f"ledger 中间行 JSON 解析失败（第 {i + 1} 行），数据可能损坏",
                        [{"line": i + 1, "content": stripped[:200]}],
                    )
        return events

    def event_count(self) -> int:
        if not self.ledger_path.exists():
            return 0
        count = 0
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for _ in f:
                count += 1
        return count

    def load_checkpoint(self) -> dict[str, Any] | None:
        if not self.checkpoint_path.exists():
            return None
        try:
            return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def save_checkpoint(self, data: dict[str, Any]) -> None:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.checkpoint_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(self.checkpoint_path))

    def acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)
        for delay in _LOCK_RETRIES_FLAT:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lock_fd = fd
                return
            except (OSError, IOError):
                time.sleep(delay)
        os.close(fd)
        raise OrchdError(
            ErrorCode.E012,
            "lock_timeout: failed to acquire .orchd/.lock after 3 retries",
            [{"path": str(self.lock_path), "message": "重试 50/100/200ms 均失败"}],
        )

    def release_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            if sys.platform == "win32":
                msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None


class Store:
    """事件存储引擎，封装 ledger / checkpoint / lock 的全部 I/O。

    通过 :class:`StorageBackend` 访问底层存储，默认使用 :class:`FilesystemBackend`
    （行为与路径和改造前完全一致）。可注入自定义后端以支持 ORCHD_HOME 重定向、
    并行化 / 远程化等场景。
    """

    def __init__(self, orchd_dir: Path, backend: StorageBackend | None = None) -> None:
        """初始化 Store，默认使用 :class:`FilesystemBackend`。

        Args:
            orchd_dir: ``.orchd`` 根目录（master 目录，含 ``_master.json`` +
                ``shared/``，走 git）。账本根由此目录经 :func:`resolve_store_dir`
                解析（ORCHD_HOME 设置时重定向到外部目录）。
            backend: 可选的存储后端；缺省时按 ``resolve_store_dir(orchd_dir)``
                构造 FilesystemBackend。

        派生路径（委托给 backend，账本根 = ORCHD_HOME 或 orchd_dir）：
            - ``ledger_path``:     ``<账本根>/_ledger.jsonl``，事件追加日志。
            - ``checkpoint_path``: ``<账本根>/_checkpoint.json``，状态快照。
            - ``lock_path``:       ``<账本根>/.lock``，排他文件锁。

        ``_lock_fd`` 记录当前持有的锁文件描述符，未持锁时为 ``None``。
        """
        self.orchd_dir = orchd_dir
        self.backend = backend or FilesystemBackend(resolve_store_dir(orchd_dir))
        # H4（2026-08-13）：ledger 行数内存计数器。None = 未校准（惰性，
        # 首次 _current_line_count() 时以实际文件行数为准）；append 成功后
        # 已校准则 +1。避免每次写 checkpoint 全文件数行（O(L) → O(1)）。
        # 注：orchd 命令均为短进程，写路径（append → update_checkpoint）
        # 在文件锁内完成，校准必发生在 append 之后，故内存计数准确。
        self._line_count: int | None = None

    # 路径属性 / 锁 fd 转发到 backend（保持既有调用方与测试兼容）
    @property
    def ledger_path(self) -> Path:
        return self.backend.ledger_path

    @property
    def checkpoint_path(self) -> Path:
        return self.backend.checkpoint_path

    @property
    def lock_path(self) -> Path:
        return self.backend.lock_path

    @property
    def _lock_fd(self) -> int | None:
        return self.backend._lock_fd

    @_lock_fd.setter
    def _lock_fd(self, value: int | None) -> None:
        self.backend._lock_fd = value

    # ------------------------------------------------------------------
    # 文件锁
    # ------------------------------------------------------------------

    def acquire_lock(self) -> None:
        """获取排他文件锁。重试 50/100/200ms，全部失败抛 E012。"""
        self.backend.acquire_lock()

    def release_lock(self) -> None:
        """释放文件锁。"""
        self.backend.release_lock()

    # ------------------------------------------------------------------
    # Ledger 写入
    # ------------------------------------------------------------------

    def append_event(self, event: dict[str, Any]) -> None:
        """以 append 模式写入 JSONL，写入后 flush + fsync。"""
        self.backend.append_event(event)
        # H4：已校准时追加 1 行（写入成功才递增；未校准则保持 None 惰性校准）
        if self._line_count is not None:
            self._line_count += 1

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay(self) -> dict[str, TaskState]:
        """从 checkpoint + 增量 replay 重建任务状态。

        增量优化：优先加载 checkpoint 快照，仅读取 checkpoint 之后的增量事件，
        避免每次全量遍历整个 ledger，大幅提升大型项目的 replay 性能。

        容错规则：
        - checkpoint 解析失败 → 回退全量 replay
        - 最后一行 JSON 解析失败 → 跳过 + warning
        - 中间行解析失败 → E002
        """
        checkpoint_line, tasks, retracted = self._load_checkpoint()
        events = self._read_ledger_lines(from_line=checkpoint_line + 1)
        self._apply_events(events, tasks, retracted)
        return tasks

    def replay_full(self) -> dict[str, TaskState]:
        """全量 replay（忽略 checkpoint）。"""
        tasks: dict[str, TaskState] = {}
        retracted: set[str] = set()
        events = self._read_ledger_lines(from_line=1)
        self._apply_events(events, tasks, retracted)
        return tasks

    def _load_checkpoint(self) -> tuple[int, dict[str, TaskState], set[str]]:
        """加载 checkpoint 文件，返回 ``(ledger_line, tasks, retracted)``。

        回退行为：
            - checkpoint 文件不存在：返回 ``(0, {}, set())``，调用者将从第 1 行全量 replay。
            - checkpoint 解析失败（JSON 损坏 / 字段缺失）：打印 warning，自动调用
              :meth:`replay_full` 全量重建状态，并返回 ``(total_lines, tasks, set())``。
              此时 ``total_lines`` 是 ledger 的总行数，表示已全量 replay 完毕，
              调用者无需再读取增量事件。

        Returns:
            三元组：
            - ``int``: checkpoint 对应的 ledger 行号（增量 replay 从该行之后开始）。
            - ``dict[str, TaskState]``: 截至该行号的任务状态快照。
            - ``set[str]``: 已撤回的事件 ID 集合。
        """
        data = self.backend.load_checkpoint()
        if data is None:
            # backend 返回 None：区分「checkpoint 不存在」与「解析失败」。
            # 前者正常返回空快照；后者回退全量 replay（保持既有 warning 语义）。
            if not self.checkpoint_path.exists():
                return 0, {}, set()
            warnings.warn("checkpoint 解析失败，回退全量 replay", stacklevel=2)
            tasks = self.replay_full()
            # 返回一个特殊标记让调用者知道已全量 replay
            total_lines = self._count_ledger_lines()
            return total_lines, tasks, set()
        # 兼容旧格式：tasks 为 dict[str, TaskState.from_dict]
        ledger_line = data.get("ledger_line", 0)
        tasks = {
            tid: TaskState.from_dict(ts) for tid, ts in data.get("tasks", {}).items()
        }
        retracted = set(data.get("retracted", []))
        return ledger_line, tasks, retracted

    def _count_ledger_lines(self) -> int:
        return self.backend.event_count()

    def _replay_prefix(self, n: int) -> dict[str, TaskState]:
        """重放 ledger 前 ``n`` 条事件，返回派生任务状态（忽略 checkpoint）。

        供 :meth:`check_integrity` 使用：以 checkpoint 声明的 ``ledger_line``
        为界，重放该前缀事件并与 checkpoint 的 ``tasks`` 快照比对，检测运行时
        文件（ledger / checkpoint）被手改的篡改。

        注意：停用 RETRACT 递归（改用 :meth:`_apply_events_no_retract`）——
        前缀重放只关心前 ``n`` 条事件的派生结果，若 RETRACT 触发全量重建会越过
        前缀边界读到 ``n`` 之后的事件，破坏前缀语义。
        """
        events = self.backend.read_events()[:n]
        tasks: dict[str, TaskState] = {}
        retracted: set[str] = set()
        # 与 _rebuild_after_retract 一致：先完整收集前缀内的 RETRACT 目标，
        # 再应用非撤回事件，避免被撤回事件「复活」。
        for ev in events:
            if ev.get("type") == "RETRACT":
                te = ev.get("target_event_id", "")
                if te:
                    retracted.add(te)
        self._apply_events_no_retract(events, tasks, retracted)
        return tasks

    def check_integrity(self) -> list[dict[str, Any]]:
        """校验 ledger 与 checkpoint 一致性（红线 8，R3，只读，不自动修复）。

        检测手改运行时文件（``_ledger.jsonl`` / ``_checkpoint.json``）的篡改：
        1. checkpoint.``ledger_line`` 必须 <= 实际 ledger 行数（ledger 被截断 /
           checkpoint 行号被改大）；
        2. 重放前 ``ledger_line`` 条事件，必须与 checkpoint 的 ``tasks`` 快照一致
           （checkpoint 快照被改 / ledger 早期事件被改）。

        返回告警列表；一致（或 checkpoint 缺失）时返回空列表。仅告警，不阻断
        合法操作（对齐 §3 判据 3 降级路径）。告警用 ``code=E030``，不抛异常。
        """
        warnings_list: list[dict[str, Any]] = []
        checkpoint = self.backend.load_checkpoint()
        if checkpoint is None:
            # checkpoint 缺失（未写过快照 / 解析失败）→ 视为未校验到篡改
            return warnings_list

        ledger_line = checkpoint.get("ledger_line")
        if not isinstance(ledger_line, int) or ledger_line < 0:
            warnings_list.append({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": (
                    f"checkpoint.ledger_line 非法（{ledger_line!r}），"
                    "运行时文件疑似被篡改（不自动修复）"
                ),
                "path": str(self.checkpoint_path),
            })
            return warnings_list

        actual = self._count_ledger_lines()
        if ledger_line > actual:
            warnings_list.append({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": (
                    f"checkpoint.ledger_line={ledger_line} 超过实际 ledger 行数 "
                    f"{actual}，运行时文件疑似被篡改；建议人工核对（不自动修复）"
                ),
                "path": str(self.checkpoint_path),
            })
            return warnings_list

        try:
            derived = self._replay_prefix(ledger_line)
        except OrchdError:
            # ledger 中间行损坏（E002）——replay 处已抛，此处仅附加告警
            warnings_list.append({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": "ledger 中间行 JSON 解析失败，运行时文件疑似被篡改（不自动修复）",
                "path": str(self.ledger_path),
            })
            return warnings_list

        derived_dict = {tid: ts.to_dict() for tid, ts in derived.items()}
        ck_tasks = checkpoint.get("tasks") or {}
        if derived_dict != ck_tasks:
            warnings_list.append({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": (
                    "checkpoint 快照与 ledger 重放结果不一致，运行时文件疑似被篡改"
                    "（不自动修复）"
                ),
                "path": str(self.checkpoint_path),
            })
        return warnings_list

    def _current_line_count(self) -> int:
        """返回 ledger 当前行数：已校准则 O(1) 返回内存计数，未校准则
        全文件数行校准一次（H4，2026-08-13）。

        内存计数的正确性前提：orchd 命令为短进程，写路径（append →
        update_checkpoint）在文件锁内完成，故首次校准必发生在 append
        之后（校准值即含已追加行），此后 append 逐行 +1 保持同步。
        """
        if self._line_count is None:
            self._line_count = self._count_ledger_lines()
        return self._line_count

    def _read_ledger_lines(self, from_line: int) -> list[dict[str, Any]]:
        """读取 ledger 从 ``from_line``（1-based）起的所有事件。

        参数 ``from_line`` 使用 1-based 行号，即 ``from_line=1`` 表示从文件第一行开始读取。
        内部实现委托 ``backend.read_events(from_line)``，在文件层先跳过前
        ``from_line - 1`` 行，从而定位到起始行（B-1 修复：被跳过的早期损坏行
        不参与解析，恢复 checkpoint 增量 replay 的容错语义）。

        容错：最后一行解析失败跳过+warning；中间行失败抛 E002。
        """
        return self.backend.read_events(from_line=from_line)

    def _apply_events(
        self,
        events: list[dict[str, Any]],
        tasks: dict[str, TaskState],
        retracted: set[str],
    ) -> None:
        """事件驱动的状态机：逐条应用事件列表，原地修改 ``tasks`` 和 ``retracted``。

        支持的事件类型及其效果：
            - ``CLAIMED``          : 任务被 agent 认领，状态 → ``claimed``，清空审核字段。
            - ``DONE``             : agent 提交完成，状态 → ``done``，递增 ``attempt_count``。
            - ``REVIEW_READY``     : 进入审核，状态 → ``in_review``，设置 ``review_phase``。
            - ``REVIEW_CLAIMED``   : reviewer 认领审核，设置 ``review_claimed_by``。
            - ``REVIEW_SUBMITTED`` : 审核结果提交。``APPROVED``(code) → ``completed``；
                                    ``CHANGES_REQUESTED`` → 回退 ``pending``。
            - ``FORCE_STATUS``     : 强制覆盖状态到指定值，附带相关字段重置逻辑。
            - ``RETRACT``          : 撤回指定事件，触发全量重建（参见 :meth:`_rebuild_after_retract`）。

        已被撤回（``retracted`` 集合中）的事件会被跳过。没有 ``task_id`` 的事件也会被忽略。
        """
        for event in events:
            eid = event.get("event_id", "")
            if eid in retracted:
                # 跳过已被 RETRACT 撤回的事件，不纳入状态计算
                continue

            etype = event.get("type", "")
            task_id = event.get("task_id", "")
            if not task_id:
                # 没有 task_id 的事件（如系统级事件）不影响任务状态，直接跳过
                continue

            if task_id not in tasks:
                # 首次出现的 task_id，初始化默认状态（pending）
                tasks[task_id] = TaskState()
            ts = tasks[task_id]

            if etype == "CLAIMED":
                ts.status = "claimed"
                ts.claimed_by = event.get("agent_id")
                ts.review_phase = None
                ts.review_claimed_by = None

            elif etype == "DONE":
                ts.status = "done"
                ts.attempt_count = event.get("attempt_count", ts.attempt_count + 1)

            elif etype == "REVIEW_READY":
                ts.status = "in_review"
                ts.review_phase = event.get("review_type")
                ts.review_claimed_by = None

            elif etype == "REVIEW_CLAIMED":
                ts.review_claimed_by = event.get("agent_id")

            elif etype == "REVIEW_SUBMITTED":
                verdict = event.get("verdict", "")
                review_type = event.get("review_type", "")
                if verdict == "APPROVED":
                    if review_type == "code":
                        # code review 通过 → 任务彻底完成
                        ts.status = "completed"
                        ts.review_phase = None
                        ts.review_claimed_by = None
                        # B1（2026-08-13 full-audit-v2）：merge 降级标记随事件持久化，
                        # completed 但 merge 未落地时保留 merge_warning 供 audit-merge 告警
                        ts.merge_warning = event.get("merge_warning")
                    else:
                        # spec review 通过但 code review 尚未开始，
                        # CLI 层会自动生成 REVIEW_READY(code) 事件，此处无需处理
                        pass
                elif verdict == "CHANGES_REQUESTED":
                    # 审核被驳回 → 任务回退到 pending，清空认领和审核信息。
                    # 注意：不重置 attempt_count——attempt_count 累计「打回次数」，
                    # 供 request 的 max_attempts 上限警告（exceeded_max_attempts）；
                    # 仅 force-status pending 才重置计数（人工恢复手段）。
                    ts.status = "pending"
                    ts.claimed_by = None
                    ts.review_phase = None
                    ts.review_claimed_by = None

            elif etype == "FORCE_STATUS":
                # 强制状态覆盖：根据 target_status 重置关联字段，
                # 确保派生状态与被强制设置的状态保持一致
                target = event.get("target_status", "pending")
                ts.status = target
                if target == "pending":
                    # 回退到待认领：清空所有执行和审核相关字段
                    ts.attempt_count = 0
                    ts.review_phase = None
                    ts.review_claimed_by = None
                    ts.claimed_by = None
                elif target == "claimed":
                    # 强制认领：设置指派的 agent
                    ts.claimed_by = event.get("assignee")
                elif target == "cancelled":
                    # 强制取消：清空所有执行和审核相关字段
                    ts.claimed_by = None
                    ts.review_phase = None
                    ts.review_claimed_by = None
                elif target == "completed":
                    # 强制完成：保留实现者信息（claimed_by），仅清空审查字段，
                    # 与 code APPROVED 语义对齐（避免 completed 状态残留审查阶段/审查者）
                    ts.review_phase = None
                    ts.review_claimed_by = None

            elif etype == "RETRACT":
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
                    # RETRACT 具有级联效应：撤回某事件后，后续依赖该事件的状态变化
                    # 都需要重新计算，因此必须从头全量重建
                    self._rebuild_after_retract(tasks, retracted)
                    # 性能（H1，2026-08-13）：_rebuild_after_retract 已从第 1 行
                    # 全量重放全部事件（含本 RETRACT 之后的事件），tasks 已是最终
                    # 状态。此处必须 return，避免：
                    #   ① 后续事件被重复应用（重建已应用过一遍）；
                    #   ② 后续 RETRACT 再次触发全量重建——K 个 RETRACT 原实现触发
                    #      K 次 O(L) 重建（O(K·L)），单次重建后即 O(L)。
                    return

    def _rebuild_after_retract(
        self, tasks: dict[str, TaskState], retracted: set[str]
    ) -> None:
        """RETRACT 后从头全量重建任务状态。

        为什么不能增量修补：RETRACT 撤回的事件可能已被后续事件依赖
        （如 CLAIMED → DONE → REVIEW_READY 链条中撤回 CLAIMED），
        单纯"反向操作"无法正确还原状态，必须从第 1 行开始重新应用
        所有未被撤回的事件，以保证最终状态的完整性和一致性。

        正确性关键（P0 修复）：重建前必须先完整收集所有 RETRACT 事件的
        ``target_event_id``，再应用非撤回事件。若边应用边累积 ``retracted``，
        则「早于 RETRACT 事件出现的被撤回事件」会先被应用、后才发现被撤回，
        导致已撤回事件「复活」（例如 checkpoint 之前发生过 retract、之后又
        来一次 retract 触发重建时，checkpoint 前的 retract 集合若缺失就会
        静默错乱状态）。
        """
        tasks.clear()
        all_events = self._read_ledger_lines(from_line=1)
        # 先完整收集所有 RETRACT 的目标事件 ID，再应用事件（两遍扫描）
        for event in all_events:
            if event.get("type") == "RETRACT":
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
        self._apply_events_no_retract(all_events, tasks, retracted)

    def _apply_events_no_retract(
        self,
        events: list[dict[str, Any]],
        tasks: dict[str, TaskState],
        retracted: set[str],
    ) -> None:
        """全量应用事件（跳过 retracted），不再递归处理 RETRACT。

        与 :meth:`_apply_events` 的区别：
            - 本方法在遇到 RETRACT 事件时仅记录 ``target_event_id`` 到 ``retracted``
              集合，**不会**再触发 ``_rebuild_after_retract``，避免无限递归。
            - 仅在 ``_rebuild_after_retract`` 内部调用，用于全量重建场景。
            - 逻辑与 ``_apply_events`` 基本一致，但去掉了递归 RETRACT 分支。
        """
        for event in events:
            eid = event.get("event_id", "")
            if eid in retracted:
                continue

            etype = event.get("type", "")
            task_id = event.get("task_id", "")
            if not task_id:
                continue

            if etype == "RETRACT":
                # 仅记录被撤回的事件 ID，不触发全量重建（避免递归）
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
                continue

            if task_id not in tasks:
                tasks[task_id] = TaskState()
            ts = tasks[task_id]

            if etype == "CLAIMED":
                ts.status = "claimed"
                ts.claimed_by = event.get("agent_id")
                ts.review_phase = None
                ts.review_claimed_by = None
            elif etype == "DONE":
                ts.status = "done"
                ts.attempt_count = event.get("attempt_count", ts.attempt_count + 1)
            elif etype == "REVIEW_READY":
                ts.status = "in_review"
                ts.review_phase = event.get("review_type")
                ts.review_claimed_by = None
            elif etype == "REVIEW_CLAIMED":
                ts.review_claimed_by = event.get("agent_id")
            elif etype == "REVIEW_SUBMITTED":
                verdict = event.get("verdict", "")
                review_type = event.get("review_type", "")
                if verdict == "APPROVED" and review_type == "code":
                    ts.status = "completed"
                    ts.review_phase = None
                    ts.review_claimed_by = None
                    # B1（2026-08-13 full-audit-v2）：与 _apply_events 保持一致——
                    # merge 降级标记随事件持久化。全量重建（RETRACT / check_integrity
                    # 前缀重放）若不设置，merge_warning 将丢失：既让 audit-merge
                    # 对漏 merge 失明，又会与含 merge_warning 的 checkpoint 快照
                    # 不一致而误报 E030 篡改告警。
                    ts.merge_warning = event.get("merge_warning")
                elif verdict == "CHANGES_REQUESTED":
                    # 不重置 attempt_count（与 _apply_events 一致）：打回次数累计，
                    # 供 request 的 max_attempts 上限警告；仅 force-status pending 重置。
                    ts.status = "pending"
                    ts.claimed_by = None
                    ts.review_phase = None
                    ts.review_claimed_by = None
            elif etype == "FORCE_STATUS":
                # 强制状态覆盖：与 _apply_events 中逻辑一致
                target = event.get("target_status", "pending")
                ts.status = target
                if target == "pending":
                    ts.attempt_count = 0
                    ts.review_phase = None
                    ts.review_claimed_by = None
                    ts.claimed_by = None
                elif target == "claimed":
                    ts.claimed_by = event.get("assignee")
                elif target == "cancelled":
                    ts.claimed_by = None
                    ts.review_phase = None
                    ts.review_claimed_by = None
                elif target == "completed":
                    # 与 _apply_events 一致：强制完成仅清空审查字段
                    ts.review_phase = None
                    ts.review_claimed_by = None

    # ------------------------------------------------------------------
    # Checkpoint
    # ------------------------------------------------------------------

    def update_checkpoint(self, state: dict[str, TaskState], retracted: set[str] | None = None) -> None:
        """原子更新 checkpoint：write-to-tmp + os.replace。

        原子写入的必要性：先写入 ``.tmp`` 临时文件再通过 ``os.replace`` 原子替换，
        确保在写入过程中进程崩溃或断电时，旧的 checkpoint 仍然完整可用，
        不会因写入中断而产生半截损坏的 JSON 文件。

        正确性（P0 修复）：``retracted`` 缺省（None）时，本方法不再依赖调用方
        透传，而是直接从 ledger 扫描所有 ``RETRACT`` 事件自算完整撤回集合，
        随快照一起持久化。此前所有写操作调用 ``update_checkpoint(new_state)``
        均不传 ``retracted``，导致 checkpoint 从不写入撤回集合；一旦 checkpoint
        之后又发生新 RETRACT，全量重建时会因撤回集合不完整而让已撤回事件「复活」，
        任务状态被静默改写。
        """
        if retracted is None:
            retracted = self._collect_retracted_event_ids()
        # H4：行数由内存计数器提供（O(1)），不再全文件数行
        total_lines = self._current_line_count()
        checkpoint = {
            "ledger_line": total_lines,
            "tasks": {tid: ts.to_dict() for tid, ts in state.items()},
        }
        if retracted:
            checkpoint["retracted"] = sorted(retracted)

        self.backend.save_checkpoint(checkpoint)

    def _collect_retracted_event_ids(self) -> set[str]:
        """扫描 ledger 中所有 RETRACT 事件的 ``target_event_id``，得到完整撤回集合。

        用于 ``update_checkpoint`` 在调用方未显式传入 ``retracted`` 时自算持久化集合
        （P0 修复：checkpoint 必须记录 checkpoint 之前的撤回，否则之后的 retract
        重建会复活已撤回事件）。ledger 不存在时返回空集合。
        """
        retracted: set[str] = set()
        if not self.ledger_exists():
            return retracted
        for event in self._read_ledger_lines(from_line=1):
            if event.get("type") == "RETRACT":
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
        return retracted

    def scan_task_derived(self) -> TaskDerived:
        """单次扫描 ledger 构建 per-task 派生信息缓存（H2，2026-08-13 性能审核）。

        一次 O(L) 遍历同时提取 DONE 最近事件 / REVIEW_SUBMITTED 意见 /
        REVIEW_CLAIMED baseline 三类信息，供 request/claim/done/review_submit
        的多次查询复用（原实现每个 ``_extract_*`` 查询都从头全扫，单命令内
        可能 2-4 次 O(L)）。ledger 不存在时返回空缓存。
        """
        info = TaskDerived()
        if not self.ledger_exists():
            return info
        # M-1（2026-08-12）：与 replay() 同源——先收集完整撤回集合，跳过被
        # RETRACT 撤回的事件，保证派生缓存与派生状态一致（否则被撤回的 DONE /
        # REVIEW_SUBMITTED / REVIEW_CLAIMED 仍会污染 last_done / review_comments /
        # review_baselines）。两遍扫描与 _rebuild_after_retract 语义一致。
        retracted = self._collect_retracted_event_ids()
        for event in self._read_ledger_lines(from_line=1):
            if event.get("event_id", "") in retracted:
                continue
            etype = event.get("type", "")
            tid = event.get("task_id", "")
            if not tid:
                continue
            if etype == "DONE":
                # 正序遍历，后者覆盖前者 → last_done 保持「最近一次」
                info.last_done[tid] = event
            elif etype == "REVIEW_SUBMITTED":
                comments = event.get("comments")
                if comments:
                    info.review_comments.setdefault(tid, []).append(comments)
            elif etype == "REVIEW_CLAIMED":
                aid = event.get("agent_id")
                if aid:
                    # 正序遍历后者覆盖 → (tid, aid) 保持「最近一次」的 baseline
                    info.review_baselines[(tid, aid)] = event.get("baseline_sha")
        return info

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def ledger_exists(self) -> bool:
        """检查 ledger 文件是否存在，用于判断项目是否已初始化过事件流。"""
        return self.ledger_path.exists()

    def ledger_line_count(self) -> int:
        """返回 ledger 文件的总行数（即事件总数），用于进度展示和 checkpoint 对齐。"""
        return self._current_line_count()


def open_store(orchd_dir: Path | str) -> Store:
    """打开或创建 Store 实例。

    推荐使用场景：外部模块（如 CLI、spec、pool）应通过本函数获取 Store 实例，
    因为它会校验 ``orchd_dir`` 是否存在，未初始化时抛出 E013 错误提示用户执行
    ``orchd init``。直接调用 ``Store(orchd_dir)`` 构造函数则跳过此校验，
    适用于内部流程已确保目录存在的场景（如 ``orchd init`` 本身）。

    Args:
        orchd_dir: ``.orchd`` 目录路径，支持 ``str`` 和 ``Path``。

    Raises:
        OrchdError E013: orchd_dir 不存在（未初始化）。
    """
    orchd_dir = Path(orchd_dir)
    if not orchd_dir.is_dir():
        raise OrchdError(
            ErrorCode.E013,
            f"not_initialized: {orchd_dir} does not exist",
            [{"path": str(orchd_dir), "message": ".orchd/ 目录缺失，请先执行 orchd init"}],
        )
    return Store(orchd_dir)
