"""doctor 只读总检入口（doctor）：check_repo + 残留摘要合流。"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from orchd.doctor.checks_repo import check_repo
from orchd.doctor.residue import detect_residues
from orchd.doctor.runtime import _DETECTION_UNAVAILABLE_RESIDUE, _make_check_unavailable
from .constants import AUTO_CLEAN_DISPOSITIONS


def doctor(project_root: Path) -> dict[str, Any]:
    """执行完整仓库健康检查（结构检查 + 残留/幽灵摘要）。

    DR-8（2026-09-15）：旧实现只调 :func:`check_repo`，不含 residue / ghost，于是
    同一异常会出现「doctor 报健康、watchdog 报 stuck」的判定不一致。现并入
    :func:`detect_residues` 摘要，并按 disposition 归入 ``issues``——判据与卫生
    门禁同源（``AUTO_CLEAN_DISPOSITIONS``）：

    - ``auto_clean`` / ``legacy_move``：交给自动清理通道处置 → 不计入失败
      （避免「检测器报清理器即将删除之物」的抢跑抖动）；
    - ``manual``：无自动通道、需人工处置 → 计入 ``issues`` 且 ``repo_ok=False``。

    INV-4a（task-detection-fail-closed-doctor-ledger）：「检测不可用」不再静默——
    残留扫描整体异常时产出 ``residue_scan`` 不可用检查项（而非空残留=健康）；各检测
    器内部的异常由其自身转成 ``detection_unavailable`` 残留项或不可用检查项（见模块内
    「检测不可用」语义块）。

    Returns:
        ``{repo_ok, degraded, detection_unavailable, checks, issues, warnings, repo,
        residues, residue_summary}``；
        ``repo_ok`` 为 False 表示存在任一 fail 检查项（含「检测不可用」项）或 manual
        档残留（调用方可据此设非零退出码）；
        ``warnings`` 为 ``status == "warn"`` 检查项的 hint 清单（**非阻断**档：数据
        停滞一类，不进 ``issues``、不翻 ``repo_ok``、不改退出码，见模块头 INV-4a
        词汇表节第 5 条）；
        ``degraded`` 为 True 表示本轮**至少一项检测未能判定**；
        ``detection_unavailable`` 为这些检测的名称列表（不可用检查项名 + 不可用残留项
        的 ``detector``）——agent 据此区分「查出问题该修」与「没查成该人工介入」。
    """
    checks = check_repo(project_root)
    try:
        residues = detect_residues(project_root)
    except Exception as exc:  # INV-4a：残留扫描整体失败 → 不可用检查项（非"无残留"）
        residues = []
        checks = [
            *checks,
            _make_check_unavailable("residue_scan", exc, "残留扫描整体不可用"),
        ]
    issues = [c["hint"] for c in checks if c["status"] == "fail"]
    # warn = 非阻断档（文件头 INV-4a 第 5 条）：不进 issues、不翻 repo_ok、不改
    # 退出码，只经顶层 warnings 透出——「数据停滞」与「仓库完整性故障」分档。
    warnings = [c["hint"] for c in checks if c["status"] == "warn"]
    by_disposition: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in residues:
        disp = str(item.get("disposition", "unknown"))
        by_disposition[disp] = by_disposition.get(disp, 0) + 1
        rtype = str(item.get("type", "unknown"))
        by_type[rtype] = by_type.get(rtype, 0) + 1
    manual = [
        r for r in residues
        if r.get("disposition") not in AUTO_CLEAN_DISPOSITIONS
    ]
    for item in manual:
        issues.append(f"[residue/{item.get('type')}] {item.get('reason', '')}"
                      f"（path={item.get('path')}）")
    # INV-4a：本轮「没能判定」的检测名（检查项 unavailable 键 + 残留项 detector）
    unavailable = sorted({
        str(c["name"]) for c in checks if c.get("unavailable")
    } | {
        str(r.get("detector"))
        for r in residues
        if r.get("type") == _DETECTION_UNAVAILABLE_RESIDUE and r.get("detector")
    })
    return {
        "repo_ok": len(issues) == 0,
        "degraded": bool(unavailable),
        "detection_unavailable": unavailable,
        "checks": checks,
        "issues": issues,
        "warnings": warnings,
        "repo": str(Path(project_root).resolve()),
        "residues": residues,
        "residue_summary": {
            "total": len(residues),
            "by_disposition": by_disposition,
            "by_type": by_type,
            "manual_failures": len(manual),
        },
    }
