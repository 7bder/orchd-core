"""orchd CLI 路由：release 命令 handler（task-release-steps-wiring）。

发版受管通道（方案 C，2026-10-23 拍板：**tag/push 均不由引擎执行**）：
把「跑全量回归 → 落发版凭据并提交 → 发版就绪校验 → 记录基线 → tag 就位验证」
串成一条带凭据与断点续跑的编排通道。

步骤接线（task-release-steps-wiring）：
  - preflight / full-regression / release-ready / baseline 四步以 subprocess
    执行**既有入口**（不重造门禁，口径与 .githooks/pre-push 权威参数逐字同参）；
  - tag 步为**只读验证**（rev-parse 双查 tag^{commit} 与 HEAD）：tag 不存在 →
    awaiting_admin（凭据含待执行指令，重跑时该步重执行）；同 HEAD → ok；
    异 HEAD → failed；
  - push 一律不执行：由项目管理员手动 push（触发既有 pre-push→sync 链）。

边界（不可收窄的逃生口，本命令是**新增可选通道**）：
  - 不改 ``.githooks/pre-push`` 的判定语义；
  - 不移除 ``git push --no-verify`` 紧急出口；
  - 不移除账本专线（``refs/heads/orchd/ledger``）skip；
  - 不替代裸 ``git push``：push 仍由项目管理员执行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from orchd.contracts.responses import (
    COMMAND_KEY,
    ERROR_KEY,
    ERRORS_KEY,
    MESSAGE_KEY,
    OK_KEY,
    STATUS_KEY,
    STEP_KEY,
)
from orchd.guards.release import (
    DEFAULT_BASELINE_PATH as _BASELINE_PATH,
    DEFAULT_PYTEST_ARGS as _DEFAULT_PYTEST_ARGS,
    ReleaseGuardConfigError,
    ReleasePolicy,
    load_release_policy,
)
from orchd.milestone.constants import (
    MilestoneOverrideError,
    merge_overrides,
    milestone_for_version,
)

# 宿主化（task-host-guard-release）：四步入口、基线路径与 pytest 参数全部由
# ``orchd/guards/release.py`` 的默认值派生；宿主可在 guards.json 的 release 段覆盖。
# 缺脚本 / 目标路径不存在 ⇒ 该步记 ``not_applicable``（结构化理由），不再让子进程报 ENOENT。

# 发版步骤固定序列（单一真源）：order 即执行顺序，command 为将复用的既有入口
# （baseline 参数与 .githooks/pre-push 的 PYTEST_ARGS/BASELINE_PATH 权威口径逐字同参）。
STEPS: tuple[dict[str, str], ...] = (
    {
        "key": "preflight",
        "name": "发版前置校验",
        COMMAND_KEY: "python .orchd/__main__.py status  # 核工作树/分支/在池任务",
    },
    {
        "key": "surface-check",
        "name": "共享面汇总检查",
        COMMAND_KEY: (
            "python .orchd/__main__.py surfaces check --require-assembled  "
            "# 只读：未汇总片段 / 非法注册表 / required 面缺失即失败，不 tag 不 push"
        ),
    },
    {
        "key": "full-regression",
        "name": "全量回归 + 证据提交",
        COMMAND_KEY: "python .orchd/__main__.py full-regression",
    },
    {
        "key": "release-ready",
        "name": "发版就绪断言",
        COMMAND_KEY: "python scripts/check_release_ready.py <version>",
    },
    {
        "key": "baseline",
        "name": "全量基线记录",
        COMMAND_KEY: (
            "python scripts/check_test_baseline.py --record "
            ".orchd/test_baseline.json -- tests/ -n 8 --max-worker-restart=5"
        ),
    },
    {
        "key": "tag",
        "name": "tag 就位验证（只读）",
        COMMAND_KEY: (
            "git rev-parse <tag>^{commit} == HEAD（只读双查）；"
            "tag 不存在 → awaiting_admin：待管理员执行 git tag <tag> 后重跑"
        ),
    },
)

STATE_NAME = "_release_state.json"

# 各步骤子进程超时上限（秒；full-regression 预算同 orchd full-regression 命令档）
_STEP_TIMEOUTS: dict[str, int] = {
    "preflight": 120,
    "full-regression": 1800,
    "release-ready": 300,
    "baseline": 1800,
}

# 基线步默认参数（与 .githooks/pre-push 的 PYTEST_ARGS/BASELINE_PATH 逐字同参；
# 真源已上移到 orchd/guards/release.py，本处仅为兼容旧引用名）。
_BASELINE_PYTEST_ARGS = list(_DEFAULT_PYTEST_ARGS)

# 步骤执行器签名：(step, tag, project_root) -> {"ok", "evidence", "awaiting"?}
StepRunner = Callable[[dict[str, str], str, Path], dict[str, Any]]


def _now() -> str:
    """ISO-8601 UTC 时间戳。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _version_from_tag(tag: str) -> str:
    """tag → 版本号：去 v 前缀（与 check_release_ready 的两形态口径一致）。"""
    return tag[1:] if tag.startswith("v") else tag


