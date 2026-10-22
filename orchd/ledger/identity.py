"""orchd 身份与会话域（file-split-program W1-L3，2026-10-15）。

本模块自 ``orchd/ledger/__init__.py`` 整块搬出「身份 + 会话」两组叶子原语（15 函数 + 5 私有
常量，语句级等价，零逻辑改动）：

- 身份派生：``resolve_session_identity`` / ``resolve_agent_id`` / ``is_fingerprint_agent_id``
  / ``_derive_session_identity_from_token``（``ORCHD_SESSION_ID`` 注入 → 64 位 session_id +
  12 位兼容指纹）；
- session runtime：``session_runtime_dir`` / ``_session_runtime_path`` / ``session_start`` /
  ``session_current`` / ``session_end`` / ``_touch_session_last_seen``；
- TTL 与事件时间序：``session_ttl_minutes`` / ``_parse_session_ts`` / ``parse_event_time`` /
  ``event_sort_key`` / ``is_session_expired``。

依赖方向：顶层只依赖标准库与 ``orchd.errors``；对 ``resolve_store_dir``（paths 域）与
``_write_text_atomic``（atomic 域）的引用一律用**函数级惰性导入**
``from orchd.ledger import ...``：① 避免与聚合入口 ``orchd/ledger/__init__.py`` 形成加载期
循环；② 保持「调用点解析」语义——``monkeypatch.setattr("orchd.ledger.resolve_store_dir"`` /
``"_write_text_atomic", ...)`` 的既有补丁面不变（搬移前这些调用走同模块全局查找，补丁同样生效）。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError


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
    from orchd.ledger import resolve_store_dir
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
    from orchd.ledger import _write_text_atomic
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
        _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
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
    from orchd.ledger import _write_text_atomic
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
    _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
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
    from orchd.ledger import _write_text_atomic
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
    _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
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
    from orchd.ledger import _write_text_atomic
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
    _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {**data, "path": str(path), "ended": True}
