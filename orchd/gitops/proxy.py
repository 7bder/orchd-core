"""orchd/gitops/proxy.py — git 写操作代理（红线 #1/#2 引擎化拦截）。

**定位（ROADMAP §1.4.6 D1a/D1b）**：红线 #1「禁手动 git 写操作（checkout / branch /
reset / stash / merge / push 等，豁免 = **任务分支 git commit** + **任务分支
``merge main`` 精确形态**）」与红线 #2
「禁破坏性 git 操作」此前只写在 ``.orchd/rules/git.md``——**靠 agent 记得**。本模块把
它变成引擎命令 ``orchd git <args>``：命中判定由代码承担，人工纪律退化为「照命令走」。

**放行 / 拒绝矩阵**（fail-closed：未登记子命令一律拒绝）：

+----------------------+--------------------------------------------------------+
| 类别                 | 处置                                                   |
+======================+========================================================+
| 只读子命令           | 透传执行（``status`` / ``log`` / ``diff`` / ``rev-parse`` |
|                      | / ``ls-files`` …；含 ``branch``/``tag``/``stash``/       |
|                      | ``config`` 的只读形态）                                 |
+----------------------+--------------------------------------------------------+
| ``commit``           | **红线 #1 豁免一**：仅 ``task/*`` 分支放行（否则       |
|                      | E018 wrong_branch）；提交范围由 E020 pre-commit hook     |
|                      | 把守（staged ⊆ files_to_edit ∪ exempt_files）           |
+----------------------+--------------------------------------------------------+
| ``merge main``       | **红线 #1 豁免二（task-proxy-merge-allowlist）**：仅    |
| 精确形态             | ``task/*`` 分支上的 ``merge main`` /                    |
|                      | ``merge --no-edit main`` 放行（否则 E018；其余 merge    |
|                      | 形态仍拒绝）。任务分支合入 main 方向是安全的（不碰主分支；
|                      | 冲突由 git 正常报告，解决后提交）。                     |
+----------------------+--------------------------------------------------------+
| 其余写操作           | 结构化拒绝（E007），按族给出引擎替代通道 + hint          |
+----------------------+--------------------------------------------------------+

**无 git 降级（与 A0 一致）**：git 不可用时代理不抛错——``commit`` **推进 committed
快照**（与 A0b ``done`` 同口径的「本地快照」），其余写操作 **no-op**（单目录没有对应
git 动作），只读同样 no-op 并说明 git 不可用。

**语义边界（刻意不动既有路径）**：本模块**只新增入口**，不改 ``ensure_committed`` /
``try_git_merge`` / ``hook_*`` 任何一处——故：

- E020 hook 删除(D) / 重命名(R) 口径（task-decl-hook-delete-parity 既定）不受影响；
- 红线 #3 提交层变更检测口径（``git diff --name-only`` 含 D/R、快照侧超集口径）不受
  影响：代理不参与变更检测，不做任何 diff 计算；
- 代理**不安装 / 不移除 / 不改写** pre-commit hook，也不触碰 worktree 生命周期
  （红线 #④：worktree 由引擎管理，agent 零操作）。

**错误码映射**（复用既有码，不新增 schema）：写操作拒绝 → ``E007 invalid_state``
（与「amend 在任务分支被拒」「intake 非 main」同为「协议通道错误」语义）；
commit / 受管 merge 落在非任务分支 → ``E018 wrong_branch``（分支守卫语义，与
L1 分支守卫同码）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops._const import _GIT_ENCODING, _GIT_ERRORS, _GIT_TIMEOUT
from orchd.gitops.query import check_workspace_state

# ------------------------------------------------------------------
# 分类表
# ------------------------------------------------------------------

# 纯只读子命令：透传执行
READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    "status",
    "log",
    "diff",
    "show",
    "show-ref",
    "rev-parse",
    "rev-list",
    "ls-files",
    "ls-tree",
    "cat-file",
    "describe",
    "blame",
    "shortlog",
    "grep",
    "merge-base",
    "diff-tree",
    "diff-index",
    "diff-files",
    "name-rev",
    "for-each-ref",
    "whatchanged",
    "check-ignore",
    "check-attr",
    "count-objects",
    "verify-commit",
    "verify-tag",
    "help",
    "version",
})

# 双形态子命令 → 只读形态的旗标/动词白名单（未命中即视为写操作，fail-closed）
CONDITIONAL_READ_FLAGS: dict[str, frozenset[str]] = {
    "branch": frozenset({
        "--list", "-l", "-a", "-r", "-v", "-vv", "--show-current", "--contains",
        "--merged", "--no-merged", "--points-at", "--format", "--sort", "--column",
        "--all", "--remotes",
    }),
    "tag": frozenset({
        "--list", "-l", "-n", "--contains", "--points-at", "--sort", "--format",
        "--verify", "--merged", "--no-merged",
    }),
    "config": frozenset({
        "--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin",
    }),
    "remote": frozenset({"-v", "--verbose", "show", "get-url"}),
    "stash": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "notes": frozenset({"list", "show"}),
    "submodule": frozenset({"status", "summary"}),
    "reflog": frozenset({"show"}),
}

# 无参即只读列举的子命令（``git branch`` / ``git tag`` / ``git reflog``）
_BARE_READ_SUBCOMMANDS: frozenset[str] = frozenset({"branch", "tag", "reflog"})

# 写操作族 → (红线, 引擎替代通道 hint)
_WRITE_FAMILIES: dict[str, tuple[str, str]] = {
    "merge": (
        "red_line_git_write",
        "merge 由引擎在 code APPROVED 时于主工作树执行（review 流程）；"
        "任务分支合入 main 方向可用受管通道：orchd git merge main（精确形态，"
        "仅 task/* 分支放行）；若需检查与 main 的冲突可合并性，另可走 "
        "orchd done（前置 reconcile；reconcile 只判定合并冲突，不管声明同步"
        "——声明权威按布局收敛，flat 下读 main blob）",
    ),
    "branch": (
        "red_line_git_write",
        "分支 / 工作树 / 暂存全生命周期由引擎管理（red line #④，agent 零操作）："
        "建分支用 orchd claim、切回用 orchd done / review；"
        "不要在代理里手动 checkout / switch / branch / stash",
    ),
    "history": (
        "red_line_destructive",
        "破坏性 / 历史改写操作（red line #②）禁止：reset / rebase / cherry-pick / revert / "
        "am / apply / clean / rm / mv / update-ref / gc / prune 均不在 agent 职责内；"
        "需要丢弃或重做请用 orchd retract / force-status 并重新 claim",
    ),
    "index": (
        "red_line_git_write",
        "索引态由提交与 hook 承担：实现过程中可直接 orchd git commit（任务分支唯一豁免），"
        "范围由 E020 pre-commit hook 校验（staged ⊆ files_to_edit ∪ exempt_files）",
    ),
    "network": (
        "red_line_git_write",
        "远端写 / 网络操作禁止（red line #①：不 push；远端推送由项目管理员负责）；"
        "账本跨设备共享走 orchd sync",
    ),
    "other": (
        "red_line_git_write",
        "该 git 写操作未在协议白名单内：请改用引擎命令（claim / done / review / amend / "
        "intake / sync / retract），或仅在任务分支执行 orchd git commit",
    ),
}

# 写操作子命令 → 族
_WRITE_SUBCOMMANDS: dict[str, str] = {
    # 分支 / 工作树 / 暂存（red line #④）
    "checkout": "branch",
    "switch": "branch",
    "branch": "branch",
    "restore": "branch",
    "stash": "branch",
    "worktree": "branch",
    "sparse-checkout": "branch",
    "submodule": "branch",
    "lfs": "branch",
    "bisect": "branch",
    # 合并（引擎专属通道）
    "merge": "merge",
    # 破坏性 / 历史改写（red line #②）
    "reset": "history",
    "rebase": "history",
    "revert": "history",
    "cherry-pick": "history",
    "am": "history",
    "apply": "history",
    "filter-branch": "history",
    "filter-repo": "history",
    "replace": "history",
    "update-ref": "history",
    "gc": "history",
    "prune": "history",
    "clean": "history",
    "rm": "history",
    "mv": "history",
    "maintenance": "history",
    "repack": "history",
    # 索引 / 提交辅助
    "add": "index",
    "update-index": "index",
    "write-tree": "index",
    "commit-tree": "index",
    "notes": "index",
    "commit": "index",
    # 远端 / 网络
    "push": "network",
    "fetch": "network",
    "pull": "network",
    "clone": "network",
    "init": "network",
    "remote": "network",
    "tag": "network",
    "send-pack": "network",
    "request-pull": "network",
    # 其他可写形态
    "symbolic-ref": "other",
    "config": "other",
    "reflog": "other",
    "archive": "other",
    "format-patch": "other",
    "instaweb": "other",
    "pack-refs": "other",
}


# ------------------------------------------------------------------
# 判定（纯函数，便于测试正负控制）
# ------------------------------------------------------------------


def _is_read_variant(subcommand: str, rest: list[str]) -> bool:
    """双形态子命令的**只读形态**判定（命中白名单旗标/动词，或无参列举）。"""
    allowed = CONDITIONAL_READ_FLAGS.get(subcommand)
    if allowed is None:
        return False
    if not rest:
        return subcommand in _BARE_READ_SUBCOMMANDS
    return any(arg in allowed for arg in rest)


# 受管 merge 出口的精确形态（task-proxy-merge-allowlist）：仅 task/* 分支上的
# ``merge main`` 与 ``merge --no-edit main``。其余 merge 形态（换目标分支、加旗标、
# 无参等）一律仍走 write 拒绝（fail-closed）。精确形态刻意收窄：合入 main 方向是
# 任务分支同步的唯一合法手动形态，其余合并语义一律走引擎通道。
_MERGE_PASSTHROUGH_RESTS: tuple[tuple[str, ...], ...] = (
    ("main",),
    ("--no-edit", "main"),
)


def _is_merge_passthrough(rest: list[str]) -> bool:
    """是否为受管 merge 出口精确形态（与当前分支无关，分支在执行层判定）。"""
    return tuple(rest) in _MERGE_PASSTHROUGH_RESTS


def classify_git_argv(argv: list[str] | None) -> dict[str, Any]:
    """对 ``orchd git <args>`` 的 args 做放行/拒绝分类（纯函数，不执行 git）。

    Returns:
        ``{"subcommand", "rest", "kind", "family", "reason"}``：

        - ``kind="read"``   只读，透传执行；
        - ``kind="commit"`` 红线 #1 豁免一（仅任务分支）；
        - ``kind="merge"``  红线 #1 豁免二（仅任务分支上的精确形态）；
        - ``kind="write"``  写操作，拒绝（``family`` 给出族与替代通道）；
        - ``kind="unknown"`` 未登记子命令（fail-closed，同拒绝处置）。
    """
    args = [str(a) for a in (argv or [])]
    # 允许显式分隔符：``orchd git -- status``
    if args and args[0] == "--":
        args = args[1:]
    subcommand = args[0] if args else None
    rest = args[1:]
    if subcommand is None:
        return {
            "subcommand": None,
            "rest": [],
            "kind": "unknown",
            "family": "other",
            "reason": "缺少 git 子命令（用法：orchd git <args>，如 orchd git status）",
        }
    if subcommand == "commit":
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "commit",
            "family": "commit",
            "reason": "任务分支 git commit 是红线 #1 豁免一（提交范围由 E020 hook 校验）",
        }
    if subcommand == "merge" and _is_merge_passthrough(rest):
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "merge",
            "family": "merge",
            "reason": "任务分支 merge main 精确形态是红线 #1 豁免二（受管出口）",
        }
    if subcommand in READ_ONLY_SUBCOMMANDS:
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "read",
            "family": "read",
            "reason": "只读子命令",
        }
    if _is_read_variant(subcommand, rest):
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "read",
            "family": "read",
            "reason": f"{subcommand} 的只读形态（列举 / 查询）",
        }
    family = _WRITE_SUBCOMMANDS.get(subcommand)
    if family is None:
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "unknown",
            "family": "other",
            "reason": (
                f"未登记子命令 '{subcommand}'：代理 fail-closed，未知命令一律拒绝"
                "（如确为只读用途，请补登记 READ_ONLY_SUBCOMMANDS）"
            ),
        }
    return {
        "subcommand": subcommand,
        "rest": rest,
        "kind": "write",
        "family": family,
        "reason": f"命中写操作族 '{family}'（红线 #1/#2 拦截）",
    }


# ------------------------------------------------------------------
# 执行
# ------------------------------------------------------------------


def _git_available(project_root: Path) -> bool:
    """git 是否可用（复用 ``check_workspace_state`` 三态单一真源）。"""
    try:
        return bool(check_workspace_state(Path(project_root)).get("available"))
    except Exception:
        return False


def _execute_git(project_root: Path, args: list[str], cls: dict[str, Any]) -> dict[str, Any]:
    """透传执行 git 命令（返回结构化结果，不抛非零退出）。"""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrchdError(
            ErrorCode.E007,
            f"git_proxy_failed: 代理执行 git 失败：{exc}",
            [{
                "git_args": args,
                "subcommand": cls.get("subcommand"),
                "hint": "请重试；持续失败请检查 git 可执行文件与仓库状态",
            }],
        ) from exc
    return {
        "proxied": True,
        "executed": True,
        "kind": cls["kind"],
        "subcommand": cls.get("subcommand"),
        "git_args": args,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _bound_tasks_for_root(project_root: Path, orchd_dir: Path | None) -> list[str]:
    """绑定到该目录的任务 id 清单（best-effort；无绑定 / 读取失败 → 空清单）。

    读取共享账本根的 ``session-worktrees.json``（:func:`orchd.worktree.bindings_path`
    为路径单一来源），反查 ``worktree`` 等于本目录的任务——无 git 单目录模式下
    claim 把任务绑定到**项目主目录**（task-nogit-single-dir-pivot）。
    """
    try:
        from orchd.ledger import resolve_store_dir
        from orchd.worktree import bindings_path

        root = resolve_store_dir(orchd_dir or (Path(project_root) / ".orchd"))
        path = bindings_path(root)
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        target = Path(project_root).resolve()
        hits: list[str] = []
        for task_id, entry in data.items():
            wt = entry.get("worktree") if isinstance(entry, dict) else None
            if not wt:
                continue
            try:
                if Path(wt).resolve() == target:
                    hits.append(str(task_id))
            except OSError:
                continue
        return sorted(hits)
    except Exception:
        return []


def _nogit_payload(
    args: list[str],
    cls: dict[str, Any],
    project_root: Path,
    orchd_dir: Path | None,
) -> dict[str, Any]:
    """无 git 单目录模式的降级载荷（commit → 本地快照；其余 → no-op）。"""
    payload: dict[str, Any] = {
        "proxied": True,
        "executed": False,
        "nogit": True,
        "kind": cls["kind"],
        "subcommand": cls.get("subcommand"),
        "git_args": args,
    }
    if cls["kind"] == "commit":
        bound = _bound_tasks_for_root(project_root, orchd_dir)
        if len(bound) == 1:
            from orchd.nogit import take_snapshot

            info = take_snapshot(Path(project_root), bound[0], "committed")
            payload.update({
                "noop": False,
                "task_id": bound[0],
                "snapshot": info,
                "reason": "nogit_committed_snapshot",
                "message": "nogit: committed snapshot advanced",
                "hint": (
                    "无 git 单目录模式没有 git 提交：「提交」= 推进 committed 快照"
                    "（与 done 同口径，A0b）；残留检测据此判定"
                ),
            })
            return payload
        payload.update({
            "noop": True,
            "bound_tasks": bound,
            "reason": "nogit_no_binding",
            "hint": (
                "未唯一解析出绑定到本目录的任务（无 git 单目录模式下 claim 会把任务绑定到"
                "项目主目录）；基线/提交快照由 claim 建立、done 推进，无需手动提交"
            ),
        })
        return payload
    payload.update({
        "noop": True,
        "reason": "nogit_single_dir",
        "hint": (
            "无 git 单目录模式没有对应 git 动作（与 A0 一致）：工作直接在项目主目录进行，"
            "变更检测用快照口径、提交快照由引擎在 done 推进；本命令为 no-op"
        ),
    })
    return payload


def _proxy_commit(project_root: Path, args: list[str], cls: dict[str, Any]) -> dict[str, Any]:
    """任务分支 commit 放行（红线 #1 豁免一）；非任务分支 → E018。"""
    state = check_workspace_state(project_root)
    branch = state.get("branch")
    if not branch or not str(branch).startswith("task/"):
        raise OrchdError(
            ErrorCode.E018,
            f"wrong_branch: git commit 仅在任务分支允许（红线 #1 豁免一），当前在 '{branch}'",
            [{
                "git_args": args,
                "current_branch": branch,
                "rule": "commit_requires_task_branch",
                "hint": (
                    "主分支提交由引擎承担：实现内容由 orchd done 自动兜底提交，"
                    "摄入产物（_master.json / IDEAS.md / ROADMAP.md）由 orchd intake / amend 提交；"
                    "任务分支内的细粒度提交请先 orchd claim 获得 task/* 分支"
                ),
            }],
        )
    return _execute_git(project_root, args, cls)


