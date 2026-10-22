"""提案注册消费与校验（_consume_registered_proposal/_apply_register_proposals），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError


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


def _apply_register_proposals(master, register_path: str) -> None:
    """task-amend-register-channel：读提案 JSON（单任务对象或任务数组）追加到 master。

    注册契约（proposals/ schema 单一来源）：提案须为单个任务对象或任务数组。
    非法提案 fail-fast（调用方在写盘前抛出 → canonical 零修改）：
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
    if isinstance(data, dict) and "tasks" in data:
        raise OrchdError(
            ErrorCode.E007,
            "register_proposal_is_master: 提案应为单个任务对象或任务数组，收到整份 master"
            "（疑似基于过期 canonical，缺已有任务）",
            [{
                "register": str(proposal_file),
                "hint": "请把单任务定义写入 proposals/<id>.json 后重试",
            }],
        )
    proposals = data if isinstance(data, list) else [data]
    existing = {t.get("id") for t in master.tasks}
    required = (
        "id", "name", "brief", "module", "depends_on", "estimated_hours",
        "difficulty", "requires", "acceptance_criteria", "files_to_edit", "source",
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
        tid = prop.get("id")
        if tid in existing:
            raise OrchdError(
                ErrorCode.E007,
                f"register_proposal_dup_id: 任务 id 已存在: {tid}",
                [{
                    "register": str(proposal_file),
                    "id": tid,
                }],
            )
        master.raw.setdefault("tasks", []).append(prop)
        existing.add(tid)
