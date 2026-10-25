"""orchd gitops proxy — 执行通道域（W6-C4 拆分，纯搬移）。

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
# - ``whatchanged``：**默认拒绝执行**（rc=128），且拒绝文案随 git 版本漂移——2.55 为
#   ``is nominated for removal``、2.56 为 ``will be removed soon.``（故测试只锁行为、不锁措辞）；
#   加 ``--i-still-use-this`` 后执行面恢复（rc=0），注入两旗标 stdout 逐字节不变（实测
#   git 2.56.0.windows.2）⇒ 注入**有效且无害**。代理默认形态不带该旗标，故
#   ``orchd git whatchanged`` 恒 rc=128。保留为白名单成员：撤除属白名单变更、须按上面三条
#   收录条件重新实测（预期结论为保留，但须由实测背书而非沿用旧论证）。
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
