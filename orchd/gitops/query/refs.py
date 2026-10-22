"""git ref 只读查询（当前分支 / HEAD / upstream / 默认分支 / 分支存在性）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import shutil
import subprocess

import orchd.gitops as _gitops_pkg
from .cache import _PROBE_CACHE, _probe_cache_key


def get_current_branch(project_root: Path) -> str | None:
    """获取当前 git 分支名。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。

    按次缓存：同键（同目录 + 同 HEAD 快照）重复调用零 spawn。
    确定性失败（git 正常返回非零退出码，如非仓库）同样缓存——同键下答案
    不变；抛异常路径（超时/IO，瞬时故障）永不缓存，保证恢复力。
    """
    key = _probe_cache_key(project_root)
    ckey = ("branch", key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        result = _gitops_pkg._run_git(project_root, ["branch", "--show-current"])
        value = result.stdout.strip() or None if result.returncode == 0 else None
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def get_head_commit(project_root: Path) -> str | None:
    """获取当前 HEAD 的 commit SHA（用于 review baseline 追踪）。

    非 git 仓库、git 不可用或任何异常返回 None（best-effort 降级）。

    按次缓存：同键（同目录 + 同 HEAD 快照）重复调用零 spawn。
    确定性失败（git 正常返回非零退出码）同样缓存；抛异常路径
    （超时/IO，瞬时故障）永不缓存，保证恢复力。
    """
    key = _probe_cache_key(project_root)
    ckey = ("head_commit", key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        result = _gitops_pkg._run_git(project_root, ["rev-parse", "HEAD"])
        if result.returncode == 0:
            value = result.stdout.strip() or None
        else:
            value = None
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def upstream_state(project_root: Path) -> dict[str, Any]:
    """探测「HEAD 是否已推送到上游」的**单一真源**（里程碑 F1 与发版门禁共用）。

    卡 A（task-ci-evidence-authority）：本地自证绿不足以支撑里程碑/发版——证据必须来自
    远端已验证的历史。本函数是该判定的唯一探测入口：``orchd.milestone`` 的 F1 与
    ``scripts/check_release_ready.py`` 的 check_upstream 都调它，避免两套 rev-list 口径漂移。

    Returns:
        结构化结果，永不抛异常（``state`` 取值及其含义）：
            - ``pushed``：有上游且 ``@{u}..HEAD`` 为空；
            - ``ahead``：``ahead`` 条提交未推送；
            - ``no_upstream``：分支存在但未设置上游（无法证明已推送）；
            - ``detached``：HEAD 为 detached（CI 按 tag 检出等，无分支上游可比对）；
            - ``error``：git 不可用或探测失败（调用方须 fail-closed，不得当作通过）。
    """
    try:
        if shutil.which("git") is None:
            return {"state": "error", "upstream": None, "ahead": None,
                    "branch": None, "detail": "git 不可用"}
        branch = _gitops_pkg._run_git(
            project_root, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        name = (branch.stdout or "").strip() if branch.returncode == 0 else ""
        if branch.returncode != 0 or not name:
            return {"state": "detached", "upstream": None, "ahead": None,
                    "branch": None, "detail": "HEAD 为 detached，无分支上游可比对"}
        up = _gitops_pkg._run_git(
            project_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        upstream_ref = (up.stdout or "").strip()
        if up.returncode != 0 or not upstream_ref or upstream_ref == "@{u}":
            return {"state": "no_upstream", "upstream": None, "ahead": None,
                    "branch": name, "detail": f"分支 {name} 未设置上游"}
        ahead = _gitops_pkg._run_git(project_root, ["rev-list", "--count", "@{u}..HEAD"])
        if ahead.returncode != 0:
            reason = (ahead.stderr or "").strip()[:60] or "git rev-list 失败"
            return {"state": "error", "upstream": upstream_ref, "ahead": None,
                    "branch": name, "detail": reason}
        try:
            n = int((ahead.stdout or "").strip())
        except ValueError:
            return {"state": "error", "upstream": upstream_ref, "ahead": None,
                    "branch": name, "detail": "上游未推送计数无法解析"}
        if n:
            return {"state": "ahead", "upstream": upstream_ref, "ahead": n,
                    "branch": name,
                    "detail": f"{n} 条提交未推送到上游 {upstream_ref}"}
        return {"state": "pushed", "upstream": upstream_ref, "ahead": 0,
                "branch": name, "detail": f"已推送（上游 {upstream_ref}）"}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"state": "error", "upstream": None, "ahead": None,
                "branch": None, "detail": f"{type(exc).__name__}: {exc}"}


def get_default_branch(project_root: Path) -> str | None:
    """检测**本仓库**的默认分支名（best-effort）。

    优先级：
    1. ``git config init.defaultBranch``（用户显式配置）
    2. 本地存在 ``main`` 分支
    3. 本地存在 ``master`` 分支
    4. 都没有返回 None

    非 git 仓库、git 不可用或任何异常返回 None。

    按次缓存：同键（同目录 + 同 HEAD 快照）重复调用零新增 spawn。
    确定性失败（非工作树、配置缺失、名单为空）同样缓存——同键下答案
    不变；抛异常路径（超时/IO，瞬时故障）永不缓存，保证恢复力。

    Note:
        必须先判定 ``project_root`` 是否为 git 工作树，再读 ``init.defaultBranch``。
        ``git config --get`` 会一路回溯到**系统 / 全局**配置（Git for Windows 的
        ``etc/gitconfig`` 预设 ``init.defaultbranch=main``），在**任意目录**——
        包括非 git 目录——都返回非 None。若据此认定"存在默认分支"，下游门禁
        会把"非 git 环境（不适用）"误判成"git 探测故障"而错误 fail-closed
        （2026-08-29 实踩：引擎经 Git Bash 执行 verify 时命中该组合）。

    缓存边界：同键复用整次判定的终值；``init.defaultBranch`` 这类仓库外
    配置在同键下变更不会触发 miss（实践中进程内静态，测试以 fake 隔离）。
    """
    key = _probe_cache_key(project_root)
    ckey = ("default_branch", key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        # 0. 非 git 工作树 → 无"本仓库默认分支"概念，直接返回 None
        inside = _gitops_pkg._run_git(project_root, ["rev-parse", "--is-inside-work-tree"])
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            value = None
        else:
            # 1. 显式配置优先
            cfg = _gitops_pkg._run_git(project_root, ["config", "--get", "init.defaultBranch"])
            if cfg.returncode == 0 and cfg.stdout.strip():
                value = cfg.stdout.strip()
            else:
                # 2. 本地常见默认分支名单次调用判定（task-gitops-probe-cache 压扁）：
                #    `git branch --list main master` 一次返回存在性，替代逐个
                #    `rev-parse --verify`（省 1 spawn；语义与文档"本地存在 main/master
                #    分支"逐字一致——且比 --verify 更严格：后者连同名 tag 也命中）。
                names = _gitops_pkg._run_git(
                    project_root, ["branch", "--list", "main", "master", "--format=%(refname:short)"])
                if names.returncode == 0:
                    have = set((names.stdout or "").split())
                    if "main" in have:
                        value = "main"
                    elif "master" in have:
                        value = "master"
                    else:
                        value = None
                else:
                    value = None
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


def branch_exists(project_root: Path, branch: str) -> bool | None:
    """判定分支是否存在（三态，供门禁的"环境不适用"判定使用）。

    Returns:
        - ``True`` 分支存在；
        - ``False`` 分支不存在（前提不成立 → 门禁不适用，允许降级 + 留痕）；
        - ``None`` git 探测故障（超时 / IO）→ 调用方必须 fail-closed，
          不得当作"不存在"降级放行。

    Note:
        与 :func:`check_workspace_state` 同构：把「不适用」与「故障」分开，
        避免用 ``returncode != 0`` 一次性吞掉两种语义。

    按次缓存：键含分支名（同目录 + 同 HEAD 快照 + 同分支名）重复调用
    零新增 spawn。只缓存确定性结论（存在 True / 不存在 False）；其他
    退出码与抛异常路径（超时/IO）永不缓存，调用方 fail-closed 语义不变。
    """
    key = _probe_cache_key(project_root)
    ckey = ("branch_exists", branch, key) if key is not None else None
    if ckey is not None and ckey in _PROBE_CACHE:
        return _PROBE_CACHE[ckey]
    try:
        proc = _gitops_pkg._run_git(project_root, ["rev-parse", "--verify", "--quiet", branch])
        if proc.returncode == 0:
            value = True
        elif proc.returncode == 1:
            value = False
        else:
            return None
        if ckey is not None:
            _PROBE_CACHE[ckey] = value
        return value
    except (subprocess.SubprocessError, OSError):
        return None
