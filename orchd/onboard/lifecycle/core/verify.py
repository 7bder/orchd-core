"""lifecycle core verify 域 — verify_command 执行与 E014 构造。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

覆盖 done 侧 lock-out verify（含假失败消除）与审查侧 ``rerun_verify_at_head``
（注解而非门禁：失败/超时一律降级为记录，不抛）。``_current_head`` 与
``_verify_output_summary`` 是本域的模块级绑定，补丁键随落点迁移。
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops_ops import (
    decode_subprocess_output as _decode_subprocess_output,
    verify_output_summary as _verify_output_summary,
)
from orchd.ledger import Store
from orchd.review import find_last_done_event as _find_last_done_event


# 模块级常量：迁移自 orchd/onboard.py（原行号 120-123）
_VERIFY_TIMEOUT = 120


_FULL_REGRESSION_TIMEOUT = 300


def _current_head(project_root: Path | None) -> str | None:
    """best-effort 取当前 HEAD sha（verify 记录的**同源锚**）。

    task-review-verify-at-head：DONE 存证与「按 HEAD 复跑」记录都带本值，
    两侧比对才判得出「存证与所审内容是否同源」。取锚失败（非 git 仓库 / git
    不可用）返回 None——**不得因此阻断 done**：锚缺失时比对侧 fail-open，
    行为退化为改动前，比把整条 done 打崩更安全。

    Args:
        project_root: 仓库/工作树根目录；None 直接返回 None。

    Returns:
        HEAD 的完整 sha 字符串；取不到时 None。
    """
    if project_root is None:
        return None
    try:
        from orchd.gitops.query import get_head_commit

        return get_head_commit(project_root)
    except Exception:
        return None


def _run_verify(
    store: Store,
    task_def: dict[str, Any],
    task_id: str,
    project_root: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """S-A2 阶段 3：锁外执行 verify_command，返回 verify_record。

    返回二元组 (verify_record, early_done)：
    - verify_record：verify 通过（或无 verify_command）时的结果记录，供 DONE 入库；
    - early_done：假失败消除命中（本次 attempt 的 DONE 已并发落地、任务已离开
      claimed）时，需提前返回的成功响应；否则为 None（继续主流程）。

    P2：verify 结果摘要随 DONE 事件入库（verify_record），供 review claim 注入引用。
    """
    verify_cmd = task_def.get("verify_command")
    if not verify_cmd or not project_root:
        return None, None

    # B3（ROADMAP 1.1.1）：verify 超时可配置——任务级 verify_timeout_seconds 可选字段，
    # 缺失回退引擎默认 _VERIFY_TIMEOUT=120。
    verify_timeout = task_def.get("verify_timeout_seconds") or _VERIFY_TIMEOUT

    from orchd.spec import verify_command_dangerous_reasons

    _dangerous = verify_command_dangerous_reasons(verify_cmd)
    if _dangerous:
        raise OrchdError(
            ErrorCode.E027,
            "verify_command 含 shell 注入风险，拒绝执行",
            [{
                "task_id": task_id,
                "reasons": _dangerous,
                "hint": (
                    "verify_command 仅允许 pytest / python -c / exit 等，"
                    "禁止命令替换/管道/命令链/重定向/危险命令"
                ),
            }],
        )

    # task-review-verify-at-head AC1：先取 HEAD 锚再执行——锚代表「本次 verify
    # 所覆盖的代码状态」。没有锚就无从判断后续存证与所审内容是否同源（旧行为
    # 下「补交一笔改动再送审」可绕过机器复跑，正是因为存证里只有 cwd）。
    verify_head = _current_head(project_root)
    started = time.monotonic()
    try:
        # 通过包模块获取 run_shell（使测试 monkeypatch 传导到本模块）
        from orchd.onboard import run_shell as _run_shell
        result = _run_shell(verify_cmd, str(project_root), verify_timeout)
        elapsed = round(time.monotonic() - started, 1)
        if result.returncode != 0:
            early = _verify_failure_early_done(store, task_id, elapsed)
            if early is not None:
                early["verify"] = {
                    "returncode": result.returncode,
                    "elapsed_seconds": elapsed,
                }
                return None, early
            raise _verify_fail_error(verify_cmd, task_id, result, elapsed)
        verify_record = {
            "ok": True,
            "exit_code": result.returncode,
            "elapsed_seconds": elapsed,
            "output_summary": _verify_output_summary(result.stdout, result.stderr),
            # task-done-root-resolution AC4：记录 verify 实际执行位置（cwd），
            # reviewer 可仅凭 DONE 事件的 verify 摘要辨识 verify 跑在哪个工作树。
            "cwd": str(project_root),
            # AC1：DONE 存证落 HEAD 锚。取不到（git 不可用）时为 None，比对侧
            # 见「无锚」即 fail-open，行为退化为改动前（不告警、不阻断）。
            "head": verify_head,
        }
        return verify_record, None
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - started, 1)
        partial_out = _decode_subprocess_output(
            (exc.stdout or b"")[:300] if hasattr(exc, "stdout") else b""
        )
        early = _verify_failure_early_done(store, task_id, elapsed)
        if early is not None:
            early["note"] = "timeout_but_done_already_written"
            early["verify"] = {
                "timeout": verify_timeout,
                "elapsed_seconds": elapsed,
                "partial_stdout": partial_out,
            }
            return None, early
        raise _verify_timeout_error(verify_cmd, task_id, verify_timeout, elapsed,
                                    partial_out)


def rerun_verify_at_head(
    task_def: dict[str, Any],
    task_id: str,
    project_root: Path | None,
) -> dict[str, Any] | None:
    """按**当前**代码状态复跑 verify_command（task-review-verify-at-head AC2）。

    DONE 时跑的那一次 verify 只覆盖 DONE 那一刻的代码；审查认领前若又补交了
    提交，机器视角里那份存证就成了「凭空引用」。本函数产出一份**当前**状态下
    的同构记录，与 DONE 存证并列，让「补交的改动」重新回到机器验证范围内。

    与 :func:`_run_verify` 的三处刻意差异（本函数服务审查侧，不服务 done）：
    - **不写事件、不做假失败消除**（early_done）：只产出一份记录，不改状态机；
    - **失败/超时不抛** ``OrchdError``：复跑是**注解**不是门禁，失败只降级为
      ``ok=False``（超时带 ``timeout`` 字段），由调用方决定呈现方式（本卡口径：
      告警而非阻断，故不得打断「边审边补交」）；
    - 记录带 ``head`` 锚（与 DONE 存证同构），供两侧比对。

    Args:
        task_def: 任务定义（取 ``verify_command`` 与可选 ``verify_timeout_seconds``）。
        task_id: 任务 id（诊断字段；本函数不校验其状态）。
        project_root: 复跑目录，**须为任务 worktree**（container 布局下审查由
            主工作树发起 → 由调用方经 ``_resolve_review_worktree`` 解析后再传入，
            否则 verify 会跑在错误的目录里）。

    Returns:
        与 DONE 存证同构的记录（``ok`` / ``exit_code`` / ``elapsed_seconds`` /
        ``output_summary`` / ``cwd`` / ``head``）；超时返回 ``ok=False`` 记录；
        仅当 ``project_root`` 缺失或任务无 ``verify_command`` 时返回 None。
    """
    verify_cmd = task_def.get("verify_command")
    if not verify_cmd or project_root is None:
        return None
    # 封顶单次 verify 硬预算（rules/verify.md:13）：任务级 verify_timeout_seconds
    # 可配到 600s，但复跑发生在**审查认领**里，不是门禁点——一次认领最多只吃
    # 120s，不得让审查动作被一条拖长的 verify 挂住。
    verify_timeout = min(
        task_def.get("verify_timeout_seconds") or _VERIFY_TIMEOUT, _VERIFY_TIMEOUT)
    started = time.monotonic()
    try:
        # 经包模块取 run_shell：使测试 monkeypatch 传导到本模块
        from orchd.onboard import run_shell as _run_shell

        result = _run_shell(verify_cmd, str(project_root), verify_timeout)
        elapsed = round(time.monotonic() - started, 1)
        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "elapsed_seconds": elapsed,
            "output_summary": _verify_output_summary(result.stdout, result.stderr),
            "cwd": str(project_root),
            "head": _current_head(project_root),
        }
    except subprocess.TimeoutExpired as exc:
        elapsed = round(time.monotonic() - started, 1)
        partial = (exc.stdout or b"") if hasattr(exc, "stdout") else b""
        return {
            "ok": False,
            "timeout": verify_timeout,
            "elapsed_seconds": elapsed,
            "output_summary": _verify_output_summary(partial, b""),
            "cwd": str(project_root),
            "head": _current_head(project_root),
        }
    except Exception as exc:
        # 复跑是**注解**不是门禁：任何失败都不得逸出（OSError / 执行器内部异常
        # 等）。claim 侧虽由 run_guard 兜底，但本函数可被别处复用——让每个调用
        # 点都记得包 guard 是靠不住的约定，故在此就地降级成一条 ok=False 记录。
        elapsed = round(time.monotonic() - started, 1)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": elapsed,
            "cwd": str(project_root),
            "head": _current_head(project_root),
        }


def _verify_fail_error(
    verify_cmd: str,
    task_id: str,
    result: Any,
    elapsed: float,
) -> OrchdError:
    """构造 verify 非零退出的 E014 错误（含诊断增强出口）。"""
    _output_l = (
        _decode_subprocess_output(result.stdout).lower()
        + " " + _decode_subprocess_output(result.stderr).lower()
    )
    _hint = (
        "verify 失败可能因断言不匹配或环境问题；若为 pytest 超长执行，"
        "按 SKILL.md 自检约定改用模块定向 verify_command；"
        "若在 Windows 上 verify 失败，请确认已安装 Git Bash"
        "（verify_command 以 POSIX 语法经 Git Bash 执行）"
    )
    # task-pass9-verify-path-guard（pass9 实测）：exit 5 = "no tests ran"——
    # addopts 强制 xdist 下 verify_command 参数含不存在路径时 usage error 被
    # 整吞，合法测试静默不跑。失败已发生，把根因 hint 顶到最前。
    if result.returncode == 5 or "no tests ran" in _output_l:
        _hint = (
            "未收集到任何测试（exit 5）：核对 verify_command 路径拼写——"
            "addopts 强制 xdist 下参数含不存在路径的 usage error 会被整吞"
            "（task-pass9-verify-path-guard）；" + _hint
        )
    return OrchdError(
        ErrorCode.E014,
        f"verify_command_failed: exit code {result.returncode} after {elapsed}s",
        [{
            "command": verify_cmd,
            "returncode": result.returncode,
            "elapsed_seconds": elapsed,
            "stderr": _decode_subprocess_output(result.stderr)[:500],
            "stdout": _decode_subprocess_output(result.stdout)[:300],
            "hint": _hint,
        }],
    )


def _verify_timeout_error(
    verify_cmd: str,
    task_id: str,
    verify_timeout: int,
    elapsed: float,
    partial_out: str,
) -> OrchdError:
    """构造 verify 超时的 E014 错误（含超时诊断与定向 verify 指引）。"""
    return OrchdError(
        ErrorCode.E014,
        f"verify_command_failed: timeout after {verify_timeout}s"
        f"（实际执行 {elapsed}s，未完成）",
        [{
            "command": verify_cmd,
            "timeout": verify_timeout,
            "elapsed_seconds": elapsed,
            "partial_stdout": partial_out,
            "hint": (
                "verify 超时通常是全量 pytest 累计耗时超上限：按 SKILL.md 自检约定"
                "改用模块定向 verify_command（python -m pytest tests/test_<模块>.py "
                "--basetemp=...），并确认测试不触发沙箱拦截"
            ),
        }],
    )


def _verify_failure_early_done(
    store: Store,
    task_id: str,
    elapsed: float,
) -> dict[str, Any] | None:
    """假失败消除：verify 失败/超时但本次 attempt 的 DONE 已并发落地 → 复用成功。

    仅当任务已离开 claimed（本次 done 已写完 DONE）才复用；rework 场景（仍 claimed，
    ledger 残留上一次旧 DONE）是真实失败 → 返回 None（由调用方抛 E014），避免
    误复用上一次 DONE 掩盖真实失败（2026-08-12 实踩：rework 误触发）。

    注意：此处在 verify 期间实时扫描 ledger——并发落地的 DONE 不在 done 开头的
    缓存内（H2 缓存仅覆盖 claim/request/review_submit 等静态查询场景）。
    """
    fresh_state = store.replay()
    cur_ts = fresh_state.get(task_id)
    if cur_ts is None or cur_ts.status == "claimed":
        return None
    prior_done = _find_last_done_event(store, task_id)
    if prior_done is None:
        return None
    return {
        "done": True,
        "task_id": task_id,
        "status": "done",
        "attempt_count": prior_done.get("attempt_count", cur_ts.attempt_count + 1),
        "note": "verify_failed_but_done_already_written",
        "ledger_timestamp": prior_done.get("timestamp"),
        "event_id": prior_done.get("event_id"),
    }
