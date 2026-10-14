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
|                      | ``config`` 的只读形态）。**例外（洞②）**：携带「可启动外部 |
|                      | 进程 / 可写盘 / 可改索引」旗标（``--ext-diff``、           |
|                      | ``--textconv``、``--ext-grep``、``--open-files-in-pager``、|
|                      | ``--output``、``-O<val>``、``grep -p``、``--refresh``…）   |
|                      | 按写操作拒绝                                             |
+----------------------+--------------------------------------------------------+
| ``commit``           | **红线 #1 豁免一**：仅 ``task/*`` 分支放行（否则 E018）， |
|                      | 且旗标形态收窄为**取值型白名单**（``-m/--message``、      |
|                      | ``-F/--file``、``--author``、``--date``、``--cleanup``、   |
|                      | ``--trailer``；洞①）：``--no-verify`` / ``-a`` /           |
|                      | ``--amend`` / pathspec 等绕过形态按写操作拒绝；             |
|                      | 提交范围由 E020 pre-commit hook 把守（staged ⊆             |
|                      | files_to_edit ∪ exempt_files）                              |
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
git 动作），只读同样 no-op 并说明 git 不可用。**唯一例外**：形态不合法的 ``commit``
豁免（洞①命中白名单外旗标）两态都拒绝——收窄的是豁免范围，与 git 是否可用无关。

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
from orchd.line_ctx import resolve_trunk_for

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

# 取值旗标二分（task-git-proxy-enforcement-holes，洞③）：git parse-options 的
# PARSE_OPT_REQUIRED（必值）与 PARSE_OPT_OPTARG（可值）在「空格形态吃不吃后随参数」
# 上语义不同，代理必须区分，否则 ``branch --column task/x`` 这类「可值旗标 + 分支名」
# 会被误判只读而真建分支（scratch 仓库实测：rc=0 且 refs/heads/task/x 被创建）。
#
# 必值：``--flag <值>`` 与 ``--flag=<值>`` 都吃值，后随参数不是位置参数。
#   出处 git-branch(1)/git-tag(1)：--contains/
#   --points-at/--merged/--no-merged/--format/--sort（branch 形态值可省则按 HEAD 缺省，
#   但空格形态实测吃值：``branch --merged HEAD task/a1`` 零新建）；
#   git-config(1)：--get/--get-all/--get-regexp。
#   ``-n`` 在 git-tag(1) 手册是 ``-n[=<num>]`` 可值形态，此处按必值处理：实测
#   ``tag -n tagD`` 与 ``tag -n4 tagE`` 均零新建（-n 蕴含 --list，裸参数是
#   pattern 而非标签名），成对消费不会放行任何创建语义，且避免误伤 ``tag -n v1*``。
_REQUIRED_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "branch": frozenset({
        "--contains", "--points-at", "--merged", "--no-merged",
        "--format", "--sort",
    }),
    "tag": frozenset({
        "--contains", "--points-at", "--merged", "--no-merged",
        "--format", "--sort", "-n",
    }),
    "config": frozenset({"--get", "--get-all", "--get-regexp"}),
}

# 可值：仅 ``--flag=<值>`` 内联形态带值；裸形态**不吃**后随参数——裸名仍是位置参数
#   （创建语义）。出处 git-branch(1)：``--column[=<options>]``（实测 ``branch --column
#   task/x`` 建分支、``branch --column=always task/x`` 亦建分支、``branch --column``
#   裸跑为列举）。展示旗标 ``-v/-vv/-l/--list/-a/-r/--show-current`` 本就不取值，
#   后随裸名由 :func:`_is_read_variant` 的「展示旗标后跟裸名」分支拒。
_OPTIONAL_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "branch": frozenset({"--column"}),
    "tag": frozenset(),
    "config": frozenset(),
}

# 兼容别名：旧代码与本模块 docstring 以 _VALUE_FLAGS 指称「取值旗标」总集。
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    name: _REQUIRED_VALUE_FLAGS[name] | _OPTIONAL_VALUE_FLAGS.get(name, frozenset())
    for name in _REQUIRED_VALUE_FLAGS
}

# 无参即只读列举的子命令（``git branch`` / ``git tag`` / ``git reflog``）
_BARE_READ_SUBCOMMANDS: frozenset[str] = frozenset({"branch", "tag", "reflog"})

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


# ------------------------------------------------------------------
# 判定（纯函数，便于测试正负控制）
# ------------------------------------------------------------------


