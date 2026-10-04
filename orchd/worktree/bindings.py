"""任务 worktree 绑定（task-14-worktree-lifecycle）：建后绑/用/守卫。

依赖方向：同包 layout（命名/布局原语）+ recycle（回收留痕）。
"""


from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.worktree.layout import _GIT_TIMEOUT


# ------------------------------------------------------------------
# 任务 worktree 生命周期（task-14-worktree-lifecycle，阶段 1 并发引擎）
# ------------------------------------------------------------------
# 建（ensure_task_wt）/ 绑（bind_task_wt，session-worktrees.json 带锁）/
# 用（resolve_task_root）/ 回收（remove_task_wt）/ 清理（prune_orphans）
# 全由引擎自动处理，agent 无感（弱 LLM 友好：best-effort 降级 + 明确错误码 hint）。
#
# flat 降级路径（S2 回归面）：flat 单会话（无 linked worktrees）下不建独立
# worktree——任务 worktree 即主工作树本身（维持现状行为零回归）；container 布局
# 或已存在 linked worktrees（多会话并发）才建独立任务 worktree
# （``<task_wt_root>/task-<id>/``）。

_BINDINGS_FILENAME = "session-worktrees.json"
# 任务 worktree 目录名前缀
_TASK_WT_PREFIX = "task-"


def _task_wt_name(task_id: str) -> str:
    """任务 worktree 目录名：短 id 前缀 ``task-``（full id 去掉冗余 ``task-`` 前缀）。

    例：``task-14-worktree-lifecycle`` → ``task-14-worktree-lifecycle``；
    ``t1`` → ``task-t1``。
    """
    short = task_id[5:] if task_id.startswith("task-") else task_id
    return f"{_TASK_WT_PREFIX}{short}"


def worktree_hint(task_id: str) -> str:
    """任务 worktree 目录名提示（单一来源，task-review-diagnostics-hardening AC4）。

    内部使用 ``_task_wt_name`` 输出真实目录名，供 guard.py / review.py / doctor.py
    等多处调用方统一引用，消除 ``task-task-<id>`` 双前缀漏网（task_id 已含
    ``task-`` 前缀时再拼 ``task-`` 即产出双前缀）。

    例：``task-14-worktree-lifecycle`` → ``task-14-worktree-lifecycle``；
    ``t1`` → ``task-t1``。
    """
    return _task_wt_name(task_id)


