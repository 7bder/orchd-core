"""orchd session runtime / lifecycle / TTL / lineage 域。

自 ``orchd/ledger/identity.py`` 迁出（task-residuals-ledger-identity-split）：
session runtime 目录与文件、TTL 与过期判定、session start/current/end、
lineage 记录与畸形 runtime 结构化错误。身份派生与事件时间原语仍在
``orchd/ledger/identity.py``；聚合入口 ``orchd.ledger`` 的重导出面不变。

依赖方向：顶层只依赖标准库、``orchd.constants`` / ``orchd.errors`` 与
同包 ``identity``（身份派生 / 时间解析）；对 ``resolve_store_dir`` /
``_write_text_atomic`` 的引用保持函数级惰性导入，避免加载期环并保留调用点补丁。
"""

from __future__ import annotations

import json
import os

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchd.constants import ORCHD_SESSION_ID_ENV, ORCHD_SESSION_TTL_MIN_ENV
from orchd.errors import ErrorCode, OrchdError
from orchd.ledger.identity import (
    _PARENT_SESSION_ID_RE,
    _SESSION_TOKEN_RE,
    _derive_session_identity_from_token,
    _lineage_text,
    _parse_session_ts,
    _validate_parent_session_id,
    resolve_session_identity,
)


# ------------------------------------------------------------------
# Session runtime（task-session-cli-lifecycle，Session Identity Layer）
# ------------------------------------------------------------------
# 每个 session 由引擎显式开启：session start 生成唯一 session_token +
# 派生 session_id/fingerprint，并写入共享账本根下的 sessions/<id>.json；
# 后续命令通过 ORCHD_SESSION_ID 指向该 session。它把“会话边界”从宿主
# 的隐式环境常量升级为引擎持有的运行时实体，避免多个对话共享同一指纹。

_SESSION_RUNTIME_DIRNAME = "sessions"
_SESSION_RUNTIME_ACTIVE = True



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


_SESSION_TTL_MIN_ENV = ORCHD_SESSION_TTL_MIN_ENV  # 变量名真源：orchd.constants
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
        # 脏 runtime（顶层 null / [1,2] / 标量）不是对象：与「无该文件」同档静默
        # 降级，避免裸 AttributeError（json.loads 只兜 JSON 语法错，兜不住形态错）。
        if not isinstance(data, dict) or not data.get("active"):
            return
        data["last_seen"] = datetime.now(timezone.utc).isoformat()
        _write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except (OSError, ValueError, TypeError, AttributeError):
        # best-effort 活性刷新：形态/类型脏值（AttributeError/TypeError）同样静默
        # 降级——失败只影响判活精度，不得击穿写命令主路径。
        return


# lineage 声明的合法角色取值（唯一真源：CLI 的 ``--kind`` choices 与本常量对拍，
# 不另立第二份词表）。start 仅把声明记进 runtime；判定发生在审查 claim/submit
# 的 lineage 硬门（:func:`require_review_lineage`）。
SESSION_KINDS = ("implementer", "reviewer")

# --parent 取值域校验 / token 形态校验的真源在 ``orchd.ledger.identity``
# （task-cont-session-adopt-end-guard）；此处保留同名绑定，聚合入口重导出同一对象。


