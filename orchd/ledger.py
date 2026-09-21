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
import threading
import time
import uuid
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError, to_json_response
from orchd.lockfile import ExclusiveFileLock, _depth_registry, _flock_op, read_locked_text

# task-storage-port-adapter-split：端口/适配器物理分层——StorageBackend（端口）迁至
# orchd.storage、FilesystemBackend（文件适配器）迁至 orchd.storage.filesystem；
# 此处重导出保持 `from orchd.ledger import StorageBackend / FilesystemBackend` 兼容。
from orchd.storage import StorageBackend  # noqa: F401
from orchd.storage.filesystem import FilesystemBackend  # noqa: F401

# checkpoint 字段 schema 版本（P2-10 / ROADMAP 1.4.1 引擎性能）：
# update_checkpoint 稳态下用增量 state 写快照（O(tail)）；仅当 checkpoint 的
# schema_version 落后于本常量（新字段引入/升级）才 replay_full() 自愈一次。
# 之后新增 TaskState 字段时递增本常量即可触发一次全量重建（字段漂移自愈）。
# v2（2026-08-27）：review_claimed_session 引入（e7e70a8）时漏 bump，既有
# checkpoint 缺该字段且自愈永不触发 → E030 持续告警；bump 至 2 触发一次自愈。
# v3（2026-08-28，W-2）：新增 review_claimed_at（僵尸审查认领判定）后 bump，
# 触发一次 replay_full 自愈，避免旧 checkpoint 缺该字段自我传播 → E030。
# v4（2026-09-15，停服升级）：新增 review_self_review——自审事实落账到 REVIEW_CLAIMED /
# REVIEW_SUBMITTED 事件的 is_self_review 字段与派生状态，事后可直接回查，不再依赖
# DONE.agent_id == REVIEW_CLAIMED.agent_id 的启发式推导。属 conventions.md「安全边界」
# 第 2 条（事件格式与 _apply_event 语义）改动，按约定人工停服升级、不走自托管任务管线。
# 同样 bump 触发一次 replay_full 自愈，避免旧 checkpoint 缺该字段。
_CHECKPOINT_SCHEMA_VERSION = 4


# ------------------------------------------------------------------
# 通道 C 结构化收敛（task-errexit-channel-c-structured）
# ------------------------------------------------------------------
# 手工 dict 错误（不冒泡到 cli 统一异常处理器的 {code,...} 裸 dict）拿不到
# 按码指引。structured_error 内部统一 to_json_response 语义 +
# attach_error_guidance，是通道 C 的唯一收敛入口（设计稿 §6.2）。
#
# 宿主位置选 ledger：cli / spec / gitops 均可按既有依赖方向引用
# （cli→ledger 已文档化；spec→ledger 与 validate_source 同向；
# gitops→ledger 无环），guide 依赖用函数内惰性导入保持 ledger→errors
# 主干不膨胀。


