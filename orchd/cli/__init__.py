"""Orchd CLI 路由：入口 + 辅助。

迁移自 orchd/cli.py（task-split-cli-init-skeleton）：
  - main: CLI 入口
  - _load_tasks: 加载 master 并返回 (tasks, orchd_dir, master)
  - _maybe_archive_ideas: 任务进入终态后触发 IDEAS 归档
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orchd import __version__
from orchd.errors import ErrorCode, OrchdError, to_json_response

# 从同模块子包导入
from orchd.cli.skeleton import (
    _cli_skeleton,
    _output,
    _fix_windows_console_encoding,
)


def _load_tasks(master_path: str | None = None) -> tuple[list, Path, Any]:
    """加载 master 并返回 (tasks, orchd_dir, master)。

    canonical-master-read（task-canonical-project-root）：未显式指定
    ``master_path`` 时，master 任务定义统一从 canonical 主工作树
    ``.orchd/_master.json`` 读取（``resolve_canonical_project_root`` 定位），
    避免任务 worktree 本地 checkout 副本与主工作树不同步导致的任务池不一致
    （pool/status/request 等业务读与 main 一致）。flat 布局 canonical == 本地
    → 零回归。返回的 ``orchd_dir`` 保持当前 worktree 定位语义不变（Store 账本
    经 ``resolve_store_dir`` 解析到共享账本根；project_root 物理操作基准不受影响）。
    """
    from orchd.spec import load_master
    from orchd.worktree import resolve_canonical_project_root

    if master_path:
        path = Path(master_path)
        orchd_dir = path.parent
    else:
        orchd_dir = _find_orchd_dir()
        canonical_root = resolve_canonical_project_root(orchd_dir.parent)
        path = canonical_root / ".orchd" / "_master.json"
    master = load_master(path)
    return master.tasks, orchd_dir, master


def _maybe_archive_ideas(orchd_dir: Path) -> dict:
    """best-effort：任务进入终态后触发 IDEAS 归档并自动提交。

    加载 master → 调 ``archive_resolved_ideas`` → 若有归档条目则
    ``ensure_committed([IDEAS.md, IDEAS-archive.md])``。非 main 分支降级
    为不提交（对齐 amend 的 ``not_on_main`` 语义），避免把归档提交进任务分支。
    任何异常静默降级，不阻断调用方。

    container 终态回收守卫（task-review-archive-crash-guard）：review code
    APPROVED 终态回收会删除正在运行的任务 worktree（含 orchd/ 源码）。此后
    ``_cmd_review`` 调本函数时，``orchd_dir`` 与其下的 orchd.ideas 等源码模块
    已从磁盘消失——须在懒加载前先判 ``orchd_dir`` 是否仍存在，否则懒加载
    ``from orchd.ideas import ...`` 抛 ModuleNotFoundError、命令 exit 1、
    best-effort 分支清理被中断。此处降级跳过，并保证懒加载/归档任意异常
    静默降级（对齐"任何异常静默降级"契约）。

    Returns:
        归档结果；若无可归档条目或异常，返回 ``{"archived": [], ...}``。
    """
    # 前置守卫：worktree 已终态回收 → orchd_dir（含 orchd/ 源码）消失，
    # 必须在懒加载之前降级，避免 ModuleNotFoundError 阻断调用方。
    if not orchd_dir.exists():
        return {"archived": [], "kept": 0, "skipped": "worktree_recycled"}
    master_path = orchd_dir / "_master.json"
    if not master_path.exists():
        return {"archived": [], "kept": 0, "skipped": "no_master"}
    try:
        from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
        from orchd.ideas import archive_resolved_ideas
        from orchd.spec import load_master

        master = load_master(master_path)
    except Exception:
        return {"archived": [], "kept": 0, "skipped": "archive_error"}
    project_root = orchd_dir.parent
    try:
        result = archive_resolved_ideas(project_root, master)
    except Exception:
        return {"archived": [], "kept": 0, "skipped": "archive_error"}
    if result.get("archived"):
        current_branch = get_current_branch(project_root)
        default_branch = get_default_branch(project_root) or "main"
        if current_branch is not None and current_branch != default_branch:
            result["commit"] = {
                "performed": False,
                "reason": "not_on_main",
                "branch": current_branch,
            }
        else:
            # AC3（task-12-engine-path-abstraction）：工作区文档走统一工作区根
            # helper（默认 .orchd/，兼容旧根路径）——ensure_committed 用相对
            # project_root 的路径，工作区根为 .orchd/ 时路径前缀 .orchd/。
            from orchd.ledger import resolve_workspace_root
            ws_root = resolve_workspace_root(project_root)

            def _rel(name: str) -> str:
                return str((ws_root / name).relative_to(project_root))

            result["commit"] = ensure_committed(
                project_root,
                [_rel("IDEAS.md"), _rel("IDEAS-archive.md")],
                "chore(ideas): 自动归档已完结条目",
            )
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回 exit code。

    命令处理函数可返回 dict（自动 JSON 输出，exit code 0）或
    ``(dict, exit_code)`` 元组（JSON 输出 + 自定义 exit code），
    例如 watchdog 在检测到僵死任务时返回 ``(result, 1)``。
    """
    _fix_windows_console_encoding()
    _auto_inject_session_id()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 2

    command = _command_name(args)
    try:
        _reject_container_root_cwd()   # 纪律护栏：容器根拒绝（E036）
        result = args.func(args)
        if result is None:
            return 0
        # 支持命令返回 (dict, exit_code) 元组
        if isinstance(result, tuple):
            data, code = result
            data = _attach_guidance(data, command, guidance_mode=getattr(args, "guidance", "slim"))
            _output(data)
            _emit_guidance(data)
            return code
        data = _attach_guidance(result, command, guidance_mode=getattr(args, "guidance", "slim"))
        _output(data)
        _emit_guidance(data)
        return 0
    except OrchdError as exc:
        resp = to_json_response(exc)
        # task-guidance-rule-summary：错误响应也附加恢复指引（只提示不代行）
        from orchd.guide import attach_error_guidance
        resp = attach_error_guidance(resp, exc.code.name, _find_orchd_dir())
        _output(resp)
        _emit_guidance(resp)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # P1-6：兜底捕获意外异常，保证 stdout 恒为可解析 JSON
        import traceback
        traceback.print_exc()  # traceback 只进 stderr，不污染 stdout
        _output({
            "error": {
                "code": "E999",
                "message": f"unexpected_error: {exc}",
                "details": [{"exception": type(exc).__name__}],
            }
        })
        return 1