def _tail(text: str, limit: int = 400) -> str:
    """输出摘要：取尾部，压掉首尾空白。"""
    return (text or "").strip()[-limit:]


def _not_applicable(step_key: str, reason: str, message: str, **details: Any) -> dict[str, Any]:
    """构造结构化 not_applicable（步骤跳过但不计失败）。"""
    payload: dict[str, Any] = {
        "kind": "not_applicable",
        "reason": reason,
        STEP_KEY: step_key,
        MESSAGE_KEY: f"not_applicable: {message}",
    }
    payload.update(details)
    return payload


def _script_command(
    project_root: Path, step_key: str, script_rel: str, tail: list[str]
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """脚本存在 → 返回命令；不存在 → 返回结构化 not_applicable（不交子进程报 ENOENT）。"""
    if not (project_root / script_rel).is_file():
        return None, _not_applicable(
            step_key, "missing_script", f"宿主缺少脚本 {script_rel}（步骤 {step_key} 跳过）",
            script=script_rel,
        )
    return [sys.executable, script_rel, *tail], None


def _step_plan(
    step: dict[str, str], tag: str, project_root: Path, policy: ReleasePolicy
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """构建步骤计划：``(命令, not_applicable)``；tag 步返回 ``(None, None)``。"""
    key = step["key"]
    if key == "surface-check":
        # 共享面检查是引擎内置步骤（读 orchd.surface_assembly），不依赖宿主 scripts/，
        # 因此这里不构造子进程命令；执行见 default_runner 的同名分支。
        return None, None
    if key == "preflight":
        return _script_command(project_root, key, policy.entry_script, ["status"])
    if key == "full-regression":
        return _script_command(project_root, key, policy.entry_script, ["full-regression"])
    if key == "release-ready":
        return _script_command(
            project_root, key, policy.ready_script, [_version_from_tag(tag)]
        )
    if key == "baseline":
        # 先判脚本（缺脚本是根因，缺目标路径是次因），再按显式 targets 判目标路径；
        # 不再用「像不像路径」猜 `pytest_args` 里的选项值。
        command, skipped = _script_command(
            project_root,
            key,
            policy.baseline_script,
            ["--record", policy.baseline_path, "--", *policy.pytest_args],
        )
        if skipped is not None:
            return command, skipped
        missing = [
            item
            for item in policy.baseline_targets
            if not (project_root / item).exists()
        ]
        if missing:
            return None, _not_applicable(
                key,
                "missing_paths",
                f"基线 pytest 目标路径不存在：{'、'.join(missing)}（步骤 {key} 跳过）",
                paths=missing,
            )
        return command, None
    return None, None


def _milestone_block(project_root: Path) -> dict[str, Any]:
    """宿主 milestone 覆盖的有效值视图（无配置 → 空块；非法 → 结构化 error）。"""
    try:
        policy = load_release_policy(project_root)
    except ReleaseGuardConfigError as exc:
        return {ERROR_KEY: {"kind": "release_guard_config", MESSAGE_KEY: str(exc)}}
    if not policy.milestone:
        return {}
    try:
        effective = merge_overrides(policy.milestone)
    except MilestoneOverrideError as exc:
        return {
            ERROR_KEY: {
                "kind": "milestone_override",
                MESSAGE_KEY: str(exc),
                ERRORS_KEY: [dict(item) for item in exc.errors],
            }
        }
    return {"overrides": dict(policy.milestone), "effective": effective}


def _verify_tag(project_root: Path, tag: str) -> dict[str, Any]:
    """tag 就位只读验证（零 git 写）：rev-parse 双查 tag^{commit} 与 HEAD。

    - tag 不存在 → ``{"ok": False, "awaiting": True, ...}``（待管理员打 tag）；
    - tag 存在且指向 HEAD → ok；
    - tag 存在但异 HEAD → failed（evidence 含两 sha，须重打）；
    - git 不可用 / 超时 → failed + ``git_unavailable`` 证据（不得裸抛，否则状态机会把
      凭据永久留在 ``running``）。
    """

    def _rev(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", *args], cwd=str(project_root),
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
        return proc.returncode, (proc.stdout or "").strip()

    try:
        rc_head, head = _rev(["rev-parse", "HEAD"])
        if rc_head != 0:
            return {OK_KEY: False, "evidence": f"rev-parse HEAD 失败 rc={rc_head}"}
        rc_tag, tagged = _rev(["rev-parse", f"{tag}^{{commit}}"])
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            OK_KEY: False,
            "evidence": f"git_unavailable: {type(exc).__name__}: {exc}",
        }
    if rc_tag != 0:
        return {
            OK_KEY: False,
            "awaiting": True,
            "evidence": (
                f"tag {tag} 不存在（只读验证，不代打）：待管理员执行 "
                f"`git tag {tag}` 后重跑 release run 续跑"
            ),
        }
    if tagged == head:
        return {OK_KEY: True, "evidence": f"tag {tag} -> {tagged} == HEAD（就位）"}
    return {
        OK_KEY: False,
        "evidence": f"tag {tag} -> {tagged} != HEAD {head}（须重打 tag 或换版本号）",
    }


def plan_payload(project_root: Path | None = None) -> dict[str, Any]:
    """发版计划（dry-run）：固定步骤序列 + 每步将复用的既有入口，零副作用。

    ``project_root`` 给出且宿主配置了 ``release.milestone`` 覆盖时，附加 ``milestone``
    块（overrides + effective）；无配置时响应与旧版逐条一致（不多出任何键）。
    """
    payload: dict[str, Any] = {
        "action": "plan",
        "steps": _plan_steps(project_root) if project_root is not None
        else [dict(step) for step in STEPS],
        "push": "本通道不含 push：push 由项目管理员执行（逃生口不变）",
        "note": "dry-run：只列计划，不写状态、不跑测试、不打 tag、不 push",
    }
    if project_root is not None:
        block = _milestone_block(project_root)
        if block:
            payload["milestone"] = block
    return payload


def _render_command(command: list[str]) -> str:
    """命令渲染：解释器统一显示成 ``python``（避免把本机解释器绝对路径写进计划）。"""
    return " ".join("python" if item == sys.executable else item for item in command)


def _split_note(text: str) -> tuple[str, str]:
    """把 ``STEPS`` 的「命令  # 人文说明」拆成 (命令, 说明)。"""
    command, marker, note = text.partition("#")
    return command.strip(), (note.strip() if marker else "")


def _plan_steps(project_root: Path) -> list[dict[str, str]]:
    """plan 的 steps：用**与 run 同源**的策略渲染命令（计划不得与执行两套）。

    与 run 的差异只有两处：``release-ready`` 的版本位渲染成 ``<version>``（plan 无 tag），
    以及不可执行步骤直接渲染成 ``not_applicable: <理由>``（配置非法同样如此，不留
    「永不执行却看着能跑」的命令）。原 ``STEPS`` 的人文说明落 ``note`` 字段，不丢信息。
    """
    steps = [dict(step) for step in STEPS]
    for entry in steps:
        command_text, note = _split_note(entry[COMMAND_KEY])
        if note:
            entry["note"] = note
        entry[COMMAND_KEY] = command_text
    try:
        policy = load_release_policy(project_root)
    except ReleaseGuardConfigError as exc:
        for entry in steps:
            if entry["key"] != "tag":
                entry[COMMAND_KEY] = f"not_applicable: guards.json release 段非法（{exc}）"
        return steps
    for entry in steps:
        if entry["key"] == "tag":
            continue
        step = {"key": entry["key"], "name": entry["name"]}
        command, skipped = _step_plan(step, "<version>", project_root, policy)
        if skipped is not None:
            entry[COMMAND_KEY] = skipped[MESSAGE_KEY]
        elif command is not None:
            entry[COMMAND_KEY] = _render_command(command)
    return steps


def load_state(state_path: Path) -> dict[str, Any]:
    """读取发版凭据；缺失/损坏时返回空骨架（不抛异常）。"""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tag": None, "failed_step": None, "awaiting_step": None, "steps": {}}
    if not isinstance(data, dict):
        return {"tag": None, "failed_step": None, "awaiting_step": None, "steps": {}}
    data.setdefault("tag", None)
    data.setdefault("failed_step", None)
    data.setdefault("awaiting_step", None)
    raw_steps = data.get("steps")
    # 凭据可能被手改/旧版本写坏：steps 必须是「key → 对象」；非对象项丢弃（丢弃即重跑，
    # fail-closed），否则下游 .get 会抛 AttributeError 给用户。
    steps = raw_steps if isinstance(raw_steps, dict) else {}
    data["steps"] = {
        str(key): value for key, value in steps.items() if isinstance(value, dict)
    }
    return data


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """原子写凭据：先写同目录临时文件再 os.replace，避免半截文件。"""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, state_path)


_SURFACE_ASSEMBLE_HINT = "python .orchd/__main__.py surfaces assemble"


def _surface_check_step(project_root: Path) -> dict[str, Any]:
    """发版前的共享面汇总检查（只读；不写盘、不打 tag、不 push）。

    宿主只装引擎、没有 scripts/ 时本步照跑（检查逻辑在引擎内），这是与
    full-regression / release-ready 等脚本步的关键差异。
    """
    try:
        from orchd.surface_assembly import check_surfaces
    except ImportError as exc:  # pragma: no cover - 安装半残时显式失败而非静默通过
        return {
            OK_KEY: False,
            "evidence": f"engine_missing_surface_assembly: {exc}",
            "details": {"kind": "surface_module_missing", MESSAGE_KEY: str(exc)},
        }
    try:
        result = check_surfaces(project_root, require_assembled=True)
    except Exception as exc:  # noqa: BLE001 - 步骤失败必须结构化，不得裸抛中断状态机
        return {
            OK_KEY: False,
            "evidence": f"surface_check_error: {type(exc).__name__}: {exc}",
            "details": {"kind": "surface_check_error", MESSAGE_KEY: str(exc)},
        }
    checks = [item for item in (result.get("checks") or []) if isinstance(item, dict)]
    states: list[str] = []
    for item in checks:
        name = item.get("name", "?")
        if item.get("mode") == "fragments":
            unassembled = item.get("unassembled") or []
            states.append(
                f"{name}: unassembled={len(unassembled)}"
                f" fragments={item.get('fragments', 0)} path={item.get('path', '')}"
            )
        elif item.get("mode") == "generated":
            states.append(f"{name}: generated dispatch={item.get('dispatch', '?')}")
        else:
            states.append(f"{name}: serial")
    summary = "；".join(states) or "无注册面"
    errors = [item for item in (result.get("errors") or []) if isinstance(item, dict)]
    if errors or not result.get("ok", True):
        lines = [
            f"{item.get('surface', '?')}: {item.get('reason', 'error')}"
            for item in errors
        ]
        return {
            OK_KEY: False,
            "evidence": _tail(
                "surface-check 失败："
                + "；".join(lines)
                + f"（{summary}）建议：{_SURFACE_ASSEMBLE_HINT}"
            ),
            "details": {"kind": "surface_check_failed", ERRORS_KEY: errors},
        }
    return {
        OK_KEY: True,
        "evidence": _tail(f"surface-check ok：{summary}"),
        "details": {
            "kind": "surface_check_ok",
            "checks": checks,
        },
    }


def default_runner(
    step: dict[str, str], tag: str, project_root: Path
) -> dict[str, Any]:
    """真实步骤执行器：四步 subprocess 跑既有入口，tag 步只读验证。

    不执行任何 git 写（不打 tag、不 push）；子进程失败/超时结构化落 evidence，
    绝不伪造发版成功。
    """
    key = step["key"]
    if key == "tag":
        return _verify_tag(project_root, tag)
    if key == "surface-check":
        return _surface_check_step(project_root)
    try:
        policy = load_release_policy(project_root)
    except ReleaseGuardConfigError as exc:
        return {
            OK_KEY: False,
            "evidence": f"config_invalid: {exc}",
            "details": {
                "kind": "release_guard_config",
                ERRORS_KEY: [dict(item) for item in exc.errors],
            },
        }
    command, skipped = _step_plan(step, tag, project_root, policy)
    if skipped is not None:
        return {
            OK_KEY: True,
            "not_applicable": True,
            "evidence": skipped["message"],
            "details": skipped,
        }
    if command is None:  # pragma: no cover - STEPS 全量登记，防御分支
        return {OK_KEY: False, "evidence": f"not_wired: 步骤 {key} 无命令定义"}
    timeout = _STEP_TIMEOUTS.get(key, 300)
    try:
        proc = subprocess.run(
            command, cwd=str(project_root),
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {OK_KEY: False, "evidence": f"timeout: {key} 超 {timeout}s 上限"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {OK_KEY: False, "evidence": f"spawn_failed: {key}: {exc}"}
    summary = _tail((proc.stdout or "") + (proc.stderr or ""))
    if proc.returncode == 0:
        return {OK_KEY: True, "evidence": summary or f"{key} rc=0"}
    return {OK_KEY: False, "evidence": f"rc={proc.returncode}: {summary}"}


def _default_commit(project_root: Path, message: str) -> dict[str, Any]:
    """默认分支上把凭据经 ensure_committed 提交（best-effort，永不抛）。"""
    try:
        from orchd.gitops.commit import ensure_committed
    except ImportError:  # pragma: no cover - 引擎不可导入时降级
        return {"performed": False, "reason": "engine_unavailable"}
    try:
        return ensure_committed(project_root, [".orchd/" + STATE_NAME], message)
    except Exception as exc:  # noqa: BLE001 - best-effort 提交不得影响编排
        return {"performed": False, "reason": "commit_error", MESSAGE_KEY: str(exc)}


def run_release(
    orchd_dir: Path,
    tag: str,
    runner: StepRunner | None = None,
    commit: Callable[[Path, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """驱动发版状态机：逐步执行、落凭据、失败停点、同一 tag 幂等续跑。

    awaiting_admin 语义（tag 步）：tag 不存在不算 failed——步状态记
    ``awaiting_admin``、结果带 ``awaiting=true``；重跑时该步**重执行**
    （跳过条件仍仅 ``status == "ok"``），管理员打完 tag 后重跑即转 ok 收尾。

    not_applicable 语义（task-host-guard-release）：宿主缺脚本 / 缺目标路径的步骤记
    ``not_applicable``——不算失败、不停点，但**续跑会重执行**（宿主补上脚本后该步必须能
    真跑，否则凭据里的旧结论会永久掩盖「门禁从未执行」），且**不计入成功**：只有全部门禁步
    ``ok`` 才返回 ``ok=True``（tag 只读验证不计入门禁步）。
    """
    runner = runner or default_runner
    commit = commit or _default_commit
    project_root = orchd_dir.parent
    state_path = orchd_dir / STATE_NAME

    state = load_state(state_path)
    if state.get("tag") != tag:
        state = {"tag": tag, "failed_step": None, "awaiting_step": None, "steps": {}}

    stopped_at = None
    for step in STEPS:
        key = step["key"]
        entry = state["steps"].get(key)
        if isinstance(entry, dict) and entry.get("status") == "ok":
            continue  # 幂等续跑：只跳过已完成步（awaiting_admin / failed / not_applicable 重执行）
        started = _now()
        state["steps"][key] = {
            STATUS_KEY: "running",
            "started_at": started,
            "finished_at": None,
            "evidence": "",
            ERROR_KEY: None,
        }
        save_state(state_path, state)
        commit(project_root, f"chore(release): {tag} 步骤 {key} 开始")
        outcome = runner(step, tag, project_root)
        ok = bool(outcome.get("ok"))
        awaiting = bool(outcome.get("awaiting"))
        skipped = bool(outcome.get("not_applicable"))
        if skipped:
            step_status = "not_applicable"
        elif ok:
            step_status = "ok"
        elif awaiting:
            step_status = "awaiting_admin"
        else:
            step_status = "failed"
        state["steps"][key] = {
            STATUS_KEY: step_status,
            "started_at": started,
            "finished_at": _now(),
            "evidence": outcome.get("evidence", ""),
            ERROR_KEY: None if (ok or skipped) else outcome.get("evidence", "step failed"),
        }
        if ok or skipped:
            state["failed_step"] = None
            state["awaiting_step"] = None
        elif awaiting:
            state["awaiting_step"] = key
            stopped_at = key
        else:
            state["failed_step"] = key
            stopped_at = key
        save_state(state_path, state)
        if skipped:
            tail = "not_applicable（宿主缺少该检查面）"
        elif ok:
            tail = "完成"
        elif awaiting:
            tail = "待管理员（tag 未就位）"
        else:
            tail = "失败（停点）"
        commit(project_root, f"chore(release): {tag} 步骤 {key} {tail}")
        if not ok and not skipped:
            break

    # 一律按 STEPS 的 key 取值（凭据里可能残留改名前的旧 key，不得拿来充数）。
    all_keys = [step["key"] for step in STEPS]
    gate_keys = [key for key in all_keys if key != "tag"]
    statuses = {key: state["steps"].get(key, {}).get("status") for key in all_keys}
    skipped_steps = [key for key in gate_keys if statuses.get(key) == "not_applicable"]
    gate_ok = [key for key in gate_keys if statuses.get(key) == "ok"]
    # 只有**全部门禁步都真跑且 ok** 才算成功：任意门禁步 not_applicable ⇒ ok=False
    #（「发版就绪断言没跑」不得与「全绿」不可区分）。
    result: dict[str, Any] = {
        OK_KEY: stopped_at is None and len(gate_ok) == len(gate_keys),
        "tag": tag,
        "steps": statuses,
        "awaiting": bool(state.get("awaiting_step")),
        "state_path": str(state_path),
    }
    if skipped_steps:
        result["not_applicable"] = skipped_steps
        if not result[OK_KEY] and stopped_at is None:
            result["reason"] = "not_applicable"
    if stopped_at is not None:
        result["stopped_at"] = stopped_at
        if state.get("failed_step"):
            result["failed_step"] = state["failed_step"]
    block = _milestone_block(project_root)
    if block:
        result["milestone"] = block
        effective = block.get("effective")
        if effective is not None:
            milestone_id = milestone_for_version(tag, effective)
            if milestone_id is not None:
                result["milestone_id"] = milestone_id
    return result


def status_payload(orchd_dir: Path, tag: str | None = None) -> dict[str, Any]:
    """只读进度：读凭据输出上次各步状态、失败停点与待管理员步骤。"""
    state_path = orchd_dir / STATE_NAME
    state = load_state(state_path)
    return {
        "action": "status",
        "tag": tag or state.get("tag"),
        "failed_step": state.get("failed_step"),
        "awaiting_step": state.get("awaiting_step"),
        "steps": {k: v.get("status") for k, v in state.get("steps", {}).items()},
        "state_path": str(state_path),
        "exists": state_path.is_file(),
    }


def _cmd_release_plan(args) -> tuple[dict, int]:
    """orchd release plan：打印发版步骤计划（dry-run，零副作用）。"""
    from orchd.cli._util import _find_orchd_dir

    root = Path(_find_orchd_dir()).parent
    return plan_payload(root), 0


def _cmd_release_status(args) -> tuple[dict, int]:
    """orchd release status：只读显示发版凭据进度与失败停点。"""
    from orchd.cli._util import _find_orchd_dir

    orchd_dir = Path(_find_orchd_dir())
    return status_payload(orchd_dir, getattr(args, "tag", None)), 0


def _cmd_release_run(args) -> tuple[dict, int]:
    """orchd release run：驱动发版状态机（失败停点、幂等续跑、awaiting_admin）。"""
    from orchd.cli._util import _find_orchd_dir

    orchd_dir = Path(_find_orchd_dir())
    result = run_release(orchd_dir, args.tag)
    return result, (0 if result["ok"] else 1)


def register(sub) -> None:
    """注册 release 模块的子命令。"""
    p = sub.add_parser(
        "release",
        help="发版受管通道（骨架）：plan 计划 / run 编排 / status 进度；不含 push",
    )
    actions = p.add_subparsers(dest="action")
    actions.add_parser(
        "plan", help="打印发版步骤计划（dry-run，零副作用）"
    ).set_defaults(func=_cmd_release_plan)
    actions.add_parser(
        "status", help="只读显示发版凭据进度与失败停点"
    ).set_defaults(func=_cmd_release_status)
    run_p = actions.add_parser(
        "run", help="驱动发版状态机（失败停点、幂等续跑）"
    )
    run_p.add_argument("--tag", required=True, help="目标 tag（同一 tag 重跑续跑）")
    run_p.set_defaults(func=_cmd_release_run)
