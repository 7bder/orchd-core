"""orchd gitops proxy — 拒绝原因域（W6-C4 拆分，纯搬移）。

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
