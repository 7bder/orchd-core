"""提案注册消费与校验（_consume_registered_proposal/_apply_register_proposals），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.contracts.task_schema import (
    ACCEPTANCE_CRITERIA_FIELD,
    BRIEF_FIELD,
    DEPENDS_ON_FIELD,
    DIFFICULTY_FIELD,
    ESTIMATED_HOURS_FIELD,
    FILES_TO_EDIT_FIELD,
    ID_FIELD,
    MODULE_FIELD,
    NAME_FIELD,
    REQUIRES_FIELD,
    SOURCE_FIELD,
)


def _consume_registered_proposal(proposal_file: Path,
                                project_root: Path) -> dict[str, Any]:
    """注册成功后消费提案草稿（task-hygiene-gate-executor-and-proposal-lifecycle AC1）。

    可逆性依据（不是口头假设，由 tests/test_hygiene_gate_wiring.py 断言）：
    ① 草稿正文已逐字段进 ``_master.json``（同一 JSON 对象 append，非字段映射）；
    ② ``_master.json`` 被 ``.gitignore`` 的 ``!.orchd/_master.json`` 显式反忽略并随注册
       自动提交入库，而草稿本身被 ``.orchd/*`` 规则忽略、零跟踪 ⇒ 注册成功后草稿不含
       独立信息量，删除不丢事实。（引用规则文本而非行号：行号正是本域一直在收口的
       漂移形态。）

    删除走 :func:`orchd.gitops.cleanup._safe_delete`（沙箱把裸 ``Path.unlink`` 劫持为
    「移入回收站」，回收站不可用时 FAIL_CLOSED 抛 OSError → 静默不清理）。幂等：路径
    已不存在视为已消费。

    Args:
        proposal_file: ``--register`` 传入的提案路径。
        project_root: 删除降级的唯一命名基准。

    Returns:
        ``{"path", "consumed", ...}``：``consumed=False`` 时附 ``error`` 与 ``hint``。
        不抛异常——注册已成功（master 已原子落盘），草稿滞留会被卫生门禁
        ``[proposals]`` 检查与 ``doctor --fix`` 再点名一次，故只留痕不反噬注册结果。
    """
    from orchd.gitops.cleanup import _safe_delete

    if not proposal_file.exists():
        return {"path": str(proposal_file), "consumed": True,
                "reason": "already_absent"}
    try:
        _safe_delete(proposal_file, project_root)
    except OSError as exc:
        return {
            "path": str(proposal_file),
            "consumed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": ("注册已成功（任务已入 _master.json），仅草稿文件未清理；"
                     "可运行 orchd doctor --fix 受管移入 .doctor-backup 后删除"),
        }
    return {
        "path": str(proposal_file),
        "consumed": not proposal_file.exists(),
    }


def _closeout_ids(proposals: list[Any]) -> list[str]:
    """本批中 id 以 ``-closeout`` 结尾的任务（收口卡识别口径）。"""
    return [
        str(prop.get(ID_FIELD))
        for prop in proposals
        if isinstance(prop, dict) and str(prop.get(ID_FIELD, "")).endswith("-closeout")
    ]


def _validate_closeout_coverage(
    proposals: list[Any], register: str,
) -> None:
    """旧数组/单任务提案的收口覆盖门：closeout 须显式依赖全部同批子卡。

    ``serial_order`` 信封由 :func:`_expand_serial_order` 自动生成覆盖，不走本函数。
    含 closeout 的批量提案若漏覆盖，注册期直接拒绝（防 2026-10-24 的提前收口事故）。
    """
    closeouts = _closeout_ids(proposals)
    if not closeouts:
        return
    if len(closeouts) > 1:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_closeout_ambiguous: 同批提案含多个 *-closeout 任务",
            [{
                "register": register,
                "closeouts": closeouts,
                "hint": "一个包只能有一张收口卡；请拆分为多个提案或修正 id",
            }],
        )
    if len(proposals) < 2:
        return
    closeout_id = closeouts[0]
    closeout = next(p for p in proposals if p.get(ID_FIELD) == closeout_id)
    others = {
        str(p.get(ID_FIELD)) for p in proposals
        if isinstance(p, dict) and p.get(ID_FIELD) != closeout_id
    }
    missing = others - set(closeout.get(DEPENDS_ON_FIELD) or [])
    if missing:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_closeout_uncovered: closeout 未显式依赖本批全部子卡",
            [{
                "register": register,
                "closeout": closeout_id,
                "missing": sorted(missing),
                "hint": ("含 closeout 的批量提案须让 closeout 覆盖全部非 closeout 任务；"
                         "或改用 {tasks, serial_order, closeout} 信封自动展开"),
            }],
        )


def _expand_serial_order(data: dict, register: str) -> list[dict]:
    """校验并展开 ``{tasks, serial_order, closeout}`` 串行包信封。

    触发条件（全部满足）：唯一 ``*-closeout``、同 source、全为新任务（由调用方
    的重复 id 检查兜底）、``serial_order`` 是除 closeout 外全部 id 的排列、各卡
    无非空显式 ``depends_on``。展开为链：首张 []，第 N 张 [N-1]；closeout 依赖
    ``serial_order`` 全部任务。
    """
    tasks = data.get("tasks")
    order = data.get("serial_order")
    closeout = data.get("closeout")
    if not isinstance(tasks, list) or not tasks:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_tasks_invalid: serial_order 信封缺少非空 tasks 数组",
            [{"register": register}],
        )
    if not isinstance(order, list) or not order:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_order_invalid: serial_order 须为非空数组",
            [{"register": register}],
        )
    if not isinstance(closeout, str) or not closeout:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_closeout_missing: serial_order 信封缺少 closeout id",
            [{"register": register}],
        )
    if not all(isinstance(prop, dict) for prop in tasks):
        raise OrchdError(
            ErrorCode.E007,
            "register_proposal_bad_type: tasks 项须为任务对象",
            [{"register": register}],
        )
    ids = [str(prop.get(ID_FIELD, "")) for prop in tasks]
    if not all(ids):
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_task_id_missing: tasks 中有任务缺 id",
            [{"register": register}],
        )
    closeouts = _closeout_ids(tasks)
    if closeouts != [closeout]:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_closeout_ambiguous: closeout 须为本批唯一 *-closeout",
            [{
                "register": register,
                "closeout": closeout,
                "closeouts": closeouts,
            }],
        )
    others = [tid for tid in ids if tid != closeout]
    if not all(isinstance(tid, str) for tid in order):
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_order_type: serial_order 项须为字符串 id",
            [{"register": register}],
        )
    if len(set(order)) != len(order):
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_order_duplicate: serial_order 含重复 id",
            [{"register": register}],
        )
    if len(order) != len(others) or set(order) != set(others):
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_order_mismatch: serial_order 须为除 closeout 外全部 id 的排列",
            [{
                "register": register,
                "missing": sorted(set(others) - set(order)),
                "extra": sorted(set(order) - set(others)),
            }],
        )
    sources = {prop.get(SOURCE_FIELD) for prop in tasks}
    if len(sources) != 1 or not next(iter(sources)):
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_source_mismatch: serial_order 包须同 source 且 source 非空",
            [{"register": register, "sources": sorted(str(s) for s in sources)}],
        )
    explicit = [prop.get(ID_FIELD) for prop in tasks if prop.get(DEPENDS_ON_FIELD)]
    if explicit:
        raise OrchdError(
            ErrorCode.E007,
            "register_serial_depends_conflict: serial_order 与显式 depends_on 不可混用",
            [{"register": register, "tasks": explicit}],
        )
    by_id = {str(prop[ID_FIELD]): prop for prop in tasks}
    previous: list[str] = []
    for tid in order:
        by_id[tid][DEPENDS_ON_FIELD] = list(previous)
        previous = [tid]
    by_id[closeout][DEPENDS_ON_FIELD] = list(order)
    return tasks


def _apply_register_proposals(master, register_path: str) -> None:
    """task-amend-register-channel：读提案 JSON（单任务对象或任务数组）追加到 master。

    注册契约（proposals/ schema 单一来源）：提案须为单个任务对象、任务数组，或
    ``{tasks, serial_order, closeout}`` 串行包信封。信封存在时先按 serial_order
    展开链式 depends_on，并让 closeout 依赖全部子卡；旧数组含 closeout 时必须显式
    覆盖全部子卡。非法提案 fail-fast（调用方在写盘前抛出 → canonical 零修改）：
      ① 文件不存在 / ② JSON 解析失败 / ③ 提案为整份 master（疑似基于过期 canonical，
      缺已有任务）/ ④ 缺必填字段 / ⑤ 重复 id。坏 source 由后续 amend() 的 E025 口径拒绝。

    成功注册后由调用方消费本文件（见 :func:`_consume_registered_proposal`）——上述
    任一路径抛出时调用方尚未落盘，草稿原样保留（批量数组同理：一项失败整批不写不删）。
    """
    import json as _json

    proposal_file = Path(register_path)
    if not proposal_file.is_file():
        raise OrchdError(
            ErrorCode.E007,
            "register_proposal_missing: 提案文件不存在",
            [{
                "register": str(proposal_file),
                "hint": "传入 proposals/<id>.json（单任务对象）路径",
            }],
        )
    try:
        data = _json.loads(proposal_file.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrchdError(
            ErrorCode.E002,
            f"register_proposal_invalid_json: {exc}",
            [{"register": str(proposal_file)}],
        )
    if isinstance(data, dict) and "serial_order" in data:
        proposals = _expand_serial_order(data, str(proposal_file))
    elif isinstance(data, dict) and "tasks" in data:
        raise OrchdError(
            ErrorCode.E007,
            "register_proposal_is_master: 提案应为单个任务对象、任务数组或 "
            "{tasks, serial_order, closeout} 串行包信封；收到整份 master"
            "（疑似基于过期 canonical，缺已有任务）",
            [{
                "register": str(proposal_file),
                "hint": "请把单任务定义写入 proposals/<id>.json 后重试",
            }],
        )
    else:
        proposals = data if isinstance(data, list) else [data]
        _validate_closeout_coverage(proposals, str(proposal_file))
    existing = {t.get(ID_FIELD) for t in master.tasks}
    required = (
        ID_FIELD, NAME_FIELD, BRIEF_FIELD, MODULE_FIELD, DEPENDS_ON_FIELD,
        ESTIMATED_HOURS_FIELD, DIFFICULTY_FIELD, REQUIRES_FIELD,
        ACCEPTANCE_CRITERIA_FIELD, FILES_TO_EDIT_FIELD, SOURCE_FIELD,
    )
    for prop in proposals:
        if not isinstance(prop, dict):
            raise OrchdError(
                ErrorCode.E007,
                "register_proposal_bad_type: 提案项须为任务对象",
                [{"register": str(proposal_file)}],
            )
        missing = [f for f in required if f not in prop]
        if missing:
            raise OrchdError(
                ErrorCode.E007,
                "register_proposal_missing_fields: 提案缺必填字段",
                [{
                    "register": str(proposal_file),
                    "missing": missing,
                }],
            )
        tid = prop.get(ID_FIELD)
        if tid in existing:
            raise OrchdError(
                ErrorCode.E007,
                f"register_proposal_dup_id: 任务 id 已存在: {tid}",
                [{
                    "register": str(proposal_file),
                    ID_FIELD: tid,
                }],
            )
        master.raw.setdefault("tasks", []).append(prop)
        existing.add(tid)
