"""Orchd CLI 路由：argparse 子命令 + 统一 JSON 输出 + 错误捕获。

提供 18 个子命令：validate、bootstrap、init、amend、request、pool、claim、
done、review、retract、force-status、status、watchdog、ideas-archive、doctor、
intake、roadmap-land、idea（含 propose/confirm/drop）。

特性：
- 所有命令统一输出 JSON（indent=2，ensure_ascii=False）。
- 顶层 try/except 捕获 OrchdError 并转为 JSON 错误响应，exit code 为 1。
- Windows 控制台 UTF-8 修复：启动时自动将 stdout/stderr 重配置为 UTF-8，
  避免中文输出在 GBK/CP936 代码页下显示为乱码。

依赖方向：cli.py → onboard.py / spec.py / report.py / split.py（顶层入口）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from orchd import __version__
from orchd.errors import ErrorCode, OrchdError, to_json_response


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

    try:
        result = args.func(args)
        if result is None:
            return 0
        # 支持命令返回 (dict, exit_code) 元组
        if isinstance(result, tuple):
            data, code = result
            data = _attach_guidance(data)
            _output(data)
            _emit_guidance(data)
            return code
        data = _attach_guidance(result)
        _output(data)
        _emit_guidance(data)
        return 0
    except OrchdError as exc:
        _output(to_json_response(exc))
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


def _attach_guidance(data: Any) -> Any:
    """为命令 JSON 响应统一附加 guidance 字段（task-guide-seamless-guidance）。

    无感引导契约：
    - **加法式**：只新增 ``guidance`` 键，不删不改既有字段（next_action 等保留）。
    - **幂等**：已有 guidance 不覆盖。
    - **best-effort**：读取 ledger / 构造引导的任何异常静默跳过，不阻塞主流程。
    - **知识路由闭环**（task-guide-routing-loop）：附加 guidance 后透传
      ``orchd_dir`` 调用 ``resolve_read_paths`` 过滤 read/template 路径，
      只保留指向实际存在文件的路径，不存在时静默跳过。

    仅对 dict 响应生效；非 dict（如字符串/None）原样返回。
    """
    if not isinstance(data, dict) or "guidance" in data:
        return data
    try:
        from orchd.ledger import Store
        from orchd.spec import load_master
        from orchd.worktree import resolve_canonical_project_root

        orchd_dir = _find_orchd_dir()
        state = Store(orchd_dir).replay()
        # master 任务定义统一从 canonical 主工作树读（task-canonical-project-root）
        canonical_root = resolve_canonical_project_root(orchd_dir.parent)
        master_path = canonical_root / ".orchd" / "_master.json"
        tasks = load_master(master_path).tasks if master_path.exists() else []

        from orchd.guide import next_guidance, resolve_read_paths
        # task-guidance-dual-view-engine：传 agent_id（_resolve_agent_id 解析）与
        # has_master（master_path.exists()），支撑双视角与未初始化/空项目区分。
        agent_id = _resolve_agent_id(orchd_dir)
        has_master = master_path.exists()
        data["guidance"] = resolve_read_paths(
            next_guidance(state, tasks, agent_id=agent_id, has_master=has_master),
            orchd_dir,
        )
    except Exception:
        # best-effort：引导失败静默跳过，绝不阻塞命令主流程
        pass
    return data


def _emit_guidance(data: Any) -> None:
    """将 guidance 的人类可读提示块打印到 stderr（task-guide-seamless-guidance）。

    设计契约：
    - **不污染 stdout**：stdout 保持纯 JSON，供机器/agent 解析；人看的"下一步"提示块
      打在 stderr，紧跟 JSON 之后，终端中人机同时可见。
    - **醒目可辨**：用分隔线围成块状，一眼可区分是 orchd 系统输出而非命令结果。
    - **best-effort**：非 dict / 无 guidance / 无 hint 时静默跳过，不影响主流程。
    - **可配置开关**（task-guide-block-config）：config.guidance_stderr 为 false 时
      跳过提示块打印；缺失/读取失败回退默认 true（向后兼容）。
    """
    if not isinstance(data, dict):
        return
    g = data.get("guidance")
    if not isinstance(g, dict):
        return
    hint = g.get("hint")
    if not hint:
        return
    if not _guidance_stderr_enabled():
        return
    command = g.get("command") or "<无命令>"
    sep = "─" * 40
    lines = [
        "",
        sep,
        f"orchd ▸ {hint}",
        f"建议执行：{command}",
        sep,
        "",
    ]
    print("\n".join(lines), file=sys.stderr)


def resolve_guidance_paths(
    guidance: dict[str, Any] | None,
    orchd_dir: Path | None = None,
) -> dict[str, Any]:
    """将 guidance 的 read/template 路径解析为实际存在的文件条目（知识路由闭环）。

    知识路由闭环的 agent 侧解析接口（task-guide-routing-loop）：agent 收到
    guidance 后按 ``read`` 数组读规则文件、按 ``template`` 数组加载模板。
    ``resolve_read_paths`` 已把两数组过滤为实际存在的路径；本接口进一步把每条
    路径解析为**可读文件条目**（绝对路径 + 是否存在），供 agent 直接据以读取，
    不要求引擎在此处自动读取文件内容（只提供可解析的路由数据，读取由 agent
    按需进行）。

    契约：
    - 返回值与 guidance 同构：``{read: [{path, abs_path, exists}], template: [...]}``；
      guidance 为空 / 缺键时对应数组为空（无害，向下兼容）。
    - 空数组 / orchd_dir 缺失时原样返回空结构，不抛异常（best-effort）。

    Args:
        guidance: 含 read/template 数组的 guidance 字典（可为 None）。
        orchd_dir: 规则/模板根目录（.orchd/）；None 时自动查找。

    Returns:
        解析后的 read/template 文件条目字典。
    """
    if orchd_dir is None:
        orchd_dir = _find_orchd_dir()
    root = str(orchd_dir)
    parent = str(orchd_dir.parent)

    def _entries(paths: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in paths:
            if os.path.isabs(p):
                abs_path = p
            else:
                candidate = os.path.join(root, p)
                if not os.path.isfile(candidate):
                    candidate = os.path.join(parent, p)
                abs_path = candidate
            out.append({
                "path": p,
                "abs_path": abs_path,
                "exists": os.path.isfile(abs_path),
            })
        return out

    g = guidance or {}
    return {
        "read": _entries(g.get("read") or []),
        "template": _entries(g.get("template") or []),
    }


def _guidance_stderr_enabled() -> bool:
    """读取 config.guidance_stderr 决定是否打印 stderr 提示块（task-guide-block-config）。

    契约：
    - 默认 true：config 缺失、键缺失或读取失败时回退 true（向后兼容，不影响旧项目）。
    - 显式 false：跳过提示块打印，stdout 纯 JSON 契约不受影响。
    """
    try:
        from orchd.spec import load_master
        from orchd.worktree import resolve_canonical_project_root

        orchd_dir = _find_orchd_dir()
        canonical_root = resolve_canonical_project_root(orchd_dir.parent)
        master_path = canonical_root / ".orchd" / "_master.json"
        if not master_path.exists():
            return True
        master = load_master(master_path)
        return bool(master.config.get("guidance_stderr", True))
    except Exception:
        # best-effort：读取失败回退默认 true，绝不阻塞主流程
        return True


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


# TRAE 宿主注入的每对话唯一会话码（session-id-fingerprint 宿主接入）。
# 各 agent 宿主统一把各自会话唯一码写入 ORCHD_SESSION_ID；TRAE 已暴露
# ICUBE_CODEMAIN_SESSION（UUID），此处在其缺省时自动搬运，TRAE 场景开箱即用；
# codex / opencode / workbuddy 等由各自接入层注入同名变量。
_TRAE_SESSION_ID_ENV = "ICUBE_CODEMAIN_SESSION"


def _auto_inject_session_id() -> None:
    """宿主会话身份自动注入：若 ORCHD_SESSION_ID 未设置，则用 TRAE 会话码兜底。"""
    if os.environ.get("ORCHD_SESSION_ID"):
        return
    trae_sid = os.environ.get(_TRAE_SESSION_ID_ENV)
    if trae_sid:
        os.environ["ORCHD_SESSION_ID"] = trae_sid


def _resolve_text_arg(
    inline: str | None,
    file_path: str | None,
    inline_name: str,
    file_name: str,
    required: bool = True,
) -> str | None:
    """解析 内联文本 / --xxx-file 二选一参数。

    Windows shell（尤其 PowerShell / cmd）对多行字符串的解析存在已知问题：
    含换行符的长文本在命令行中可能被 shell 拆分为多个独立参数，导致 argparse
    报错或截断。因此本函数允许调用者将长文本写入临时 UTF-8 文件后经
    file_name 参数传入，绕过 shell 的多行解析限制。

    Args:
        inline: 内联文本值（命令行直接传入）。
        file_path: 文件路径（--xxx-file 参数），文件需为 UTF-8 编码。
        inline_name: 内联参数的 CLI 名称（如 "--changes"），用于错误提示。
        file_name: 文件参数的 CLI 名称（如 "--changes-file"），用于错误提示。
        required: 若为 True，两者均未提供时抛出 E007 错误。

    Returns:
        解析后的文本内容；若 required=False 且两者均未提供则返回 None。

    Raises:
        OrchdError E007: inline 与 file_path 同时提供，或 required=True 时均未提供。
        OrchdError E001: file_path 指定的文件不存在。
    """
    if inline and file_path:
        raise OrchdError(
            ErrorCode.E007,
            f"{inline_name} 与 {file_name} 只能二选一",
            [{"arguments": [inline_name, file_name]}],
        )
    if file_path:
        p = Path(file_path)
        if not p.exists():
            raise OrchdError(
                ErrorCode.E001,
                f"file not found: {file_path}",
                [{"path": str(p), "message": f"{file_name} 指定的文件不存在"}],
            )
        return p.read_text(encoding="utf-8").strip()
    if inline:
        return inline
    if required:
        raise OrchdError(
            ErrorCode.E007,
            f"必须提供 {inline_name} 或 {file_name}",
            [{"arguments": [inline_name, file_name]}],
        )
    return None


def _build_parser() -> argparse.ArgumentParser:
    """构建 argparse 解析器并注册全部 13 个子命令。

    子命令列表：
    1. validate   — 校验 _master.json
    2. bootstrap  — 输出分解套件 JSON
    3. init       — 初始化 .orchd/ 并生成 snapshot
    4. amend      — 增量更新 snapshot
    5. request    — 获取下一个候选任务
    6. pool       — 列出就绪池
    7. claim      — 认领任务
    8. done       — 报告任务完成
    9. review     — 提交审查结果
    10. retract    — 撤回事件
    11. force-status — 强制设置任务状态
    12. status    — 全局状态快照 / 单任务详情
    13. watchdog  — 僵死任务巡检

    Returns:
        配置完毕的 ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        prog="orchd",
        description="Cross-agent-platform task distribution CLI",
        epilog=(
            "无感引导：任意命令的 JSON 响应含 guidance 字段（step/command/hint，"
            "指导下一步行动）；运行 'orchd status --text' 查看任务池与下一步引导。"
        ),
    )
    parser.add_argument("--version", action="version", version=f"orchd {__version__}")
    sub = parser.add_subparsers(dest="command")

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

    # amend
    p = sub.add_parser("amend", help="增量更新 snapshot")
    p.add_argument("--master", default=".orchd/_master.json")
    p.set_defaults(func=_cmd_amend)

    # request
    p = sub.add_parser("request", help="获取下一个候选任务")
    p.add_argument("--capabilities", nargs="*")
    p.add_argument("--exclude", nargs="*")
    p.add_argument("--sort", choices=["importance", "downstream", "hours"])
    p.add_argument("--auto-claim", action="store_true",
                   help="request 成功返回候选后自动执行 claim（绕过人工确认，适合无人值守场景）")
    p.add_argument("--with-context", action="store_true",
                   help="--auto-claim 时附加全部共享上下文（默认按需）")
    p.add_argument("--max-active", type=int, default=None,
                   help="全局活跃（claimed）任务数达到该值时拒绝候选（容量控制）")
    p.set_defaults(func=_cmd_request)

    # pool
    p = sub.add_parser("pool", help="列出就绪池")
    p.add_argument("--capabilities", nargs="*")
    p.add_argument("--all", action="store_true", dest="show_all")
    p.set_defaults(func=_cmd_pool)

    # claim
    p = sub.add_parser("claim", help="认领任务")
    p.add_argument("--task", required=True)
    p.add_argument("--type", dest="review_type", choices=["spec", "code"],
                   help="reviewer 认领时指定审查阶段（默认锁任务当前阶段）")
    p.add_argument("--confirm", action="store_true",
                   help="确认执行认领（无 --confirm 时仅输出预览，不写事件、不建分支）")
    p.add_argument("--with-context", action="store_true",
                   help="显式附加全部共享上下文（architecture + conventions），默认按需")
    p.set_defaults(func=_cmd_claim)

    # done
    p = sub.add_parser("done", help="报告任务完成")
    p.add_argument("--task", required=True)
    p.add_argument("--changes")
    p.add_argument("--changes-file", help="从文件读取变更描述（UTF-8），与 --changes 二选一")
    p.add_argument("--concerns")
    p.set_defaults(func=_cmd_done)

    # review
    p = sub.add_parser("review", help="提交审查结果")
    p.add_argument("--task", required=True)
    # review-unify-r2：unified 单阶段模式下无需 --type（一次 APPROVED 即 merge）；
    # two_phase 模式仍须传 spec/code。
    p.add_argument("--type", required=False, choices=["spec", "code"],
                   help="审查阶段（spec/code）；unified 单阶段模式下可省略")
    p.add_argument("--verdict", required=True, choices=["APPROVED", "CHANGES_REQUESTED"])
    p.add_argument("--comments")
    p.add_argument("--comments-file", help="从文件读取审查意见（UTF-8），与 --comments 二选一")
    p.set_defaults(func=_cmd_review)

    # retract
    p = sub.add_parser("retract", help="撤回事件")
    p.add_argument("--event", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_retract)

    # force-status
    p = sub.add_parser("force-status", help="强制设置任务状态")
    p.add_argument("--task", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--assignee")
    p.add_argument("--force", action="store_true",
                   help="显式确认走逃生口（claimed→completed / cancelled→pending）")
    p.add_argument("--evidence-sha", default=None,
                   help="复活已完成任务的 git 证据 commit SHA（仅 completed→pending 时需要）")
    p.set_defaults(func=_cmd_force_status)

    # merge-ack（task-merge-warning-ack）
    p = sub.add_parser("merge-ack", help="merge_warning 人工销账（merge-acks 确认清单）")
    p.add_argument("--task", required=True, help="已人工确认的 task_id")
    p.add_argument("--reason", required=True, help="确认原因（必填）")
    p.set_defaults(func=_cmd_merge_ack)

    # status
    p = sub.add_parser("status", help="全局状态快照；可跟 task-id 查单任务详情")
    p.add_argument("task", nargs="?", default=None, help="可选：任务 ID，查询单任务详情")
    p.add_argument("--text", action="store_true")
    p.add_argument("--audit-merge", action="store_true",
                   help="附加 merge 巡检：completed 任务对应 task/{id} 分支未并入 main 的告警清单（只读）")
    p.add_argument("--audit-intake", action="store_true",
                   help="附加摄入产物审计：未提交的 IDEAS.md / _master.json 改动告警（只读）")
    p.add_argument("--audit-revive", action="store_true",
                   help="附加复活巡检：扫描 ledger 中 completed→pending 的强制复活操作，列告警（只读）")
    p.add_argument("--audit-task", action="store_true",
                   help="附加任务完整性巡检：merged 任务的历史缺失/残留（main 残留 + 分支 diff 缺失声明文件，只读）")
    p.set_defaults(func=_cmd_status)

    # watchdog
    p = sub.add_parser("watchdog", help="僵死任务巡检")
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("--takeover", action="store_true",
                   help="对 stale_claims 中确认会话已失效的任务执行 force-status 回退 pending（best-effort）")
    p.set_defaults(func=_cmd_watchdog)

    # ideas-archive
    p = sub.add_parser("ideas-archive", help="自动归档已完结的 IDEAS 条目")
    p.set_defaults(func=_cmd_ideas_archive)

    # doctor
    p = sub.add_parser("doctor", help="git 仓库完整性只读检测")
    p.add_argument("--path", default=".", help="项目根目录（含 .git），默认当前目录")
    p.set_defaults(func=_cmd_doctor)

    # layout-migrate（task-14-worktree-layout）：flat → container 迁移
    p = sub.add_parser(
        "layout-migrate", help="flat → container 布局迁移（保留 git 历史、可回滚）"
    )
    p.add_argument("--path", default=".", help="flat 主工作树根，默认当前目录")
    p.set_defaults(func=_cmd_layout_migrate)

    # intake（2026-08-14 intake-commit-enforcement）
    p = sub.add_parser("intake", help="提交摄入产物（IDEAS.md；ROADMAP.md 不纳入 git，自动跳过）并校验状态合法性")
    p.set_defaults(func=_cmd_intake)

    # roadmap-land（2026-08-15 intake-dual-path）：ROADMAP 规划章节 → IDEAS pending 落地
    p = sub.add_parser("roadmap-land", help="为 ROADMAP 规划章节生成 IDEAS pending 落地条目")
    p.add_argument("version", help="规划章节版本（如 1.3，匹配 ROADMAP ## 版本 章节头）")
    p.set_defaults(func=_cmd_roadmap_land)

    # idea（2026-08-15 idea-write-gate）：灵感写入 IDEAS 写入门禁（propose / confirm / drop）
    p = sub.add_parser("idea", help="灵感写入 IDEAS 写入门禁：propose 记入 study，confirm/drop 裁决")
    idea_sub = p.add_subparsers(dest="idea_action", required=True)

    _p = idea_sub.add_parser("propose", help="为灵感追加 status: study 条目到 IDEAS.md（agent 执行）")
    _p.add_argument("--title", required=True, help="灵感标题")
    _p.add_argument("--feasibility", required=True, help="可行性论证（写入 - 论证: 字段）")
    _p.set_defaults(func=_cmd_idea_propose)

    _p = idea_sub.add_parser("confirm", help="将 status: study 条目升为 pending（仅用户执行）")
    _p.add_argument("--title", required=True, help="灵感标题")
    _p.set_defaults(func=_cmd_idea_confirm)

    _p = idea_sub.add_parser("drop", help="将 status: study 条目降为 dropped（仅用户执行）")
    _p.add_argument("--title", required=True, help="灵感标题")
    _p.set_defaults(func=_cmd_idea_drop)

    # session（Session Identity Layer）：引擎显式会话生命周期
    p = sub.add_parser("session", help="会话生命周期：start / current / end")
    session_sub = p.add_subparsers(dest="session_action", required=True)

    _p = session_sub.add_parser("start", help="开启新会话并输出 session_token/session_id")
    _p.add_argument("--agent", default=None, help="具名 agent（可选，如 codex-1）")
    _p.set_defaults(func=_cmd_session_start)

    _p = session_sub.add_parser("current", help="显示当前会话")
    _p.set_defaults(func=_cmd_session_current)

    _p = session_sub.add_parser("end", help="结束当前会话")
    _p.set_defaults(func=_cmd_session_end)

    return parser


# ------------------------------------------------------------------
# 辅助
# ------------------------------------------------------------------


def _find_orchd_dir() -> Path:
    """查找 .orchd/ 目录。

    搜索策略：从当前工作目录开始，逐级向上遍历父目录，返回第一个包含
    ``.orchd/`` 子目录的路径。若一直未找到，则回退为 ``cwd / ".orchd"``
    （即假设当前目录为项目根目录，后续操作会自动创建该目录）。

    Returns:
        .orchd 目录的 Path 对象。
    """
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        candidate = parent / ".orchd"
        if candidate.is_dir():
            return candidate
    return cwd / ".orchd"


def _flatten_nargs(values: list[str] | None) -> list[str] | None:
    """展平 nargs="*" 参数：引号包裹的单个参数按空白或逗号拆分为多个。

    shell 引号（--capabilities "python git docs"）下 argparse 会把整个
    引号串当作一个参数（["python git docs"]），能力过滤全部不匹配并误报
    "当前无就绪任务"；此处按空白展平，等价于 --capabilities python git docs。
    同时兼容逗号分隔（--exclude "task-a,task-b"，SKILL.md 曾用逗号示例），
    避免逗号形式静默失效（2026-08-13 全面审核 §7.1）。
    None 或空列表原样返回。
    """
    if not values:
        return values
    out: list[str] = []
    for v in values:
        for token in v.replace(",", " ").split():
            if token:
                out.append(token)
    return out


def _resolve_agent_id(orchd_dir: Path | None = None) -> str:
    """解析当前会话身份：由宿主注入的 ``ORCHD_SESSION_ID`` 派生（session-id-fingerprint）。

    引擎统一从 ``orchd.ledger.resolve_agent_id`` 取 agent 身份：
    - 有 ``ORCHD_SESSION_ID`` → 确定性派生 12 位 hex 指纹（同一对话内稳定，
      切换对话换指纹）；
    - 无该变量 → 返回空字符串（引擎不生成、不借用、不落盘任何身份）。
    宿主（TRAE / codex / opencode / workbuddy）在启动 orchd 前统一把各自
    会话唯一码注入 ``ORCHD_SESSION_ID``。写命令在身份为空时由调用方拒绝。
    """
    from orchd.ledger import resolve_agent_id

    return resolve_agent_id(orchd_dir)


def _require_agent_id(orchd_dir: Path | None = None) -> str:
    """解析当前会话身份；为空（宿主未注入 ORCHD_SESSION_ID）则 E033 拒绝。

    供写命令（claim / done / review / retract / force-status）调用：这些命令
    需要把身份写进事件，身份缺失时不可静默降级，须明确报错提示宿主注入会话 ID。
    """
    agent_id = _resolve_agent_id(orchd_dir)
    if not agent_id:
        raise OrchdError(
            ErrorCode.E033,
            "session_identity_missing: 宿主未注入 ORCHD_SESSION_ID，无法识别当前会话身份",
            [{
                "agent_id": agent_id,
                "hint": (
                    "本命令需要会话身份。请宿主在启动 orchd 前把当前会话唯一码注入 "
                    "ORCHD_SESSION_ID（TRAE 会话自动注入；codex/opencode/workbuddy "
                    "由各自接入层注入），再重试"
                ),
            }],
        )
    return agent_id


def _detect_claim_role(store, tasks: list[dict[str, Any]], task_id: str) -> str:
    """按任务当前状态自动判定认领角色（task-fp-identity-engine）。

    - in_review → reviewer（审查认领，REVIEW_CLAIMED）
    - 其他（pending / claimed 等）→ implementer（实现认领，CLAIMED）
    引擎据此在 claim 时省略 --role，实现按状态自动分流。
    """
    state = store.replay()
    ts = state.get(task_id)
    return "reviewer" if (ts and ts.status == "in_review") else "implementer"


def _identity_warning(agent_id: str, orchd_dir: Path) -> dict[str, Any] | None:
    """比对 git config user.name 与 agent_id，不一致返回 E021 warning（不阻断）。

    git 不可用 / user.name 未配置 / 与 agent_id 一致 → 返回 None（无 warning）。
    指纹形态身份 agent_id（12 位 hex）豁免
    E021——指纹为自动化 agent 身份锚定，不与人名 git user.name 硬比对。
    用于写命令（claim/done/review）前的身份审计（ROADMAP 1.1 L5）：
    仅提示，不阻断状态机。
    """
    import subprocess

    # 指纹形态身份 agent_id 豁免 E021（12 位 hex）
    if _is_fingerprint_agent_id(agent_id):
        return None

    try:
        proc = subprocess.run(
            ["git", "config", "user.name"], cwd=str(orchd_dir.parent),
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    git_name = proc.stdout.strip()
    if not git_name or git_name == agent_id:
        return None
    return {
        "code": "E021",
        "warning": "identity_mismatch",
        "git_user_name": git_name,
        "agent_id": agent_id,
        "hint": "git config user.name 与 agent_id 不一致，请核对身份（SKILL.md 命名规范：{provider}-{序号}）",
    }


def _is_fingerprint_agent_id(agent_id: str) -> bool:
    """判断 agent_id 是否为指纹形态身份（12 位 hex）。

    task-fp-identity-single-source（2026-08-22）：单一事实源为
    ``orchd.ledger.is_fingerprint_agent_id``，此处惰性导入转发（保持调用点
    不变，避免模块级循环依赖），消除本地副本的同步漂移风险。
    """
    from orchd.ledger import is_fingerprint_agent_id

    return is_fingerprint_agent_id(agent_id)


def _current_task_from_branch(project_root: Path) -> str | None:
    """best-effort 从当前 git 分支名推导本流程归属任务（``task/<id>`` 前缀）。

    容器布局（1.4）下 agent 在专属 task worktree 中工作，分支即 ``task/<id>``，
    据此可将「本流程发起的任务」从并行活跃任务中排除，避免单任务流程误报。
    无 git / 非 task 分支 / 调用失败 → 返回 None（调用方退化为更保守的阈值）。
    """
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if branch.startswith("task/"):
        return branch[len("task/"):]
    return None


def _session_collision_warning(
    agent_id: str,
    store,
    exclude_task_id: str | None = None,
    review_task_id: str | None = None,
) -> dict[str, Any] | None:
    """只读检测当前会话指纹的「并行会话碰撞」，返回 ``session_collision_warning`` 或 None。

    host 注入项目级会话码（全项目同一 ``ORCHD_SESSION_ID``）时，所有并行对话派生出
    **同一指纹**，引擎会把它们静默识别为同一身份、归属错乱。本函数仅只读 replay
    ledger，**不修改身份机制（resolve_agent_id 不变）、不落状态（不写 ledger/checkpoint）、
    不阻断命令**，只在命中时附加只读告警，提示 host 注入粒度违约。

    触发条件（任一即告警）：

    1. 并行活跃任务：当前指纹名下已存在非本流程发起的并行活跃任务
       （``claimed``/``in_review``，owner 指纹 == 当前指纹，且 ``task_id != exclude_task_id``）；
    2. 自审实现：``review_task_id`` 的实现者指纹 == 当前指纹（审查自己实现）。

    非指纹身份（如 ``provider-{n}``）豁免——其粒度本就为 agent 级，碰撞语义不同，
    不在此告警范围。

    Args:
        agent_id: 当前会话指纹（由 ``resolve_agent_id`` 派生）。
        store: ledger Store（用于 replay 当前任务状态）。
        exclude_task_id: 本流程正在操作/归属的任务（claim 传被认领任务，
            status 传当前分支或显式 task_id）；命中后从并行集合排除，避免自指。
        review_task_id: 若本流程为审查认领，传被审查任务，用于自审检测。

    Returns:
        告警 dict（含 ``code``/``warning``/``reason``/``colliding_tasks``/``hint``），
        或 None（无碰撞、零误报）。
    """
    if not agent_id or not _is_fingerprint_agent_id(agent_id):
        return None

    from orchd.ledger import resolve_session_identity

    current_session_id = resolve_session_identity(getattr(store, "orchd_dir", None))["session_id"]

    def _same_owner(claimed_by: str | None, claimed_session: str | None) -> bool:
        if claimed_session and current_session_id:
            return claimed_session == current_session_id and claimed_by == agent_id
        return bool(claimed_by and claimed_by == agent_id)

    states = store.replay()

    # 条件 2：审查自己实现（reviewer session == 实现者 session）
    if review_task_id:
        ts = states.get(review_task_id)
        if ts is not None and _same_owner(ts.claimed_by, ts.claimed_session):
            return _session_collision_warn_dict(
                reason="self_implementation_review",
                colliding_tasks=[review_task_id],
                hint=(
                    "当前会话与任务实现者 session 相同，疑似审查自己实现：host 注入的"
                    "会话码可能为项目级（全项目同指纹/session），导致并行对话被识别为同一身份。"
                    "请换一个独立会话（新对话 / 重新 session start 注入唯一会话码）担任 reviewer，或核对归属。"
                ),
            )

    # 条件 1：当前 session 名下存在其他并行活跃任务
    colliding: list[str] = []
    for tid, ts in states.items():
        if tid == exclude_task_id:
            continue
        if ts.status in ("claimed", "in_review"):
            if _same_owner(ts.claimed_by, ts.claimed_session) or _same_owner(
                ts.review_claimed_by, ts.review_claimed_session
            ):
                colliding.append(tid)

    if colliding:
        # 本流程有明确归属任务 → 其余活跃任务必为并行（误报概率低，直接告警）；
        # 无明确归属任务 → 仅当指纹名下已并行多任务（>=2）才告警，避免单任务正常流误报。
        if exclude_task_id is not None or len(colliding) >= 2:
            return _session_collision_warn_dict(
                reason="parallel_active_tasks",
                colliding_tasks=colliding,
                hint=(
                    "当前会话指纹名下已存在其他并行活跃任务（claimed/in_review），"
                    "但本流程并未发起它们：host 注入的会话码为项目级（全项目同指纹），"
                    "导致并行对话被识别为同一身份、归属错乱。请为每次对话注入唯一会话码，"
                    "或核对任务归属。"
                ),
            )
    return None


def _session_collision_warn_dict(
    reason: str, colliding_tasks: list[str], hint: str,
) -> dict[str, Any]:
    """构造 ``session_collision_warning`` 告警 dict（E035 告警码，不阻断命令）。"""
    return {
        "code": "E035",
        "warning": "session_collision_warning",
        "reason": reason,
        "colliding_tasks": colliding_tasks,
        "hint": hint,
    }


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


# ------------------------------------------------------------------
# 命令处理
# ------------------------------------------------------------------


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

    master = load_master(args.path)
    structure_errors = validate_structure(master) + validate_references(master)
    quality_warnings = validate_quality(master)  # E022/E023/E024 为质量告警，不判 invalid

    # 终态任务集合；无可用 ledger（新项目 / replay 失败）时跳过过滤，validate 保持可运行。
    terminal_ids: set[str] | None = None
    try:
        state = Store(Path(args.path).parent).replay()
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
    quality_warnings += roadmap_landing_warnings(Path(args.path).parent)
    # task-14-worktree-layout：双布局标记校验（LAYOUT 告警，不判 invalid；缺失自动探测 + 告警）。
    # 入参为项目根（master 目录的父级）；container 下为 <容器>/main。
    quality_warnings += layout_marker_warnings(Path(args.path).parent.parent)

    def _warn_dict(e) -> dict:
        """把 ValidationError 或告警 dict 归一化为输出结构（含 roadmap-land E031 dict）。"""
        if isinstance(e, dict):
            return {"code": e.get("code"), "path": e.get("path"), "message": e.get("message")}
        return {"code": e.code.name, "path": e.path, "message": e.message}

    if structure_errors:
        return {
            "valid": False,
            "errors": [{"code": e.code.name, "path": e.path, "message": e.message} for e in structure_errors],
            "warnings": [_warn_dict(e) for e in quality_warnings],
        }
    return {
        "valid": True,
        "errors": [],
        "warnings": [_warn_dict(e) for e in quality_warnings],
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

    master_path = Path(args.master).resolve()
    orchd_dir = master_path.parent
    project_root = orchd_dir.parent

    # 新项目（无 master）→ 默认 container（AC3）
    if not master_path.exists():
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

    # 既有项目（master 已存在）→ flat（AC5 零回归）；标记缺失时补写 flat 标记（AC2）
    if read_layout(orchd_dir) is None:
        write_layout(orchd_dir, "flat", project_root)
    master = load_master(master_path)
    return init(orchd_dir, master)


def _cmd_amend(args) -> dict:
    """增量更新 snapshot，依据状态约束矩阵过滤变更。

    CLI 参数: args.master — master 文件路径（默认 .orchd/_master.json）。
    返回: 变更摘要字典（new_tasks / updated_tasks / unchanged_tasks / removed_tasks），
    成功后附加 commit 字段（best-effort 自动提交 master 与 IDEAS.md，不阻塞）；
    新增/变更任务若声明 verify_command，附加 verify_dry_run 字段（试跑结果仅提示、不阻断注册）。
    """
    import subprocess

    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.ledger import Store, resolve_workspace_root
    from orchd.onboard import _decode_subprocess_output
    from orchd.spec import load_master
    from orchd.split import amend

    master = load_master(args.master)
    orchd_dir = Path(args.master).parent
    store = Store(orchd_dir)
    result = amend(orchd_dir, master, store)

    # 成功后 best-effort 自动提交（锁外、不阻塞状态机，语义对齐 merged:false）
    project_root = orchd_dir.parent
    changed = list(result.get("new_tasks", [])) + list(result.get("updated_tasks", []))
    summary = ", ".join(changed) if changed else "snapshot refresh"

    # 分支校验：intake/amend 约定只在 main 执行，非 main 时降级为不提交，
    # 避免 master+IDEAS.md 被误提交进任务分支（污染待 merge 内容）
    current_branch = get_current_branch(project_root)
    default_branch = get_default_branch(project_root) or "main"
    if current_branch is not None and current_branch != default_branch:
        result["commit"] = {
            "performed": False,
            "reason": "not_on_main",
            "branch": current_branch,
        }
    else:
        # AC3（task-12-engine-path-abstraction）：IDEAS.md 走统一工作区根 helper
        # （默认 .orchd/，兼容旧根路径）；commit 路径与 ensure_committed 期望一致。
        ws_root = resolve_workspace_root(project_root)
        # intake-commit-enforcement（2026-08-14）：提交范围含 ROADMAP.md——
        # roadmap 摄入改的 ROADMAP.md 此前不在范围，必然残留未提交改动
        commit = ensure_committed(
            project_root,
            [str(Path(args.master)), str(ws_root / "IDEAS.md"), str(ws_root / "ROADMAP.md")],
            f"chore(intake): orchd amend — {summary}",
        )
        result["commit"] = commit
        # intake-commit-enforcement（2026-08-14）：commit 降级可审计化（对齐
        # merge_warning 先例）——注册成功后 commit 未执行（非 no_changes）不再
        # 静默：写入 commit_warning 供 status --audit-intake 巡检。git 环境不可用
        # （not_a_git_repo / git_unavailable）保留 best-effort 降级（判据 3）；
        # git 可用但提交失败（commit_failed）同样告警——"注册成功但改动未入库"
        # 违背"强制提交"语义，须人工核对。
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"amend 注册成功但 commit 未执行（{commit.get('reason')}）："
                    "摄入产物改动可能未入库"
                ),
                "hint": (
                    "若为 git 环境异常，可运行 'orchd status --audit-intake' "
                    "巡检未提交摄入产物，或运行 'orchd intake' 手动提交"
                ),
            }

    # dry-run 试跑新增/变更任务的 verify_command（与 done 相同 shell 执行、同 cwd、
    # 限时 30s；2026-08-08 升级：assertion_mismatch 类失败阻断注册（E028），
    # E024/E027（缺 basetemp / 不安全段）阻断注册；expected_pending 仅提示）
    from orchd.split import classify_dry_run_failure
    from orchd.spec import validate_quality, verify_command_dangerous_reasons
    from orchd.subproc import run_shell

    task_map = {t.get("id", ""): t for t in master.tasks}
    dry_run_results: list[dict[str, Any]] = []
    blocking_errors: list[dict[str, Any]] = []
    for tid in changed:
        verify_cmd = task_map.get(tid, {}).get("verify_command")
        if not verify_cmd:
            continue
        _dangerous = verify_command_dangerous_reasons(verify_cmd)
        if _dangerous:
            blocking_errors.append({
                "code": ErrorCode.E027.name,
                "task_id": tid,
                "verify_command": verify_cmd,
                "reasons": _dangerous,
                "message": (
                    "verify_command 含 shell 注入风险，dry-run 拒绝执行（E027）"
                ),
            })
            continue
        try:
            proc = run_shell(verify_cmd, str(project_root), 30)
            failure_class = None
            if proc.returncode != 0:
                failure_class = classify_dry_run_failure(
                    verify_cmd, proc.returncode,
                    _decode_subprocess_output(proc.stderr)[:500],
                    _decode_subprocess_output(proc.stdout)[:300],
                )
                if failure_class == "assertion_mismatch":
                    blocking_errors.append({
                        "code": ErrorCode.E028.name,
                        "task_id": tid,
                        "verify_command": verify_cmd,
                        "exit_code": proc.returncode,
                        "stderr": _decode_subprocess_output(proc.stderr)[:500],
                        "message": (
                            "dry-run 断言不匹配（assertion_mismatch）：verify_command "
                            "引用现有文件但断言失败/语法错误，注册已阻断（E028）"
                        ),
                    })
            dry_run_results.append({
                "task_id": tid,
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "failure_class": failure_class,
                "stderr": _decode_subprocess_output(proc.stderr)[:500],
                "hint": (
                    "dry-run 仅提示不阻断注册：实现未完成时失败属预期（可忽略）；"
                    "若断言应匹配现有文件而失败，则 verify_command 定义可能有误，建议核对"
                ),
            })
        except (subprocess.SubprocessError, OSError):
            # 运行环境异常：静默跳过，不影响 amend 主流程
            continue

    # E024/E027 阻断：amend 注册点对新增/变更任务的 verify_command 质量校验
    # （E024 缺 basetemp / E027 不安全段 → 阻断注册，不再等 done 期 E014）
    # AC4 grandfather：仅收集 changed 任务（或新注册任务）的 E024/E027，
    # 存量任务命中仅 warning 不阻断——避免存量 E027/E024 阻塞任何后续 amend。
    qerrors = validate_quality(master)
    changed_set = set(changed)
    for qe in qerrors:
        if qe.code in (ErrorCode.E024, ErrorCode.E027):
            # 从 path（$.tasks[i].verify_command）解析任务 index → 定位 task id
            import re as _re
            m_idx = _re.search(r"\$\.tasks\[(\d+)\]", qe.path)
            tid = None
            if m_idx:
                idx = int(m_idx.group(1))
                if 0 <= idx < len(master.tasks):
                    tid = master.tasks[idx].get("id")
            # grandfather：仅 changed 任务阻断，存量（未变更）命中跳过
            if tid is not None and tid in changed_set:
                blocking_errors.append({
                    "code": qe.code.name,
                    "path": qe.path,
                    "message": qe.message,
                })

    if blocking_errors:
        raise OrchdError(
            ErrorCode.E028 if any(e.get("code") == "E028" for e in blocking_errors)
            else ErrorCode.E027,
            "amend_blocked: verify_command 校验未通过（注册已阻断）",
            blocking_errors,
        )
    if dry_run_results:
        result["verify_dry_run"] = dry_run_results
    return result


def _cmd_request(args) -> dict:
    """获取下一个可认领的候选任务。

    CLI 参数: args.capabilities、args.exclude、
    args.sort（importance/downstream/hours）、args.auto_claim（--auto-claim，
    候选返回后自动 claim）。agent 身份由引擎按宿主注入的 ORCHD_SESSION_ID
    （session-id-fingerprint）。
    不再有 --agent/--role。
    返回: 匹配的任务信息或空结果。--auto-claim 时附加 claim 结果（或错误）。
    """
    from orchd.ledger import Store
    from orchd.onboard import claim, request

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _resolve_agent_id(orchd_dir)
    enforce_self_review_block = bool(
        master.config.get("enforce_self_review_block") if hasattr(master, "config") else False
    )
    result = request(
        store, tasks, agent_id=agent_id,
        capabilities=_flatten_nargs(args.capabilities),
        exclude=_flatten_nargs(args.exclude),
        sort_key=args.sort,
        max_active=getattr(args, "max_active", None),
        importance_thresholds=(
            (master.config.get("importance") if hasattr(master, "config") else None)
            or None
        ),
        enforce_self_review_block=enforce_self_review_block,
    )

    # --auto-claim：候选非空时自动 claim（绕过人工确认）。
    # 默认禁用：仅当 _master.json 顶层 config.allow_auto_claim 显式为 true 时，
    # agent 才可调用 --auto-claim（用户明确授权）；否则结构化拒绝，防止无人值守
    # agent 绕过 claim 人工确认闸门连续领任务。
    if getattr(args, "auto_claim", False):
        allow_auto_claim = bool(
            (master.config.get("allow_auto_claim") if hasattr(master, "config") else False)
        )
        if not allow_auto_claim:
            result["auto_claim_disabled"] = True
            result["error"] = {
                "code": "E032",
                "message": "自动认领（--auto-claim）默认禁用",
                "details": (
                    "agent 不得擅自使用 --auto-claim 连续领任务。仅在用户明确于 "
                    "_master.json 顶层 config.allow_auto_claim 设为 true 后才允许。"
                ),
            }
            return result
    if getattr(args, "auto_claim", False) and result.get("candidate"):
        candidate_id = result["candidate"]["task_id"]
        shared = master.shared if hasattr(master, "shared") else None
        claim_result = claim(
            store, tasks, agent_id=agent_id, task_id=candidate_id,
            project_root=orchd_dir.parent, shared=shared,
            with_context=getattr(args, "with_context", False),
            enforce_self_review_block=enforce_self_review_block,
        )
        result["auto_claimed"] = True
        result["claimed"] = claim_result
    return result


def _cmd_pool(args) -> dict:
    """列出当前就绪池中的可认领任务。

    CLI 参数: args.capabilities（可选能力过滤）、args.show_all（--all，包含非就绪任务）。
    返回: {"pool": [...], "pool_size": N}。
    """
    from orchd.ledger import Store
    from orchd.pool import (
        build_pool,
        compute_downstream_blocked,
        effective_importance,
        sort_candidates,
    )

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    state = store.replay()
    imp_thresholds = master.config.get("importance") if hasattr(master, "config") else None

    def _entry(task: dict, blocked_count: int) -> dict:
        entry = {
            "task_id": task.get("id", ""),
            "name": task.get("name", ""),
            "brief": task.get("brief", ""),
            "module": task.get("module", ""),
            "importance": effective_importance(task, blocked_count, imp_thresholds),
            "blocked_downstream_count": blocked_count,
            "source": task.get("source"),
        }
        if "difficulty" in task:
            entry["difficulty"] = task["difficulty"]
        return entry

    blocked_counts = compute_downstream_blocked(tasks, state)

    if args.show_all:
        # --all：包含非就绪任务并附加 status 字段
        all_entries = []
        for task in tasks:
            tid = task.get("id", "")
            ts = state.get(tid)
            entry = _entry(task, blocked_counts.get(tid, 0))
            entry["status"] = ts.status if ts else "pending"
            all_entries.append(entry)
        return {"pool": all_entries, "pool_size": len(all_entries), "all": True}

    candidates = build_pool(
        tasks, state, capabilities=_flatten_nargs(args.capabilities)
    )
    candidates = sort_candidates(candidates, importance_thresholds=imp_thresholds)
    return {
        "pool": [_entry(c.task, c.blocked_downstream_count) for c in candidates],
        "pool_size": len(candidates),
    }


def claim_preview(
    store,
    tasks: list[dict[str, Any]],
    agent_id: str,
    task_id: str,
    role: str = "implementer",
    project_root: Path | None = None,
    review_type: str | None = None,
    enforce_self_review_block: bool = False,
) -> dict[str, Any]:
    """claim 前确认闸门预览（task-claim-confirm-gate，只读，不写事件不建分支）。

    显式 ``orchd claim``（无 ``--confirm``）先展示预览：claim_type
    （implementer/reviewer）、任务基本信息、当前状态、git 状况（分支 + 工作区
    干净度）、将执行动作，以及 reviewer 角色的 ``review_phase``（spec/code）、
    ``reviewers`` 名单与 self-review 预期校验（E016）。用户确认无误后再以
    ``--confirm`` 真正执行 claim（走 ``onboard.claim`` 的锁内 check-then-act）。

    与 ``onboard.claim`` 的区别：本函数只读（replay + 派生缓存 + git 状态探测），
    不写任何事件、不建分支；所有"校验预期"仅作透明展示，不抛错阻断。
    预览逻辑归属 CLI 层：任务 files_to_edit 仅声明 orchd/cli.py（mod-core）。

    Args:
        store: ``orchd.ledger.Store`` 实例。
        review_type: reviewer 认领时显式指定的审查阶段（spec/code）。
    """
    from orchd.gitops import check_workspace_state
    from orchd.onboard import _extract_last_done
    from orchd.report import task_revive_markers

    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E008, f"task '{task_id}' not found in master",
                         [{"task_id": task_id}])

    state = store.replay()
    derived = store.scan_task_derived()
    ts = state.get(task_id)
    status = ts.status if ts else "pending"
    # review-unify-r2：unified 模式下审查阶段显示为 unified（单阶段），
    # 不再回落 spec；two_phase 模式保持 spec/code 展示。
    from orchd.ledger import resolve_review_mode
    if resolve_review_mode(store.orchd_dir) == "unified":
        current_phase = "unified"
    else:
        current_phase = (ts.review_phase if ts else None) or "spec"

    preview: dict[str, Any] = {
        "claim_type": role,
        "task_id": task_id,
        "name": task_def.get("name", ""),
        "brief": task_def.get("brief", ""),
        "module": task_def.get("module", ""),
        "depends_on": task_def.get("depends_on", []),
        "reviewers": task_def.get("reviewers", []),
        "current_status": status,
        "review_phase": current_phase,
    }

    # git 状况（best-effort，非 git 环境降级为 available:false）
    if project_root is not None:
        ws = check_workspace_state(project_root)
        preview["git"] = {
            "available": ws.get("available", False),
            "branch": ws.get("branch"),
            "clean": ws.get("clean"),
        }
    else:
        preview["git"] = {"available": False}

    # 将执行动作（claim() 会做的事）
    preview["actions"] = [
        f"写 {'REVIEW_CLAIMED' if role == 'reviewer' else 'CLAIMED'} 事件到 ledger",
        f"创建/切换 task/{task_id} 分支",
    ]

    # 校验预期（与 claim() 锁内校验一致，透明展示；失败仅提示不阻断预览）
    if role == "reviewer":
        done_author, _ = _extract_last_done(store, task_id, derived)
        preview["done_by"] = done_author
        is_self = bool(done_author and done_author == agent_id)
        preview["expected_checks"] = [
            {"check": "任务处于 in_review（可认领审查）",
             "expected_pass": status == "in_review"},
            {"check": "agent 在任务 reviewers 名单内",
             "expected_pass": agent_id in task_def.get("reviewers", [])},
            {"check": "审查阶段与当前 review_phase 匹配",
             "expected_pass": (not review_type) or review_type == current_phase},
        ]
        # 自审（E016）降级为提示项：默认不阻断，仅标注；enable 时才作为检查生效
        if is_self:
            preview["expected_checks"].append({
                "check": "自审提示（E016：实现者 = 审查者）",
                "expected_pass": not enforce_self_review_block,
                "note": "默认仅提示不阻断；线上版 config.enforce_self_review_block=true 时该检查才生效并阻断",
            })
    else:
        preview["expected_checks"] = [
            {"check": "任务处于 pending（可认领）",
             "expected_pass": status == "pending"},
            {"check": "依赖全部满足",
             "expected_pass": all(
                 (state.get(d).status if state.get(d) else "pending")
                 in ("completed", "cancelled") for d in task_def.get("depends_on", [])
             )},
            {"check": "未被其他 agent 认领",
             "expected_pass": not (ts and ts.claimed_by and ts.claimed_by != agent_id)},
        ]
    # 复活标记（task-force-status-revive-audit）：该任务曾有 completed→pending 复活
    # 历史时透明展示（reason+evidence_sha+时间），正常任务不展示（零误伤）。
    markers = task_revive_markers(store)
    if task_id in markers:
        preview["revive_marker"] = markers[task_id]
    return preview


