"""任务 worktree 回收事务（task-14-worktree-lifecycle）：回收留痕 +
tx journal + 移除。依赖方向：同包 layout/bindings 原语。
"""


from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchd.worktree.bindings import (
    _TASK_WT_PREFIX, _task_wt_name, load_bindings, unbind_task_wt,
)
from orchd.worktree.layout import _GIT_TIMEOUT, detect_layout


def _recycle_actor() -> str:
    """删除/回收动作执行者标识（best-effort）：优先会话指纹，其次原始 session id。"""
    try:
        from orchd.ledger import resolve_agent_id

        agent = resolve_agent_id()
        if agent:
            return agent
    except Exception:
        pass
    return os.environ.get("ORCHD_SESSION_ID") or "<unknown>"


# ``ORCHD_QUIET`` 的真值集合（环境变量宽松解析；未设置 / 空 / 其他值一律不抑制）
_QUIET_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _recycle_quiet() -> bool:
    """回收留痕是否被 ``ORCHD_QUIET`` 抑制（缺省 False = 不抑制）。

    宿主把 orchd 包进「stderr 非空即失败」的外壳时（典型：PowerShell 把
    stderr 行包装成 ``NativeCommandError``，见 conventions.md「命令输出通道
    契约」），可用 ``ORCHD_QUIET=1`` 关闭 ``[回收]`` 留痕。**缺省行为与引入
    开关前逐字节一致**——未设置 / 空值 / 非真值一律不抑制，审计契约不因加
    开关而弱化；抑制仅作用于本留痕，不影响 stdout 的 JSON 契约。

    Returns:
        True = 抑制留痕（不写 stderr）；False = 照常留痕。
    """
    return os.environ.get("ORCHD_QUIET", "").strip().lower() in _QUIET_TRUTHY


def _log_recycle(records: list[dict[str, Any]]) -> None:
    """把删除决策/动作记录打印到 stderr（``orchd ▸ [回收]`` 前缀，best-effort）。

    审计契约（2026-08-30 task-audit-* 分支丢失复盘 §1）：任何 worktree /
    分支 / 目录的删除动作与「拒绝删除」决策都必须留痕，杜绝 best-effort 静默
    删除无法追溯。单条记录为结构化 dict（action / target / reason / evidence /
    actor），JSON 序列化输出。失败静默跳过，不阻断主流程。

    ``ORCHD_QUIET=1`` 时整体跳过（见 :func:`_recycle_quiet`），抑制判定在
    任何输出之前——不触发 W-13 的 stderr UTF-8 reconfigure，零副作用。
    """
    if _recycle_quiet():
        return
    try:
        # W-13：Windows 控制台 stderr 常为 gbk，审计留痕含中文/箭头/路径时会被
        # 乱码或直接抛 UnicodeEncodeError（留痕不可读＝审计失效）→ 输出前把
        # stderr 切到 UTF-8（幂等；不支持 reconfigure 的载体静默跳过）。
        # 跨 mypy 版本免疫：getattr 取回 Any，无需 type-ignore（旧 ignore 码
        # [attr-defined] 在新版报 union-attr，pass8 F3）；缺失/异常仍静默跳过。
        _reconfigure = getattr(sys.stderr, "reconfigure", None)
        if callable(_reconfigure):
            try:
                _reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
        for rec in records:
            print(f"orchd ▸ [回收] {json.dumps(rec, ensure_ascii=False)}", file=sys.stderr)
    except Exception:
        pass


# 回收事务 journal（task-worktree-recycle-tx，AC1/AC2/AC3）。
#
# ``remove_task_wt`` 的四个持久化动作收敛为事务四步：
#   worktree_remove → branch_delete → unbind → commit（删除 journal）。
# 意图先落 journal（``<store_root>/.recycle-journal/<task_id>.json``，原子
# tmp+replace 写），每步"先生效、后记 done"；崩溃/异常中断后下次调用按 journal
# 重放：记 done 的步骤经 git/绑定事实二次验证（防"journal 已记但动作未落"），
# 未记的直接重做。各步天然幂等（缺席 prune / -d 幂等成功 / unbind 缺席成功），
# 故重放收敛（AC2：半完成态可恢复/可重放）。
#
# 无原子回滚（删除不可逆）："原子或可补偿"的补偿 = 前向恢复到完成 + 拒绝语义
# 保持（未合并分支无 force 不删，W-3）。journal 不可用（目录不可写）时退化为
# 既有 best-effort，零回归。并发双回收：分任务 journal 文件互不干扰；同任务
# 并发靠各步幂等收敛（不新增锁，见 remove_task_wt docstring）。
_TX_STEPS = ("worktree_remove", "branch_delete", "unbind")
_RECYCLE_JOURNAL_DIRNAME = ".recycle-journal"


def _recycle_journal_path(store_root: Path, task_id: str) -> Path:
    """回收事务 journal 路径（task-worktree-recycle-tx）。"""
    return Path(store_root) / _RECYCLE_JOURNAL_DIRNAME / f"{task_id}.json"


