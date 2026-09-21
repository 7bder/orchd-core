"""Orchd CLI 路由：control 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-workflow-control）：
  - _cmd_amend: amend 命令（snapshot 增量更新）
  - _cmd_retract: retract 命令（事件撤回）
  - _cmd_force_status: force-status 命令（强制状态转换）
  - _cmd_merge_ack: merge-ack 命令（merge_warning 人工销账）

3a 阶段说明：本模块是 control 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

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

from orchd.ledger import structured_error

from orchd.cli.identity import _identity_warning, _require_agent_id
from orchd.cli.skeleton import _cli_skeleton


def _log_decl_withdraw(
    task_id: str,
    withdrawn: list[dict[str, str]],
    not_present: list[dict[str, str]],
    branch_overlap: list[str],
) -> None:
    """声明撤回留痕（stderr，结构化；task-decl-withdraw-channel）。

    撤回是声明域变更，必须可追溯——尤其 ``branch_overlap``：被撤回的声明仍出现在
    该任务分支改动集中时，review 期 E010 声明完整性会反向告警。与 session/amend
    降级留痕同风格，不受 ``ORCHD_QUIET`` 抑制；任何异常静默跳过，不影响补丁主流程。
    """
    import json
    import sys

    payload: dict[str, Any] = {
        "action": "decl_withdraw",
        "task_id": task_id,
        "withdrawn": withdrawn,
    }
    if not_present:
        payload["not_present"] = not_present
    if branch_overlap:
        payload["branch_overlap"] = branch_overlap
        payload["hint"] = ("被撤回的声明仍出现在该任务分支改动集中：撤回会使 review 期 E010 "
                           "声明完整性反向告警，请确认是否应先处理该分支再撤回")
    try:
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass
    try:
        print(
            f"orchd ▸ [amend-decl] {json.dumps(payload, ensure_ascii=False)}",
            file=sys.stderr)
    except Exception:
        pass


def _decl_withdraw_branch_overlap(project_root: Path, task_id: str,
                                  paths: list[str]) -> list[str]:
    """被撤回的声明路径中，仍出现在该任务分支改动集里的部分（best-effort）。

    ``git diff --name-only <base>...task/<id>``（三点 = 自分叉点起的改动）与本批
    撤回路径求交。非 git / 分支不存在 / git 不可用 / 探测超时 → 返回空列表（保守
    跳过：绝不因探测失败而误报重叠）。
    """
    import subprocess

    if not paths:
        return []
    branch = f"task/{task_id}"
    try:
        exists = subprocess.run(
            [
                "git", "-C",
                str(project_root), "rev-parse", "--verify", "--quiet", branch
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if exists.returncode != 0:
            return []
        base = "main"
        probe = subprocess.run(
            [
                "git", "-C",
                str(project_root), "rev-parse", "--verify", "--quiet", base
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if probe.returncode != 0:
            base = "master"
        diff = subprocess.run(
            [
                "git", "-C",
                str(project_root), "diff", "--name-only", f"{base}...{branch}"
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if diff.returncode != 0:
            return []
        changed = {ln.strip() for ln in diff.stdout.splitlines() if ln.strip()}
        return sorted(changed & set(paths))
    except (OSError, subprocess.SubprocessError):
        return []


# task-verify-timeout-amend-channel：任务级 verify 预算的 CLI 通道值域。
# schema（schema/_master.schema.json → tasks[].verify_timeout_seconds）只约束
# ``minimum: 1``；上限 600s 为**防呆硬顶**——>10 分钟会让 done 在单点阻塞过久，且
# 违背「verify 只跑定向档」的分级约定（见 .orchd/rules/verify.md）。schema 若新增
# ``maximum``，以 schema 为单一真源并同步本常量：一致性由
# tests/test_cli_control.py::TestAmendVerifyTimeout 机器断言（含引擎默认 120s 不变）。
_VERIFY_TIMEOUT_MIN = 1
_VERIFY_TIMEOUT_MAX = 600


def _validate_verify_timeout_patch(task_id: str, raw: Any) -> int:
    """校验 ``--verify-timeout-seconds`` 取值并返回 int（非法 → E007，调用方不落盘）。

    值域 ``[1, 600]``：下界与 schema 同源，上界为防呆硬顶（见上方常量注释）。
    ``bool`` 显式拒绝——``True`` 是 ``int`` 子类，落盘会写出 ``true`` 而非秒数
    （schema 要求 integer，宁可 CLI 先拒）；非整数文本（``abc`` / ``1.5`` / 空串）
    同样 E007。越界与非法都不写入：调用方在写文件前先走到本函数。
    """
    value: int | None = None
    if not isinstance(raw, bool):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = None
    if value is None:
        raise OrchdError(
            ErrorCode.E007,
            f"verify_timeout_seconds 非法：须为正整数秒（收到 {raw!r}），未写入",
            [{
                "task_id": task_id,
                "field": "verify_timeout_seconds",
                "value": str(raw),
                "expected": f"{_VERIFY_TIMEOUT_MIN}..{_VERIFY_TIMEOUT_MAX} 的整数秒",
            }],
        )
    if not _VERIFY_TIMEOUT_MIN <= value <= _VERIFY_TIMEOUT_MAX:
        raise OrchdError(
            ErrorCode.E007,
            f"verify_timeout_seconds 越界：须在 {_VERIFY_TIMEOUT_MIN}..{_VERIFY_TIMEOUT_MAX} "
            f"秒之间（收到 {value}），未写入",
            [{
                "task_id": task_id,
                "field": "verify_timeout_seconds",
                "value": value,
                "min": _VERIFY_TIMEOUT_MIN,
                "max": _VERIFY_TIMEOUT_MAX,
            }],
        )
    return value


def _validate_additional_sources_patch(
    task_id: str,
    refs: list[str],
    project_root: Path,
) -> None:
    """补丁 additional_sources 引用按与 spec.validate_source 相同口径校验（E025）。

    task-amend-additional-sources-field：amend --additional-sources 补登的每个引用
    须与 source 同口径（格式 ^(idea|roadmap|debug):[a-z0-9-]+$；idea 条目存在且
    status 为 pending；roadmap 章节存在；debug 不校验文件引用）。校验通过返回
    None；任一引用非法 raise E025（未写入任何补丁）。
    """
    import re as _re

    from orchd.ledger import resolve_roadmap_path, resolve_workspace_root
    from orchd.spec import (
        _check_idea_reference,
        _check_roadmap_reference,
    )

    workspace_root = resolve_workspace_root(project_root)
    for j, ref in enumerate(refs):
        if not isinstance(ref, str) or not _re.fullmatch(
                r"(idea|roadmap|debug):[a-z0-9-]+", ref):
            raise OrchdError(
                ErrorCode.E025,
                f"amend_blocked: additional_sources[{j}] '{ref}' 格式非法"
                "（须 ^(idea|roadmap|debug):[a-z0-9-]+$），未写入",
                [{"task_id": task_id, "reference": ref}],
            )
        prefix, _, ref_id = ref.partition(":")
        ref_id = ref_id.strip()
        _errors: list[Any] = []
        if prefix == "idea":
            ideas_path = workspace_root / "IDEAS.md"
            if not ideas_path.exists():
                raise OrchdError(
                    ErrorCode.E025,
                    f"amend_blocked: 引用 {ref} 但 IDEAS.md 文件缺失（无法溯源），未写入",
                    [{"task_id": task_id, "reference": ref}],
                )
            _errors = _check_idea_reference(task_id, 0, ref_id, ideas_path)
        elif prefix == "roadmap":
            roadmap_path = resolve_roadmap_path(project_root)
            if not roadmap_path.exists():
                raise OrchdError(
                    ErrorCode.E025,
                    f"amend_blocked: 引用 {ref} 但 ROADMAP.md 文件缺失（无法溯源），未写入",
                    [{"task_id": task_id, "reference": ref}],
                )
            _errors = _check_roadmap_reference(task_id, 0, ref_id, roadmap_path)
        else:  # debug: 前缀不校验文件引用（与 validate_source 同口径）
            continue
        if _errors:
            raise OrchdError(
                ErrorCode.E025,
                "amend_blocked: additional_sources 引用校验未通过（未写入）",
                [{
                    "task_id": task_id,
                    "reference": ref,
                    "errors": [e.message for e in _errors],
                }],
            )


def _apply_register_proposals(master, register_path: str) -> None:
    """task-amend-register-channel：读提案 JSON（单任务对象或任务数组）追加到 master。

    注册契约（proposals/ schema 单一来源）：提案须为单个任务对象或任务数组。
    非法提案 fail-fast（调用方在写盘前抛出 → canonical 零修改）：
      ① 文件不存在 / ② JSON 解析失败 / ③ 提案为整份 master（疑似基于过期 canonical，
      缺已有任务）/ ④ 缺必填字段 / ⑤ 重复 id。坏 source 由后续 amend() 的 E025 口径拒绝。
    """
    import json as _json

    proposal_file = Path(register_path)
    if not proposal_file.is_file():
        raise OrchdError(
            ErrorCode.E007,
            "register_proposal_missing: 提案文件不存在",
            [{
                "register": str(proposal_file),
                "hint": "传入 proposals/<id>.json（单任务对象）路径",
            }],
        )
    try:
        data = _json.loads(proposal_file.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrchdError(
            ErrorCode.E002,
            f"register_proposal_invalid_json: {exc}",
            [{"register": str(proposal_file)}],
        )
    if isinstance(data, dict) and "tasks" in data:
        raise OrchdError(
            ErrorCode.E007,
            "register_proposal_is_master: 提案应为单个任务对象或任务数组，收到整份 master"
            "（疑似基于过期 canonical，缺已有任务）",
            [{
                "register": str(proposal_file),
                "hint": "请把单任务定义写入 proposals/<id>.json 后重试",
            }],
        )
    proposals = data if isinstance(data, list) else [data]
    existing = {t.get("id") for t in master.tasks}
    required = (
        "id", "name", "brief", "module", "depends_on", "estimated_hours",
        "difficulty", "requires", "acceptance_criteria", "files_to_edit", "source",
    )
    for prop in proposals:
        if not isinstance(prop, dict):
            raise OrchdError(
                ErrorCode.E007,
                "register_proposal_bad_type: 提案项须为任务对象",
                [{"register": str(proposal_file)}],
            )
        missing = [f for f in required if f not in prop]
        if missing:
            raise OrchdError(
                ErrorCode.E007,
                "register_proposal_missing_fields: 提案缺必填字段",
                [{
                    "register": str(proposal_file),
                    "missing": missing,
                }],
            )
        tid = prop.get("id")
        if tid in existing:
            raise OrchdError(
                ErrorCode.E007,
                f"register_proposal_dup_id: 任务 id 已存在: {tid}",
                [{
                    "register": str(proposal_file),
                    "id": tid,
                }],
            )
        master.raw.setdefault("tasks", []).append(prop)
        existing.add(tid)


@_cli_skeleton
def _cmd_amend(args, tasks, orchd_dir, master, store, agent_id) -> dict:
    """增量更新 snapshot，依据状态约束矩阵过滤变更。

    CLI 参数: args.master — master 文件路径（默认 .orchd/_master.json）。
    返回: 变更摘要字典（new_tasks / updated_tasks / unchanged_tasks / removed_tasks），
    成功后附加 commit 字段（best-effort 自动提交 master 与 IDEAS.md，不阻塞）；
    新增/变更任务若声明 verify_command，附加 verify_dry_run 字段（试跑结果仅提示、不阻断注册）。
    """
    import json as _json
    import subprocess

    from orchd.gitops import ensure_committed, get_current_branch, get_default_branch
    from orchd.ledger import Store, resolve_store_dir, resolve_workspace_root
    from orchd.onboard import _decode_subprocess_output
    from orchd.spec import load_master
    from orchd.split import (
        _git_head_sha,
        amend,
        is_text_only_spec_revision,
        validate_terminal_revision,
    )

    # amend-only-canonical（task-master-single-copy）：分支守卫前移——在 canonical
    # 化**之前**检查调用方 cwd 所在分支。此前守卫位于 split.amend 内且基于
    # orchd_dir.parent 判定，而本函数先 canonical 到主工作树再调 amend，守卫查的
    # 永远是 main 分支 → 生产路径下守卫是死代码（review AC3 实锤：任务分支 amend
    # exit=0/amended:true）。前移后 container 任务 worktree 内执行 amend：cwd 分支
    # task/xxx ≠ default → 直接 E007，不触碰主工作树副本。split.amend 内守卫保留
    # 为函数级防御（直调者兜底）。
    from orchd.worktree import resolve_canonical_project_root

    caller_root = Path.cwd()
    caller_branch = get_current_branch(caller_root)
    default_branch = get_default_branch(caller_root) or "main"
    if caller_branch is not None and caller_branch != default_branch:
        try:
            canonical_root = resolve_canonical_project_root(caller_root)
        except Exception:
            canonical_root = caller_root
        raise OrchdError(
            ErrorCode.E007,
            f"invalid_branch: amend 仅在 default（{default_branch}）分支执行（主工作树），"
            f"当前分支 {caller_branch} 拒绝注册（红线 7）",
            [{
                "branch":
                caller_branch,
                "default":
                default_branch,
                "main_worktree":
                str(canonical_root),
                "hint":
                (f"任务分支不再允许 amend；请在主工作树 {canonical_root} 上补充/注册 "
                 "files_to_edit 等声明后再执行。"
                 "定位方法：git worktree list 中带 (main) 标记的路径即主工作树；"
                 "或从任务 worktree 路径上溯到项目根下的 main/ 目录。"
                 f'可执行命令：cd "{canonical_root}"; python .orchd/__main__.py amend '
                 "--task <id> --files-to-edit <file>"),
            }],
        )

    # task-amend-register-channel：显式注册通道 —— --register <proposal.json> 恒以
    # canonical master 为写目标（忽略 --master，避免把注册写进临时/异仓 master）。
    register_path = getattr(args, "register", None)
    default_master = getattr(args, "master", None)
    if register_path is not None:
        canonical_root = resolve_canonical_project_root(Path.cwd())
        master_path = canonical_root / ".orchd" / "_master.json"
    elif default_master in (None, "", ".orchd/_master.json"):
        canonical_root = resolve_canonical_project_root(Path.cwd())
        master_path = canonical_root / ".orchd" / "_master.json"
    else:
        master_path = Path(default_master)
        # task-amend-master-path-derivation-fix：--master 外部路径 fail-fast。
        # 此前无条件 parent.parent 推导——仓库外路径（如 C:/Temp/master_batch1.json）
        # 会把 project_root 推成盘符根，dry-run 以错误 cwd 执行，引用仓库内文件的
        # verify_command 必然失败并误判 E028，错误归因到未修改的存量任务。现校验
        # --master 必须位于某仓库的 .orchd/ 下（文件存在 + 父目录名 .orchd + 再上一级
        # 为 git 仓库根），不满足即 fail-fast（E007）：不执行 dry-run、不写 snapshot。
        if not master_path.is_file():
            raise OrchdError(
                ErrorCode.E007,
                "amend_master_missing: --master 文件不存在",
                [{
                    "master_path": str(master_path),
                    "hint": ("请传入仓库内 canonical master 的路径，或先把新任务并入 "
                             "<repo>/.orchd/_master.json 再省略 --master "
                             "（默认解析 canonical master）触发 amend"),
                }],
            )
        if (master_path.parent.name != ".orchd"
                or not (master_path.parent.parent / ".git").is_dir()):
            raise OrchdError(
                ErrorCode.E007,
                "amend_master_outside: --master 必须位于某仓库的 .orchd/ 目录下",
                [{
                    "master_path": str(master_path),
                    "orchd_dir": str(master_path.parent),
                    "project_root": str(master_path.parent.parent),
                    "hint": ("把新任务并入 <repo>/.orchd/_master.json 后省略 --master "
                             "（默认解析 canonical master）触发；仓库外临时 master "
                             "无法推导合法 project_root，dry-run 会以错误 cwd 执行，"
                             "引用仓库内文件的 verify_command 必然失败"),
                }],
            )
    master = load_master(master_path)
    orchd_dir = master_path.parent
    store = Store(orchd_dir)
    project_root = orchd_dir.parent

    # task-concurrent-amend-lost-update：读时 HEAD 快照（乐观并发 CAS 基准）。
    # 后续 dry-run（数十秒）与提案应用均在锁外；split.amend 持锁后比对，不一致
    # 即 E007 stale_base 拒绝（未写任何内容，可直接重试）。非 git → None 跳过。
    read_head = _git_head_sha(project_root)

    # task-amend-register-channel：读提案并追加新任务（非法提案 fail-fast，
    # canonical 零修改——此处仅改内存 master，写盘在后续 amend() 校验通过后）。
    if register_path is not None:
        _apply_register_proposals(master, register_path)

    # task-terminal-spec-revision-channel：终态规格文本修订通道（--revise-terminal）。
    # --reason 非空硬校验前移到此处（任何写入 / dry-run 之前 fail-fast）；终态性校验
    # 留在 split.amend 内（那里有 status 权威来源），两处共用 validate_terminal_revision
    # 单一事实源。
    revise_terminal = getattr(args, "revise_terminal", None)
    if revise_terminal is not None:
        validate_terminal_revision(getattr(args, "reason", None))

    # task-amend-decl-patch-channel：声明域 CLI 补登（--task + --files-to-edit /
    # --exempt-files / --verify-command）。列表类追加为并集语义；verify_command 为覆写。
    # task-decl-withdraw-channel（本任务）：新增撤回语义 --remove-files-to-edit /
    # --remove-exempt-files（集合差）——此前 CLI 表达不了删除，幽灵声明/幽灵豁免
    # 永久常驻（amend 只增不删）。撤回与追加同路径视为语义冲突，fail-fast（E007）。
    patch_task = getattr(args, "task", None)
    patch_files = list(getattr(args, "files_to_edit", None) or [])
    patch_exempt = list(getattr(args, "exempt_files", None) or [])
    patch_remove_files = list(
        getattr(args, "remove_files_to_edit", None) or [])
    patch_remove_exempt = list(
        getattr(args, "remove_exempt_files", None) or [])
    patch_verify = getattr(args, "verify_command", None)
    patch_sources = list(getattr(args, "additional_sources", None) or [])
    # task-verify-timeout-amend-channel：任务级 verify 预算补丁（default None = 不改）。
    patch_timeout = getattr(args, "verify_timeout_seconds", None)
    if patch_task is not None:
        if not (patch_files or patch_exempt or patch_remove_files
                or patch_remove_exempt or patch_verify is not None
                or patch_sources or patch_timeout is not None):
            raise OrchdError(
                ErrorCode.E007,
                "amend --task 需至少携带一个补丁字段",
                [{
                    "task_id":
                    patch_task,
                    "hint":
                    "补登示例：--files-to-edit <file> / --exempt-files <file> / "
                    "--verify-command \"<cmd>\" / --verify-timeout-seconds <N> / "
                    "--additional-sources <ref>；撤回示例："
                    "--remove-files-to-edit <file> / --remove-exempt-files <file>"
                    "（追加为并集、撤回为集合差，只增不删语义已由撤回通道补齐）",
                }],
            )
        target = next(
            (t for t in master.tasks if t.get("id") == patch_task),
            None,
        )
        if target is None:
            raise OrchdError(
                ErrorCode.E005,
                f"task '{patch_task}' not found in master",
                [{
                    "task_id": patch_task,
                    "hint": f"任务 {patch_task} 在 _master.json 中不存在，检查 id 拼写或注册"
                }],
            )
        # 同一路径同时追加与撤回 → 语义不明（用户意图二义）→ fail-fast，不猜测
        _clash = sorted(set(patch_files) & set(patch_remove_files))
        _clash_ex = sorted(set(patch_exempt) & set(patch_remove_exempt))
        if _clash or _clash_ex:
            raise OrchdError(
                ErrorCode.E007,
                "amend_decl_conflict: 同一路径不能同时追加与撤回声明",
                [{
                    "task_id": patch_task,
                    "files_to_edit_clash": _clash,
                    "exempt_files_clash": _clash_ex,
                    "hint": "追加与撤回互斥：确认该路径应保留还是移除后，只带其中一个标志重试",
                }],
            )
        withdrawn: list[dict[str, str]] = []
        not_present: list[dict[str, str]] = []
        cur_files = set(target.get("files_to_edit", []))
        cur_exempt = set(target.get("exempt_files", []) or [])
        next_files = sorted((cur_files | set(patch_files)) -
                            set(patch_remove_files))
        next_exempt = sorted((cur_exempt | set(patch_exempt)) -
                             set(patch_remove_exempt))
        # schema 要求 files_to_edit 非空：撤回唯一声明会让任务无声明域 → fail-fast
        #（不落盘、不留半成品；整任务作废应走 force-status/retract 而非清空声明）
        if (patch_files or patch_remove_files) and not next_files:
            raise OrchdError(
                ErrorCode.E007,
                "amend_decl_empty: files_to_edit 撤回后为空（schema 要求非空）",
                [{
                    "task_id":
                    patch_task,
                    "removed":
                    sorted(set(patch_remove_files)),
                    "declared_before":
                    sorted(cur_files),
                    "hint":
                    "每个任务至少须保留一个 files_to_edit 声明；若整任务作废，"
                    "请用 force-status / retract 处置，而非清空声明域",
                }],
            )
        if patch_files or patch_remove_files:
            not_present += [{
                "field": "files_to_edit",
                "path": p
            } for p in sorted(set(patch_remove_files) - cur_files)]
            withdrawn += [{
                "field": "files_to_edit",
                "path": p
            } for p in sorted(set(patch_remove_files) & cur_files)]
            target["files_to_edit"] = next_files
        if patch_exempt or patch_remove_exempt:
            not_present += [{
                "field": "exempt_files",
                "path": p
            } for p in sorted(set(patch_remove_exempt) - cur_exempt)]
            withdrawn += [{
                "field": "exempt_files",
                "path": p
            } for p in sorted(set(patch_remove_exempt) & cur_exempt)]
            target["exempt_files"] = next_exempt
        # task-amend-additional-sources-field：additional_sources 补登（并集追加、
        # 只增不删，与 files_to_edit 补登一致）。写入前按 spec.validate_source
        # 同口径校验补丁引用（_validate_additional_sources_patch），非法引用
        # E025 报错且不写入。
        if patch_sources:
            _validate_additional_sources_patch(
                patch_task, patch_sources, project_root)
            cur_sources = set(target.get("additional_sources", []) or [])
            target["additional_sources"] = sorted(
                cur_sources | set(patch_sources))
        if patch_verify is not None:
            target["verify_command"] = patch_verify
        if patch_timeout is not None:
            # task-verify-timeout-amend-channel：任务级 verify 预算通道（默认 120s 不变）。
            # 与 --verify-command 同属 patch 语义（可同一次调用组合生效）；claimed /
            # done / in_review 状态均可 patch——`split._AMEND_ATTACHABLE_FIELDS`
            # 为单一事实源，该字段已在白名单内（前置校验在写盘之前完成）。
            target["verify_timeout_seconds"] = _validate_verify_timeout_patch(
                patch_task, patch_timeout)
        if withdrawn or not_present:
            # 撤回留痕（stderr，不受 ORCHD_QUIET 抑制）：撤回若影响仍在分支 diff 中
            # 的路径，会让 review 期 E010 声明完整性反向告警 → 显式提示而非静默
            overlap = _decl_withdraw_branch_overlap(
                project_root, patch_task, [w["path"] for w in withdrawn])
            _log_decl_withdraw(patch_task, withdrawn, not_present, overlap)
        # task-amend-patch-write-after-validate：此处仅改内存 master，不落盘。
        # 后续 dry-run 预计算与 amend() 均以内存对象为准（无任何回读文件），落盘
        # 推迟到 amend() 成功之后——失败路径（E007/E025/E028）不留脏 master，
        # 杜绝"失败 amend 污染声明域并致后续 amend 死锁"。

    # task-e028-dryrun-exit4-priority：dry-run 前置到写入/提交之前。
    # 原实现先 amend()（写 snapshot + amend 自动 commit）再 dry-run，E028 阻断时
    # snapshot/commit 已实际完成，错误文案「注册已阻断」与真实副作用不符（实测
    # ae2f9a2 已提交入库）。现改为：预计算将变更任务 → 先 dry-run 校验 → 阻断则
    # raise（此时未写入）；通过后再 amend() 写入 + 自动提交。
    from orchd.split import classify_dry_run_failure
    from orchd.spec import validate_quality, verify_command_dangerous_reasons
    from orchd.subproc import run_shell

    # 预计算将变更任务（与 split.amend 内部 existing_tasks 加载同源）：
    # store_root/mod-*/spec.json 的存量定义 vs master，值差异即视为将变更。
    store_root = Path(resolve_store_dir(orchd_dir))
    existing_tasks: dict[str, dict[str, Any]] = {}
    for spec_path in sorted(store_root.glob("mod-*/spec.json")):
        snapshot = _json.loads(spec_path.read_text(encoding="utf-8"))
        for t in snapshot.get("tasks", []):
            existing_tasks[t.get("id", "")] = t
    changed = [
        t.get("id", "") for t in master.tasks
        if t.get("id", "") not in existing_tasks
        or existing_tasks.get(t.get("id", "")) != t
    ]

    # task-terminal-spec-revision-channel：纯文本修订不改 verify_command → 重跑 dry-run
    # 无信息量；且目标任务的**存量** E024/E027（历史定义缺 --basetemp / 含不安全段）
    # 会把这次文本修订整体阻断，使通道对历史终态任务不可用。故把"仅文本修订"的目标
    # 任务同时剔出 dry-run 与存量告警阻断集合（只影响开关指向的那一个任务）。
    dry_run_skipped: list[str] = []
    if revise_terminal is not None:
        old_def = existing_tasks.get(revise_terminal)
        new_def = next(
            (t for t in master.tasks if t.get("id") == revise_terminal),
            None,
        )
        if (old_def is not None and new_def is not None
                and is_text_only_spec_revision(old_def, new_def)):
            dry_run_skipped.append(revise_terminal)
    dry_run_changed = [tid for tid in changed if tid not in dry_run_skipped]

    # dry-run 试跑将变更任务的 verify_command（与 done 相同 shell 执行、同 cwd、
    # 限时 30s；2026-08-08 升级：assertion_mismatch 类失败阻断注册（E028），
    # E024/E027（缺 basetemp / 不安全段）阻断注册；expected_pending 仅提示）
    task_map = {t.get("id", ""): t for t in master.tasks}
    dry_run_results: list[dict[str, Any]] = []
    blocking_errors: list[dict[str, Any]] = []
    for tid in dry_run_changed:
        verify_cmd = task_map.get(tid, {}).get("verify_command")
        if not verify_cmd:
            continue
        _dangerous = verify_command_dangerous_reasons(verify_cmd)
        if _dangerous:
            blocking_errors.append({
                "code":
                ErrorCode.E027.name,
                "task_id":
                tid,
                "verify_command":
                verify_cmd,
                "reasons":
                _dangerous,
                "message": ("verify_command 含 shell 注入风险，dry-run 拒绝执行（E027）"),
            })
            continue
        try:
            proc = run_shell(verify_cmd, str(project_root), 30)
            failure_class = None
            if proc.returncode != 0:
                # E028 误判修复（task-fix-e028-dryrun-created-file）：缺失路径若属于
                # 本任务 files_to_edit（即将创建）→ expected_pending 仅提示不阻断
                _to_be_created = set(
                    task_map.get(tid, {}).get("files_to_edit", []) or [])
                failure_class = classify_dry_run_failure(
                    verify_cmd,
                    proc.returncode,
                    _decode_subprocess_output(proc.stderr)[:500],
                    _decode_subprocess_output(proc.stdout)[:300],
                    _to_be_created,
                )
                if failure_class == "assertion_mismatch":
                    _msg28 = "dry-run 断言不匹配（assertion_mismatch）：verify_command 引用现有文件但断言失败/语法错误，注册已阻断（E028）"
                    _details28 = [{
                        "task_id":
                        tid,
                        "verify_command":
                        verify_cmd,
                        "exit_code":
                        proc.returncode,
                        "project_root":
                        str(project_root),
                        "cwd":
                        str(Path.cwd()),
                        "stderr":
                        _decode_subprocess_output(proc.stderr)[:500]
                    }]
                    _resp28 = structured_error("E028", _msg28, _details28,
                                               project_root)
                    _err28 = _resp28.get("error", {})
                    _guid28 = _resp28.get("guidance")
                    blocking_errors.append({
                        "code":
                        _err28.get("code", "E028"),
                        "task_id":
                        tid,
                        "verify_command":
                        verify_cmd,
                        "exit_code":
                        proc.returncode,
                        "stderr":
                        _decode_subprocess_output(proc.stderr)[:500],
                        "message":
                        _err28.get("message", _msg28),
                        "details":
                        _err28.get("details", _details28),
                        "guidance":
                        _guid28,
                        "severity":
                        _err28.get("severity", "error"),
                    })
            dry_run_results.append({
                "task_id":
                tid,
                "ok":
                proc.returncode == 0,
                "exit_code":
                proc.returncode,
                "failure_class":
                failure_class,
                "stderr":
                _decode_subprocess_output(proc.stderr)[:500],
                "hint": ("dry-run 仅提示不阻断注册：实现未完成时失败属预期（可忽略）；"
                         "若断言应匹配现有文件而失败，则 verify_command 定义可能有误，建议核对"),
            })
        except (subprocess.SubprocessError, OSError):
            # 运行环境异常：静默跳过，不影响 amend 主流程
            continue

    # E024/E027 阻断：amend 注册点对新增/变更任务的 verify_command 质量校验
    # （E024 缺 basetemp / E027 不安全段 → 阻断注册，不再等 done 期 E014）
    # AC4 grandfather：仅收集 changed 任务（或新注册任务）的 E024/E027，
    # 存量任务命中仅 warning 不阻断——避免存量 E027/E024 阻塞任何后续 amend。
    qerrors = validate_quality(master)
    changed_set = set(dry_run_changed)
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
                    "code":
                    qe.code.name,
                    "path":
                    qe.path,
                    "message":
                    f"task '{tid}': {qe.message}",
                })

    if blocking_errors:
        # dry-run 前置：此时 amend 尚未执行，snapshot/commit 均未写入，
        # 「阻断注册」与真实副作用一致（task-e028-dryrun-exit4-priority）。
        raise OrchdError(
            ErrorCode.E028 if any(
                e.get("code") == "E028"
                for e in blocking_errors) else ErrorCode.E027,
            "amend_blocked: verify_command 校验未通过（注册已阻断，未写入 snapshot）",
            blocking_errors,
        )

    # 校验通过后执行 amend（写入 snapshot + checkpoint；透传终态文本修订通道）。
    # task-concurrent-amend-lost-update：release_lock=False 使准入写锁穿越
    # master 写盘 + 自动提交（写+提交原子化），提交后在 finally 释放；amend 内
    # CAS（expected_head=读时 HEAD）已在持锁后校验，未漂移才走到这里。
    # amend() 内抛异常 → 锁由其内部 except 释放，此处 held_lock 保持 None。
    held_lock: dict[str, Any] | None = None
    try:
        result = amend(
            orchd_dir,
            master,
            store,
            revise_terminal=revise_terminal,
            reason=getattr(args, "reason", None),
            expected_head=read_head,
            release_lock=False,
        )
        if isinstance(result, dict):
            held_lock = result.pop("_intake_lock", None)

        # task-amend-patch-write-after-validate：--task 补丁落盘点（--register 路径见
        # 热修复 1b5cdef）。amend() 成功返回才写 master：此前任何失败（E007/E025/E028）
        # 均在写盘前抛出，canonical 零修改；与 1b5cdef 同格式（无尾换行）。
        # （锁仍持有中：写盘纳入原子区。）
        if patch_task is not None:
            master_path.write_text(
                _json.dumps(master.raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # task-amend-register-channel-hotfix（2026-09-21）：--register 路径内存追加后落盘。
        # split.amend() 只重写快照不写 master（写盘仅 --task 分支 577 行），缺此行则注册
        # 仅存于内存/快照、canonical master 丢失（新任务幽灵化；下次快照重生成连快照痕迹
        # 一并消失，响应却报 amended:true）。位置：blocking_errors/dry-run 阻断之后、
        # amend() 成功之后——失败路径不落盘；与 577 行同格式（无尾换行）。
        if register_path is not None:
            master_path.write_text(
                _json.dumps(master.raw, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # 成功后 best-effort 自动提交（锁内、不阻塞状态机，语义对齐 merged:false）
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
                [
                    str(master_path),
                    str(ws_root / "IDEAS.md"),
                    str(ws_root / "ROADMAP.md")
                ],
                f"chore(intake): orchd amend — {summary}",
            )
            result["commit"] = commit
            # intake-commit-enforcement（2026-08-14）：commit 降级可审计化（对齐
            # merge_warning 先例）——注册成功后 commit 未执行（非 no_changes）不再
            # 静默：写入 commit_warning 供 status --audit-intake 巡检。git 环境不可用
            # （not_a_git_repo / git_unavailable）保留 best-effort 降级（判据 3）；
            # git 可用但提交失败（commit_failed）同样告警——"注册成功但改动未入库"
            # 违背"强制提交"语义，须人工核对。
            if commit.get("performed") is False and commit.get(
                    "reason") != "no_changes":
                result["commit_warning"] = {
                    "reason":
                    commit.get("reason"),
                    "message": (f"amend 注册成功但 commit 未执行（{commit.get('reason')}）："
                                "摄入产物改动可能未入库"),
                    "hint": ("若为 git 环境异常，可运行 'orchd status --audit-intake' "
                             "巡检未提交摄入产物，或运行 'orchd intake' 手动提交"),
                }
    finally:
        # 写+提交原子区出口：无论提交成败释放准入写锁（amend 内异常路径已自释，
        # 此处 held_lock 为 None，不重复释放）。
        if held_lock is not None:
            try:
                from orchd.ledger import intake_lock_release as _release_intake_lock

                _release_intake_lock(held_lock)
            except Exception:
                pass

    if dry_run_results:
        result["verify_dry_run"] = dry_run_results
    if dry_run_skipped:
        # 透明化：本次跳过 dry-run 的任务（纯文本修订，verify_command 未变）
        result["verify_dry_run_skipped"] = dry_run_skipped

    # task-amend-register-channel：注册语义显式化——
    # ① --register：恒写 canonical，registered=true（一条龙注册）。
    # ② amend --master 指向非 canonical：仅写该 master 所在 orchd_dir，不落当前
    #    canonical → registered=false + 明确 hint（不再用 amended:true 暗示已注册）。
    if register_path is not None:
        result["registered"] = True
    else:
        # task-master-path-residual-convergence：经单一真源取 canonical master
        # （对 canonical orchd_dir 自身解析恒等，无裸拼；AST 门禁零新增）。
        from orchd.worktree import resolve_master_path_from_dir

        canonical_master = resolve_master_path_from_dir(
            resolve_canonical_project_root(project_root) / ".orchd")
        if master_path.resolve() != canonical_master.resolve():
            result["registered"] = False
            result["hint"] = (
                "amend --master 指向非 canonical master：本次仅写该 master 所在 orchd_dir，"
                "未写入当前 canonical。如需注册，请把任务并入 canonical .orchd/_master.json "
                "后重跑裸 amend，或用 `amend --register <proposal.json>` 显式注册。")
    return result


def _cmd_retract(args) -> dict:
    from orchd.cli import _load_tasks
    """撤回已提交的事件。

    CLI 参数: args.event（可选，事件 ID 精确撤回）或 args.task + args.event_type
    （可选，按任务+类型自动定位最近匹配事件）、args.reason（必需）。
    返回: 撤回事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import retract

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)

    event_id = getattr(args, "event", None)
    task_id = getattr(args, "task", None)
    event_type = getattr(args, "event_type", None)

    if not event_id and not (task_id and event_type):
        return {
            "error": "retract 需要 --event <事件ID> 或 --task <任务ID> --type <事件类型>"
        }

    return retract(
        store,
        agent_id=agent_id,
        target_event_id=event_id,
        reason=args.reason,
        project_root=orchd_dir.parent,
        task_id=task_id,
        event_type=event_type,
        disposition=getattr(args, "disposition", None) or "abandon",
    )


