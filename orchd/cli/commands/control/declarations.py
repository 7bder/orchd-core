"""declare 撤回日志与分支重叠判定（_log_decl_withdraw/_decl_withdraw_branch_overlap），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


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
    # 跨 mypy 版本免疫：getattr 取回 Any，无需 type-ignore（旧 ignore 码
    # [attr-defined] 在新版报 union-attr，pass8 F3）；缺失/异常仍静默跳过。
    _reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
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
    # task-line-diag-wiring：分支名与 base 按当前线解析（单线恒 task/{id} / main）
    from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

    branch = resolve_task_branch_for(project_root, task_id)
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
        base = resolve_trunk_for(project_root)
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
