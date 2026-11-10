"""共享落地面（shared surface）注册表与片段协议。

设计基线见 ``docs/shared-surfaces.md``（task-shared-surface-engine 首落）：
「同一个文件被几十个任务串行声明」的根因是**任务直接编辑共享文件**。解法不是放宽
冲突检测，而是把共享文件改成「落地面」：引擎在 done 期代任务写**片段**，发布前统一
装配为主文件；生成面（由脚本产出的文件）与串行面（必须整文件声明的少数文件）只登记、
不拆分。

本模块是引擎侧唯一真源：

- **注册表**：内置默认面 + 宿主 ``.orchd/shared/surfaces.json`` 覆盖 + 兼容旧
  ``guards.docs.changelog``（宿主带旧配置升级不失效）；三档模式
  ``fragments`` / ``generated`` / ``serial``；
- **片段协议**：``<dir>/<task_id>.<category>.md``，内容为单条
  ``- <task_id>：<summary>``（一行一变更点、≤150 字符、禁全角分号），由引擎渲染/校验/
  写盘，LLM 不手写片段；
- **扫描判据**：:func:`recorded` / :func:`count_rows` / :func:`scan_surface`，供 done 期
  E039 预检（``orchd.onboard.lifecycle.guards._guard_landing_precheck``）、merge 后审计
  （``orchd.report.audit.changelog_landing_scan``）与宿主 guard ``docs`` 共用同源口径。

纪律：注册表非法一律 ``E007`` 报错（fail-closed，不静默降级）；写盘只发生在
:func:`write_fragment` / :func:`write_entries`（done 期）与 S2a 的 ``surfaces assemble``；
片段目录由引擎管理，其余进程只读扫描。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError

#: 三档落地面模式（顺序即文档顺序）。
SURFACE_MODES: tuple[str, ...] = ("fragments", "generated", "serial")

#: 生成器逻辑 id → 引擎内置生成命令。注册表只写逻辑 id，宿主不能执行任意命令。
GENERATOR_COMMANDS: dict[str, str] = {
    "file_census": "python scripts/file_census.py --write",
    "docsgen": "python -m orchd docs",
}


def generator_command(generator: str | None) -> str | None:
    """把注册表里的生成器逻辑 id 映射为引擎内置命令（未知 id 返回 None）。"""
    if not generator:
        return None
    return GENERATOR_COMMANDS.get(generator.strip())

#: 默认落地面名（CHANGELOG）。
DEFAULT_SURFACE_NAME = "changelog"

#: 宿主覆盖注册表的仓库相对路径。
SURFACE_CONFIG_REL = ".orchd/shared/surfaces.json"

#: 宿主注册表 schema 版本。
SCHEMA_VERSION = 1

#: 片段单行最大字符数（含 ``- task-xxx：`` 前缀）。
MAX_ENTRY_CHARS = 150

#: 片段文件编码（UTF-8，无 BOM）。
FRAGMENT_ENCODING = "utf-8"

#: 默认 CHANGELOG 分类（与 Keep a Changelog 的六个 section 对齐）。
_CHANGELOG_CATEGORIES: tuple[str, ...] = (
    "added",
    "changed",
    "deprecated",
    "removed",
    "fixed",
    "security",
)

#: 分类 → section 标题（装配时按此分组；S2a 使用）。
_CHANGELOG_SECTIONS: Mapping[str, str] = {
    "added": "### Added",
    "changed": "### Changed",
    "deprecated": "### Deprecated",
    "removed": "### Removed",
    "fixed": "### Fixed",
    "security": "### Security",
}

#: 片段行模板（``：`` 为全角冒号，与 CHANGELOG 现存行一致）。
_DEFAULT_ENTRY_TEMPLATE = "- {task_id}：{summary}"

#: 引擎内置默认落地面（宿主可用 ``.orchd/shared/surfaces.json`` 覆盖/追加）。
DEFAULT_SURFACES_RAW: Mapping[str, Mapping[str, Any]] = {
    DEFAULT_SURFACE_NAME: {
        "mode": "fragments",
        "path": "CHANGELOG.md",
        "dir": "changelog.d",
        "filename": "{task_id}.{category}.md",
        "categories": _CHANGELOG_CATEGORIES,
        "entry": _DEFAULT_ENTRY_TEMPLATE,
        "anchor": "## [Unreleased]",
        "sections": _CHANGELOG_SECTIONS,
    },
    "file_census": {
        "mode": "generated",
        "path": "file-census-baseline.json",
        "generator": "file_census",
    },
    "dependency_graph": {
        "mode": "generated",
        "path": "docs/_generated/dependency-graph.md",
        "generator": "docsgen",
    },
    "conventions": {"mode": "serial", "path": ".orchd/shared/conventions.md", "required": False},
    "user_manual": {"mode": "serial", "path": "docs/user-manual.md", "required": False},
    "skill": {"mode": "serial", "path": ".orchd/SKILL.md", "required": False},
}

#: 片段/注册表字段允许键（未知键 fail-closed，防拼写漂移静默生效）。
_SPEC_KEYS = frozenset(
    {
        "mode",
        "path",
        "dir",
        "filename",
        "categories",
        "entry",
        "anchor",
        "sections",
        "generator",
        "required",
    }
)

#: ``--surface-entry`` 允许键。
_ENTRY_KEYS = frozenset({"surface", "category", "summary"})

#: 任务号允许形态（做文件名前先校验，防路径穿越）。
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class SurfaceSpec:
    """一个落地面的注册表规约（不可变）。

    Attributes:
        name: 面名（注册表键）。
        mode: ``fragments`` / ``generated`` / ``serial``。
        path: 主文件仓库相对 POSIX 路径。
        dir: 片段目录（仅 ``fragments`` 模式；不得与 ``path`` 相同或互相包含）。
        filename: 片段文件模板，必须含 ``{task_id}`` 与 ``{category}``。
        categories: 片段分类白名单（``fragments`` 模式必填）。
        entry: 片段行模板，必须含 ``{task_id}`` 与 ``{summary}``。
        anchor: 装配锚（主文件内插入位置，S2a 使用）。
        sections: 分类 → section 标题映射（S2a 使用）。
        generator: 生成器的逻辑 id（``generated`` 模式必填，由引擎映射到内置命令）。
        required: 面是否强制存在（False 表示宿主可缺省）。
    """

    name: str
    mode: str
    path: str
    dir: str | None = None
    filename: str = "{task_id}.{category}.md"
    categories: tuple[str, ...] = ()
    entry: str = _DEFAULT_ENTRY_TEMPLATE
    anchor: str | None = None
    sections: Mapping[str, str] = field(default_factory=dict)
    generator: str | None = None
    required: bool = True


@dataclass(frozen=True)
class Fragment:
    """一个已通过校验的片段文件。"""

    task_id: str
    category: str
    rel_path: str
    line: str
    text: str


@dataclass(frozen=True)
class FragmentScan:
    """片段目录扫描结果（``invalid`` 为文件名可识别但内容非法的相对路径）。"""

    surface: str
    dir_rel: str
    fragments: Mapping[str, Fragment] = field(default_factory=dict)
    invalid: tuple[str, ...] = ()


@dataclass(frozen=True)
class SurfaceScan:
    """单面扫描结果（读主文件 + 扫片段；``degradation`` 非空表示降级留痕）。"""

    name: str
    mode: str
    path: str
    text: str = ""
    fragments: Mapping[str, Fragment] = field(default_factory=dict)
    fragment_ids: tuple[str, ...] = ()
    unassembled: tuple[str, ...] = ()
    rows: int = 0
    degradation: str | None = None
    detail: str = ""
    invalid: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    degraded: bool = False


@dataclass(frozen=True)
class SurfaceEntry:
    """done 期一条待写片段（已校验：面/任务/分类/摘要/路径/行）。"""

    surface: str
    task_id: str
    category: str
    summary: str
    rel_path: str
    line: str


# ---------------------------------------------------------------------------
# 注册表读取与校验
# ---------------------------------------------------------------------------


def _error(message: str, **details: Any) -> OrchdError:
    """构造注册表/片段域的 ``E007``（details 恒带 ``rule`` 便于分流）。"""
    return OrchdError(ErrorCode.E007, message, [{"rule": "surface_config", **details}])


def _normalize_rel(value: Any, *, field_name: str, which: str) -> str:
    """规范化仓库相对路径（POSIX、禁绝对路径/盘符/``..``/空段）。"""
    if not isinstance(value, str) or not value.strip():
        raise _error(f"落地面 '{which}' 的 {field_name} 必须是非空字符串")
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        raise _error(f"落地面 '{which}' 的 {field_name} 必须是仓库相对路径：{value!r}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise _error(f"落地面 '{which}' 的 {field_name} 非法（空或含 '..'）：{value!r}")
    return "/".join(parts)


def _coerce_categories(value: Any, *, which: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise _error(f"落地面 '{which}' 的 categories 必须是数组，不是字符串")
    if not isinstance(value, (list, tuple)):
        raise _error(f"落地面 '{which}' 的 categories 必须是数组")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _error(f"落地面 '{which}' 的 categories 含非法项：{item!r}")
        name = item.strip()
        if "/" in name or "\\" in name or name in (".", ".."):
            raise _error(
                f"落地面 '{which}' 的 categories 不能含路径分隔符：{name!r}"
            )
        if name in out:
            raise _error(f"落地面 '{which}' 的 categories 重复：{name!r}")
        out.append(name)
    if not out:
        raise _error(f"落地面 '{which}' 的 categories 不能为空（fragments 模式必填）")
    return tuple(out)


def _coerce_sections(value: Any, *, which: str) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _error(f"落地面 '{which}' 的 sections 必须是对象")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
            raise _error(f"落地面 '{which}' 的 sections 含非法项：{key!r}")
        out[key] = item.strip()
    return out


def _spec_from_raw(
    name: str,
    raw: Mapping[str, Any],
    base: Mapping[str, Any] | None = None,
) -> SurfaceSpec:
    """由原始 dict（叠加 base 默认值）构造并校验一个 :class:`SurfaceSpec`。"""
    if not isinstance(name, str) or not name.strip():
        raise _error("落地面名必须是非空字符串")
    if not isinstance(raw, Mapping):
        raise _error(f"落地面 '{name}' 的定义必须是对象")
    merged: dict[str, Any] = dict(base or {})
    merged.update(raw)
    unknown = sorted(set(merged) - _SPEC_KEYS)
    if unknown:
        raise _error(
            f"落地面 '{name}' 存在未知字段：{', '.join(unknown)}"
            f"（已知：{', '.join(sorted(_SPEC_KEYS))}）"
        )
    mode = merged.get("mode")
    if mode not in SURFACE_MODES:
        raise _error(
            f"落地面 '{name}' 的 mode 非法：{mode!r}（可选：{', '.join(SURFACE_MODES)}）"
        )
    path = _normalize_rel(merged.get("path"), field_name="path", which=name)
    dir_rel: str | None = None
    if merged.get("dir") is not None:
        dir_rel = _normalize_rel(merged.get("dir"), field_name="dir", which=name)
    filename = merged.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise _error(f"落地面 '{name}' 的 filename 必须是字符串")
    entry = merged.get("entry")
    if entry is not None and not isinstance(entry, str):
        raise _error(f"落地面 '{name}' 的 entry 必须是字符串")
    anchor = merged.get("anchor")
    if anchor is not None and not isinstance(anchor, str):
        raise _error(f"落地面 '{name}' 的 anchor 必须是字符串")
    generator = merged.get("generator")
    if generator is not None and (not isinstance(generator, str) or not generator.strip()):
        raise _error(f"落地面 '{name}' 的 generator 必须是非空字符串")
    required = merged.get("required", True)
    if not isinstance(required, bool):
        raise _error(f"落地面 '{name}' 的 required 必须是布尔值")
    categories = (
        _coerce_categories(merged.get("categories"), which=name)
        if merged.get("categories") is not None
        else ()
    )
    return SurfaceSpec(
        name=name.strip(),
        mode=mode,
        path=path,
        dir=dir_rel,
        filename=filename or "{task_id}.{category}.md",
        categories=categories,
        entry=entry or _DEFAULT_ENTRY_TEMPLATE,
        anchor=anchor,
        sections=_coerce_sections(merged.get("sections"), which=name),
        generator=generator.strip() if isinstance(generator, str) else None,
        required=required,
    )


def default_surfaces() -> dict[str, SurfaceSpec]:
    """返回引擎内置默认注册表（每次新副本，调用方可安全修改返回值）。"""
    return {
        name: _spec_from_raw(name, raw, None) for name, raw in DEFAULT_SURFACES_RAW.items()
    }


def validate_surfaces(specs: Mapping[str, SurfaceSpec]) -> None:
    """整体校验注册表（模式字段、路径包含关系、模板占位符、重复路径）。"""
    if not isinstance(specs, Mapping) or not specs:
        raise _error("落地面注册表不能为空")
    seen_paths: dict[str, str] = {}
    for name, spec in specs.items():
        if not isinstance(spec, SurfaceSpec):
            raise _error(f"落地面 '{name}' 不是 SurfaceSpec：{type(spec).__name__}")
        if spec.name != name:
            raise _error(f"落地面键与自身 name 不一致：'{name}' vs '{spec.name}'")
        if spec.mode not in SURFACE_MODES:
            raise _error(
                f"落地面 '{name}' 的 mode 非法：{spec.mode!r}"
                f"（可选：{', '.join(SURFACE_MODES)}）"
            )
        normalized = _normalize_rel(spec.path, field_name="path", which=name)
        if normalized != spec.path:
            raise _error(f"落地面 '{name}' 的 path 未规范化：{spec.path!r}")
        owner = seen_paths.get(spec.path)
        if owner is not None:
            raise _error(f"落地面 '{name}' 与 '{owner}' 声明了同一路径：{spec.path}")
        seen_paths[spec.path] = name
        if spec.mode == "fragments":
            if not spec.dir:
                raise _error(f"落地面 '{name}' 是 fragments 模式，必须声明 dir")
            if not spec.categories:
                raise _error(f"落地面 '{name}' 是 fragments 模式，必须声明 categories")
            if spec.dir == spec.path or spec.dir.startswith(spec.path + "/"):
                raise _error(f"落地面 '{name}' 的 dir 不得位于 path 内：{spec.dir} vs {spec.path}")
            if spec.path.startswith(spec.dir + "/"):
                raise _error(
                    f"落地面 '{name}' 的 path 不得位于 dir 内：{spec.path} vs {spec.dir}"
                )
            if "{task_id}" not in spec.filename or "{category}" not in spec.filename:
                raise _error(
                    f"落地面 '{name}' 的 filename 必须同时含 {{task_id}} 与 {{category}}："
                    f"{spec.filename!r}"
                )
            if "/" in spec.filename or "\\" in spec.filename:
                raise _error(f"落地面 '{name}' 的 filename 不得含路径分隔符：{spec.filename!r}")
            if "{summary}" not in spec.entry:
                raise _error(f"落地面 '{name}' 的 entry 必须含 {{summary}}：{spec.entry!r}")
            if "{task_id}" not in spec.entry:
                raise _error(f"落地面 '{name}' 的 entry 必须含 {{task_id}}：{spec.entry!r}")
            unknown_sections = sorted(set(spec.sections) - set(spec.categories))
            if unknown_sections:
                raise _error(
                    f"落地面 '{name}' 的 sections 含未知分类：{', '.join(unknown_sections)}"
                )
        elif spec.mode == "generated":
            if not spec.generator:
                raise _error(f"落地面 '{name}' 是 generated 模式，必须声明 generator")
        # serial：无额外必填字段。


def _read_json_object(path: Path, *, what: str) -> Mapping[str, Any]:
    try:
        raw_text = path.read_text(encoding=FRAGMENT_ENCODING)
    except (OSError, UnicodeDecodeError) as exc:
        raise _error(f"{what}不可读：{path}", path=str(path), detail=str(exc)) from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise _error(f"{what}不是合法 JSON：{path}", path=str(path), detail=str(exc)) from exc
    if not isinstance(payload, Mapping):
        raise _error(f"{what}必须是 JSON 对象：{path}", path=str(path))
    return payload


def _with_config_path(
    exc: OrchdError,
    config_path: Path,
    *,
    surface: str | None = None,
) -> OrchdError:
    """给注册表校验错误补 ``path``（可选 ``surface``）细节，便于按条目定位。"""
    details = [dict(item) for item in (exc.details or ()) if isinstance(item, Mapping)]
    if not details:
        details = [{"rule": "surface_config"}]
    for item in details:
        item.setdefault("path", str(config_path))
        if surface is not None:
            item.setdefault("surface", surface)
    return OrchdError(exc.code, exc.message, details)


def _host_surfaces(project_root: Path, config_rel: str) -> Mapping[str, Mapping[str, Any]]:
    """读取宿主覆盖注册表；缺失时返回空映射（内置默认即生效）。"""
    path = project_root / config_rel
    if not path.exists():
        return {}
    payload = _read_json_object(path, what="宿主落地面注册表")
    version = payload.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise _error(
            f"宿主落地面注册表 schema_version 不受支持：{version!r}（期望 {SCHEMA_VERSION}）",
            path=str(path),
        )
    surfaces = payload.get("surfaces")
    if not isinstance(surfaces, Mapping):
        raise _error("宿主落地面注册表缺少 surfaces 对象", path=str(path))
    out: dict[str, Mapping[str, Any]] = {}
    for name, raw in surfaces.items():
        if not isinstance(name, str) or not name.strip():
            raise _error("宿主落地面注册表的键必须是非空字符串", path=str(path))
        if not isinstance(raw, Mapping):
            raise _error(f"宿主落地面 '{name}' 的定义必须是对象", path=str(path))
        out[name] = raw
    return out


def _default_fragment_dir(changelog_rel: str) -> str:
    """由 CHANGELOG 路径推导片段目录：``<父目录>/changelog.d``（无父目录时 ``changelog.d``）。"""
    parent = _path_parent(changelog_rel)
    return f"{parent}/changelog.d" if parent else "changelog.d"


def _path_parent(rel: str) -> str:
    if "/" not in rel:
        return ""
    return rel.rsplit("/", 1)[0]


def _legacy_changelog_rel(project_root: Path) -> str | None:
    """读取宿主旧口径 ``guards.docs.changelog``（缺失/非法一律返回 None，由 guard 侧留痕）。"""
    try:
        from orchd.guards import load_guards_config
        from orchd.guards.docs import DocsGuardConfigError, parse_policy
    except ImportError:  # pragma: no cover - 守卫包缺席属残缺安装
        return None
    try:
        config = load_guards_config(project_root)
    except Exception:  # noqa: BLE001 - 守卫配置是否合法由 guards 域报，不阻断落地面读取
        return None
    # 旧口径只在**显式声明**时参与合并（设计 §4.1 优先级）：
    # guards.json 缺文件时 load_guards_config 会返回全 off 视图（entries 齐全但
    # 全是默认值），若照单全收会把默认 CHANGELOG.md 当成宿主显式口径，从而与
    # 仅配置 .orchd/shared/surfaces.json 的宿主冲突（spec 审查 AC1）。
    if not config.present or "docs" not in config.declared:
        return None
    for entry in config.entries:
        if entry.name != "docs":
            continue
        if "changelog" not in entry.options:
            return None
        try:
            policy = parse_policy(entry.options)
        except DocsGuardConfigError:
            return None
        return policy.changelog_rel
    return None


def load_surfaces(
    project_root: Path | str,
    *,
    changelog_rel: str | None = None,
    config_rel: str = SURFACE_CONFIG_REL,
) -> dict[str, SurfaceSpec]:
    """加载并校验落地面注册表（默认 + 宿主覆盖 + 旧 ``guards.docs.changelog`` 兼容）。

    Args:
        project_root: 仓库根。
        changelog_rel: 调用方显式指定的 CHANGELOG 路径（覆盖注册表口径；用于宿主
            guard 与审计传参）。
        config_rel: 宿主注册表相对路径（测试可注入）。

    Returns:
        面名 → :class:`SurfaceSpec` 的字典（含全部默认面）。

    Raises:
        OrchdError: ``E007`` 注册表非法（含新旧配置冲突）。
    """
    root = Path(project_root)
    config_path = root / config_rel
    raw_surfaces = _host_surfaces(root, config_rel)
    specs = default_surfaces()
    for name, raw in raw_surfaces.items():
        try:
            specs[name] = _spec_from_raw(name, raw, DEFAULT_SURFACES_RAW.get(name))
        except OrchdError as exc:
            raise _with_config_path(exc, config_path, surface=name) from exc
    host_changelog = raw_surfaces.get(DEFAULT_SURFACE_NAME)
    if (
        isinstance(host_changelog, Mapping)
        and host_changelog.get("path") is not None
        and "dir" not in host_changelog
    ):
        changelog = specs[DEFAULT_SURFACE_NAME]
        derived_dir = _default_fragment_dir(changelog.path)
        if changelog.mode == "fragments" and changelog.dir != derived_dir:
            specs[DEFAULT_SURFACE_NAME] = _spec_replaced(changelog, dir=derived_dir)
    legacy = _legacy_changelog_rel(root)
    if legacy is not None:
        legacy = _normalize_rel(legacy, field_name="changelog", which="guards.docs")
        host_changelog = raw_surfaces.get(DEFAULT_SURFACE_NAME, {})
        host_explicit_path = (
            host_changelog.get("path") if isinstance(host_changelog, Mapping) else None
        )
        if host_explicit_path is not None:
            host_path = _normalize_rel(
                host_explicit_path, field_name="path", which=DEFAULT_SURFACE_NAME
            )
            if host_path != legacy:
                raise _error(
                    "宿主落地面注册表与旧 guards.docs.changelog 声明的 CHANGELOG 路径冲突："
                    f"{host_path!r} vs {legacy!r}",
                    path=str(root / config_rel),
                )
        changelog = specs[DEFAULT_SURFACE_NAME]
        if changelog.mode == "fragments" and changelog.path != legacy:
            dir_rel = changelog.dir
            if not (isinstance(host_changelog, Mapping) and "dir" in host_changelog):
                dir_rel = _default_fragment_dir(legacy)
            specs[DEFAULT_SURFACE_NAME] = _spec_replaced(changelog, path=legacy, dir=dir_rel)
    if changelog_rel is not None:
        explicit = _normalize_rel(changelog_rel, field_name="changelog_rel", which="caller")
        changelog = specs[DEFAULT_SURFACE_NAME]
        if changelog.mode == "fragments" and changelog.path != explicit:
            host_changelog = raw_surfaces.get(DEFAULT_SURFACE_NAME, {})
            dir_rel = changelog.dir
            if not (isinstance(host_changelog, Mapping) and "dir" in host_changelog):
                dir_rel = _default_fragment_dir(explicit)
            specs[DEFAULT_SURFACE_NAME] = _spec_replaced(changelog, path=explicit, dir=dir_rel)
    try:
        validate_surfaces(specs)
    except OrchdError as exc:
        raise _with_config_path(exc, config_path) from exc
    return specs


def _spec_replaced(spec: SurfaceSpec, **changes: Any) -> SurfaceSpec:
    """``dataclasses.replace`` 的薄包装（集中一处，便于类型检查）。"""
    from dataclasses import replace

    return replace(spec, **changes)


def surface(project_root: Path | str, name: str = DEFAULT_SURFACE_NAME) -> SurfaceSpec:
    """取单个落地面规约（未知面名 → ``E007``）。"""
    specs = load_surfaces(project_root)
    spec = specs.get(name)
    if spec is None:
        raise _error(
            f"未知落地面：{name!r}（已注册：{', '.join(sorted(specs))}）",
            surface=name,
        )
    return spec


# ---------------------------------------------------------------------------
# 片段路径 / 渲染 / 校验 / 写盘
# ---------------------------------------------------------------------------


def _check_task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise _error(f"非法任务号，不能用于片段文件名：{task_id!r}", task_id=str(task_id))
    return task_id


def fragment_path(spec: SurfaceSpec, task_id: str, category: str) -> str:
    """返回片段文件仓库相对路径（``<dir>/<filename 渲染>``）。

    验收 API 名（AC2）为 ``fragment_path``；旧名 ``fragment_rel_path`` 保留为别名。
    """
    if spec.mode != "fragments":
        raise _error(f"落地面 '{spec.name}' 不是 fragments 模式：{spec.mode}", surface=spec.name)
    tid = _check_task_id(task_id)
    cat = _check_category(spec, category)
    if not spec.dir:
        raise _error(f"落地面 '{spec.name}' 缺少 dir", surface=spec.name)
    filename = spec.filename.replace("{task_id}", tid).replace("{category}", cat)
    return f"{spec.dir}/{filename}"


# 旧名兼容（内部与既有调用仍可用；验收面以上方 canonical 名为准）。
fragment_rel_path = fragment_path


def _check_category(spec: SurfaceSpec, category: Any) -> str:
    if not isinstance(category, str) or category not in spec.categories:
        raise _error(
            f"落地面 '{spec.name}' 的分类非法：{category!r}"
            f"（可选：{', '.join(spec.categories)}）",
            surface=spec.name,
            category=str(category),
        )
    return category


def normalize_summary(summary: Any) -> str:
    """规整摘要：去首尾空白、内部空白折叠为单空格（片段必须单行）。"""
    if summary is None:
        return ""
    return " ".join(str(summary).split())


def render_line(spec: SurfaceSpec, task_id: str, summary: str) -> str:
    """按 entry 模板渲染单行片段（``- <task_id>：<summary>``）。

    验收 API 名（AC2）为 ``render_line``；旧名 ``render_entry`` 保留为别名。
    """
    tid = _check_task_id(task_id)
    text = normalize_summary(summary)
    if not text:
        raise _error(f"落地面 '{spec.name}' 的摘要不能为空", surface=spec.name, task_id=tid)
    return spec.entry.replace("{task_id}", tid).replace("{summary}", text)


def truncate_summary(spec: SurfaceSpec, task_id: str, summary: str) -> str:
    """把（默认来源的）摘要安全截断到片段行 ≤150 字符，供 ``done`` 兜底摘要使用。

    显式 ``--changelog-summary`` 不做截断（超长直接 ``E007``）；只有由
    ``--changes`` 首行 / 任务名派生的默认摘要才走本函数（AC4「默认 summary 可安全截断」）。
    """
    tid = _check_task_id(task_id)
    text = normalize_summary(summary)
    if not text:
        return text
    prefix = spec.entry.split("{summary}", 1)[0].replace("{task_id}", tid)
    budget = MAX_ENTRY_CHARS - len(prefix)
    if budget <= 0:
        return text[:0]
    if len(text) <= budget:
        return text
    return text[:budget].rstrip()


def surface_descriptor(spec: SurfaceSpec) -> dict[str, Any]:
    """落地面描述（审计 / 报告用的稳定只读投影）。"""
    return {
        "name": spec.name,
        "mode": spec.mode,
        "path": spec.path,
        "dir": spec.dir or "",
    }


def validate_fragment(spec: SurfaceSpec, task_id: str, line: str) -> None:
    """校验单行片段：单行、模板前缀、≤150 字符、禁全角分号。

    验收 API 名（AC2）为 ``validate_fragment``；旧名 ``validate_entry`` 保留为别名。
    """
    if not isinstance(line, str) or not line:
        raise _error("片段内容不能为空", surface=spec.name, task_id=task_id)
    if "\n" in line or "\r" in line:
        raise _error(
            "片段必须单行（一行一变更点）", surface=spec.name, task_id=task_id, detail=line[:80]
        )
    if "；" in line:
        raise _error(
            "片段不得使用全角分号拼接多个变更点（一行一变更点）",
            surface=spec.name,
            task_id=task_id,
            detail=line[:80],
        )
    prefix = spec.entry.split("{summary}", 1)[0].replace("{task_id}", task_id)
    if not line.startswith(prefix):
        raise _error(
            f"片段行不符合模板前缀 {prefix!r}",
            surface=spec.name,
            task_id=task_id,
            detail=line[:80],
        )
    if len(line) > MAX_ENTRY_CHARS:
        raise _error(
            f"片段行超过 {MAX_ENTRY_CHARS} 字符上限：{len(line)}",
            surface=spec.name,
            task_id=task_id,
            detail=line[:80],
        )


# 旧名兼容（内部与既有调用仍可用；验收面以上方 canonical 名为准）。
render_entry = render_line
validate_entry = validate_fragment


def _atomic_write_text(path: Path, text: str) -> None:
    """经 ledger 原子写入口落盘（tmp + fsync + ``_atomic_replace`` + 目录 fsync）。

    片段与主文件的写盘统一走 ``orchd.ledger._write_text_atomic``，不再自持裸
    ``os.replace`` / 裸耐久写实现：该入口带 Windows 句柄争用有界重试，且已登记在
    ``tests/test_control_amend_atomic.py`` 的裸写登记面内（收敛而非新增落点）。
    """
    from orchd.ledger import _write_text_atomic

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(path, text)


def write_fragment(
    project_root: Path | str,
    spec: SurfaceSpec,
    task_id: str,
    category: str,
    summary: str,
) -> dict[str, Any]:
    """写一条片段（幂等：同内容视为已存在；不同内容 → ``E007``）。

    Returns:
        ``{"path": 仓库相对路径, "created": 是否新写, "line": 片段行}``。
    """
    rel_path = fragment_rel_path(spec, task_id, category)
    line = render_entry(spec, task_id, summary)
    validate_entry(spec, task_id, line)
    root = Path(project_root)
    path = root / rel_path
    text = line + "\n"
    # 同任务同 fragments 面唯一（设计 §5）：同内容幂等，跨分类/不同内容 E007。
    existing = _existing_fragment_for(root, spec, task_id)
    if existing is not None:
        if existing.category == category and existing.line.strip() == line.strip():
            return {"path": existing.rel_path, "created": False, "line": line}
        if existing.category == category:
            raise _error(
                f"片段已存在且内容不同：{existing.rel_path}",
                path=existing.rel_path,
                existing=existing.line.strip(),
                desired=line,
            )
        raise _error(
            f"同一任务在每个落地面只能有一条片段：{task_id} 已有 {existing.rel_path}"
            f"（分类 {existing.category}），不能再写 {rel_path}（分类 {category}）",
            path=rel_path,
            existing=existing.rel_path,
            existing_category=existing.category,
            desired_category=category,
        )
    if path.exists():
        try:
            current = path.read_text(encoding=FRAGMENT_ENCODING)
        except (OSError, UnicodeDecodeError) as exc:
            raise _error(f"既有片段不可读：{rel_path}", path=rel_path, detail=str(exc)) from exc
        if current == text:
            return {"path": rel_path, "created": False, "line": line}
        raise _error(
            f"片段已存在且内容不同：{rel_path}",
            path=rel_path,
            existing=current.strip(),
            desired=line,
        )
    _atomic_write_text(path, text)
    return {"path": rel_path, "created": True, "line": line}


def prepare_entries(
    project_root: Path | str,
    task_id: str,
    entries: Sequence[Mapping[str, Any]] | None,
    *,
    default_surface: str = DEFAULT_SURFACE_NAME,
) -> list[SurfaceEntry]:
    """校验 done 期 ``--surface-entry`` 列表（全部校验通过才返回，先校验后写盘）。

    Raises:
        OrchdError: ``E007`` 未知面/非 fragments 模式/分类非法/摘要为空/同面重复。
    """
    if not entries:
        return []
    root = Path(project_root)
    specs = load_surfaces(root)
    tid = _check_task_id(task_id)
    prepared: list[SurfaceEntry] = []
    seen_surfaces: set[str] = set()
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise _error("--surface-entry 必须是 JSON 对象", task_id=tid)
        unknown = sorted(set(raw) - _ENTRY_KEYS)
        if unknown:
            raise _error(
                f"--surface-entry 存在未知字段：{', '.join(unknown)}"
                f"（已知：{', '.join(sorted(_ENTRY_KEYS))}）",
                task_id=tid,
            )
        name = raw.get("surface") or default_surface
        spec = specs.get(name)
        if spec is None:
            raise _error(
                f"未知落地面：{name!r}（已注册：{', '.join(sorted(specs))}）",
                surface=str(name),
                task_id=tid,
            )
        if spec.mode != "fragments":
            raise _error(
                f"落地面 '{name}' 是 {spec.mode} 模式，不接受 --surface-entry"
                f"（片段只属于 fragments 模式）",
                surface=name,
                task_id=tid,
            )
        category = raw.get("category") or "changed"
        summary = normalize_summary(raw.get("summary"))
        if not summary:
            raise _error(
                f"落地面 '{name}' 缺少 summary（--surface-entry 或 --changelog-summary）",
                surface=name,
                task_id=tid,
            )
        if name in seen_surfaces:
            raise _error(
                f"同一任务在每个落地面只能有一条片段（surface '{name}' 重复）",
                surface=name,
                task_id=tid,
            )
        seen_surfaces.add(str(name))
        rel_path = fragment_rel_path(spec, tid, category)
        line = render_entry(spec, tid, summary)
        validate_entry(spec, tid, line)
        prepared.append(
            SurfaceEntry(
                surface=str(name),
                task_id=tid,
                category=category,
                summary=summary,
                rel_path=rel_path,
                line=line,
            )
        )
    return prepared


def write_entries(project_root: Path | str, entries: Iterable[SurfaceEntry]) -> list[str]:
    """逐条写片段，返回**本次新写**的仓库相对路径（已存在同内容不计）。"""
    root = Path(project_root)
    specs = load_surfaces(root)
    created: list[str] = []
    try:
        for entry in entries:
            spec = specs.get(entry.surface)
            if spec is None:
                raise _error(f"未知落地面：{entry.surface!r}", surface=entry.surface)
            result = write_fragment(root, spec, entry.task_id, entry.category, entry.summary)
            if result["created"]:
                created.append(result["path"])
    except BaseException:
        # 失败时回滚本次已写文件（AC2/AC4：不留半写残留），原始异常照常上抛。
        discard_new_fragments(root, created)
        raise
    return created


def discard_new_fragments(
    project_root: Path | str,
    rel_paths: Iterable[str],
    *,
    commit_landed: bool = False,
) -> list[str]:
    """回滚**本次新写**的片段文件；提交已落地时一律不删（``[]``）。"""
    if commit_landed:
        return []
    root = Path(project_root)
    removed: list[str] = []
    for rel in rel_paths:
        path = root / rel
        if path.is_file():
            try:
                path.unlink()
            except OSError:  # pragma: no cover - 回滚失败不掩盖原始异常
                continue
            removed.append(rel)
    return removed


def commit_landed_from_error(exc: BaseException) -> bool:
    """从 done 异常 details 判断提交是否已落地（``_postflight_failure_details`` 口径）。"""
    details = getattr(exc, "details", None) or []
    return any(isinstance(item, Mapping) and item.get("commit_landed") is True for item in details)


# ---------------------------------------------------------------------------
# 扫描 / 在册判定 / 行数
# ---------------------------------------------------------------------------


def _filename_regex(filename: str) -> re.Pattern[str]:
    """把 filename 模板编译为解析正则（占位符 → 命名组）。"""
    parts = re.split(r"(\{task_id\}|\{category\})", filename)
    chunks: list[str] = []
    for part in parts:
        if part == "{task_id}":
            chunks.append(r"(?P<task_id>[^/\\]+?)")
        elif part == "{category}":
            chunks.append(r"(?P<category>[^/\\]+?)")
        else:
            chunks.append(re.escape(part))
    return re.compile("".join(chunks))


def _strip_line_endings(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _existing_fragment_for(
    project_root: Path,
    spec: SurfaceSpec,
    task_id: str,
) -> Fragment | None:
    """返回该任务在片段目录内**已存在的合法片段**（同任务重复文件视为非法）。"""
    if spec.mode != "fragments" or not spec.dir:
        return None
    scan = _scan_fragments_dir(Path(project_root) / spec.dir, spec)
    found = scan.fragments.get(task_id)
    if found is not None:
        return found
    pattern = _filename_regex(spec.filename)
    for invalid_rel in scan.invalid:
        matched = pattern.fullmatch(invalid_rel.rsplit("/", 1)[-1])
        if matched is not None and matched.group("task_id") == task_id:
            raise _error(f"片段已存在但内容非法：{invalid_rel}", path=invalid_rel)
    return None


def _scan_fragments_dir(base: Path, spec: SurfaceSpec) -> FragmentScan:
    """扫描片段目录：合法片段按任务号索引，非法/重复的片段记入 ``invalid``。"""
    if not base.is_dir():
        return FragmentScan(surface=spec.name, dir_rel=spec.dir or "")
    pattern = _filename_regex(spec.filename)
    fragments: dict[str, Fragment] = {}
    invalid: list[str] = []
    for path in sorted(base.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        matched = pattern.fullmatch(path.name)
        if matched is None:
            continue
        task_id = matched.group("task_id")
        category = matched.group("category")
        rel_path = f"{spec.dir}/{path.name}"
        try:
            raw_text = path.read_text(encoding=FRAGMENT_ENCODING)
        except (OSError, UnicodeDecodeError):
            invalid.append(rel_path)
            continue
        lines = [line for line in _strip_line_endings(raw_text) if line.strip()]
        if len(lines) != 1:
            invalid.append(rel_path)
            continue
        line = lines[0].strip()
        try:
            validate_entry(spec, task_id, line)
            _check_category(spec, category)
        except OrchdError:
            invalid.append(rel_path)
            continue
        if task_id in fragments:
            # 同任务多片段（跨分类/重复文件名）一律非法，禁止静默覆盖（设计 §5）。
            invalid.append(rel_path)
            continue
        fragments[task_id] = Fragment(
            task_id=task_id,
            category=category,
            rel_path=rel_path,
            line=line,
            text=raw_text,
        )
    return FragmentScan(
        surface=spec.name,
        dir_rel=spec.dir or "",
        fragments=fragments,
        invalid=tuple(invalid),
    )


def scan_fragments(project_root: Path | str, spec: SurfaceSpec) -> FragmentScan:
    """扫描片段目录：合法片段按任务号索引，非法文件名/内容/重复记入 ``invalid``。"""
    if spec.mode != "fragments" or not spec.dir:
        return FragmentScan(surface=spec.name, dir_rel=spec.dir or "")
    return _scan_fragments_dir(Path(project_root) / spec.dir, spec)


def _fragment_mapping(fragments: Any) -> Mapping[str, Fragment]:
    if isinstance(fragments, FragmentScan):
        return fragments.fragments
    if isinstance(fragments, Mapping):
        return fragments
    return {}


def recorded(
    task_id: str,
    surface: SurfaceSpec,
    text: str,
    fragments_dir: Path | str | None = None,
) -> bool:
    """任务是否「在册」：主文件文本含任务号，或片段目录存在该任务的合法片段。

    设计 §7.1 的唯一判据入口：``recorded(task_id, surface, text, fragments_dir)``。
    ``fragments_dir`` 为片段目录（绝对/相对路径均可）；未装配片段同样算在册。
    """
    if not task_id:
        return False
    if text and task_id in text:
        return True
    if fragments_dir is None or surface.mode != "fragments" or not surface.dir:
        return False
    base = Path(fragments_dir)
    if base.is_file():
        base = base.parent
    return task_id in _scan_fragments_dir(base, surface).fragments


def count_rows(
    text: str,
    fragments: Mapping[str, Fragment] | FragmentScan | None = None,
) -> int:
    """落地面「行数」：主文件内条目行数 + 尚未装配的片段行（每片段一行）。"""
    lines = _strip_line_endings(text or "")
    bullets = sum(1 for line in lines if line.startswith("- "))
    assembled = {line.strip() for line in lines}
    extra = sum(
        1
        for fragment in _fragment_mapping(fragments).values()
        if fragment.line.strip() not in assembled
    )
    return bullets + extra


def _read_surface_text(path: Path, name: str) -> tuple[str, str | None, str]:
    try:
        return path.read_text(encoding=FRAGMENT_ENCODING), None, ""
    except (OSError, UnicodeDecodeError) as exc:
        degradation = (
            "changelog_unreadable" if name == DEFAULT_SURFACE_NAME else "surface_unreadable"
        )
        return "", degradation, str(exc)


def scan_surface(
    project_root: Path | str,
    spec: SurfaceSpec,
    *,
    text: str | None = None,
) -> SurfaceScan:
    """读主文件 + 扫片段，返回统一扫描结果（缺文件/不可读 → 降级留痕，不抛）。"""
    root = Path(project_root)
    if text is None:
        text, degradation, detail = _read_surface_text(root / spec.path, spec.name)
    else:
        degradation, detail = None, ""
    fragment_scan = scan_fragments(root, spec)
    fragments = fragment_scan.fragments
    assembled = {line.strip() for line in _strip_line_endings(text)}
    unassembled = tuple(
        task_id
        for task_id, fragment in fragments.items()
        if fragment.line.strip() not in assembled
    )
    missing: list[str] = []
    if fragment_scan.invalid and spec.mode == "fragments":
        pattern = _filename_regex(spec.filename)
        for invalid_rel in fragment_scan.invalid:
            matched = pattern.fullmatch(invalid_rel.rsplit("/", 1)[-1])
            if matched is not None:
                missing.append(matched.group("task_id"))
    return SurfaceScan(
        name=spec.name,
        mode=spec.mode,
        path=spec.path,
        text=text,
        fragments=fragments,
        fragment_ids=tuple(sorted(fragments)),
        unassembled=unassembled,
        rows=count_rows(text, fragments),
        degradation=degradation,
        detail=detail,
        invalid=fragment_scan.invalid,
        missing=tuple(sorted(set(missing))),
        degraded=degradation is not None,
    )


def scan_changelog(
    project_root: Path | str,
    *,
    changelog_rel: str | None = None,
    text: str | None = None,
) -> SurfaceScan:
    """扫描默认落地面（CHANGELOG），供审计与宿主 guard 共用。"""
    specs = load_surfaces(project_root, changelog_rel=changelog_rel)
    return scan_surface(project_root, specs[DEFAULT_SURFACE_NAME], text=text)


def existing_fragment_rel_paths(
    project_root: Path | str,
    task_id: str,
    *,
    name: str | None = None,
) -> list[str]:
    """列出该任务在任一片段面下**已存在**的片段相对路径（done 重试放行用）。"""
    root = Path(project_root)
    specs = load_surfaces(root)
    selected = [specs[name]] if name is not None else list(specs.values())
    out: list[str] = []
    for spec in selected:
        if spec.mode != "fragments":
            continue
        for category in spec.categories:
            rel_path = fragment_rel_path(spec, task_id, category)
            if (root / rel_path).is_file():
                out.append(rel_path)
    return sorted(dict.fromkeys(out))

# ── generated 面受控刷新声明（task-surface-refresh-channel）──────────────
# 背景：S3 准入把 generated 面的写集声明彻底关死（防 LLM 手改生成物），但合法
# 刷新（census 基线 / 依赖图）也随之失去提交通道——pre-commit 动态写集只认
# files_to_edit ∪ exempt_files。本通道的任务级声明 ``surface_refresh`` 只登记
# 「哪个 generated 面由本任务重新生成、为什么」；path / generator 一律由注册表
# 解析（任务不能借此改任意文件），写集展开发生在 hook 与 done 的 commit_files。
SURFACE_REFRESH_FIELD = "surface_refresh"

#: 刷新声明条目允许键（未知键 fail-closed）。
_REFRESH_KEYS = frozenset({"surface", "path", "generator", "reason"})

#: 刷新理由最大字符数（单行，进 AMEND 审计）。
_MAX_REFRESH_REASON_CHARS = 200


def _normalize_refresh_path(raw: Any) -> str:
    r"""刷新路径归一化（``\``→``/``、去 ``./``、折叠、casefold）。"""
    import posixpath

    text = str(raw).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    return posixpath.normpath(text).strip("/").casefold()


def _refresh_reason(reason: Any) -> str:
    """刷新理由：去空白后非空、单行、长度上限内。"""
    if not isinstance(reason, str) or not reason.strip():
        raise _error(
            "刷新声明缺少非空 reason（--reason \"<为何需要刷新>\"）",
            rule="surface_refresh",
        )
    text = reason.strip()
    if "\n" in text or "\r" in text:
        raise _error(
            "刷新声明的 reason 必须是单行文本（✓ 一句说明为何刷新）",
            rule="surface_refresh",
        )
    if len(text) > _MAX_REFRESH_REASON_CHARS:
        raise _error(
            f"刷新声明的 reason 超长（{len(text)} > {_MAX_REFRESH_REASON_CHARS} 字符）",
            rule="surface_refresh",
        )
    return text


def build_refresh_entry(
    specs: Mapping[str, SurfaceSpec],
    surface_name: Any,
    reason: Any,
    *,
    declared_paths: Iterable[str] = (),
) -> dict[str, str]:
    """构造一条 generated 面刷新声明（注册表解析 path/generator）。

    Raises:
        OrchdError: E007 —— 面不存在 / 非 generated / 生成器未登记 / reason 非法
            / 路径与 files_to_edit ∪ exempt_files 重复。
    """
    name = surface_name.strip() if isinstance(surface_name, str) else ""
    if not name:
        raise _error("刷新声明缺少 surface 名", rule="surface_refresh")
    spec = specs.get(name)
    if spec is None:
        raise _error(
            f"刷新声明引用了未注册的共享面 '{name}'"
            f"（可用面：{', '.join(sorted(specs)) or '无'}）",
            surface=name,
            rule="surface_refresh",
        )
    if spec.mode != "generated":
        raise _error(
            f"共享面 '{spec.name}' 为 {spec.mode} 面，只有 generated 面支持受控刷新",
            surface=spec.name,
            mode=spec.mode,
            rule="surface_refresh",
        )
    if not generator_command(spec.generator):
        raise _error(
            f"共享面 '{spec.name}' 的生成器 '{spec.generator}' 未在引擎登记，"
            "无法刷新",
            surface=spec.name,
            generator=spec.generator,
            rule="surface_refresh",
        )
    key = _normalize_refresh_path(spec.path)
    for raw in declared_paths:
        if key and key == _normalize_refresh_path(raw):
            raise _error(
                f"刷新路径 '{spec.path}' 不得同时出现在 files_to_edit/exempt_files"
                "（generated 面不进入任务写集，刷新走 surface_refresh 声明）",
                surface=spec.name,
                path=spec.path,
                rule="surface_refresh",
            )
    return {
        "surface": spec.name,
        "path": spec.path,
        "generator": str(spec.generator or ""),
        "reason": _refresh_reason(reason),
    }


def normalize_refresh_entries(
    specs: Mapping[str, SurfaceSpec],
    entries: Any,
    *,
    declared_paths: Iterable[str] = (),
) -> list[dict[str, str]]:
    """校验并归一化 ``surface_refresh`` 数组（amend/register 共用同一套判定）。

    ``entries`` 为 None/空 → ``[]``；非列表 / 非对象条目 / 未知键 / 重复面 /
    声明内 path|generator 与注册表不一致一律 E007。
    """
    if entries is None:
        return []
    if not isinstance(entries, (list, tuple)):
        raise _error(
            "surface_refresh 必须是数组",
            value_type=type(entries).__name__,
            rule="surface_refresh",
        )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    declared = list(declared_paths)
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            raise _error(
                f"surface_refresh[{index}] 必须是对象",
                index=index,
                rule="surface_refresh",
            )
        unknown = sorted(set(raw) - _REFRESH_KEYS)
        if unknown:
            raise _error(
                f"surface_refresh[{index}] 含未知键 {unknown}",
                index=index,
                keys=unknown,
                rule="surface_refresh",
            )
        entry = build_refresh_entry(
            specs, raw.get("surface"), raw.get("reason"),
            declared_paths=declared,
        )
        for key in ("path", "generator"):
            if key in raw and raw[key] not in (None, ""):
                if str(raw[key]) != entry[key]:
                    raise _error(
                        f"surface_refresh[{index}].{key} 与共享面注册表不一致"
                        f"（注册表：{entry[key]!r}，声明：{raw[key]!r}）",
                        index=index,
                        field=key,
                        rule="surface_refresh",
                    )
        if entry["surface"] in seen:
            raise _error(
                f"surface_refresh 重复声明同一面 '{entry['surface']}'",
                surface=entry["surface"],
                rule="surface_refresh",
            )
        seen.add(entry["surface"])
        out.append(entry)
    return out


def refresh_rel_paths(task: Any) -> list[str]:
    """任务 ``surface_refresh`` 声明的路径（仅取 path；校验在 amend 期完成）。"""
    entries = (task or {}).get(SURFACE_REFRESH_FIELD) or ()
    out: list[str] = []
    for item in entries:
        if isinstance(item, Mapping):
            path = item.get("path")
            if isinstance(path, str) and path.strip():
                out.append(path.strip())
    return out