def structured_error(
    code: str,
    message: str,
    details: Any = None,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    """通道 C 结构化错误：to_json_response + attach_error_guidance 一体化收敛。

    保证两条契约（设计稿 §8.8 / §6.2）：

    - ``error.details`` 恒为 ``list[dict]``——None/str/dict/list/其他一律归一，
      消灭 E032 类 details 为字符串的契约违例；
    - 响应恒带 ``guidance``（recovery/command/exit_type 等），按静态表挂接，
      details 含 ``hint`` 时场景化覆盖通用 recovery（对齐异常通道行为）。

    Args:
        code: 错误码名（如 ``"E032"``）；未知码回退 E007（best-effort）。
        message: 错误消息。
        details: 任意形态，归一为 ``list[dict]``。
        base_dir: guidance 路径解析用 ``.orchd`` 根（可 None）。

    Returns:
        ``{"error": {code, message, details, severity, suggest_report},
           "guidance": {...}}``。
    """
    if details is None:
        norm_details: list[dict[str, Any]] = []
    elif isinstance(details, str):
        norm_details = [{"message": details}]
    elif isinstance(details, dict):
        norm_details = [details]
    elif isinstance(details, list):
        norm_details = [
            {"message": d} if isinstance(d, str)
            else (d if isinstance(d, dict) else {"value": str(d)})
            for d in details
        ]
    else:
        norm_details = [{"value": str(details)}]
    try:
        code_enum = ErrorCode[code] if isinstance(code, str) else code
    except KeyError:
        code_enum = ErrorCode.E007
    err = OrchdError(code_enum, message, norm_details)
    resp = to_json_response(err)
    try:
        from orchd.guide import attach_error_guidance

        resp = attach_error_guidance(resp, code_enum.name, base_dir)
    except Exception:
        # guidance 为加法式附加：挂接失败不得击穿错误主体输出（通道 C best-effort）
        pass
    return resp


def _attach_structured_guidance(
    entry: dict[str, Any], base_dir: Path | None = None
) -> dict[str, Any]:
    """给 E030 完整性/降级告警条目附加结构化 ``details`` + ``guidance``。

    保留条目原有键（code/severity/message/path/guard/...）不改变既有消费方
    断言面，仅加法式附加；任何异常静默降级（告警链不得因指引挂接失败）。

    注意：details 必须用条目的**浅拷贝**——直接传条目自身会让归一后的
    ``details[0]`` 与条目互指，回填 ``entry["details"]`` 后形成循环引用，
    JSON 序列化即崩（实测 E999 Circular reference）。
    """
    try:
        resp = structured_error(
            entry.get("code", ErrorCode.E030.name),
            entry.get("message", ""),
            [dict(entry)],
            base_dir,
        )
        entry["details"] = resp.get("error", {}).get("details", [])
        guidance = resp.get("guidance")
        if guidance:
            entry["guidance"] = guidance
    except Exception:
        pass
    return entry


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
        review_claimed_at:  审查认领发生的 ISO 时间戳（源自 REVIEW_CLAIMED 事件的
                            timestamp）。用于僵尸审查认领判定（W-2）：in_review 且
                            认领超时未见提交 → 可接管。未认领/已提交时为 None。
        review_self_review: 本次审查是否为自审（实现者与审查者同一身份），源自
                            REVIEW_CLAIMED / REVIEW_SUBMITTED 事件的 ``is_self_review``
                            字段（v4，2026-09-15 停服升级）。把「自审」事实落账到事件与
                            派生状态后，事后审计直接读账本即可判定，不再依赖
                            ``DONE.agent_id == REVIEW_CLAIMED.agent_id`` 的启发式推导。
                            非自审时为 False（快照省略）。
        merge_warning:      代码审查通过后 git merge 未执行（环境异常/best-effort 降级），
                            标记完成但 merge 未落地，audit-merge 需告警。仅 completed 有值。
    """

    status: str = "pending"
    claimed_by: str | None = None
    claimed_session: str | None = None
    attempt_count: int = 0
    review_phase: str | None = None
    review_claimed_by: str | None = None
    review_claimed_session: str | None = None
    review_claimed_at: str | None = None
    review_self_review: bool = False
    merge_warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """将任务状态序列化为字典，用于写入 checkpoint JSON。

        为保持 checkpoint 紧凑，值为 ``None`` 的可选字段（``claimed_by``、
        ``review_phase``、``review_claimed_by``、``merge_warning``）会被省略；
        ``review_self_review``（bool）仅在为 ``True`` 时写入。
        始终包含 ``status`` 和 ``attempt_count``。
        """
        d: dict[str, Any] = {"status": self.status, "attempt_count": self.attempt_count}
        if self.claimed_by:
            d["claimed_by"] = self.claimed_by
        if self.claimed_session:
            d["claimed_session"] = self.claimed_session
        if self.review_phase:
            d["review_phase"] = self.review_phase
        if self.review_claimed_by:
            d["review_claimed_by"] = self.review_claimed_by
        if self.review_claimed_session:
            d["review_claimed_session"] = self.review_claimed_session
        if self.review_claimed_at:
            d["review_claimed_at"] = self.review_claimed_at
        if self.review_self_review:
            d["review_self_review"] = True
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
            claimed_session=d.get("claimed_session"),
            attempt_count=d.get("attempt_count", 0),
            review_phase=d.get("review_phase"),
            review_claimed_by=d.get("review_claimed_by"),
            review_claimed_session=d.get("review_claimed_session"),
            review_claimed_at=d.get("review_claimed_at"),
            review_self_review=bool(d.get("review_self_review", False)),
            merge_warning=d.get("merge_warning"),
        )


# 僵尸审查认领（W-2）：审查认领超过该时长且未见提交，即由 request / status /
# doctor 浮出、可供接管。默认 10 分钟（复用本次实测校准值）。
_REVIEW_STALE_DEFAULT_S = 300


def review_stale_timeout_s() -> float:
    """返回审查认领超时秒数；环境变量 ``ORCHD_REVIEW_STALE_SECS`` 可覆盖（测试用）。"""
    env = os.environ.get("ORCHD_REVIEW_STALE_SECS")
    if env is not None:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    return _REVIEW_STALE_DEFAULT_S


def review_claim_age_s(claimed_at: str | None, now: str | None = None) -> float | None:
    """审查认领距今秒数。时间戳缺失/不可解析 → None（不判 stale，避免误伤）。"""
    if not claimed_at:
        return None
    try:
        t = datetime.fromisoformat(claimed_at)
        base = datetime.fromisoformat(now) if now else datetime.now(t.tzinfo)
        return (base - t).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def stale_review_claims(
    state: dict[str, TaskState],
    timeout_s: float | None = None,
    now: str | None = None,
) -> dict[str, dict[str, Any]]:
    """从派生状态找出「僵尸审查认领」：in_review 且有认领、认领超时未见提交。

    仅依据派生状态（status / review_claimed_by / review_claimed_at）判定，不加
    ledger 扫描、不引入独立标志位（与 replay 同源，判定即派生）。``timeout_s`` /
    ``now`` 缺省取常量/环境覆盖与当前时刻，便于测试注入。

    返回 ``{task_id: {claimed_by, claimed_session, review_phase, age_s, timeout_s}}``。
    """
    tmo = review_stale_timeout_s() if timeout_s is None else timeout_s
    stale: dict[str, dict[str, Any]] = {}
    for tid, ts in state.items():
        if not (ts.status == "in_review" and ts.review_claimed_by and ts.review_claimed_at):
            continue
        age = review_claim_age_s(ts.review_claimed_at, now)
        if age is not None and age >= tmo:
            stale[tid] = {
                "claimed_by": ts.review_claimed_by,
                "claimed_session": ts.review_claimed_session,
                "review_phase": ts.review_phase or "spec",
                "age_s": round(age),
                "timeout_s": tmo,
            }
    return stale


def generate_event_id() -> str:
    """生成事件 ID：``evt-{uuid4-hex-16}``（64 bit 熵，L-9）。

    历史形态为 ``evt-{8hex}``（仅 32 bit）：当前账本规模下生日碰撞概率约
    0.12%、万级约 1%，碰撞事件会被 ``orchd sync`` 的 event_id 去重**静默丢弃**，
    或被 RETRACT 误撤他人事件。加宽到 64 bit 后同规模碰撞概率可忽略。

    兼容性：旧 8hex 事件原样读取，去重与引用仍按字符串相等（不重写历史）；
    新旧混存安全，但**跨设备需同时升级**才能实质消除碰撞。
    """
    return f"evt-{uuid.uuid4().hex[:16]}"


# 原子替换的 Windows 有界重试参数（L-8）
_ATOMIC_REPLACE_RETRIES = 5
_ATOMIC_REPLACE_BACKOFF_S = 0.05


def _is_transient_replace_error(exc: OSError) -> bool:
    """是否属「目标文件被短暂占用」类可重试错误（Windows WinError 5 / 32）。"""
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in (5, 32)


def _fsync_dir(path: Path) -> None:
    """best-effort fsync 目录项（Windows 下不可用 → 静默忽略 OSError）。"""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _atomic_replace(
    tmp_path: Path,
    target_path: Path,
    *,
    retries: int = _ATOMIC_REPLACE_RETRIES,
    backoff_s: float = _ATOMIC_REPLACE_BACKOFF_S,
) -> None:
    """原子替换 ``target_path`` ← ``tmp_path``，Windows 句柄争用有界重试（L-8）。

    Windows 下目标文件被短暂持有句柄（并行的 orchd 只读命令正在读、杀软扫描、
    索引器）时 ``os.replace`` 抛 ``PermissionError``（WinError 5/32）。旧实现
    无重试：一次争用即让「事件已 append、但 checkpoint 未更新」的中间态以
    E999 逃逸（命令失败、状态错配）。此处退避重试有限次，耗尽后抛结构化
    E007（invalid_state，通道 A 已登记且为错误级；同模块既有 git_sync_failed
    先例），**不无限重试、不静默吞掉**。

    Args:
        tmp_path: 已写完并 fsync 的临时文件。
        target_path: 目标运行时文件（ledger / checkpoint）。
        retries: 最大尝试次数（>=1）。
        backoff_s: 线性退避基数（秒）。

    Raises:
        OrchdError: E007，重试耗尽仍未成功（含最后异常与占用提示）。
    """
    attempts = max(1, retries)
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(str(tmp_path), str(target_path))
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc):
                raise
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(backoff_s * (attempt + 1))
    raise OrchdError(
        ErrorCode.E007,
        "atomic_replace_failed: 目标运行时文件被占用，重试耗尽",
        [
            {
                "tmp": str(tmp_path),
                "target": str(target_path),
                "retries": attempts,
                "error": f"{type(last_exc).__name__}: {last_exc}",
                "hint": "关闭持有该文件的进程（编辑器 / 杀软扫描 / 并行的 orchd 只读命令）后重试",
            }
        ],
    )


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
    容错语义：末行损坏跳过、中间行损坏降级跳过 + E030 warning），保证
    「读到的辅助信息」与「派生状态」一致。
    """

    last_done: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_comments: dict[str, list[str]] = field(default_factory=dict)
    review_baselines: dict[tuple[str, str], str] = field(default_factory=dict)


# 宿主注入的每对话唯一会话标识环境变量（session-id-fingerprint）。
# 由宿主在每次对话启动时注入，据此确定性派生 12 位 hex 指纹：
# 同一对话内所有命令返回同一指纹，切换对话（新值）即换指纹。
_ORCHD_SESSION_ID_ENV = "ORCHD_SESSION_ID"

# 会话身份完全由宿主注入的 ORCHD_SESSION_ID 派生，引擎不自持身份、不借用
# 任何历史身份。据此彻底废除 .orchd/.agent_id 文件（读取与写入均删除）：
# 未注入 ORCHD_SESSION_ID 时身份为空（None），引擎不生成、不落盘、不复用。


def resolve_agent_id(orchd_dir: Path | None = None) -> str:
    """解析当前 agent 身份，返回 12 位 hex 指纹（会话级，session-id-fingerprint）。

    会话身份与宿主注入的 ``ORCHD_SESSION_ID`` 一一对应：
    - 有值 → 确定性派生 ``sha256("orchd-session:" + SESSION_ID)[:12]``：
      同一对话内所有命令返回同一指纹，切换对话（注入新值）即换指纹，
      实现「一个对话一个永久指纹」。派生函数恒为 hex，天然满足 12 位指纹
      形态判定（E021 豁免自动生效）。
    - 无值（宿主未注入）→ 返回空字符串：引擎不生成、不借用、不落盘任何身份，
      杜绝把工作区历史身份误当成当前会话。

    用途（session-id-fingerprint）：
    - 依据宿主注入的每对话唯一码锚定身份，实现者对话与审查者对话身份不同，
      切换到新对话可正常领取 review（不被 E016 自审阻断）。
    - 各 agent 宿主（TRAE / codex / opencode / workbuddy 等）在启动 orchd 前
      统一把各自会话唯一码注入 ``ORCHD_SESSION_ID``，本函数只认该标准化变量。

    Note:
        单一事实源为 :func:`resolve_session_identity`，本函数取其 ``fingerprint``。
    """
    return resolve_session_identity(orchd_dir)["fingerprint"]


def resolve_session_identity(orchd_dir: Path | None = None) -> dict[str, str]:
    """解析当前会话的引擎级身份，返回 ``{"session_id": ..., "fingerprint": ...}``。

    Session Identity Layer：
    - ``session_id`` 为会话级身份主键（64 位 SHA-256 十六进制），同一
      ``ORCHD_SESSION_ID`` 会话内恒定，不同会话不同；
    - ``fingerprint`` 为兼容旧的 12 位 hex 指纹（取 ``session_id`` 前 12 位）；
    - 未注入 ``ORCHD_SESSION_ID`` 时返回 ``{"session_id": "", "fingerprint": ""}``，
      引擎不生成、不借用、不落盘任何身份。

    与 :func:`resolve_agent_id` 的区别：后者只返回指纹；本函数同时返回
    session_id，供事件账本写入和 session 级并发判定使用。
    """
    sid = os.environ.get(_ORCHD_SESSION_ID_ENV) or None
    if not sid:
        return {"session_id": "", "fingerprint": ""}
    import hashlib

    full = hashlib.sha256(("orchd-session:" + sid).encode("utf-8")).hexdigest()
    return {"session_id": full, "fingerprint": full[:12]}


def is_fingerprint_agent_id(agent_id: str) -> bool:
    """判断 agent_id 是否为指纹形态身份（12 位 hex，task-fp-identity-engine）。

    :func:`resolve_agent_id` 派生的稳定身份指纹为 12 位 SHA-256 短哈希（恒为 hex）。
    指纹身份由引擎自动识别、无法预写入静态 reviewers 名单，故在名单门禁
    （claim E007 / request review_priority / request_reviewer）与 E021 身份
    warning 中豁免——审查独立性仍由 E016 防自审 + E011 忙度兜底。

    **单一事实源（task-fp-identity-single-source，2026-08-22）**：onboard.py /
    review.py / cli.py 统一由此导入，消除三处副本的判定逻辑同步漂移风险。

    Args:
        agent_id: 待判定身份字符串。

    Returns:
        True：恰为 12 位 hex（指纹形态）；False：空 / None / 长度不符 / 非 hex。
    """
    if not agent_id or not isinstance(agent_id, str) or len(agent_id) != 12:
        return False
    try:
        int(agent_id, 16)
        return True
    except ValueError:
        return False


def _find_orchd_dir() -> Path:
    """从当前工作目录向上定位 .orchd 目录（发布态自包含布局）。

    仓库边界（task-canonical-root-boundary-guard，AC1/AC2）：与 CLI 侧
    ``orchd.cli._util._find_orchd_dir`` **同源**委托
    :func:`orchd.worktree.find_orchd_dir_within_git_boundary`——不越过起点所属
    的最近 git 仓库根：内层独立 git 仓库无 ``.orchd`` 时返回 ``cwd/.orchd``
    （flat/自身），绝不爬到宿主真实 ``.orchd``；非 git 目录维持逐级向上（零回归）。
    """
    from orchd.worktree import find_orchd_dir_within_git_boundary

    return find_orchd_dir_within_git_boundary(Path.cwd())


def resolve_store_dir(orchd_dir: Path) -> Path:
    """解析账本根目录（task-orchd-home-redirect，roadmap:snapshotstore-m-p0）。

    账本（ledger / checkpoint / lock / mod-*）由环境变量 ``ORCHD_HOME`` 重定向到
    外部目录；未设置时回退到传入的 ``orchd_dir``（默认 ``<cwd 向上找的 .orchd/>``）。

    1.4 共享账本默认（task-14-worktree-lifecycle，R3）：未设 ``ORCHD_HOME`` 时，
    若主工作树存在 **container** 布局标记（``.orchd/.layout.json`` layout=container）
    → 默认布局级 runtime 根（``<容器>/.orchd-runtime/``，多会话共享账本）；
    flat（含标记与未迁移项目）→ 维持 ``orchd_dir`` 现状零回归
    （flat 单会话账本仍在主工作树 .orchd/，共享账本留待 container 形态落地）。

    注意：``orchd_dir`` 语义是「master 目录」（含 ``_master.json`` + ``shared/``，
    走 git，不入 backend）；返回的账本根仅用于 FilesystemBackend 派生账本文件路径。
    """
    home = os.environ.get("ORCHD_HOME")
    if home:
        return Path(home)
    # task-14-worktree-lifecycle：仅 container 布局 → 布局级 runtime 根（共享账本默认）
    try:
        from orchd.worktree import read_layout

        marker = read_layout(orchd_dir)
        if marker is not None and marker.get("layout") == "container":
            main_wt = Path(marker["main_worktree"])
            return main_wt.parent / ".orchd-runtime"
    except Exception:
        pass
    return orchd_dir


def resolve_review_mode(orchd_dir: Path) -> str:
    """解析项目审查模式（review-unify-r2：unified / two_phase）。

    读 ``.orchd/_master.json`` 顶层 ``project.review_mode``：
    - ``"unified"``  → 单阶段审查：一次 APPROVED 即 merge；
    - ``"two_phase"``/缺失/非法值 → 两阶段审查（spec → code），保持旧行为。

    缺省 two_phase 保证观察期兼容：旧项目 / 测试 / 老事件均不受影响，
    显式配置 ``project.review_mode: "unified"`` 才启用单阶段链路。
    best-effort：master 缺失/解析失败返回 ``"two_phase"``（不抛异常）。

    master 路径经 ``orchd.worktree.resolve_master_path_from_dir`` 单一真源解析
    （本地优先 → canonical 主工作树回退）：container 容器根视角读到 canonical
    权威配置，不再裸读本地副本静默回落。函数内惰性导入（ledger 不在顶层依赖
    worktree，避免循环）。
    """
    try:
        from orchd.worktree import resolve_master_path_from_dir

        master_path = resolve_master_path_from_dir(orchd_dir)
        if not master_path.exists():
            return "two_phase"
        import json as _json

        master = _json.loads(master_path.read_text(encoding="utf-8"))
        mode = (master.get("project") or {}).get("review_mode")
        if mode == "unified":
            return "unified"
        return "two_phase"
    except (OSError, ValueError):
        return "two_phase"


# ------------------------------------------------------------------
# Session runtime（task-session-cli-lifecycle，Session Identity Layer）
# ------------------------------------------------------------------
# 每个 session 由引擎显式开启：session start 生成唯一 session_token +
# 派生 session_id/fingerprint，并写入共享账本根下的 sessions/<id>.json；
# 后续命令通过 ORCHD_SESSION_ID 指向该 session。它把“会话边界”从宿主
# 的隐式环境常量升级为引擎持有的运行时实体，避免多个对话共享同一指纹。

_SESSION_RUNTIME_DIRNAME = "sessions"
_SESSION_RUNTIME_ACTIVE = True


def _derive_session_identity_from_token(sid: str) -> dict[str, str]:
    """由会话 token 确定性派生 session_id/fingerprint（与 resolve_session_identity 同算法）。"""
    import hashlib

    full = hashlib.sha256(("orchd-session:" + sid).encode("utf-8")).hexdigest()
    return {"session_id": full, "fingerprint": full[:12]}


def session_runtime_dir(orchd_dir: Path) -> Path:
    """返回 session runtime 目录（共享账本根下，container/flat 兼容）。"""
    return resolve_store_dir(orchd_dir) / _SESSION_RUNTIME_DIRNAME


def _session_runtime_path(orchd_dir: Path, session_id: str) -> Path:
    return session_runtime_dir(orchd_dir) / f"{session_id}.json"


# ------------------------------------------------------------------
# Session TTL 与惰性过期（task-audit-session-ttl-lazy-expiry）
# ------------------------------------------------------------------
# 根因：session 依赖显式 end，而宿主会话中断（关对话 / IDE 崩 / agent 超时 /
# context 满）时 session end 永不被调用，orchd 每次调用都是独立短进程、无法
# 感知宿主存活 → 僵尸 runtime 文件累积（实测约 9 个/天）。
#
# 策略：不追求「删除」（清理是被动的，得有人跑命令才触发），而是读时按
# ``last_seen + TTL`` 判定过期、**判定即生效**——过期文件即使仍在磁盘：
#   - 不参与身份解析：session current / end → E033（reason=session_expired），
#     附重新 start 指引（对齐 E033 现有语义）；
#   - 不再计入活跃：watchdog stale_sessions → reason=session_expired
#     （见 report.py，供接管流程发现）。


_SESSION_TTL_MIN_ENV = "ORCHD_SESSION_TTL_MIN"
_SESSION_TTL_DEFAULT_MIN = 24 * 60  # 默认 24h


def session_ttl_minutes() -> int:
    """返回 session TTL（分钟）：``ORCHD_SESSION_TTL_MIN`` 覆盖，默认 24h（1440）。

    非法值（非整数 / <= 0 / 空串）静默回退默认——环境差异不得击穿身份路径。

    Returns:
        当前生效的 TTL 分钟数（正整数）。
    """
    raw = os.environ.get(_SESSION_TTL_MIN_ENV)
    if raw is None or not raw.strip():
        return _SESSION_TTL_DEFAULT_MIN
    try:
        val = int(raw.strip())
    except ValueError:
        return _SESSION_TTL_DEFAULT_MIN
    return val if val > 0 else _SESSION_TTL_DEFAULT_MIN


def _parse_session_ts(value: Any) -> datetime | None:
    """解析 ISO-8601 时间戳为 aware datetime；缺失 / 非法返回 None（naive 视为 UTC）。"""
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def parse_event_time(value: Any) -> datetime:
    """把事件 ``timestamp`` 解析为 aware 绝对时刻（L-2 归并排序第一元）。

    兼容两类形态（不重写历史事件）：

    - 旧事件：本地时区 + 秒精度（``2026-09-01T10:00:00+08:00``）；
    - 新事件：UTC + 微秒（``2026-09-15T05:40:12.123456+00:00``）。

    无法解析（缺失 / 非字符串 / 格式非法）→ 返回 ``datetime.min``（UTC aware），
    使其稳定地排在最前且不抛异常（归并是 best-effort 路径，一条脏事件不应中断
    整个合并）。naive 时间戳（无 offset 的历史脏数据）按 UTC 解释，避免依赖
    本机时区导致两台设备得到不同的序。
    """
    parsed = _parse_session_ts(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed


def event_sort_key(ev: dict[str, Any]) -> tuple[datetime, str, str]:
    """跨设备归并排序键（L-2，用户裁决 A）：``(绝对时刻, 写入方标识, event_id)``。

    第二元取 ``session_id``（缺失回退 ``agent_id``）：事件格式内没有设备 id，
    以事件里真实存在、稳定、跨设备一致的**写入方标识**作平局键（不新增事件字段，
    无 §9.1 事件格式变更；口径差异已在任务变更描述 / 设计文档 / 审查意见留痕）。

    第三元 ``event_id`` **仅对亚秒精度事件启用**：秒精度旧事件第三元留空，依赖
    Python 稳定排序保留其输入顺序（= 该写入方的追加顺序，各设备一致）。依据（实测）：
    当前账本 3289 事件全为秒精度，757 组共享 ``(timestamp, writer)``（覆盖 1595
    事件，48%），其中 382 组「按 event_id 排序会打乱追加序」（样例：
    ``DONE evt-6e320a0a`` → ``REVIEW_READY evt-13cfb06f`` 会被倒置），无条件启用
    第三元将复现 2026-09-10 已修的同秒乱序实锤。新事件因 ``now_iso`` 的进程内严格
    单调保证不会同秒并列，第三元仅作跨写入方同瞬间的确定性兜底。

    Returns:
        ``(aware 绝对时刻, 写入方标识, event_id 或 "")``。
    """
    raw = ev.get("timestamp")
    raw_str = raw if isinstance(raw, str) else ""
    fine_precision = "." in raw_str
    return (
        parse_event_time(raw),
        str(ev.get("session_id") or ev.get("agent_id") or ""),
        str(ev.get("event_id") or "") if fine_precision else "",
    )


def is_session_expired(data: dict[str, Any], now: datetime | None = None) -> bool:
    """TTL 判定：``last_seen + TTL <= now`` → 过期（惰性过期，判定即生效）。

    纯只读计算，不触碰磁盘、不依赖任何清理动作：

    - ``last_seen`` 缺失 / 非法 → 判**未过期**（兼容降级：旧式 runtime 文件
      与测试夹具可能缺该字段；缺字段不构成过期依据，僵尸判定宁可保守，
      与既有「无 last_seen 的 active runtime 不算僵死」行为零回归）；
    - 否则按 :func:`session_ttl_minutes`（``ORCHD_SESSION_TTL_MIN`` 可覆盖）比较。

    Args:
        data: session runtime JSON 内容（含 ``last_seen``）。
        now: 判定基准时刻；缺省取当前 UTC 时间。

    Returns:
        True：会话已过期；False：未过期或无法判定。
    """
    last_seen = _parse_session_ts(data.get("last_seen"))
    if last_seen is None:
        return False
    moment = now or datetime.now(timezone.utc)
    return last_seen + timedelta(minutes=session_ttl_minutes()) <= moment


def _touch_session_last_seen(orchd_dir: Path) -> None:
    """刷新当前会话 runtime 的 ``last_seen``（best-effort，任何异常静默降级）。

    写命令路径事件追加成功后调用（另见 :func:`session_current` 的活跃刷新）：
    仅当 runtime 文件存在且 active 时写入新时间戳。TTL 判定依赖该信号，
    但活性刷新自身不得阻塞写路径——失败只影响判活精度，不影响账本正确性。
    """
    try:
        identity = resolve_session_identity(orchd_dir)
        if not identity["session_id"]:
            return
        path = _session_runtime_path(Path(orchd_dir), identity["session_id"])
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("active"):
            return
        data["last_seen"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        return


def session_start(
    orchd_dir: Path,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """开启一个新的会话，返回 engine 生成的 session_id/fingerprint/token。

    每次调用都会生成全新 ``session_token``（UUID），据此确定性派生
    ``session_id`` 与兼容指纹。写入：
    ``<runtime>/sessions/<session_id>.json``。

    调用方（宿主接入层）应把返回的 ``session_token`` 注入
    ``ORCHD_SESSION_ID`` 环境变量，使本会话后续命令解析到同一身份。
    """
    orchd_dir = Path(orchd_dir)
    token = uuid.uuid4().hex
    identity = _derive_session_identity_from_token(token)
    now = datetime.now(timezone.utc).isoformat()
    data: dict[str, Any] = {
        "session_id": identity["session_id"],
        "fingerprint": identity["fingerprint"],
        "session_token": token,
        "agent_name": agent_name or "",
        "created_at": now,
        "last_seen": now,
        "active": _SESSION_RUNTIME_ACTIVE,
    }
    path = _session_runtime_path(orchd_dir, identity["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        **data,
        "path": str(path),
        "started": True,
        "inject_action": {
            "description": "将 session_token 注入 ORCHD_SESSION_ID 环境变量，后续命令即识别为同一会话身份",
            "powershell": f'$env:ORCHD_SESSION_ID="{token}"',
            "bash": f'export ORCHD_SESSION_ID="{token}"',
            "env_var": "ORCHD_SESSION_ID",
            "token": token,
        },
    }


def session_current(orchd_dir: Path) -> dict[str, Any]:
    """返回当前会话运行时信息；未开启（无 ORCHD_SESSION_ID/无 runtime 文件）→ E033。

    判定：取 ``resolve_session_identity()`` 的 session_id，再在 runtime 目录中
    定位同名 JSON。若 runtime 文件缺失或已 inactive，提示重新 session start。

    TTL 惰性过期（task-audit-session-ttl-lazy-expiry）：runtime active 但
    ``last_seen`` 超过 TTL（默认 24h，``ORCHD_SESSION_TTL_MIN`` 覆盖）→
    E033（reason=session_expired）附重新 start 指引；判定即生效、不清理文件。
    活跃会话每次查询刷新 ``last_seen``（活性信号）。
    """
    orchd_dir = Path(orchd_dir)
    identity = resolve_session_identity(orchd_dir)
    if not identity["session_id"]:
        raise OrchdError(
            ErrorCode.E033,
            "session_identity_missing: 未开启 orchd session，无法识别当前会话身份",
            [{
                "hint": (
                    "请先运行 'orchd session start' 并在后续命令中将返回的 "
                    "session_token 注入 ORCHD_SESSION_ID；"
                    "若已有活跃 session（session current 可查），可直接复用其 "
                    "session_token 注入 ORCHD_SESSION_ID，无需重复 start"
                ),
            }],
        )
    path = _session_runtime_path(orchd_dir, identity["session_id"])
    if not path.exists():
        # 兼容：runtime 文件未被 session start 写入（如旧式指纹会话）
        raise OrchdError(
            ErrorCode.E033,
            "session_not_found: 当前指纹没有对应的 session runtime 文件",
            [{
                "session_id": identity["session_id"],
                "hint": "请先运行 'orchd session start' 开启会话，并将 session_token 注入 ORCHD_SESSION_ID；若已有活跃 session 可复用其 session_token，无需重复 start",
            }],
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("active"):
        raise OrchdError(
            ErrorCode.E033,
            "session_inactive: 当前 session 已结束，请重新 session start",
            [{"session_id": data.get("session_id"), "path": str(path)}],
        )
    if is_session_expired(data):
        raise OrchdError(
            ErrorCode.E033,
            "session_expired: 当前 session 已超过 TTL 未活动，请重新 session start",
            [{
                "session_id": data.get("session_id"),
                "reason": "session_expired",
                "ttl_minutes": session_ttl_minutes(),
                "last_seen": data.get("last_seen"),
                "hint": (
                    "会话已按 TTL 惰性过期（判定即生效，无需清理）：重新运行 "
                    "'orchd session start' 开启新会话，并将返回的 session_token "
                    "注入 ORCHD_SESSION_ID"
                ),
            }],
        )
    now = datetime.now(timezone.utc).isoformat()
    data["last_seen"] = now
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**data, "path": str(path), "current": True}


def session_end(
    orchd_dir: Path,
    *,
    force_bypass: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """结束当前会话：标记 runtime 文件 inactive（best-effort，不删除）。

    红线 #5 硬化（task-audit-session-end-clean-gate）：CLI 层在调用前已校验
    工作区无已跟踪改动，脏且未 --force 时会被拒绝，不会到达本函数。当
    ``--force`` 放行时，``force_bypass`` 携带放行原因与待提交文件清单，
    原样写入 runtime 文件，保证审计可查。
    """
    orchd_dir = Path(orchd_dir)
    identity = resolve_session_identity(orchd_dir)
    if not identity["session_id"]:
        raise OrchdError(
            ErrorCode.E033,
            "session_identity_missing: 未开启 orchd session，无法结束会话",
            [{"hint": "无需结束：当前没有可识别的会话身份"}],
        )
    path = _session_runtime_path(orchd_dir, identity["session_id"])
    if not path.exists():
        raise OrchdError(
            ErrorCode.E033,
            "session_not_found: 当前指纹没有对应的 session runtime 文件",
            [{"session_id": identity["session_id"], "hint": "先 session start 再 session end"}],
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("active") and is_session_expired(data):
        # TTL 惰性过期：会话早已无人活动，无需「结束」一个已过期会话——
        # 与 session_current 同样报 E033（reason=session_expired），指引重新 start。
        raise OrchdError(
            ErrorCode.E033,
            "session_expired: 当前 session 已超过 TTL 未活动，无需结束，请重新 session start",
            [{
                "session_id": data.get("session_id"),
                "reason": "session_expired",
                "ttl_minutes": session_ttl_minutes(),
                "last_seen": data.get("last_seen"),
                "hint": (
                    "会话已按 TTL 惰性过期（判定即生效，无需清理）：重新运行 "
                    "'orchd session start' 开启新会话，并将返回的 session_token "
                    "注入 ORCHD_SESSION_ID"
                ),
            }],
        )
    data["active"] = False
    data["ended_at"] = datetime.now(timezone.utc).isoformat()
    if force_bypass:
        data["force_bypass"] = {
            **force_bypass,
            "at": data["ended_at"],
        }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**data, "path": str(path), "ended": True}


_WORKSPACE_DOCS = ("IDEAS.md", "ROADMAP.md", "SKILL.md")


def resolve_workspace_root(project_root: Path) -> Path:
    """解析工作区文档根目录（IDEAS.md / ROADMAP.md / SKILL.md 所在目录）。

    task-12-engine-path-abstraction 双态兼容：
    - 根布局（开发态）：工作区文档在项目根 → 返回 ``project_root``。
    - 发布态布局（自包含 ``.orchd/``）：工作区文档归置 ``.orchd/`` → 返回
      ``project_root / ".orchd"``。

    task-canonical-workspace-docs（2026-08-25）canonical 化：入口先经
    ``resolve_canonical_project_root`` 解析到 canonical 主工作树根——
    container 布局返回 ``<容器>/main/``（布局标记权威），flat 布局返回本地。
    再按上述布局规则定位文档根：intake/ideas/amend 在任务 worktree 内调用时
    仍统一从主工作树读 IDEAS/ROADMAP/SKILL，避免 worktree 本地 ``.orchd/``
    拷贝（引擎传播的 SKILL.md 等）过期导致摄入/引导不一致。

    仓库边界（task-canonical-root-boundary-guard，AC2）：本函数不自行扫祖先，
    一律经 ``resolve_canonical_project_root`` 归位，故同源遵守「不得越过起点
    所属最近 git 仓库根」——内层独立 git 仓库不会被误归到宿主工作区文档根。

    判定：``.orchd/`` 下已存在任一工作区文档 → 发布态；否则若项目根存在 → 开发态；
    两者都无 → 默认返回 ``.orchd/``（发布态默认，AC3）。

    调用方：``validate_source``（spec.py）、``archive_resolved_ideas``（ideas.py）、
    ``amend``/``ideas_archive``（cli.py）——统一经本 helper 解析 IDEAS/ROADMAP/SKILL 路径。
    """
    project_root = Path(project_root)
    # task-canonical-workspace-docs（2026-08-25）：统一共享读入口，container 布局
    # 解析到 canonical 主工作树根（flat 返回本地），worktree 本地副本不参与文档定位。
    from orchd.worktree import resolve_canonical_project_root

    project_root = resolve_canonical_project_root(project_root)
    project_root = Path(project_root)
    orchd_dir = project_root / ".orchd"
    if any((orchd_dir / name).exists() for name in _WORKSPACE_DOCS):
        return orchd_dir
    if any((project_root / name).exists() for name in _WORKSPACE_DOCS):
        return project_root
    return orchd_dir


_ROADMAP_DOC = "ROADMAP.md"


def resolve_roadmap_path(project_root: Path) -> Path:
    """定位 ROADMAP.md——**唯一源 = 宿主项目根**，与布局无关，无回退候选。

    task-roadmap-single-source（用户裁定 2026-09-17）：ROADMAP 是**宿主资产**不是
    引擎产物，唯一源是宿主项目根 ``ROADMAP.md``（flat 布局即仓库根，container 布局
    为 canonical 主工作树 ``<容器>/main/``）；``.orchd/ROADMAP.md`` 不应存在，本函数
    **不读它**（旧布局残留时仅 stderr 留痕提示归根，见 :func:`_log_legacy_roadmap`）。

    演进：task-roadmap-root-resolution 曾实现为「宿主根优先 + .orchd/ 回退」，回退
    分支等于给引擎产物留后门（同一份文档有两个可能位置），与裁定冲突，故此处去掉。

    本 helper 仍是 ROADMAP 定位的**唯一真源**：roadmap-land / intake_commit /
    E025 溯源 / roadmap_landing_warnings / 任务 worktree 同步统一复用。

    与 :func:`resolve_workspace_root` 的分工：后者按 IDEAS / SKILL / ROADMAP 三份文档
    的捆绑判定返回**工作区文档根**（IDEAS / SKILL / 归档继续复用，语义零回归）；
    ROADMAP 的路径一律走本函数，不再由工作区文档根拼出。

    Args:
        project_root: 起点目录（任务 worktree 亦可，内部先 canonical 化）。

    Returns:
        宿主项目根的 ROADMAP.md 路径；**不保证存在**——消费点按需判 ``exists()``。
    """
    from orchd.worktree import resolve_canonical_project_root

    root = Path(resolve_canonical_project_root(Path(project_root)))
    root_doc = root / _ROADMAP_DOC
    legacy = root / ".orchd" / _ROADMAP_DOC
    # 旧布局残留（.orchd/ROADMAP.md）可观测：不读、不搬，仅留痕提示宿主处置。
    # 不自动搬家的原因：ROADMAP 是宿主资产，移动与提交归属由宿主决定
    # （一次性迁移点由安装器代搬，见 release/install.py::_roadmap_disposition）。
    # 判据按**内容**而非「根文件是否存在」（task-installer-legacy-roadmap-nonmask AC2）：
    # 旧判据 `legacy.exists() and not root.exists()` 会被空模板落根抑制 → 遮蔽反而更静默。
    if legacy.exists() and _legacy_roadmap_actionable(legacy, root_doc):
        _log_legacy_roadmap(root, legacy, root_doc=root_doc)
    return root_doc


def _legacy_roadmap_actionable(legacy: Path, root_doc: Path) -> bool:
    """旧布局副本是否仍需宿主处置：唯一源缺失，或两份内容已不一致。

    内容一致（安装器迁移后的稳态）视为「已处置」→ 不再重复告警，避免引擎热路径噪声；
    任何读取异常按「需处置」返回（宁可多报，不可静默）。CRLF/LF 差异不算分歧。
    """
    if not root_doc.exists():
        return True
    try:
        legacy_bytes = legacy.read_bytes()
        root_bytes = root_doc.read_bytes()
    except OSError:
        return True
    if legacy_bytes == root_bytes:
        return False
    return legacy_bytes.replace(b"\r\n", b"\n") != root_bytes.replace(b"\r\n", b"\n")


def _log_legacy_roadmap(root: Path, legacy: Path, *, root_doc: Path | None = None) -> None:
    """旧布局 ``.orchd/ROADMAP.md`` 留痕（禁静默）：ROADMAP 唯一源已改为宿主项目根。

    触发判据见 :func:`_legacy_roadmap_actionable`：唯一源缺失（``root_missing``）或与
    副本内容不一致（``content_diverges``）；``reason`` 区分可操作场景
    （task-installer-legacy-roadmap-nonmask AC2：不再被「宿主根已有文件」抑制）。

    与 ``worktree._log_recycle`` 同型：结构化 JSON 单行写 stderr，任何异常静默不阻断
    主流程。宿主据此把文件移到项目根（安装器在一次性迁移点代做），或删除已确认的旧副本。
    """
    try:
        target = root_doc if root_doc is not None else (root / _ROADMAP_DOC)
        reason = "root_missing" if not target.exists() else "content_diverges"
        record = {
            "action": "legacy_orchd_roadmap_ignored",
            "reason": reason,
            "expected": str(target),
            "legacy": str(legacy),
            "hint": (
                "ROADMAP 唯一源为宿主项目根：请把该文件移到项目根后重试"
                if reason == "root_missing"
                else "唯一源已就绪，.orchd/ROADMAP.md 为旧布局副本且内容已不一致："
                     "确认无待迁移内容后删除该副本"
            ),
        }
        print(f"orchd ▸ [roadmap] {json.dumps(record, ensure_ascii=False)}",
              file=sys.stderr)
    except Exception:
        pass


# ------------------------------------------------------------------
# 准入/公共文件锁（task-intake-file-lock）
# 锁维度盘点（task-audit-lock-residue-reclaim AC1）：intake 准入写锁
# ``.intake.lock`` 落于共享账本根（resolve_store_dir，container/flat 兼容）。
# 路径解析：ORCHD_HOME 重定向 > container 布局 ``<容器>/.orchd-runtime/`` > flat
# 回退 ``orchd_dir``（intake_lock_path）。生命周期：acquire（阻塞+超时 flock + 写
# 诊断标记）→ release（引用计数归零释放 flock，**不 unlink**，文件保留）→ 无 live
# flock 时由 intake_lock_check 判未持有；长时间未用的超时残留标记由
# intake_lock_check 自动清除（AC3）。迁移孤儿：flat→container 后旧路径
# ``<main>/.orchd/.intake.lock`` 由 reclaim_orphan_intake_locks 在 intake 路径回收（AC2）。
# ------------------------------------------------------------------
# 背景：_master.json / IDEAS.md / ROADMAP.md 是 git 跟踪的全局文件、无进程级锁，
# 与账本文件（被 Store.acquire_lock()) 的 flock 串行不对等。两个 agent 并行准入
# （intake/amend）同时改写这几个文件会互相覆盖 / 全量 amend 撞车。这里提供一把
# **独立快捷锁** ``.intake.lock``，落于共享账本根（resolve_store_dir，container/flat
# 兼容），只用进程内 flock 互斥 + 超时判定，**不**复用账本 Store 锁——从而保证并行
# claim/done（账本锁）不被一次 amend 阻塞。

_INTAKE_LOCK_FILENAME = ".intake.lock"
# 准入写最长持有锁的超时（秒）。超过视为僵死锁，watchdog 可巡检/告警（task-admission-lock-engine）。
_INTAKE_LOCK_TIMEOUT = 120
# 准入写获取锁的阻塞等待上限（秒）。超过即抛 E012 并给出明确处置指引，不无限等待。
# 可用环境变量 ORCHD_INTAKE_LOCK_WAIT_SECS 覆盖（task-admission-lock-engine：A 项）。
_INTAKE_LOCK_WAIT_SECS = 60

# 进程内准入锁注册表（task-admission-lock-engine 修复）：按规范锁路径持有
# (ExclusiveFileLock, refcount)，实现同一进程内嵌套获取的可重入，避免
# init → bootstrap_container 对同一 .intake.lock 的自死锁；跨进程仍依赖底层
# flock 真实互斥（并发 agent 阻塞等待）。
# task-concurrent-amend-lost-update：条目另持 threading.RLock（跨线程互斥；
# 同线程重入放行）。注册表自身的 get-or-create 由下方 guard 串行化（短临界，
# 无阻塞操作）。
_intake_lock_registry: dict = {}
_intake_lock_registry_guard = threading.Lock()


def _intake_lock_wait_secs() -> float:
    """准入写获取锁的阻塞等待上限（秒）。

    默认 :data:`_INTAKE_LOCK_WAIT_SECS`（60s）；环境变量
    ``ORCHD_INTAKE_LOCK_WAIT_SECS`` 可覆盖（须为正数）。
    """
    env = os.environ.get("ORCHD_INTAKE_LOCK_WAIT_SECS")
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return float(_INTAKE_LOCK_WAIT_SECS)


def intake_lock_path(orchd_dir: Path) -> Path:
    """返回准入锁文件路径（共享账本根下，container/flat 兼容）。"""
    return resolve_store_dir(orchd_dir) / _INTAKE_LOCK_FILENAME


def _reclaim_stale_lock_atomically(
    lock_path: Path, timeout_s: float
) -> dict[str, Any]:
    """以原子序清理陈旧准入锁标记（L-7），返回 ``{"status": ...}``。

    序列：``open → 非阻塞 flock → 经该 fd 重读确认仍陈旧 → unlink（持锁删除）
    → 释放``。要点：

    - **拿不到 flock 即让位**（返回 ``status="held"``）：首判据与本次 flock 之间
      他人可能刚获取活锁，此时绝不删除（旧实现 check→read→unlink 三步非原子，
      会删掉活锁造成 unlink-alias：文件消失、别人新建同名文件 → 双方同时持锁）；
    - **持锁重读**：以本 fd 独占时的内容为准，读到的是「此刻没有其它写者」的标记；
    - **持锁删除**：POSIX 下 flock 持有者删除自身锁文件是原子的（无 unlink-alias）；
    - **Windows 回退**：本进程句柄占用时 ``unlink`` 被系统拒绝（无 FILE_SHARE_DELETE），
      故在释放句柄后重试一次——该平台「他人持锁 = 其打开句柄存在 = 删除被系统拒绝」，
      删除动作由 OS 保证不会误删活锁（无需额外复检）。

    Returns:
        ``{"status": "held"|"fresh"|"unreadable"|"cleaned"|"cleanup_failed"}``；
        ``cleaned`` 附带 ``age_s`` 与 ``cleanup_result``（含是否真正删除文件）。
    """
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
    except OSError:
        return {"status": "unreadable"}
    owned = False
    stale_age: float | None = None
    removed = False
    try:
        try:
            _flock_op(fd, "lock_nb")
            owned = True
        except (OSError, IOError):
            return {"status": "held"}
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536).decode("utf-8", errors="replace")
        except OSError:
            return {"status": "unreadable"}
        try:
            data = json.loads(raw)
            ts = float(data.get("timestamp", 0))
        except (json.JSONDecodeError, ValueError, TypeError):
            return {"status": "unreadable"}
        age = time.time() - ts
        if age < timeout_s or not data.get("agent_id"):
            return {"status": "fresh"}
        stale_age = age
        try:
            lock_path.unlink()
            removed = not lock_path.exists()
        except OSError:
            removed = False  # Windows：本进程句柄占用 → 走 finally 内回退
    finally:
        if owned:
            try:
                _flock_op(fd, "unlock")
            except (OSError, IOError):
                pass
        try:
            os.close(fd)
        except OSError:
            pass
        if owned and stale_age is not None and not removed:
            try:
                lock_path.unlink()
                removed = not lock_path.exists()
            except OSError:
                removed = False
    return {
        "status": "cleaned" if removed else "cleanup_failed",
        "age_s": round(stale_age, 1) if stale_age is not None else None,
        "cleanup_result": {"cleared": removed, "path": str(lock_path)},
    }


def intake_lock_check(
    orchd_dir: Path, timeout_s: float = _INTAKE_LOCK_TIMEOUT
) -> dict[str, Any]:
    """检查准入锁状态（不阻塞），供 watchdog / caller 判定是否僵死。

    以 :class:`ExclusiveFileLock` 的 flock 探测为权威：无 live flock 持有即未锁；
    被持有则尽量读诊断标记（agent_id/timestamp）。task-audit-lock-residue-reclaim
    （AC3）：未持有但存在**超时残留标记**（无 live flock 的陈旧标记）时**自动清除**
    陈旧标记并返回 ``reason="timeout_cleaned"``——陈旧标记不再产生
    ``reason="timeout"`` 误导（watchdog 曾据此误报 stale_marker），返回未锁可续获取。

    Returns:
        ``{"locked": False}`` 未被持有（无 live flock，可获取）。
        ``{"locked": False, "reason": "timeout_cleaned", "age_s",
        "cleanup_result"}`` 未被持有且残留超时标记已被自动清除。
        ``{"locked": True, "agent_id", "timestamp", "age_s"}`` 被持有，诊断标记可读。
        ``{"locked": True, "reason": "no_marker"}`` 被持有但标记不可读。
        ``{"locked": True, "reason": "held"}`` 首判据未持有、但清理前复核发现被
        并发获取（原子清理让位，不删除活锁；L-7）。
    """
    lock_path = intake_lock_path(orchd_dir)
    probe = ExclusiveFileLock(lock_path).check()
    if probe.get("held"):
        # 优先走本进程持锁 fd 读取标记（Windows msvcrt 字节锁阻止新句柄读取）
        content = read_locked_text(lock_path)
        if content is None:
            try:
                content = lock_path.read_text(encoding="utf-8")
            except (OSError, IOError):
                content = None
        if content is None:
            return {"locked": True, "reason": "no_marker"}
        try:
            data = json.loads(content)
            ts = float(data.get("timestamp", 0))
            return {
                "locked": True,
                "agent_id": data.get("agent_id") or "unknown",
                "timestamp": data.get("timestamp", str(ts)),
                "age_s": round(time.time() - ts, 1),
            }
        except (OSError, IOError, json.JSONDecodeError, ValueError, TypeError):
            return {"locked": True, "reason": "no_marker"}
    # 未被持有（probe 已确认无 live flock）：清理陈旧标记必须是**原子序**（L-7），
    # 见 _reclaim_stale_lock_atomically：拿不到 flock 即让位（返回 locked=True/held），
    # 绝不删除他人刚获取的活锁；确认陈旧才持锁删除（Windows 走释放句柄后的 OS 保护回退）。
    if not lock_path.exists():
        return {"locked": False}
    try:
        outcome = _reclaim_stale_lock_atomically(lock_path, timeout_s)
    except (OSError, IOError, ValueError, TypeError):
        return {"locked": False}
    if outcome.get("status") == "held":
        return {"locked": True, "reason": "held"}
    if outcome.get("status") == "cleaned":
        return {
            "locked": False,
            "reason": "timeout_cleaned",
            "age_s": outcome.get("age_s"),
            "cleanup_result": outcome.get("cleanup_result"),
        }
    return {"locked": False}


def intake_lock_acquire(
    orchd_dir: Path, agent_id: str, timeout_s: float | None = None
) -> dict[str, Any]:
    """获取准入写锁（ExclusiveFileLock 原语 + 线程层 RLock，双层互斥）。

    flock 为跨进程唯一互斥权威；同进程跨线程由条目 RLock 互斥（同线程重入放行）
    ——此前"同一进程即重入"把跨线程并发也放行了（线程级 amend 并发零互斥，
    task-concurrent-amend-lost-update 实测撞出 git index.lock）。无强夺接管。

    与账本 Store 锁解耦：这里锁 ``.intake.lock``，不阻塞并行 claim/done。
    多进程尝试准入写时，后到者**阻塞等待** ``timeout_s``（默认 60s，可经
    ``ORCHD_INTAKE_LOCK_WAIT_SECS`` 覆盖）——正常并发的持有者释放后即自动成功，
    **不再**"无声卡死"；仅当真正僵死（持锁进程挂起/未退出）超过等待上限才抛 E012。

    Args:
        orchd_dir: .orchd 目录。
        agent_id: 当前 agent（仅写入诊断标记，不参与互斥）。
        timeout_s: 阻塞等待上限（秒）；``None`` → :func:`_intake_lock_wait_secs`
            （默认 60s，env 可覆盖）。

    Returns:
        锁句柄 dict（传给 :func:`intake_lock_release`）。

    Raises:
        OrchdError: E012 等待 ``timeout_s`` 内仍未拿到锁（持有者僵死）。
    """
    wait = _intake_lock_wait_secs() if timeout_s is None else timeout_s
    canonical = intake_lock_path(orchd_dir).resolve()
    # 进程内可重入（task-admission-lock-engine 修复）：同一进程对同一锁路径，
    # 仅引用计数 +1，不重复 flock——避免 init → bootstrap_container 的嵌套自死锁，
    # 同时保留跨进程真实 flock 互斥（并发 agent 仍阻塞等待）。
    # task-concurrent-amend-lost-update：上述"同一进程即重入"把**跨线程**并发也
    # 放行了（同进程第二线程直接 refcount+1，不碰 flock）——线程级 amend 并发零互斥，
    # 实测撞出 git index.lock 且 master 静默丢失。故加一层与 canonical 绑定的
    # threading.RLock：跨线程阻塞等待（同 timeout_s / E012 口径），同线程重入放行
    # （嵌套调用不断言）。flock 仍是跨进程唯一互斥权威，语义不变。
    with _intake_lock_registry_guard:
        entry = _intake_lock_registry.get(canonical)
        if entry is None:
            entry = {
                "lock": ExclusiveFileLock(canonical),
                "refcount": 0,
                "threadlock": threading.RLock(),
            }
            _intake_lock_registry[canonical] = entry
        threadlock = entry["threadlock"]
    if not threadlock.acquire(blocking=True, timeout=wait):
        raise OrchdError(
            ErrorCode.E012,
            "lock_timeout: failed to acquire .intake.lock (线程级阻塞超时，"
            "同进程他线程正持有准入写锁)",
            [{
                "path": str(canonical),
                "timeout_s": wait,
                "hint": (
                    "同进程内另一线程正在执行准入写且未释放（ информацией见上）。"
                    "已阻塞等待仍未拿到锁；检查持锁线程是否僵死，不要无上限重试。"
                ),
            }],
        )
    try:
        if entry["refcount"] > 0:
            entry["refcount"] += 1
            return {
                "acquired": True,
                "agent_id": agent_id,
                "path": str(canonical),
                "_lock": entry["lock"],
                "reentrant": True,
            }
        lock = entry["lock"]
        try:
            lock.acquire(blocking=True, timeout_s=wait)
        except OrchdError as exc:
            # 注入 intake 语义，保留 E012；hint 明确化（task-admission-lock-engine：C 项）
            raise OrchdError(
                ErrorCode.E012,
                "lock_timeout: failed to acquire .intake.lock (准入写被并发 agent 持有)",
                [{
                    "path": str(canonical),
                    "timeout_s": wait,
                    "hint": (
                        "另一 agent 正在执行准入写（intake / amend / roadmap-land / idea *）。"
                        f"已阻塞等待 {round(wait)}s 仍未拿到锁。若长时间无进展，可能是其进程僵死"
                        "（卡在子进程 / 等待交互）：请检查其进程，或等待其退出"
                        "（flock 将在进程退出时由内核自动释放），不要无上限重试。"
                    ),
                }],
            ) from exc
        entry["refcount"] = 1
    except OrchdError:
        threadlock.release()
        raise
    except Exception:
        threadlock.release()
        raise
    # 诊断标记（best-effort，非互斥依据）：供 intake_lock_check 报障
    try:
        lock.write_text(
            json.dumps({"agent_id": agent_id, "timestamp": str(time.time()),
                        "path": str(canonical)}, ensure_ascii=False) + "\n"
        )
    except OSError:
        pass
    # 迁移孤儿回收（task-audit-lock-residue-reclaim AC2）：账本根重定向
    # （container / ORCHD_HOME）时清理 flat→container 迁移后旧路径
    # ``<main>/.orchd/.intake.lock`` 残留，best-effort 不阻断准入。
    try:
        reclaim_orphan_intake_locks(orchd_dir)
    except Exception:
        pass
    return {"acquired": True, "agent_id": agent_id,
            "path": str(canonical), "_lock": lock}
    # 诊断标记（best-effort，非互斥依据）：供 intake_lock_check 报障
    try:
        lock.write_text(
            json.dumps({"agent_id": agent_id, "timestamp": str(time.time()),
                        "path": str(canonical)}, ensure_ascii=False) + "\n"
        )
    except OSError:
        pass
    # 迁移孤儿回收（task-audit-lock-residue-reclaim AC2）：账本根重定向
    # （container / ORCHD_HOME）时清理 flat→container 迁移后旧路径
    # ``<main>/.orchd/.intake.lock`` 残留，best-effort 不阻断准入。
    try:
        reclaim_orphan_intake_locks(orchd_dir)
    except Exception:
        pass
    return {"acquired": True, "agent_id": agent_id,
            "path": str(canonical), "_lock": lock}


def _mark_intake_lock_released(
    lock_path: Path | None, lock: dict[str, Any]
) -> None:
    """把准入锁标记改写为「已释放」（best-effort，绝不抛错）。

    task-intake-lock-released-marker（2026-09-15）：释放 flock 后写入
    ``{"agent_id", "released": true, "released_at", "timestamp", "path"}``，
    使 ``doctor._detect_residual_intake_locks`` 与卫生门禁一眼可判「已释放」，
    不再按「无 live flock 且 age >= 120s」报超时残留（消除常驻假红）。

    仍**不删除文件**——保住既有反 unlink-alias 设计（文件永久保留、flock 为唯一
    互斥权威）；重写失败静默降级：下一次准入写会用新鲜标记覆盖，功能不受影响。
    """
    if lock_path is None:
        return
    payload = {
        "agent_id": lock.get("agent_id") or "unknown",
        "released": True,
        "released_at": datetime.now(timezone.utc).isoformat(),
        "timestamp": str(time.time()),
        "path": str(lock_path),
    }
    try:
        Path(lock_path).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except (OSError, IOError, TypeError, ValueError):
        pass


def intake_lock_release(lock: dict[str, Any]) -> None:
    """释放准入锁（ExclusiveFileLock 原语释放）。

    进程内可重入：仅当引用计数归零才真正 flock 释放（与 :func:`intake_lock_acquire`
    的嵌套获取配对），线程层 RLock 随之释放。**不 unlink 锁文件**：文件永久保留，flock 释放后由
    :func:`intake_lock_check` 判为未持有——避免 unlink-alias / 删后误判持锁。

    已释放标记（task-intake-lock-released-marker）：flock 释放后经
    :func:`_mark_intake_lock_released` 把标记改写为 ``released: true``，使残留检测
    与卫生门禁不再把「正常释放后留下的诊断标记」判为需人工处置的超时残留。
    互斥语义零变化：flock 仍是唯一互斥权威，标记仅为诊断/留档。
    """
    lk = lock.get("_lock")
    if lk is None:
        return
    canonical = None
    p = lock.get("path")
    if p:
        try:
            canonical = Path(p).resolve()
        except (OSError, ValueError):
            canonical = None
    entry = _intake_lock_registry.get(canonical) if canonical else None
    if entry is None:
        # 不在注册表（跨进程持锁句柄 / 异常路径）→ 直接释放底层锁，不碰注册表
        try:
            lk.release()
        except (OSError, IOError):
            pass
        _mark_intake_lock_released(canonical, lock)
        return
    entry["refcount"] -= 1
    if entry["refcount"] <= 0:
        try:
            lk.release()
        except (OSError, IOError):
            pass
        _intake_lock_registry.pop(canonical, None)
        _mark_intake_lock_released(canonical, lock)
        # task-concurrent-amend-lost-update：随 flock 一并释放线程层（配对 acquire
        # 侧的 threadlock.acquire；重入中（refcount>0）不释放）。
        _tl = entry.get("threadlock")
        if _tl is not None:
            try:
                _tl.release()
            except Exception:
                pass


def reclaim_orphan_intake_locks(orchd_dir: Path) -> dict[str, Any]:
    """回收迁移后旧路径下的 ``.intake.lock`` 孤儿（task-audit-lock-residue-reclaim AC2）。

    flat→container 迁移后账本根从 ``<main>/.orchd/`` 变为 ``<runtime>/.orchd-runtime/``，
    flat 时代落在 ``<main>/.orchd/.intake.lock`` 的准入锁文件成为**迁移孤儿**（新路径
    在 runtime 根，旧路径不再被读写）。本函数在账本根重定向生效
    （``resolve_store_dir != orchd_dir``，container / ORCHD_HOME 重定向）时识别并清理
    旧路径残留：仅当无 live flock 持有才删除（与 :func:`intake_lock_clear` 同语义，
    防 unlink-alias 竞态）。

    Args:
        orchd_dir: 主工作树的 .orchd 目录。

    Returns:
        ``{"scanned": bool, "cleaned": [<str>], "reason": <str|None>}``：
        - ``scanned=False``：账本根未重定向（flat 未迁移），无迁移孤儿路径，零操作；
        - ``scanned=True``：已检查旧路径；``cleaned`` 列出已删除的孤儿文件路径
          （无 live flock 且旧路径存在时删除）；``reason="held"`` 表示旧路径仍被
          live flock 持有（不删除，保守跳过）。
    """
    canonical = resolve_store_dir(orchd_dir)
    old_path = Path(orchd_dir) / _INTAKE_LOCK_FILENAME
    if Path(canonical).resolve() == Path(orchd_dir).resolve():
        # 账本根未重定向（flat 未迁移）→ 不存在"旧路径"概念，零操作
        return {"scanned": False, "cleaned": []}
    if not old_path.exists():
        return {"scanned": True, "cleaned": []}
    if ExclusiveFileLock(old_path).check().get("held"):
        return {"scanned": True, "cleaned": [], "reason": "held"}
    try:
        old_path.unlink()
        return {
            "scanned": True,
            "cleaned": [str(old_path)] if not old_path.exists() else [],
        }
    except OSError:
        return {"scanned": True, "cleaned": []}


def intake_lock_clear(orchd_dir: Path) -> dict[str, Any]:
    """强制清理残量准入锁（best-effort，watchdog 调用）。

    仅当无 live flock 持有该锁文件时才删除（避免 unlink-alias）；若被持有则
    返回 ``cleared=False``。正常 acquire/release 不删除文件，此处只兜底清残留。
    """
    lock_path = intake_lock_path(orchd_dir)
    if not lock_path.exists():
        return {"cleared": False, "path": str(lock_path)}
    if ExclusiveFileLock(lock_path).check().get("held"):
        return {"cleared": False, "path": str(lock_path), "reason": "held"}
    try:
        lock_path.unlink()
        return {"cleared": True, "path": str(lock_path)}
    except OSError:
        return {"cleared": False, "path": str(lock_path)}


# task-storage-port-adapter-split：FilesystemBackend 已迁至 orchd.storage.filesystem。


# 跃迁白矩阵（task-audit-ledger-state-machine-dedup）：事件类型 → 允许的当前状态
# 集合。仅约束「会改变状态」的事件；目标状态 == 当前状态（幂等 self-transition，
# 如两阶段审查 spec APPROVED 后任务仍 in_review、REVIEW_READY(code) 再次到来）
# 永远合法。FORCE_STATUS（强制逃生口，支持 cancelled→pending 复活 / claimed→
# completed）、RETRACT（回滚）、REVIEW_CLAIMED（非状态跃迁）不受矩阵约束。
_TRANSITION_WHITELIST: dict[str, frozenset[str]] = {
    "CLAIMED": frozenset({"pending"}),
    "DONE": frozenset({"claimed"}),
    "REVIEW_READY": frozenset({"done", "in_review"}),
    "REVIEW_SUBMITTED": frozenset({"in_review"}),
}
# 不受白矩阵约束的事件类型（引擎逃生口 / 回滚 / 非状态跃迁）
_UNGATED_TRANSITIONS = frozenset({"FORCE_STATUS", "RETRACT", "REVIEW_CLAIMED"})


def _event_target_status(event: dict[str, Any]) -> str | None:
    """推导事件将设置的目标状态；不改变状态或不受约束的事件返回 None（跳过校验）。"""
    etype = event.get("type", "")
    if etype in _UNGATED_TRANSITIONS:
        return None
    if etype == "CLAIMED":
        return "claimed"
    if etype == "DONE":
        return "done"
    if etype == "REVIEW_READY":
        return "in_review"
    if etype == "REVIEW_SUBMITTED":
        verdict = event.get("verdict", "")
        rt = event.get("review_type")
        if verdict == "CHANGES_REQUESTED":
            return "pending"
        if verdict == "APPROVED" and (rt == "code" or rt is None):
            return "completed"
        if verdict == "APPROVED" and rt == "spec":
            return "in_review"  # self-transition，放行
    return None


def validate_transition(
    event_type: str,
    current_status: str,
    target_status: str,
) -> None:
    """跃迁白矩阵校验（引擎硬约束）：非法状态跃迁抛 E007（含当前/目标状态）。

    - 不受约束的事件（FORCE_STATUS / RETRACT / REVIEW_CLAIMED）直接放行；
    - 目标 == 当前（幂等 self-transition，状态不变）放行；
    - 否则要求 ``current_status`` 属于白矩阵中该事件允许的来源集合。
    未知事件类型保守放行（不阻塞未来事件扩展）。
    """
    if event_type in _UNGATED_TRANSITIONS:
        return
    allowed = _TRANSITION_WHITELIST.get(event_type)
    if allowed is None:
        return
    if target_status == current_status:
        return
    if current_status not in allowed:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_transition: {current_status} --{event_type}--> {target_status} "
            "不在跃迁白矩阵",
            [{
                "event_type": event_type,
                "current_status": current_status,
                "target_status": target_status,
                "hint": f"{event_type} 仅允许从 {sorted(allowed)} 状态跃迁到 "
                        f"{target_status}",
            }],
        )


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

        ``_file_lock`` 为 ExclusiveFileLock 原语实例，未持锁时 ``_lock_fd`` 为 None。
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
    def _lock_fd(self) -> Any | None:
        """兼容属性：转发到 backend._file_lock._fd，保证既有测试与调用方兼容。

        FilesystemBackend docstring 明文承诺'Store 的 _lock_fd 属性转发到本后端'，
        未持锁时为 None，持锁时为 fd 真值。
        """
        try:
            return self.backend._file_lock._fd  # type: ignore[attr-defined]
        except AttributeError:
            return None

    @_lock_fd.setter
    def _lock_fd(self, value: Any | None) -> None:
        """no-op setter，兼容直接赋值测试（不实际改变锁状态）。"""
        try:
            self.backend._file_lock._fd = value  # type: ignore[attr-defined]
        except AttributeError:
            pass

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
        """以 append 模式写入 JSONL，写入后 flush + fsync。

        写前校验跃迁白矩阵（引擎硬约束，2026-08-29）：受约束事件若引起非法状态
        跃迁（如 pending 直接 DONE）抛 E007、事件不落盘；self-transition /
        FORCE_STATUS / RETRACT / REVIEW_CLAIMED 放行。各写命令自身仍保留前置
        校验，此处为底层硬约束兜底，防调用方绕过状态机。

        持锁兜底（task-audit-ledger-write-atomicity AC1）：写命令在命令级
        ``acquire_lock`` 内调用（backend 持锁 → 直接写）；未持锁时（单测 /
        单次 append）自动加锁写一次保证原子，backed 层持锁断言因此恒满足。
        自动加锁前先查进程级锁登记（_depth_registry）：同进程其他 Store 实例
        （如 container 布局 review 合并流 merge_lock）已持同路径锁时不重复
        flock——同一进程双 fd 对同一区域加锁会自锁死（E012），须复用已持锁。

        锁序（L-5 修正）：**先加锁 → 锁内 replay+validate → 写事件**。旧实现把
        ``replay()`` 与 ``validate_transition`` 放在加锁之前，未持锁调用方存在
        check-then-act 窗口：两个进程各自基于陈旧状态通过校验后串行落盘，后者
        写入的是非法跃迁（校验形同虚设）。
        """
        task_id = event.get("task_id", "")
        target = _event_target_status(event)
        held = (
            self.backend._file_lock._fd is not None
            or str(self.lock_path.resolve()) in _depth_registry
        )
        if not held:
            self.acquire_lock()
        try:
            # 锁内校验：replay 读到的与本临界区落盘的是同一状态，校验与写入之间
            # 不再有其它写者（校验失败抛 E007 由 finally 释放锁）。
            if task_id and target:
                current = self.replay().get(task_id)
                validate_transition(
                    event.get("type", ""),
                    current.status if current else "pending",
                    target,
                )
            self.backend.append_event(event)
        finally:
            if not held:
                self.release_lock()
        # H4：已校准时追加 1 行（写入成功才递增；未校准则保持 None 惰性校准）
        if self._line_count is not None:
            self._line_count += 1
        # Session TTL（task-audit-session-ttl-lazy-expiry）：写命令路径事件追加
        # 成功后刷新当前会话 last_seen——写事件是宿主活动的最强信号，惰性过期
        # 据此判活。best-effort，任何异常不反向影响已成功的账本写入。
        _touch_session_last_seen(self.orchd_dir)

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
        - 中间行解析失败 → 降级跳过 + E030 warning（AC4，不再抛 E002）
        """
        checkpoint_line, tasks, retracted = self._load_checkpoint()
        events = self._read_ledger_lines(from_line=checkpoint_line + 1)
        self._replay_violations = []
        self._apply_events(events, tasks, retracted)
        return tasks

    def replay_full(self) -> dict[str, TaskState]:
        """全量 replay（忽略 checkpoint）。"""
        tasks: dict[str, TaskState] = {}
        retracted: set[str] = set()
        events = self._read_ledger_lines(from_line=1)
        self._replay_violations = []
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

        注意：停用 RETRACT 递归（``handle_retract=False``，task-audit-ledger-
        state-machine-dedup 合并去重后统一走 :meth:`_apply_events`）——前缀重放只
        关心前 ``n`` 条事件的派生结果，若 RETRACT 触发全量重建会越过前缀边界读到
        ``n`` 之后的事件，破坏前缀语义。

        L-11（2026-09-15）：``n`` 是**物理行号**（checkpoint.ledger_line 的口径），
        故按物理行切片而非 ``read_events()[:n]``（解析事件序号）——撕裂/损坏行会让
        两者错位（少一行事件 ⇒ 序号 n 对应物理行 n+1），旧实现因此多带一条事件，
        派生结果与 checkpoint 快照不符，把引擎自身的撕裂行误报成「疑似被篡改」。
        """
        events = self.backend.read_events(from_line=1, to_line=n)
        tasks: dict[str, TaskState] = {}
        retracted: set[str] = set()
        # 前缀重放是独立观测：重置违规收集，避免与调用方先前的全量 replay 混淆
        self._replay_violations = []
        # 与 _rebuild_after_retract 一致：先完整收集前缀内的 RETRACT 目标，
        # 再应用非撤回事件，避免被撤回事件「复活」。
        for ev in events:
            if ev.get("type") == "RETRACT":
                te = ev.get("target_event_id", "")
                if te:
                    retracted.add(te)
        self._apply_events(events, tasks, retracted, handle_retract=False)
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

        actual = self._count_ledger_lines()
        if ledger_line > actual:
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
        if isinstance(ckpt_version, int) and ckpt_version != _CHECKPOINT_SCHEMA_VERSION:
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
        try:
            if getattr(self.backend, "corrupt_lines", None) is None:
                self.backend.read_events()
            entries = [
                _attach_structured_guidance(dict(raw), self.orchd_dir)
                for raw in list(self.backend.corrupt_lines or [])
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
            self.backend._file_lock._fd is not None
            or str(self.lock_path.resolve()) in _depth_registry
        )
        if held:
            self._line_count = self._count_ledger_lines()
            return self._line_count
        try:
            # 非阻塞尝试（只读路径不得因并发写而阻塞）
            self.backend._file_lock.acquire(blocking=False)
        except OrchdError:
            self._line_count = self._count_ledger_lines()
            return self._line_count
        try:
            self._line_count = self._count_ledger_lines()
        finally:
            try:
                self.backend._file_lock.release()
            except (OSError, IOError):
                pass
        return self._line_count

    def _read_ledger_lines(self, from_line: int) -> list[dict[str, Any]]:
        """读取 ledger 从 ``from_line``（1-based）起的所有事件。

        参数 ``from_line`` 使用 1-based 行号，即 ``from_line=1`` 表示从文件第一行开始读取。
        内部实现委托 ``backend.read_events(from_line)``，在文件层先跳过前
        ``from_line - 1`` 行，从而定位到起始行（B-1 修复：被跳过的早期损坏行
        不参与解析，恢复 checkpoint 增量 replay 的容错语义）。

        容错：末行解析失败跳过 + warning；中间行失败降级跳过 + E030 warning
        （AC4，不再抛 E002 中断引擎）。
        """
        return self.backend.read_events(from_line=from_line)

    def _apply_events(
        self,
        events: list[dict[str, Any]],
        tasks: dict[str, TaskState],
        retracted: set[str],
        *,
        handle_retract: bool = True,
    ) -> None:
        """事件驱动的状态机：逐条应用事件列表，原地修改 ``tasks`` 和 ``retracted``。

        已被撤回（``retracted`` 集合中）的事件会被跳过；没有 ``task_id`` 的事件
        也会被忽略。单个事件的跃迁逻辑收敛到 :meth:`_apply_event`（去重后单一实现）。

        ``handle_retract``（合并去重的唯一分歧点，2026-08-29）：
        - ``True``（活跃路径 replay / replay_full）：遇到 RETRACT 时把目标事件标记
          撤回并触发 :meth:`_rebuild_after_retract` 全量重建，随后 **return**——
          重建已从头重放全部事件（含本 RETRACT 之后的事件），``tasks`` 已是最终
          状态，继续循环会重复应用；且可避免 K 个 RETRACT 触发 K 次 O(L) 级联重建
          （O(K·L) → 单次重建后即 O(L)）。
        - ``False``（_rebuild_after_retract / _replay_prefix 内部）：仅记录
          ``target_event_id`` 到 ``retracted``，不递归重建，避免无限递归 / 越过前缀边界。
        """
        for event in events:
            eid = event.get("event_id", "")
            if eid in retracted:
                # 跳过已被 RETRACT 撤回的事件，不纳入状态计算
                continue

            task_id = event.get("task_id", "")
            if not task_id:
                # 没有 task_id 的事件（如系统级事件）不影响任务状态，直接跳过
                continue

            if event.get("type") == "RETRACT":
                # RETRACT 在任务初始化之前处理：它是系统级回滚事件，不应为某任务
                # 凭空创建状态条目（活跃路径与重建路径一致，见 _apply_event 注释）。
                target_eid = event.get("target_event_id", "")
                if target_eid:
                    retracted.add(target_eid)
                if handle_retract:
                    # RETRACT 具有级联效应：撤回某事件后，后续依赖该事件的状态变化
                    # 都需要重新计算，因此必须从头全量重建（_rebuild_after_retract
                    # 已重放全部事件，tasks 已是最终状态，此处必须 return）
                    self._rebuild_after_retract(tasks, retracted)
                    return
                continue

            if task_id not in tasks:
                # 首次出现的 task_id，初始化默认状态（pending）
                tasks[task_id] = TaskState()
            self._apply_event(event, tasks[task_id])

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
        self._apply_events(all_events, tasks, retracted, handle_retract=False)

    @property
    def replay_violations(self) -> list[dict[str, Any]]:
        """最近一次 replay 收集到的序列级软校验违规（L-3，只读观测）。

        与 :meth:`check_integrity` 的区别：这里记录的是**语义可疑但派生状态照旧
        应用**的序列（如跨设备双认领），不构成篡改判定，也不新增 E030 误报；
        消费方是 sync 合并响应（结构化字段 / warnings）。
        """
        return list(getattr(self, "_replay_violations", []) or [])

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
        if status in ("completed", "cancelled") and etype in (
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

    def _apply_event(self, event: dict[str, Any], ts: TaskState) -> None:
        """应用单个非 RETRACT 事件到任务状态（原地修改 ``ts``）。

        支持的事件类型及其效果（task-audit-ledger-state-machine-dedup 合并去重后
        的单一实现，活跃路径与全量重建路径共用，根除「改一处漏一处」缺陷类别）：
            - ``CLAIMED``          : 任务被 agent 认领，状态 → ``claimed``，清空审核字段。
            - ``DONE``             : agent 提交完成，状态 → ``done``，递增 ``attempt_count``。
            - ``REVIEW_READY``     : 进入审核，状态 → ``in_review``，设置 ``review_phase``。
            - ``REVIEW_CLAIMED``   : reviewer 认领审核，设置 ``review_claimed_by``。
            - ``REVIEW_SUBMITTED`` : 审核结果提交。``APPROVED``(code) → ``completed``；
                                    ``CHANGES_REQUESTED`` → 回退 ``pending``。
            - ``FORCE_STATUS``     : 强制覆盖状态到指定值，附带相关字段重置逻辑。

        RETRACT 由调用方 :meth:`_apply_events` 在进入本方法前拦截（活跃路径触发
        全量重建、重建路径仅记录），此处只处理状态跃迁事件。跃迁合法性由白矩阵
        （:func:`validate_transition`）在写入时校验。

        L-3（2026-09-15）：入口先做**序列级软校验**（:meth:`_record_soft_violation`）
        ——只观测收集、不阻断也不改变下方派生结果，供 sync 合并响应暴露
        「跨设备双认领」类语义冲突。
        """
        self._record_soft_violation(event, ts)
        etype = event.get("type", "")
        if etype == "CLAIMED":
            ts.status = "claimed"
            ts.claimed_by = event.get("agent_id")
            ts.claimed_session = event.get("session_id")
            ts.review_phase = None
            ts.review_claimed_by = None
            ts.review_claimed_session = None
            ts.review_claimed_at = None
            ts.review_self_review = False

        elif etype == "DONE":
            ts.status = "done"
            ts.attempt_count = event.get("attempt_count", ts.attempt_count + 1)

        elif etype == "REVIEW_READY":
            ts.status = "in_review"
            ts.review_phase = event.get("review_type")
            ts.review_claimed_by = None
            ts.review_claimed_session = None
            ts.review_claimed_at = None
            # v4：新一轮审查开始，自审标记随上一轮清零（事实由历史事件承载）
            ts.review_self_review = False

        elif etype == "REVIEW_CLAIMED":
            ts.review_claimed_by = event.get("agent_id")
            ts.review_claimed_session = event.get("session_id")
            ts.review_claimed_at = event.get("timestamp")
            # v4（2026-09-15 停服升级）：自审事实落账——事件缺失该字段的历史数据
            # 视为非自审（False），需回查历史时仍可用 DONE/REVIEW_CLAIMED 的
            # agent_id 相等性启发式推导，二者不冲突。
            ts.review_self_review = bool(event.get("is_self_review"))

        elif etype == "REVIEW_SUBMITTED":
            verdict = event.get("verdict", "")
            review_type = event.get("review_type")
            if verdict == "APPROVED":
                if review_type == "code" or review_type is None:
                    # review-unify-r2：code review 通过，或 unified 单阶段
                    # （事件无 review_type 字段）APPROVED → 任务彻底完成；
                    # 老事件含 review_type: spec 仍按两阶段语义（仅 spec 通过，
                    # 等待 code），保持 checkpoint 与历史一致。
                    ts.status = "completed"
                    ts.review_phase = None
                    ts.review_claimed_by = None
                    ts.review_claimed_session = None
                    ts.review_claimed_at = None
                    # v4（2026-09-15 停服升级）：自审标记不随审查字段清空——completed
                    # 之后仍需能回查「本次完成系自审通过」；用 or 语义合并，使历史事件
                    # （无 is_self_review 字段）不清掉 REVIEW_CLAIMED 已落的值。
                    ts.review_self_review = ts.review_self_review or bool(
                        event.get("is_self_review")
                    )
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
                ts.claimed_session = None
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                # v4：审查被打回 → 回退 pending，本轮自审标记随回退清零
                # （事实仍由历史 REVIEW_SUBMITTED 事件承载，缺口不回退）
                ts.review_self_review = False

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
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                ts.review_self_review = False
                ts.claimed_by = None
                ts.claimed_session = None
            elif target == "claimed":
                # 强制认领：设置指派的 agent
                ts.claimed_by = event.get("assignee")
                ts.claimed_session = event.get("session_id")
            elif target == "cancelled":
                # 强制取消：清空所有执行和审核相关字段
                ts.claimed_by = None
                ts.claimed_session = None
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                ts.review_self_review = False
            elif target == "completed":
                # 强制完成：保留实现者信息（claimed_by），仅清空审查字段，
                # 与 code APPROVED 语义对齐（避免 completed 状态残留审查阶段/审查者）
                ts.review_phase = None
                ts.review_claimed_by = None
                ts.review_claimed_session = None
                ts.review_claimed_at = None
                # v4：人工强制完成无审查事件来源，自审标记一并清零（避免残留
                # 上一轮的 True 被误读为「本次完成系自审通过」）
                ts.review_self_review = False

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

        字段漂移自愈（task-merge-failed-completed 附带修复，2026-08-25）：`state`
        通常由增量 ``replay()`` 得到——若 checkpoint 早于某个 TaskState 字段引入
        （如 review_claimed_session），增量 replay 会从旧 checkpoint 继承「缺该字段」
        的状态，再把这些字段集写回 checkpoint，导致缺漏**自我传播**、E030
        （checkpoint 快照与 ledger 重放不一致）持续无法自愈。因此这里改为基于
        ``replay_full()``（从第 1 行重放，始终含全字段）构建快照；仅当全量重放
        失败时回退到传入 ``state``。
        """
        if retracted is None:
            retracted = self._collect_retracted_event_ids()
        # H4：行数由内存计数器提供（O(1)），不再全文件数行
        total_lines = self._current_line_count()
        # P2-10 / 1.4.1：稳态用增量 state（O(tail)）；仅 checkpoint schema 版本落后
        # 时才 replay_full() 自愈字段漂移一次。这样写命令不再每次 O(L) 全量重放。
        prev = self.backend.load_checkpoint() or {}
        needs_full = prev.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION
        if needs_full:
            try:
                full_state = self.replay_full()
                tasks = {tid: ts.to_dict() for tid, ts in full_state.items()}
            except (OrchdError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as _exc:
                # 收窄：仅捕获可预期的重放/序列化异常，不吞全部 Exception
                tasks = {tid: ts.to_dict() for tid, ts in state.items()}
        else:
            tasks = {tid: ts.to_dict() for tid, ts in state.items()}
        checkpoint = {
            "ledger_line": total_lines,
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "tasks": tasks,
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
        # M-1 合并扫描：单次读取后同时收集撤回集合与派生信息（3→1）
        all_events = self._read_ledger_lines(from_line=1)
        retracted: set[str] = set()
        for ev in all_events:
            if ev.get("type") == "RETRACT":
                te = ev.get("target_event_id", "")
                if te:
                    retracted.add(te)
        for event in all_events:
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
                # task-review-comments-gate-and-stale-timeout（B）：与
                # extract_review_comments 对齐，历史空打回注入占位（derived 缓存路径）。
                elif event.get("verdict") == "CHANGES_REQUESTED":
                    info.review_comments.setdefault(tid, []).append(
                        "[该次打回未附审查意见（历史数据），请联系审查者补充；"
                        "当前版本已强制要求 CHANGES_REQUESTED 必须附意见]"
                    )
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