def _tx_journal_read(path: Path | None) -> dict[str, Any] | None:
    """读 journal；不存在/损坏/形态非法/无 journal → None（调用方按全新事务处理）。"""
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _tx_journal_write(path: Path | None, payload: dict[str, Any]) -> bool:
    """原子写 journal（tmp + os.replace）；失败返回 False，不抛异常。"""
    if path is None:
        return False
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(path))
    except OSError:
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _tx_journal_clear(path: Path | None) -> bool:
    """提交：删除 journal；已不存在视为成功，失败返回 False。"""
    if path is None:
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _tx_mark_step_done(
    path: Path | None, task_id: str, step: str, info: dict[str, Any] | None = None,
) -> bool:
    """记某步 done（读-改-写，其他步状态保留；journal 缺失则新建）。"""
    if path is None:
        return False
    doc = _tx_journal_read(path) or {"task_id": task_id, "steps": {}}
    steps = doc.get("steps")
    if not isinstance(steps, dict):
        steps = {}
        doc["steps"] = steps
    entry: dict[str, Any] = {"state": "done"}
    if info:
        entry.update(info)
    steps[step] = entry
    return _tx_journal_write(path, doc)


def _tx_fact_wt_gone(wt_path: Path) -> bool:
    """事实验证：worktree 目录已不存在（残留空壳也算"在"，需重做清理）。"""
    try:
        return not Path(wt_path).exists()
    except OSError:
        return False


