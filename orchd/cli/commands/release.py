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
    MESSAGE_KEY,
    OK_KEY,
    STATUS_KEY,
)

# 发版步骤固定序列（单一真源）：order 即执行顺序，command 为将复用的既有入口
# （baseline 参数与 .githooks/pre-push 的 PYTEST_ARGS/BASELINE_PATH 权威口径逐字同参）。
STEPS: tuple[dict[str, str], ...] = (
    {
        "key": "preflight",
        "name": "发版前置校验",
        COMMAND_KEY: "python .orchd/__main__.py status  # 核工作树/分支/在池任务",
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

# 基线步权威参数（与 .githooks/pre-push 的 PYTEST_ARGS/BASELINE_PATH 逐字同参）
_BASELINE_PATH = ".orchd/test_baseline.json"
_BASELINE_PYTEST_ARGS = ["tests/", "-n", "8", "--max-worker-restart=5"]

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


def _build_step_command(step: dict[str, str], tag: str) -> list[str] | None:
    """构建步骤的子进程命令（tag 步返回 None：走只读验证，不执行子进程）。"""
    key = step["key"]
    if key == "preflight":
        return [sys.executable, ".orchd/__main__.py", "status"]
    if key == "full-regression":
        return [sys.executable, ".orchd/__main__.py", "full-regression"]
    if key == "release-ready":
        return [sys.executable, "scripts/check_release_ready.py", _version_from_tag(tag)]
    if key == "baseline":
        return [
            sys.executable,
            "scripts/check_test_baseline.py",
            "--record",
            _BASELINE_PATH,
            "--",
            *_BASELINE_PYTEST_ARGS,
        ]
    return None


def _verify_tag(project_root: Path, tag: str) -> dict[str, Any]:
    """tag 就位只读验证（零 git 写）：rev-parse 双查 tag^{commit} 与 HEAD。

    - tag 不存在 → ``{"ok": False, "awaiting": True, ...}``（待管理员打 tag）；
    - tag 存在且指向 HEAD → ok；
    - tag 存在但异 HEAD → failed（evidence 含两 sha，须重打）。
    """

    def _rev(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", *args], cwd=str(project_root),
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
        return proc.returncode, (proc.stdout or "").strip()

    rc_head, head = _rev(["rev-parse", "HEAD"])
    if rc_head != 0:
        return {OK_KEY: False, "evidence": f"rev-parse HEAD 失败 rc={rc_head}"}
    rc_tag, tagged = _rev(["rev-parse", f"{tag}^{{commit}}"])
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


def plan_payload() -> dict[str, Any]:
    """发版计划（dry-run）：固定步骤序列 + 每步将复用的既有入口，零副作用。"""
    return {
        "action": "plan",
        "steps": [dict(step) for step in STEPS],
        "push": "本通道不含 push：push 由项目管理员执行（逃生口不变）",
        "note": "dry-run：只列计划，不写状态、不跑测试、不打 tag、不 push",
    }


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
    data.setdefault("steps", {})
    return data


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """原子写凭据：先写同目录临时文件再 os.replace，避免半截文件。"""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, state_path)


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
    command = _build_step_command(step, tag)
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
            continue  # 幂等续跑：已成功步跳过（awaiting_admin / failed 均重执行）
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
        if ok:
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
            ERROR_KEY: None if ok else outcome.get("evidence", "step failed"),
        }
        if ok:
            state["failed_step"] = None
            state["awaiting_step"] = None
        elif awaiting:
            state["awaiting_step"] = key
            stopped_at = key
        else:
            state["failed_step"] = key
            stopped_at = key
        save_state(state_path, state)
        commit(
            project_root,
            f"chore(release): {tag} 步骤 {key} "
            f"{'完成' if ok else ('待管理员（tag 未就位）' if awaiting else '失败（停点）')}",
        )
        if not ok:
            break

    result: dict[str, Any] = {
        OK_KEY: stopped_at is None,
        "tag": tag,
        "steps": {k: v.get("status") for k, v in state["steps"].items()},
        "awaiting": bool(state.get("awaiting_step")),
        "state_path": str(state_path),
    }
    if stopped_at is not None:
        result["stopped_at"] = stopped_at
        if state.get("failed_step"):
            result["failed_step"] = state["failed_step"]
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
    return plan_payload(), 0


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
