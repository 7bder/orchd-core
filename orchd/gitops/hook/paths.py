"""gitops hook 域：pre-commit / reference-transaction hook 的路径、模板与安装。

域：paths（W6-C8 从 orchd/gitops/hook.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
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



# ---- typed hooks 目录探测（task-engine-defect-fix-unified-u11-hook-path-failclosed）
#
# 旧实现把「git 探测失败」与「core.hooksPath 未配置」折叠成同一个回退值
# ``.git/hooks``：探测失败（PermissionError / 超时 / 非仓库）时 installer 会往错误
# 目录写、卸载会删错文件、residue 把「没查成」当成「hook 缺失」报红（探测缺失本身
# 就是 fail-open）。故把**判定**与**回退**拆开：判定失败即 ``git_unavailable``，
# 由调用方 fail-closed 处置（拒绝写/删并显式报错），绝不静默回退。
_HOOKS_DIR_OK = "ok"
_HOOKS_DIR_NOT_CONFIGURED = "not_configured"
_HOOKS_DIR_GIT_UNAVAILABLE = "git_unavailable"



def _get_hooks_dir(project_root: Path, probe: dict[str, Any] | None = None) -> Path:
    """解析 hooks 目录：git config core.hooksPath，缺省回退 .git/hooks。

    本函数是 hook 路径域**唯一**的直接 git 探测点（判定与回退同址）：``probe`` 非空
    时回填 typed 判定结果，供门禁 / 清理 / 检测路径 fail-closed 处置：

    - ``ok``：git 正常回答且已配置 → 返回解析后的目录（相对值按仓库根解析、
      绝对值原样，语义同 git-config 文档）；
    - ``not_configured``：git 正常回答「未设置 / 空值」→ 返回 ``<repo>/.git/hooks``
      （此时「该位置没有 hook」是**真相**，可以据此告警）；
    - ``git_unavailable``：git 无法回答（可执行缺失 / 权限拒绝 / 超时 / 非仓库 /
      rc 非 0/1）→ 返回 ``<repo>/.git/hooks`` 仅为兼容旧调用面的回退值；
      调用方**必须**看 ``probe["status"]`` 而非返回值，门禁与清理路径禁止直接消费
      该回退值，应改用 :func:`_resolve_hooks_dir_checked`。

    Args:
        project_root: 仓库根目录。
        probe: 可选出参字典，回填 ``{"status", "hooks_dir", "raw", "source",
            "error"}``（``raw`` 仅 ``ok`` 时非空）。

    Returns:
        hooks 目录（``git_unavailable`` 时为兼容回退值，见上）。
    """
    root = Path(project_root)
    detail: dict[str, Any] = probe if probe is not None else {}
    default = root / ".git" / "hooks"
    try:
        proc = _run_git(root, ["config", "--get", "core.hooksPath"])
    except Exception as exc:  # PermissionError / TimeoutExpired / FileNotFoundError ...
        detail.update({
            "status": _HOOKS_DIR_GIT_UNAVAILABLE, "hooks_dir": None, "raw": None,
            "source": None, "error": f"{type(exc).__name__}: {exc}",
        })
        return default
    rc = getattr(proc, "returncode", None)
    if rc == 0:
        value = (getattr(proc, "stdout", "") or "").strip()
        if value:
            resolved = Path(value) if _is_absolute_hooks_path(value) else root / value
            detail.update({
                "status": _HOOKS_DIR_OK, "hooks_dir": str(resolved),
                "raw": value, "source": "config", "error": None,
            })
            return resolved
    if rc in (0, 1):  # rc==1：git config --get 键不存在（git 明确回答「未配置」）
        detail.update({
            "status": _HOOKS_DIR_NOT_CONFIGURED, "hooks_dir": str(default),
            "raw": None, "source": "default", "error": None,
        })
        return default
    stderr = (getattr(proc, "stderr", "") or "").strip()
    detail.update({
        "status": _HOOKS_DIR_GIT_UNAVAILABLE, "hooks_dir": None, "raw": None,
        "source": None,
        "error": stderr or f"git config --get core.hooksPath rc={rc}",
    })
    return default


def _probe_hooks_dir(project_root: Path) -> dict[str, Any]:
    """typed 探测 hooks 目录语义（纯包装 :func:`_get_hooks_dir` 的判定面）。

    三态语义见 :func:`_get_hooks_dir`；本函数自身不含外部进程调用，只把判定结果
    以字典返回，供 :func:`_resolve_hooks_dir_checked` 与测试消费。

    Args:
        project_root: 仓库根目录。

    Returns:
        ``{"status": str, "hooks_dir": str|None, "raw": str|None,
        "source": "config"|"default"|None, "error": str|None}``。
    """
    detail: dict[str, Any] = {}
    _get_hooks_dir(project_root, detail)
    return detail


def _git_unavailable_result(action: str, detail: dict[str, Any]) -> dict[str, Any]:
    """构造 fail-closed 结构化结果（git 无法回答 hooks 目录时的唯一出口）。

    语义恒为**不写、不删、不回退**：安装类调用点取 ``installed=False``，卸载类取
    ``uninstalled=False``（两键都给，避免各调用点自造措辞漂移）。``reason`` 固定
    ``git_unavailable``，与 ``config_failed`` / ``io_error`` 等既有 reason 区分，
    便于测试与 doctor 断言「这是探测失败，不是配置失败」。

    Args:
        action: 调用点动作名（install / install_ref_tx / uninstall / uninstall_ref_tx）。
        detail: :func:`_probe_hooks_dir` 或 ``_ensure_hooks_path`` 的结构化失败。

    Returns:
        结构化结果（含 ``error`` / ``hint`` / ``probe``）。
    """
    error = str(detail.get("error") or "git 无法回答 core.hooksPath").strip()
    return {
        "installed": False,
        "uninstalled": False,
        "reason": _HOOKS_DIR_GIT_UNAVAILABLE,
        "action": action,
        "error": error,
        "hint": (
            "git 探测 hooks 目录失败（权限拒绝 / 超时 / git 可执行缺失 / 非仓库）："
            "已 fail-closed 拒绝本次操作（不写、不删、不回退 .git/hooks）；"
            "排查 git 可用性后重跑，或手动执行 "
            "git config core.hooksPath <绝对路径> 再试"
        ),
        "probe": detail,
    }


def _resolve_hooks_dir_checked(
    project_root: Path, action: str = "probe",
) -> tuple[Path | None, dict[str, Any] | None]:
    """探测 hooks 目录：成功 ``(dir, None)``；git 不可用 ``(None, 结构化 fail-closed 结果)``。

    门禁 / 清理路径（installer、uninstaller、residue 检测器）**必须**走本函数，
    不得消费 :func:`_get_hooks_dir` 的兼容回退值——「没查成」与「未配置」的处置完全
    不同（前者禁止写删，后者可据真相告警）。
    """
    probe = _probe_hooks_dir(project_root)
    hooks_dir = probe.get("hooks_dir")
    if probe.get("status") == _HOOKS_DIR_GIT_UNAVAILABLE or not hooks_dir:
        return None, _git_unavailable_result(action, probe)
    return Path(str(hooks_dir)), None


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
    """写入 ``core.hooksPath``；成功返回 ``None``，失败返回结构化报错。

    git 不可用（权限拒绝 / 超时 / 可执行缺失）一律结构化返回、不抛异常：调用方
    据此 fail-closed（``reason="git_unavailable"``），不得把「没写成」当成「已配置」
    继续往下装 hook——否则 hook 会落到未生效目录，门禁静默失效。
    """
    try:
        proc = _run_git(root, ["config", "core.hooksPath", value])
    except Exception as exc:  # PermissionError / TimeoutExpired / FileNotFoundError ...
        return {"configured": False, "reason": _HOOKS_DIR_GIT_UNAVAILABLE,
                "error": f"{type(exc).__name__}: {exc}",
                "hint": "git 无法写入仓库配置（权限/超时/可执行缺失），原配置保持不变"}
    if proc.returncode != 0:
        return {"configured": False, "reason": "config_failed",
                "error": (getattr(proc, "stderr", "") or "").strip()}
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


_HOOK_LOCK_FILENAME = "hook.lock"
_HOOK_DEGRADED_FILENAME = "hook_runtime_degraded.json"
_HOOK_RUNTIME_TIMEOUT_S = 10.0


def _hook_runtime_root(project_root: Path) -> Path:
    """hook 运行时目录：与账本根同源（container → .orchd-runtime）。"""
    from orchd.ledger import resolve_store_dir

    return resolve_store_dir(Path(project_root) / ".orchd")


def hook_lock_path(project_root: Path) -> Path:
    """安装/卸载共用的排他锁路径（``.orchd-runtime/hook.lock``）。"""
    return _hook_runtime_root(project_root) / _HOOK_LOCK_FILENAME


def hook_runtime_degraded_path(project_root: Path) -> Path:
    """degraded 认领记录路径（claim 放行时写入，doctor/status 据此标红）。"""
    return _hook_runtime_root(project_root) / _HOOK_DEGRADED_FILENAME


def record_hook_runtime_degraded(
    project_root: Path, task_id: str, probe: dict[str, Any],
) -> dict[str, Any]:
    """在 ``ORCHD_ALLOW_HOOK_DEGRADED=1`` 放行时落 degraded 记录（best-effort）。"""
    record = {
        "v": 1,
        "task_id": task_id,
        "shell": probe.get("shell"),
        "error": probe.get("error", ""),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from orchd.ledger import _write_text_atomic

        _write_text_atomic(
            hook_runtime_degraded_path(project_root),
            json.dumps(record, ensure_ascii=False) + "\n",
        )
    except (OSError, ValueError):
        pass
    return record


def read_hook_runtime_degraded(project_root: Path) -> dict[str, Any] | None:
    """读取 degraded 记录；缺失 / 损坏返回 None。"""
    try:
        data = json.loads(
            hook_runtime_degraded_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_hook_runtime_degraded(project_root: Path) -> None:
    """hook 安装成功后清除 degraded 记录（best-effort）。"""
    try:
        hook_runtime_degraded_path(project_root).unlink(missing_ok=True)
    except OSError:
        pass


def _acquire_hook_lock(project_root: Path) -> tuple[Any, dict[str, Any] | None]:
    """获取安装/卸载共用的排他锁；失败返回结构化结果（不抛异常）。"""
    # 经 ledger 取锁与错误类型：install 已登记 ledger 延迟边，避免新增依赖边。
    from orchd.ledger import ExclusiveFileLock, OrchdError

    lock = ExclusiveFileLock(hook_lock_path(project_root))
    try:
        lock.acquire(blocking=True, timeout_s=_HOOK_RUNTIME_TIMEOUT_S)
    except OrchdError as exc:
        return None, {
            "installed": False, "uninstalled": False,
            "reason": "hook_lock_unavailable",
            "error": exc.message,
            "hint": (
                "hook 安装/卸载共用 .orchd-runtime/hook.lock 的 ExclusiveFileLock："
                "等待超时或锁被占用，稍后重试"
            ),
        }
    except OSError as exc:
        return None, {
            "installed": False, "uninstalled": False,
            "reason": "hook_lock_unavailable",
            "error": str(exc),
            "hint": "hook 安装/卸载共用 .orchd-runtime/hook.lock：锁文件不可用，检查运行时目录权限",
        }
    return lock, None


def _engine_source_root() -> Path:
    """引擎源码根（仓库根）：**与实现文件深度无关**的推导。

    W6-C8 修正：此处原本是 `_precommit_hook_params` 体内的
    ``Path(__file__).resolve().parent.parent.parent`` —— 它暗含「本文件在
    ``orchd/gitops/<file>.py``」这一深度假设。拆包后本文件变成
    ``orchd/gitops/hook/content.py``，同一表达式少算一层、把引擎根算成 ``<repo>/orchd``，
    于是 hook 运行期 ``import orchd`` 失败（``No module named 'orchd'``）、ledger 回退查询
    失效，实测把 DONE 任务误判为活跃并拦提交（E020）。改用 ``orchd`` 包自身的 ``__file__``
    （``<repo>/orchd/__init__.py`` 上两级 = 仓库根），函数内导入避免顶层循环。
    """
    from orchd import __file__ as _orchd_file

    return Path(_orchd_file).resolve().parent.parent