def _tx_fact_branch_gone(stable_wt: Path | None, task_id: str) -> bool | None:
    """事实验证：``task/<id>`` 分支已不存在。

    Returns:
        True/False；git 探针失败（环境异常）→ None（未知，调用方按"需重做"处理，
        重做本身 best-effort，失败口径与既有行为一致）。
    """
    if stable_wt is None:
        return None
    try:
        from orchd.line_ctx import resolve_task_branch_for

        branch = resolve_task_branch_for(stable_wt, task_id)
        proc = subprocess.run(
            ["git", "-C", str(stable_wt), "show-ref", "--verify",
             f"refs/heads/{branch}"],
            capture_output=True, timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    return proc.returncode != 0


def _tx_fact_unbound(store_root: Path, task_id: str) -> bool | None:
    """事实验证：绑定表已无该任务。绑定表损坏（读失败）→ None（需重做，重做会
    以 E002 诚实报错，与既有解绑失败口径一致）。"""
    try:
        data = load_bindings(store_root)
    except Exception:
        return None
    return task_id not in data


def _tx_prune_registry(stable_wt: Path | None) -> bool | None:
    """best-effort ``git worktree prune``（跳过路径的登记收敛，防幽灵登记占用
    分支名）。成功 True，失败 False，无 root 时 None。"""
    if stable_wt is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(stable_wt), "worktree", "prune"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


def _task_status_for_recycle(store_root: Path, task_id: str) -> str | None:
    """读取任务当前状态（删除保护断言与决策打点共用）。

    三态（W-10，2026-09-15：禁用 fail-open）：

    - **有记录** → 返回状态字符串（``in_review`` 等），调用方据此决定是否保护；
    - **确认无记录**（任务不在 replay 派生结果中）→ 返回 ``None``，可安全回收；
    - **读取失败**（store_root 不可用 / replay 异常）→ 抛 ``OrchdError(E030)``，
      由调用方显式处置（拒绝回收 + 留痕，可 force_recycle 覆盖）。旧实现在此处
      返回 None，与「确认无记录」不可区分 → 账本瞬时不可读时 in_review 保护
      **静默失效**，正在审查的任务 worktree 可能被回收。
    """
    from orchd.errors import ErrorCode, OrchdError
    from orchd.ledger import Store

    try:
        ts = Store(store_root).replay().get(task_id)
    except Exception as exc:
        raise OrchdError(
            ErrorCode.E030,
            f"任务状态读取失败，无法确认回收保护（{store_root}）：{exc}",
            [{
                "task_id": task_id,
                "store_root": str(store_root),
                "hint": (
                    "账本不可读时不再按「无记录」放行回收；请修复账本后重试，"
                    "或显式 force-recycle 覆盖保护"
                ),
            }],
        ) from exc
    return ts.status if ts else None


def _classify_remove_failure(err_text: str | None, engine_cwd_inside: bool) -> str | None:
    """worktree 移除失败归因（task-review-submit-mainwt-guidance）。

    ``Permission denied`` 且引擎自身 cwd 当时不在待删目录内 → 持有者极可能是
    调用方 shell 的 cwd（协议要求认领/审查在任务分支执行，review 提交时调用方
    常仍站在任务目录内，Windows 下删 cwd 确定性失败）→ ``cwd_held_by_caller``。
    引擎自身曾在目录内（已自愈仍失败）则归外部句柄（杀毒/索引），返回 None
    走通用失败通道。

    Returns:
        ``"cwd_held_by_caller"`` 或 None（通用失败）。
    """
    if "permission denied" in (err_text or "").lower() and not engine_cwd_inside:
        return "cwd_held_by_caller"
    return None


def _tx_step_remove_worktree(
    stable_wt: Path | None,
    wt_path: Path,
    status: str | None,
    actor: str,
    task_id: str,
    recycle_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """事务第 1 步：worktree 移除（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：cwd 自愈 → ``git worktree
    remove``（脏工作区升级 ``--force``）/ 缺席时 prune 陈旧登记 → 残留空壳清理。
    幂等：目录本就不存在 → prune 后视为已回收。
    """
    removed = False
    discarded_uncommitted = False
    # task-recycle-observability：--force 前快照的被丢弃路径清单（None=无丢弃
    # 或未走到 --force；[] 不可能——空清单时不附键，见 _remove_record 组装）。
    _remove_record_discarded: list[str] | None = None
    wt_existed = (wt_path / ".git").exists()
    # task-worktree-recycle-cwd-selfheal（AC1）：回收前检测调用方进程 cwd 是否
    # 位于待回收 worktree 内。Windows 下进程 cwd 会锁定目录，导致 git worktree
    # remove 失败（已知原因却不自愈）。若命中则 os.chdir 到主工作树（stable_wt），
    # 释放目录锁后再执行 remove；幂等：cwd 不在 worktree 内时零操作。
    # W-7：os.chdir 是进程级副作用——记录调用方原 cwd，移除阶段 finally 恢复。
    cwd_self_healed = False
    _original_cwd: Path | None = None
    try:
        _cur_cwd = Path.cwd().resolve()
        _original_cwd = _cur_cwd
        _wt_resolved = wt_path.resolve()
        # 与 guard_task_root 同口径（task-worktree-residual-self-heal）：双侧
        # normcase，Windows 路径大小写不一致时不再漏判自愈。
        _cwd_inside = (
            os.path.normcase(str(_cur_cwd)) == os.path.normcase(str(_wt_resolved))
            or str(os.path.normcase(str(_cur_cwd))).startswith(
                str(os.path.normcase(str(_wt_resolved))) + os.sep)
        )
    except OSError:
        _cwd_inside = False
    if _cwd_inside and stable_wt and stable_wt.exists():
        try:
            os.chdir(str(stable_wt))
            cwd_self_healed = True
        except OSError:
            cwd_self_healed = False
    if cwd_self_healed:
        recycle_log.append({
            "action": "cwd_self_heal",
            "task_id": task_id,
            "from": str(_cur_cwd),
            "to": str(stable_wt),
            "actor": actor,
        })
    remove_error: str | None = None
    _remove_reason: str | None = None
    registry_pruned: bool | None = None
    try:
        if wt_existed:
            # P2-9：先无 --force 移除（仅干净 worktree 可移，避免丢弃未提交改动）。
            proc = subprocess.run(
                ["git", "worktree", "remove", str(wt_path)],
                cwd=str(stable_wt),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if proc.returncode != 0:
                # W-6 / R-35：只有失败原因确为「工作区脏」才升级 --force；句柄占用 /
                # 权限 / 未知原因一律不升级（旧实现无条件升级：真脏时静默销毁未提交
                # 改动，句柄占用时白跑一遍 --force 且掩盖真实原因）。
                _err = (proc.stderr or "").lower()
                if "modified or untracked" in _err:
                    # task-recycle-observability：--force 前快照将被丢弃的路径清单
                    # （静默 discarded_uncommitted=true 即数据丢失尾巴；清单上限 50，
                    # best-effort，取不到不阻断 --force）。
                    discarded_files: list[str] | None = None
                    try:
                        _st = subprocess.run(
                            ["git", "-C", str(wt_path), "status",
                             "--porcelain=v1", "--untracked-files=all"],
                            capture_output=True, encoding="utf-8",
                            errors="replace", timeout=30,
                        )
                        if _st.returncode == 0:
                            discarded_files = [
                                ln[3:].strip() for ln in
                                (_st.stdout or "").splitlines() if ln.strip()
                            ][:50]
                    except (subprocess.SubprocessError, FileNotFoundError, OSError):
                        discarded_files = None
                    proc = subprocess.run(
                        ["git", "worktree", "remove", "--force", str(wt_path)],
                        cwd=str(stable_wt),
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=30,
                    )
                    discarded_uncommitted = proc.returncode == 0
                    if discarded_uncommitted and discarded_files:
                        _remove_record_discarded = list(discarded_files)
                else:
                    remove_error = (proc.stderr or proc.stdout or "").strip()[:300]
                    _remove_reason = _classify_remove_failure(
                        proc.stderr or proc.stdout or "", _cwd_inside)
            removed = proc.returncode == 0
        else:
            # W-8 / R-35：目录本就不存在时仍收敛 git 登记——陈旧登记会占用
            # task/* 分支名导致分支无法删除（幽灵登记 → 分支永久泄漏）。
            try:
                pr = subprocess.run(
                    ["git", "-C", str(stable_wt), "worktree", "prune"],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=_GIT_TIMEOUT,
                )
                registry_pruned = pr.returncode == 0
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                registry_pruned = False
            removed = True  # 独立 worktree 本就不存在 → 视为已回收
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        remove_error = f"{type(exc).__name__}: {exc}"[:300]
        removed = False
        if isinstance(exc, PermissionError):
            _remove_reason = _classify_remove_failure(str(exc), _cwd_inside)
    finally:
        # W-7：os.chdir 是进程级副作用，移除阶段结束即恢复调用方原 cwd。原 cwd 已被
        # 本次回收删除（常见情形）时保持 stable_wt，避免把进程放进失效目录；后续
        # git 调用一律显式 -C stable_wt，不依赖进程 cwd。
        if cwd_self_healed and _original_cwd is not None:
            try:
                if _original_cwd.is_dir():
                    os.chdir(str(_original_cwd))
            except OSError:
                pass
    _remove_record: dict[str, Any] = {
        "action": "worktree_remove" if wt_existed else "worktree_absent",
        "task_id": task_id,
        "target": str(wt_path),
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "status": status,
        "actor": actor,
    }
    if remove_error:
        _remove_record["error"] = remove_error
    if _remove_reason:
        _remove_record["reason"] = _remove_reason
    if _remove_record_discarded:
        _remove_record["discarded_files"] = _remove_record_discarded
    if registry_pruned is not None:
        _remove_record["registry_pruned"] = registry_pruned
    recycle_log.append(_remove_record)
    # P0-19：Windows 下 git worktree remove 可能删除内容但残留空目录
    # （文件句柄 / .lock / 杀毒扫描导致目录删除不完整）。best-effort 清理残留。
    residual_cleaned = False
    if wt_path.exists() and not (wt_path / ".git").exists():
        try:
            # 先尝试 rmdir（仅空目录成功，安全）
            wt_path.rmdir()
            residual_cleaned = True
        except OSError:
            # 非空目录或权限问题 → 尝试 rmtree（兜底，可能因杀毒/句柄失败）
            try:
                shutil.rmtree(str(wt_path), ignore_errors=True)
                if not wt_path.exists():
                    residual_cleaned = True
            except Exception:
                pass
    if residual_cleaned:
        recycle_log.append({
            "action": "residual_clean",
            "task_id": task_id,
            "target": str(wt_path),
            "actor": actor,
        })
    return {
        "removed": removed,
        "discarded_uncommitted": discarded_uncommitted,
        "discarded_files": _remove_record_discarded,
        "residual_cleaned": residual_cleaned,
        "remove_reason": _remove_reason,
    }


def _tx_step_delete_branch(
    stable_wt: Path | None,
    task_id: str,
    force_recycle: bool,
    actor: str,
    recycle_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """事务第 2 步：任务分支删除（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：先取未合并提交数，非零
    且无 force_recycle → 拒绝并留痕（W-3 红线）；否则安全 ``-d``（分支已不
    存在视为幂等成功）。幂等：分支缺席 → True。
    """
    # 删任务分支（best-effort）——W-3 / R-17（AGENTS.md 红线：不得用 -D 销毁未合并提交）：
    # ① 先取未合并提交数（git rev-list --count main..task/<id>）；② 非零 → 拒绝删除并
    # 留痕，除非调用方显式 force_recycle；③ 否则用安全 -d；④ 分支已不存在视为幂等成功。
    # 以主工作树为稳定 cwd（git -C）：worktree 已回收时 task/{id} 不再被占用可删除；
    # project_root（任务 worktree）可能已被 git worktree remove 删除，不能作为 cwd。
    branch_deleted = False
    branch_refused: dict[str, Any] | None = None
    unmerged_count: int | None = None
    from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

    branch = resolve_task_branch_for(stable_wt, task_id)
    trunk = resolve_trunk_for(stable_wt)
    try:
        count_proc = subprocess.run(
            ["git", "-C", str(stable_wt), "rev-list", "--count", f"{trunk}..{branch}"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        if count_proc.returncode == 0:
            _raw_count = (count_proc.stdout or "").strip()
            if _raw_count.isdigit():
                unmerged_count = int(_raw_count)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        unmerged_count = None

    if unmerged_count and unmerged_count > 0 and not force_recycle:
        branch_refused = {
            "branch": branch,
            "unmerged_commits": unmerged_count,
            "reason": "unmerged_branch_protected",
            "hint": "该分支未被主分支包含；确需丢弃请显式 force_recycle（CLI: --force-recycle）",
        }
        recycle_log.append({
            "action": "branch_delete_refused",
            "task_id": task_id,
            "branch": branch,
            "unmerged_commits": unmerged_count,
            "reason": "unmerged_branch_protected",
            "actor": actor,
        })
    else:
        _branch_flag = "-D" if (force_recycle and unmerged_count) else "-d"
        try:
            proc = subprocess.run(
                ["git", "-C", str(stable_wt), "branch", _branch_flag, branch],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
            )
            branch_deleted = proc.returncode == 0
            if not branch_deleted:
                _berr = (proc.stderr or "").lower()
                if (
                    "not found" in _berr
                    or "doesn't exist" in _berr
                    or "does not exist" in _berr
                ):
                    branch_deleted = True  # 幂等：分支已不存在（如 merge 流程已删）
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            branch_deleted = False
        recycle_log.append({
            "action": "branch_delete",
            "task_id": task_id,
            "branch": branch,
            "mode": _branch_flag,
            "unmerged_commits": unmerged_count,
            "deleted": branch_deleted,
            "actor": actor,
        })
    return {
        "branch_deleted": branch_deleted,
        "branch_refused": branch_refused,
        "unmerged_count": unmerged_count,
    }


def _tx_step_unbind(
    store_root: Path,
    task_id: str,
    lock_held: bool,
    actor: str,
    recycle_log: list[dict[str, Any]],
) -> dict[str, Any]:
    """事务第 3 步：绑定解绑（task-worktree-recycle-tx，AC1 边界之一）。

    原 ``remove_task_wt`` 主体逐行迁移，行为零变化：best-effort，E002/锁失败
    转 warning 留痕并继续（保证 worktree/分支动作留痕不丢失）。幂等：无绑定
    也成功。注意：经模块全局名调用 :func:`unbind_task_wt`（测试 monkeypatch
    ``wt.unbind_task_wt`` 仍可拦截）。
    """
    unbind_error: str | None = None
    try:
        unbound = unbind_task_wt(store_root, task_id, lock_held=lock_held)
    except Exception as exc:
        # best-effort（task-unbind-recycle-audit-best-effort）：解绑抛 E002
        # （绑定损坏）或锁失败时不再向上传播——异常转 warning 并继续执行，
        # 保证本次 worktree / 分支动作的 recycle_log 仍落盘、不丢失留痕。
        code = getattr(getattr(exc, "code", None), "name", type(exc).__name__)
        unbind_error = f"{code}: {exc}"[:300]
        unbound = {
            "unbound": False,
            "task_id": task_id,
            "unbind_error": unbind_error,
        }
        recycle_log.append({
            "action": "unbind_failed",
            "task_id": task_id,
            "unbound": False,
            "error": unbind_error,
            "actor": actor,
        })
    else:
        recycle_log.append({
            "action": "unbind",
            "task_id": task_id,
            "unbound": unbound.get("unbound", False),
            "actor": actor,
        })
    return {"unbound": unbound, "unbind_error": unbind_error}


def remove_task_wt(
    project_root: Path, task_id: str, store_root: Path, *, lock_held: bool = False,
    force_recycle: bool = False,
) -> dict[str, Any]:
    """终态回收：git worktree remove + 删 task/{id} 分支 + 解绑（best-effort 幂等）。

    ``lock_held=True``：调用方已在同一共享账本根持有 ``.lock``（同进程），解绑时
    复用该锁、不再重复 flock（见 :func:`unbind_task_wt`），规避同进程双 fd E012 死锁。

    分支删除安全闸（review W-3 / R-17，AGENTS.md 红线）：
    - 默认只用安全 ``git branch -d``（拒绝删除未合并分支），**绝不再无条件 ``-D``**；
    - 删除前以 ``git rev-list --count main..task/<id>`` 取未合并提交数：非零即拒绝删除，
      计数与拒绝原因记入 ``recycle_log``（``action=branch_delete_refused``）并在返回值
      中给出 ``branch_delete_refused``；
    - 仅当调用方显式传 ``force_recycle=True``（CLI 侧对应 ``--force-recycle``）时，
      才对未合并分支升级 ``-D``；计数为 0 时仍走 ``-d``（无需强删）。

    worktree 移除安全闸（review W-6）：非 ``--force`` 移除失败时，仅当 stderr 表明
    「contains modified or untracked files」才升级 ``--force``（终态回收允许丢弃脏
    工作区并记 ``discarded_uncommitted``）；句柄占用 / 权限 / 未知原因一律不升级，
    失败原因写入 ``recycle_log`` 与 ``residual``（可读、不静默）。

    稳定 cwd 闸（review W-7）：``stable_wt`` 必须是与待回收 worktree **不同**且真实
    存在的主工作树根（``main_worktree_root`` 探测失败会回退 ``project_root``，容器
    布局下那往往就是待删的 worktree 本身）；不满足即拒绝执行删除动作（``reason=
    unstable_main_worktree``）。回收前的 ``os.chdir`` 自愈在移除阶段 ``finally`` 中
    恢复调用方原 cwd（原 cwd 已被本次回收删除时保持 ``stable_wt``，不落进失效目录）。

    幽灵登记收敛（review W-8）：worktree 目录本就不存在时，仍执行 ``git worktree
    prune`` 清理陈旧登记（否则 ``task/*`` 分支被残留 worktree 登记占用而无法删除，
    永久泄漏），结果记入 ``recycle_log`` 的 ``registry_pruned``。

    in_review 保护断言（2026-08-30 复盘 §1）：任务状态为 in_review 时拒绝删除。
    in_review 是审查等待期（任务可能 idle 数小时），恰是分支误删高危窗口；正常
    回收流（review 通过 / force_status 终态）都在状态写入 completed/cancelled
    **之后**才调用本函数，故出现 in_review 即视为异常请求。

    Returns:
        ``{"removed": True, "unbound": True}``；失败降级 ``{"removed": False, ...}``。
        in_review 保护命中返回 ``{"removed": False, "reason": "in_review_protected",
        "status": "in_review"}``；删除动作/决策记录于 ``recycle_log``。
    """
    layout = detect_layout(project_root)
    wt_path = layout["task_wt_root"] / _task_wt_name(task_id)
    # task-14-review-branch-cleanup(AC2)：容器布局下 project_root 即任务 worktree
    # （== wt_path），git worktree remove 会删掉该目录。用 main_worktree_root 在
    # 移除前（cwd 仍有效）解析主工作树根作为稳定 cwd，后续删分支不再依赖已删除的
    # cwd。local import 复用 gitops 已有解析（flat 布局回退 project_root，零回归）。
    from orchd.gitops import main_worktree_root

    stable_wt = main_worktree_root(project_root)
    actor = _recycle_actor()
    recycle_log: list[dict[str, Any]] = []

    # in_review 保护断言（只读查询，确认命中才拒绝）。W-10：账本读取失败**不再
    # 静默放行**（旧实现返回 None 会让保护在账本瞬时不可读时失效）→ 拒绝回收并
    # 留痕，只有显式 force_recycle 才覆盖。
    try:
        status = _task_status_for_recycle(store_root, task_id)
    except Exception as exc:
        if not force_recycle:
            record = {
                "action": "blocked",
                "reason": "status_unreadable",
                "task_id": task_id,
                "target": str(wt_path),
                "error": str(exc)[:300],
                "actor": actor,
            }
            recycle_log.append(record)
            _log_recycle(recycle_log)
            return {
                "removed": False,
                "unbound": False,
                "reason": "status_unreadable",
                "recycle_log": recycle_log,
            }
        status = None
    if status == "in_review":
        record = {
            "action": "blocked",
            "reason": "in_review_protected",
            "task_id": task_id,
            "target": str(wt_path),
            "status": status,
            "actor": actor,
        }
        recycle_log.append(record)
        _log_recycle(recycle_log)
        return {
            "removed": False,
            "unbound": False,
            "reason": "in_review_protected",
            "status": status,
            "recycle_log": recycle_log,
        }

    # W-7 / R-35：stable_wt 必须是与待回收 worktree 不同的真实主工作树根。
    # main_worktree_root 在 git 探测失败时回退 project_root——容器布局下
    # project_root 往往就是待删的任务 worktree 本身；此时继续执行会让
    # git worktree remove / 分支删除带着失效 cwd 冒进（旧实现静默如此）。
    # 此处 fail-safe：拒绝执行删除动作并留痕（宁可留残留交 doctor 处置）。
    _stable_resolved: Path | None = None
    try:
        _stable_resolved = Path(stable_wt).resolve() if stable_wt else None
    except OSError:
        _stable_resolved = None
    _wt_resolved_for_guard = wt_path.resolve()
    if (
        _stable_resolved is None
        or _stable_resolved == _wt_resolved_for_guard
        or not _stable_resolved.is_dir()
    ):
        recycle_log.append({
            "action": "blocked",
            "reason": "unstable_main_worktree",
            "task_id": task_id,
            "target": str(wt_path),
            "stable_worktree": str(_stable_resolved) if _stable_resolved else None,
            "actor": actor,
        })
        _log_recycle(recycle_log)
        return {
            "removed": False,
            "unbound": False,
            "reason": "unstable_main_worktree",
            "recycle_log": recycle_log,
        }

    # ---- F4 事务 preamble：journal 载入 + 重放判定（task-worktree-recycle-tx）----
    # 早退守卫（状态 / in_review / stable）均在本行之前：journal 只在"即将执行
    # 删除动作"时读写，不污染拒绝路径 recycle_log 的精确形态（== ["blocked"]）。
    # 意图先落 journal（四步全 pending）：崩溃落在任何位置都有据可重放；落盘失败
    # （store_root 为 None / 目录不可写）→ journal-less 降级，既有行为零变化。
    _jx_path: Path | None = (
        _recycle_journal_path(store_root, task_id)
        if store_root is not None else None
    )
    _jx_doc = _tx_journal_read(_jx_path)
    _jx_resumed = isinstance(_jx_doc, dict)
    if not _jx_resumed:
        _jx_fresh: dict[str, Any] = {
            "task_id": task_id,
            "steps": {name: {"state": "pending"} for name in _TX_STEPS},
        }
        if not _tx_journal_write(_jx_path, _jx_fresh):
            _jx_path = None
    _jx_prior = ((_jx_doc or {}).get("steps") or {}) if _jx_resumed else {}

    def _jx_was_done(_name: str) -> bool:
        _ent = _jx_prior.get(_name)
        return isinstance(_ent, dict) and _ent.get("state") == "done"

    # 记 done + 事实验证通过 → 跳过并收敛子动作；否则重做（事实不明按重做计，
    # 重做本身 best-effort，失败口径与既有行为一致）。
    _skip_remove = _jx_was_done("worktree_remove") and _tx_fact_wt_gone(wt_path)
    _skip_branch = _jx_was_done("branch_delete") and (
        _tx_fact_branch_gone(stable_wt, task_id) is True)
    _skip_unbind = _jx_was_done("unbind") and (
        _tx_fact_unbound(store_root, task_id) is True)
    if _jx_resumed:
        recycle_log.append({
            "action": "tx_resume",
            "task_id": task_id,
            "skipped": sorted(
                _n for _n, _s in (("worktree_remove", _skip_remove),
                                  ("branch_delete", _skip_branch),
                                  ("unbind", _skip_unbind)) if _s),
            "actor": actor,
        })
    else:
        recycle_log.append({
            "action": "tx_begin",
            "task_id": task_id,
            "steps": list(_TX_STEPS),
            "actor": actor,
        })

    # ---- 事务第 1 步：worktree 移除（失败/拒绝不阻断后继步骤，与既有线性一致）----
    if _skip_remove:
        removed = True
        discarded_uncommitted = False
        discarded_files = None
        residual_cleaned = False
        _remove_reason = None
        # 子动作收敛：prune 仍跑（防崩溃落在 prune 与记 done 之间——幽灵登记会
        # 占用分支名，致后继删分支失败，属半完成态，必须在重放中收敛）。
        _tx_prune_registry(stable_wt)
        recycle_log.append({
            "action": "tx_skip",
            "tx_step": "worktree_remove",
            "task_id": task_id,
            "verified": "wt_absent",
            "actor": actor,
        })
    else:
        _s1 = _tx_step_remove_worktree(
            stable_wt=stable_wt, wt_path=wt_path, status=status,
            actor=actor, task_id=task_id, recycle_log=recycle_log)
        removed = _s1["removed"]
        discarded_uncommitted = _s1["discarded_uncommitted"]
        discarded_files = _s1.get("discarded_files")
        residual_cleaned = _s1["residual_cleaned"]
        _remove_reason = _s1["remove_reason"]
        if removed:
            _tx_mark_step_done(
                _jx_path, task_id, "worktree_remove", {"removed": True})
    # 删任务分支（best-effort）——W-3 / R-17（AGENTS.md 红线：不得用 -D 销毁未合并提交）：
    # ---- 事务第 2 步：分支删除（拒绝/失败不阻断第 3 步，与既有线性一致）----
    if _skip_branch:
        branch_deleted = True
        branch_refused = None
        recycle_log.append({
            "action": "tx_skip",
            "tx_step": "branch_delete",
            "task_id": task_id,
            "verified": "branch_absent",
            "actor": actor,
        })
    else:
        _s2 = _tx_step_delete_branch(
            stable_wt=stable_wt, task_id=task_id, force_recycle=force_recycle,
            actor=actor, recycle_log=recycle_log)
        branch_deleted = _s2["branch_deleted"]
        branch_refused = _s2["branch_refused"]
        if branch_deleted:
            _tx_mark_step_done(
                _jx_path, task_id, "branch_delete", {"deleted": True})

    # ---- 事务第 3 步：解绑 ----
    if _skip_unbind:
        unbound = {"unbound": True, "task_id": task_id}
        unbind_error = None
        recycle_log.append({
            "action": "tx_skip",
            "tx_step": "unbind",
            "task_id": task_id,
            "verified": "binding_absent",
            "actor": actor,
        })
    else:
        _s3 = _tx_step_unbind(
            store_root=store_root, task_id=task_id, lock_held=lock_held,
            actor=actor, recycle_log=recycle_log)
        unbound = _s3["unbound"]
        unbind_error = _s3["unbind_error"]
        if unbound.get("unbound", False):
            _tx_mark_step_done(_jx_path, task_id, "unbind", {"unbound": True})

    # ---- 事务第 4 步 commit：三步全成 → 删 journal；否则保留供重放 ----
    # 保留 journal = 显式半完成态记录（可恢复/可重放，AC2），非新增静默状态：
    # 失败本就经 residual/branch_delete_refused/unbind_error 返回，journal 只是
    # 让"下次调用"能接着做完而不是从头再试（AC3：故障注入可收敛）。
    _tx_committed = False
    if removed and branch_deleted and unbound.get("unbound", False):
        # journal-less 降级（落盘失败）无 journal 可删，视同已提交，不误报。
        _tx_committed = _jx_path is None or _tx_journal_clear(_jx_path)
        recycle_log.append({
            "action": "tx_commit" if _tx_committed else "tx_commit_failed",
            "task_id": task_id,
            "actor": actor,
        })
    _log_recycle(recycle_log)
    result: dict[str, Any] = {
        "removed": removed,
        "unbound": unbound.get("unbound", False),
        "branch_deleted": branch_deleted,
        "recycle_log": recycle_log,
    }
    if unbind_error is not None:
        result["unbind_error"] = unbind_error
        result["warnings"] = [{
            "code": "unbind_failed",
            "task_id": task_id,
            "error": unbind_error,
        }]
    if branch_refused:
        # W-3：未合并分支被拒绝删除 —— 显式回传（含未合并提交数与逃生开关指引）
        result["branch_delete_refused"] = branch_refused
    if discarded_uncommitted:
        result["discarded_uncommitted"] = True
        if discarded_files:
            result["discarded_files"] = discarded_files
    if residual_cleaned:
        result["residual_cleaned"] = True
    if _jx_resumed:
        result["resumed_from_journal"] = True
    if not _tx_committed and (
        removed and branch_deleted and unbound.get("unbound", False)
    ):
        # 三步全成但 journal 落盘删不掉（目录不可写等）：残留 journal 下次调用
        # 重放收敛（全验证通过→再次 commit），此处显式标记供审计。
        result["journal_commit"] = False
    # AC6（task-review-baseline-and-worktree-recycle-fix）：回收失败可解释。
    # 修复前 removed=false 时仅静默返回（unbind + 删分支照旧），磁盘残留空壳目录
    # 无人知晓（实测 task-check-test-dedup-utf8/ 残留，仅 doctor 可检出）。现显式
    # 返回 residual 标记并指向 worktree_residual 处置入口（禁止静默失败）。
    residual_dir = str(wt_path) if wt_path.exists() else None
    if not removed or residual_dir:
        # task-recycle-observability：残留附诊断束——并发 git 进程数（复用
        # guard 计数，只读）、调用方 cwd/pid（句柄占用归因线索）。无 sysinternals
        # 不可得句柄持有者 PID，此处不做无据指认，只给可执行排查信息。
        _diag: dict[str, Any] = {}
        try:
            from orchd.gitops.guard import _count_git_processes

            _diag["concurrent_git_processes"] = _count_git_processes()
        except Exception:
            pass
        try:
            _diag["caller_cwd"] = str(Path.cwd().resolve())
        except OSError:
            pass
        try:
            _diag["caller_pid"] = os.getpid()
            _diag["caller_ppid"] = os.getppid()
        except (OSError, AttributeError):
            pass
        result["residual"] = {
            "path": residual_dir or str(wt_path),
            "removed": removed,
            "residual_cleaned": residual_cleaned,
            "reason": (
                "调用方 shell 的 cwd 仍位于待删目录，git worktree remove 被句柄占用拒绝"
                "（切出目录后重试，或等下次引擎调用自动回收）"
                if _remove_reason == "cwd_held_by_caller"
                else (
                    "git worktree remove 未成功（常见于调用方 cwd 位于该 worktree 内，"
                    "或 Windows 文件句柄占用）"
                    if not removed
                    else "worktree 已注销但目录仍存在（残留空壳，Windows 句柄/杀毒扫描常见）"
                )
            ),
            "hint": (
                "退出该目录后重试回收；或运行 python .orchd/__main__.py doctor "
                "查看 worktree_residual 项并执行 --fix 清理"
            ),
            "doctor_check": "worktree_residual",
        }
        if _diag:
            result["residual"]["diagnostics"] = _diag
    return result


def _cleanup_stale_session_locks(store_root: Path, task_wt_root: Path) -> list[str]:
    """清理 worktree 已不存在的会话锁残留（best-effort）。

    task-workspace-docs-isolation：任务 worktree 终态回收后，其会话锁标记
    （``.session-<wt>.lock`` / ``.session-gate-<wt>.lock``）残留在共享账本根。
    仅清理 worktree 名以 ``task-`` 开头且对应目录已不存在的锁；删除前探活
    flock（他人仍持活锁则跳过，防 flock-unlink 竞态，见
    ``gitops._probe_session_lock_os_active``）。主 worktree 锁
    （``.session.lock`` / ``.session.gate.lock``）不在匹配范围，不触碰。

    Returns:
        已清理的锁文件名清单。
    """
    from orchd.gitops import _probe_session_lock_os_active

    cleaned: list[str] = []
    try:
        for pattern in (".session-*.lock", ".session-gate-*.lock"):
            for p in sorted(store_root.glob(pattern)):
                name = p.name
                if pattern == ".session-*.lock" and name.startswith(".session-gate-"):
                    # review W-17：``.session-*.lock`` 通配也会命中门锁
                    # （``.session-gate-<wt>.lock``），门锁交由专用 pattern 处理，
                    # 避免同一文件在一轮回收里被访问两次。
                    continue
                if name.startswith(".session-gate-"):
                    wt = name[len(".session-gate-"):-len(".lock")]
                elif name.startswith(".session-"):
                    wt = name[len(".session-"):-len(".lock")]
                else:
                    continue
                if not wt.startswith(_TASK_WT_PREFIX):
                    continue  # 仅任务 worktree 维度锁；主 worktree / 其他锁不碰
                if (task_wt_root / wt).exists():
                    continue  # worktree 仍存在 → 活跃，跳过
                if _probe_session_lock_os_active(p).get("active"):
                    continue  # 他人仍持活锁 → 不删（flock-unlink 竞态）
                try:
                    p.unlink()
                    cleaned.append(name)
                except OSError:
                    pass
    except OSError:
        pass
    return cleaned


def _rmtree_force(path: Path) -> bool:
    """删除目录树；Windows 下先清只读属性再删（git 对象文件只读导致 rmtree 失败）。

    task-workspace-docs-isolation：测试残留（如 .pytest-tmp 内 git 仓库）对象文件
    带只读属性，``shutil.rmtree`` 在 Windows 上删除失败（PermissionError WinError 5）。
    onerror 回调清只读后重试单文件删除，其余错误静默跳过。

    Returns:
        ``True`` 目录已不存在（删除成功或本就不存在）。
    """
    from orchd.gitops import _os_delete_tree

    return _os_delete_tree(path)
