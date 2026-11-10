"""落地面片段汇总（S2a）：把 fragments 面的未装配片段写回主文件。

设计基线：``docs/shared-surfaces.md`` §8.1 / §9。

职责边界：

- **只做装配**：读注册表（:mod:`orchd.surfaces`）→ 校验全部片段 → 按分类
  顺序把缺失条目插入主文件对应节 → 原子写 → 默认删除已装配片段；
- **不碰 git**：不执行任何 git 写命令；只读 git 状态用于「非默认分支 /
  脏工作区」拒绝守卫；
- **幂等**：主文件里已有该任务条目则跳过（不重复插入、不重写既有行）；
- **可预览**：``dry_run=True`` 只返回计划，不写盘、不删片段；
- **串行**：仓库级排他锁（``.orchd/.surface-assemble.lock``）避免与并发
  merge / assemble 竞争。

``fragments`` 面之外的模式（generated / serial）不在此汇总：generated 由
生成器负责、serial 由 LLM 按串行纪律单写（见 §3 的适用性判据）。
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from orchd import surfaces
from orchd.errors import ErrorCode, OrchdError
from orchd.gitops import list_tracked_changes
from orchd.gitops.query import get_current_branch, get_default_branch
from orchd.lockfile import ExclusiveFileLock

__all__ = (
    "AssemblyPlan",
    "LOCK_REL",
    "assemble",
    "check_surfaces",
    "plan_surface",
    "select_targets",
)

#: 仓库级装配锁（相对 project_root）。
LOCK_REL = ".orchd/.surface-assemble.lock"

#: 任务分支前缀（禁止在任务分支上汇总）。
TASK_BRANCH_PREFIX = "task/"

#: 主文件缺失时的错误提示后缀。
_ASSEMBLE_HINT = "orchd surfaces assemble"


@dataclass(frozen=True)
class AssemblyPlan:
    """单面汇总计划（``plan_surface`` 的只读产物）。"""

    name: str
    mode: str
    path: str
    fragments: tuple[dict[str, Any], ...] = ()
    inserted: tuple[dict[str, str], ...] = ()
    already: tuple[str, ...] = ()
    degraded: str | None = None
    detail: str = ""
    new_text: str = field(default="", compare=False, repr=False)

    @property
    def changed(self) -> bool:
        """主文件是否会被本次汇总改变。"""
        return bool(self.inserted)


@dataclass(frozen=True)
class _FragmentView:
    """装配用片段视图（task_id 已从文件名解析，行已校验）。"""

    task_id: str
    category: str
    rel_path: str
    line: str


def _error(message: str, **details: Any) -> OrchdError:
    """构造装配域 ``E007``（details 恒带 ``rule`` 便于分流）。"""
    return OrchdError(ErrorCode.E007, message, [{"rule": "surface_assembly", **details}])


# ---------------------------------------------------------------------------
# 目标选择与守卫


def select_targets(
    specs: Mapping[str, surfaces.SurfaceSpec],
    names: Iterable[str] | None = None,
) -> list[surfaces.SurfaceSpec]:
    """选定要汇总的面。

    Args:
        specs: ``surfaces.load_surfaces`` 的注册表。
        names: 显式面名；为空时选全部 ``required=True`` 的 fragments 面。

    Returns:
        选中的 ``SurfaceSpec`` 列表（保持注册表顺序）。

    Raises:
        OrchdError: 显式面名不存在或不是 fragments 面。
    """
    wanted = [str(n) for n in (names or []) if str(n)]
    if wanted:
        selected: list[surfaces.SurfaceSpec] = []
        for name in wanted:
            spec = specs.get(name)
            if spec is None:
                raise _error(f"落地面不存在：{name}", surface=name)
            if spec.mode != "fragments":
                raise _error(
                    f"落地面 '{name}' 是 {spec.mode} 模式，不能用片段汇总",
                    surface=name,
                    mode=spec.mode,
                )
            if spec not in selected:
                selected.append(spec)
        return selected
    return [
        spec
        for spec in specs.values()
        if spec.mode == "fragments" and spec.required
    ]


def _read_master_declarations(project_root: Path) -> dict[str, str]:
    """claimed/in_review 任务对共享面的声明（路径→任务 id，经 master 单一真源解析）。"""
    from orchd.worktree import resolve_master_path_from_dir
    master_path = resolve_master_path_from_dir(project_root / ".orchd")
    try:
        doc = json.loads(master_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    declared: dict[str, str] = {}
    for task in doc.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if task.get("status") not in {"claimed", "in_review"}:
            continue
        task_id = str(task.get("id") or task.get("task_id") or "")
        for key in ("files_to_edit", "exempt_files"):
            for raw in task.get(key) or []:
                path = str(raw).strip().replace("\\", "/")
                if path:
                    declared[path] = task_id
    return declared


def _guard_declarations(project_root: Path, specs: Iterable[surfaces.SurfaceSpec]) -> None:
    """拒绝汇总仍被 claimed/in_review 任务声明的共享面（先迁移再汇总）。"""
    declared = _read_master_declarations(project_root)
    conflicts: list[dict[str, str]] = []
    for spec in specs:
        owners: set[str] = set()
        for path, task_id in declared.items():
            if path == spec.path:
                owners.add(task_id)
            elif spec.dir and (path == spec.dir or path.startswith(spec.dir + "/")):
                owners.add(task_id)
        if owners:
            conflicts.append(
                {
                    "surface": spec.name,
                    "path": spec.path,
                    "tasks": ",".join(sorted(owners)),
                }
            )
    if conflicts:
        raise _error(
            "以下共享面仍被 claimed/in_review 任务声明，请先迁移后再汇总："
            + "; ".join(
                f"{item['surface']}({item['tasks']})" for item in conflicts
            ),
            conflicts=conflicts,
            hint="orchd amend --task <task_id> --remove-files-to-edit <path>",
        )


def _guard_repo_state(project_root: Path) -> None:
    """装配环境守卫：非任务分支、默认分支、干净工作区。

    非 git 仓库 / git 不可用时放行（宿主可能没有 git；纯文件汇总仍安全）。
    """
    branch = get_current_branch(project_root)
    if branch is None:
        return
    if branch.startswith(TASK_BRANCH_PREFIX):
        raise _error(
            f"surface assemble 不得在任务分支执行（当前 {branch}）",
            branch=branch,
        )
    default = get_default_branch(project_root)
    if default and branch != default:
        raise _error(
            f"surface assemble 仅允许在默认分支执行（当前 {branch}，默认 {default}）",
            branch=branch,
            default=default,
        )
    tracked = list_tracked_changes(project_root)
    if tracked:
        raise _error(
            "surface assemble 要求工作区干净（存在已跟踪文件改动）",
            paths=list(tracked)[:20],
            hint="先提交或 orchd restore --path <path> 后重试",
        )


# ---------------------------------------------------------------------------
# 主文件读写（保留 EOL / BOM）
# ---------------------------------------------------------------------------


def _decode_surface(raw: bytes) -> tuple[str, bool, str]:
    """解码主文件，返回 (文本, 是否有 BOM, 换行符)。"""
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig") if has_bom else raw.decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text, has_bom, eol


def _encode_surface(text: str, has_bom: bool, eol: str) -> bytes:
    """按原 EOL / BOM 编码回字节（新行统一用原换行符）。"""
    body = text.replace("\r\n", "\n")
    if eol != "\n":
        body = body.replace("\n", eol)
    data = body.encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if has_bom else data


def _read_surface(path: Path, *, name: str) -> tuple[str, bool, str]:
    """读取主文件文本（缺失 / 不可读 → E007，附生成命令提示）。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _error(
            f"落地面文件不存在或不可读：{name}",
            surface=name,
            path=str(path),
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    try:
        return _decode_surface(raw)
    except UnicodeDecodeError as exc:
        raise _error(
            f"落地面文件不是合法 UTF-8：{name}",
            surface=name,
            path=str(path),
            detail=str(exc),
        ) from exc


def _write_surface_atomic(path: Path, data: bytes) -> None:
    """经 ledger 原子写入口落盘（tmp + fsync + ``_atomic_replace`` + 目录 fsync）。

    与 :func:`orchd.surfaces._atomic_write_text` 同源：不为落地面另造第二套原子写，
    裸 ``os.replace`` / 裸耐久写落点因此只减不增（见 ``tests/test_control_amend_atomic.py``
    的裸写登记表）。
    """
    from orchd.ledger import _write_bytes_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(path, data)


# ---------------------------------------------------------------------------
# 片段收集与插入计划
# ---------------------------------------------------------------------------


def _collect_fragments(
    project_root: Path, spec: surfaces.SurfaceSpec
) -> tuple[list[_FragmentView], list[str]]:
    """收集某 fragments 面的合法片段（非法片段以相对路径返回）。"""
    scan = surfaces.scan_fragments(project_root, spec)
    invalid = [str(p) for p in scan.invalid]
    fragments: list[_FragmentView] = []
    for fragment in scan.fragments.values():
        fragments.append(
            _FragmentView(
                task_id=fragment.task_id,
                category=fragment.category,
                rel_path=fragment.rel_path,
                line=fragment.line,
            )
        )
    fragments.sort(key=lambda f: f.task_id)
    return fragments, invalid


def _anchor_index(lines: list[str], anchor: str | None) -> int | None:
    """定位锚行（``## [Unreleased]``）的行号。"""
    if not anchor:
        return None
    target = anchor.strip()
    for index, line in enumerate(lines):
        if line.strip() == target:
            return index
    return None


def _region_end(lines: list[str], start: int) -> int:
    """锚区结束行号（下一个同级 ``## `` 标题，或文件末尾）。"""
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            return index
    return len(lines)


def _find_section(lines: list[str], start: int, end: int, header: str) -> int | None:
    """在锚区内定位 section 标题行号（``### Added``）。"""
    target = header.strip()
    for index in range(start + 1, end):
        if lines[index].strip() == target:
            return index
    return None


def _section_end(lines: list[str], header_index: int, region_end: int) -> int:
    """section 内容结束行号（下一个 ``###`` / ``##`` 标题，或锚区末尾）。"""
    for index in range(header_index + 1, region_end):
        stripped = lines[index].strip()
        if stripped.startswith("### ") or stripped.startswith("## "):
            return index
    return region_end


def _trim_blank_before(lines: list[str], position: int, floor: int) -> int:
    """把插入点上移到尾部空行之前（避免条目与空行/标题之间出现双空行）。"""
    while position - 1 > floor and not lines[position - 1].strip():
        position -= 1
    return position


def _section_order(spec: surfaces.SurfaceSpec, category: str) -> int:
    """分类在注册表 ``categories`` 中的顺序（未登记 → 末尾）。"""
    try:
        return spec.categories.index(category)
    except ValueError:
        return len(spec.categories)


def plan_surface(project_root: Path | str, spec: surfaces.SurfaceSpec) -> AssemblyPlan:
    """计算单面汇总计划（只读，不写盘、不删片段）。"""
    root = Path(project_root)
    if spec.mode != "fragments":
        raise _error(
            f"落地面 '{spec.name}' 不是 fragments 模式（{spec.mode}），无需汇总",
            surface=spec.name,
            mode=spec.mode,
        )
    text, _has_bom, _eol = _read_surface(root / spec.path, name=spec.name)
    fragments, invalid = _collect_fragments(root, spec)
    if invalid:
        raise _error(
            f"落地面 '{spec.name}' 存在非法片段，请先修正："
            + ", ".join(invalid[:10]),
            surface=spec.name,
            invalid=invalid,
        )
    lines = text.replace("\r\n", "\n").split("\n")
    anchor_index = _anchor_index(lines, spec.anchor)
    if anchor_index is None:
        raise _error(
            f"落地面 '{spec.name}' 缺少锚：{spec.anchor or '(未配置)'}",
            surface=spec.name,
            path=spec.path,
            hint="在 surfaces 注册表或宿主机配置中补 anchor",
        )
    region_end = _region_end(lines, anchor_index)
    already: list[str] = []
    pending: list[tuple[int, _FragmentView]] = []
    for fragment in fragments:
        if fragment.task_id in text:
            already.append(fragment.task_id)
            continue
        pending.append((_section_order(spec, fragment.category), fragment))
    if not pending:
        return AssemblyPlan(
            name=spec.name,
            mode=spec.mode,
            path=spec.path,
            fragments=tuple(_fragment_view_json(f) for f in fragments),
            inserted=(),
            already=tuple(already),
        )
    pending.sort(key=lambda item: (item[0], item[1].task_id))

    ops: list[tuple[int, int, list[str]]] = []
    inserted: list[dict[str, str]] = []
    for order, fragment in pending:
        header = spec.sections.get(fragment.category)
        if not header:
            raise _error(
                f"落地面 '{spec.name}' 缺少分类 {fragment.category!r} 的 section 映射",
                surface=spec.name,
                category=fragment.category,
            )
        section_index = _find_section(lines, anchor_index, region_end, header)
        if section_index is not None:
            position = _trim_blank_before(
                lines, _section_end(lines, section_index, region_end), section_index
            )
            ops.append((position, order, [fragment.line]))
        else:
            # 缺失的 section：按 categories 顺序插到锚区里第一个更靠后 section
            # 之前，否则追加到锚区末尾（保留空行分隔）。
            position = region_end
            for category in spec.categories[order + 1 :]:
                other_header = spec.sections.get(category)
                if not other_header:
                    continue
                other_index = _find_section(lines, anchor_index, region_end, other_header)
                if other_index is not None:
                    position = other_index
                    break
            position = _trim_blank_before(lines, position, anchor_index)
            ops.append((position, order, ["", header.strip(), fragment.line]))
        inserted.append(
            {"task_id": fragment.task_id, "category": fragment.category, "line": fragment.line}
        )

    # 同一位置的多组插入按分类顺序落位：先应用分类序靠后的，靠前的后插入；
    # 同位同序时按计划顺序倒序应用，保证最终文本与 task_id 定序一致。
    indexed_ops = [
        (position, order, index, payload)
        for index, (position, order, payload) in enumerate(ops)
    ]
    for position, _order, _index, payload in sorted(
        indexed_ops, key=lambda item: (item[0], item[1], item[2]), reverse=True
    ):
        lines[position:position] = payload
    new_text = "\n".join(lines)
    return AssemblyPlan(
        name=spec.name,
        mode=spec.mode,
        path=spec.path,
        fragments=tuple(_fragment_view_json(f) for f in fragments),
        inserted=tuple(inserted),
        already=tuple(already),
        new_text=new_text,
    )


def _fragment_view_json(fragment: _FragmentView) -> dict[str, Any]:
    """片段视图 → 可 JSON 序列化的计划条目。"""
    return {
        "task_id": fragment.task_id,
        "category": fragment.category,
        "path": fragment.rel_path,
    }


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------


def assemble(
    project_root: Path | str,
    *,
    names: Iterable[str] | None = None,
    dry_run: bool = False,
    keep_fragments: bool = False,
) -> dict[str, Any]:
    """汇总 fragments 面（默认处理全部 required 面）。

    Args:
        project_root: 项目根（含 ``.orchd``）。
        names: 显式面名列表；为空时选全部 required fragments 面。
        dry_run: 只返回计划，不写盘、不删片段。
        keep_fragments: 写盘成功后保留已装配片段（默认删除）。

    Returns:
        JSON 可序列化结果（``surfaces`` 列表逐面给出 inserted/already/removed）。

    Raises:
        OrchdError: 注册表非法、任务分支/脏工作区、面冲突、片段非法、主文件缺失等。
    """
    root = Path(project_root)
    specs = surfaces.load_surfaces(root)
    surfaces.validate_surfaces(specs)
    targets = select_targets(specs, names)
    if not targets:
        raise _error("没有可汇总的 fragments 面（注册表里 required fragments 为空）")
    _guard_declarations(root, targets)
    if not dry_run:
        _guard_repo_state(root)
    else:
        branch = get_current_branch(root)
        if branch and branch.startswith(TASK_BRANCH_PREFIX):
            raise _error(
                f"surface assemble（包含 --dry-run）不得在任务分支执行（当前 {branch}）",
                branch=branch,
            )

    lock_path = root / LOCK_REL
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = ExclusiveFileLock(lock_path)
    lock_acquired = False
    results: list[dict[str, Any]] = []
    try:
        if not dry_run:
            lock.acquire(blocking=True, timeout_s=30.0)
            lock_acquired = True
        # §8.1：先在锁内为全部目标面建计划并完成校验，任何一面失败都不得动其它面。
        plans = [plan_surface(root, spec) for spec in targets]
        removed_by_name: dict[str, list[str]] = {}
        if not dry_run:
            removed_by_name = _apply_plans(root, plans, keep_fragments=keep_fragments)
        for plan in plans:
            entry: dict[str, Any] = {
                "name": plan.name,
                "mode": plan.mode,
                "path": plan.path,
                "changed": plan.changed,
                "fragments": [dict(item) for item in plan.fragments],
                "inserted": [dict(item) for item in plan.inserted],
                "already": list(plan.already),
                "removed": list(removed_by_name.get(plan.name, [])),
            }
            if plan.changed and dry_run:
                entry["new_text"] = plan.new_text
            results.append(entry)
    finally:
        if lock_acquired:
            lock.release()
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "keep_fragments": bool(keep_fragments),
        "surfaces": results,
        "changed": any(item["changed"] for item in results),
        "hint": None if not dry_run else _ASSEMBLE_HINT,
    }


