"""Orchd CLI 路由：argparse 子命令 + 统一 JSON 输出 + 错误捕获。

提供 13 个子命令：validate、bootstrap、init、amend、request、pool、claim、
done、review、retract、force-status、status、watchdog。

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
            _output(data)
            return code
        _output(result)
        return 0
    except OrchdError as exc:
        _output(to_json_response(exc))
        return 1
    except KeyboardInterrupt:
        return 130


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
    p.add_argument("--agent", required=True)
    p.add_argument("--capabilities", nargs="*")
    p.add_argument("--exclude", nargs="*")
    p.add_argument("--role", default="implementer", choices=["implementer", "reviewer"])
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
    p.add_argument("--agent", required=True)
    p.add_argument("--role", default="implementer", choices=["implementer", "reviewer"])
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
    p.add_argument("--agent", required=True)
    p.add_argument("--changes")
    p.add_argument("--changes-file", help="从文件读取变更描述（UTF-8），与 --changes 二选一")
    p.add_argument("--concerns")
    p.set_defaults(func=_cmd_done)

    # review
    p = sub.add_parser("review", help="提交审查结果")
    p.add_argument("--task", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--type", required=True, choices=["spec", "code"])
    p.add_argument("--verdict", required=True, choices=["APPROVED", "CHANGES_REQUESTED"])
    p.add_argument("--comments")
    p.add_argument("--comments-file", help="从文件读取审查意见（UTF-8），与 --comments 二选一")
    p.set_defaults(func=_cmd_review)

    # retract
    p = sub.add_parser("retract", help="撤回事件")
    p.add_argument("--event", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=_cmd_retract)

    # force-status
    p = sub.add_parser("force-status", help="强制设置任务状态")
    p.add_argument("--task", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--assignee")
    p.add_argument("--force", action="store_true",
                   help="显式确认走逃生口（claimed→completed / cancelled→pending）")
    p.set_defaults(func=_cmd_force_status)

    # status
    p = sub.add_parser("status", help="全局状态快照；可跟 task-id 查单任务详情")
    p.add_argument("task", nargs="?", default=None, help="可选：任务 ID，查询单任务详情")
    p.add_argument("--text", action="store_true")
    p.add_argument("--audit-merge", action="store_true",
                   help="附加 merge 巡检：completed 任务对应 task/{id} 分支未并入 main 的告警清单（只读）")
    p.set_defaults(func=_cmd_status)

    # watchdog
    p = sub.add_parser("watchdog", help="僵死任务巡检")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=_cmd_watchdog)

    # ideas-archive
    p = sub.add_parser("ideas-archive", help="自动归档已完结的 IDEAS 条目")
    p.set_defaults(func=_cmd_ideas_archive)

    # doctor
    p = sub.add_parser("doctor", help="git 仓库完整性只读检测")
    p.add_argument("--path", default=".", help="项目根目录（含 .git），默认当前目录")
    p.set_defaults(func=_cmd_doctor)

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


def _identity_warning(agent_id: str, orchd_dir: Path) -> dict[str, Any] | None:
    """比对 git config user.name 与 agent_id，不一致返回 E021 warning（不阻断）。

    git 不可用 / user.name 未配置 / 与 agent_id 一致 → 返回 None（无 warning）。
    用于写命令（claim/done/review）前的身份审计（ROADMAP 1.1 L5）：
    仅提示，不阻断状态机。
    """
    import subprocess

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


def _load_tasks(master_path: str | None = None) -> tuple[list, Path, Any]:
    """加载 master 并返回 (tasks, orchd_dir, master)。"""
    from orchd.spec import load_master

    if master_path:
        path = Path(master_path)
    else:
        orchd_dir = _find_orchd_dir()
        path = orchd_dir / "_master.json"
    master = load_master(path)
    orchd_dir = path.parent
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
    from orchd.spec import load_master, validate_quality, validate_references, validate_structure

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

    if structure_errors:
        return {
            "valid": False,
            "errors": [{"code": e.code.name, "path": e.path, "message": e.message} for e in structure_errors],
            "warnings": [{"code": e.code.name, "path": e.path, "message": e.message} for e in quality_warnings],
        }
    return {
        "valid": True,
        "errors": [],
        "warnings": [{"code": e.code.name, "path": e.path, "message": e.message} for e in quality_warnings],
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
    """
    from orchd.spec import load_master
    from orchd.split import init

    master = load_master(args.master)
    orchd_dir = Path(args.master).parent
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
        result["commit"] = ensure_committed(
            project_root,
            [str(Path(args.master)), str(ws_root / "IDEAS.md")],
            f"chore(intake): orchd amend — {summary}",
        )

    # dry-run 试跑新增/变更任务的 verify_command（与 done 相同 shell 执行、同 cwd、
    # 限时 30s；2026-08-08 升级：assertion_mismatch 类失败阻断注册（E028），
    # E024/E027（缺 basetemp / 不安全段）阻断注册；expected_pending 仅提示）
    from orchd.split import classify_dry_run_failure
    from orchd.spec import validate_quality

    task_map = {t.get("id", ""): t for t in master.tasks}
    dry_run_results: list[dict[str, Any]] = []
    blocking_errors: list[dict[str, Any]] = []
    for tid in changed:
        verify_cmd = task_map.get(tid, {}).get("verify_command")
        if not verify_cmd:
            continue
        try:
            proc = subprocess.run(
                verify_cmd, shell=True, cwd=str(project_root),
                capture_output=True, timeout=30,
            )
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

    CLI 参数: args.agent（必需）、args.capabilities、args.exclude、
    args.role（implementer/reviewer）、args.sort（importance/downstream/hours）、
    args.auto_claim（--auto-claim，候选返回后自动 claim）。
    返回: 匹配的任务信息或空结果。--auto-claim 时附加 claim 结果（或错误）。
    """
    from orchd.ledger import Store
    from orchd.onboard import claim, request

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    result = request(
        store, tasks, agent_id=args.agent,
        capabilities=_flatten_nargs(args.capabilities),
        exclude=_flatten_nargs(args.exclude),
        role=args.role,
        sort_key=args.sort,
        max_active=getattr(args, "max_active", None),
        importance_thresholds=(
            (master.config.get("importance") if hasattr(master, "config") else None)
            or None
        ),
    )

    # --auto-claim：候选非空时自动 claim（绕过人工确认）。
    # 候选为空（含 review_priority 提示）不触发 claim，原样返回。
    if getattr(args, "auto_claim", False) and result.get("candidate"):
        candidate_id = result["candidate"]["task_id"]
        shared = master.shared if hasattr(master, "shared") else None
        claim_result = claim(
            store, tasks, agent_id=args.agent, task_id=candidate_id,
            role=args.role, project_root=orchd_dir.parent, shared=shared,
            with_context=getattr(args, "with_context", False),
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

    task_map = {t.get("id", ""): t for t in tasks}
    task_def = task_map.get(task_id)
    if task_def is None:
        raise OrchdError(ErrorCode.E008, f"task '{task_id}' not found in master",
                         [{"task_id": task_id}])

    state = store.replay()
    derived = store.scan_task_derived()
    ts = state.get(task_id)
    status = ts.status if ts else "pending"
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
        preview["expected_checks"] = [
            {"check": "任务处于 in_review（可认领审查）",
             "expected_pass": status == "in_review"},
            {"check": "agent 在任务 reviewers 名单内",
             "expected_pass": agent_id in task_def.get("reviewers", [])},
            {"check": "审查阶段与当前 review_phase 匹配",
             "expected_pass": (not review_type) or review_type == current_phase},
            {"check": "非自审（E016：实现者 ≠ 审查者）",
             "expected_pass": not (done_author and done_author == agent_id)},
        ]
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
    return preview


def _cmd_claim(args) -> dict:
    """认领指定任务。

    CLI 参数: args.task（必需）、args.agent（必需）、args.role（implementer/reviewer）、
    args.confirm（--confirm，确认执行认领）。
    返回: 认领事件信息；无 --confirm 时仅返回确认闸门预览
    （confirm_required:true + preview，不写事件、不建分支）。
    """
    from orchd.ledger import Store
    from orchd.onboard import claim

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    shared = master.shared if hasattr(master, "shared") else None
    project_root = orchd_dir.parent

    # 确认闸门：无 --confirm 仅输出预览（只读，不写事件、不建分支）
    if not getattr(args, "confirm", False):
        preview = claim_preview(
            store, tasks, agent_id=args.agent, task_id=args.task,
            role=args.role, project_root=project_root,
            review_type=getattr(args, "review_type", None),
        )
        result = {
            "confirm_required": True,
            "claim_type": preview["claim_type"],
            "hint": "预览模式：确认无误后请加 --confirm 真正执行认领（写事件 + 建分支）",
            "preview": preview,
        }
        warning = _identity_warning(args.agent, orchd_dir)
        if warning:
            result["warning"] = warning
        return result

    result = claim(
        store, tasks, agent_id=args.agent, task_id=args.task,
        role=args.role, project_root=project_root, shared=shared,
        review_type=getattr(args, "review_type", None),
        with_context=getattr(args, "with_context", False),
    )
    warning = _identity_warning(args.agent, orchd_dir)
    if warning:
        result["warning"] = warning
    return result


def _cmd_done(args) -> dict:
    """报告任务完成，提交变更描述与可选的关切事项。

    CLI 参数: args.task（必需）、args.agent（必需）、
    args.changes / args.changes_file（二选一，变更描述）、
    args.concerns（可选关切事项）。
    返回: 完成事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import done

    changes = _resolve_text_arg(args.changes, args.changes_file, "--changes", "--changes-file")
    tasks, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    result = done(
        store, tasks, agent_id=args.agent, task_id=args.task,
        changes_description=changes, concerns=args.concerns,
        project_root=orchd_dir.parent,
    )
    warning = _identity_warning(args.agent, orchd_dir)
    if warning:
        result["warning"] = warning
    return result


