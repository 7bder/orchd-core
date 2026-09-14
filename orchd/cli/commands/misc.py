"""Orchd CLI 路由：misc 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-query-init-misc）：
  - _cmd_full_regression: full-regression 命令（全量回归并记录 last_pass_commit）
  - _cmd_layout_migrate: layout-migrate 命令（flat → container 布局迁移）
  - _cmd_intake: intake 命令（提交摄入产物并校验状态合法性）
  - _cmd_roadmap_land: roadmap-land 命令（ROADMAP 规划章节 → IDEAS pending 落地）

3a 阶段说明：本模块是 misc 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)



def _cmd_full_regression(args) -> tuple[dict, int]:
    """跑全量 pytest 并通过后写 .orchd/_full_regression.json（task-full-regression-gate-r2）。

    全量回归通过 → 记录 last_pass_commit=当前 HEAD + passed_at；失败不写通过标记、
    返回非零退出码（供 sync_orchd_core.sh 发版前检查消费）。

    test-suite-slim §5.3 修复三处缺陷：
    - Python 解释器路径反斜杠转正斜杠，避免 Windows Git Bash 双引号内反斜杠被
      当转义符吞掉（嵌套引号 bug）；
    - basetemp 落**系统临时目录**下的固定可复用子目录（task-release-chain-hardening），
      不能放项目内：旧版 ``build/fullreg-basetemp`` 使 pytest ``tmp_path`` 进入仓库，
      canonical root 解析据此爬到真实 ``.orchd``，同一条命令实测 140 failed；出仓后
      0 failed。固定子目录而非 ``$$``：full-regression 是发版门禁、一次一人跑，
      固定路径可复用，避免 ``$$`` 每次新建且从不清理导致临时目录膨胀；
    - 显式 ``-c pyproject.toml`` 确保读到 addopts 的 ``-n auto --dist=loadscope``
      并行配置，不依赖 shell cwd 推断 rootdir。
    """
    import subprocess
    import time
    from datetime import datetime, timezone

    from orchd.subproc import run_shell

    project_root = Path(args.path).resolve() if args.path else Path.cwd()
    orchd_dir = project_root / ".orchd"
    # basetemp 出仓到系统临时目录（task-release-chain-hardening）：若落在项目内，
    # pytest tmp_path 进入仓库会污染 canonical root 解析（实测 140 failed）。
    basetemp = Path(tempfile.gettempdir()) / "orchd-fullreg-basetemp"
    basetemp.mkdir(parents=True, exist_ok=True)
    # Git Bash 双引号内反斜杠会被当转义符；统一正斜杠（POSIX 路径本无反斜杠，无副作用）
    py = sys.executable.replace("\\", "/")
    basetemp_arg = str(basetemp).replace("\\", "/")
    reg_cmd = (
        f'"{py}" -m pytest tests/ -q -c pyproject.toml '
        f"--basetemp={basetemp_arg}"
    )
    reg_started = time.monotonic()
    try:
        reg_result = run_shell(reg_cmd, str(project_root), 600)
    except subprocess.TimeoutExpired:
        reg_elapsed = round(time.monotonic() - reg_started, 1)
        return {
            "ok": False,
            "code": "full_regression_timeout",
            "message": f"全量回归超时（600s）after {reg_elapsed}s",
        }, 1
    reg_elapsed = round(time.monotonic() - reg_started, 1)
    if reg_result.returncode != 0:
        return {
            "ok": False,
            "code": "full_regression_failed",
            "message": f"exit code {reg_result.returncode} after {reg_elapsed}s",
            "details": {
                "returncode": reg_result.returncode,
                "elapsed_seconds": reg_elapsed,
            },
        }, 1
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(project_root),
        capture_output=True, text=True,
    ).stdout.strip()
    payload = {
        "last_pass_commit": head,
        "passed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": "python -m pytest tests/ -q -c pyproject.toml",
    }
    orchd_dir.mkdir(exist_ok=True)
    (orchd_dir / "_full_regression.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "ok": True,
        "last_pass_commit": head,
        "elapsed_seconds": reg_elapsed,
        "note": f"已写入 {orchd_dir / '_full_regression.json'}",
    }, 0

def _cmd_layout_migrate(args) -> dict:
    """flat → container 布局迁移（task-14-worktree-layout，AC4）。

    CLI 参数: args.path（flat 主工作树根，默认当前目录）。
    返回: 迁移结果字典（migrated / main_worktree / moved / marker /
    runtime_root / 可选 reason + hint）。
    """
    from orchd.worktree import layout_migrate

    return layout_migrate(Path(args.path))

def _cmd_intake(args) -> dict:
    """提交摄入产物（IDEAS.md / ROADMAP.md）并校验条目状态（intake-commit-enforcement）。

    CLI 参数: 无（项目根由 .orchd/ 定位）。
    返回: 提交结果字典（committed / commit / 可选 status_warnings / commit_warning）。
    """
    from orchd.intake import intake_commit

    orchd_dir = _find_orchd_dir()
    return intake_commit(orchd_dir.parent)

def _cmd_roadmap_land(args) -> dict:
    """为 ROADMAP 规划章节生成 IDEAS pending 落地条目（intake-dual-path）。

    CLI 参数: args.version — 规划章节版本（如 1.3）。
    返回: 落地结果字典（landed / version / section_id / commit）。
    """
    from orchd.intake import roadmap_land

    orchd_dir = _find_orchd_dir()
    return roadmap_land(orchd_dir.parent, args.version)


def register(sub) -> None:
    """注册 misc 模块的子命令。"""
    # layout-migrate（task-14-worktree-layout）：flat → container 迁移
    p = sub.add_parser(
        "layout-migrate", help="flat → container 布局迁移（保留 git 历史、可回滚）"
    )
    p.add_argument("--path", default=".", help="flat 主工作树根，默认当前目录")
    p.set_defaults(func=_cmd_layout_migrate)

    # full-regression（task-full-regression-gate-r2）：全量回归并记录 last_pass_commit
    p = sub.add_parser(
        "full-regression",
        help="跑全量 pytest 并通过后写 .orchd/_full_regression.json（last_pass_commit）；失败不写通过标记",
    )
    p.add_argument("--path", default=None, help="项目根目录，默认当前目录")
    p.set_defaults(func=_cmd_full_regression)

    # intake（2026-08-14 intake-commit-enforcement）
    p = sub.add_parser("intake", help="提交摄入产物（IDEAS.md；ROADMAP.md 不纳入 git，自动跳过）并校验状态合法性")
    p.set_defaults(func=_cmd_intake)

    # roadmap-land（2026-08-15 intake-dual-path）：ROADMAP 规划章节 → IDEAS pending 落地
    p = sub.add_parser("roadmap-land", help="为 ROADMAP 规划章节生成 IDEAS pending 落地条目")
    p.add_argument("version", help="规划章节版本（如 1.3，匹配 ROADMAP ## 版本 章节头）")
    p.set_defaults(func=_cmd_roadmap_land)