def task_branch_head(project_root: Path, task_id: str) -> str | None:
    """best-effort 取 ``task/{task_id}`` 分支 tip SHA（审查基线单一事实源）。

    task-review-baseline-and-worktree-recycle-fix AC1：REVIEW_CLAIMED 的
    ``baseline_sha`` 与 review 提交期的 ``current_sha`` 必须取自**同一来源**
    ——任务分支 tip。修复前两处分别取「主工作树 HEAD」（认领期 project_root
    解析为主工作树，恒为 main HEAD）与「任务 worktree HEAD」（任务分支 tip），
    container 布局下二者必然不同，导致漂移检测恒误报（每次审查都输出
    ``baseline_warning``，实质审查基线校验失效）。

    git 不可用 / 分支不存在 / 异常 → None（调用方回退旧行为，best-effort）。
    """
    try:
        from orchd.line_ctx import resolve_task_branch_for

        branch = resolve_task_branch_for(project_root, task_id)
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def branch_reflog_tip(project_root: Path, branch: str) -> str | None:
    """best-effort 取分支 reflog 中最新一条记录的 tip SHA。

    refs 目录被删但 reflog 存活时（``.git/logs/refs/heads/<branch>``，
    2026-08-08 / 2026-09-10 两次同型事故形态），reflog 末行即分支最后的落点，
    可作为 ``git branch <branch> <tip>`` 的重建依据。无 reflog / 解析失败 → None。
    """
    try:
        proc = subprocess.run(
            ["git", "reflog", "show", "--format=%H", branch],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            # git reflog show 为倒序输出（最新在前）
            if lines:
                return lines[0]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    # ref 无法解析时（refs 被删）git reflog 失效 → 直读 reflog 文件（正序，末行最新）
    return _reflog_file_tip(project_root, branch)


def _reflog_file_tip(project_root: Path, branch: str) -> str | None:
    """直读 ``.git/logs/refs/heads/<branch>`` 末行的 new sha（ref 缺失兜底）。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        git_dir = Path(proc.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (Path(project_root) / git_dir).resolve()
        log = git_dir / "logs" / "refs" / "heads" / branch
        if not log.is_file():
            return None
        lines = [
            ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
        if not lines:
            return None
        # reflog 行格式：<old> <new> <who> <ts> <tz>\t<message>
        fields = lines[-1].split()
        return fields[1] if len(fields) >= 2 else None
    except (OSError, subprocess.SubprocessError, FileNotFoundError):
        return None


def bindings_path(store_root: Path) -> Path:
    """返回任务↔worktree 绑定文件路径（位于共享账本根）。"""
    return Path(store_root) / _BINDINGS_FILENAME


def load_bindings(store_root: Path) -> dict[str, Any]:
    """读取任务↔worktree 绑定映射。

    三态区分（W-2 / R-22，禁止把损坏当空表）：
    - **文件不存在** → 返回 ``{}``（正常初次，调用方可安全覆写）；
    - **文件存在但 JSON 解析失败 / 非 dict 结构** → 抛 ``OrchdError(E002)``，
      调用方**不得**把它当作空绑定表继续覆写（否则一次坏绑定吞掉所有活跃绑定）；
    - **解析正常** → 返回 ``data``。

    抛出 E002 而非静默返回空 dict，是让 ``bind_task_wt`` / ``unbind_task_wt`` 等
    写路径在损坏时走到"保留原文件 + 备份损坏副本 + 告警 + 不覆写"，而非整表覆写。
    只读调用方（``resolve_task_root`` / prune）自行 catch E002 后按保守语义处理。
    """
    path = bindings_path(store_root)
    if not path.is_file():
        return {}
    from orchd.errors import ErrorCode, OrchdError

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise OrchdError(
            ErrorCode.E002,
            f"任务↔worktree 绑定文件损坏（{path}）：{exc}",
            [{"path": str(path), "message": str(exc)}],
        ) from exc
    if not isinstance(data, dict):
        raise OrchdError(
            ErrorCode.E002,
            f"任务↔worktree 绑定文件结构非法（非 dict）：{path}",
            [{"path": str(path), "kind": type(data).__name__}],
        )
    return data


def _save_bindings(store_root: Path, data: dict[str, Any]) -> None:
    """原子写入绑定映射（tmp + os.replace）。

    W-2 / R-22：写失败（磁盘满 / IO 错误）不得静默 —— 原文件保持不变（原子
    替换失败即不改动原文件），并清理残留 tmp，向上抛 ``OSError`` 由调用方告警。
    绝不用坏结果整表覆写成功绑定表。
    """
    path = bindings_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _backup_corrupt_bindings(store_root: Path, path: Path, cause: Exception) -> Path:
    """损坏绑定文件的隔离备份（保留原件，另存可排查副本，幂等）。

    W-2 / R-22：损坏发生时**绝不用空表覆写**成功绑定表。把损坏原件复制为
    ``<name>.corrupt-<ts>`` 副本归档（原件保持不动供继续排查），返回副本路径。
    若文件已损坏到无法读取（IO 层），仅备份保留；copy 失败也吞掉（告警以
    ``OrchdError.details`` 承载，不因备份失败升级为中断）。
    """
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    corrupt = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        shutil.copy2(path, corrupt)
    except OSError:
        # 备份失败不阻断主告警链；cause 已在抛出的 OrchdError 承载
        pass
    return corrupt


def bind_task_wt(store_root: Path, task_id: str, worktree_path: Path) -> dict[str, Any]:
    """绑定「任务 ↔ worktree」到共享账本根 session-worktrees.json（带 Store 文件锁）。

    Args:
        store_root: 共享账本根（resolve_store_dir 结果；未设 ORCHD_HOME 时随布局）。
        task_id: 任务 ID。
        worktree_path: 该任务的 worktree 根（独立任务 worktree 或主工作树降级）。

    Returns:
        ``{"bound": True, "task_id": <str>, "worktree": <str>}``

    Raises:
        ``OrchdError(E002)``：绑定文件已损坏时**中断且不覆写**（保留原件 +
        备份损坏副本 + 结构化告警），防止一次坏绑定吞掉其它活跃任务绑定。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import Store

    store = Store(store_root)
    store.acquire_lock()
    try:
        try:
            data = load_bindings(store_root)
        except OrchdError as exc:
            corrupt = _backup_corrupt_bindings(store_root, bindings_path(store_root), exc)
            raise OrchdError(
                ErrorCode.E002,
                f"绑定文件损坏，中止绑定 {task_id} 以避免覆写其它活跃绑定：{exc.message}",
                [{"corrupt_backup": str(corrupt), "path": str(bindings_path(store_root))}],
            ) from exc
        data[task_id] = {
            "worktree": str(Path(worktree_path).resolve()),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_bindings(store_root, data)
    finally:
        store.release_lock()
    return {"bound": True, "task_id": task_id, "worktree": str(Path(worktree_path).resolve())}


def unbind_task_wt(
    store_root: Path, task_id: str, *, lock_held: bool = False,
) -> dict[str, Any]:
    """解绑「任务 ↔ worktree」（终态回收时调用，带 Store 文件锁）。

    ``lock_held=True``：调用方（如 code 合并流）已在同一共享账本根持有 ``.lock``
    （同进程）。此时解绑**不得再次 flock**——同一进程内两个独立 open() 的 fd 在
    POSIX flock 下互相独立/互斥（Linux flock(2)「以另一 fd 加锁可能被本进程已有锁
    拒绝」），重 flock 会自锁死 → E012。复用已持锁，跳过 acquire/release。

    Returns:
        ``{"unbound": True, "task_id": <str>}``（无绑定也返回 unbound=True，幂等）。

    Raises:
        ``OrchdError(E002)``：绑定文件已损坏时中断（备份损坏副本 + 告警），
        不尝试在不可解析的表基础上 pop+覆写（避免制造二次损坏）。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import Store

    store = Store(store_root)
    if not lock_held:
        store.acquire_lock()
    try:
        try:
            data = load_bindings(store_root)
        except OrchdError as exc:
            corrupt = _backup_corrupt_bindings(store_root, bindings_path(store_root), exc)
            raise OrchdError(
                ErrorCode.E002,
                f"绑定文件损坏，中止解绑 {task_id}：{exc.message}",
                [{"corrupt_backup": str(corrupt), "path": str(bindings_path(store_root))}],
            ) from exc
        data.pop(task_id, None)
        _save_bindings(store_root, data)
    finally:
        if not lock_held:
            store.release_lock()
    return {"unbound": True, "task_id": task_id}


def resolve_task_root(store_root: Path, task_id: str) -> Path | None:
    """从绑定解析任务的 worktree 根；无绑定返回 None。

    W-9 / R-36：登记路径做 ``is_dir()`` 校验（当前环境有效性）。跨环境（沙箱→
    本机 / 拷贝 runtime 根）迁移后登记路径可能指向本机不存在的目录 —— 直接返回
    会让 ``guard_task_root`` E018 指向不存在目录、agent 死循环。失效登记：
    1) 返回 None（调用方守旧语义，当作未绑定）；2) **自动解绑**该任务（best-effort，
       解绑失败**不阻断**返回值 —— 见下方兜底注释）；3) 落一条引擎回收留痕
       （``_log_recycle`` 的 ``unbind_stale_binding`` 动作，stderr ``[回收]`` +
       recycle_log），并注明失效登记与实际解绑结果。

    Returns:
        ``Path``：绑定且当前环境有效的 worktree 绝对路径；或 None（未绑定 /
        登记路径失效已解绑 / 绑定文件损坏）。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.worktree.recycle import _log_recycle, _recycle_actor
    try:
        data = load_bindings(store_root)
    except OrchdError:
        # 损坏时不冒险自动解绑（_backup+告警 在 bind/unbind 入口处理，这里只读降级）
        return None
    entry = data.get(task_id)
    if not entry or not entry.get("worktree"):
        return None
    bound = Path(entry["worktree"])
    if not bound.is_dir():
        # 失效登记：留痕 + 自动解绑 + 返回 None（不把失效路径交给 guard 死循环）。
        # 两处兜底都必须是 **宽 except**：本路径是 guard 读路径，且 _save_bindings
        # 现在会 re-raise OSError（只读/权限/磁盘满）——只 catch OrchdError 会让
        # 读路径抛裸 OSError 落 E999（返工整改：与「解绑失败不阻断返回值」契约对齐）。
        try:
            _log_recycle([{
                "action": "unbind_stale_binding",
                "task_id": task_id,
                "target": str(bound),
                "reason": "登记 worktree 路径在本环境不存在（is_dir=False），自动解绑",
                "actor": _recycle_actor(),
            }])
        except Exception:
            pass
        try:
            unbind_task_wt(store_root, task_id, lock_held=False)
        except Exception:
            pass
        return None
    return bound


