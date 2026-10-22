"""orchd gitops proxy — 代理入口域（run_git_proxy 总调度）（W6-C4 拆分，纯搬移）。

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
from .classify import (
    classify_git_argv,
)

from .execute import (
    _DRIVER_NEUTRALIZE_FLAGS,
    _execute_git,
    _git_available,
    _neutralized_read_args,
)

from .write import (
    _nogit_payload,
    _proxy_commit,
    _proxy_merge,
    _write_rejection,
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
