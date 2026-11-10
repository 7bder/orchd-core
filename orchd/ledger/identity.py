"""orchd 身份派生与事件时间原语（file-split-program W1-L3；2026-11-01 再拆）。

本模块保留与 session runtime 无关的叶子原语：

- 身份派生：``resolve_agent_id`` / ``resolve_session_identity`` /
  ``is_fingerprint_agent_id``（``ORCHD_SESSION_ID`` 注入 → 64 位 session_id +
  12 位兼容指纹）；
- 事件时间序：``_parse_session_ts`` / ``parse_event_time`` / ``event_sort_key``。

session runtime / lifecycle / TTL / lineage 已迁至 ``orchd/ledger/session.py``
（task-residuals-ledger-identity-split），聚合入口 ``orchd.ledger`` 的重导出面
保持不变。依赖方向：顶层只依赖标准库与 ``orchd.constants``。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.constants import ORCHD_SESSION_ID_ENV
from orchd.errors import ErrorCode, OrchdError

# 宿主注入的每对话唯一会话标识环境变量（session-id-fingerprint）。
_ORCHD_SESSION_ID_ENV = ORCHD_SESSION_ID_ENV  # 变量名真源：orchd.constants


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
      审查须由**当前实现会话主动 spawn 的独立 subagent** 携带自己的 session token
      领取（异指纹只是 E016 的下限，不足以证明「是子代理」；三条禁止与无 subagent
      能力即中断见 rules/review.md「审查者身份约定」）。
    - 各 agent 宿主（TRAE / codex / opencode / workbuddy 等）在启动 orchd 前
      统一把各自会话唯一码注入 ``ORCHD_SESSION_ID``，本函数只认该标准化变量。

    Note:
        单一事实源为 :func:`resolve_session_identity`，本函数取其 ``fingerprint``。
    """
    return resolve_session_identity(orchd_dir)["fingerprint"]


def _derive_session_identity_from_token(sid: str) -> dict[str, str]:
    """由会话 token 确定性派生 session_id/fingerprint（与 resolve 同算法）。"""
    import hashlib

    full = hashlib.sha256(("orchd-session:" + sid).encode("utf-8")).hexdigest()
    return {"session_id": full, "fingerprint": full[:12]}


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
    return _derive_session_identity_from_token(sid)


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


def _lineage_text(value: Any) -> str:
    """lineage 字段归一：**只有真字符串**才回显，其余脏值一律按「未声明」处理。

    task-session-runtime-dirty-input：旧实现 ``str(value or "")`` 会把手写
    runtime 里的 ``{"a":1}`` / ``123`` / ``True`` 变成 ``"{'a': 1}"`` / ``"123"``
    / ``"True"`` 这类非空垃圾，令 ``review_lineage_unverified`` 的「缺声明」告警
    被静默压制——观测面因此失去可信度。此处类型判定优先于真值判定：非 ``str``
    直接返回空串，``strip()`` 后为空（纯空白）同样算未声明。
    """
    return value.strip() if isinstance(value, str) else ""


# --parent 取值域（12–64 位小写 hex）：12 位 = 兼容指纹形态，64 位 = session_id 全形态。
_PARENT_SESSION_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")

# --adopt 注入 token 的合法形态：session start 生成的 ``uuid4().hex``。
_SESSION_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def _validate_parent_session_id(parent_session_id: str | None) -> str:
    """校验并归一 ``--parent``：空值合法（未声明），非空须 12–64 位小写 hex。"""
    value = (parent_session_id or "").strip()
    if value and not _PARENT_SESSION_ID_RE.fullmatch(value):
        raise OrchdError(
            ErrorCode.E007,
            "invalid_parent_session_id: --parent 仅接受 12–64 位小写 hex"
            "（12 位指纹或 64 位 session_id）",
            [{
                "field": "parent_session_id",
                "observed_length": len(value),
                "hint": (
                    "用 'orchd session current'（在实现会话内执行）获取完整 "
                    "session_id 后原样传入；不要传指纹以外的自造标识"
                ),
            }],
        )
    return value


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