def _is_read_variant(subcommand: str, rest: list[str]) -> bool:
    """双形态子命令的**只读形态**判定（task-proxy-read-strict，N4；
    pass8 F1 收口位置参数变体）。

    fail-closed（任一不满足即写操作）：
    1. 所有 ``-`` 旗标必须命中只读白名单（未知旗标即拒绝）；
    2. 任何已知写动词/写旗标（``-d/-D/-m/add/push/--edit-description`` 等）
       出现即拒绝——位置参数不能假设中性；
    3. 位置参数必须被**必值**旗标成对消费（``--points-at HEAD``、
       ``--format=x`` 内联形态亦可；tag 的 ``-l/--list`` 后 pattern 除外）——
       展示旗标（``-v/-vv/-l/--list`` 等）后跟裸名（``branch -v mybranch``
       真建分支）一律拒绝；``--`` 之后一切皆位置参数。
    4. **可值**旗标（``--column[=<options>]`` 一类 PARSE_OPT_OPTARG）只认内联形态
       ``--column=always``；裸形态不吃后随参数，故 ``branch --column task/x``
       按第 3 条落为写操作（task-git-proxy-enforcement-holes 洞③）。

    旧 ``any()`` 任一命中即放行，``branch -v --edit-description`` 等混合形态
    逃逸为只读，违背自身 fail-closed 承诺。无参列举保持原语义。
    """
    allowed = CONDITIONAL_READ_FLAGS.get(subcommand)
    if allowed is None:
        return False
    if not rest:
        return subcommand in _BARE_READ_SUBCOMMANDS
    value_required = _REQUIRED_VALUE_FLAGS.get(subcommand, frozenset())
    value_optional = _OPTIONAL_VALUE_FLAGS.get(subcommand, frozenset())
    tag_pattern = subcommand == "tag"
    seen_list_flag = False
    has_flag = False
    after_dashdash = False
    i, n = 0, len(rest)
    while i < n:
        arg = rest[i]
        if after_dashdash:
            # ``--`` 之后一切皆位置参数 → 写操作
            return False
        if arg == "--":
            after_dashdash = True
            i += 1
            continue
        if arg.startswith("-"):
            has_flag = True
            name, eq, _val = arg.partition("=")
            if name not in allowed:
                return False
            if eq and name not in value_required and name not in value_optional:
                # ``--flag=val`` 内联形态仅取值旗标可消费
                return False
            if name in ("-l", "--list"):
                seen_list_flag = True
            if name in value_optional and not eq:
                # 可值旗标裸形态**不吃**后随参数：``branch --column task/x`` 的
                # task/x 仍是位置参数（实测建分支），由下方「展示旗标后跟裸名」拒。
                i += 1
                continue
            if name in value_required and not eq:
                # 成对消费后随位置参数；行末缺值时 git 以 HEAD 为缺省
                # （``branch --merged`` 即查已合入 HEAD），保持放行；
                # 被消费值不得以 `-` 开头（git 自身亦按旗标解析，防逃逸）。
                if i + 1 < n and rest[i + 1].startswith("-"):
                    return False
                i += 2
                continue
        elif arg in _WRITE_TOKENS:
            return False
        elif not has_flag and arg not in allowed:
            # 无旗标的裸位置参数：白名单动词（如 list）除外一律拒绝
            # （branch main / config user.name x 皆为写操作）。
            return False
        elif tag_pattern and seen_list_flag:
            # tag 的 list pattern（``tag -l v1*``）：展示查询，保持放行
            pass
        elif subcommand in ("branch", "tag", "config"):
            # 展示旗标后跟裸名（branch -v mybranch 真建分支）一律拒绝；
            # config 的取值须由 --get 系消费（上分支已处理），余者拒绝。
            return False
        i += 1
    return True


# 受管 merge 出口的精确形态（task-proxy-merge-allowlist）：仅任务分支上的
# ``merge <trunk>`` 与 ``merge --no-edit <trunk>``。其余 merge 形态（换目标分支、
# 加旗标、无参等）一律仍走 write 拒绝（fail-closed）。精确形态刻意收窄：合入
# 当前线 trunk 方向是任务分支同步的唯一合法手动形态，其余合并语义一律走引擎通道。
# task-line-guard-worktree：``<trunk>`` 按当前线解析（单线恒 main）。
def _merge_passthrough_forms(trunk: str) -> tuple[tuple[str, ...], ...]:
    """受管 merge 出口精确形态：``merge <trunk>`` / ``merge --no-edit <trunk>``。"""
    return ((trunk,), ("--no-edit", trunk))


def _is_merge_passthrough(rest: list[str], trunk: str = "main") -> bool:
    """是否为受管 merge 出口精确形态（与当前分支无关，分支在执行层判定）。"""
    return tuple(rest) in _merge_passthrough_forms(trunk)


# 红线 #1 豁免一的**旗标白名单**（task-git-proxy-enforcement-holes 洞①）：旧实现按
# 子命令名整棵放行 commit 树，于是 ``commit --no-verify``（跳过 E020 pre-commit hook
# ——范围校验的唯一守门人）、``commit -a``（把全部已跟踪改动批量入索引，绕过
# staged ⊆ files_to_edit 判定）、``commit --amend``（改写历史，红线 #②）、
# ``commit -m x -- <路径>``（pathspec 直提）都能顶着「豁免一」的名义通过分类
# （本机实测：四者分类均为 commit）。现只放行**取值型**旗标，其余旗标与位置参数
# 一律拒绝——绕过型 / 批量型 / 历史改写型不必逐个列黑名单（fail-closed）。
# 白名单逐条出处（git-commit(1)；均按**必值**收口。实测注：``--cleanup`` / ``--author``
# 裸用时 git 会把后随 argv 当值吞掉——
# ``git commit --cleanup -m x`` → ``fatal: Invalid cleanup mode -m``、
# ``git commit --author -m x`` → ``fatal: --author '-m' is not 'Name <email>'``，
# 故代理要求显式带值，不做「裸形态按缺省放行」的猜测。本机 git 2.55）：
# 键 = 旗标名，值 = 手册口径 + 取值形态。
_COMMIT_VALUE_FLAGS: dict[str, str] = {
    "-m": "git-commit(1) -m <msg>（= --message）：提交说明，必值；另支持粘连 -m<msg>",
    "--message": "git-commit(1) --message=<msg>：提交说明，必值",
    "-F": "git-commit(1) -F <file>（= --file）：从文件读提交说明，必值；另支持粘连 -F<path>",
    "--file": "git-commit(1) --file=<path>：从文件读提交说明，必值",
    "--author": "git-commit(1) --author=<Name <email>>：仅在有授权身份时使用，必值",
    "--date": "git-commit(1) --date=<ISO 8601>：作者时间戳，必值",
    "--cleanup": "git-commit(1) --cleanup=<strip|whitespace|verbatim|default>：说明清洗模式，必值",
    "--trailer": "git-commit(1) --trailer=<token:arg>：追加提交说明尾注，必值",
}

