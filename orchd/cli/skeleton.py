"""Orchd CLI 路由：命令骨架 + 输出辅助。

迁移自 orchd/cli.py（task-split-cli-init-skeleton）：
  - _cli_skeleton: 命令骨架装饰器
  - _output: JSON 序列化输出
  - _fix_windows_console_encoding: Windows 控制台 UTF-8 修复
"""

from __future__ import annotations

import json
import sys
from functools import wraps
from typing import Any, Callable


def _cli_skeleton(
    func: Callable[[Any, Any, Any, Any], Any],
) -> Callable[[Any], Any]:
    """命令骨架装饰器：统一承担样板，业务函数仅保留核心逻辑。

    样板包括：_load_tasks / _resolve_agent_id / _identity_warning / 异常统一
    转 JSON（stdout 恒合法）/ _attach_guidance 已在 main 层统一处理。
    本骨架聚焦命令内样板：master/store/agent 加载与 guard 透传。
    """

    @wraps(func)
    def wrapper(args: Any) -> Any:
        # 业务函数签名：func(args, tasks, orchd_dir, master, store, agent_id)
        from orchd.cli import _load_tasks
        from orchd.cli._legacy import _resolve_agent_id
        tasks, orchd_dir, master = _load_tasks()
        from orchd.ledger import Store

        store = Store(orchd_dir)
        agent_id = _resolve_agent_id(orchd_dir)
        return func(args, tasks, orchd_dir, master, store, agent_id)

    return wrapper


def _output(data: Any) -> None:
    """将数据序列化为 JSON 并打印到 stdout。

    使用 indent=2 美化输出，ensure_ascii=False 以保留中文等非 ASCII 字符。
    """
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _fix_windows_console_encoding() -> None:
    """Windows 控制台默认代码页（GBK/CP936）会把 UTF-8 中文输出显示为乱码。

    在 Windows 上将 stdout/stderr 重配置为 UTF-8。仅当流支持 reconfigure
    时生效（重定向到管道的测试环境不受影响）。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass