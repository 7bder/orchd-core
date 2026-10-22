"""已落地任务反解：三个 _LANDED_* 正则 + landed_task_ids_since / previous_release_tag。"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import orchd.gitops as _gitops_pkg


# 落地任务集三正则（task-changelog-landed-single-source）：git 历史为权威源，
# merge/impl/paren 三形态 single-source 收敛于此，发版门⑧与漂移测试双消费。
# 错口径对照：``--merges`` 漏 ff 合并（ff 不产 merge 提交）；DONE 事件 ≠ 落地
# （done 未必合）；CHANGELOG 触碰提交作锚一提交即空转。三者负控制见 tests。
_LANDED_MERGE_RE = re.compile(r"Merge branch 'task/([a-z0-9-]+)'")


_LANDED_IMPL_RE = re.compile(r"^task-([a-z0-9-]+):")


_LANDED_PAREN_RE = re.compile(r"[（(](task-[a-z0-9-]+)[）)]")


def landed_task_ids_since(project_root: Path, ref: str) -> list[str] | None:
    """取 ``ref..HEAD`` first-parent 历史中的落地任务 id（保序去重）。

    落地 = 分支已合入主干：merge 提交（``Merge branch 'task/<id>'``）、实现提交
    （``task-<slug>:`` 前缀，ff 合并的唯一痕迹）、括号引用（回填行内任务号）。
    未合分支（只存在、未合入）天然不在范围内。``ref`` 为空串时读全历史。

    Returns:
        任务 id 列表；git 不可用 / 非仓库 / ref 非法返回 None（调用方定
        fail-closed 还是 skip——门禁侧判红，测试侧 skip）。
    """
    try:
        result = _gitops_pkg._run_git(
            project_root, ["log", "--first-parent", "--format=%s", f"{ref}..HEAD"])
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    landed: list[str] = []
    for line in (result.stdout or "").splitlines():
        found: set[str] = set()
        m = _LANDED_MERGE_RE.search(line)
        if m:
            found.add(m.group(1))
        m = _LANDED_IMPL_RE.match(line)
        if m:
            found.add("task-" + m.group(1))
        found.update(_LANDED_PAREN_RE.findall(line))
        for tid in found:
            if tid not in landed:
                landed.append(tid)
    return landed


def previous_release_tag(project_root: Path,
                         exclude: tuple[str, ...] = ()) -> str | None:
    """按创建时间倒序取最新 tag（剔除 ``exclude``，典型为目标版本自身）。

    发版门⑧与漂移测试共用同一锚：无 tag（首版或浅克隆无 tag）返回 None，
    调用方显式不适用——不得回落 CHANGELOG 触碰提交（该锚一提交即空转）。

    排序：creatordate 倒序，同秒并列时按版本 refname 倒序兜底（脚本化连续打
    tag 同秒是常态；无次级排序则并列顺序不定，上一锚会漂）。
    """
    try:
        result = _gitops_pkg._run_git(
            project_root, ["tag", "--list", "--sort=-creatordate",
                           "--sort=-v:refname"])
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    excluded = set(exclude)
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if name and name not in excluded:
            return name
    return None