def session_start(
    orchd_dir: Path,
    agent_name: str | None = None,
    kind: str | None = None,
    parent_session_id: str | None = None,
    *,
    adopt: bool = False,
) -> dict[str, Any]:
    """开启一个新的会话，返回 engine 生成的 session_id/fingerprint/token。

    每次调用都会生成全新 ``session_token``（UUID），据此确定性派生
    ``session_id`` 与兼容指纹。写入：
    ``<runtime>/sessions/<session_id>.json``。

    ``kind`` / ``parent_session_id`` 是 lineage **声明面**
    （task-review-subagent-lineage-record）：把调用方自己声明的会话角色与父会话
    记进 runtime，供 ``session current`` 回显与 reviewer 认领响应引用。判定发生在
    审查 claim/submit 的 lineage 硬门（:func:`require_review_lineage`，
    task-review-hardblock-e3-lineage-gate）：``kind == reviewer`` 且
    ``parent_session_id`` == 该任务最后一次 DONE 事件的 ``session_id``（DONE 归属
    口径见 :mod:`orchd.onboard.lifecycle.core.finish`：agent_id=claimed_by、
    session_id=执行 done 的调用方会话），不符一律 E016。本函数只做格式校验
    （``--parent`` 12–64 位小写 hex），不校验 parent 是否真实存在于其他会话
    （自声明残余，见 require_review_lineage 模块注释）。
    缺省写空串：「声明缺失」本身就是要能被检索到的信号。
    ``kind`` 的取值域**只在 CLI 面**（``--kind`` 的 argparse choices）校验；本 API
    不做域校验，审计侧不得假设 ``kind`` 一定属于
    ``SESSION_KINDS``，更不得把「两键非空」读成「独立审查已被引擎印证」。
    ``parent_session_id`` 非空时须为 12–64 位小写 hex
    （task-cont-session-adopt-end-guard），否则 E007 ``invalid_parent_session_id``。

    ``adopt=True``（task-cont-session-adopt-end-guard）：不为当前会话生成第二
    身份——对当前 ``ORCHD_SESSION_ID`` 指向的 token 建立/复用 runtime：已有
    active runtime → 原样复用（仅刷新 ``last_seen``，不生成新 token、不改声明）；
    无 runtime → 按该 token 派生身份补建（``session_token`` = env token）。
    inactive / 过期 / 畸形 runtime 一律 E033（``--adopt`` 不复活已结束会话），
    env token 缺失同样 E033。

    调用方（宿主接入层）应把返回的 ``session_token`` 注入
    ``ORCHD_SESSION_ID`` 环境变量，使本会话后续命令解析到同一身份。
    """
    from orchd.ledger import _write_text_atomic
    orchd_dir = Path(orchd_dir)
    parent_session_id = _validate_parent_session_id(parent_session_id)
    now = datetime.now(timezone.utc).isoformat()
    if adopt:
        token = os.environ.get(ORCHD_SESSION_ID_ENV, "").strip()
        if not token:
            raise OrchdError(
                ErrorCode.E033,
                "session_identity_missing: --adopt 需要宿主已注入 "
                "ORCHD_SESSION_ID，当前环境无会话 token",
                [{
                    "reason": "adopt_missing_token",
                    "hint": (
                        "--adopt 语义是「为已注入的 token 建立/复用 runtime」，"
                        "不生成新身份；宿主未注入时请直接 'orchd session start' "
                        "取新 token 并注入 ORCHD_SESSION_ID"
                    ),
                }],
            )
        if not _SESSION_TOKEN_RE.fullmatch(token):
            raise OrchdError(
                ErrorCode.E033,
                "session_token_malformed: --adopt 注入的 ORCHD_SESSION_ID 形态非法"
                "（须为 32 位小写 hex，即 session start 生成的 uuid4().hex）",
                [{
                    "reason": "adopt_token_malformed",
                    "observed_length": len(token),
                    "expected": "^[0-9a-f]{32}$",
                    "hint": (
                        "不要手工拼造或截断 token；重新运行 "
                        "'python .orchd/__main__.py session start' 取新 token，"
                        "把返回的 session_token 原样注入 ORCHD_SESSION_ID"
                    ),
                    "command": "python .orchd/__main__.py session start",
                }],
            )
        identity = _derive_session_identity_from_token(token)
        path = _session_runtime_path(orchd_dir, identity["session_id"])
        if path.exists():
            try:
                runtime_doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise _malformed_session_runtime(
                    identity["session_id"], path,
                    observed=f"decode:{type(exc).__name__}",
                ) from exc
            if not isinstance(runtime_doc, dict):
                raise _malformed_session_runtime(
                    identity["session_id"], path,
                    observed=f"top_level:{type(runtime_doc).__name__}",
                )
            if not runtime_doc.get("active"):
                raise OrchdError(
                    ErrorCode.E033,
                    "session_inactive: 该 token 的 session 已结束，--adopt 不复活"
                    "已结束会话，请重新 session start",
                    [{"session_id": identity["session_id"],
                      "reason": "session_inactive", "path": str(path)}],
                )
            if is_session_expired(runtime_doc):
                raise OrchdError(
                    ErrorCode.E033,
                    "session_expired: 该 token 的 session 已超过 TTL，--adopt 不"
                    "复活过期会话，请重新 session start 取新 token",
                    [{
                        "session_id": identity["session_id"],
                        "reason": "session_expired",
                        "ttl_minutes": session_ttl_minutes(),
                        "last_seen": runtime_doc.get("last_seen"),
                        "hint": (
                            "会话已按 TTL 惰性过期（判定即生效）：重新运行 "
                            "'orchd session start' 开启新会话并注入新 token"
                        ),
                    }],
                )
            runtime_agent = str(runtime_doc.get("agent_name") or "").strip()
            requested_agent = str(agent_name or "").strip()
            if runtime_agent and runtime_agent != requested_agent:
                raise OrchdError(
                    ErrorCode.E033,
                    "session_agent_mismatch: 该 token 属于其他 agent，"
                    "--adopt 不接管",
                    [{
                        "reason": "adopt_agent_mismatch",
                        "session_id": identity["session_id"],
                        "expected_agent": runtime_agent,
                        "requested_agent": requested_agent,
                        "hint": (
                            "如确认就是该 agent 的同一会话，请原样复述 agent 名："
                            f"'python .orchd/__main__.py session start --adopt "
                            f"--agent {runtime_agent}'；否则用 session start "
                            "另取身份"
                        ),
                        "command": (
                            "python .orchd/__main__.py session start --adopt "
                            f"--agent {runtime_agent}"
                        ),
                    }],
                )
            # 复用：幂等——不生成新 token、不改声明，仅刷新活性信号。
            runtime_doc["last_seen"] = now
            _write_text_atomic(
                path,
                json.dumps(runtime_doc, ensure_ascii=False, indent=2) + "\n")
            return {
                **runtime_doc,
                "path": str(path),
                "started": True,
                "adopted": True,
                "reused": True,
                "inject_action": {
                    "description": (
                        "将 session_token 注入 ORCHD_SESSION_ID 环境变量，"
                        "后续命令即识别为同一会话身份"
                    ),
                    "powershell": f'$env:ORCHD_SESSION_ID="{token}"',
                    "bash": f'export ORCHD_SESSION_ID="{token}"',
                    "env_var": ORCHD_SESSION_ID_ENV,
                    "token": token,
                },
            }
        # 补建：无 runtime → 按 env token 派生身份建档，不生成新 token。
        fresh_doc: dict[str, Any] = {
            "session_id": identity["session_id"],
            "fingerprint": identity["fingerprint"],
            "session_token": token,
            "agent_name": agent_name or "",
            "kind": (kind or "").strip(),
            "parent_session_id": parent_session_id,
            "created_at": now,
            "last_seen": now,
            "active": _SESSION_RUNTIME_ACTIVE,
            "adopted_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_text_atomic(
            path, json.dumps(fresh_doc, ensure_ascii=False, indent=2) + "\n")
        return {
            **fresh_doc,
            "path": str(path),
            "started": True,
            "adopted": True,
            "reused": False,
            "inject_action": {
                "description": (
                    "将 session_token 注入 ORCHD_SESSION_ID 环境变量，"
                    "后续命令即识别为同一会话身份"
                ),
                "powershell": f'$env:ORCHD_SESSION_ID="{token}"',
                "bash": f'export ORCHD_SESSION_ID="{token}"',
                "env_var": ORCHD_SESSION_ID_ENV,
                "token": token,
            },
        }
    token = uuid.uuid4().hex
    identity = _derive_session_identity_from_token(token)
    now = datetime.now(timezone.utc).isoformat()
    data: dict[str, Any] = {
        "session_id": identity["session_id"],
        "fingerprint": identity["fingerprint"],
        "session_token": token,
        "agent_name": agent_name or "",
        "kind": (kind or "").strip(),
        "parent_session_id": (parent_session_id or "").strip(),
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
            "description": (
                "将 session_token 注入 ORCHD_SESSION_ID 环境变量，"
                "后续命令即识别为同一会话身份"
            ),
            "powershell": f'$env:ORCHD_SESSION_ID="{token}"',
            "bash": f'export ORCHD_SESSION_ID="{token}"',
            "env_var": ORCHD_SESSION_ID_ENV,
            "token": token,
        },
    }


