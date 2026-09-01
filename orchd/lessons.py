"""orchd/lessons.py — 经验回灌引擎（task-lesson-feedback-engine）。

将「引擎未覆盖、agent 自行解决」的问题沉淀为可复用 guidance，相似场景自动
触发给出解决方案指引，形成「问题 → 自愈 → 沉淀 → 触发」闭环。

设计：design/lesson-feedback-design-20260828.md（§1–§13）。核心特性：
- lesson 库（lessons.jsonl）+ 暂存区（lessons.staged.jsonl），append-only JSONL。
- 触发注入（lookup_lessons）：精确匹配 trigger.key，预算 cap（3 verified + 1 proposed /
  600 字符），摘要截断，版本漂移标注。
- 信任分级：proposed（参考）/ verified（正式触发）/ archived（不再触发）。
- 并发写保护：复用 lockfile.ExclusiveFileLock（best-effort 5s 超时，失败跳过不阻塞）。
- 冲突检测：同 trigger.key 矛盾 solution 标记 conflicts_with（关键词粗筛）。
- 版本漂移判定：engine_version 与当前版本比对，minor 差异追加确认标注、major 差异降级。
- done 收尾 hook：检测暂存建议 + resolved 交叉验证（verify 未通过自动降级）。

依赖方向：lessons.py → lockfile.py / errors.py / orchd.__version__（不导入
ledger / onboard / cli，避免循环依赖）。
best-effort：任何 I/O 异常静默降级，绝不阻塞命令主流程（设计原则 4）。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from orchd import __version__
from orchd.errors import ErrorCode, OrchdError
from orchd.lockfile import ExclusiveFileLock

# ── 常量 ──────────────────────────────────────────────────────────────────────
SYMPTOM_MAX = 200          # §6.2 单条 symptom 上限
SOLUTION_MAX = 500         # §6.2 单条 solution 上限
SYMPTOM_TRUNC = 80         # §8.5 注入摘要截断
SOLUTION_TRUNC = 200       # §8.5 注入摘要截断
CASES_VERIFIED_CAP = 3     # §8.5 verified 条数硬 cap
CASES_PROPOSED_CAP = 1     # §8.5 无 verified 时 proposed 候选 ≤ 1
CASES_CHAR_BUDGET = 600    # §8.5 单次注入字符预算
LOCK_TIMEOUT_S = 5.0       # §6.3 并发写锁 best-effort 超时
LESSON_ID_RE = re.compile(r"^lesson-(\d+)$")

# 冲突检测否定词（§6.5）：新 solution 含这些词且复用旧 solution 核心动作 → 标记矛盾。
_NEG_WORDS = {"不要", "禁止", "不应", "不应该", "不能", "勿", "避免", "拒绝", "别", "不建议"}

# warning 级错误码（需触发注入标注「参考（warning）」）。
_WARNING_CODES = {"E023", "E026", "E028", "E029", "E030"}
# 引擎预判「值得上报」的 warning 码（§5.1 信号 A）。
_SUGGEST_REPORT_CODES = {"E030"}


# ── 路径解析 ──────────────────────────────────────────────────────────────────
def _resolve_runtime(orchd_dir: Path | str) -> Path:
    """解析 lesson 运行时根目录（与 ledger 同根，设计 §6.3）。

    复用引擎 ``resolve_store_dir`` 作为唯一事实源：container 布局落到
    ``<容器>/.orchd-runtime/``，flat 布局落到 ``orchd_dir`` 本身（与 _ledger.jsonl
    同层）。任一解析失败 best-effort 回退 ``orchd_dir``。
    """
    try:
        from orchd.ledger import resolve_store_dir

        return Path(resolve_store_dir(Path(orchd_dir)))
    except Exception:
        return Path(orchd_dir)


def _lib_path(orchd_dir: Path | str) -> Path:
    return _resolve_runtime(orchd_dir) / "lessons.jsonl"


def _staged_path(orchd_dir: Path | str) -> Path:
    return _resolve_runtime(orchd_dir) / "lessons.staged.jsonl"


def _lock_path(orchd_dir: Path | str) -> Path:
    return _resolve_runtime(orchd_dir) / "lessons.lock"


# ── JSONL 读写（best-effort + 并发锁）─────────────────────────────────────────
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，跳过空行与损坏行（含不完整的末行，设计 §6.3）。"""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except (ValueError, json.JSONDecodeError):
            # 不完整/损坏行：跳过（末行写一半进程崩溃场景）
            continue
    return out


