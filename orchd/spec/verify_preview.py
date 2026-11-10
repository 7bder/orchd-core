"""verify_command 可执行性预演（单一实现，task-engine-defect-fix-unified-u7）。

根因：verify_command 的可执行性只在注册期 dry-run 判一次，结果不随工作树重算；
``expected_pending``（本任务即将创建目标文件）被当成放行，claim 之后没有任何环节
复查目标文件是否已创建；注册期的 30s 预演又把 ``TimeoutExpired`` 交给
``except (SubprocessError, OSError): continue`` 静默吞掉，任务直接从
``verify_dry_run`` 结果里消失，agent 完全看不到「为什么没被预演」。

本模块把判定收敛为唯一预演实现，两处消费：

1. **注册期**（``amend_register_gate``）：:func:`preview_verify_command` 真实执行
   verify_command 并分类失败（复用 ``orchd.split.classify_dry_run_failure``）；
   超时不再吞掉，记 ``classification="timeout"`` / ``reason="timeout"`` 并保留在
   结果列表中（AC3）。
2. **claim 期**（``onboard.claim.precheck``）：:func:`claim_verify_preview` 纯静态
   判定命令引用的目标文件是否就绪——``ready`` / ``expected_pending``（已在
   files_to_edit 声明，提示先创建 verify 目标文件）/ ``missing_target``（缺失且
   未声明，给修正方向）。claim 期不跑子进程（不能给每次认领加 30s 开销）。

依赖方向：spec.verify_preview → spec.verify_cmd（同包，函数内惰性）、
spec.declarations（声明覆盖判定）、subproc / split（执行与分类）。除同包外全部
函数内惰性导入，deps.json 记 delayed 边。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

# 注册期真实预演的超时预算（秒；与历史 amend_register_gate 的 30s 一致）。
PREVIEW_TIMEOUT_SECONDS = 30

# claim 期静态预演的三类结果。
CLAIM_READY = "ready"
CLAIM_EXPECTED_PENDING = "expected_pending"
CLAIM_MISSING_TARGET = "missing_target"

# 注册期阻断类失败（与历史 _E028 阻断集合一致；timeout / env_error 非阻断仅留痕）。
BLOCKING_FAILURE_CLASSES = frozenset({
    "assertion_mismatch", "usage_error", "invalid_shell", "internal_error",
})

# verify_command 里的可执行目标（.py 路径）；排除 --basetemp 等参数值里不含 .py 的片段。
_TARGET_RE = re.compile(r"""["']?((?:[A-Za-z0-9_.\-]+[\\/])*[A-Za-z0-9_.\-]+\.py)["']?""")


def _decode(raw: Any) -> str:
    """把 run_shell 的 bytes/str 输出统一成 str（截断由调用方负责）。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _norm_path(path: Any) -> str:
    """声明/引用的路径归一：反斜杠转正斜杠，去前导 ./ 与分隔符。"""
    text = str(path).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def verify_target_paths(verify_cmd: str) -> list[str]:
    """从 verify_command 中提取它引用的 .py 目标路径（保序去重）。

    Args:
        verify_cmd: verify_command 定义。

    Returns:
        归一化后的相对路径列表；无引用返回空列表。
    """
    seen: list[str] = []
    for match in _TARGET_RE.finditer(verify_cmd or ""):
        path = _norm_path(match.group(1))
        if path and path not in seen:
            seen.append(path)
    return seen


def _path_exists(project_root: Path | None, rel_path: str) -> bool:
    """目标路径是否已存在（兼容 container 布局：工作树根下再套 main/）。"""
    if project_root is None:
        return True
    root = Path(project_root)
    if (root / rel_path).exists():
        return True
    return (root / "main" / rel_path).exists()


def _covered_by_declared(rel_path: str, declared: Iterable[str]) -> bool:
    """目标路径是否被 files_to_edit 声明覆盖（文件/目录/等价覆盖同一判定源）。"""
    declared_list = [str(p) for p in declared]
    if not declared_list:
        return False
    try:
        from orchd.spec.declarations import is_path_covered

        return any(is_path_covered(p, rel_path) for p in declared_list)
    except Exception:
        # 兜底：声明层不可用时退化为前缀比较（不放大放行范围）。
        norm = _norm_path(rel_path)
        return any(
            norm == _norm_path(p) or norm.startswith(_norm_path(p) + "/")
            for p in declared_list
        )


def claim_verify_preview(
    task_def: dict[str, Any] | None,
    project_root: Path | None,
) -> dict[str, Any]:
    """claim 期 verify 可执行性预演（纯静态，不执行子进程）。

    Args:
        task_def: 任务定义（读取 verify_command / files_to_edit）。
        project_root: 工作树根（None 时视为全部就绪，不误报）。

    Returns:
        ``{ok, classification, hint, verify_command, targets,
        missing_targets, expected_pending}``：
        - ``ready``：引用的目标文件均已存在；
        - ``expected_pending``：缺失但已在 files_to_edit 声明（提示先创建）；
        - ``missing_target``：缺失且未声明（提示修正 verify_command / 补声明）。
    """
    task_def = task_def or {}
    verify_cmd = str(task_def.get("verify_command") or "")
    targets = verify_target_paths(verify_cmd)
    declared = [str(p) for p in (task_def.get("files_to_edit") or [])]
    missing = [t for t in targets if not _path_exists(project_root, t)]
    preview: dict[str, Any] = {
        "ok": not missing,
        "classification": CLAIM_READY,
        "hint": "",
        "verify_command": verify_cmd,
        "targets": targets,
        "missing_targets": [],
        "expected_pending": [],
    }
    if not missing:
        preview["hint"] = "verify 命令引用的目标文件均已存在"
        return preview
    pending = [t for t in missing if _covered_by_declared(t, declared)]
    unexpected = [t for t in missing if t not in pending]
    preview["missing_targets"] = unexpected
    preview["expected_pending"] = pending
    if pending and not unexpected:
        preview["classification"] = CLAIM_EXPECTED_PENDING
        preview["hint"] = (
            "先创建 verify 目标文件："
            + "、".join(pending)
            + "（已在 files_to_edit 声明，属预期待创建；实现完成后重跑 claim/done）"
        )
        return preview
    preview["classification"] = CLAIM_MISSING_TARGET
    preview["hint"] = (
        "verify_command 引用的目标不存在且未在 files_to_edit 声明："
        + "、".join(unexpected)
        + "——请修正 verify_command，或把目标文件加入 files_to_edit 后 amend"
    )
    return preview


def preview_verify_command(
    verify_cmd: str,
    project_root: Path | None,
    *,
    files_to_edit: Iterable[str] = (),
    timeout: int | None = None,
) -> dict[str, Any]:
    """注册期真实预演：执行 verify_command 并分类结果（单一实现）。

    Args:
        verify_cmd: verify_command 定义。
        project_root: 执行 cwd（与 done 期一致）。
        files_to_edit: 本任务声明（expected_pending 判定用）。
        timeout: 超时预算（秒），None 用 :data:`PREVIEW_TIMEOUT_SECONDS`。

    Returns:
        ``{ok, classification, reason, exit_code, stdout, stderr, hint, timed_out}``。
        超时时 classification/reason 均为 ``timeout``（不再静默吞掉）。
    """
    from orchd.spec.verify_cmd import verify_command_dangerous_reasons

    dangerous = verify_command_dangerous_reasons(verify_cmd)
    if dangerous:
        return {
            "ok": False,
            "classification": "unsafe_shell",
            "reason": "unsafe_shell",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "reasons": list(dangerous),
            "hint": "verify_command 含 shell 注入风险段（E027），预演拒绝执行",
        }

    import subprocess

    from orchd.subproc import run_shell

    budget = PREVIEW_TIMEOUT_SECONDS if timeout is None else int(timeout)
    try:
        proc = run_shell(verify_cmd, str(project_root) if project_root else None,
                         budget)
    except subprocess.TimeoutExpired:
        # AC3：TimeoutExpired 记 reason=timeout，调用方必须把它留在结果列表里。
        return {
            "ok": False,
            "classification": "timeout",
            "reason": "timeout",
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "hint": (
                f"verify_command 预演超时（>{budget}s）——命令过重或卡住；"
                "请缩到目标用例（-k / 单文件），任务仍保留在本次预演结果中"
            ),
        }
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "ok": False,
            "classification": "env_error",
            "reason": "env_error",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "hint": "预演运行环境异常（shell/解释器不可用），未执行判定",
        }

    stderr = _decode(getattr(proc, "stderr", None))[:500]
    stdout = _decode(getattr(proc, "stdout", None))[:300]
    exit_code = getattr(proc, "returncode", None)
    if exit_code == 0:
        return {
            "ok": True,
            "classification": "ok",
            "reason": "ok",
            "exit_code": 0,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
            "hint": "",
        }

    from orchd.split import classify_dry_run_failure

    failure_class = classify_dry_run_failure(
        verify_cmd, int(exit_code or 0), stderr, stdout,
        set(str(p) for p in files_to_edit) or None,
    )
    blocking = failure_class in BLOCKING_FAILURE_CLASSES
    return {
        "ok": False,
        "classification": failure_class,
        "reason": f"dry_run_{failure_class}" if blocking else failure_class,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": False,
        "blocking": blocking,
        "hint": "",
    }