# 短旗标的粘连取值形态（``-m<text>`` / ``-F<path>``）：实测 ``git commit -mfoo``
# 即以 foo 为 message，属合法取值，不得因名字不在集合内误杀。
_COMMIT_SHORT_WITH_VALUE = ("-m", "-F")

# 正文来源旗标：git 据此取提交说明而**不启动编辑器**（第 1 轮 code review F-a）。
# 无正文形态（``rest == []``、只有 --author/--date 等）会让 git spawn ``core.editor``
# ——那是豁免一通道里的任意命令执行面。分档每轮由
# ``tests/test_git_proxy.py::TestDeniedFlagExecutionSurface::test_editor_spawn_forms`` 复现
# （夹具：scratch 仓库、``core.editor=touch MARKER_EDIT``、已有 staged 改动、
# GIT_EDITOR/EDITOR/VISUAL/GIT_SEQUENCE_EDITOR 从环境剔除）：
# ``commit -C HEAD``/``--reuse-message=HEAD``/``-am msg`` → rc=0 且 MARKER_EDIT **不**落盘；
# ``commit -c HEAD`` → rc=0 且 MARKER_EDIT **落盘**；``commit --author=X <x@y>`` 与裸 ``commit``
# → rc=1、MARKER_EDIT **落盘**、stderr「Aborting commit due to empty commit message.」；
# ``commit -m ''`` → rc=1 且**不**落盘（git 自拒空正文，落不到编辑器）。
# 故正文来源集合排除 ``-c``（小写：复用正文但开编辑器改写）；``-m ''`` 这类空值形态仍放行
# （git 自己拒空正文，不会落到编辑器）。
_COMMIT_MESSAGE_FLAGS = frozenset(
    {"-m", "--message", "-F", "--file", "-C", "--reuse-message"}
)

# 已知高危旗标 → 精确拒绝理由（不在表内的非白名单旗标走通用理由）
_COMMIT_DENY_EXPLAIN: dict[str, str] = {
    "--no-verify": "跳过 pre-commit hook，即绕过 E020 提交范围校验（红线唯一守门人）",
    "-n": "同 --no-verify",
    "-a": "批量把已跟踪改动入索引，绕过 staged ⊆ files_to_edit 的声明范围判定",
    "--all": "同 -a",
    "-am": "-a 与 -m 的短旗标簇（含批量入索引）",
    "--amend": "改写已提交历史（红线 #②）；需要重做走 orchd retract / force-status",
    "--fixup": "改写式提交（配 --rebase 使用，红线 #②）",
    "--squash": "改写式提交（红线 #②）",
    "-C": "复用既有提交（含 cherry-pick 语义，越出细粒度提交职责）",
    "-c": "同 -C 且开编辑器",
    "--reuse-message": "同 -C",
    "--reedit-message": "同 -c",
    "--allow-empty": "空提交属占位噪音，提交由引擎在 done 收口",
    "--allow-empty-message": "无信息提交，同上",
    "--no-gpg-sign": "关闭签名校验属绕过型旗标",
    "-e": "开编辑器改写提交（交互式，代理非交互通道）",
    "--edit": "同 -e",
    "-i": "交互式选择性提交（代理非交互通道）",
    "--interactive": "同 -i",
    "-p": "逐块选择入索引（越出声明范围判定）",
    "--patch": "同 -p",
    "--pathspec-from-file": "从文件批量读路径入索引，绕过声明范围判定",
    "--edit-description": "非 git-commit 旗标（实测 rc=129 unknown option；它属 git branch）",
    "--reset-author": "覆写作者身份（身份纪律）",
}