def _append_jsonl(path: Path, obj: dict[str, Any], lock_path: Path) -> bool:
    """追加一行 JSONL（持 ExclusiveFileLock，best-effort 超时跳过）。

    Returns:
        True 写入成功；False 锁超时跳过（不阻塞主流程）。
    """
    lock = ExclusiveFileLock(lock_path)
    try:
        lock.acquire(blocking=False, timeout_s=LOCK_TIMEOUT_S)
    except OrchdError:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError:
        return False
    finally:
        lock.release()


def _rewrite_jsonl(path: Path, rows: list[dict[str, Any]], lock_path: Path) -> bool:
    """整体重写 JSONL（持 ExclusiveFileLock，互斥写，设计 §6.3）。

    Returns:
        True 成功；False 锁超时跳过。
    """
    lock = ExclusiveFileLock(lock_path)
    try:
        lock.acquire(blocking=False, timeout_s=LOCK_TIMEOUT_S)
    except OrchdError:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        # 持锁（独立锁文件 lessons.lock）期间整体覆写「数据文件」path。
        # 锁 fd 属于锁文件本身，不可用于写数据；path 为不同文件，
        # 无 Windows msvcrt 字节锁的跨句柄冲突问题。
        path.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        lock.release()


# ── 配置 ──────────────────────────────────────────────────────────────────────
def load_lessons_config(orchd_dir: Path | str) -> dict[str, Any]:
    """读取 lessons 配置（设计 §9.5 / §10.1），缺省：enabled=true / require_review=true
    / review_timeout_minutes=60。best-effort：任何异常回退默认。"""
    default = {"enabled": True, "require_review": True, "review_timeout_minutes": 60}
    try:
        mp = Path(orchd_dir) / "_master.json"
        if mp.exists():
            cfg = json.loads(mp.read_text(encoding="utf-8")).get("config", {})
            lessons = cfg.get("lessons")
            if isinstance(lessons, dict):
                if "enabled" in lessons:
                    default["enabled"] = bool(lessons["enabled"])
                if "require_review" in lessons:
                    default["require_review"] = bool(lessons["require_review"])
                if "review_timeout_minutes" in lessons:
                    try:
                        default["review_timeout_minutes"] = int(lessons["review_timeout_minutes"])
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass
    return default


def is_lessons_enabled(orchd_dir: Path | str) -> bool:
    """lessons.enabled 开关（设计 §9.5）。关闭时注入/引导/写命令全部拒绝。"""
    return bool(load_lessons_config(orchd_dir).get("enabled", True))


# ── 校验与 ID ──────────────────────────────────────────────────────────────────
def _validate_lengths(symptom: str, solution: str) -> None:
    """长度校验（§6.2）：超限抛 E007。"""
    if len(symptom) > SYMPTOM_MAX:
        raise OrchdError(
            ErrorCode.E007,
            f"lesson_validation: symptom 超过 {SYMPTOM_MAX} 字符（当前 {len(symptom)}）",
            [{"field": "symptom", "max": SYMPTOM_MAX, "actual": len(symptom)}],
        )
    if len(solution) > SOLUTION_MAX:
        raise OrchdError(
            ErrorCode.E007,
            f"lesson_validation: solution 超过 {SOLUTION_MAX} 字符（当前 {len(solution)}）",
            [{"field": "solution", "max": SOLUTION_MAX, "actual": len(solution)}],
        )