def _cmd_review(args) -> dict:
    """提交审查结果（spec review 或 code review）。

    CLI 参数: args.task（必需）、args.agent（必需）、args.type（spec/code）、
    args.verdict（APPROVED/CHANGES_REQUESTED）、
    args.comments / args.comments_file（可选，二选一）。
    返回: 审查事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import review_submit

    comments = _resolve_text_arg(
        args.comments, args.comments_file, "--comments", "--comments-file",
        required=False,
    )
    tasks, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    result = review_submit(
        store, tasks, agent_id=args.agent, task_id=args.task,
        review_type=args.type, verdict=args.verdict, comments=comments,
        project_root=orchd_dir.parent,
    )
    warning = _identity_warning(args.agent, orchd_dir)
    if warning:
        result["warning"] = warning
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("task_status") == "completed":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _cmd_retract(args) -> dict:
    """撤回已提交的事件。

    CLI 参数: args.event（必需，事件 ID）、args.agent（必需）、args.reason（必需）。
    返回: 撤回事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import retract

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    return retract(
        store, agent_id=args.agent, target_event_id=args.event,
        reason=args.reason, project_root=orchd_dir.parent,
    )


def _cmd_force_status(args) -> dict:
    """强制设置任务状态（用于恢复僵死任务或手动干预）。

    CLI 参数: args.task（必需）、args.status（必需，目标状态）、
    args.agent（必需）、args.reason（必需）、args.assignee（可选，指定认领人）、
    args.force（可选，逃生口二次确认——claimed→completed / cancelled→pending）。
    返回: 强制状态变更事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import force_status

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    result = force_status(
        store, agent_id=args.agent, task_id=args.task,
        target_status=args.status, reason=args.reason, assignee=args.assignee,
        force=args.force,
    )
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("new_status") == "cancelled":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _maybe_archive_ideas(orchd_dir: Path) -> dict:
    """best-effort：任务进入终态后触发 IDEAS 归档并自动提交。

    加载 master → 调 ``archive_resolved_ideas`` → 若有归档条目则
    ``ensure_committed([IDEAS.md, IDEAS-archive.md])``。非 main 分支降级
    为不提交（对齐 amend 的 ``not_on_main`` 语义），避免把归档提交进任务分支。
    任何异常静默降级，不阻断调用方。

    Returns:
        归档结果；若无可归档条目或异常，返回 ``{"archived": [], ...}``。
    """
    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.ideas import archive_resolved_ideas
    from orchd.spec import load_master

    master_path = orchd_dir / "_master.json"
    if not master_path.exists():
        return {"archived": [], "kept": 0, "skipped": "no_master"}
    master = load_master(master_path)
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


def _cmd_status(args) -> dict:
    """获取全局状态快照或单任务详情。

    CLI 参数: args.task（可选，任务 ID）、args.text（--text，人类可读表格输出）、
    args.audit_merge（--audit-merge，附加只读 merge 巡检）。
    返回: 全局状态字典或单任务详情字典；若 --text 模式则直接打印表格并返回 None。
    """
    from orchd.ledger import Store
    from orchd.report import merge_audit, status

    tasks, orchd_dir, master = _load_tasks()
    store = Store(orchd_dir)
    # 红线 8（R3）：status 前置校验运行时文件完整性（只读告警，不阻断）
    integrity_warnings = store.check_integrity()
    result = status(
        store, tasks, project=master.project, text=args.text, task_id=args.task
    )
    if integrity_warnings:
        result["integrity_warnings"] = integrity_warnings
    if args.audit_merge and args.task is None:
        result["merge_audit"] = merge_audit(store, tasks, orchd_dir.parent)
    if args.text and "_text" in result:
        # --text 为人类可读展示层：只输出表格，不再混入 JSON
        print(result.pop("_text"))
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
    result = watchdog(store, tasks, timeout_min=args.timeout)
    if result["stuck_count"] > 0:
        return result, 1
    return result


if __name__ == "__main__":
    sys.exit(main())