def _commit_rejections(rest: list[str]) -> list[str]:
    """commit 豁免一旗标白名单校验，返回违规说明清单（空清单 = 放行）。

    白名单旗标必须**带值**：``-m x`` / ``-mx`` / ``--message=x`` 三形态皆可；裸形态
    不得吞掉以 ``-`` 开头的后随 argv（否则 ``-m --no-verify`` 把绕过旗标当消息偷吃）。
    ``--`` 之后与任何裸位置参数一律拒绝（提交范围由索引 + E020 hook 决定；扫描器不为
    被拒旗标建模取值位，故不区分 pathspec 与旗标取值，fail-closed）。
    另须至少出现一个**正文来源旗标**（见 ``_COMMIT_MESSAGE_FLAGS``：git 据此取说明而不
    启动编辑器）——否则 git 会 spawn ``core.editor`` 取正文，豁免通道就成了命令执行面（F-a）。
    """
    problems: list[str] = []
    # 已出现的正文来源旗标名（缺值或被拒且不带捆绑取值的旗标不入集，故 --author 裸用
    # 不能冒充正文来源满足 F-a 要求）
    seen: set[str] = set()
    after_dashdash = False
    i, n = 0, len(rest)
    while i < n:
        arg = rest[i]
        if after_dashdash:
            problems.append(f"裸参数 '{arg}'：提交范围由索引与 E020 hook 决定")
            i += 1
            continue
        if arg == "--":
            after_dashdash = True
            i += 1
            continue
        if not arg.startswith("-"):
            problems.append(f"裸参数 '{arg}'：提交范围由索引与 E020 hook 决定")
            i += 1
            continue
        name, eq, _val = arg.partition("=")
        has_value = bool(eq)
        if name not in _COMMIT_VALUE_FLAGS and len(arg) > 2 and arg[:2] in _COMMIT_SHORT_WITH_VALUE:
            # 粘连短旗标 -m<msg> / -F<path>：值已在 argv 片段内
            name, has_value = arg[:2], True
        if name not in _COMMIT_VALUE_FLAGS:
            why = _COMMIT_DENY_EXPLAIN.get(name, "非取值型白名单旗标（fail-closed 拒绝）")
            problems.append(f"旗标 '{name}'：{why}")
            # git parse-options 短旗标捆绑：``-am x`` = ``-a`` + ``-m x``，末位取值型字符
            # 的正文来自后随 argv（实测 ``git commit -am msg`` rc=0 且不 spawn 编辑器），
            # 故按 git 语义吞掉该 argv——它不是 pathspec，正文也确实存在。
            if (
                not eq
                and len(name) > 2
                and not name.startswith("--")
                and name[-1] in ("m", "F")
            ):
                seen.add("-" + name[-1])
                i += 2
                continue
            i += 1
            continue
        if has_value:
            seen.add(name)
            i += 1
            continue
        if i + 1 >= n:
            problems.append(f"旗标 {name} 缺值（白名单旗标必须显式带值）")
            break
        if rest[i + 1].startswith("-"):
            problems.append(
                f"旗标 {name} 的值不得以 '-' 开头（防把绕过旗标当值吞掉）"
            )
            i += 2
            continue
        seen.add(name)
        i += 2
    if not problems and not seen & _COMMIT_MESSAGE_FLAGS:
        # 只在「除此之外形态合法」时报缺正文：被拒旗标（-C/-c/-e 等）的取值位不建模，
        # 对其断言「git 会开编辑器」可能不实（实测 -C/--reuse-message 不 spawn）。
        problems.append(
            "缺少正文来源旗标：须显式给 -m/--message/-F/--file"
            "（代理不开编辑器；实测裸 commit、-c、仅 --author 都会 spawn core.editor 取正文）"
        )
    return problems