def _proxy_merge(project_root: Path, args: list[str], cls: dict[str, Any]) -> dict[str, Any]:
    """任务分支 merge main 精确形态放行（红线 #1 豁免二，task-proxy-merge-allowlist）。

    仅 ``task/*`` 分支放行（否则 E018，与 commit 豁免同码同语义）；合入 main 方向
    不碰主分支，冲突由 git 正常报告、解决后提交。非精确形态在分类层已拒绝，
    到此的必为 ``merge main`` / ``merge --no-edit main``。
    """
    state = check_workspace_state(project_root)
    branch = state.get("branch")
    if not branch or not str(branch).startswith("task/"):
        raise OrchdError(
            ErrorCode.E018,
            f"wrong_branch: git merge main 仅在任务分支允许（红线 #1 豁免二），当前在 '{branch}'",
            [{
                "git_args": args,
                "current_branch": branch,
                "rule": "merge_requires_task_branch",
                "hint": (
                    "主分支的合并由引擎承担（code APPROVED 自动 merge）；"
                    "任务分支同步 main 请先 orchd claim 获得 task/* 分支，"
                    "再执行 orchd git merge main"
                ),
            }],
        )
    return _execute_git(project_root, args, cls)


def run_git_proxy(
    argv: list[str] | None,
    *,
    project_root: Path | None,
    orchd_dir: Path | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """``orchd git <args>`` 代理入口：只读透传 / 任务分支 commit 与 merge main 放行 /
    其余结构化拒绝。

    Args:
        argv: git 参数（如 ``["status"]`` / ``["commit", "-m", "msg"]``）。
        project_root: 项目根（主工作树）。
        orchd_dir: ``.orchd`` 目录（用于解析共享账本根；缺省由 project_root 推导）。
        agent_id: 调用者身份（仅用于拒绝详情与审计，不参与判定）。

    Returns:
        结构化载荷（``proxied`` / ``executed`` / ``kind`` / ``returncode`` 等）。

    Raises:
        OrchdError: 写操作被拒（E007）/ commit 不在任务分支（E018）/ git 执行故障（E007）。
    """
    args = [str(a) for a in (argv or [])]
    cls = classify_git_argv(args)
    if project_root is None:
        raise OrchdError(
            ErrorCode.E007,
            "git_proxy_no_project: 无法定位项目根，代理拒绝执行",
            [{
                "git_args": args,
                "hint": "请在项目目录内执行（引擎按 cwd 定位 .orchd）；容器布局请进入主工作树",
            }],
        )
    root = Path(project_root)

    if not _git_available(root):
        # 无 git 降级（A0）：commit → 本地快照，其余 → no-op（不抛错）
        return _nogit_payload(args, cls, root, orchd_dir)

    if cls["kind"] == "read":
        return _execute_git(root, args, cls)
    if cls["kind"] == "commit":
        return _proxy_commit(root, args, cls)
    if cls["kind"] == "merge":
        return _proxy_merge(root, args, cls)

    rule, hint = _WRITE_FAMILIES.get(cls["family"], _WRITE_FAMILIES["other"])
    raise OrchdError(
        ErrorCode.E007,
        f"manual_git_write_forbidden: '{cls.get('subcommand')}' 属红线 #1/#2 禁止的手动 git 写操作",
        [{
            "git_args": args,
            "subcommand": cls.get("subcommand"),
            "family": cls["family"],
            "rule": rule,
            "agent_id": agent_id,
            "reason": cls.get("reason"),
            "hint": hint,
            "allowed": (
                "只读子命令透传（orchd git status / log / diff …）；"
                "任务分支内细粒度提交放行（orchd git commit -m '…'）；"
                "任务分支同步 main 受管放行（orchd git merge main 精确形态）"
            ),
        }],
    )