# 从 legacy cli.py 透传尚未拆分的符号（包目录遮蔽模块文件，
# 须用 importlib 加载；后续任务逐步迁移到子模块后删除此段）。
import importlib.util as _util
import pathlib as _pathlib
import sys as _sys
_legacy_path = str(_pathlib.Path(__file__).resolve().parent.with_name("cli.py"))
_legacy_spec = _util.spec_from_file_location("orchd.cli._legacy", _legacy_path)
_legacy = _util.module_from_spec(_legacy_spec)
_sys.modules["orchd.cli._legacy"] = _legacy
_legacy_spec.loader.exec_module(_legacy)

# ---- 重定向 legacy 模块的 monkeypatch 敏感符号到本模块命名空间 ----
# 先保存原始函数引用，再创建 lambda 重定向，避免循环引用
_self_mod = _sys.modules[__name__]
_legacy_maybe_archive = _legacy._maybe_archive_ideas
_self_mod._maybe_archive_ideas = _legacy_maybe_archive
_legacy._maybe_archive_ideas = lambda *a, **kw: _self_mod._maybe_archive_ideas(*a, **kw)

# 透传函数（尚在 legacy 中的符号）
_auto_inject_session_id = _legacy._auto_inject_session_id
_build_parser = _legacy._build_parser
_command_name = _legacy._command_name
_reject_container_root_cwd = _legacy._reject_container_root_cwd
_attach_guidance = _legacy._attach_guidance
_emit_guidance = _legacy._emit_guidance
_find_orchd_dir = _legacy._find_orchd_dir
_resolve_agent_id = _legacy._resolve_agent_id
_identity_warning = _legacy._identity_warning
_flatten_nargs = _legacy._flatten_nargs
_is_fingerprint_agent_id = _legacy._is_fingerprint_agent_id
_session_collision_warning = _legacy._session_collision_warning
_session_collision_warn_dict = _legacy._session_collision_warn_dict
_current_task_from_branch = _legacy._current_task_from_branch
_require_agent_id = _legacy._require_agent_id
_detect_claim_role = _legacy._detect_claim_role
_resolve_text_arg = _legacy._resolve_text_arg
# 命令处理函数（尚未迁移到子模块）
_cmd_validate = _legacy._cmd_validate
_cmd_bootstrap = _legacy._cmd_bootstrap
_cmd_init = _legacy._cmd_init
_cmd_amend = _legacy._cmd_amend
_cmd_request = _legacy._cmd_request
_cmd_pool = _legacy._cmd_pool
_cmd_claim = _legacy._cmd_claim
_cmd_done = _legacy._cmd_done
_cmd_review = _legacy._cmd_review
_cmd_retract = _legacy._cmd_retract
_cmd_force_status = _legacy._cmd_force_status
_cmd_merge_ack = _legacy._cmd_merge_ack
_cmd_ideas_archive = _legacy._cmd_ideas_archive
_cmd_full_regression = _legacy._cmd_full_regression
_cmd_doctor = _legacy._cmd_doctor
_cmd_layout_migrate = _legacy._cmd_layout_migrate
_cmd_intake = _legacy._cmd_intake
_cmd_roadmap_land = _legacy._cmd_roadmap_land
_cmd_idea_propose = _legacy._cmd_idea_propose
_cmd_idea_confirm = _legacy._cmd_idea_confirm
_cmd_idea_drop = _legacy._cmd_idea_drop
_cmd_session_start = _legacy._cmd_session_start
_cmd_session_current = _legacy._cmd_session_current
_cmd_session_end = _legacy._cmd_session_end
_cmd_status = _legacy._cmd_status
_cmd_watchdog = _legacy._cmd_watchdog
_cmd_lesson_stage = _legacy._cmd_lesson_stage
_cmd_lesson_add = _legacy._cmd_lesson_add
_cmd_lesson_report = _legacy._cmd_lesson_report
_cmd_lesson_review = _legacy._cmd_lesson_review
_cmd_lesson_resolve = _legacy._cmd_lesson_resolve
_cmd_lesson_archive = _legacy._cmd_lesson_archive
_cmd_lesson_list = _legacy._cmd_lesson_list
_cmd_lesson_show = _legacy._cmd_lesson_show