# pass9 F17（task-pass9-gitops-read-boundary）→ task-git-proxy-enforcement-holes 洞②：
# 「只读」子命令的危险旗标按**副作用类别**收口。旧实现只有 3 个名字
# （``--ext-diff`` / ``--output`` / ``-O``），漏网的真危险旗标按本机 git 2.55 逐条实测。
#
# 证据形态（task-git-proxy-driver-residual-boundary 收口）：下列每条**执行面**事实都由
# ``tests/test_git_proxy.py::TestDeniedFlagExecutionSurface`` 每轮**直连 git** 复现（正对照
# 不成立即显式失败），「判写」那半边由 ``tests/test_git_proxy_read_denylist.py`` 锁。
# pass13 把原文记在 ``C:/Temp/p13-*.txt``——scratch 会被清扫，引用即失真，故一律改指 CI。
#
# 执行类（spawn 外部命令）：
# - ``--ext-diff`` / ``diff.external``：配了 external 时**裸** ``diff HEAD~1`` 就执行它
#   （标记集合 EXT+F；external 路径不再调 textconv，故无 TC）。``--ext-diff`` 与裸跑同结果
#   ⇒ 判写**正确但非充分**，真正挡住外部命令的是只读通道注入的 ``--no-ext-diff``；而
#   ``--no-ext-diff`` 会把 textconv 放回来（F+TC）——两个否定旗标各管一条 spawn 路径，
#   缺一不可。⇒ ``::test_ext_diff_runs_the_configured_external_command``
# - ``--textconv``：真选项（rc=0）且落 MARKER_TC
#   ⇒ ``::test_textconv_flag_is_a_real_option_and_spawns_textconv``
# - ``--text``：``--textconv`` 的合法前缀缩写，迫使 git 取文本内容 ⇒ textconv 照样被调用
#   （``diff/log/show/blame --text`` 四形全 rc=0 且 MARKER_TC 落盘）
#   ⇒ ``::test_text_flag_forces_textconv_spawn``
# - ``--ext-grep``：git-grep(1) 改用外部 grep 命令，真选项 rc=0
#   ⇒ ``::test_ext_grep_is_a_real_option``
# - ``--open-files-in-pager``、``grep`` 域内的 ``-p`` 与 ``-O<cmd>``：把命中文件交给外部命令。
#   ``grep -O1 line1`` → rc=255 ``cannot spawn 1``（真去 spawn）；
#   ``grep --open-files-in-pager=touch PAGER_HIT line1`` → rc=0 且**建出 PAGER_HIT**；
#   ``grep -lOpager line1`` → rc=255 ``cannot spawn pager``（簇内任意位置的 O 同义 -Opager）。
#   ⇒ ``::test_grep_dash_upper_o_really_spawns`` / ``::test_open_files_in_pager_creates_files``
#   / ``::test_short_cluster_spawns_from_any_position``
# - ``--filters``：git-cat-file(1) 对 blob 应用 clean/smudge 过滤器。
#   ``cat-file --filters --path=bin.dat <blob>`` → rc=0 且 MARKER_F 落盘；同一 blob 走
#   ``cat-file blob`` 一个标记都不落（落盘确由该旗标引起）。
#   ⇒ ``::test_cat_file_filters_spawns_smudge_filter``
# - ``--exec`` / ``--paginate`` / ``--pager``：漂到子命令位时 git 自身即拒（本 build rc=128
#   ``unrecognized argument``），收录属**防御性**
#   ⇒ ``::test_top_level_flags_are_unrecognized_at_subcommand_position``
#
# 写盘类：``--output``（git-log(1)/git-diff(1) 输出重定向到任意路径）——
# ``log --output=OUTER -1`` rc=0 且真在该路径落文件
# ⇒ ``::test_output_flag_writes_an_arbitrary_path``
#
# 状态类：``--refresh``——本行**按实测改述**。pass13 原记「``diff-index --refresh`` 被识别
# （非 unknown）」，该判据不可复现：同一仓库里 ``diff-index --bogus-flag HEAD`` 同样只吐
# rc=129 usage、不带 ``unknown option``，两者无从区分。实测口径是
# ``diff --refresh HEAD~1`` / ``diff-files --refresh`` / ``diff-index --refresh HEAD`` **一律
# rc=129**（三者去掉 ``--refresh`` 后均 rc=0）⇒ 本 build 里 git 自己就不接受它，拒绝代价为零，
# 属防御性收口；不得声称「实测会写 ``.git/index`` stat 缓存」。
# ⇒ ``::test_refresh_is_rejected_by_this_git_build``
#
# 形态覆盖（第 1 轮 code review F-b 后补齐）：``--opt``、``--opt=val``、``-O<val>``（粘连）、
# **长旗标前缀缩写**、**短旗标簇**。
# - 前缀缩写：git parse-options 对长旗标默认接受唯一前缀，实测
#   ``git grep --ope=touch ABBR_PAGER line1`` rc=0 且**建出 ABBR_PAGER**（按名相等匹配可绕，
#   ⇒ ``::test_prefix_abbreviation_really_reaches_the_pager``），故长旗标按「相等或为被禁名的
#   前缀」命中；``--`` 之后为 pathspec，扫描终止（名为 ``--output`` 的文件路径不误伤，由
#   test_dashdash_terminates_flag_scan 守）。
# - 短旗标簇：``-lOpager`` 一类簇内任意位置的 ``O``（grep）同义于 ``-Opager``，实测 git
#   按字符逐个解析并吞余下为值（⇒ ``::test_short_cluster_spawns_from_any_position``），故按
#   字符扫描；``p`` **只在 grep 域**危险（``log -p`` / ``diff -p`` 是 --patch，真只读，超集态
#   必须不动 ⇒ ``::test_dash_p_outside_grep_stays_read``）。
# - 前缀命中的已知代价：被禁名的短前缀（``--o`` / ``--re`` / ``--te`` …）一并拒绝——git 自身
#   对这类缩写多数报 ambiguous，属可接受的过严。
# - **不设前缀豁免集**（第 2 轮 code review F-c 推翻）：曾把 ``--text`` 当「``--textconv`` 的
#   真只读前缀」豁免，实测为假——``--text`` 迫使 git 取文本内容，textconv driver 照样被调用
#   （四形 MARKER_TC 落盘 ⇒ ``::test_text_flag_forces_textconv_spawn``）。看二进制文本差异的
#   替代形态是 ``diff --no-textconv``（``TestDriverNeutralization`` 断言经代理零标记）与
#   ``cat-file blob``（本类负控制零标记）。
# - 残留面（不由本表闭合，已登记后继任务 task-git-proxy-driver-exec-surface）：driver 对 diff 族
#   是**默认开启**的——实测 ``git diff HEAD~1`` / ``log -1 -p`` / ``show`` / ``blame bin.dat``
#   不带任何旗标也会 spawn textconv 与 filter（正对照
#   ``TestReadFormDriverSurface::test_positive_control_direct_git_still_spawns_drivers``），
#   须在执行层中和。
_READ_DENY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "exec": (
        "--ext-diff", "--textconv", "--ext-grep",
        "--open-files-in-pager", "--exec", "--paginate", "--pager",
        "--filters",
    ),
    "write": ("--output",),
    "state": ("--refresh",),
}

# 危险短旗标字符，按子命令分域；``"*"`` 为默认域。
_READ_DENY_SHORT_CHARS: dict[str, frozenset[str]] = {
    "grep": frozenset({"O", "p"}),
    "*": frozenset({"O"}),
}

