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
from orchd.worktree import resolve_master_path as _master_path

# 模块级常量：迁移自 orchd/onboard.py（原行号 123）
# task-release-gate-blocking（2026-09-19）：300 → 600s。依据 = 全量实测
# 251.8 / 273.9 / 322.3 / 351.5s（同一套件多 worker 下波动约 ±20%），300s 会被随机打爆
# （超时表现为「假红」，比慢更伤门禁可信度）；600s ≈ 最差观测的 1.7x，且与 amend 预算通道
# 的上界一致（>10 分钟视为阻塞过久）。耗时另落盘（见 _record_done_run），便于「该抬上限 or
# 该优化」用数据判断而非等超时。
_FULL_REGRESSION_TIMEOUT = 600


def _full_regression_enabled(store: Store) -> bool:
    """config.full_regression_on_done 是否显式开启（缺省/读失败 → False）。"""
    try:
        _mp = _master_path(store)
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
            _record_done_run(store, {
                "status": "passed",
                "elapsed_seconds": reg_elapsed,
                "timeout": _FULL_REGRESSION_TIMEOUT,
            })
            return {
                "ok": True,
                "status": "passed",
                "elapsed_seconds": reg_elapsed,
                "output_summary": _verify_output_summary(reg_result.stdout, reg_result.stderr),
            }
        _record_done_run(store, {
            "status": "failed",
            "elapsed_seconds": reg_elapsed,
            "timeout": _FULL_REGRESSION_TIMEOUT,
            "returncode": reg_result.returncode,
        })
        return {
            "ok": False,
            "status": "failed",
            "code": "full_regression",
            "severity": "warning",
            "elapsed_seconds": reg_elapsed,
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
        _record_done_run(store, {
            "status": "timeout",
            "elapsed_seconds": reg_elapsed,
            "timeout": _FULL_REGRESSION_TIMEOUT,
        })
        return {
            "ok": False,
            "status": "timeout",
            "code": "full_regression",
            "severity": "warning",
            "elapsed_seconds": reg_elapsed,
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


def _record_done_run(store: Store, payload: dict[str, Any]) -> None:
    """把本次 done 侧全量回归的观察值并入 ``.orchd/_full_regression.json``（best-effort）。

    为什么落盘（task-release-gate-blocking AC3）：套件在长，预算是否还够需要**机器信号**——
    每次记录 ``status`` / ``elapsed_seconds`` / ``timeout``，才能在「该抬上限 or 该优化速度」
    时用数据判断，而不是等超时随机假红。只追加 ``last_done_run`` 字段，不动该文件的既有字段
    （``last_pass_commit`` / ``passed_at`` 由 ``full-regression`` 命令维护）。任何失败都不抛出：
    记录耗时不得影响 done 主流程。
    """
    try:
        import json as _json

        path = _master_path(store).parent / "_full_regression.json"
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = _json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except ValueError:
                data = {}
        record = dict(payload)
        record["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data["last_done_run"] = record
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _has_engine_files(files_to_edit: list[str]) -> bool:
    """files_to_edit 是否含核心引擎 Python 文件（orchd/ 或 .orchd/orchd/ 下）。"""
    return any(
        (f.startswith("orchd/") or f.startswith(".orchd/orchd/")) and f.endswith(".py")
        for f in files_to_edit
    )