"""Orchd 任务生命周期管理 - lifecycle/regression 域。

迁移自 orchd/onboard.py（task-split-onboard-lifecycle-core）：
  - _full_regression_enabled: 全量回归是否开启
  - _maybe_full_regression: 全量回归执行
  - _has_engine_files: 是否含引擎文件
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops_ops import (
    decode_subprocess_output as _decode_subprocess_output,
    verify_output_summary as _verify_output_summary,
)
from orchd.ledger import Store
from orchd.subproc import run_shell

# 模块级常量：迁移自 orchd/onboard.py（原行号 123）
_FULL_REGRESSION_TIMEOUT = 300


def _full_regression_enabled(store: Store) -> bool:
    """config.full_regression_on_done 是否显式开启（缺省/读失败 → False）。"""
    try:
        _mp = store.orchd_dir / "_master.json"
        if _mp.exists():
            import json as _json17
            _master_cfg = _json17.loads(_mp.read_text(encoding="utf-8"))
            _fr_val = (_master_cfg.get("config") or {}).get("full_regression_on_done")
            if _fr_val is not None:
                return bool(_fr_val)
    except (OSError, ValueError):
        pass
    return False


def _maybe_full_regression(
    store: Store,
    files_to_edit: list[str],
    project_root: Path | None,
) -> dict[str, Any] | None:
    """S-A2 阶段 5：全量回归（task-full-regression-gate-r2，默认关闭）。

    files_to_edit 含 orchd/*.py（核心引擎）且 config.full_regression_on_done 显式
    true 时，done verify 通过、自动提交后锁外附加一次全量 pytest 冒烟，防止契约
    漂移在合并时静默通过。失败仅生成本次 DONE 的 full_regression 警告，不阻断
    done、不改任务状态。缺省/显式 false 时跳过回归段，响应无 full_regression 字段。
    """
    if not project_root:
        return None
    if not (_full_regression_enabled(store) and _has_engine_files(files_to_edit)):
        return None

    reg_started = time.monotonic()
    try:
        # 全量回归走专属固定 basetemp（独占通道，一次一人跑，固定路径安全），
        # 并行度收敛到 -n 8（本机 16 核的一半），加 --max-worker-restart 兜底
        # worker 崩溃自动重启，避免主进程等僵尸 worker 挂死。
        # 日常 pytest 路径不受影响（多 agent 并发跑各自 numbered tmpdir）。
        reg_cmd = (
            f'"{sys.executable}" -m pytest tests/ -q -n 8 '
            f'--max-worker-restart=5 '
            f'--basetemp=C:/Temp/orchd-fr-baseline-$$'
        )
        reg_result = run_shell(reg_cmd, str(project_root), _FULL_REGRESSION_TIMEOUT)
        reg_elapsed = round(time.monotonic() - reg_started, 1)
        if reg_result.returncode == 0:
            return {
                "ok": True,
                "elapsed_seconds": reg_elapsed,
                "output_summary": _verify_output_summary(reg_result.stdout, reg_result.stderr),
            }
        return {
            "ok": False,
            "code": "full_regression",
            "severity": "warning",
            "message": (
                f"full_regression_failed: exit code {reg_result.returncode} "
                f"after {reg_elapsed}s"
            ),
            "details": {
                "command": f'"{sys.executable}" -m pytest tests/ -q',
                "returncode": reg_result.returncode,
                "elapsed_seconds": reg_elapsed,
                "output_summary": _verify_output_summary(reg_result.stdout, reg_result.stderr),
            },
        }
    except subprocess.TimeoutExpired as exc:
        reg_elapsed = round(time.monotonic() - reg_started, 1)
        partial_out = _decode_subprocess_output(
            (exc.stdout or b"")[:300] if hasattr(exc, "stdout") else b""
        )
        return {
            "ok": False,
            "code": "full_regression",
            "severity": "warning",
            "message": (
                f"full_regression_timeout: after {reg_elapsed}s "
                f"(timeout={_FULL_REGRESSION_TIMEOUT}s)"
            ),
            "details": {
                "command": f'"{sys.executable}" -m pytest tests/ -q',
                "timeout": _FULL_REGRESSION_TIMEOUT,
                "elapsed_seconds": reg_elapsed,
                "partial_stdout": partial_out,
            },
        }


def _has_engine_files(files_to_edit: list[str]) -> bool:
    """files_to_edit 是否含核心引擎 Python 文件（orchd/ 或 .orchd/orchd/ 下）。"""
    return any(
        (f.startswith("orchd/") or f.startswith(".orchd/orchd/")) and f.endswith(".py")
        for f in files_to_edit
    )