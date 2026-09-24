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
from orchd.cli.identity import (
    _auto_inject_session_id,
    _resolve_agent_id,
    _require_agent_id,
    _detect_claim_role,
    _identity_warning,
    _is_fingerprint_agent_id,
    _current_task_from_branch,
    _session_collision_warning,
    _session_collision_warn_dict,
    record_session_command,
)
# 3a 收尾（task-split-cli-remove-legacy）：从子包导入全部符号，
# 替代原 legacy cli.py importlib 透传段。
from orchd.cli.guidance import (
    _attach_guidance,
    _emit_guidance,
)
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)
from orchd.cli.parser import (
    _build_parser,
)
from orchd.cli.commands.workflow import (
    _cmd_claim,
    _cmd_done,
    _cmd_request,
    _cmd_review,
    claim_preview,
)
from orchd.cli.commands.control import (
    _cmd_amend,
    _cmd_force_status,
    _cmd_merge_ack,
    _cmd_retract,
)
from orchd.cli.commands.query import (
    _cmd_doctor,
    _cmd_pool,
    _cmd_status,
    _cmd_watchdog,
)
from orchd.cli.commands.init import (
    _cmd_bootstrap,
    _cmd_init,
    _cmd_validate,
)
from orchd.cli.commands.misc import (
    _cmd_context_digest,
    _cmd_full_regression,
    _cmd_intake,
    _cmd_layout_migrate,
    _cmd_roadmap_land,
)
from orchd.cli.commands.ideas import (
    _cmd_idea_confirm,
    _cmd_idea_drop,
    _cmd_idea_propose,
    _cmd_ideas_archive,
)
from orchd.cli.commands.session import (
    _cmd_session_current,
    _cmd_session_end,
    _cmd_session_start,
)
from orchd.cli.commands.lessons import (
    _cmd_lesson_add,
    _cmd_lesson_archive,
    _cmd_lesson_list,
    _cmd_lesson_report,
    _cmd_lesson_resolve,
    _cmd_lesson_review,
    _cmd_lesson_show,
    _cmd_lesson_stage,
)



# 从 _util.py 重新导出（打破 commands -> __init__ 循环依赖）
from orchd.cli._util import (
    _load_tasks,
    _maybe_archive_ideas,
)


def _init_guide_routing_best_effort() -> None:
    """初始化引导路由缓存（task-guide-routing-meta）。

    - 找到 .orchd 且含 rules/ → 加载宿主规则路由；
    - .orchd 缺失 → 空降级（bootstrap 形态，输出本就无 read）；
    - rules/ 缺失 → 跳过（保留既有缓存；fixture 极简项目）；
    - front-matter 损坏（ValueError 点名文件）→ 直接抛出（fail-closed：
      坏元数据静默丢路由比崩更糟，文件名行号随异常给出）。
    其他异常 → 空降级（启动优先；结构问题由合入门禁拦截）。
    """
    from orchd.guide import init_routing

    try:
        try:
            orchd_dir = _find_orchd_dir()
        except Exception:
            orchd_dir = None
        init_routing(orchd_dir, Path.cwd())
    except ValueError:
        raise
    except Exception:
        try:
            init_routing(None)
        except Exception:
            pass

def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回 exit code。

    命令处理函数可返回 dict（自动 JSON 输出，exit code 0）或
    ``(dict, exit_code)`` 元组（JSON 输出 + 自定义 exit code），
    例如 watchdog 在检测到僵死任务时返回 ``(result, 1)``。

    stdout 恒为纯 JSON 契约（task-cli-json-error-envelope）：裸跑无子命令时
    help 改写 stderr；非法/缺参时捕获 argparse 的 SystemExit，在 stderr 保留
    argparse 原文的同时向 stdout 输出 E007 JSON 错误封套（exit code 保持 2）。
    ``--help`` / ``--version`` 的 SystemExit(0) 原样放行（显式请求，既有测试
    以 stdout 承载其输出）。
    """
    _fix_windows_console_encoding()
    _auto_inject_session_id()
    _init_guide_routing_best_effort()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # task-cli-json-error-envelope AC2：argparse 的 error() 已把 usage/error
        # 写入 stderr（原文保留）；这里补 stdout 的 JSON 错误封套，退出码沿用 2。
        # SystemExit(0) 是 --help/--version 的正常出口，原样放行。
        code = exc.code if isinstance(exc.code, int) else 2
        if code == 0:
            raise
        from orchd.guide import attach_error_guidance

        resp: dict[str, Any] = {
            "error": {
                "code": "E007",
                "message": "invalid_usage: argparse 解析失败（非法参数或缺少必需参数）",
                "details": [{"exit_code": code, "argv": list(sys.argv[1:])}],
            }
        }
        resp = attach_error_guidance(resp, "E007", _find_orchd_dir())
        _output(resp)
        _emit_guidance(resp)
        return 2

    if not hasattr(args, "func"):
        # task-cli-json-error-envelope AC1：裸跑无子命令——help 改写 stderr，
        # stdout 不再混入非 JSON（退出码保持 2）。
        parser.print_help(sys.stderr)
        return 2

    command = _command_name(args)
    try:
        _reject_container_root_cwd()   # 纪律护栏：容器根拒绝（E036）
        try:
            # 会话命令记录（task-ref-tx-hook-cost）：E035 colliding_command
            # 数据源；best-effort，不阻断主流程。
            record_session_command(_find_orchd_dir(), command)
        except Exception:
            pass
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
        # task-cli-exit-honesty：软失败必须体现在退出码——顶层含 error 键的
        # 纯 dict 响应（如 request E032 拒绝、retract 用法错误）此前恒返回 0，
        # 下游脚本只读退出码即静默失败（E-13）。成功响应无顶层 error 键
        # （check 命令的 error 嵌在 checks 条目内，不触发本规则）。
        if isinstance(data, dict) and "error" in data:
            return 1
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
