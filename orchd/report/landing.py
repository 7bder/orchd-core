"""report — 落地面在册扫描域（自 ``audit.py`` 拆出，纯搬移）。

``audit`` 重导出本模块符号，保持 ``orchd.report.audit.<名>`` 同一对象。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchd import surfaces
from orchd.errors import OrchdError


@dataclass(frozen=True)
class LandingScan:
    """『落地任务须在 CHANGELOG 在册』的只读扫描结果（引擎与宿主 guard 共用）。

    degradation 非空表示判据不可用（无锚 / git 不可读 / CHANGELOG 不可读）：
    此时 missing 恒空，调用方按自身纪律降级留痕（引擎 audit 静默或告警，
    宿主 guard 标记 failed），不得把「没查成」当成「没缺口」或「有缺口」。
    """

    anchor: str | None
    landed: tuple[str, ...]
    missing: tuple[str, ...]
    changelog_path: Path
    text: str = ""
    degradation: str | None = None
    detail: str = ""
    fragment_ids: tuple[str, ...] = ()
    unassembled: tuple[str, ...] = ()
    rows: int = 0
    fragments: tuple[str, ...] = ()
    surface: dict[str, Any] = field(default_factory=dict)
    missing_fragments: tuple[str, ...] = ()
    degraded: bool = False

def changelog_landing_scan(
    project_root: Path,
    *,
    changelog_rel: str | None = None,
    task_pattern: "re.Pattern[str] | None" = None,
    anchor: str | None = None,
) -> LandingScan:
    """『落地任务须在 CHANGELOG 在册』的判据本体（只读、零副作用）。

    真源（**禁**自建 git 遍历、**禁**复制 CHANGELOG 判据）：
    ``orchd.gitops.query.previous_release_tag`` 定锚（可用 ``anchor`` 覆盖）、
    ``orchd.gitops.query.landed_task_ids_since`` 取落地集；在册判定为
    「任务 id 是否出现在 CHANGELOG 全文」。

    宿主侧可配置 ``changelog_rel``（仓库相对路径）、``task_pattern``
    （对落地 id 做 fullmatch 过滤，未匹配的 id 不参与「缺行」判定）与
    ``anchor``（显式锚，缺省沿用上一发版 tag）。降级情形一律以
    :attr:`LandingScan.degradation` 返回，**不抛异常**：

    - ``no_anchor``：无上一发版 tag / 显式锚为空——「缺行」无从定义；
    - ``git_unreadable``：git 不可用或 ref 非法——落地集不可读；
    - ``changelog_unreadable``：CHANGELOG 缺失 / 权限 / 非 UTF-8。

    Args:
        project_root: 仓库根目录。
        changelog_rel: CHANGELOG 仓库相对路径；None 时用落地面注册表解析结果。
        task_pattern: 可选正则；非空时只统计 fullmatch 命中的落地 id。
        anchor: 可选显式锚（tag/commit）；None 时取上一发版 tag。

    Returns:
        :class:`LandingScan`；``missing`` 保序且去重。
    """
    from orchd.gitops import query as _query

    root = Path(project_root)
    try:
        registry = surfaces.load_surfaces(root, changelog_rel=changelog_rel)
    except OrchdError as exc:
        return LandingScan(
            None, (), (), root,
            degradation="surface_config",
            detail=str(getattr(exc, "message", exc)),
        )
    changelog_spec = registry[surfaces.DEFAULT_SURFACE_NAME]
    changelog = root / changelog_spec.path
    ref = anchor or _query.landed.previous_release_tag(root)
    if not ref:
        return LandingScan(None, (), (), changelog, degradation="no_anchor")
    landed = _query.landed.landed_task_ids_since(root, ref)
    if landed is None:
        return LandingScan(ref, (), (), changelog, degradation="git_unreadable")
    if task_pattern is not None:
        landed = [tid for tid in landed if task_pattern.fullmatch(tid)]
    try:
        text = changelog.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError ⊂ **ValueError**，不在 OSError 内：只捕 OSError 会让
        # 非 UTF-8 的 CHANGELOG 把异常抛到调用方——而 `status --audit-merge`
        # 对 merge_audit 无 try 保护，会整条命令以 exit 1 失败。降级留痕。
        return LandingScan(
            ref, tuple(landed), (), changelog,
            degradation="changelog_unreadable",
            detail=f"{type(exc).__name__}: {exc}",
        )
    try:
        surface_scan = surfaces.scan_surface(root, changelog_spec, text=text)
        fragments_dir = root / changelog_spec.dir if changelog_spec.dir else None
    except OrchdError as exc:
        return LandingScan(
            ref, tuple(landed), (), changelog, text=text,
            degradation="surface_config",
            detail=str(getattr(exc, "message", exc)),
        )
    missing = tuple(
        tid
        for tid in landed
        if not surfaces.recorded(tid, changelog_spec, text, fragments_dir)
    )
    return LandingScan(
        ref, tuple(landed), missing, changelog, text=text,
        fragment_ids=surface_scan.fragment_ids,
        unassembled=surface_scan.unassembled,
        rows=surface_scan.rows,
        fragments=tuple(fragment.rel_path for fragment in surface_scan.fragments.values()),
        surface=surfaces.surface_descriptor(changelog_spec),
        missing_fragments=surface_scan.missing,
        degraded=surface_scan.degraded,
    )

def surface_landing_scan(
    project_root: Path,
    surface_name: str,
    *,
    task_pattern: "re.Pattern[str] | None" = None,
    anchor: str | None = None,
) -> LandingScan:
    """通用落地面在册扫描（任意已注册面：fragments / generated / serial）。

    与 :func:`changelog_landing_scan` 同判据（同一 git 真源 + ``surfaces.recorded``），
    只是面由 ``surface_name`` 指定：fragments 面把未装配片段也算在册，
    generated / serial 面按主文件文本判定。降级情形同样以 ``degradation`` 返回，不抛。
    """
    from orchd.gitops import query as _query

    root = Path(project_root)
    try:
        specs = surfaces.load_surfaces(root)
    except OrchdError as exc:
        return LandingScan(
            None, (), (), root,
            degradation="surface_config",
            detail=str(getattr(exc, "message", exc)),
        )
    spec = specs.get(surface_name)
    if spec is None:
        return LandingScan(
            None, (), (), root, degradation="surface_unknown", detail=str(surface_name)
        )
    surface_path = root / spec.path
    descriptor = surfaces.surface_descriptor(spec)
    ref = anchor or _query.landed.previous_release_tag(root)
    if not ref:
        return LandingScan(None, (), (), surface_path, surface=descriptor,
                           degradation="no_anchor")
    landed = _query.landed.landed_task_ids_since(root, ref)
    if landed is None:
        return LandingScan(ref, (), (), surface_path, surface=descriptor,
                           degradation="git_unreadable")
    if task_pattern is not None:
        landed = [tid for tid in landed if task_pattern.fullmatch(tid)]
    try:
        surface_scan = surfaces.scan_surface(root, spec)
    except OrchdError as exc:
        return LandingScan(ref, tuple(landed), (), surface_path, surface=descriptor,
                           degradation="surface_config",
                           detail=str(getattr(exc, "message", exc)))
    fragments_dir = root / spec.dir if spec.dir else None
    missing = tuple(
        tid
        for tid in landed
        if not surfaces.recorded(tid, spec, surface_scan.text, fragments_dir)
    )
    return LandingScan(
        ref, tuple(landed), missing, surface_path,
        text=surface_scan.text,
        degradation=surface_scan.degradation,
        detail=surface_scan.detail,
        fragment_ids=surface_scan.fragment_ids,
        unassembled=surface_scan.unassembled,
        rows=surface_scan.rows,
        fragments=tuple(fragment.rel_path for fragment in surface_scan.fragments.values()),
        surface=descriptor,
        missing_fragments=surface_scan.missing,
        degraded=surface_scan.degraded,
    )

def _changelog_landing_gaps(
    project_root: Path,
    *,
    incoming_task_id: str | None = None,
) -> list[dict[str, Any]]:
    """『落地任务须在 CHANGELOG 在册』的缺行清单（只读，恒不阻断）。

    判据本体已收敛到 :func:`changelog_landing_scan`（引擎与宿主 guard ``docs``
    同源同口径）：本函数只把扫描结果映射为 merge 审计 warning 形状，并叠加
    done 前置预检的 ``incoming_task_id`` 投影。

    降级（best-effort，绝不抛异常出界）：无锚 / git 不可读 → 返回空；落地集
    为空时 merge 审计侧返回空（锚缺失时「缺行」无从定义），done 预检侧仍投影
    ``incoming_task_id``；``CHANGELOG.md`` 不可读 → 单列 ``changelog_unreadable``
    留痕（仍不阻断）。

    Args:
        project_root: 仓库根目录（git 命令 cwd，与 ``merge_audit`` 一致）。
        incoming_task_id: 可选。done 前置预检
            （``orchd.onboard.lifecycle.guards._guard_landing_precheck``）传入
            **本任务 id**：其实现提交尚未合入、不在落地集内时仍按「落地后将
            缺行」投影判定，返回项置 ``incoming_missing=True``；merge 审计不
            传该参数（只判已落地集，保持既有告警语义）。

    Returns:
        warning 项列表（0 或 1 项），由 ``merge_audit`` 并入 warnings；
        done 预检只消费 ``incoming_missing`` 标志，历史落地缺口不阻断。
    """
    scan = changelog_landing_scan(project_root)
    if scan.degradation == "changelog_unreadable":
        return [{
            "reason": "changelog_unreadable",
            "path": str(scan.changelog_path),
            "message": (f"CHANGELOG.md 不可读（{scan.detail}）"
                        "——『落地任务在册』检查降级，需人工核对"),
        }]
    if scan.degradation is not None:
        return []
    missing = list(scan.missing)
    # done 预检（task-engine-defect-fix-unified-u9）投影：本任务实现提交尚未合入
    # 时不会进入落地集，但落地后必然缺行——提前按「落地后将缺行」判定，避免
    # 合入当时才发现缺账（与 merge 审计同一 helper，禁止消费点自建判据）。
    # 判据只看「本任务 id 是否在 CHANGELOG 全文」，**不看**它是否已在 missing：
    # 实现者先行提交时（rules/git.md 允许），其 `task-<slug>: ...` 主题会被 landed
    # 正则计入落地集并落进 missing；若再加 `not in missing` 过滤，缺口就会被吞成
    # False，done 预检 fail-open（u9 一轮审查 B1 的根因）。
    incoming_missing = False
    if incoming_task_id:
        try:
            changelog_spec = surfaces.load_surfaces(project_root)[
                surfaces.DEFAULT_SURFACE_NAME
            ]
        except OrchdError:
            changelog_spec = None
        if changelog_spec is not None:
            fragments_dir = (
                Path(project_root) / changelog_spec.dir if changelog_spec.dir else None
            )
            incoming_missing = not surfaces.recorded(
                incoming_task_id, changelog_spec, scan.text, fragments_dir
            )
    if not missing and not incoming_missing:
        return []
    if incoming_missing and incoming_task_id not in missing:
        missing.append(incoming_task_id)
    hint = ("在 CHANGELOG.md 的 [Unreleased] 分区为上述 id 各补一行语义化条目"
            "（判据真源：orchd.gitops.query.landed_task_ids_since）")
    if incoming_missing:
        hint += ("；本任务行须在任务分支内补齐（done 前置预检先于 verify 与自动提交，"
                 "未产生任何提交）")
    return [{
        "reason": "changelog_landed_task_missing",
        "path": str(scan.changelog_path),
        "task_ids": missing,
        "incoming_missing": incoming_missing,
        "message": (f"自 {scan.anchor} 以来 {len(missing)} 张落地任务未在 CHANGELOG.md 在册"
                    f"（不阻断合入，但须补记）：{', '.join(missing)}"),
        "hint": hint,
    }]