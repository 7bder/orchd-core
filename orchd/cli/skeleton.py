"""Orchd CLI 路由：命令骨架 + 输出辅助。

迁移自 orchd/cli.py（task-split-cli-init-skeleton）：
  - _cli_skeleton: 命令骨架装饰器
  - _output: JSON 序列化输出
  - _fix_windows_console_encoding: Windows 控制台 UTF-8 修复
"""

from __future__ import annotations

import json
import re
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
        from orchd.cli.identity import _resolve_agent_id
        tasks, orchd_dir, master = _load_tasks()
        from orchd.ledger import Store

        store = Store(orchd_dir)
        agent_id = _resolve_agent_id(orchd_dir)
        return func(args, tasks, orchd_dir, master, store, agent_id)

    return wrapper


# 在 ensure_ascii=False 直写下会破坏「严格 json.loads 解析」或「UTF-8 编码」的字符：
#  - U+007F DEL 与 U+0080–U+009F C1 控制符（JSON 允许原样但部分消费方不容 / 可读性差）
#  - 孤立代理码位 U+D800–U+DFFF（无法 UTF-8 编码，直接触发 GBK/乱码或流写入失败）
#  - 私用区 PUA：U+E000–U+F8FF、U+F0000–U+FFFFD、U+100000–U+10FFFD（多为脏数据/图标字体残留）
# C0 控制字符由 json.dumps 自行转义为 \u00XX，无需在此处理；\t\n\r 与结构字符保持不动。
_UNSAFE_JSON_CHARS = re.compile(
    "[\x7f-\x9f\ud800-\udfff\ue000-\uf8ff"
    "\U000f0000-\U000ffffd\U00100000-\U0010fffd]"
)


def _output(data: Any) -> None:
    """将数据序列化为 JSON 并打印到 stdout（stdout 恒为严格可解析 JSON）。

    使用 indent=2 美化、ensure_ascii=False 保留中文直写；序列化后把「不安全字符」
    （孤立代理 / DEL / C1 控制符 / 私用区 PUA，见 ``_UNSAFE_JSON_CHARS``）统一替换为
    U+FFFD，确保管道/脚本消费方对 stdout 一次 ``json.loads``（strict）即可解析，
    且不因脏字符在 Windows 控制台触发 GBK/编码错误。
    """
    text = json.dumps(data, ensure_ascii=False, indent=2)
    text = _UNSAFE_JSON_CHARS.sub("\ufffd", text)
    print(text)


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