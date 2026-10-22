"""lifecycle core baseline 域 — 基线 ref 解析与「基线既有」谓词。

来源：``orchd/onboard/lifecycle/core.py（1336 行）``（W6-C6 纯搬移）。

形态 A（全局共享文件）判定的基线来源：git 模式走 ``git ls-tree``，无 git 单目录
模式走 claim 时建立的 base 快照（``_nogit_manifest_probe``）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchd.gitops import GUARD_STATUS_NOT_APPLICABLE, record_degraded_guard
from orchd.shared_entries import BaselineTestProbe


# 形态 A 的基线 ref 候选（按序解析）：远端默认分支优先，其后是常见本地默认分支名。
# **不硬编码 main**——宿主默认分支为 master（或改名 / 浅克隆无 main）时，硬编码会让形态 A
# 静默失效且无痕（task-e039-escape-hatch-bypass-fix，pass6 Q-2；违反 L-12「不可用即结构化
# 上报」）。
_BASELINE_REF_CANDIDATES = ("main", "master")


def _resolve_baseline_ref(project_root: Path) -> str | None:
    """解析形态 A 的基线 ref（``origin/HEAD`` → ``main`` → ``master``），全失败返回 ``None``。

    ``git symbolic-ref refs/remotes/origin/HEAD`` 指向远端默认分支（形如
    ``refs/remotes/origin/main``），是「本任务从哪条线分出」最权威的线索；远端信息缺失时
    回落本地 ``main`` / ``master``。候选须能 ``rev-parse`` 到 commit 才算命中。
    """
    candidates: list[str] = []
    try:
        head = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if head.returncode == 0 and head.stdout.strip():
            ref = head.stdout.strip()
            if ref.startswith("refs/remotes/"):
                candidates.append(ref[len("refs/remotes/"):])
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.extend(_BASELINE_REF_CANDIDATES)
    for candidate in candidates:
        try:
            probe = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            return candidate
    return None


def _uncommitted_changes(project_root: Path) -> list[str]:
    """任务工作树内已跟踪文件的未提交改动（best-effort，取不到返回空列表）。

    用于共享入口门禁的「实际改动」清单——引擎 auto-commit 发生在 done 靠后阶段，故
    done 判定时刻工作树可能带未提交改动。失败按空列表处理：调用方的 fail-open 口径
    由 :func:`_guard_shared_entry_coverage` 的 docstring 说明（兜底=越界/声明门禁）。
    """
    try:
        from orchd.gitops import list_tracked_changes

        return list(list_tracked_changes(project_root) or [])
    except Exception:
        return []


def _nogit_manifest_probe(
    project_root: Path,
    task_id: str | None,
    degraded_guards: list[dict[str, Any]] | None,
) -> BaselineTestProbe | None:
    """无 git 等价：以 **claim 时建立的主目录基线快照**判定「基线既有」（task-nogit-guard-parity）。

    形态 A 的基线在 git 模式下由 ``git ls-tree <基线 ref>`` 判定；无 git 单目录模式下
    分支/ref 不存在，此前整体降级为「基线 ref 不可解析」⇒ 形态 A 门禁在无 git 下**完全
    不生效**。等价实现：claim 已对主目录取基线快照（``base`` manifest，见
    ``orchd.nogit.take_snapshot``），「基线既有」⇔ 该路径出现在 base manifest 中。

    Returns:
        - 非无 git orchd 项目 / 无 ``task_id`` → ``None``（调用方走既有降级）；-
        - 有基线快照 → 谓词（manifest 命中谓 True，否则 False）；
        - 无基线快照（claim 未建立）→ 恒 ``None`` 谓词**并留痕**（reason 与 git 侧的
          「基线 ref 不可解析」可区分，便于定位是快照缺失还是 ref 缺失）。
    """
    if not task_id:
        return None
    from orchd.nogit import is_nogit_project, read_manifest, snapshot_dir

    if not is_nogit_project(project_root):
        return None
    base = read_manifest(snapshot_dir(Path(project_root), task_id, "base"))
    if base is None:
        record_degraded_guard(
            degraded_guards,
            guard_name="shared_entry_coverage",
            status=GUARD_STATUS_NOT_APPLICABLE,
            reason="无 git 且无 claim 基线快照（main-base manifest 缺失）：形态 A 基线不可判定",
            context={
                "guard_rule": "global_shared_file",
                "task_id": task_id,
                "project_root": str(project_root),
            },
            hint=(
                "无 git 单目录模式的形态 A 基线来自 claim 时建立的主目录快照；"
                "本任务缺少 base 快照 ⇒ 覆盖判定被跳过。请重跑 claim 建立基线后重试 done"
            ),
        )
        return lambda path: None
    baseline_paths = {str(p).replace("\\", "/") for p in base}
    return lambda path: str(path).replace("\\", "/") in baseline_paths


def _baseline_test_predicate(
    project_root: Path, degraded_guards: list[dict[str, Any]] | None = None
) -> BaselineTestProbe:
    """构造「文件在基线分支中已存在」的判定谓词（形态 A 用）。

    用 ``git ls-tree --name-only <基线 ref> -- <path>`` 的**输出**而非退出码判定：输出非空 ⇒
    基线既有；输出空 ⇒ 非基线（本任务新建，退出码仍为 0）；rc≠0（非 git 仓库 / ref 不可
    解析 / git 不可用）⇒ ``None``（不可判定，调用方 fail-open 跳过）。

    兼容「修改既有测试文件」：只要该文件在基线上存在即计为基线既有——形态 A 排除的只有
    「本任务新建的文件」，不是「本任务改过的文件」。

    **无 git 等价（task-nogit-guard-parity）**：无 git orchd 项目的基线由
    :func:`_nogit_manifest_probe` 提供（claim 时主目录基线快照），调用方
    （:func:`_guard_shared_entry_coverage`）在其返回非 ``None`` 时优先采用，故本函数
    **签名与调用口径零变化**（既有 monkeypatch 用例不受影响）。

    基线 ref 全不可解析（``origin/HEAD`` / ``main`` / ``master`` 均失败）时**留痕不静默**
    （task-e039-escape-hatch-bypass-fix，pass6 Q-2）：记 ``degraded_guards``（E030 /
    not_applicable）后返回恒 ``None`` 的谓词（调用方按既有 fail-open 口径跳过）。
    """
    ref = _resolve_baseline_ref(project_root)
    if ref is None:
        record_degraded_guard(
            degraded_guards,
            guard_name="shared_entry_coverage",
            status=GUARD_STATUS_NOT_APPLICABLE,
            reason="形态 A 基线 ref 不可解析（origin/HEAD / main / master 均失败）",
            context={"guard_rule": "global_shared_file", "project_root": str(project_root)},
            hint="形态 A 覆盖判定被跳过；确认仓库默认分支名或补 fetch 远端引用后重试 done",
        )
        return lambda path: None

    def is_baseline(path: str) -> bool | None:
        try:
            proc = subprocess.run(
                ["git", "ls-tree", "--name-only", ref, "--", path],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return bool(proc.stdout.strip())

    return is_baseline
