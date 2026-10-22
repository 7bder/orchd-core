"""``orchd.ledger.Store`` 一致性诊断 mixin（file-split-program W1-L8，2026-10-16）。

承载 ``_StoreChecksMixin``：``check_integrity`` / ``_corrupt_line_warnings`` /
``reconcile_counters`` / ``counter_reconcile_stats`` / ``_current_line_count`` /
``_record_soft_violation``；方法体与 L7 基线逐字一致。

依赖方向：标准库 + ``orchd.contracts.events`` / ``orchd.errors`` / ``orchd.lockfile`` +
``orchd.ledger.store_types``；对 ``resolve_store_dir`` 与 ``_attach_structured_guidance`` 用
**函数级惰性导入**（保持 ``orchd.ledger.<名>`` 的 monkeypatch 补丁面）。不得导入聚合入口。
"""
from __future__ import annotations

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger.store_types import TaskState, _CHECKPOINT_SCHEMA_VERSION
from orchd.lockfile import _depth_registry
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchd.storage import StorageBackend

class _StoreChecksMixin:

    # 宿主接口（**仅类型层声明**，运行期不产生任何类属性，故不进入 AST / vars() 扫描面）：
    # 属性与派生路径由 ``store.Store`` 的 ``__init__`` 与骨架 property 提供，
    # ``if TYPE_CHECKING:`` 块内的方法由兄弟段经 ``Store`` 的 MRO 拼装提供。
    orchd_dir: Path
    backend: StorageBackend
    _line_count: int | None

    if TYPE_CHECKING:
        @property
        def ledger_path(self) -> Path: ...
        @property
        def checkpoint_path(self) -> Path: ...
        @property
        def lock_path(self) -> Path: ...
        def _count_ledger_lines(self) -> int: ...
        def _replay_prefix(self, n: int) -> dict[str, TaskState]: ...

    def check_integrity(self) -> list[dict[str, Any]]:
        """校验 ledger 与 checkpoint 一致性（红线 8，R3）。

        「只读」口径（task-counter-cache-reconcile D3 修订）：**事件实体与
        checkpoint 数据只读、不自动修复**；唯一允许的写是计数缓存的接触即
        纠归位（reconcile，不触碰任何事件/checkpoint 内容）——比较基准恒取
        实体口径，缓存漂移不再被误报成篡改。

        检测手改运行时文件（``_ledger.jsonl`` / ``_checkpoint.json``）的篡改：
        1. checkpoint.``ledger_line`` 必须 <= 实际 ledger 行数（ledger 被截断 /
           checkpoint 行号被改大）；
        2. 重放前 ``ledger_line`` 条事件，必须与 checkpoint 的 ``tasks`` 快照一致
           （checkpoint 快照被改 / ledger 早期事件被改）。

        返回告警列表；一致（或 checkpoint 缺失）时返回空列表。仅告警，不阻断
        合法操作（对齐 §3 判据 3 降级路径）。告警用 ``code=E030``，不抛异常。
        """
        # L7：保持调用点解析（补丁点仍为 orchd.ledger._attach_structured_guidance）。
        from orchd.ledger import _attach_structured_guidance
        # L7：保持调用点解析（补丁点仍为 orchd.ledger.resolve_store_dir）。
        from orchd.ledger import resolve_store_dir
        warnings_list: list[dict[str, Any]] = []
        # L-12：先把「损坏行」结构化告警收集到位（read_events 观测到的撕裂 / 非法
        # JSON 行）——无论后续 checkpoint 分支如何提前返回，agent 都能看到
        # 「replay 丢了哪些事件」。仅告警、不阻断、不影响派生结果。
        warnings_list.extend(self._corrupt_line_warnings())
        checkpoint = self.backend.load_checkpoint()
        if checkpoint is None:
            # checkpoint 缺失（未写过快照 / 解析失败）→ 视为未校验到篡改
            return warnings_list

        ledger_line = checkpoint.get("ledger_line")
        if not isinstance(ledger_line, int) or ledger_line < 0:
            warnings_list.append(_attach_structured_guidance({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": (
                    f"checkpoint.ledger_line 非法（{ledger_line!r}），"
                    "运行时文件疑似被篡改（不自动修复）"
                ),
                "path": str(self.checkpoint_path),
            }, self.orchd_dir))
            return warnings_list

        # 归因收窄（task-counter-cache-reconcile D3）：先经接触即纠再比较——
        # 可写通道把漂移持久归位，只读通道至少取到实体真值。纯计数漂移
        # 不再参与「篡改」审判；下方 ledger_line > actual 仅在实体真实缺行
        # 时触发（V2' 实锤形态：事件体完好、旧逻辑拿撒谎缓存报 E030）。
        rec = self.reconcile_counters()
        truth = rec.get("to")
        actual = self._count_ledger_lines() if truth is None else int(truth)
        # task-ledger-archive-compact：journal 感知。中断崩溃的三写残留与"篡改"
        # 不可区分对待——前者重跑即愈，后者须人工核对，故以 distinct 代码分流。
        try:
            from orchd.storage import compact_journal_path

            _journal_present = compact_journal_path(
                resolve_store_dir(self.orchd_dir)).is_file()
        except Exception:
            _journal_present = False
        if ledger_line > actual:
            if _journal_present:
                warnings_list.append(_attach_structured_guidance({
                    "code": "compact_interrupted",
                    "severity": "warning",
                    "message": (
                        f"compact 疑似中断：checkpoint.ledger_line={ledger_line} "
                        f"超过实际逻辑行数 {actual}，且中断标记仍在；非篡改——"
                        "重跑 ledger-compact 即可收敛（幂等），不自动修复"
                    ),
                    "path": str(self.checkpoint_path),
                }, self.orchd_dir))
                return warnings_list
            warnings_list.append(_attach_structured_guidance({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": (
                    f"checkpoint.ledger_line={ledger_line} 超过实际 ledger 行数 "
                    f"{actual}，运行时文件疑似被篡改；建议人工核对（不自动修复）"
                ),
                "path": str(self.checkpoint_path),
            }, self.orchd_dir))
            return warnings_list

        # 兼容层清理：schema 升级后 checkpoint 快照与重放结果不一致属预期
        # 版本落后时跳过快照一致性比较，消除升级后误报 E030
        ckpt_version = checkpoint.get("schema_version")
        if _journal_present:
            # 计数一致但标记残留（成功后删标记前崩溃）：仅提示清理，不阻断。
            warnings_list.append(_attach_structured_guidance({
                "code": "compact_interrupted",
                "severity": "warning",
                "message": (
                    "compact 中断标记残留但计数一致：上次压实已完成，仅标记未删；"
                    "重跑一次 ledger-compact 即可清除（幂等）"
                ),
                "path": str(self.checkpoint_path),
            }, self.orchd_dir))
        if isinstance(ckpt_version, int) and ckpt_version != _CHECKPOINT_SCHEMA_VERSION:
            return warnings_list
        if checkpoint.get("bootstrap_tasks") is not None:
            # 快照基（task-sync-compact-bootstrap-from-state）：tasks 含本地物理
            # 事件无法派生的派生态（compact 清空了转录前缀），前缀比对在此不适用；
            # 强行比对会对每个新设备恒报 E030「疑似被篡改」（红线 8 假阳性）。
            return warnings_list

        try:
            derived = self._replay_prefix(ledger_line)
        except OrchdError:
            # 防御性兜底：replay 异常（如校验故障）时附加 E030 告警。
            # 注：AC4 起中间行损坏已降级跳过，此处不再因 E002 触发。
            warnings_list.append(_attach_structured_guidance({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": "ledger 重放异常，运行时文件疑似被篡改（不自动修复）",
                "path": str(self.ledger_path),
            }, self.orchd_dir))
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

    def _corrupt_line_warnings(self) -> list[dict[str, Any]]:
        """把 read_events 观测到的损坏行转成结构化 E030 条目（L-12）。

        若账本尚未被本 backend 读过（``corrupt_lines`` 未初始化），先读一次以采集
        观测（诊断路径，代价可接受）。

        task-detection-fail-closed-doctor-ledger（INV-4a）：采集失败**不再降级为空
        列表**——空列表语义是「未观测到损坏行」，与「采集本身失败」同形，会让
        :meth:`check_integrity` 在账本不可读时报告「无篡改迹象」（假阴性）。改为产出
        一条带 ``detection_unavailable=True`` 的 E030 告警：走既有 warning 通道
        （不新增错误码），且遵守 :meth:`check_integrity` 的「仅告警、不阻断」契约
        （**不改变退出码**，可见性由条目自身承担）——与 doctor 通道（unavailable 项
        计入 repo_ok=False）强度不同，但两者都不再静默。
        """
        # L7：保持调用点解析（补丁点仍为 orchd.ledger._attach_structured_guidance）。
        from orchd.ledger import _attach_structured_guidance
        try:
            if getattr(self.backend, "corrupt_lines", None) is None:
                self.backend.read_events()
            entries = [
                _attach_structured_guidance(dict(raw), self.orchd_dir)
                for raw in list(self.backend.corrupt_lines or [])  # type: ignore[attr-defined]
            ]
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            return [_attach_structured_guidance({
                "code": ErrorCode.E030.name,
                "severity": "warning",
                "message": (
                    f"账本损坏行检测不可用（{reason}）——结果不可信（可能漏报），"
                    "需人工核对账本完整性（不自动修复）"
                ),
                "path": str(self.ledger_path),
                "detection_unavailable": True,
                "reason": reason,
            }, self.orchd_dir)]
        return entries

    def reconcile_counters(self) -> dict[str, Any]:
        """计数缓存对账唯一入口（task-counter-cache-reconcile D2）。

        委托后端 :meth:`StorageBackend.recalibrate_event_count`：

        - 缓存与实体重算一致 → ``{"drift": False}``；
        - 漂移且可写 → 持久归位（D4 观测键随写累计），并同步归位本实例
          内存层 ``_line_count``（**两层缓存一并治**），返回
          ``{"drift": True, "recalibrated": True, "from": …, "to": …, "history": …}``；
        - 漂移但只读通道（doctor / readonly 后端）→ 遵守只读红线不写回，
          返回 ``recalibrated: False`` 且 ``to`` 携实体真值，供消费方改用作
          比较口径——漂移被命名，不再是「篡改」判据的一部分。

        对账只动计数缓存与观测键，**绝不触碰事件实体**（红线）。
        """
        detail = self.backend.recalibrate_event_count()
        if detail is None:
            return {"drift": False}
        repaired = bool(detail.get("repaired"))
        if repaired and self._line_count is not None:
            self._line_count = detail["to"]
        out: dict[str, Any] = {
            "drift": True,
            "recalibrated": repaired,
            "from": detail.get("from"),
            "to": detail.get("to"),
        }
        if repaired:
            out["history"] = self.backend.counter_reconcile_stats()
        return out

    def counter_reconcile_stats(self) -> dict[str, Any]:
        """计数缓存纠偏累计与末次明细（D4 观测面，纯读）。"""
        return self.backend.counter_reconcile_stats()

    def _current_line_count(self) -> int:
        """返回 ledger 当前行数（H4 缓存 + L-13 锁内校准契约）。

        - 已校准 → O(1) 返回内存计数；写路径在账本锁内 append 后 ``+1`` 保持同步，
          故同一进程内计数始终与自身写动作一致。
        - **未校准 → 首次校准优先在账本锁内**取物理行数快照（L-13）：与并发 append
          互斥，避免「读到落后半拍的行数」被长期缓存后又被增量写入沿用。
        - 锁不可得（并发写持有）→ 退化为锁外计数，并按**显式契约**标注：
          结果为**有界可重试**——ledger 为 append-only、行数单调不减，滞后只影响
          展示与 checkpoint ``ledger_line``（偏小即多读几行增量，replay 结果不变），
          **绝不作为删除 / 覆盖 / 篡改判据**。
        """
        if self._line_count is not None:
            return self._line_count
        held = (
            self.backend._file_lock._fd is not None  # type: ignore[attr-defined]
            or str(self.lock_path.resolve()) in _depth_registry
        )
        if held:
            self._line_count = self._count_ledger_lines()
            return self._line_count
        try:
            # 非阻塞尝试（只读路径不得因并发写而阻塞）
            self.backend._file_lock.acquire(blocking=False)  # type: ignore[attr-defined]
        except OrchdError:
            self._line_count = self._count_ledger_lines()
            return self._line_count
        try:
            self._line_count = self._count_ledger_lines()
        finally:
            try:
                self.backend._file_lock.release()  # type: ignore[attr-defined]
            except (OSError, IOError):
                pass
        return self._line_count

    def _record_soft_violation(
        self, event: dict[str, Any], ts: TaskState
    ) -> None:
        """replay 期序列级不变量软校验（L-3）：只记录，绝不阻断 / 不改派生状态。

        判据比写入期白矩阵更严（白矩阵允许 self-transition，如两条 CLAIMED 依次
        到来都合法；但**序列级**看这是跨设备双认领的迹象）。检测项：

        - ``double_claim``：同一任务在 ``claimed`` 状态下又来一条 CLAIMED
          （中间没有任何终态/审查事件）；
        - ``done_without_active_claim``：非 ``claimed`` 状态下出现 DONE；
        - ``review_without_done``：非 ``done`` / ``in_review`` 状态下出现审查类事件；
        - ``terminal_reentry``：终态（completed / cancelled）后再次出现推进类事件。
        """
        etype = event.get("type", "")
        status = ts.status
        invariant: str | None = None
        reason = ""
        if status in TERMINAL_STATUSES and etype in (
            "CLAIMED", "DONE", "REVIEW_READY", "REVIEW_SUBMITTED",
        ):
            # 终态后推进优先判定（比「缺活跃认领」更精确：终态本身就是最强判据）
            invariant = "terminal_reentry"
            reason = f"任务已 {status} 却再次出现 {etype}（终态后推进）"
        elif etype == "CLAIMED" and status == "claimed":
            invariant = "double_claim"
            reason = (
                "同一任务在 claimed 状态下再次 CLAIMED（中间无终态/审查事件）——"
                "典型成因为跨设备双认领或事件顺序被合并打乱"
            )
        elif etype == "DONE" and status != "claimed":
            invariant = "done_without_active_claim"
            reason = f"任务处于 {status} 状态却出现 DONE（无活跃认领）"
        elif etype in ("REVIEW_READY", "REVIEW_CLAIMED", "REVIEW_SUBMITTED") and \
                status not in ("done", "in_review"):
            invariant = "review_without_done"
            reason = f"任务处于 {status} 状态却出现 {etype}（未经 DONE/进入审查）"
        if invariant is None:
            return
        violations = getattr(self, "_replay_violations", None)
        if violations is None:
            violations = []
            self._replay_violations = violations
        violations.append({
            "code": "replay_violation",
            "invariant": invariant,
            "task_id": event.get("task_id", ""),
            "event_id": event.get("event_id", ""),
            "event_type": etype,
            "observed_status": status,
            "message": reason,
            "hint": (
                "replay 序列级软校验：事件已被照常应用（派生状态与改动前一致），"
                "此处仅上报以暴露跨设备并发的语义冲突；硬性拒绝/隔离需跨设备裁决"
                "通道（另案）。"
            ),
        })
