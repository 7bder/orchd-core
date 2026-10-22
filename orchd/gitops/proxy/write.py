"""orchd gitops proxy — 写通道域（commit / merge / nogit 载荷 / 写拒绝）（W6-C4 拆分，纯搬移）。

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
    _WRITE_FAMILIES,
)

from .execute import (
    _execute_git,
)




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
