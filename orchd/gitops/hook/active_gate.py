"""hook 卸载活跃认领门控（task-debug-hook-active-gate，hook 子域叶模块）。

**要解决的问题**：hook 目录由 ``core.hooksPath`` 决定，是全仓绝对路径、所有工作树共享
（见 :mod:`.paths`）。此前每个任务收尾（done / retract / code APPROVED）都**无条件**
卸载 pre-commit，于是并行会话下出现「A 合并完成删 hook → B 的提交窗口期越界校验
fail-open」——实测 ``.githooks/pre-commit`` 瞬时消失约 1 分钟后被下一会话 claim 重烘焙，
窗口期内的提交完全绕过 L3 范围校验与 reference-transaction 强制层。

**判据**：账本里仍有活跃认领（``status == claimed``，或 ``status == in_review`` 且
``review_claimed_by`` 非空）时保留 hook。``done`` / ``completed`` / ``pending`` 不算——
done 之后本任务的提交已经发生，再拦没有意义，留着反而让 hook 永久残留。

**回退方向**：状态不可读（未传 ``orchd_dir`` / 目录不存在 / 账本或后端异常）时**回退旧
行为（删除）**，只把原因写进 ``state_check``：能力探测失败不得让共享 hook 永久残留。

依赖方向：本模块只依赖标准库（顶层零 orchd 导入）；账本、事件状态常量与日志函数
一律函数级惰性导入——既避免 ``hook/__init__`` 的加载期环，也沿用本仓「叶域零 orchd
顶层依赖」的结构面约定。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# 日志回调类型（由调用方注入 install 域的 ``_log_hook_skip``）：本模块**不得**
# import ``.install``——那会与 install→active_gate 的顶层导入形成依赖环，
# 也让「叶域」结构面判据失效。回调是单向边，代价为零。
LogFn = Callable[[str, dict[str, Any]], None]


def active_claim_tasks(
    orchd_dir: Path | None,
) -> tuple[bool, list[dict[str, Any]], str]:
    """解析账本里仍持实现/审查认领的任务。

    Args:
        orchd_dir: ``.orchd`` 目录（账本根由 :func:`orchd.ledger.resolve_store_dir`
            解析，container 布局自动指向布局级 runtime 根）。

    Returns:
        ``(available, active_tasks, error)``。``available=False`` 表示状态不可读
        （``error`` 带原因：``orchd_dir_not_provided`` / ``orchd_dir_missing`` /
        ``<异常类型>: <消息>``），调用方据此回退旧行为并留痕。
        ``active_tasks`` 每项含 ``task_id`` / ``status`` / ``claimed_by`` /
        ``review_claimed_by``。
    """
    if orchd_dir is None:
        return (False, [], "orchd_dir_not_provided")
    try:
        from orchd.contracts.events import STATUS_CLAIMED, STATUS_IN_REVIEW
        from orchd.ledger import Store, resolve_store_dir

        # 判据必须落在**真账本根**上：orchd_dir 是 master 目录，container 布局下
        # 账本根由 resolve_store_dir 解析到布局级 runtime 根。判错路径会恒回退删除。
        store_root = resolve_store_dir(Path(orchd_dir))
        if not store_root.is_dir():
            return (False, [], "store_root_missing")
        # 探测期**只读**：非 readonly 打开会触发 jsonl→sqlite 迁移/建库，迁移失败或
        # 库打不开就会被当成「无活跃认领」→ 删除；基础设施故障不得冒充状态结论。
        state = Store(Path(orchd_dir), readonly=True).replay()
    except Exception as exc:  # noqa: BLE001 - 能力探测失败不得阻断主流程
        return (False, [], f"{type(exc).__name__}: {exc}")
    active: list[dict[str, Any]] = []
    for task_id, ts in state.items():
        if ts.status == STATUS_CLAIMED or (
                ts.status == STATUS_IN_REVIEW and ts.review_claimed_by):
            active.append({
                "task_id": task_id,
                "status": ts.status,
                "claimed_by": ts.claimed_by,
                "review_claimed_by": ts.review_claimed_by,
            })
    return (True, active, "")


def with_state_check(
    result: dict[str, Any], state_check: dict[str, Any] | None,
) -> dict[str, Any]:
    """给卸载结果附活跃判定留痕（``state_check`` 仅在真做过判定时出现）。"""
    if state_check is not None:
        result["state_check"] = state_check
    return result


def uninstall_gate(
    *, hook_is_ours: bool, orchd_dir: Path | None, force: bool,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """决定本次卸载是否被活跃认领拦下。

    ``hook_is_ours`` 由调用方按 owner-aware 判据给出——门控只对本引擎生成物生效：
    宿主自有 hook 任何时候都不删，此时如实报 ``foreign_hook_present`` 比报「保留」
    更符合调用方预期。

    Args:
        hook_is_ours: 目标 pre-commit 是否为本引擎生成物（含标记头）。
        orchd_dir: ``.orchd`` 目录；``None`` = 不做判定（旧行为）。
        force: 显式跳过门控（仅保留给测试/极端兜底；生产调用点不使用）。
        log: 留痕回调（注入 ``install._log_hook_skip``，保持单向依赖）；``None`` = 静默。

    Returns:
        ``{"retain", "result", "state_check", "ref_tx_retained"}``：
        ``retain=True`` 时调用方直接返回 ``result``（pre-commit 与强制层都保留）；
        ``ref_tx_retained=True`` 表示**只保留 reference-transaction 强制层**（pre-commit
        非本引擎生成物，走既有 owner-aware 分支），调用方据此跳过强制层卸载。
        两者都 False 时继续走完整卸载流程。
    """
    emit = log or (lambda action, payload: None)
    if force or orchd_dir is None:
        return {"retain": False, "result": None, "state_check": None,
                "ref_tx_retained": False}
    available, active, error = active_claim_tasks(orchd_dir)
    if not available:
        state_check: dict[str, Any] = {"available": False, "reason": "state_unavailable"}
        if error:
            state_check["error"] = error[:200]
        emit("uninstall_state_unavailable", {
            "path": str(orchd_dir), "error": error[:200],
        })
        return {"retain": False, "result": None, "state_check": state_check,
                "ref_tx_retained": False}
    if not active:
        return {
            "retain": False,
            "result": None,
            "state_check": {"available": True, "active_tasks": 0},
            "ref_tx_retained": False,
        }
    # 仍有活跃认领：**强制层一律保留**（红线 #1/#2），与 pre-commit 归属无关——
    # pre-commit 缺失/被宿主 hook 顶替时若照旧卸载 ref-tx，等于把 fail-open 从提交层
    # 转移到引用事务层（本条由 code 审查 BLOCKER 1 指出）。
    if not hook_is_ours:
        emit("uninstall_retained_force_layer", {
            "active_tasks": [t["task_id"] for t in active],
        })
        return {
            "retain": False,
            "result": None,
            "state_check": {"available": True, "active_tasks": len(active)},
            "ref_tx_retained": True,
        }
    emit("uninstall_retained_active_claims", {
        "active_tasks": [t["task_id"] for t in active],
    })
    return {
        "retain": True,
        "result": {
            "uninstalled": False,
            "reason": "retained_active_claims",
            "active_tasks": active,
            "state_check": {"available": True},
        },
        "state_check": {"available": True},
        "ref_tx_retained": True,
    }
