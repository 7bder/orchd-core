"""Orchd 任务生命周期管理 - control 域。

迁移自 orchd/onboard.py（task-split-onboard-control-config）：
  - retract: 撤回事件（级联）
  - _task_completed_epoch: 扫描 ledger 求任务最近一次进入 completed 状态的 epoch 秒
  - _validate_revive_evidence: 严格校验复活证据 commit
  - force_status: 强制设置任务状态
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from orchd.constants import ORCHD_CLAIM_STALE_SECS_ENV, ORCHD_REVIEW_STALE_SECS_ENV
from orchd.contracts.events import (
    EVENT_CLAIMED,
    EVENT_FORCE_STATUS,
    EVENT_RETRACT,
    EVENT_REVIEW_CLAIMED,
    EVENT_REVIEW_SUBMITTED,
    FORCE_TARGET_STATUSES,
    STATUS_CANCELLED,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_DONE,
    STATUS_IN_REVIEW,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import get_default_branch as _get_default_branch, hook_uninstall
from orchd.worktree import (
    load_bindings,
    resolve_master_path_from_dir,
    unbind_task_wt,
)
from orchd.gitops_ops import make_event as _make_event
from orchd.ledger import (
    Store,
    TaskState,
    resolve_store_dir,
    review_claim_age_s,
    review_stale_timeout_s,
    session_ttl_minutes,
)
from orchd.contracts.responses import (
    BRANCH_KEY,
    COMMAND_KEY,
    ERROR_KEY,
    EVENT_ID_KEY,
    GUIDANCE_KEY,
    HINT_KEY,
    SESSION_ID_KEY,
    STATUS_KEY,
    TASK_ID_KEY,
    WARNINGS_KEY,
)

# force-status 合法目标：force_status() 只允许将任务强制设置到这四种状态，
# 其余状态（如 in_review、done）由正常事件流驱动，不可被强制跳转。
# 枚举本身在 contracts.events.FORCE_TARGET_STATUSES（单一真源，与 replay 守卫、
# sync 入站拒收共用）；此处保留旧名别名，不改调用点。
_FORCE_TARGETS = FORCE_TARGET_STATUSES


# _is_fingerprint_agent_id 单一事实源已上移 ledger.is_fingerprint_agent_id
# （task-fp-identity-single-source），见顶部 import 区（导入为旧名别名保持调用点不变）。

# M-3 逃生口（2026-08-12 全面审计）：默认跳转矩阵 _ALLOWED_FROM 外的两条
# "合理但不鼓励"的跳转，仅当调用方显式 force=True（CLI --force 二次确认）时放行。
# 格式为 (target_status, current_status)，与 _ALLOWED_FROM 的 key 语义一致。
# - ("completed", "claimed")：claimed → completed（实现者弃坑但功能已完成，善后）
# - ("pending", "cancelled")：cancelled → pending（误取消后复活）
# 其余默认非法跳转（如跳过审查的 pending→completed、终态互转 completed→cancelled）
# 不在此白名单内，--force 对它们无效，防止逃生口被用来绕过正常审查/终态保护。
_FORCE_ESCAPE_HATCHES = frozenset({
    (STATUS_COMPLETED, STATUS_CLAIMED),
    (STATUS_PENDING, STATUS_CANCELLED),
})

# verify_command 超时（秒）：120s 是在"给测试套件足够执行时间"与
# "避免无限期阻塞 done 流程"之间的折中值；大部分单测 / lint 命令在 2 分钟内可完成。
_VERIFY_TIMEOUT = 120
# 方案 C（task-full-regression-gate）：全量 pytest 冒烟超时上限（秒）。
# 全量套件不含慢用例时约 35s，预留足够余量；失败仅告警不阻断 done。
_FULL_REGRESSION_TIMEOUT = 300
# retract 认领冷却期（秒）：任务被 retract 后默认 300s 内拒绝重新认领，
# 区分「用户主动放弃」与「临时撤回」；--force 可绕过（task-retract-bind-cooloff）。
_RETRACT_COOLDOWN_S = 300

# 实现认领超时接管（W-2 对称通道，task-takeover-residue-alignment）：
# 跨 agent 撤认他人 CLAIMED 仅在目标认领已超时（stale，押注作者/会话失联）时
# 放行——与 REVIEW_CLAIMED 的 review_stale_timeout_s 对称，使 rules/session.md
# 接管 SOP 第4步（接管方会话 retract 原 CLAIMED）可达（此前只能走 force-status）。
# 默认 10 分钟：实现 session 失联判定窗口（与 REVIEW_CLAIMED 的 300s 对称）；
# 未超时仍报 E034，防借撤认抢活或绕过独立审查。env 覆盖仅供测试。
_CLAIM_STALE_DEFAULT_S = 600


def claim_stale_timeout_s() -> float:
    """返回实现认领超时秒数；环境变量 ``ORCHD_CLAIM_STALE_SECS`` 可覆盖（测试用）。

    与 ``orchd.ledger.review_stale_timeout_s`` 同风格：非法 / 非正值回退默认，
    解析失败静默回退（不因环境变量误设而放宽守卫）。
    """
    env = os.environ.get(ORCHD_CLAIM_STALE_SECS_ENV)
    if env is not None:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    return _CLAIM_STALE_DEFAULT_S


# ---- stale 判定单一时钟源（task-engine-defect-fix-unified-u10） ----------------
# 背景：此前 claim 超龄（600s）、review 超龄（300s）、session TTL（1440min）、
# watchdog stuck（timeout_min）四套时钟分散在 onboard.control / ledger /
# report.watchdog 三处各自内联，status 与 E034 语义互斥（status 看不到 claim_age，
# E034 却按同一阈值放行）。本块是**唯一**判定点：理由（reason）+ 时钟（clock）+
# 阈值（threshold_s）同源产出，watchdog / status / retract 一律消费，禁止就地重算。
STALE_CLOCK_CLAIM_AGE = "claim_stale_timeout_s"
STALE_CLOCK_REVIEW_AGE = "review_stale_timeout_s"
STALE_CLOCK_SESSION = "session_ttl_minutes"
STALE_CLOCK_STUCK = "watchdog_timeout_min"

STALE_REASON_CLAIM_AGE = "claim_age"
STALE_REASON_REVIEW_AGE = "review_age"
STALE_REASON_MISSING_RUNTIME = "missing_runtime"
STALE_REASON_INACTIVE = "inactive"
STALE_REASON_SESSION_EXPIRED = "session_expired"
STALE_REASON_STUCK_CLAIM = "stuck_claim"
STALE_REASON_STUCK_REVIEW = "stuck_review"

STALE_SESSION_ACTIVE = "active"
STALE_REASON_UNKNOWN = "unknown"

STALE_SESSION_REASONS = frozenset({
    STALE_REASON_MISSING_RUNTIME,
    STALE_REASON_INACTIVE,
    STALE_REASON_SESSION_EXPIRED,
})


def stale_clock_thresholds(timeout_min: float | None = None) -> dict[str, float | None]:
    """返回各 stale 时钟的阈值（秒）；``stuck`` 时钟仅在给定 ``timeout_min`` 时存在。"""
    return {
        STALE_CLOCK_CLAIM_AGE: claim_stale_timeout_s(),
        STALE_CLOCK_REVIEW_AGE: review_stale_timeout_s(),
        STALE_CLOCK_SESSION: session_ttl_minutes() * 60.0,
        STALE_CLOCK_STUCK: None if timeout_min is None else float(timeout_min) * 60.0,
    }


def classify_stale_claim(
    *,
    role: str,
    status: str,
    age_s: float | None = None,
    session_state: str | None = None,
    timeout_min: float | None = None,
) -> list[dict[str, Any]]:
    """单一 stale 判定点：返回该认领命中的**全部**理由（``[]`` 表示不 stale）。

    每个理由自带 ``clock`` 与 ``threshold_s``，阈值统一来自
    :func:`stale_clock_thresholds`——消费点（watchdog / status / E034 retract）
    只读结果，不再各自维护阈值或年龄判定（task-engine-defect-fix-unified-u10）。

    命中顺序（同时可能存在多个）：
    1. session 失联（missing_runtime / inactive / session_expired）；
    2. implementer 认领超龄（claim_age）或 reviewer 审查超龄（review_age）；
       ``session_state=active`` 时抑制本项——长实现无引擎写操作不等于僵尸；
    3. watchdog 的 ``timeout_min`` 级僵死（stuck_claim / stuck_review）——
       与 2 相互独立：同一认领可以既超龄又僵死（旧 watchdog 语义要求超过
       ``timeout_min`` 必进 stuck 列表），故这里不互相短路。
    """
    decisions: list[dict[str, Any]] = []
    rounded_age = None if age_s is None else round(float(age_s), 1)
    if session_state in STALE_SESSION_REASONS:
        decisions.append({
            "reason": session_state,
            "clock": STALE_CLOCK_SESSION,
            "threshold_s": (
                session_ttl_minutes() * 60.0
                if session_state == STALE_REASON_SESSION_EXPIRED
                else None
            ),
            "age_s": rounded_age,
        })
    if age_s is None:
        return decisions
    thresholds = stale_clock_thresholds(timeout_min)
    if session_state != STALE_SESSION_ACTIVE:
        claim_threshold = thresholds[STALE_CLOCK_CLAIM_AGE]
        review_threshold = thresholds[STALE_CLOCK_REVIEW_AGE]
        if (
            role == "implementer"
            and status == STATUS_CLAIMED
            and claim_threshold is not None
            and float(age_s) >= float(claim_threshold)
        ):
            decisions.append({
                "reason": STALE_REASON_CLAIM_AGE,
                "clock": STALE_CLOCK_CLAIM_AGE,
                "threshold_s": claim_threshold,
                "age_s": rounded_age,
            })
        if (
            role == "reviewer"
            and status == STATUS_IN_REVIEW
            and review_threshold is not None
            and float(age_s) >= float(review_threshold)
        ):
            decisions.append({
                "reason": STALE_REASON_REVIEW_AGE,
                "clock": STALE_CLOCK_REVIEW_AGE,
                "threshold_s": review_threshold,
                "age_s": rounded_age,
            })
    stuck_threshold = thresholds[STALE_CLOCK_STUCK]
    if stuck_threshold is not None and float(age_s) > float(stuck_threshold):
        decisions.append({
            "reason": (
                STALE_REASON_STUCK_CLAIM
                if role == "implementer"
                else STALE_REASON_STUCK_REVIEW
            ),
            "clock": STALE_CLOCK_STUCK,
            "threshold_s": stuck_threshold,
            "age_s": rounded_age,
        })
    return decisions


def stale_claim_snapshot(
    store: Store,
    state: dict[str, TaskState] | None = None,
    *,
    timeout_min: float | None = 60.0,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """统一 stale 快照：``{"sessions": [...], "claims": [...], "stuck": [...]}``。

    watchdog / status 共用本函数（E034 的 retract 走 :func:`classify_stale_claim`
    同一判定）。session runtime 不可读时 best-effort 降级为空（不影响既有行为）。
    """
    import json

    from orchd.ledger import (
        is_session_expired,
        session_runtime_dir,
        session_ttl_minutes,
    )

    result: dict[str, list[dict[str, Any]]] = {"sessions": [], "claims": [], "stuck": []}
    state = state if state is not None else store.replay()

    # 事件时间戳：最接近当前的 CLAIMED / REVIEW_CLAIMED（账本同源）
    claim_info: dict[str, dict[str, Any]] = {}
    for ev in store._read_ledger_lines(from_line=1):
        tid = ev.get(TASK_ID_KEY, "")
        ts_str = ev.get("timestamp", "")
        if not tid or not ts_str:
            continue
        if ev.get("type") == EVENT_CLAIMED:
            claim_info.setdefault(tid, {})["claim"] = {
                "timestamp": ts_str,
                EVENT_ID_KEY: ev.get(EVENT_ID_KEY, ""),
            }
        elif ev.get("type") == EVENT_REVIEW_CLAIMED:
            claim_info.setdefault(tid, {})["review"] = {
                "timestamp": ts_str,
                EVENT_ID_KEY: ev.get(EVENT_ID_KEY, ""),
            }

    sessions_by_id: dict[str, dict[str, Any]] = {}
    unknown_sessions: set[str] = set()
    try:
        session_dir = session_runtime_dir(store.orchd_dir)
        if session_dir.exists():
            for f in sorted(session_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    unknown_sessions.add(f.stem)
                    continue
                if not isinstance(data, dict):
                    unknown_sessions.add(f.stem)
                    continue
                sid = data.get(SESSION_ID_KEY)
                if sid:
                    sessions_by_id[sid] = data
                else:
                    unknown_sessions.add(f.stem)
    except Exception:
        # best-effort：session runtime 目录不可用时视为「无 session 信息」，
        # 仍按认领年龄给出 claim_age / review_age（不因基础设施降级而丢失判定）。
        sessions_by_id = {}
        unknown_sessions = set()

    for tid, ts in sorted(state.items()):
        if ts.status not in (STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW):
            continue
        info = claim_info.get(tid, {})
        for role, sid, owner in (
            ("implementer", ts.claimed_session, ts.claimed_by),
            ("reviewer", ts.review_claimed_session, ts.review_claimed_by),
        ):
            if not sid:
                continue
            runtime = sessions_by_id.get(sid)
            session_state = None
            if runtime is None:
                session_state = (
                    STALE_REASON_UNKNOWN
                    if sid in unknown_sessions
                    else STALE_REASON_MISSING_RUNTIME
                )
            elif not runtime.get("active", True):
                session_state = STALE_REASON_INACTIVE
            elif is_session_expired(runtime):
                session_state = STALE_REASON_SESSION_EXPIRED
            else:
                session_state = STALE_SESSION_ACTIVE

            age_s: float | None = None
            event_id = ""
            if role == "implementer" and ts.status == STATUS_CLAIMED:
                claim_ev = info.get("claim") or {}
                age_s = review_claim_age_s(claim_ev.get("timestamp"), now.isoformat() if now else None)
                event_id = claim_ev.get(EVENT_ID_KEY, "")
            elif role == "reviewer" and ts.status == STATUS_IN_REVIEW:
                review_ev = info.get("review") or {}
                claim_ts = ts.review_claimed_at or review_ev.get("timestamp")
                age_s = review_claim_age_s(claim_ts, now.isoformat() if now else None)
                event_id = review_ev.get(EVENT_ID_KEY, "")

            # 认领侧（session 失联 + 超龄）；stuck 由下方独立循环从账本事件计算
            # （不依赖 session runtime），两者共用同一 classify_stale_claim。
            for decision in classify_stale_claim(
                role=role,
                status=ts.status,
                age_s=age_s,
                session_state=session_state,
            ):
                reason = decision["reason"]
                if reason in STALE_SESSION_REASONS:
                    result["sessions"].append({
                        SESSION_ID_KEY: sid,
                        TASK_ID_KEY: tid,
                        "role": role,
                        "owner": owner,
                        "reason": reason,
                    })
                entry = {
                    TASK_ID_KEY: tid,
                    "role": role,
                    "owner": owner,
                    SESSION_ID_KEY: sid,
                    "reason": reason,
                    "clock": decision["clock"],
                    "threshold_s": decision["threshold_s"],
                    "age_s": decision["age_s"],
                }
                if event_id:
                    entry[EVENT_ID_KEY] = event_id
                result["claims"].append(entry)

    # stuck（watchdog 的 timeout_min 级）：只看账本时间戳与任务状态，不要求
    # session runtime（与旧 watchdog 语义一致——会话运行时不可读也要能报僵死）。
    for tid, ts in sorted(state.items()):
        info = claim_info.get(tid, {})
        if ts.status == STATUS_CLAIMED:
            role = "implementer"
            event = info.get("claim") or {}
            claim_ts = event.get("timestamp")
        elif ts.status == STATUS_IN_REVIEW and ts.review_claimed_by:
            role = "reviewer"
            event = info.get("review") or {}
            claim_ts = ts.review_claimed_at or event.get("timestamp")
        else:
            continue
        age_s = review_claim_age_s(claim_ts, now.isoformat() if now else None)
        for decision in classify_stale_claim(
            role=role,
            status=ts.status,
            age_s=age_s,
            timeout_min=timeout_min,
        ):
            if decision["reason"] not in (
                STALE_REASON_STUCK_CLAIM,
                STALE_REASON_STUCK_REVIEW,
            ):
                continue
            entry = {
                TASK_ID_KEY: tid,
                "role": role,
                "clock": decision["clock"],
                "threshold_s": decision["threshold_s"],
                "age_s": decision["age_s"],
            }
            if event.get(EVENT_ID_KEY):
                entry[EVENT_ID_KEY] = event[EVENT_ID_KEY]
            result["stuck"].append(entry)
    return result


def _session_runtime_state(store: Store, session_id: str | None) -> str | None:
    """解析事件 session_id 的 runtime 状态；None 表示无身份（按年龄兜底）。"""
    if not session_id:
        return None
    import json

    from orchd.ledger import is_session_expired, session_runtime_dir

    path = session_runtime_dir(store.orchd_dir) / f"{session_id}.json"
    if not path.exists():
        return STALE_REASON_MISSING_RUNTIME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return STALE_REASON_UNKNOWN
    if not isinstance(data, dict):
        return STALE_REASON_UNKNOWN
    if not data.get("active", True):
        return STALE_REASON_INACTIVE
    if is_session_expired(data):
        return STALE_REASON_SESSION_EXPIRED
    return STALE_SESSION_ACTIVE


def retract(
    store: Store,
    agent_id: str,
    target_event_id: str | None = None,
    reason: str = "",
    project_root: Path | None = None,
    *,
    task_id: str | None = None,
    event_type: str | None = None,
    disposition: str = "abandon",
) -> dict[str, Any]:
    """撤回事件（级联）。

    支持两种定位方式：
    - ``target_event_id``：按事件 ID 精确撤回（向后兼容）；
    - ``task_id`` + ``event_type``：自动定位该任务最近一条匹配类型的事件（P0-10）。

    找到目标事件后，自动撤回该事件及其后同 task_id 的所有后续事件（级联撤回）。
    注意：FORCE_STATUS 事件不可撤回——它是管理员强制操作，具有不可逆语义，
    若需修正应再次调用 force_status 而非 retract。

    disposition（task-retract-bind-cooloff，加法式）：
      - abandon（默认）：用户主动放弃，触发认领冷却期 + 级联解绑 worktree
      - retry：临时撤回后重试，不触发冷却（仍解绑，因为 worktree 需重建）
      - handoff：移交给他人，不触发冷却（仍解绑）
    旧事件无 disposition 字段时 replay 兼容视为 abandon。
    """
    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        # 读取全部事件找 target
        all_events = store._read_ledger_lines(from_line=1)

        # P0-10：task_id + event_type 自动定位最近匹配事件
        if target_event_id is None and task_id and event_type:
            target_event = None
            for ev in reversed(all_events):
                if (ev.get("task_id") == task_id
                        and ev.get("type") == event_type
                        and not ev.get("retracted")):
                    target_event = ev
                    target_event_id = ev.get("event_id")
                    break
            if target_event is None:
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: no active {event_type} event found for task '{task_id}'",
                    [{TASK_ID_KEY: task_id, "event_type": event_type,
                      HINT_KEY: "确认任务 ID 和事件类型是否正确；可用 python .orchd/__main__.py status 查看事件历史"}],
                )
        elif target_event_id is None:
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: retract requires --event <id> or --task <id> --type <type>",
                [{HINT_KEY: "请提供事件 ID 或任务 ID + 事件类型"}],
            )

        target_event = None
        for ev in all_events:
            if ev.get("event_id") == target_event_id:
                target_event = ev
                break

        if target_event is None:
            raise OrchdError(
                ErrorCode.E007,
                f"invalid_state: event '{target_event_id}' not found",
                [{EVENT_ID_KEY: target_event_id}],
            )
        if target_event.get("type") == EVENT_FORCE_STATUS:
            raise OrchdError(
                ErrorCode.E007,
                "invalid_state: cannot retract FORCE_STATUS event",
                [{EVENT_ID_KEY: target_event_id, "type": EVENT_FORCE_STATUS}],
            )

        # E034: 撤认归属守卫（task-retract-ownership-guard）。
        # 仅允许事件作者本人或 admin 撤回；跨 agent 撤认他人事件走超时接管通道
        # （见下）或 force-status/admin 控制面，防止实现者撤认独立审查者的
        # REVIEW_CLAIMED 以绕过独立审查（2026-08-24 自审+撤认绕过事件后的引擎层修复）。
        # 注：admin 控制面语义为有意保留（test_admin_cross_retract_allowed 断言）；
        # 其「无认证」属 P2-11，根治需 1.6 Registry 认证机制，暂不在此移除。
        target_author = target_event.get("agent_id")
        if target_author is not None and target_author != agent_id and agent_id != "admin":
            # W-2 僵尸认领接管（对称通道）：跨 agent 撤认他人 CLAIMED /
            # REVIEW_CLAIMED 仅在目标认领已超时（stale，押注作者/会话失联）时
            # 放行——这正是「接管僵死任务 / 僵死审查」的必要路径；未超时仍受
            # E034 保护，防借撤认抢活或绕过独立审查。
            # 统一时钟源（task-engine-defect-fix-unified-u10）：超时接管判定与
            # watchdog / status 的 stale 判定共用 classify_stale_claim，禁止此处
            # 另写 claim/review 阈值（两处时钟漂移正是本卡要消除的缺陷）。
            # 事件类型严格白名单（审查返工）：超时接管通道只覆盖本守卫历史覆盖的
            # 两类「认领事件」——CLAIMED（实现认领）与 REVIEW_CLAIMED（审查认领）。
            # DONE / REVIEW_READY / REVIEW_SUBMITTED 等其余事件从未被本守卫放行，
            # 若也按 claim_age 放行，跨 agent 即可撤掉他人已完成/已批准事件
            # （2026-11-02 审查实测：DONE 与 REVIEW_SUBMITTED(APPROVED) 被误放行）。
            _age = review_claim_age_s(target_event.get("timestamp"))
            _evt_type = target_event.get("type")
            if _evt_type == EVENT_REVIEW_CLAIMED:
                _stale_role = "reviewer"
                _stale_status = STATUS_IN_REVIEW
            elif _evt_type == EVENT_CLAIMED:
                _stale_role = "implementer"
                _stale_status = STATUS_CLAIMED
            else:
                _stale_role = None
                _stale_status = None
            stale_release = False
            _runtime_state = _session_runtime_state(
                store, target_event.get(SESSION_ID_KEY)
            )
            if _stale_role is not None and _stale_status is not None:
                if _runtime_state == STALE_SESSION_ACTIVE:
                    stale_release = False
                elif _runtime_state in (
                    STALE_REASON_MISSING_RUNTIME,
                    STALE_REASON_UNKNOWN,
                    None,
                ):
                    stale_release = bool(classify_stale_claim(
                        role=_stale_role, status=_stale_status, age_s=_age
                    ))
                else:
                    stale_release = True
            if not stale_release:
                _hint = (
                    "跨 agent 撤认需事件作者本人或 admin 操作，或目标为超时（僵尸）"
                    f"认领——CLAIMED 超时阈值 {claim_stale_timeout_s():.0f}s"
                    f"（{ORCHD_CLAIM_STALE_SECS_ENV} 可覆盖）、REVIEW_CLAIMED 超时阈值 "
                    f"{review_stale_timeout_s():.0f}s（{ORCHD_REVIEW_STALE_SECS_ENV} 可覆盖），"
                    f"未超时仍受本守卫保护；如确须纠正，请事件作者撤回或管理员 force-status"
                )
                if _runtime_state == STALE_SESSION_ACTIVE:
                    _hint += (
                        "；目标会话 runtime 仍为 active：长实现无引擎写操作不等于僵尸，"
                        "请等待其退出或由事件作者撤回"
                    )
                _evt_task_id = str(target_event.get("task_id") or "")
                _current_status = "unknown"
                try:
                    _ts = store.replay().get(_evt_task_id)
                    if _ts is not None:
                        _current_status = _ts.status
                except Exception:
                    pass
                _force_cmd = (
                    f"python .orchd/__main__.py force-status --task {_evt_task_id} "
                    f'--status pending --reason "retract {target_event_id}: '
                    '请替换为实际原因"'
                )
                raise OrchdError(
                    ErrorCode.E034,
                    f"retract_not_authorized: event '{target_event_id}' owned by "
                    f"'{target_author}', caller '{agent_id}' cannot retract",
                    [{EVENT_ID_KEY: target_event_id, TASK_ID_KEY: _evt_task_id,
                      "owner": target_author, "caller": agent_id,
                      STATUS_KEY: _current_status, COMMAND_KEY: _force_cmd,
                      HINT_KEY: _hint}],
                )

        # 找级联事件（同 task_id，在 target 之后的事件）
        task_id = target_event.get("task_id", "")
        assert task_id is not None
        target_idx = next(i for i, ev in enumerate(all_events)
                         if ev.get("event_id") == target_event_id)
        cascade_ids = [target_event_id]
        for ev in all_events[target_idx + 1:]:
            if ev.get("task_id") == task_id and ev.get("type") not in (EVENT_RETRACT, EVENT_FORCE_STATUS):
                cascade_ids.append(ev.get("event_id"))

        # 逐条写 RETRACT
        retracted_events = []
        for eid in cascade_ids:
            retract_ev = _make_event(
                task_id, agent_id, EVENT_RETRACT,
                target_event_id=eid,
                reason=reason,
                disposition=disposition,
            )
            store.append_event(retract_ev)
            retracted_events.append(eid)

        new_state = store.replay()
        store.update_checkpoint(new_state)

        # task-retract-bind-cooloff：级联解绑 worktree 绑定。
        # 撤回 CLAIMED/REVIEW_CLAIMED 时，若任务在 session-worktrees.json 有绑定
        # → unbind_task_wt（幂等，无绑定也返回 unbound=True）。
        # 归属守卫由 retract 入口的 E034 保证（仅事件作者本人或 admin 可撤回），
        # 此处不再重复检查绑定归属。
        unbind_result = None
        if target_event.get("type") in (EVENT_CLAIMED, EVENT_REVIEW_CLAIMED):
            try:
                bindings = load_bindings(resolve_store_dir(store.orchd_dir))
                if task_id in bindings:
                    unbind_result = unbind_task_wt(
                        resolve_store_dir(store.orchd_dir), task_id, lock_held=True,
                    )
                    unbind_result["recycle_log"] = (
                        f"retract cascade unbind: task={task_id} "
                        f"disposition={disposition} by {agent_id}"
                    )
                else:
                    unbind_result = {"unbound": True, TASK_ID_KEY: task_id, "reason": "no_binding"}
            except Exception as e:
                unbind_result = {"unbound": False, TASK_ID_KEY: task_id, ERROR_KEY: str(e)}
    finally:
        store.release_lock()

    # L3 pre-commit hook 卸载（best-effort，锁外）
    # 传 orchd_dir：其他任务仍持活跃认领时保留共享 hook（task-debug-hook-active-gate）
    if project_root:
        hook_uninstall(project_root, orchd_dir=store.orchd_dir)

    # 生命周期终态收敛（task-pass9-lifecycle-release-invariant）：retract 属放弃
    # 路径——先写事件后尽力恢复，切换失败降级为 stranded 明细不阻断（放弃正是
    # 解困动作，不得被 git 故障堵死）；历史滞留由读侧自愈兜底消化。
    release_info: dict[str, Any] | None = None
    if project_root:
        from orchd.gitops.guard import release_task_lifecycle

        release_info = release_task_lifecycle(
            store, project_root,
            task_id=task_id, agent_id=agent_id,
            command="retract", mode="abandon",
        )

    result = {
        "retracted": True,
        "retracted_events": retracted_events,
        TASK_ID_KEY: task_id,
        "new_status": new_state.get(task_id, TaskState()).status,
        "disposition": disposition,
    }
    if release_info is not None:
        result["checked_out_main"] = release_info.get("checked_out_main")
        if release_info.get("stranded"):
            result["stranded"] = release_info["stranded"]
    if unbind_result is not None:
        result["unbind"] = unbind_result
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result


# ------------------------------------------------------------------
# force_status（写操作，锁内）
# ------------------------------------------------------------------


def _task_completed_epoch(store: Store, task_id: str) -> float | None:
    """扫描 ledger 求任务最近一次进入 completed 状态的 epoch 秒；从未完成返回 None。

    与 replay 状态机同源（task-force-status-revive-guard）：
    - 先收集全部 RETRACT 的 ``target_event_id``，跳过被撤回事件；
    - 对 ``REVIEW_SUBMITTED``（code+APPROVED）与 ``FORCE_STATUS``（→completed）
      视为进入 completed，取最后一条的 ``timestamp``（ISO 8601 → epoch 秒）。
    timestamp 缺失或解析失败返回 None（调用方按"无法证明晚于完成"从严拒绝）。
    """
    if not store.ledger_exists():
        return None
    events = store._read_ledger_lines(from_line=1)
    retracted = store._collect_retracted_event_ids()
    completed_iso: str | None = None
    for ev in events:
        if ev.get("type") == EVENT_RETRACT or ev.get("event_id", "") in retracted:
            continue
        if ev.get("task_id") != task_id:
            continue
        etype = ev.get("type")
        if etype == EVENT_REVIEW_SUBMITTED:
            # review-unify-r2：unified 单阶段（无 review_type）与 code APPROVED
            # 均视为进入 completed 的终审。
            if ev.get("verdict") == "APPROVED" and (
                ev.get("review_type") == "code" or ev.get("review_type") is None
            ):
                completed_iso = ev.get("timestamp")
        elif etype == EVENT_FORCE_STATUS and ev.get("target_status") == STATUS_COMPLETED:
            completed_iso = ev.get("timestamp")
    if not completed_iso:
        return None
    try:
        return datetime.fromisoformat(completed_iso).timestamp()
    except (ValueError, TypeError):
        return None


def _validate_revive_evidence(
    project_root: Path | None,
    evidence_sha: str,
    completed_epoch: float | None,
    task_id: str,
) -> None:
    """严格校验复活证据 commit（task-force-status-revive-guard，completed→pending 门禁）。

    三项校验，任一不满足抛 E007 拒绝复活：
      1. git 仓库可用（``project_root`` 非空）。
      2. ``evidence_sha`` 指向真实 commit，且是默认分支（main/master）的祖先。
      3. 该 commit 的提交时间（epoch 秒）**晚于**任务最近一次完成时间。

    git 证据保证复活"事后有据可查"：commit 必须落在任务完成后且已进入主干，
    避免用任意/陈旧 sha 蒙混过关。``completed_epoch`` 无法确定时同样拒绝。
    """
    if project_root is None:
        raise OrchdError(
            ErrorCode.E007,
            f"revive_blocked: 复活 '{task_id}' 需 git 仓库做证据校验（project_root 缺失）",
            [{TASK_ID_KEY: task_id, "missing": "project_root"}],
        )

    def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    try:
        # 1. sha 必须是有效 commit（rev-parse --verify 失败返回非 0）
        verify = _git(["rev-parse", "--verify", "--quiet", f"{evidence_sha}^{{commit}}"])
        if verify.returncode != 0:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 证据 sha '{evidence_sha}' 不是有效 commit",
                [{TASK_ID_KEY: task_id, "evidence_sha": evidence_sha, "reason": "not_a_commit"}],
            )

        # 2. 必须是默认分支祖先
        branch = _get_default_branch(project_root)
        if branch is None:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 无法判定默认分支，证据 sha '{evidence_sha}' 校验中止",
                [{TASK_ID_KEY: task_id, "evidence_sha": evidence_sha, "reason": "no_default_branch"}],
            )
        anc = _git(["merge-base", "--is-ancestor", evidence_sha, branch])
        if anc.returncode != 0:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 证据 sha '{evidence_sha}' 不是 {branch} 的祖先",
                [{TASK_ID_KEY: task_id, "evidence_sha": evidence_sha, BRANCH_KEY: branch,
                  "reason": "not_ancestor"}],
            )

        # 3. 提交时间必须晚于任务最近一次完成时间（严格证据，无法证明则拒绝）
        if completed_epoch is None:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 无法确定任务完成时间，证据 sha '{evidence_sha}' 校验中止",
                [{TASK_ID_KEY: task_id, "evidence_sha": evidence_sha, "reason": "no_completion_time"}],
            )
        show = _git(["show", "-s", "--format=%ct", evidence_sha])
        if show.returncode != 0 or not show.stdout.strip():
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 无法读取证据 sha '{evidence_sha}' 提交时间",
                [{TASK_ID_KEY: task_id, "evidence_sha": evidence_sha, "reason": "no_commit_time"}],
            )
        commit_epoch = float(show.stdout.strip())
        if commit_epoch <= completed_epoch:
            raise OrchdError(
                ErrorCode.E007,
                f"revive_blocked: 复活 '{task_id}' 证据 sha '{evidence_sha}' 早于任务完成时间"
                "（须晚于最近一次 completed 时刻）",
                [{TASK_ID_KEY: task_id, "evidence_sha": evidence_sha,
                  "commit_epoch": commit_epoch, "completed_epoch": completed_epoch,
                  "reason": "before_completion"}],
            )
    except OrchdError:
        raise
    except (subprocess.SubprocessError, OSError):
        raise OrchdError(
            ErrorCode.E007,
            f"revive_blocked: 复活 '{task_id}' git 证据校验不可用（git 命令失败）",
            [{TASK_ID_KEY: task_id, "reason": "git_unavailable"}],
        ) from None


# ── 共享面复活门禁（task-shared-surface-admission，S3）───────────────────────
# completed→pending 复活前，若任务旧声明仍含 fragments / generated 面路径 → 拒绝
# 并给出精确 amend 命令（不静默归一化）。serial 面不阻断（仅串行化写入语义）。
_REVIVE_BLOCKING_SURFACE_MODES = frozenset({"fragments", "generated"})


def _normalize_surface_path(raw: Any) -> str:
    """声明路径归一化（与 surfaces 注册表口径一致：分隔符/前缀/casefold）。"""
    if not isinstance(raw, str):
        return ""
    text = raw.strip().replace("\\", "/").casefold()
    while text.startswith("./"):
        text = text[2:]
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip("/")


def _load_master_tasks(project_root: Path) -> list[dict[str, Any]] | None:
    """读项目 ``_master.json`` 的任务表；缺失/不可解析 → None。

    路径一律经 :func:`orchd.worktree.resolve_master_path_from_dir` 解析
    （task-master-path-resolver-convergence：本地优先 → canonical 主工作树回退），
    不再裸拼 ``<root>/.orchd/_master.json``——后者在任务 worktree 下会命中缺失副本
    而静默回落。
    """
    import json as _json

    master_path = resolve_master_path_from_dir(Path(project_root) / ".orchd")
    try:
        payload = _json.loads(master_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    tasks = payload.get("tasks")
    return tasks if isinstance(tasks, list) else None


def _revive_task_definition(
    project_root: Path | None,
    task_id: str,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """复活校验用任务定义：优先显式 ``tasks``；否则读**主工作树** master。

    code review 修复（阻断 2）：任务 worktree 下 ``project_root`` 是 worktree 根
    （无 ``.orchd/_master.json``），此前直接返回 None → 门禁静默跳过（fail-open），
    旧 CHANGELOG/生成物声明可被复活带回运行态。现先经
    :func:`orchd.worktree.resolve_canonical_project_root` 解析 canonical 主工作树再读
    master；**解析得到的主工作树缺 master 时 fail-closed**（E007），只有真正无 git /
    无主工作树的宿主仓（resolver 原地返回）才保持 fail-open（无共享面声明可比）。
    """
    if tasks is not None:
        for task in tasks:
            if isinstance(task, dict) and task.get("id") == task_id:
                return task
        return None
    if project_root is None:
        return None
    root = Path(project_root)
    canonical = root
    try:
        from orchd.worktree import resolve_canonical_project_root

        canonical = Path(resolve_canonical_project_root(root))
    except Exception:
        canonical = root
    candidates = [canonical] if canonical != root else []
    candidates.append(root)
    for candidate in candidates:
        task_list = _load_master_tasks(candidate)
        if task_list is None:
            continue
        for task in task_list:
            if isinstance(task, dict) and task.get("id") == task_id:
                return task
        return None
    if canonical != root:
        raise OrchdError(
            ErrorCode.E007,
            f"revive_blocked: 复活 '{task_id}' 无法校验旧共享面声明——"
            f"主工作树 {canonical} 缺少 .orchd/_master.json（任务 worktree 下"
            "不得 fail-open；无 git 的宿主仓才按无声明跳过）",
            [{
                TASK_ID_KEY: task_id,
                "reason": "revive_master_unavailable",
                "canonical_project_root": str(canonical),
                HINT_KEY: ("确认主工作树 master 存在（容器仓应在 main worktree 执行"
                           "force-status，或修复 master 后重试）"),
            }],
        )
    return None


def _revive_surface_blockers(
    task_def: dict[str, Any] | None, project_root: Path | None,
) -> list[dict[str, Any]]:
    """旧声明命中的 fragments/generated 面 → [{surface, mode, path, declared}]。"""
    if not task_def or project_root is None:
        return []
    from orchd import surfaces as _surfaces

    specs = _surfaces.load_surfaces(project_root)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # task-surface-refresh-channel：旧卡若把 generated 路径登记在
    # surface_refresh（受控刷新）而非写集里，复活门禁不得误拒——刷新声明本身
    # 就是合法通道，写集里没有该路径。
    refresh_keys = {
        _normalize_surface_path(item.get("path"))
        for item in (task_def.get(_surfaces.SURFACE_REFRESH_FIELD) or [])
        if isinstance(item, dict) and item.get("path")
    }
    for field in ("files_to_edit", "exempt_files"):
        values = task_def.get(field)
        if not isinstance(values, list):
            continue
        for raw in values:
            key = _normalize_surface_path(raw)
            if not key or key in seen or key in refresh_keys:
                continue
            for spec in specs.values():
                if getattr(spec, "mode", "") not in _REVIVE_BLOCKING_SURFACE_MODES:
                    continue
                surface_dir = getattr(spec, "dir", None)
                dir_key = _normalize_surface_path(surface_dir) if surface_dir else ""
                hit = key == _normalize_surface_path(getattr(spec, "path", ""))
                if not hit and dir_key:
                    hit = key == dir_key or key.startswith(dir_key + "/")
                if not hit:
                    continue
                seen.add(key)
                out.append({
                    "reason": "revive_surface_declaration",
                    "surface": spec.name,
                    "mode": spec.mode,
                    "path": spec.path,
                    "declared": str(raw),
                    "field": field,
                })
                break
    return out


def _revive_reject_surface_declarations(
    project_root: Path | None,
    task_id: str,
    tasks: list[dict[str, Any]] | None = None,
) -> None:
    """复活前拒绝旧 fragments/generated 声明：E007 + **可执行** amend 命令。

    code review 修复（阻断 1）：此前所有声明都提示 ``--remove-files-to-edit``，
    但终态卡删 files_to_edit 会被「删的须不存在」矩阵拦（死循环）。现按声明所在
    字段分流——files_to_edit 走 amend 的显式「终态共享面声明裁剪」通道（可执行），
    exempt_files 走终态 attachable 通道的 ``--remove-exempt-files``（同样可执行）。
    """
    blockers = _revive_surface_blockers(
        _revive_task_definition(project_root, task_id, tasks), project_root)
    if not blockers:
        return
    paths = sorted({str(b["declared"]) for b in blockers})
    cmds: list[str] = []
    for path in paths:
        fields = {
            str(b.get("field", "")) for b in blockers
            if str(b["declared"]) == path
        }
        flag = "--remove-exempt-files" if fields == {"exempt_files"} \
            else "--remove-files-to-edit"
        cmds.append(
            f"python .orchd/__main__.py amend --task {task_id} "
            f"{flag} {path} --reason \"<撤下旧共享面声明>\""
        )
    amend_cmd = "；".join(cmds)
    raise OrchdError(
        ErrorCode.E007,
        f"revive_blocked: 复活 '{task_id}' 的旧声明仍含共享面路径"
        f"（{'/'.join(str(b['surface']) for b in blockers)}），请先撤下声明再复活："
        f"{amend_cmd}（上述命令对 completed 卡可直接执行：files_to_edit 走 amend"
        "「终态共享面声明裁剪」通道、exempt_files 走终态 attachable 通道；"
        "禁止手改 .orchd/_master.json）",
        [{
            TASK_ID_KEY: task_id,
            "reason": "revive_surface_declaration",
            "declared": paths,
            "surfaces": blockers,
            "amend_commands": cmds,
            COMMAND_KEY: amend_cmd,
            HINT_KEY: ("先执行上述 amend 撤下旧声明，再重试复活；"
                       "completed 卡的声明撤回有引擎通道，不得手改 master"),
        }],
    )
def force_status(
    store: Store,
    agent_id: str,
    task_id: str,
    target_status: str,
    reason: str,
    assignee: str | None = None,
    force: bool = False,
    project_root: Path | None = None,
    evidence_sha: str | None = None,
    test_data: bool = False,
    force_recycle: bool = False,
    discard_uncommitted: bool = False,
    discard_confirmed: bool = False,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """强制设置任务状态。

    受两层约束：
      1. 目标状态必须在 _FORCE_TARGETS 集合内（pending / claimed / completed / cancelled）。
      2. "允许从"矩阵：每个目标状态仅允许从特定源状态跳转，
         例如 claimed 只能从 pending 强制，completed 只能从 in_review 强制，
         cancelled 可从 pending / claimed / done / in_review 强制。
    这避免了不合逻辑的状态跳转（如从 pending 直接强制到 completed 跳过审查）。

    逃生口（M-3，2026-08-12）：矩阵外的两条"合理但不鼓励"跳转——
    claimed→completed（弃坑但功能已完成）与 cancelled→pending（误取消复活）——
    仅当 ``force=True``（CLI ``--force`` 显式二次确认）时放行；否则仍抛 E007。
    ``force`` 对白名单之外的非法跳转不生效，防止绕过正常审查/终态保护。

        复活门禁（task-force-status-revive-guard，2026-08-24 事故修复）：completed→pending
        已**移出** ``_ALLOWED_FROM["pending"]`` 默认矩阵（不再无 --force 直接放行），
        纳入需 ``--force + --evidence-sha <commit>``（git 证据严格校验）的复活路径：
        sha 必须存在、是 main 祖先、提交时间晚于该任务完成时间——任一不满足 E007 拒绝。

        ``test_data``（task-p2-ledger-audit-noise）：测试注入的 FORCE_STATUS 事件
        标记 ``test_data=True``，使 ``revive_audit`` / ``status`` 的复活扫描能区分
        生产复活与测试污染（测试数据不产生审计告警）。仅作事件标记，不影响状态机
        流转与任何门禁逻辑；生产路径默认 ``False``（零回归）。

        ``force_recycle``（task-force-status-force-recycle-flag）：终态回收
        （completed / cancelled）删 ``task/<id>`` 分支时的显式丢弃开关，透传给
        :func:`orchd.worktree.remove_task_wt`。缺省 ``False``——未合并到主分支的
        任务分支**拒绝删除**并留痕 ``branch_delete_refused``（红线：不得用 ``-D``
        销毁未合并提交）；仅显式 ``True``（CLI ``--force-recycle``）才允许 ``-D``，
        提交不可恢复，动作记入 ``recycle_log``（``mode=-D`` + ``unmerged_commits``）。

        ``discard_uncommitted`` / ``discard_confirmed``
        （task-cont-dirty-worktree-ladder）：脏 worktree 回收的显式丢弃出口——
        仅双旗标齐备（CLI ``--discard-uncommitted --confirm``）才允许 force 丢弃
        未提交改动（``discarded_uncommitted``）；单旗标 E007 拒绝。缺省走五级
        降级（L1 干净 remove → L2 救援提交 → L3 目录隔离 → L4 bundle+patch →
        L5 原地保留）；``force_recycle`` 只作用于未合并分支删除，不承担丢弃脏
        工作区。
        """
    if target_status not in _FORCE_TARGETS:
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_state: cannot force to '{target_status}' (legal: {sorted(_FORCE_TARGETS)})",
            [{"target": target_status, "legal_targets": sorted(_FORCE_TARGETS)}],
        )
    if target_status == STATUS_CLAIMED and not assignee:
        raise OrchdError(
            ErrorCode.E007,
            "invalid_state: force to 'claimed' requires --assignee",
            [{"target": STATUS_CLAIMED, "missing": "assignee"}],
        )
    if discard_uncommitted and not discard_confirmed:
        # task-cont-dirty-worktree-ladder：丢弃未提交改动必须双旗标显式授权
        # （CLI --discard-uncommitted --confirm）；单旗标一律拒绝，防误触。
        raise OrchdError(
            ErrorCode.E007,
            "invalid_flags: --discard-uncommitted requires --confirm "
            "(explicit discard of uncommitted changes needs both flags)",
            [{"flag": "--discard-uncommitted", "missing": "--confirm"}],
        )

    store.acquire_lock()
    try:
        integrity_warnings = store.check_integrity()
        state = store.replay()
        ts = state.get(task_id)
        current = ts.status if ts else STATUS_PENDING

        # "允许从" 校验矩阵（completed→pending 已移出，见复活门禁）
        _ALLOWED_FROM = {
            STATUS_PENDING: {STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW},
            STATUS_CLAIMED: {STATUS_PENDING},
            STATUS_COMPLETED: {STATUS_IN_REVIEW},
            STATUS_CANCELLED: {
                STATUS_PENDING, STATUS_CLAIMED, STATUS_DONE, STATUS_IN_REVIEW,
            },
        }
        allowed = _ALLOWED_FROM.get(target_status, set())
        if current not in allowed:
            in_hatch = (target_status, current) in _FORCE_ESCAPE_HATCHES
            # 复活门禁：completed→pending 严格路径（--force + --evidence-sha git 证据）
            is_revive = current == STATUS_COMPLETED and target_status == STATUS_PENDING
            if is_revive:
                if not force:
                    raise OrchdError(
                        ErrorCode.E007,
                        f"revive_blocked: completed→pending 复活 '{task_id}' 需 --force 显式确认"
                        "（防止无授权复活已完成任务）",
                        [{TASK_ID_KEY: task_id, "missing": "--force"}],
                    )
                if not evidence_sha:
                    raise OrchdError(
                        ErrorCode.E007,
                        f"revive_blocked: completed→pending 复活 '{task_id}' 需 "
                        "--evidence-sha <commit>（git 证据，须为 main 祖先且晚于任务完成）",
                        [{TASK_ID_KEY: task_id, "missing": "--evidence-sha"}],
                    )
                _validate_revive_evidence(
                    project_root, evidence_sha, _task_completed_epoch(store, task_id), task_id
                )
                _revive_reject_surface_declarations(project_root, task_id, tasks)
            elif force and in_hatch:
                pass  # 显式逃生口：放行
            else:
                hint = (
                    "；如需走逃生口（claimed→completed / cancelled→pending）请加 --force"
                    if in_hatch
                    else ""
                )
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: cannot force '{task_id}' from '{current}' to '{target_status}'"
                    f" (allowed from: {sorted(allowed)}){hint}",
                    [{TASK_ID_KEY: task_id, "current": current, "target": target_status,
                      "allowed_from": sorted(allowed)}],
                )

        # W-5 逃生舱语义一致性：completed 终态 = 状态终态 + 代码落 main，二者由引擎一并
        # 保证。任务分支领先 main 时自动 --ff-only 快进落码后再置终态；与 main 分叉则拒绝
        # 自动合并（仅接受快进，防止静默并入错误代码）并返回冲突指引交人工。
        merge_state: dict[str, Any] | None = None
        if target_status == STATUS_COMPLETED and project_root:
            from orchd.gitops_ops import try_ff_merge_to_main

            merge_state = try_ff_merge_to_main(project_root, task_id)
            if merge_state and merge_state.get("state") == "diverged":
                raise OrchdError(
                    ErrorCode.E007,
                    f"force_complete_diverged: 任务分支 {merge_state.get('branch')} 与 main "
                    "已分叉，无法自动快进合并（逃生舱仅接受 --ff-only）。请先手工解决分叉"
                    "（合并/rebase）使任务分支可快进到 main，再重新 force-status completed，"
                    "防止静默并入错误代码。",
                    [{TASK_ID_KEY: task_id, BRANCH_KEY: merge_state.get("branch"),
                      GUIDANCE_KEY: "resolve_divergence_then_retry"}],
                )

        event = _make_event(
            task_id, agent_id, EVENT_FORCE_STATUS,
            target_status=target_status,
            reason=reason,
        )
        if assignee:
            event["assignee"] = assignee
        # 持久化复活证据（task-force-status-revive-audit 依赖）：completed→pending 复活
        # 经 revive-guard 校验的 evidence_sha 必须落库，否则 revive 标记/巡检无法展示证据。
        if evidence_sha:
            event["evidence_sha"] = evidence_sha
        if test_data:
            event["test_data"] = True
        store.append_event(event)

        new_state = store.replay()
        store.update_checkpoint(new_state)
    finally:
        store.release_lock()

    # 生命周期终态收敛（task-pass9-lifecycle-release-invariant）：force-status 同属
    # 放弃/强制路径，abandon 模式尽力恢复主工作树 trunk 基线（best-effort 不阻断
    # 控制面；失败降级为 stranded 明细透出）。
    release_info: dict[str, Any] | None = None
    if project_root:
        from orchd.gitops.guard import release_task_lifecycle

        release_info = release_task_lifecycle(
            store, project_root,
            task_id=task_id, agent_id=agent_id,
            command="force-status", mode="abandon",
        )

    result = {
        "forced": True,
        TASK_ID_KEY: task_id,
        "previous_status": current,
        "new_status": target_status,
        "reason": reason,
    }
    if release_info is not None:
        result["checked_out_main"] = release_info.get("checked_out_main")
        if release_info.get("stranded"):
            result["stranded"] = release_info["stranded"]
    # W-5：completed 逃生隐含的落码结果透出（merged / already_in_main / None）。
    if target_status == STATUS_COMPLETED and merge_state is not None:
        result["merge"] = merge_state
    # task-14-worktree-lifecycle（AC3）：终态（completed/cancelled）自动回收任务
    # worktree（git worktree remove + 删分支 + 解绑，best-effort）。
    # force-recycle：显式丢弃未合并分支（缺省 False → remove_task_wt 拒绝删除
    # 未合并分支并返回 branch_delete_refused，红线保护）。
    if target_status in TERMINAL_STATUSES and project_root:
        from orchd.worktree import remove_task_wt

        try:
            result["worktree_recycled"] = remove_task_wt(
                project_root, task_id, resolve_store_dir(store.orchd_dir),
                force_recycle=force_recycle,
                discard_uncommitted=discard_uncommitted,
                discard_confirmed=discard_confirmed,
            )
        except Exception as exc:
            # best-effort（task-unbind-recycle-audit-best-effort）：解绑/回收
            # 异常降级为结构化 warning 并透出，不再导致 force_status 落 E999。
            code = getattr(getattr(exc, "code", None), "name", type(exc).__name__)
            result["worktree_recycled"] = {
                "removed": False,
                "unbound": False,
                ERROR_KEY: f"{code}: {exc}"[:300],
                WARNINGS_KEY: [{
                    "code": "worktree_recycle_failed",
                    TASK_ID_KEY: task_id,
                    ERROR_KEY: f"{code}: {exc}"[:300],
                }],
            }
        # L3 pre-commit hook 卸载（best-effort）：force_status 直达终态时兜底，
        # 防止任务级 hook 残留（与 done/retract 一致）。该通道常被用于清理僵尸
        # 认领，此时他人很可能仍在实现 ⇒ 与其他三处同样受活跃认领门控约束
        # （task-debug-hook-active-gate）。
        hook_uninstall(project_root, orchd_dir=store.orchd_dir)
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    return result

# task-errexit-weak-polish-batch: E007 hint polish placeholder