def _malformed_session_runtime(
    session_id: str, path: Path, *, observed: str
) -> OrchdError:
    """构造 runtime 畸形（内容不是合法 JSON 对象）的结构化 E033。

    task-session-runtime-dirty-input：``path.exists()`` 只证明文件在，不证明内容
    可读成对象。顶层 ``null`` / ``[1,2]`` / 截断 JSON 都会让后续 ``data.get`` 或
    ``json.loads`` 抛裸 ``AttributeError`` / ``JSONDecodeError``——session_current
    作为直读入口会直接崩。此处统一收口成可检索的 ``reason=session_malformed``，
    与 not_found / inactive / expired 并列，处置同样是重新 ``session start``。
    """
    return OrchdError(
        ErrorCode.E033,
        "session_malformed: 当前 session runtime 内容不是合法 JSON 对象，请重新 session start",
        [{
            "session_id": session_id,
            "reason": "session_malformed",
            "path": str(path),
            "observed": observed,
            "hint": (
                "runtime 文件被脏写（截断 / 顶层非对象 / 非法 JSON）：重新运行 "
                "'orchd session start' 获取干净 runtime，并把返回的 session_token "
                "注入 ORCHD_SESSION_ID；判定即生效，无需手工删文件"
            ),
        }],
    )


def require_session_runtime(orchd_dir: Path) -> dict[str, Any]:
    """校验当前 ``ORCHD_SESSION_ID`` 对应 runtime 存在、active、未过 TTL。

    task-feedback-session-runtime-gate：写命令与 ``session current`` 必须共用
    同一判据，不能出现「诊断命令 E033、写命令放行」的割裂。身份为空仍由
    调用方报 session_identity_missing；本函数负责 runtime 四态：
    session_not_found / session_malformed / session_inactive / session_expired。
    ``session_malformed``（task-session-runtime-dirty-input）覆盖「文件在、但内容
    不是合法 JSON 对象」——顶层 ``null`` / ``[1,2]`` / 截断 JSON 曾被裸
    ``AttributeError`` / ``JSONDecodeError`` 击穿成崩溃，现归一为结构化 E033。

    Returns:
        runtime JSON 字典（未刷新 last_seen；由 session_current 负责刷新）。

    Raises:
        OrchdError E033：身份缺失或 runtime 缺失 / 畸形 / inactive / expired。
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
        raise OrchdError(
            ErrorCode.E033,
            "session_not_found: 当前指纹没有对应的 session runtime 文件",
            [{
                "session_id": identity["session_id"],
                "reason": "session_not_found",
                "hint": (
                    "请先运行 'orchd session start' 开启会话，并将 session_token 注入 "
                    "ORCHD_SESSION_ID；若已有活跃 session 可复用其 session_token，无需重复 start"
                ),
            }],
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 截断 / 半截落盘 / 无读权限：与「顶层形态错」同档，结构化 E033。
        raise _malformed_session_runtime(
            identity["session_id"], path, observed=f"decode:{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict):
        raise _malformed_session_runtime(
            identity["session_id"], path,
            observed=f"top_level:{type(data).__name__}",
        )
    if not data.get("active"):
        raise OrchdError(
            ErrorCode.E033,
            "session_inactive: 当前 session 已结束，请重新 session start",
            [{"session_id": data.get("session_id"), "reason": "session_inactive",
              "path": str(path)}],
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
    return data



def session_lineage(orchd_dir: Path) -> dict[str, str]:
    """当前会话 runtime 的 lineage 两字段（只读、best-effort、**永不抛**）。

    runtime 缺失 / inactive / 过期 / 畸形（JSON 损坏 / 顶层不是对象 ``[1,2]`` / ``null``）
    时一律返回两个空串——返回值供 :func:`require_review_lineage` 在审查
    claim/submit 侧做 lineage 硬门判定（缺失/不符即 E016 阻断）；本函数自身只读
    不抛，判定与阻断都在其调用方。字段值非真字符串（对象 / 整数 / 布尔）或
    ``strip()`` 后为空同样按「未声明」处理（见 :func:`_lineage_text`）。
    """
    orchd_dir = Path(orchd_dir)
    data: Any = None
    try:
        data = require_session_runtime(orchd_dir)
    except OrchdError:
        data = None
    except (OSError, ValueError, TypeError, AttributeError):
        # runtime 读不出 / JSON 坏 / 顶层形态不对：与「未声明」同档处理，
        # 不放大成认领事故（本函数的契约就是「永不抛」）
        data = None
    if not isinstance(data, dict):
        data = {}
    return {
        "kind": _lineage_text(data.get("kind")),
        "parent_session_id": _lineage_text(data.get("parent_session_id")),
    }


# ------------------------------------------------------------------
# 审查 lineage 硬门（task-review-hardblock-e3-lineage-gate，2026-11-02 P0 收口）
# ------------------------------------------------------------------
# ``E016`` 的指纹门只能证明「审查者指纹与实现者不同」，证明不了「它是实现会话
# spawn 的独立 subagent」——同一个 agent 另开会话即满足指纹不同。本门把 lineage
# 升级为引擎**可机判**的最强门：审查认领与提交时校验当前会话 runtime 的
# ``kind == reviewer`` 且 ``parent_session_id`` 等于该任务最后一次 DONE 事件的
# ``session_id``，不符一律 E016（无降级档、无逃生口）。
# DONE 事件归属口径（task-cont-done-lineage-docs，真源
# ``orchd/onboard/lifecycle/core/finish.py``）：``agent_id`` = 任务认领者
# ``claimed_by``，``session_id`` = 执行 done 的调用方会话——expected parent 即
# 「执行 done 的那个实现会话」。
#
# 残余（必须如实声明，不得写成降级通道）：parent 仍是**自声明**——被审查方完全可以
# 伪造一对 (kind, parent) 取值骗过本门；真正不可伪造需宿主签名 token（宿主侧配合
# 能力，本引擎不承接）。本门只保证「引擎能机判的那部分」不再是空话。
_REVIEW_LINEAGE_KIND = SESSION_KINDS[1]  # "reviewer"（单一真源，不另立词表）


def _review_lineage_error(
    reason: str,
    *,
    task_id: str,
    stage: str,
    lineage: dict[str, str],
    expected_parent_session_id: str | None,
    hint: str,
    missing_evidence: str | None = None,
) -> OrchdError:
    """构造 lineage 硬门的结构化 E016（claim 与 submit 共用同一 details 形态）。"""
    missing = [key for key, value in lineage.items() if not value]
    details: dict[str, Any] = {
        "task_id": task_id,
        "reason": reason,
        "stage": stage,
        "kind": lineage.get("kind", ""),
        "parent_session_id": lineage.get("parent_session_id", ""),
        "expected_parent_session_id": expected_parent_session_id or "",
        "missing": missing,
        "hint": hint,
    }
    if missing_evidence:
        details["missing_evidence"] = missing_evidence
    return OrchdError(
        ErrorCode.E016,
        f"review_lineage_gate[{reason}]: 审查会话 lineage 未通过硬门（stage={stage}）",
        [details],
    )


def require_review_lineage(
    orchd_dir: Path,
    *,
    session_id: str | None,
    expected_parent_session_id: str | None,
    task_id: str,
    stage: str = "claim",
) -> dict[str, str]:
    """审查 lineage 硬门：``kind == reviewer`` 且 ``parent == DONE.session_id``。

    单一判定实现（claim 认领与 review 提交共用本函数，禁双写判据）。调用点：
    ``orchd/onboard/claim/execute.py::_validate_claim_state``（reviewer 分支，
    认领路径，precheck 与锁内复核都会经过）与
    ``orchd/review/precheck.py::_review_submit_precheck``（提交路径，防「认领后
    篡改 runtime / 旧数据残留」）。

    判定顺序（与 AC 的 reason 一一对应）：

    1. 本会话没有引擎身份（``session_id`` 空）→ **不判定**、原样返回空 lineage。
       CLI 写命令在更早的 ``require_session_runtime`` 已 E033，直连 API 的离线
       调用（单元测试 / 无会话批处理）不属「伪造 lineage」而是「没有会话概念」，
       在此阻断只会把 SDK 用法一并打死。
    2. 声明缺失（kind 或 parent 为空串）→ ``lineage_missing``。
    3. kind 非 reviewer → ``kind_not_reviewer``。
    4. 实现侧 DONE 事件没有 ``session_id``（旧账本 / 无会话完成）→ 无法印证，
       仍按 ``lineage_missing`` 阻断并标 ``missing_evidence=done_session_id``
       （fail-closed：印证不了就不放行）。
    5. parent ≠ DONE.session_id → ``lineage_mismatch``。

    Args:
        orchd_dir: ``.orchd`` 目录（runtime 定位根）。
        session_id: 当前命令解析出的会话 session_id（空 = 无引擎身份）。
        expected_parent_session_id: 该任务最后一次 DONE 事件的 ``session_id``。
        task_id: 任务 id（写进 details 便于检索）。
        stage: ``claim`` / ``submit``（区分双检站点）。

    Returns:
        当前会话 lineage 两字段（通过时；无会话身份时为空串两字段）。

    Raises:
        OrchdError E016：lineage 缺失 / kind 非 reviewer / parent 不符。
        details[0].reason ∈ {lineage_missing, kind_not_reviewer, lineage_mismatch}。
    """
    lineage = session_lineage(orchd_dir)
    if not session_id:
        return lineage
    hint = (
        "审查必须由实现会话主动 spawn 的独立 subagent 执行：done 成功响应已含 "
        "review_handoff（引擎 mint 的 kind=reviewer、parent=该任务最后一次 DONE 的 "
        "session_id 的审查身份，以及可直接交给 subagent 的 prompt）；把 prompt 原样交给"
        "该子代理，由它在交接身份下 claim/submit（.orchd/rules/review.md「审查者身份约定」）"
    )
    missing = [key for key, value in lineage.items() if not value]
    if missing:
        raise _review_lineage_error(
            "lineage_missing", task_id=task_id, stage=stage, lineage=lineage,
            expected_parent_session_id=expected_parent_session_id,
            hint=hint,
        )
    if lineage["kind"] != _REVIEW_LINEAGE_KIND:
        raise _review_lineage_error(
            "kind_not_reviewer", task_id=task_id, stage=stage, lineage=lineage,
            expected_parent_session_id=expected_parent_session_id,
            hint="本会话 lineage kind=" + lineage["kind"] + "；审查会话必须使用 "
                 "done 响应 review_handoff 交接的 kind=reviewer 身份",
        )
    expected = (expected_parent_session_id or "").strip()
    if not expected:
        raise _review_lineage_error(
            "lineage_missing", task_id=task_id, stage=stage, lineage=lineage,
            expected_parent_session_id=expected_parent_session_id,
            missing_evidence="done_session_id",
            hint="实现侧最后一次 DONE 事件没有记录 session_id（旧账本 / 无会话完成），"
                 "parent 无从印证；请由带引擎会话的实现者重新 done 后重试",
        )
    if lineage["parent_session_id"] != expected:
        raise _review_lineage_error(
            "lineage_mismatch", task_id=task_id, stage=stage, lineage=lineage,
            expected_parent_session_id=expected,
            hint="parent_session_id 必须逐字等于该任务最后一次 DONE 事件的 session_id"
                 "（实现会话身份）；不等即不是该实现会话 spawn 的审查者",
        )
    return lineage


def active_claims_for_session(orchd_dir: Path, session_id: str) -> list[dict[str, str]]:
    """当前会话名下的活跃认领清单（session end 守卫的判定面）。

    判定（task-cont-session-adopt-end-guard）：实现认领 = 任务 ``status ==
    "claimed"`` 且 ``claimed_session`` 等于本会话 session_id；审查认领 =
    ``status == "in_review"`` 且 ``review_claimed_session`` 等于本会话。
    done/in_review 未领审 / 终态均不算活跃认领（实现侧 claim 在 DONE 时已释放）。
    账本不可读（Store 构造 / replay 异常）时返回空清单——end 守卫退化为
    既有行为（脏工作区闸仍在），不因查询失败阻断正常结束。

    Returns:
        ``[{"task_id": ..., "status": ..., "phase": "implement"|"review"}]``；
        审查认领额外带 ``review_phase``（``"spec"`` / ``"code"``，unified 单阶段
        缺省为 ``"unified"``），
        按 task_id 排序。
    """
    from orchd.ledger.store import Store

    if not session_id:
        return []
    try:
        states = Store(Path(orchd_dir)).replay()
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for task_id, ts in sorted(states.items()):
        if (
            getattr(ts, "status", None) == "claimed"
            and getattr(ts, "claimed_session", None) == session_id
        ):
            out.append({"task_id": task_id, "status": "claimed",
                        "phase": "implement"})
        elif (
            getattr(ts, "status", None) == "in_review"
            and getattr(ts, "review_claimed_session", None) == session_id
        ):
            out.append({"task_id": task_id, "status": "in_review",
                        "phase": "review",
                        "review_phase": getattr(ts, "review_phase", None)
                        or "unified"})
    return out


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
    data = require_session_runtime(orchd_dir)
    # 以 env 派生的 session_id 定位（runtime 文件若被手改成缺 session_id 的
    # 坏形态，仍写回原文件而不是拼出 sessions/None.json）。
    identity = resolve_session_identity(orchd_dir)
    session_id = identity["session_id"]
    # task-residuals-session-current-malformed：畸形 runtime 可能缺 session_id /
    # fingerprint；响应与回写都用 env 派生值补齐，避免「成功但缺字段」。
    data.setdefault("session_id", session_id)
    data.setdefault("fingerprint", identity["fingerprint"])
    path = _session_runtime_path(orchd_dir, session_id)
    now = datetime.now(timezone.utc).isoformat()
    data["last_seen"] = now
    # lineage 回显稳定化：本卡之前开的会话（或手写 runtime）没有这两个键，
    # 补齐空串后回显面恒为两键，消费方不必区分「缺键」与「声明为空」。
    data.setdefault("kind", "")
    data.setdefault("parent_session_id", "")
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