_READ_DENY_LABEL: dict[str, str] = {
    "exec": "可启动外部进程",
    "write": "可写任意路径",
    "state": "可改动仓库状态（索引）",
}


def _read_denied_hit(subcommand: str, rest: list[str]) -> tuple[str, str] | None:
    """只读族的危险旗标探测：返回 ``(类别, 命中的 argv 片段)``，无命中 → None。

    命中判据三条并行（缺一即可绕，见上表实测）：长旗标相等或前缀、短旗标逐字符、
    ``-O`` 粘连（归入短旗标扫描）。
    """
    short_chars = _READ_DENY_SHORT_CHARS.get(
        subcommand, _READ_DENY_SHORT_CHARS["*"]
    )
    for arg in rest:
        if arg == "--":
            return None
        name = arg.split("=", 1)[0]
        if name.startswith("--"):
            for category, denied in _READ_DENY_CATEGORIES.items():
                if any(d == name or d.startswith(name) for d in denied):
                    return category, arg
            continue
        if name.startswith("-"):
            if any(ch in short_chars for ch in name[1:]):
                return "exec", arg
            continue
        # 非旗标片段（rev / pathspec / 取值）不参与危险旗标判定
    return None


def _read_denied_reason(subcommand: str, hit: tuple[str, str]) -> str:
    category, flag = hit
    return (
        f"只读子命令 {subcommand} 携带 {_READ_DENY_LABEL[category]} 旗标 '{flag}'："
        "只读透传拒绝（fail-closed）"
    )


def classify_git_argv(argv: list[str] | None, trunk: str = "main") -> dict[str, Any]:
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
        problems = _commit_rejections(rest)
        if problems:
            return {
                "subcommand": subcommand,
                "rest": rest,
                "kind": "write",
                "family": "commit",
                "reason": "commit 豁免一仅放行取值型旗标；" + "；".join(problems),
            }
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "commit",
            "family": "commit",
            "reason": "任务分支 git commit 是红线 #1 豁免一（提交范围由 E020 hook 校验）",
        }
    if subcommand == "merge" and _is_merge_passthrough(rest, trunk):
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "merge",
            "family": "merge",
            "reason": f"任务分支 merge {trunk} 精确形态是红线 #1 豁免二（受管出口）",
        }
    if subcommand in READ_ONLY_SUBCOMMANDS:
        hit = _read_denied_hit(subcommand, rest)
        if hit is not None:
            return {
                "subcommand": subcommand,
                "rest": rest,
                "kind": "write",
                "family": "write",
                "reason": _read_denied_reason(subcommand, hit),
            }
        return {
            "subcommand": subcommand,
            "rest": rest,
            "kind": "read",
            "family": "read",
            "reason": "只读子命令",
        }
    if _is_read_variant(subcommand, rest):
        # 纵深防御（洞②）：条件只读形态同样过一遍危险旗标探测。当前白名单与之无交集
        # （实测不可达），但 CONDITIONAL_READ_FLAGS 日后加旗标时不必再改这里。
        hit = _read_denied_hit(subcommand, rest)
        if hit is not None:
            return {
                "subcommand": subcommand,
                "rest": rest,
                "kind": "write",
                "family": "write",
                "reason": _read_denied_reason(subcommand, hit),
            }
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