def _cmd_force_status(args) -> dict:
    from orchd.cli import _load_tasks
    from orchd.cli import _maybe_archive_ideas
    """强制设置任务状态（用于恢复僵死任务或手动干预）。

    CLI 参数: args.task（必需）、args.status（必需，目标状态）、
    args.reason（必需）、args.assignee（可选，指定认领人）、
    args.force（可选，逃生口二次确认——claimed→completed / cancelled→pending）、
    args.evidence_sha（可选，completed→pending 复活所需的 git 证据 commit SHA）、
    args.force_recycle（可选，终态回收时显式丢弃未合并的 task/<id> 分支——用
    git branch -D，提交不可恢复；缺省拒绝删除未合并分支并留痕 branch_delete_refused）。
    agent 身份由引擎自动按宿主注入的 ORCHD_SESSION_ID 派生（session-id-fingerprint），不再有 --agent。
    返回: 强制状态变更事件信息。
    """
    from orchd.ledger import Store
    from orchd.onboard import force_status

    _, orchd_dir, _ = _load_tasks()
    store = Store(orchd_dir)
    agent_id = _require_agent_id(orchd_dir)
    result = force_status(
        store,
        agent_id=agent_id,
        task_id=args.task,
        target_status=args.status,
        reason=args.reason,
        assignee=args.assignee,
        force=args.force,
        project_root=orchd_dir.parent,
        evidence_sha=args.evidence_sha,
        force_recycle=getattr(args, "force_recycle", False),
    )
    # 任务进入终态后自动触发 IDEAS 归档（best-effort，用户无感）
    if result.get("new_status") == "cancelled":
        result["ideas_archive"] = _maybe_archive_ideas(orchd_dir)
    return result