def _cmd_claim(args) -> dict:
    """认领指定任务。

    CLI 参数: args.task（必需）、args.confirm（--confirm，确认执行认领）。
    agent 身份由引擎按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint）；claim 按任务
    当前状态自动分流：in_review → 审查认领（REVIEW_CLAIMED），pending →
    实现认领（CLAIMED），不再有 --agent/--role。
    返回: 认领事件信息；无 --confirm 时仅返回确认闸门预览
    （confirm_required:true + preview，不写事件、不建分支）。
    """
    from orchd.ledger import Store
    from orchd.onboard import claim

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    shared = master.shared if hasattr(master, "shared") else None
    project_root = orchd_dir.parent
    agent_id = _require_agent_id(orchd_dir)
    role = _detect_claim_role(store, tasks, args.task)
    enforce_self_review_block = bool(
        master.config.get("enforce_self_review_block") if hasattr(master, "config") else False
    )

    # 确认闸门：无 --confirm 仅输出预览（只读，不写事件、不建分支）
    if not getattr(args, "confirm", False):
        preview = claim_preview(
            store, tasks, agent_id=agent_id, task_id=args.task,
            role=role, project_root=project_root,
            review_type=getattr(args, "review_type", None),
            enforce_self_review_block=enforce_self_review_block,
        )
        result = {
            "confirm_required": True,
            "claim_type": preview["claim_type"],
            "hint": "预览模式：确认无误后请加 --confirm 真正执行认领（写事件 + 建分支）",
            "preview": preview,
        }
        warning = _identity_warning(agent_id, orchd_dir)
        if warning:
            result["warning"] = warning
        collision = _session_collision_warning(
            agent_id, store, exclude_task_id=args.task,
            review_task_id=args.task if role == "reviewer" else None,
        )
        if collision:
            result["session_collision_warning"] = collision
        return result

    result = claim(
        store, tasks, agent_id=agent_id, task_id=args.task,
        project_root=project_root, shared=shared,
        review_type=getattr(args, "review_type", None),
        with_context=getattr(args, "with_context", False),
        enforce_self_review_block=enforce_self_review_block,
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    collision = _session_collision_warning(
        agent_id, store, exclude_task_id=args.task,
        review_task_id=args.task if role == "reviewer" else None,
    )
    if collision:
        result["session_collision_warning"] = collision
    return result


def _cmd_done(args) -> dict:
    """报告任务完成，提交变更描述与可选的关切事项。

    CLI 参数: args.task（必需）、args.changes / args.changes_file（二选一，
    变更描述）、args.concerns（可选关切事项）。agent 身份由引擎自动按宿主
    注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 完成事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import done

    changes = _resolve_text_arg(args.changes, args.changes_file, "--changes", "--changes-file")
    tasks, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    result = done(
        store, tasks, agent_id=agent_id, task_id=args.task,
        changes_description=changes, concerns=args.concerns,
        project_root=orchd_dir.parent,
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    return result


def _cmd_review(args) -> dict:
    """提交审查结果（spec review 或 code review）。

    CLI 参数: args.task（必需）、args.type（spec/code）、
    args.verdict（APPROVED/CHANGES_REQUESTED）、
    args.comments / args.comments_file（可选，二选一）。agent 身份由引擎自动按
    宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 审查事件信息。
    """
    from orchd.ledger import Store
    from orchd.review import review_submit

    comments = _resolve_text_arg(
        args.comments, args.comments_file, "--comments", "--comments-file",
        required=False,
    )
    tasks, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    result = review_submit(
        store, tasks, agent_id=agent_id, task_id=args.task,
        review_type=args.type, verdict=args.verdict, comments=comments,
        project_root=orchd_dir.parent,
    )
    warning = _identity_warning(agent_id, orchd_dir)
    if warning:
        result["warning"] = warning
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("task_status") == "completed":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _cmd_retract(args) -> dict:
    """撤回已提交的事件。

    CLI 参数: args.event（必需，事件 ID）、args.reason（必需）。agent 身份由
    引擎自动按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 撤回事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import retract

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    return retract(
        store, agent_id=agent_id, target_event_id=args.event,
        reason=args.reason, project_root=orchd_dir.parent,
    )


def _cmd_force_status(args) -> dict:
    """强制设置任务状态（用于恢复僵死任务或手动干预）。

    CLI 参数: args.task（必需）、args.status（必需，目标状态）、
    args.reason（必需）、args.assignee（可选，指定认领人）、
    args.force（可选，逃生口二次确认——claimed→completed / cancelled→pending）、
    args.evidence_sha（可选，completed→pending 复活所需的 git 证据 commit SHA）。
    agent 身份由引擎自动按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 强制状态变更事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import force_status

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    result = force_status(
        store, agent_id=agent_id, task_id=args.task,
        target_status=args.status, reason=args.reason, assignee=args.assignee,
        force=args.force, project_root=orchd_dir.parent,
        evidence_sha=args.evidence_sha,
    )
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("new_status") == "cancelled":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _cmd_merge_ack(args) -> dict:
    """merge_warning 人工销账：登记 .orchd/merge-acks.json（task-merge-warning-ack）。

    CLI 参数: args.task（必需，已人工确认的 task_id）、args.reason（必需，确认原因）。
    与 resolve_sha 自动销账互补：人工路径兜底旧事件 / 无 sha 场景，登记后
    audit-merge 不再报 merge_warning_unresolved。
    返回: 登记结果 {acked, task_id, acked_at, reason}。
    """
    from orchd.report import merge_ack

    _, orchd_dir, _ = _load_tasks()
    return merge_ack(orchd_dir.parent, args.task, args.reason)


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


def _cmd_ideas_archive(args) -> dict:
    """手动触发 IDEAS 自动归档（一次性回填存量条目 + 后续可手动触发）。

    CLI 参数: 无。
    返回: 归档结果字典（archived 标题列表 + kept 数量 + 可选 commit 字段）。
    """
    _, orchd_dir, _ = _load_tasks()
    return _maybe_archive_ideas(orchd_dir)


def _cmd_doctor(args):
    """检测 git 仓库完整性（只读，task-git-doctor-command）。

    CLI 参数: args.path（项目根目录，默认当前目录）。
    返回: (result, exit_code) 元组——检出任一 fail 项时 exit_code 为 1，
    供 session 三连检查脚本化复用。
    """
    from orchd.doctor import doctor

    result = doctor(Path(args.path))
    if not result["repo_ok"]:
        return result, 1
    return result, 0


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


def _cmd_idea_propose(args) -> dict:
    """为灵感追加 status: study 条目到 IDEAS.md（idea-write-gate，agent 执行）。

    CLI 参数: args.title / args.feasibility。
    返回: 提案结果字典（proposed / title / commit）。
    """
    from orchd.intake import idea_propose

    orchd_dir = _find_orchd_dir()
    return idea_propose(orchd_dir.parent, args.title, args.feasibility)


def _cmd_idea_confirm(args) -> dict:
    """将 status: study 条目升为 pending（idea-write-gate，仅用户执行）。

    CLI 参数: args.title。
    返回: 确认结果字典（confirmed / title / new_status / commit）。
    """
    from orchd.intake import idea_confirm

    orchd_dir = _find_orchd_dir()
    return idea_confirm(orchd_dir.parent, args.title)


def _cmd_idea_drop(args) -> dict:
    """将 status: study 条目降为 dropped（idea-write-gate，仅用户执行）。

    CLI 参数: args.title。
    返回: 丢弃结果字典（dropped / title / new_status / commit）。
    """
    from orchd.intake import idea_drop

    orchd_dir = _find_orchd_dir()
    return idea_drop(orchd_dir.parent, args.title)


def _cmd_session_start(args) -> dict:
    """开启新的引擎级会话（Session Identity Layer）。

    CLI 参数: args.agent（可选具名 agent）。
    返回: session runtime 信息（session_id / session_token / fingerprint / path）。
    宿主接入层应将 session_token 写入 ORCHD_SESSION_ID，供后续命令解析身份。
    """
    from orchd.ledger import session_start

    orchd_dir = _find_orchd_dir()
    return session_start(orchd_dir, agent_name=args.agent)


def _cmd_session_current(args) -> dict:
    """显示当前会话 runtime 信息；未开启时返回 E033。"""
    from orchd.ledger import session_current

    orchd_dir = _find_orchd_dir()
    return session_current(orchd_dir)


def _cmd_session_end(args) -> dict:
    """结束当前会话：标记 runtime inactive + best-effort 释放 session lock。"""
    from orchd.gitops import release_session_lock_if_owned
    from orchd.ledger import session_end

    orchd_dir = _find_orchd_dir()
    result = session_end(orchd_dir)
    agent_id = result.get("fingerprint") or _resolve_agent_id(orchd_dir)
    result["session_lock_released"] = release_session_lock_if_owned(orchd_dir, agent_id)
    return result


def _cmd_status(args) -> dict:
    """获取全局状态快照或单任务详情。

    CLI 参数: args.task（可选，任务 ID）、args.text（--text，人类可读表格输出）、
    args.audit_merge（--audit-merge，附加只读 merge 巡检）。
    返回: 全局状态字典或单任务详情字典；若 --text 模式则直接打印表格并返回 None。
    """
    from orchd.ledger import Store
    from orchd.report import intake_audit, merge_audit, revive_audit, status, task_integrity_audit

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _resolve_agent_id(orchd_dir)
    # 红线 8（R3）：status 前置校验运行时文件完整性（只读告警，不阻断）
    integrity_warnings = store.check_integrity()
    result = status(
        store, tasks, project=master.project, text=args.text, task_id=args.task,
        project_root=orchd_dir.parent,
    )
    # 会话指纹碰撞只读告警（task-contract-session-collision-warning）：不阻断、不落状态
    if not args.text:
        exclude = args.task or _current_task_from_branch(orchd_dir.parent)
        collision = _session_collision_warning(agent_id, store, exclude_task_id=exclude)
        if collision:
            result["session_collision_warning"] = collision
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if args.audit_merge and args.task is None:
        result["merge_audit"] = merge_audit(store, tasks, orchd_dir.parent)
    if getattr(args, "audit_intake", False) and args.task is None:
        result["intake_audit"] = intake_audit(orchd_dir.parent)
    if getattr(args, "audit_revive", False) and args.task is None:
        result["revive_audit"] = revive_audit(store, tasks, orchd_dir.parent)
    if getattr(args, "audit_task", False) and args.task is None:
        result["audit_task"] = task_integrity_audit(
            store, tasks, orchd_dir.parent, scope="merged"
        )
    if args.text and "_text" in result:
        # --text 为人类可读展示层：只输出表格，不再混入 JSON。
        # 末尾追加无感引导文字（task-guide-seamless-guidance，best-effort）。
        table = result.pop("_text")
        try:
            from orchd.guide import status_guidance_text
            # status 命令前置必有 master → has_master=True（空项目时显示 empty_project 而非 first_time）
            table += status_guidance_text(store.replay(), tasks, has_master=True)
        except Exception:
            pass  # 引导失败静默跳过，不影响表格输出
        print(table)
        return None
    return result


def _cmd_watchdog(args):
    """巡检僵死任务（实现者超时 / 审查者超时）。

    CLI 参数: args.timeout（超时分钟数，默认 60）。
    返回: 巡检结果字典；若存在僵死任务则以 ``(result, 1)`` 元组返回以设置非零 exit code。
    """
    from orchd.ledger import Store
    from orchd.report import watchdog

    tasks, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    result = watchdog(
        store, tasks, timeout_min=args.timeout, project_root=orchd_dir.parent,
        agent_id=_resolve_agent_id(orchd_dir), takeover=args.takeover,
    )
    if result["stuck_count"] > 0:
        return result, 1
    return result


if __name__ == "__main__":
    sys.exit(main())
