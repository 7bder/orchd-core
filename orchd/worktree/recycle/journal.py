"""worktree recycle journal 域（W6-C5 纯搬移）。

来源：``orchd/worktree/recycle.py``（1023 行，W6-C5 纯搬移）。
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
    """原子写 journal（tmp + fsync + _atomic_replace）；失败返回 False，不抛异常。

    耐久档（task-os-replace-hardening-worktree-batch 批次 1a）：与账本
    ``ledger._write_text_atomic`` 同一硬化序——tmp 写入 → flush + fsync →
    ``ledger._atomic_replace``（WinError 5/32 有界重试）→ ``_fsync_dir``。
    ``_atomic_replace`` 争用耗尽抛的结构化 E007 在此捕获并映射回 False——
    本函数契约是 best-effort 日志面（调用方按 False 走「journal 不可用」
    降级），一次 Windows 句柄争用不得把回收路径从「降级继续」变成「崩溃」。
    tmp 的 pid 后缀命名（防同会话并行碰撞）与 JSON 文本形态原样保留。
    """
    if path is None:
        return False
    from orchd.errors import OrchdError
    from orchd.ledger import _atomic_replace, _fsync_dir

    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(tmp, path)
    except (OSError, OrchdError):
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    _fsync_dir(path.parent)
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
