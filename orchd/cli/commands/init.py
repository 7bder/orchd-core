"""Orchd CLI 路由：init 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-query-init-misc）：
  - _cmd_validate: validate 命令（校验 _master.json）
  - _cmd_bootstrap: bootstrap 命令（输出分解套件 JSON）
  - _cmd_init: init 命令（初始化 .orchd/ 并生成 snapshot）

3a 阶段说明：本模块是 init 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

import json
import sys
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



def _cmd_validate(args) -> dict:
    """校验 _master.json 的结构与引用完整性。

    CLI 参数: args.path — master 文件路径（默认 .orchd/_master.json）。
    返回: {"valid": True/False, "errors": [...], "warnings": [...]}。

    注（2026-08-14 发版清理批次）：对终态（completed/cancelled）任务豁免
    E029（粒度拆分建议）与 E023（模糊词）历史残留——拆分/改写验收标准对
    已完成任务无意义，且其核心字段（files_to_edit / acceptance_criteria）
    受 E007 终态保护无法改写。豁免按当前状态动态生效：任务被 force-status
    重置回 pending 后豁免自动失效，不掩盖新任务的质量问题。
    """
    import re

    from orchd.ledger import Store
    from orchd.spec import (
        layout_marker_warnings,
        load_master,
        roadmap_landing_warnings,
        validate_quality,
        validate_references,
        validate_structure,
    )
    from orchd.worktree import resolve_canonical_project_root

    # task-master-single-copy：container 任务 worktree 已抑制副本，本地无 _master.json
    # 时回退 canonical 主工作树（唯一权威）；flat 布局 canonical == 本地，零回归。
    master_path = Path(args.path)
    if not master_path.exists():
        cand = resolve_canonical_project_root(master_path.parent.parent) / ".orchd" / "_master.json"
        if cand.exists():
            master_path = cand

    master = load_master(str(master_path))
    structure_errors = validate_structure(master) + validate_references(master)
    quality_warnings = validate_quality(master)  # E022/E023/E024 为质量告警，不判 invalid

    # 终态任务集合；无可用 ledger（新项目 / replay 失败）时跳过过滤，validate 保持可运行。
    terminal_ids: set[str] | None = None
    try:
        state = Store(master_path.parent).replay()
        terminal_ids = {tid for tid, ts in state.items() if ts.status in ("completed", "cancelled")}
    except Exception:
        terminal_ids = None

    def _keep_quality_warning(e) -> bool:
        if terminal_ids is None or e.code.name not in ("E029", "E023"):
            return True
        m = re.match(r"\$\.tasks\[(\d+)\]", e.path or "")
        if not m:
            return True
        idx = int(m.group(1))
        tid = master.tasks[idx].get("id") if idx < len(master.tasks) else None
        return tid not in terminal_ids

    quality_warnings = [e for e in quality_warnings if _keep_quality_warning(e)]

    # intake-dual-path（2026-08-15）：ROADMAP 规划章节落地兜底（E031 告警，不判 invalid）。
    # 独立追加：E031 非任务级质量项，不参与终态豁免过滤；dict 结构，与 ValidationError 并存。
    quality_warnings += roadmap_landing_warnings(master_path.parent)
    # task-14-worktree-layout：双布局标记校验（LAYOUT 告警，不判 invalid；缺失自动探测 + 告警）。
    # 入参为项目根（master 目录的父级）；container 下为 <容器>/main。
    quality_warnings += layout_marker_warnings(master_path.parent.parent)

    def _warn_dict(e) -> dict:
        """把 ValidationError 或告警 dict 归一化为输出结构（含 roadmap-land E031 dict）。"""
        if isinstance(e, dict):
            return {"code": e.get("code"), "path": e.get("path"), "message": e.get("message")}
        return {"code": e.code.name, "path": e.path, "message": e.message}

    from orchd.guide import annotate_validation_items

    errors_list = [{"code": e.code.name, "path": e.path, "message": e.message} for e in structure_errors]
    warnings_list = [_warn_dict(e) for e in quality_warnings]

    if structure_errors:
        return {
            "valid": False,
            "errors": annotate_validation_items(errors_list, master_path.parent),
            "warnings": annotate_validation_items(warnings_list, master_path.parent),
        }
    return {
        "valid": True,
        "errors": [],
        "warnings": annotate_validation_items(warnings_list, master_path.parent),
    }

def _cmd_bootstrap(args) -> dict:
    """输出任务分解套件 JSON（供新 agent 接入时使用）。

    CLI 参数: 无。
    返回: bootstrap() 生成的套件字典。
    """
    from orchd.onboard import bootstrap

    return bootstrap()

def _cmd_init(args) -> dict:
    """初始化 .orchd/ 目录：从 master 生成 snapshot + 空 ledger + checkpoint。

    CLI 参数: args.master — master 文件路径（默认 .orchd/_master.json）。
    返回: {"initialized": True, "created_files": [...]}。

    1.4 双布局（task-14-worktree-layout，AC2/AC3/AC5）：
    - 新项目（master 不存在）→ 默认 container：自建默认 master + ``main/`` +
      ``.orchd-runtime/`` + 布局标记（零额外操作）；
    - 既有项目（master 已存在）→ flat：维持现状零回归，仅补写 flat 布局标记。
    """
    from orchd.spec import load_master
    from orchd.split import init
    from orchd.worktree import bootstrap_container, read_layout, write_layout
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    master_path = Path(args.master).resolve()
    orchd_dir = master_path.parent
    project_root = orchd_dir.parent

    # 初始化串行化（task-admission-lock-engine：E 项）—— 并发 orchd init 竞态防护。
    # 关键约束：锁必须加在「稳定、不会被 shutil.move 搬动」的路径上，否则 Windows
    # 会因锁文件被持有时无法 rename 目录而报 WinError 5/33（E999）。
    # - 新项目（无 master）→ 串行化交由 bootstrap_container：它锁最终稳定的
    #   main/.orchd/.intake.lock（该目录在 git init / move 之后才存在，move 前尚无）；
    # - 既有项目（master 已存在）→ 对稳定存在的 orchd_dir 加 .intake.lock。
    # 两路径不同、互不嵌套，既避免自死锁，也避免锁文件被 move 搬动。
    if not master_path.exists():
        # 新项目（无 master）→ 默认 container（AC3）；串行化交予 bootstrap_container。
        boot = bootstrap_container(project_root, master_path)
        orchd_dir = Path(boot["main_worktree"]) / ".orchd"
        master = load_master(orchd_dir / "_master.json")
        result = init(orchd_dir, master)
        result["container"] = boot["container"]
        result["main_worktree"] = boot["main_worktree"]
        result["runtime_root"] = boot["runtime_root"]
        result["marker"] = boot["marker"]
        result["created_files"] = boot["created"] + result.get("created_files", [])
        return result

    # 既有项目（master 已存在）→ flat（AC5 零回归）；标记缺失时补写 flat 标记（AC2）。
    # 串行化：对稳定存在的 orchd_dir 加 .intake.lock（不会被移动）。
    lk = None
    try:
        lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
        if read_layout(orchd_dir) is None:
            write_layout(orchd_dir, "flat", project_root)
        master = load_master(master_path)
        return init(orchd_dir, master)
    finally:
        if lk is not None:
            intake_lock_release(lk)


def register(sub) -> None:
    """注册 init 模块的子命令。"""
    # validate
    p = sub.add_parser("validate", help="校验 _master.json")
    p.add_argument("path", nargs="?", default=".orchd/_master.json")
    p.set_defaults(func=_cmd_validate)

    # bootstrap
    p = sub.add_parser("bootstrap", help="输出分解套件 JSON")
    p.set_defaults(func=_cmd_bootstrap)

    # init
    p = sub.add_parser("init", help="初始化 .orchd/ 并生成 snapshot")
    p.add_argument("--master", default=".orchd/_master.json")
    p.set_defaults(func=_cmd_init)