def resolve_task_branch(store_root: Path, task_id: str) -> str | None:
    """登记表权威分支（W-4）：任务绑定 worktree → 该 worktree 真实分支；无绑定 None。

    分支判定以 `session-worktrees.json` 登记表为**权威来源**：guidance 的
    ``branch_ctx`` 应描述引擎即将操作的 worktree，而非工具进程的 cwd。无绑定
    （flat 兼容 / 未走 claim 绑定）返回 None，由调用方回退 cwd（最后一次兜底）。

    best-effort：读取 / git 探测异常静默返回 None，不阻塞引导。
    """
    wt = resolve_task_root(store_root, task_id)
    if not wt:
        return None
    try:
        from orchd.gitops import get_current_branch
        return get_current_branch(wt)
    except Exception:
        return None


def guard_task_root(
    project_root: Path | None,
    store_root: Path,
    task_id: str,
    command: str = "done",
) -> dict[str, Any]:
    """守卫（AC2）：目标 root == 任务 worktree，不一致 → E018（防错目录提交/审查）。

    无绑定（flat 单会话兼容 / 未走 claim 绑定的场景）→ 跳过（best-effort）。
    传入 project_root 为 None（单元测试 / 非 git）→ 跳过。

    Returns:
        ``{"guarded": True, "bound_root": <str|None>}``（不抛异常时）。
    """
    from orchd.errors import ErrorCode, OrchdError

    if project_root is None:
        return {"guarded": True, "bound_root": None}
    bound = resolve_task_root(store_root, task_id)
    if bound is None:
        return {"guarded": True, "bound_root": None}
    if os.path.normcase(str(Path(project_root).resolve())) != os.path.normcase(
        str(bound.resolve())
    ):
        raise OrchdError(
            ErrorCode.E018,
            f"wrong_worktree: {command} 目标目录（{project_root}）与任务 {task_id} 的 "
            f"worktree（{bound}）不一致",
            [{
                "task_id": task_id,
                "current_root": str(Path(project_root).resolve()),
                "expected_root": str(bound.resolve()),
                "hint": f"请在任务 worktree 目录内执行 {command}（或使用 -C 指向任务 worktree）",
            }],
        )
    return {"guarded": True, "bound_root": str(bound)}
