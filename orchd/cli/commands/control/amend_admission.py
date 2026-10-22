"""amend 准入段（_amend_admission），依赖 proposals。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from .proposals import _apply_register_proposals


def _amend_admission(
    args, orchd_dir: Path, master, store,
) -> tuple[Any, Any, Path, Path, Any, Path, str | None, str | None]:
    """amend 准入段：分支守卫 / canonical 目标解析 / HEAD 快照 / 修订通道前置
    （原 _amend_impl 体 L442-541）。

    红线 7 分支守卫（E007 invalid_branch）前移到 canonical 化之前；--register 恒以
    canonical master 为写目标（忽略 --master）；--master 外部路径 fail-fast
    （amend_master_missing / amend_master_outside）；读时 HEAD 快照供 split.amend
    持锁后 CAS（expected_head）；--revise-terminal 的 --reason 前置校验（终态性校验
    仍在 split.amend 内）。返回本段绑定/重绑定的 8 名供后续段与编排器使用。
    """
    from orchd.gitops import get_current_branch, get_default_branch
    from orchd.ledger import Store
    from orchd.spec import load_master
    from orchd.split import _git_head_sha, validate_terminal_revision
    from orchd.worktree import resolve_canonical_project_root

    # amend-only-canonical（task-master-single-copy）：分支守卫前移——在 canonical
    # 化**之前**检查调用方 cwd 所在分支。此前守卫位于 split.amend 内且基于
    # orchd_dir.parent 判定，而本函数先 canonical 到主工作树再调 amend，守卫查的
    # 永远是 main 分支 → 生产路径下守卫是死代码（review AC3 实锤：任务分支 amend
    # exit=0/amended:true）。前移后 container 任务 worktree 内执行 amend：cwd 分支
    # task/xxx ≠ default → 直接 E007，不触碰主工作树副本。split.amend 内守卫保留
    # 为函数级防御（直调者兜底）。
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
                "reason": "invalid_branch",
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
                 "container 布局可从任务 worktree 路径上溯到项目根下的 main/ 目录，"
                 "flat 布局主工作树即仓库根本身。"
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
        master_path = Path(str(default_master))
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
                    "reason": "amend_master_missing",
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
                    "reason": "amend_master_outside",
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

    return (
        register_path,
        master,
        master_path,
        orchd_dir,
        store,
        project_root,
        read_head,
        revise_terminal,
    )
