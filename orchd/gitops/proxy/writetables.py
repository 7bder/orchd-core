"""orchd gitops proxy — 写通道数据表域（写 token / 家族 / 子命令表），叶子域（W6-C4 拆分，纯搬移）。

来源：``orchd/gitops/proxy.py``（1164 行，W6-C4 纯搬移）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.gitops._const import _GIT_ENCODING, _GIT_ERRORS, _GIT_TIMEOUT
from orchd.gitops.query import check_workspace_state
from orchd.line_ctx import resolve_trunk_for


# 已知写动词/写旗标（task-proxy-read-strict，N4）：出现在双形态子命令的
# rest 中即判写操作——位置参数不能假设中性（``branch main`` 实为创建分支）。
# 命名与白名单动词（list/show）互斥；分支名恰为此表的极端情况按 fail-closed
# 拒绝（agent 改用 --list 形态仍被拒？否——纯 ``--list`` 无位置参数即放行）。
_WRITE_TOKENS: frozenset[str] = frozenset({
    "-d", "-D", "-m", "-M", "-c", "-C", "-f", "--force", "--delete",
    "--edit-description", "--unset-upstream", "--set-upstream-to", "--move",
    "--copy", "--rename", "add", "rm", "remove", "delete", "push", "pop",
    "apply", "drop", "clear", "store", "create", "set", "unset", "rename",
    "record", "prune",
})


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
    "commit": (
        "red_line_commit_form",
        "commit 豁免一只放行取值型旗标：-m/--message、-F/--file、--author、--date、"
        "--cleanup、--trailer（形态 orchd git commit -m '…'）。--no-verify 跳过 E020 hook、"
        "-a/--all 批量入索引、--amend/--fixup/--squash 改写历史、pathspec 直提均属绕过形态，"
        "一律拒绝：需要扩大范围请 amend --files-to-edit 重新声明，需要重做请 orchd retract",
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
