"""gitops hook 域：pre-commit / reference-transaction hook 的路径、模板与安装。

域：paths（W6-C8 从 orchd/gitops/hook.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from orchd.gitops._run import _run_git



_HOOK_FILENAME = "pre-commit"

# 强制层 hook（task-ref-tx-hook）：git reference-transaction，红线 #1/#2 的**强制**拦截
# ——代理（orchd git <args>）为便捷层，被绕过时由本 hook 在引用事务边界兜底。
_REF_TX_HOOK_FILENAME = "reference-transaction"

# 仓库自带 hooks 目录（随 clone 检出，内含 pre-push 发版同步保护等仓库级 hook）。
# 只有 core.hooksPath 指向它才会生效——该配置是**本地仓库配置**，不随 clone 传播。
_REPO_HOOKS_DIR = ".githooks"



def _is_absolute_hooks_path(value: str) -> bool:
    """判断 core.hooksPath 配置值是否为绝对路径（跨平台）。

    POSIX 上无法用 Path.is_absolute() 识别 Windows 盘符形态（C:/...、C:\\...），
    单独用盘符正则兜底；保证在任意平台解析 hooksPath 均一致。
    """
    return Path(value).is_absolute() or bool(re.match(r"^[A-Za-z]:[/\\]", value))



def _get_hooks_dir(project_root: Path) -> Path:
    """解析实际 hooks 目录：git config core.hooksPath，缺省回退 .git/hooks。

    core.hooksPath 语义（git-config 文档）：
    - 未设置 / 设置为空 → 默认 .git/hooks
    - 相对路径 → 相对仓库根目录
    - 绝对路径 → 原样使用
    git 不可用或 config 读取失败 → 保守回退 .git/hooks（与旧版行为一致）。
    """
    try:
        proc = _run_git(project_root, ["config", "--get", "core.hooksPath"])
    except Exception:
        return project_root / ".git" / "hooks"
    if proc.returncode != 0:
        return project_root / ".git" / "hooks"
    value = proc.stdout.strip()
    if not value:
        return project_root / ".git" / "hooks"
    hooks = Path(value)
    if _is_absolute_hooks_path(value):
        return hooks
    return project_root / hooks



def _get_hook_path(project_root: Path) -> Path:
    """返回实际 hooks 目录下的 pre-commit 路径（适配 core.hooksPath）。"""
    return _get_hooks_dir(project_root) / _HOOK_FILENAME



def _read_hooks_path(project_root: Path) -> str | None:
    """读取 core.hooksPath 原始配置值（未设置 / 空 / git 不可用 → None）。"""
    try:
        proc = _run_git(project_root, ["config", "--get", "core.hooksPath"])
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None



def _is_same_hooks_dir(project_root: Path, value: str, target: Path) -> bool:
    """core.hooksPath 配置值是否与目标目录等价。

    解析规则与 :func:`_get_hooks_dir` 一致（相对 → 相对仓库根；绝对 → 原样），
    比较用 ``resolve()`` 抹平分隔符与 ``..`` 差异。
    """
    resolved = Path(value) if _is_absolute_hooks_path(value) else project_root / value
    try:
        return resolved.resolve() == target.resolve()
    except OSError:
        return False



def _absolute_hooks_value(target: Path) -> str:
    """把仓库自带 hooks 目录渲染为 ``core.hooksPath`` 的**绝对**配置值。

    git 对相对 ``core.hooksPath`` 按**当前工作树顶层**解析：``git worktree add`` 出的
    链接工作树里没有 ``.githooks/`` 目录 ⇒ git 找不到 hook 文件时**静默跳过**
    （fail-open），E020 提交范围门禁与 reference-transaction 强制层在任务工作树内
    双双失效。绝对值对所有工作树一致可达，故出厂一律写绝对值（``as_posix()``，
    不带引号）。
    """
    return target.resolve().as_posix()



def _write_hooks_path(root: Path, value: str) -> dict[str, Any] | None:
    """写入 ``core.hooksPath``；成功返回 ``None``，失败返回结构化报错。"""
    proc = _run_git(root, ["config", "core.hooksPath", value])
    if proc.returncode != 0:
        return {"configured": False, "reason": "config_failed",
                "error": proc.stderr.strip()}
    return None



def _ensure_hooks_path(
    project_root: Path,
    hooks_dir: str = _REPO_HOOKS_DIR,
) -> dict[str, Any]:
    """确保仓库自带 hooks 目录被 git 启用（core.hooksPath），幂等且不覆盖用户配置。

    task-decl-hooks-autoset：``core.hooksPath`` 是**本地仓库配置**，不随
    ``git clone`` 传播——新 clone 检出后仓库内 ``.githooks/``（如 pre-push 发版
    同步保护）不会生效，质量门禁形同虚设。安装流程与本模块的 hook 安装期各调用
    一次，把它自动打开。

    写入值必须是**绝对路径**（task-worktree-hooks-path-reachability）：git 把相对
    ``core.hooksPath`` 按当前工作树顶层解析，链接工作树内没有该相对目录时 git 静默
    不执行 hook。实测（git 2.55，同一仓库同一 hook 内容）：值 = ``.githooks``（相对）
    时主工作树提交触发 hook、``git worktree add`` 出的链接工作树提交**不触发**；
    值 = ``<repo>/.githooks``（绝对）时两者都触发。任务工作树正是本引擎 L3 强制层
    的主战场，故相对值形态等于「交付一套只在主工作树生效的门禁」。

    决策表（永不抛异常）：

    - 非 git 仓库 → ``reason="not_a_git_repo"``，不动；
    - 仓库内无 ``hooks_dir`` 目录 → ``reason="hooks_dir_missing"``，不动（此时
      hook 落 ``.git/hooks``，该目录全工作树共享，天然可达）；
    - ``core.hooksPath`` 未设置 / 空 → 设为**绝对**目标，``reason="set"``；
    - 已指向同一目录且为**绝对**值 → 幂等不写，``reason="already_set"``；
    - 已指向同一目录但为**相对**值 → 升级为绝对值，
      ``reason="upgraded_to_absolute"``（``configured=True``）；
    - 已为绝对值但目标目录不存在，而其末级目录名就是 ``hooks_dir``（仓库被移动/
      改名后强制层静默死的形态）→ 改写为当前绝对目标，``reason="reinstalled_stale"``；
    - 指向其他路径（用户显式自定义，如 ``.husky``）→ **不改写**，
      ``reason="custom_hooks_path"`` 并附 ``hint`` 提示手动改法。

    本函数只新增"写配置"动作；:func:`_get_hooks_dir` 的既有解析语义（相对/绝对/
    缺省回退 ``.git/hooks``）不变。

    Returns:
        ``{"configured": bool, "reason": str, ...}``；``configured=True`` 仅表示
        本次真的写入了配置。
    """
    root = Path(project_root)
    if not (root / ".git").exists():
        return {"configured": False, "reason": "not_a_git_repo"}
    target = root / hooks_dir
    if not target.is_dir():
        return {"configured": False, "reason": "hooks_dir_missing",
                "hooks_dir": hooks_dir}
    current = _read_hooks_path(root)
    absolute_target = _absolute_hooks_value(target)
    if current is None:
        failure = _write_hooks_path(root, absolute_target)
        if failure is not None:
            return failure
        return {"configured": True, "reason": "set", "hooks_path": absolute_target}
    if _is_same_hooks_dir(root, current, target):
        if _is_absolute_hooks_path(current):
            return {"configured": False, "reason": "already_set", "hooks_path": current}
        # 相对等价值：目录对，可达性错——升级为绝对值（不改指向、只改形态）
        failure = _write_hooks_path(root, absolute_target)
        if failure is not None:
            return failure
        return {
            "configured": True,
            "reason": "upgraded_to_absolute",
            "hooks_path": absolute_target,
            "previous": current,
        }
    if (
        _is_absolute_hooks_path(current)
        and not Path(current).is_dir()
        and Path(current).name == hooks_dir
    ):
        # 陈旧绝对值：仓库被移动或改名后原路径已不存在，而末级目录名仍是仓库自带
        # hooks 目录名 → 属于「曾经指向本仓库、现已失效」，重写为当前绝对目标。
        # 末级名不匹配（.husky 等宿主目录）一律走 custom 分支，不改写。
        failure = _write_hooks_path(root, absolute_target)
        if failure is not None:
            return failure
        return {
            "configured": True,
            "reason": "reinstalled_stale",
            "hooks_path": absolute_target,
            "previous": current,
        }
    return {
        "configured": False,
        "reason": "custom_hooks_path",
        "hooks_path": current,
        "expected": absolute_target,
        "hint": (
            f"core.hooksPath 已自定义为 {current}（非仓库自带 {hooks_dir}）：保持不动、"
            f"未改写；如需改为仓库自带 hooks，执行 git config core.hooksPath"
            f" {absolute_target}"
        ),
    }



def _get_ref_tx_hook_path(project_root: Path) -> Path:
    """reference-transaction hook 路径（与 pre-commit 同 hooks 目录、同 hooksPath 语义）。"""
    return _get_hooks_dir(project_root) / _REF_TX_HOOK_FILENAME



def _resolve_ledger_path(project_root: Path) -> str:
    """共享账本根的 ledger 路径（与 :func:`orchd.ledger.resolve_store_dir` 同源）。

    container → ``<容器>/.orchd-runtime/_ledger.jsonl``；flat → ``<repo>/.orchd/_ledger.jsonl``；
    ``ORCHD_HOME`` 重定向由 resolve_store_dir 处理。解析失败回退相对默认值
    （best-effort，运行时另有 ORCHD_HOME / 相对值兜底）。
    """
    try:
        from orchd.ledger import resolve_store_dir

        root = resolve_store_dir(Path(project_root) / ".orchd")
        return str(Path(root) / "_ledger.jsonl").replace("\\", "/")
    except Exception:  # noqa: BLE001 - 解析失败退回相对默认（运行时兜底）
        return ".orchd/_ledger.jsonl"