# pass13 F-e（task-git-proxy-driver-exec-surface）：diff 族的 textconv / filter driver 是
# **默认开启**的——``git diff HEAD~1`` / ``log -1 -p`` / ``show`` / ``blame bin.dat`` 不带任何
# 危险旗标也会 spawn ``.gitattributes`` + config 指定的外部命令。分类层的旗标黑名单
# **原理上无法闭合**这条面：合法形态与危险形态共享同一条 argv，故在执行层注入否定旗标中和。
#
# 注入白名单逐子命令实测建立，**不接受推测**。证据不写「原文见某临时文件」（scratch 会被
# 清扫，引用即失真），而由 CI 每轮复现：``tests/test_git_proxy.py::TestDriverNeutralization``
# （四形经代理一个标记都不落 + 直连正对照）与 ``::TestReadFormDriverSurface``（全 read 面
# 裸跑不 spawn + 注入 rc 分档）。
#
# 测量口径（git 2.55.0.windows.5）：driver 仓库配 ``diff.tc.textconv`` +
# ``filter.mk.smudge``/``filter.mk.clean``，``.gitattributes`` 把 bin.dat 交给两个 driver；
# 对照组是同构的无 driver 仓库。driver 命令一律写成 ``touch MARKER && cat`` 形态——裸
# ``touch`` 吃掉 stdin 会把需要重写的工作树文件截成 0 字节，于是「中和前后字节相同」这类
# 结论的**基线本身已被 driver 破坏**（本任务实测 merge 面时才暴露，见 _proxy_merge）。
#
# 覆盖面 = classify_git_argv 判为 kind=read 的形态清单，本次重新实测为 **43 条**（清单正文在
# ``TestReadFormDriverSurface._ALL_READ``；pass13 记的「44 形态」未留可复现清单，以本次为准。
# ``reflog show HEAD`` 这类「动词 + 裸参数」形态分类层判 **write**，本就不在 read 面上）。
#
# 夹具形态本身就是判据的一部分（本任务实测纠正 pass13）：``diff.tc.textconv`` 与
# ``filter.mk.smudge`` 都必须写成 ``touch MARKER && cat``。裸 ``touch`` 吃掉 stdin，会让
# ``git blame <path>`` 只落 MARKER_TC 而不落 MARKER_F（9 格隔离矩阵，与命令先后、是否先跑过
# diff/status 均无关）——真实 textconv 驱动都吐内容，故裸形态**低估**了 spawn 面，pass13 记的
# 「blame 只落 TC」就是这么来的。smudge 少 ``cat`` 更糟：重写工作树文件时直接把内容截成 0
# 字节，任何字节比较的基线都被 driver 自己破坏。
#
# 收录条件（三条同时成立）：
#   ① 注入后 rc 与裸跑一致（git 真接受这两个旗标，非 rc=129 unknown option）；
#   ② 无 driver 仓库里注入前后 stdout 与 stderr **逐字节相同**（不改写正常输出）；
#   ③ 属 diff 族——会物化文件内容，是 driver 的真实调用点。
#
# 实测排除项（每条都有 rc / 输出证据，不是「大概不需要」）：
# - ``status`` / ``ls-files`` / ``ls-tree`` / ``cat-file``(blob 与 -t 两形) / ``grep`` /
#   ``merge-base`` / ``name-rev`` / ``for-each-ref`` / ``show-ref`` / ``check-attr`` /
#   ``check-ignore`` / ``count-objects`` / ``describe`` / ``verify-commit`` / ``verify-tag`` /
#   ``help`` / ``version``，以及 branch/tag/config/remote/stash/worktree/notes 的只读变体：
#   注入 → **rc=129** ``error: unknown option``（本次实测该族 **28 条**形态全中），会把合法
#   只读调用打断；且裸跑实测**均不触发** driver（43 条 read 形态里只有 log -1 -p /
#   diff HEAD~1 / show / blame bin.dat 四形落盘标记，且四形都是 TC+F 同落——见上文夹具说明），
#   故既不能注入也无需注入。
# - ``submodule status``：另一族「假接受且不报错」——注入后 rc 从 0 变 **1** 并吐 usage，
#   不属 diff 族且无需中和，排除。
# - ``rev-parse``：**最危险的假接受**——注入后 rc 仍为 0（不报 unknown option），但 git 把
#   两个旗标当 rev 回显，stdout 从 ``<sha>`` 变成 ``--no-textconv\n--no-ext-diff\n<sha>``：
#   静默污染，调用方按行解析会拿到垃圾且无从察觉。二者裸跑都不触发 driver，故一律排除。
# - ``reflog``（``reflog show`` 形态）：pass13 曾记为「stdout 与 stderr 均变」，**本次实测纠正**
#   ——注入点在子命令之后，旗标把动词 ``show`` 挤成位置参数，实测 rc=**128**
#   ``fatal: ambiguous argument 'show'``：是硬失败而非静默污染。裸跑不触发 driver，仍排除。
# - ``rev-list``：注入被接受且输出逐字节相同，但裸跑不触发 driver——注入零收益，为最小化
#   argv 改写面不收录。
# - ``whatchanged``：本 build 里**恒 rc=128**（``'git whatchanged' is nominated for removal``），
#   作为白名单成员暂无实际执行面；撤除它属白名单变更、须按上面三条重新实测，故本轮不动。
#
# 已排除的替代方案（同样有实测背书，不是没想过）：用 ``GIT_CONFIG_COUNT`` +
# ``GIT_CONFIG_KEY_n``/``VALUE_n`` 把 ``diff.tc.textconv`` / ``filter.mk.smudge`` 置空来中和
# ——实测 read 面 ``diff`` / ``log -1 -p`` / ``show`` / ``blame bin.dat`` 四形**全部 rc=128**：
# ``.gitattributes`` 仍声明 ``filter=mk`` 而该 driver 的 smudge 被置空，git 直接报错。即配置
# 置空会把合法只读调用从「有副作用」变成「失败」，比原病更糟，故弃用。同一手法在 **merge**
# 面反而是 rc=0 且**静默改写合并结果**，更不可用——见 ``_proxy_merge`` docstring 的实测表。
# 上面两句的 CI 锁：read 面 ``tests/test_git_proxy.py`` 的
# ``::TestReadFormDriverSurface::test_config_blanking_is_not_a_viable_read_face_neutralization``、
# merge 面 ``::TestMergeDriverBoundary::test_config_blanking_changes_merged_bytes``。
_DRIVER_NEUTRALIZE_FLAGS: tuple[str, ...] = ("--no-textconv", "--no-ext-diff")
_DRIVER_NEUTRALIZE_SUBCOMMANDS: frozenset[str] = frozenset({
    "diff", "log", "show", "blame", "shortlog", "whatchanged",
    "diff-tree", "diff-index", "diff-files",
})


