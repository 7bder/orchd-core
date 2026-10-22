"""orchd gitops proxy — argv 分类域（W6-C4 拆分，纯搬移）。

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
from .writetables import (
    _WRITE_SUBCOMMANDS,
    _WRITE_TOKENS,
)

from .deny import (
    _commit_rejections,
    _read_denied_hit,
    _read_denied_reason,
)



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