def _next_lesson_id(orchd_dir: Path | str) -> str:
    """自增 lesson id（复用 ledger 的 id 分配模式，§6.2）。"""
    rows = _read_jsonl(_lib_path(orchd_dir))
    max_n = 0
    for r in rows:
        m = LESSON_ID_RE.match(str(r.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"lesson-{max_n + 1:04d}"


# ── 冲突检测（§6.5）────────────────────────────────────────────────────────────
def _tokenize(text: str) -> set[str]:
    """粗略分词：去标点、小写、CJK 与英文词混合。"""
    text = text.lower()
    # 保留中文连续串与英文单词
    tokens = re.findall(r"[一-鿿]+|[a-z0-9]+", text)
    return {t for t in tokens if len(t) >= 2}


def _conflict_with(existing: list[dict[str, Any]], new_solution: str) -> str | None:
    """同 trigger.key 下新 resolved 条目与已有 verified 条目矛盾检测（关键词粗筛）。

    规则：新 solution 含否定词 且 与旧 solution 共享核心动作词 → 标记 conflicts_with。
    不做 LLM 语义判定（对齐方案 C 搁置），仅关键词粗筛 + 人工裁决。
    """
    if not _NEG_WORDS.intersection(_tokenize(new_solution)):
        return None
    new_tokens = _tokenize(new_solution)
    for e in existing:
        if e.get("status") != "verified":
            continue
        old_sol = e.get("solution", "")
        if not old_sol:
            continue
        if new_tokens & _tokenize(old_sol):
            return e.get("id")
    return None


# ── 版本漂移判定（§9）──────────────────────────────────────────────────────────
def _version_relation(old_v: str | None, cur_v: str) -> str:
    """返回 major / minor / same。best-effort：解析失败回退 same。"""
    if not old_v:
        return "same"
    try:
        from packaging.version import Version

        va, vb = Version(str(old_v)), Version(str(cur_v))
        if va.major != vb.major:
            return "major"
        if va.minor != vb.minor:
            return "minor"
        return "same"
    except Exception:
        # 兜底：仅比较首个整数段
        try:
            a0 = int(str(old_v).split(".")[0])
            b0 = int(str(cur_v).split(".")[0])
            if a0 != b0:
                return "major"
            return "same"
        except (ValueError, IndexError):
            return "same"


def _to_case(entry: dict[str, Any], cur_v: str) -> dict[str, Any]:
    """将库条目转为注入 case（摘要截断 + 版本漂移标注，设计 §8.4/§9）。"""
    symptom = (entry.get("symptom") or "")[:SYMPTOM_TRUNC]
    solution = (entry.get("solution") or "")[:SOLUTION_TRUNC]
    old_v = (entry.get("source") or {}).get("engine_version")
    relation = _version_relation(old_v, cur_v)
    status = entry.get("status", "proposed")
    if relation == "major":
        # 旧大版本：降级为 proposed 标注处理
        status = "proposed"
        suffix = "（旧大版本经验，仅供参考）"
        solution = (solution + suffix)[:SOLUTION_TRUNC + len(suffix)]
    elif relation == "minor":
        suffix = f"（来自 v{old_v}，当前 v{cur_v}，请确认适用性）"
        solution = (solution + suffix)[:SOLUTION_TRUNC + len(suffix)]
    return {
        "id": entry.get("id"),
        "symptom": symptom,
        "solution": solution,
        "status": status,
        "severity": entry.get("severity", "blocking"),
        "drift_note": relation if relation != "same" else None,
    }


def _apply_char_budget(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """字符预算约束（§8.5）：超出 600 字符从高优先级末尾裁剪。"""
    total = sum(len(c.get("symptom", "")) + len(c.get("solution", "")) for c in cases)
    out = list(cases)
    while total > CASES_CHAR_BUDGET and out:
        dropped = out.pop()
        total -= len(dropped.get("symptom", "")) + len(dropped.get("solution", ""))
    return out


# ── 触发注入（§8.4）────────────────────────────────────────────────────────────
def lookup_lessons(
    orchd_dir: Path | str, trigger_key: str, current_version: str | None = None
) -> list[dict[str, Any]]:
    """按 trigger.key 精确匹配查询注入 cases（设计 §8.2/§8.5）。

    - verified 命中 → 按 hits 降序取顶 3；无 verified 命中 → proposed 取 1（标注未验证）。
    - 摘要截断 + 字符预算（≤600）；版本漂移标注。
    - best-effort：库缺失/损坏返回空列表，不注入。
    """
    if current_version is None:
        current_version = __version__
    try:
        entries = _read_jsonl(_lib_path(orchd_dir))
    except Exception:
        return []
    verified = [
        e for e in entries
        if e.get("status") == "verified" and (e.get("trigger") or {}).get("key") == trigger_key
    ]
    proposed = [
        e for e in entries
        if e.get("status") == "proposed" and (e.get("trigger") or {}).get("key") == trigger_key
    ]
    verified.sort(key=lambda e: e.get("hits", 0), reverse=True)
    cases: list[dict[str, Any]] = []
    for e in verified[:CASES_VERIFIED_CAP]:
        cases.append(_to_case(e, current_version))
    if not cases:
        for e in proposed[:CASES_PROPOSED_CAP]:
            cases.append(_to_case(e, current_version))
    return _apply_char_budget(cases)


# ── 写命令：stage / add / report ───────────────────────────────────────────────
def _enrich_hits(orchd_dir: Path | str, trigger_key: str) -> None:
    """命中计数 +1（供淘汰参考，§6.1 hits）。best-effort。"""
    try:
        path = _lib_path(orchd_dir)
        rows = _read_jsonl(path)
        changed = False
        for r in rows:
            if (r.get("trigger") or {}).get("key") == trigger_key and r.get("status") == "verified":
                r["hits"] = int(r.get("hits", 0)) + 1
                changed = True
        if changed:
            _rewrite_jsonl(path, rows, _lock_path(orchd_dir))
    except Exception:
        pass


def stage(
    orchd_dir: Path | str,
    *,
    task_id: str,
    trigger_type: str,
    trigger_key: str,
    scene: str | None,
    symptom: str,
    solution: str,
    resolved: bool,
    severity: str,
    urgent: bool,
    source: dict[str, Any],
) -> dict[str, Any]:
    """lesson stage：执行中静默打点到任务暂存区（设计 §7/§8.6）。

    写入 lessons.staged.jsonl（带 task_id + resolved 标记），无 stderr 噪音。
    校验：trigger.key 必填、symptom/solution 长度；冲突检测（resolved=true 时）。
    """
    if not trigger_key:
        raise OrchdError(ErrorCode.E007, "lesson_validation: trigger.key 必填",
                         [{"field": "trigger_key"}])
    _validate_lengths(symptom, solution)
    entry: dict[str, Any] = {
        "task_id": task_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": {"type": trigger_type, "key": trigger_key, "scene": scene},
        "symptom": symptom,
        "solution": solution,
        "resolved": bool(resolved),
        "severity": severity,
        "urgent": bool(urgent),
        "source": source,
    }
    if resolved and solution:
        conflict = _conflict_with(_read_jsonl(_lib_path(orchd_dir)), solution)
        if conflict:
            entry["conflicts_with"] = conflict
    ok = _append_jsonl(_staged_path(orchd_dir), entry, _lock_path(orchd_dir))
    if not ok:
        # best-effort 跳过：不阻塞主流程（设计 §6.3）
        return {"staged": False, "skipped": True,
                "reason": "lesson 暂存因并发冲突跳过，不影响主流程"}
    return {"staged": True, "entry": entry}


def add(
    orchd_dir: Path | str,
    *,
    trigger_type: str,
    trigger_key: str,
    scene: str | None,
    symptom: str,
    solution: str,
    severity: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    """lesson add：人工/事后手动入库（不经任务流程，设计 §7）。

    写入主库，status=proposed（信任分级：proposed 仅参考，resolve --approve 才 verified）。
    同 trigger.key + 相近 symptom 提示已有相近 lesson（不自动去重，交人工 resolve）。
    """
    if not trigger_key:
        raise OrchdError(ErrorCode.E007, "lesson_validation: trigger.key 必填",
                         [{"field": "trigger_key"}])
    if not solution:
        raise OrchdError(ErrorCode.E007, "lesson_validation: add 需提供 solution",
                         [{"field": "solution"}])
    _validate_lengths(symptom, solution)
    lesson_id = _next_lesson_id(orchd_dir)
    entry: dict[str, Any] = {
        "id": lesson_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": {"type": trigger_type, "key": trigger_key, "scene": scene},
        "symptom": symptom,
        "solution": solution,
        "source": source,
        "severity": severity,
        "status": "proposed",
        "hits": 0,
        "lengths": {"symptom": len(symptom), "solution": len(solution)},
    }
    # 相近提示（不自动去重）
    similar = [
        e for e in _read_jsonl(_lib_path(orchd_dir))
        if (e.get("trigger") or {}).get("key") == trigger_key
    ]
    note = None
    if similar:
        note = f"已有 {len(similar)} 条相近 trigger.key={trigger_key} 的 lesson，请人工比对"
    ok = _append_jsonl(_lib_path(orchd_dir), entry, _lock_path(orchd_dir))
    if not ok:
        return {"added": False, "skipped": True,
                "reason": "lesson 入库因并发冲突跳过，不影响主流程"}
    return {"added": True, "id": lesson_id, "similar_note": note}


def report(
    orchd_dir: Path | str,
    *,
    trigger_type: str,
    trigger_key: str,
    scene: str | None,
    symptom: str,
    severity: str,
    source: dict[str, Any],
    guidance_flaw: bool = False,
) -> dict[str, Any]:
    """lesson report：只记问题不记解法（设计 §7）。

    status=proposed，solution 为空；guidance_flaw 标记指引缺陷（供后续补 ERROR_GUIDANCE）。
    """
    if not trigger_key:
        raise OrchdError(ErrorCode.E007, "lesson_validation: trigger.key 必填",
                         [{"field": "trigger_key"}])
    _validate_lengths(symptom, "")
    lesson_id = _next_lesson_id(orchd_dir)
    entry: dict[str, Any] = {
        "id": lesson_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trigger": {"type": trigger_type, "key": trigger_key, "scene": scene},
        "symptom": symptom,
        "solution": "",
        "source": source,
        "severity": severity,
        "status": "proposed",
        "hits": 0,
        "guidance_flaw": bool(guidance_flaw),
        "lengths": {"symptom": len(symptom), "solution": 0},
    }
    ok = _append_jsonl(_lib_path(orchd_dir), entry, _lock_path(orchd_dir))
    if not ok:
        return {"reported": False, "skipped": True,
                "reason": "lesson 入库因并发冲突跳过，不影响主流程"}
    return {"reported": True, "id": lesson_id}


# ── 收尾 hook（§8.6）───────────────────────────────────────────────────────────
def run_done_lesson_hook(
    orchd_dir: Path | str, task_id: str, verify_passed: bool | None
) -> dict[str, Any]:
    """done 收尾 hook：检测暂存建议 + resolved 交叉验证（设计 §8.6 补丁 3）。

    - 读取该任务 staged 条目；无 → 返回 has_lessons=False（零影响）。
    - resolved=true 但 verify 未通过（verify_passed=False/None）→ 引擎降级
      resolved=false，标注降级原因并持久化回 staged。
    - 给每条打 detected_at 时间戳（供 review 超时降级判定）。
    - 返回结构化汇总（items / degraded / summary），由调用方决定是否挂起（门禁）。
    """
    staged = [e for e in _read_jsonl(_staged_path(orchd_dir)) if e.get("task_id") == task_id]
    if not staged:
        return {"has_lessons": False}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    degraded: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    all_staged = _read_jsonl(_staged_path(orchd_dir))
    for s in all_staged:
        if s.get("task_id") != task_id:
            continue
        s["detected_at"] = now_iso
        item = {
            "trigger_key": (s.get("trigger") or {}).get("key"),
            "symptom": s.get("symptom"),
            "severity": s.get("severity", "blocking"),
            "resolved": bool(s.get("resolved")),
            "conflicts_with": s.get("conflicts_with"),
        }
        if s.get("resolved") and not verify_passed:
            s["resolved"] = False
            reason = "resolved 被引擎降级：verify 未通过/未执行"
            s["degraded_reason"] = reason
            item["resolved"] = False
            item["degraded_reason"] = reason
            degraded.append(item)
        items.append(item)
    # 持久化 detected_at / 降级结果（best-effort）
    _rewrite_jsonl(_staged_path(orchd_dir), all_staged, _lock_path(orchd_dir))
    summary = (
        f"本次任务发现 {len(items)} 个 guidance 增补建议"
        f"（resolved={sum(1 for i in items if i['resolved'])}），待人工审核"
    )
    return {
        "has_lessons": True,
        "count": len(items),
        "items": items,
        "degraded": degraded,
        "summary": summary,
    }


# ── 审查收口：review / resolve / archive ──────────────────────────────────────
def review_task(
    orchd_dir: Path | str,
    *,
    task_id: str,
    approve_all: bool = False,
    reject_indices: list[int] | None = None,
) -> dict[str, Any]:
    """lesson review：批量确认任务全部暂存建议（设计 §7/§8.6）。

    - approve → 转主库：resolved=true → verified（正式触发）；resolved=false → proposed。
    - reject（指定 staged 内序号）→ 丢弃，不入库。
    - 确认后清理该任务 staged 行；超时降级：detected_at 超 review_timeout_minutes
      仍被 review → 标注 timed_out（自动放行，不阻塞）。
    Returns: {promoted:[...], rejected:[...], timed_out:bool}
    """
    reject_indices = set(reject_indices or [])
    staged = [e for e in _read_jsonl(_staged_path(orchd_dir)) if e.get("task_id") == task_id]
    if not staged:
        return {"promoted": [], "rejected": [], "timed_out": False, "note": "无暂存建议"}
    cfg = load_lessons_config(orchd_dir)
    timeout = int(cfg.get("review_timeout_minutes", 60))
    timed_out = False
    now = time.time()
    for s in staged:
        da = s.get("detected_at")
        if da:
            try:
                # 解析 %z 时间戳
                dt = time.strptime(da, "%Y-%m-%dT%H:%M:%S%z")
                if (now - time.mktime(dt)) > timeout * 60:
                    timed_out = True
            except (ValueError, OverflowError):
                pass

    promoted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    kept_staged: list[dict[str, Any]] = []
    all_staged = _read_jsonl(_staged_path(orchd_dir))
    for idx, s in enumerate(staged):
        if idx in reject_indices:
            rejected.append({"trigger_key": (s.get("trigger") or {}).get("key"),
                             "symptom": s.get("symptom")})
            continue
        if not approve_all and idx not in reject_indices:
            # 未 approve 且未 reject → 保留在 staged（等待后续处理）
            kept_staged.append(s)
            continue
        lesson_id = _next_lesson_id(orchd_dir)
        status = "verified" if s.get("resolved") else "proposed"
        entry = {
            "id": lesson_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "trigger": s.get("trigger"),
            "symptom": s.get("symptom"),
            "solution": s.get("solution", ""),
            "source": s.get("source"),
            "severity": s.get("severity", "blocking"),
            "status": status,
            "hits": 0,
            "conflicts_with": s.get("conflicts_with"),
            "lengths": {"symptom": len(s.get("symptom", "")),
                        "solution": len(s.get("solution", ""))},
        }
        # 入库后从 staged 移除
        all_staged = [x for x in all_staged if not (x is s)]
        _append_jsonl(_lib_path(orchd_dir), entry, _lock_path(orchd_dir))
        promoted.append({
            "id": lesson_id,
            "status": status,
            "trigger_key": (s.get("trigger") or {}).get("key"),
            "symptom": s.get("symptom"),
            "source": s.get("source"),
        })
    # 写回 staged（保留未处理 + 移除已处理）
    final_staged = [x for x in all_staged if x.get("task_id") != task_id] + kept_staged
    _rewrite_jsonl(_staged_path(orchd_dir), final_staged, _lock_path(orchd_dir))
    return {"promoted": promoted, "rejected": rejected, "timed_out": timed_out}


def resolve_lesson(
    orchd_dir: Path | str, *, lesson_id: str, approve: bool
) -> dict[str, Any]:
    """lesson resolve：人工确认信任分级（设计 §7/§9）。

    approve → verified（正式触发）；reject → archived（不再触发）。
    """
    path = _lib_path(orchd_dir)
    rows = _read_jsonl(path)
    target = next((r for r in rows if r.get("id") == lesson_id), None)
    if target is None:
        raise OrchdError(ErrorCode.E007, f"lesson '{lesson_id}' 不存在",
                         [{"lesson_id": lesson_id}])
    target["status"] = "verified" if approve else "archived"
    if not _rewrite_jsonl(path, rows, _lock_path(orchd_dir)):
        return {"resolved": False, "skipped": True,
                "reason": "lesson 写入因并发冲突跳过，不影响主流程"}
    return {"resolved": True, "id": lesson_id, "status": target["status"]}


def archive_lesson(orchd_dir: Path | str, *, lesson_id: str) -> dict[str, Any]:
    """lesson archive：手动归档（不再触发，设计 §7/§9）。"""
    path = _lib_path(orchd_dir)
    rows = _read_jsonl(path)
    target = next((r for r in rows if r.get("id") == lesson_id), None)
    if target is None:
        raise OrchdError(ErrorCode.E007, f"lesson '{lesson_id}' 不存在",
                         [{"lesson_id": lesson_id}])
    target["status"] = "archived"
    if not _rewrite_jsonl(path, rows, _lock_path(orchd_dir)):
        return {"archived": False, "skipped": True,
                "reason": "lesson 写入因并发冲突跳过，不影响主流程"}
    return {"archived": True, "id": lesson_id}


# ── 查询：list / show ──────────────────────────────────────────────────────────
def list_lessons(
    orchd_dir: Path | str,
    *,
    status: str | None = None,
    trigger: str | None = None,
    staged: bool = False,
    all: bool = False,
) -> list[dict[str, Any]]:
    """查看 lesson 库（设计 §7）。

    staged=True → 看暂存区；否则看主库。status/trigger 过滤（主库）。
    """
    if staged:
        rows = _read_jsonl(_staged_path(orchd_dir))
        return [
            {
                "task_id": r.get("task_id"),
                "trigger_key": (r.get("trigger") or {}).get("key"),
                "symptom": r.get("symptom"),
                "resolved": r.get("resolved"),
                "severity": r.get("severity"),
                "conflicts_with": r.get("conflicts_with"),
            }
            for r in rows
        ]
    rows = _read_jsonl(_lib_path(orchd_dir))
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if trigger:
        rows = [r for r in rows if (r.get("trigger") or {}).get("key") == trigger]
    return [
        {
            "id": r.get("id"),
            "trigger_key": (r.get("trigger") or {}).get("key"),
            "symptom": r.get("symptom"),
            "status": r.get("status"),
            "severity": r.get("severity"),
            "hits": r.get("hits", 0),
        }
        for r in rows
    ]


def show_lesson(orchd_dir: Path | str, *, lesson_id: str) -> dict[str, Any] | None:
    """查看完整条目（完整 solution，超出注入摘要，设计 §7/§8.5）。"""
    rows = _read_jsonl(_lib_path(orchd_dir))
    return next((r for r in rows if r.get("id") == lesson_id), None)

# task-errexit-weak-polish-batch: E007 hint polish placeholder