def _neutralized_read_args(
    subcommand: str | None, args: list[str]
) -> tuple[list[str], bool]:
    """只读透传的 driver 中和：返回 ``(实际执行的 argv, 是否注入)``。

    注入点固定在子命令之后、其余参数之前（git diff 族的 options 位）；实测重复给出
    ``--no-textconv`` 与调用方自带该旗标均 rc=0 且输出不变，故无需去重。
    """
    if not args or subcommand not in _DRIVER_NEUTRALIZE_SUBCOMMANDS:
        return args, False
    return [args[0], *_DRIVER_NEUTRALIZE_FLAGS, *args[1:]], True


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
    # 单根命名空间（pass8 F1-A）：任务分支恒以 task/ 为根，全线通用常量
    task_prefix = "task/"
    if not branch or not str(branch).startswith(task_prefix):
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

    **driver 边界（task-git-proxy-driver-residual-boundary，本函数刻意不中和）**：
    merge 会物化工作树文件，故仓库配置的 ``filter.*.smudge``/``clean`` 在这里**必然被
    spawn**——这是正确行为而不是漏洞，只读通道那套 ``--no-textconv --no-ext-diff``
    注入在此不可复用。两条中和路径均在 scratch 仓库实测（git 2.55.0.windows.5，
    ff 与 no-ff 两形结论一致；smudge 写成 ``touch MARKER_F && cat`` /
    ``touch MARKER_F && tr a-z A-Z`` 两态）：

    1) 旗标注入 ``git merge --no-textconv --no-ext-diff main`` → **rc=129**
       ``error: unknown option 'no-textconv'``，合并根本不执行（工作树文件也不落）。
       照搬到 merge 就是把红线 #1 豁免二这条受管出口本身打断。
    2) 配置置空（``-c filter.mk.smudge=`` 与 ``GIT_CONFIG_COUNT`` + ``KEY_0``/``VALUE_0``
       两法实测**同结果**）→ rc=0 且标记不再落盘，但**恒等 smudge 与变换 smudge 分档**：
       恒等态（``&& cat``）置空前后 bin.dat 字节相同；变换态（``&& tr a-z A-Z``）直连
       merge 得 ``UPPER`` 字节、置空得原文字节——**中和静默改掉了合并结果的内容**。
       smudge driver 存在的意义正是「入库形态 ≠ 工作树形态」（解密 / 关键字展开 / LFS
       一类），置空等于让 agent 看到一份 git 自己不会产出的工作树。

    即：中和要么把命令弄死（①），要么把结果改错（②）。故本通道口径是
    **代理 merge 必须与直连 merge 逐字节一致**（rc、工作树文件 sha256、标记集合全等），
    由 ``tests/test_git_proxy.py::TestMergeDriverBoundary`` 在 ff / no-ff × 恒等 / 变换
    四组合上锁死，并把「置空会改字节」这一反例本身钉成用例——将来若有人想在此注入中和
    或置空配置，那一条会先红。

    风险承接的前提是 **driver 命令只来自本地 ``.git/config``，而 agent 经代理写不到它**：
    ``config <key> <value>`` / ``config --global core.editor <cmd>`` / ``config --unset <key>``
    三种写形态在分类层命中写操作族、**执行前** E007 拒绝（``.git/config`` 一字未改），只有
    ``--list`` / ``--get`` 的只读形态放行——由
    ``::TestMergeDriverBoundary::test_driver_config_is_not_agent_writable_via_proxy``
    锁住。即 agent 能触发仓库所有者**已配好**的 driver，不能自己装一个。另：agent 在本仓库
    里本就拥有该工作树，这条边界与引擎自动执行的 ``git add`` / ``git checkout`` 同语义，
    详见 ``.orchd/rules/git.md``「driver 触发面与中和边界」。
    """
    state = check_workspace_state(project_root)
    branch = state.get("branch")
    # 单根命名空间（pass8 F1-A）：同 commit 豁免常量
    task_prefix = "task/"
    if not branch or not str(branch).startswith(task_prefix):
        raise OrchdError(
            ErrorCode.E018,
            f"wrong_branch: git merge {resolve_trunk_for(project_root)} 仅在任务分支允许"
            f"（红线 #1 豁免二），当前在 '{branch}'",
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


def _write_rejection(
    args: list[str], cls: dict[str, Any], agent_id: str | None
) -> OrchdError:
    """写操作（含形态不合法的 commit 豁免）的结构化拒绝载荷。"""
    rule, hint = _WRITE_FAMILIES.get(cls["family"], _WRITE_FAMILIES["other"])
    return OrchdError(
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
    cls = classify_git_argv(args, trunk=resolve_trunk_for(root))

    if cls["kind"] == "write" and cls["family"] == "commit":
        # 形态不合法的 commit 豁免：有 git / 无 git 两态都拒绝——收窄的是豁免范围，
        # 不是执行能力（无 git 下 --no-verify 同样不该推进快照）。
        raise _write_rejection(args, cls, agent_id)

    if not _git_available(root):
        # 无 git 降级（A0）：commit → 本地快照，其余 → no-op（不抛错）
        return _nogit_payload(args, cls, root, orchd_dir)

    if cls["kind"] == "read":
        exec_args, neutralized = _neutralized_read_args(cls.get("subcommand"), args)
        payload = _execute_git(root, exec_args, cls)
        if neutralized:
            # git_args 报告**实际执行**的 argv（诚实口径）；调用方原样意图另存一字段供审计。
            payload["git_args_requested"] = args
            payload["driver_neutralized"] = list(_DRIVER_NEUTRALIZE_FLAGS)
        return payload
    if cls["kind"] == "commit":
        return _proxy_commit(root, args, cls)
    if cls["kind"] == "merge":
        return _proxy_merge(root, args, cls)

    raise _write_rejection(args, cls, agent_id)