def _apply_plans(
    root: Path,
    plans: list[AssemblyPlan],
    *,
    keep_fragments: bool,
) -> dict[str, list[str]]:
    """写入全部已变更面并删除对应片段（含已记录、本次无新增的片段）；任一写入失败先回滚再抛出（§8.1）。"""
    originals: dict[Path, bytes] = {}
    written: list[Path] = []
    try:
        for plan in plans:
            if not plan.changed:
                continue
            path = root / plan.path
            originals[path] = path.read_bytes()
            _text, has_bom, eol = _read_surface(path, name=plan.name)
            _write_surface_atomic(path, _encode_surface(plan.new_text, has_bom, eol))
            written.append(path)
    except BaseException:
        for path in written:
            try:
                _write_surface_atomic(path, originals[path])
            except (OSError, OrchdError):
                continue
        raise
    removed_by_name: dict[str, list[str]] = {plan.name: [] for plan in plans}
    if keep_fragments:
        return removed_by_name
    for plan in plans:
        for fragment in plan.fragments:
            rel_path = str(fragment["path"])
            try:
                (root / rel_path).unlink()
            except OSError:
                continue
            removed_by_name[plan.name].append(rel_path)
    return removed_by_name


# ---------------------------------------------------------------------------
# 只读检查（surfaces check / release surface-check 共用）
# ---------------------------------------------------------------------------