def _cmd_merge_ack(args) -> dict:
    from orchd.cli import _load_tasks
    """merge_warning 人工销账：登记 .orchd/merge-acks.json（task-merge-warning-ack）。

    CLI 参数: args.task（必需，已人工确认的 task_id）、args.reason（必需，确认原因）。
    与 resolve_sha 自动销账互补：人工路径兜底旧事件 / 无 sha 场景，登记后
    audit-merge 不再报 merge_warning_unresolved。
    返回: 登记结果 {acked, task_id, acked_at, reason}。
    """
    from orchd.report import merge_ack

    _, orchd_dir, _ = _load_tasks()
    return merge_ack(orchd_dir.parent, args.task, args.reason)


def register(sub) -> None:
    """注册 control 模块的子命令。"""
    # amend
    p = sub.add_parser("amend", help="增量更新 snapshot")
    p.add_argument("--master", default=".orchd/_master.json")
    p.add_argument("--register",
                   default=None,
                   metavar="PROPOSAL",
                   help=("显式注册通道：读提案 JSON（单任务对象或任务数组），经 canonical 全套校验"
                         "（结构/E025/质量/dry-run）后追加并一条龙注册；恒以 canonical master 为写目标"))
    p.add_argument("--task", default=None, help="声明域补登：目标任务 id（需至少再带一个补丁字段）")
    p.add_argument("--files-to-edit",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="补登 files_to_edit（并集追加，只增不删；支持重复标志累加）")
    p.add_argument("--exempt-files",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="补登 exempt_files（并集追加，只增不删；支持重复标志累加）")
    p.add_argument("--remove-files-to-edit",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="remove_files_to_edit",
                   help="撤回 files_to_edit 声明（集合差；撤回不存在的路径为幂等 no-op 并留痕）")
    p.add_argument("--remove-exempt-files",
                   nargs="*",
                   action="extend",
                   default=None,
                   dest="remove_exempt_files",
                   help="撤回 exempt_files 声明（集合差；同上）")
    p.add_argument("--verify-command", default=None, help="覆写 verify_command")
    p.add_argument(
        "--verify-timeout-seconds",
        default=None,
        dest="verify_timeout_seconds",
        help="覆写任务级 verify 预算（秒，正整数 1..600；缺省 120s 不变）。仅当"
        "verify_command 含 hook 密集用例、在引擎 Git Bash 通道下耗时被放大而必然"
        "打爆默认预算时使用；须在任务 notes/交付说明写明实测理由与数据")
    p.add_argument("--additional-sources",
                   nargs="*",
                   action="extend",
                   default=None,
                   help="补登 additional_sources（并集追加，只增不删；支持重复标志累加；"
                   "逐条按与 source 相同口径校验：格式合法 + idea 条目存在且 pending）")
    p.add_argument("--revise-terminal",
                   default=None,
                   dest="revise_terminal",
                   help="终态任务规格文本修订：目标 task_id（须为 completed/cancelled，"
                   "需配 --reason；仅放行 acceptance_criteria / brief / name / "
                   "deliverables，写 AMEND 审计事件并同步快照）")
    p.add_argument("--reason",
                   default=None,
                   help="修订理由（--revise-terminal 必填、非空；写入 AMEND 审计事件）")
    p.set_defaults(func=_cmd_amend)

    # retract
    p = sub.add_parser("retract", help="撤回事件")
    p.add_argument("--event",
                   required=False,
                   default=None,
                   help="事件 ID（精确撤回）；与 --task + --type 二选一")
    p.add_argument("--task",
                   required=False,
                   default=None,
                   help="任务 ID（配合 --type 自动定位最近匹配事件）")
    p.add_argument("--type",
                   required=False,
                   default=None,
                   dest="event_type",
                   choices=[
                       "CLAIMED", "DONE", "REVIEW_CLAIMED", "REVIEW_SUBMITTED",
                       "REVIEW_READY", "AMEND", "MERGE_WARNING"
                   ],
                   help="事件类型（配合 --task 自动定位最近匹配事件）")
    p.add_argument("--reason", required=True)
    p.add_argument("--disposition", required=False, default=None,
                   choices=["abandon", "retry", "handoff"],
                   help="撤认处置：abandon（默认，触发300s认领冷却）/ retry（临时撤回后重试，"
                        "免冷却，AC 修正环用它）/ handoff（移交他人，免冷却）")
    p.set_defaults(func=_cmd_retract)

    # force-status
    p = sub.add_parser("force-status", help="强制设置任务状态")
    p.add_argument("--task", required=True)
    p.add_argument("--status", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--assignee")
    p.add_argument("--force",
                   action="store_true",
                   help="显式确认走逃生口（claimed→completed / cancelled→pending）")
    p.add_argument("--force-recycle",
                   action="store_true",
                   help="终态回收时显式丢弃未合并的 task/<id> 分支（git branch -D，"
                   "提交不可恢复）；缺省拒绝删除未合并分支并留痕 branch_delete_refused")
    p.add_argument("--evidence-sha",
                   default=None,
                   help="复活已完成任务的 git 证据 commit SHA（仅 completed→pending 时需要）")
    p.set_defaults(func=_cmd_force_status)

    # merge-ack（task-merge-warning-ack）
    p = sub.add_parser("merge-ack", help="merge_warning 人工销账（merge-acks 确认清单）")
    p.add_argument("--task", required=True, help="已人工确认的 task_id")
    p.add_argument("--reason", required=True, help="确认原因（必填）")
    p.set_defaults(func=_cmd_merge_ack)
