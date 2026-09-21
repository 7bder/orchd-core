"""orchd/gitops/ref_tx_hook.py — git ``reference-transaction`` hook：红线 #1/#2 **强制层**。

**分层定位（承 D1a）**：

- **便捷层** = ``orchd git <args>`` 代理（``orchd/gitops/proxy.py``）：agent 走它得到
  结构化放行/拒绝与替代通道提示；
- **强制层** = 本 hook（本模块）：即使 agent **绕过代理**直接敲 ``git``，只要发生
  ref 事务（建分支 / 删分支 / 改引用 / 打 tag / 移动 main），git 会调用本 hook，
  在 **prepared 阶段**（引用尚未落盘）拒绝事务 ⇒ 红线 #1/#2 由"靠人记得"变为
  "由 git 强制"。

**分层语义一致**：两层的**允许集合**同源（任务分支提交是唯一豁免），实现方式不同
（代理=命令级白名单，hook=引用事务级白名单）。

**hook 协议**（git 官方）：``reference-transaction <prepared|committed|aborted>``，
stdin 每行 ``<old-value> <new-value> <ref-name>``。非零退出仅对 **prepared** 生效
（committed / aborted 一律放行，避免中断已开始的提交）。

**判定表**（fail-closed 于受管命名空间，fail-open 于未知命名空间以避免打断引擎流程）：

+----------------------------------+-------------------------------------------+
| ref                              | 处置                                       |
+==================================+===========================================+
| ``refs/heads/task/**``           | 放行（任务分支是唯一豁免；删除亦放行——引擎   |
|                                  | merge 后自动清理）                          |
| ``refs/heads/orchd/**``          | 放行（引擎账本同步 ref，rules/git.md 既定）  |
| ``refs/remotes/**``              | 放行（fetch 更新远端跟踪引用，非写操作）      |
| ``refs/heads/<默认分支>``         | **仅放行快进/后代更新**（引擎 merge、intake  |
|                                  | 提交）；非快进（reset --hard / 强制移动 /    |
|                                  | rebase 改写）⇒ 拒绝（红线 #②破坏性）        |
| 其它 ``refs/heads/**``            | 拒绝（红线 #④：分支由引擎管理，用 orchd claim）|
| ``refs/tags/**`` / ``refs/stash`` | 拒绝（红线 #①：手动 tag / stash）           |
| 其它                              | 放行并记录（避免打断 fetch / 引擎内部引用）   |
+----------------------------------+-------------------------------------------+

**逃生口**：``ORCHD_ALLOW_REF_TX=1``（人工修复 / 仓库迁移；与
``ORCHD_ALLOW_CONTAINER_ROOT`` 同类，需显式设置，不留后门）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

# 引用命名空间
TASK_PREFIX = "refs/heads/task/"
ENGINE_REF_PREFIX = "refs/heads/orchd/"
REMOTE_PREFIX = "refs/remotes/"
HEADS_PREFIX = "refs/heads/"
TAG_PREFIX = "refs/tags/"
STASH_REF = "refs/stash"

_ZERO = "0" * 40

# 允许集合（与代理层同源的"唯一豁免"）
_ALLOW_TASK = "task_branch"
_ALLOW_ENGINE = "engine_ref"
_ALLOW_REMOTE = "remote_tracking"
_ALLOW_FF_DEFAULT = "default_branch_fast_forward"
_ALLOW_UNCLASSIFIED = "unclassified_allow"

# 拒绝集合
_REFUSE_NON_FF = "non_fast_forward_default"
_REFUSE_FOREIGN_BRANCH = "foreign_branch"
_REFUSE_TAG = "tag_write"
_REFUSE_STASH = "stash_write"

_REFUSAL_HINTS = {
    _REFUSE_NON_FF: (
        "默认分支只接受快进/后代更新（引擎 merge、intake 提交）；"
        "reset --hard / 强制移动 / rebase 改写属红线 #2 破坏性操作。"
        "如需丢弃进度请用 orchd retract / force-status 并重新 claim"
    ),
    _REFUSE_FOREIGN_BRANCH: (
        "分支由引擎管理（红线 #④）：请用 orchd claim 获取 task/<id> 分支，"
        "不要手动创建/移动非任务分支"
    ),
    _REFUSE_TAG: "手动打 tag 属红线 #①：发版 tag 由项目管理员执行（见 rules/git.md）",
    _REFUSE_STASH: "git stash 属红线 #①：请用任务分支提交保留细粒度进度",
}

# 阈值：默认分支名的兜底（拿不到 git 配置时）
_FALLBACK_DEFAULT_BRANCH = "main"


def parse_updates(text: str) -> list[dict[str, str]]:
    """解析 hook stdin：每行 ``<old> <new> <ref>``（空行 / 畸形行跳过）。"""
    updates: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        old, new, ref = parts
        updates.append({"old": old, "new": new, "ref": ref})
    return updates


def _is_delete(update: dict[str, str]) -> bool:
    return update["new"] == _ZERO


def _is_create(update: dict[str, str]) -> bool:
    return update["old"] == _ZERO


def classify_update(
    update: dict[str, str],
    *,
    default_branch: str,
    is_ancestor: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """单条引用更新的分类（纯函数；``is_ancestor`` 由调用方注入以隔离 git 依赖）。

    Returns:
        ``{"ref", "action": "allow"|"refuse", "rule", "reason"?, "hint"?}``。
    """
    ref = update["ref"]
    base: dict[str, Any] = {"ref": ref}

    if ref.startswith(TASK_PREFIX):
        return {**base, "action": "allow", "rule": _ALLOW_TASK}
    if ref.startswith(ENGINE_REF_PREFIX):
        return {**base, "action": "allow", "rule": _ALLOW_ENGINE}
    if ref.startswith(REMOTE_PREFIX):
        return {**base, "action": "allow", "rule": _ALLOW_REMOTE}
    if ref == STASH_REF:
        return {
            **base, "action": "refuse", "rule": _REFUSE_STASH,
            "reason": "refs/stash 更新（git stash）", "hint": _REFUSAL_HINTS[_REFUSE_STASH],
        }
    if ref.startswith(TAG_PREFIX):
        return {
            **base, "action": "refuse", "rule": _REFUSE_TAG,
            "reason": "tag 引用更新（git tag / push --tags）",
            "hint": _REFUSAL_HINTS[_REFUSE_TAG],
        }
    if ref == f"{HEADS_PREFIX}{default_branch}":
        # 创建（old=0）放行：仓库初始化 / 首次提交；非快进更新拒绝（红线 #②）
        if _is_create(update) or is_ancestor is None:
            return {
                **base, "action": "allow", "rule": _ALLOW_FF_DEFAULT,
                "reason": "默认分支创建" if _is_create(update) else "快进判定不可用（放行）",
            }
        if is_ancestor(update["old"], update["new"]):
            return {**base, "action": "allow", "rule": _ALLOW_FF_DEFAULT,
                    "reason": "快进/后代更新"}
        if _is_delete(update):
            return {
                **base, "action": "refuse", "rule": _REFUSE_FOREIGN_BRANCH,
                "reason": "删除默认分支引用",
                "hint": _REFUSAL_HINTS[_REFUSE_FOREIGN_BRANCH],
            }
        return {
            **base, "action": "refuse", "rule": _REFUSE_NON_FF,
            "reason": f"默认分支非快进更新 {update['old'][:7]} → {update['new'][:7]}",
            "hint": _REFUSAL_HINTS[_REFUSE_NON_FF],
        }
    if ref.startswith(HEADS_PREFIX):
        return {
            **base, "action": "refuse", "rule": _REFUSE_FOREIGN_BRANCH,
            "reason": (
                f"非任务分支引用操作（{'删除' if _is_delete(update) else '创建/更新'} "
                f"{ref[len(HEADS_PREFIX):]}）"
            ),
            "hint": _REFUSAL_HINTS[_REFUSE_FOREIGN_BRANCH],
        }
    return {**base, "action": "allow", "rule": _ALLOW_UNCLASSIFIED,
            "reason": "未纳入受管命名空间（放行以免打断 fetch / 引擎内部引用）"}


def evaluate(
    updates: list[dict[str, str]],
    *,
    state: str,
    default_branch: str = _FALLBACK_DEFAULT_BRANCH,
    is_ancestor: Callable[[str, str], bool] | None = None,
) -> dict[str, Any]:
    """对一个引用事务做判定（纯函数）。

    ``state != "prepared"`` 一律放行：committed / aborted 阶段拒绝无意义且会打断
    已开始的提交（git 官方语义：prepared 是唯一可拒绝点）。
    """
    if state != "prepared":
        return {
            "state": state, "blocked": False, "refusals": [],
            "allowed": [{"ref": u["ref"], "rule": "phase_not_prepared"} for u in updates],
            "reason": f"{state} 阶段不可拒绝（仅 prepared 可拒绝）",
        }
    refusals: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    for update in updates:
        verdict = classify_update(
            update, default_branch=default_branch, is_ancestor=is_ancestor,
        )
        (refusals if verdict["action"] == "refuse" else allowed).append(verdict)
    return {
        "state": state,
        "blocked": bool(refusals),
        "refusals": refusals,
        "allowed": allowed,
        "reason": (
            "红线 #1/#2 强制层：引用事务被拒（详见 refusals）" if refusals else "全部放行"
        ),
    }


# ----------------------------------------------------------------------
# 进程入口（hook 由 git 调用）
# ----------------------------------------------------------------------


def _git_ancestor_check(project_root: Path) -> Callable[[str, str], bool]:
    """构造 ``is_ancestor(old, new)``：``git merge-base --is-ancestor``（best-effort）。

    拿不到结论（非 git / 对象缺失 / git 不可用）时返回 True（**放行**）——强制层
    优先不打断引擎流程；非快进判定的兜底由 ``classify_update`` 的显式分支承担。
    """
    def _check(old: str, new: str) -> bool:
        try:
            proc = subprocess.run(
                ["git", "-C", str(project_root), "merge-base", "--is-ancestor", old, new],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        return True

    return _check


def _resolve_default_branch(project_root: Path) -> str:
    """默认分支名（best-effort；失败回退 ``main``）。"""
    try:
        from orchd.gitops import get_default_branch

        return get_default_branch(project_root) or _FALLBACK_DEFAULT_BRANCH
    except Exception:  # noqa: BLE001 - hook 环境最小依赖，任何失败都回退
        return _FALLBACK_DEFAULT_BRANCH


def main(argv: list[str] | None = None, *, stdin_text: str | None = None) -> int:
    """hook 入口：返回 0 放行 / 1 拒绝（非零仅在 prepared 阶段有意义）。

    Args:
        argv: ``["reference-transaction", "<state>"]``（缺省取 ``sys.argv``）。
        stdin_text: 引用更新文本；缺省从 stdin 读取（便于测试注入）。
    """
    args = list(sys.argv if argv is None else argv)
    state = args[1] if len(args) > 1 else ""
    if os.environ.get("ORCHD_ALLOW_REF_TX"):
        return 0
    try:
        text = stdin_text if stdin_text is not None else sys.stdin.read()
    except Exception:  # noqa: BLE001 - 读 stdin 失败视作无更新（放行）
        text = ""
    updates = parse_updates(text)
    if not updates:
        return 0
    project_root = Path.cwd()
    verdict = evaluate(
        updates,
        state=state,
        default_branch=_resolve_default_branch(project_root),
        is_ancestor=_git_ancestor_check(project_root),
    )
    if not verdict["blocked"]:
        return 0
    try:
        for refusal in verdict["refusals"]:
            print(
                f"[orchd ref-tx] 拒绝引用更新 {refusal['ref']}：{refusal.get('reason')}",
                file=sys.stderr,
            )
            print(f"[orchd ref-tx] 处置建议：{refusal.get('hint')}", file=sys.stderr)
        print(
            "[orchd ref-tx] 红线 #1/#2 强制层拦截（代理被绕过亦生效）；"
            "agent 请走 orchd claim / done / review 等引擎命令",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 - 输出失败不影响拒绝语义
        pass
    return 1


if __name__ == "__main__":  # pragma: no cover - 由 git hook 以 -m 调用
    sys.exit(main())