def _generator_kind(spec: surfaces.SurfaceSpec) -> str:
    """生成器类别（file_census / docsgen / unknown），用于检查分派。"""
    logical = (spec.generator or "").strip().lower()
    if logical in {"file_census", "docsgen"}:
        return logical
    command = logical
    if "file_census" in command:
        return "file_census"
    if "docsgen" in command or "orchd docs" in command:
        return "docsgen"
    return "unknown"


def check_surfaces(
    project_root: Path | str,
    *,
    names: Iterable[str] | None = None,
    require_assembled: bool = False,
) -> dict[str, Any]:
    """只读校验注册表 / 片段 / 未装配状态（``surfaces check`` 核心）。

    Returns:
        含 ``ok`` / ``checks`` / ``errors`` / ``warnings`` 的 JSON 结果。
    """
    root = Path(project_root)
    specs = surfaces.load_surfaces(root)
    surfaces.validate_surfaces(specs)
    wanted = [str(n) for n in (names or []) if str(n)]
    targets = [
        spec for spec in specs.values() if not wanted or spec.name in wanted
    ]
    if wanted:
        for name in wanted:
            if name not in specs:
                raise _error(f"落地面不存在：{name}", surface=name)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for spec in targets:
        item: dict[str, Any] = {
            "name": spec.name,
            "mode": spec.mode,
            "path": spec.path,
            "required": spec.required,
        }
        if spec.mode == "fragments":
            scan = surfaces.scan_surface(root, spec)
            fragments, invalid = _collect_fragments(root, spec)
            item.update(
                {
                    "fragments": len(fragments),
                    "fragment_ids": list(scan.fragment_ids),
                    "unassembled": list(scan.unassembled),
                    "rows": scan.rows,
                    "degradation": scan.degradation,
                }
            )
            if invalid:
                errors.append(
                    {
                        "surface": spec.name,
                        "reason": "invalid_fragment",
                        "paths": invalid[:20],
                        "hint": "修正片段文件名/内容后重试",
                    }
                )
            if spec.required and scan.degradation:
                errors.append(
                    {
                        "surface": spec.name,
                        "reason": "surface_unreadable",
                        "path": spec.path,
                        "degradation": scan.degradation,
                        "detail": scan.detail,
                        "hint": f"required 面必须可读：补 {spec.path} 后重试",
                    }
                )
            if require_assembled and scan.unassembled:
                errors.append(
                    {
                        "surface": spec.name,
                        "reason": "unassembled",
                        "task_ids": list(scan.unassembled),
                        "hint": _ASSEMBLE_HINT,
                    }
                )
        elif spec.mode == "generated":
            kind = _generator_kind(spec)
            item["generator"] = spec.generator
            item["dispatch"] = kind
            if kind == "unknown":
                warnings.append(
                    {
                        "surface": spec.name,
                        "reason": "unknown_generator",
                        "generator": spec.generator,
                        "message": "未登记生成器，不代行检查（只报告）",
                    }
                )
        else:
            item["serial"] = True
        checks.append(item)
    ok = not errors
    return {
        "ok": ok,
        "require_assembled": bool(require_assembled),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "hint": None if ok else _ASSEMBLE_HINT,
    }
