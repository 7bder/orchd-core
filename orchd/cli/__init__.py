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
