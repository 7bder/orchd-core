"""补丁字段校验（verify timeout / additional sources），依赖 constants。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from .constants import _VERIFY_TIMEOUT_MAX, _VERIFY_TIMEOUT_MIN


def _validate_verify_timeout_patch(task_id: str, raw: Any) -> int:
    """校验 ``--verify-timeout-seconds`` 取值并返回 int（非法 → E007，调用方不落盘）。

    值域 ``[1, 600]``：下界与 schema 同源，上界为防呆硬顶（见 constants.py 常量注释）。
    ``bool`` 显式拒绝——``True`` 是 ``int`` 子类，落盘会写出 ``true`` 而非秒数
    （schema 要求 integer，宁可 CLI 先拒）；非整数文本（``abc`` / ``1.5`` / 空串）
    同样 E007。越界与非法都不写入：调用方在写文件前先走到本函数。
    """
    value: int | None = None
    if not isinstance(raw, bool):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = None
    if value is None:
        raise OrchdError(
            ErrorCode.E007,
            f"verify_timeout_seconds 非法：须为正整数秒（收到 {raw!r}），未写入",
            [{
                "task_id": task_id,
                "field": "verify_timeout_seconds",
                "value": str(raw),
                "expected": f"{_VERIFY_TIMEOUT_MIN}..{_VERIFY_TIMEOUT_MAX} 的整数秒",
            }],
        )
    if not _VERIFY_TIMEOUT_MIN <= value <= _VERIFY_TIMEOUT_MAX:
        raise OrchdError(
            ErrorCode.E007,
            f"verify_timeout_seconds 越界：须在 {_VERIFY_TIMEOUT_MIN}..{_VERIFY_TIMEOUT_MAX} "
            f"秒之间（收到 {value}），未写入",
            [{
                "task_id": task_id,
                "field": "verify_timeout_seconds",
                "value": value,
                "min": _VERIFY_TIMEOUT_MIN,
                "max": _VERIFY_TIMEOUT_MAX,
            }],
        )
    return value


def _validate_additional_sources_patch(
    task_id: str,
    refs: list[str],
    project_root: Path,
) -> None:
    """补丁 additional_sources 引用按与 spec.validate_source 相同口径校验（E025）。

    task-amend-additional-sources-field：amend --additional-sources 补登的每个引用
    须与 source 同口径（格式 ^(idea|roadmap|debug):[a-z0-9-]+$；idea 条目存在且
    status 为 pending；roadmap 章节存在；debug 不校验文件引用）。校验通过返回
    None；任一引用非法 raise E025（未写入任何补丁）。
    """
    import re as _re

    from orchd.ledger import resolve_roadmap_path, resolve_workspace_root
    from orchd.spec import (
        _check_idea_reference,
        _check_roadmap_reference,
    )

    workspace_root = resolve_workspace_root(project_root)
    for j, ref in enumerate(refs):
        if not isinstance(ref, str) or not _re.fullmatch(
                r"(idea|roadmap|debug):[a-z0-9-]+", ref):
            raise OrchdError(
                ErrorCode.E025,
                f"amend_blocked: additional_sources[{j}] '{ref}' 格式非法"
                "（须 ^(idea|roadmap|debug):[a-z0-9-]+$），未写入",
                [{"task_id": task_id, "reference": ref}],
            )
        prefix, _, ref_id = ref.partition(":")
        ref_id = ref_id.strip()
        _errors: list[Any] = []
        if prefix == "idea":
            ideas_path = workspace_root / "IDEAS.md"
            if not ideas_path.exists():
                raise OrchdError(
                    ErrorCode.E025,
                    f"amend_blocked: 引用 {ref} 但 IDEAS.md 文件缺失（无法溯源），未写入",
                    [{"task_id": task_id, "reference": ref}],
                )
            _errors = _check_idea_reference(task_id, 0, ref_id, ideas_path)
        elif prefix == "roadmap":
            roadmap_path = resolve_roadmap_path(project_root)
            if not roadmap_path.exists():
                raise OrchdError(
                    ErrorCode.E025,
                    f"amend_blocked: 引用 {ref} 但 ROADMAP.md 文件缺失（无法溯源），未写入",
                    [{"task_id": task_id, "reference": ref}],
                )
            _errors = _check_roadmap_reference(task_id, 0, ref_id, roadmap_path)
        else:  # debug: 前缀不校验文件引用（与 validate_source 同口径）
            continue
        if _errors:
            raise OrchdError(
                ErrorCode.E025,
                "amend_blocked: additional_sources 引用校验未通过（未写入）",
                [{
                    "task_id": task_id,
                    "reference": ref,
                    "errors": [e.message for e in _errors],
                }],
            )
