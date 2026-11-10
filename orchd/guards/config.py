"""宿主 guard 配置层（task-host-guard-config）。

定位：把「宿主仓自行启用的 guard」与引擎内建判据解耦——宿主在
``.orchd/shared/guards.json`` 声明启用状态与参数；本模块只负责**读取 +
校验 + 只读展示**，不执行任何 guard 判据本身（各 guard 判据在
``orchd/guards/<name>.py`` 与 done 期接线落地；本卡只建配置层）。

缺省语义（AC1）：没有配置文件、空文件、空配置对象时，全部宿主 guard 为
``off``；``orchd guards list`` 对每项输出 ``enabled=false`` 且
``reason=no_config``。引擎仓不读该文件时代码路径不变、行为逐条一致。

fail-closed（AC2）：非法 JSON、未知 guard 名、越界 roots（绝对路径 /
``..`` / 盘符）等一律返回**结构化错误**（错误项带 ``kind`` / ``message`` /
可选 ``guard`` / ``path``），CLI 退出码非零——不得静默当成「全 off」放行。

只读边界（AC3）：本模块只读固定路径（``.orchd/shared/guards.json``），
不写盘、不起子进程、不遍历目录树；``orchd doctor`` 的回显同样走本模块的
只读视图（``doctor_echo``）。

配置文件 schema（v1，严格）::

    {
      "schema_version": 1,
      "guards": {
        "census": {"mode": "warn", "roots": ["orchd/"], "file_max": 500},
        "docs":   {"mode": "hard"}
      }
    }

- 顶层仅允许 ``schema_version`` / ``guards``，其它键报 ``unknown_key``；
- ``schema_version`` 缺省按 1，显式给出时必须为整数 1；
- ``guards`` 的键必须在 :data:`GUARD_NAMES` 注册表内，否则 ``unknown_guard``；
- 每个 guard 的 ``mode`` ∈ ``off`` / ``warn`` / ``hard``（缺省 ``off``）；
- ``roots`` 为相对项目根的路径列表（不得绝对路径 / 盘符 / ``..`` 越界）；
- ``file_max`` 为正整数；
- 其余键按 guard 专属参数原样透传（配置层不校验语义，判据层自行解释）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

SCHEMA_VERSION = 1

# 配置路径（相对项目根；container 布局同样落在主工作树的 .orchd/shared/ 下）。
CONFIG_REL = ".orchd/shared/guards.json"
SHARED_DIR_NAME = "shared"
GUARDS_FILE_NAME = "guards.json"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_HARD = "hard"
MODES: tuple[str, ...] = (MODE_OFF, MODE_WARN, MODE_HARD)

# 宿主 guard 注册表（单一真源）：新增 guard 必须先在此登记；配置里出现未登记
# 名字一律结构化报错（防拼写错误被静默忽略成「off」）。
GUARD_NAMES: tuple[str, ...] = (
    "generated",
    "census",
    "docs",
    "discipline",
    "deps",
    "pkg_contract",
    "lanes",
    "subdomain",
    "drift",
    "release",
    "perf",
)

_TOP_LEVEL_KEYS = frozenset({"schema_version", "guards"})
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_EMPTY_OPTIONS: Mapping[str, Any] = MappingProxyType({})


def _error(
    kind: str,
    message: str,
    *,
    guard: str | None = None,
    path: str | Path | None = None,
) -> dict[str, str]:
    """构造一条结构化配置错误项。"""
    item: dict[str, str] = {"kind": kind, "message": message}
    if guard is not None:
        item["guard"] = guard
    if path is not None:
        item["path"] = str(path)
    return item


class GuardsConfigError(ValueError):
    """guards 配置非法：``errors`` 为结构化错误项列表（逐项 kind 可机读）。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors: tuple[dict[str, str], ...] = tuple(errors)
        summary = "；".join(
            f"{item.get('kind')}: {item.get('message')}" for item in self.errors[:5]
        )
        super().__init__(f"guards 配置非法（{len(self.errors)} 项）：{summary}")


@dataclass(frozen=True)
class GuardEntry:
    """单个 guard 的声明视图（未声明时 mode=off、options 为空）。"""

    name: str
    mode: str
    options: Mapping[str, Any] = _EMPTY_OPTIONS

    @property
    def enabled(self) -> bool:
        """是否启用（mode 非 off 即启用；hard / warn 都算启用）。"""
        return self.mode != MODE_OFF


@dataclass(frozen=True)
class GuardsConfig:
    """一次加载得到的完整配置视图（固定按 :data:`GUARD_NAMES` 顺序排列）。"""

    path: Path
    present: bool
    schema_version: int | None
    entries: tuple[GuardEntry, ...]
    declared: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """是否声明了至少一个 guard（空文件 / ``{"guards": {}}`` 均为 False）。"""
        return bool(self.declared)


def _validate_mode(value: Any, guard: str, path: Path) -> str:
    if not isinstance(value, str) or value not in MODES:
        raise GuardsConfigError(
            [
                _error(
                    "invalid_mode",
                    f"guard {guard} 的 mode 必须是 {'/'.join(MODES)} 之一，实际为 {value!r}",
                    guard=guard,
                    path=path,
                )
            ]
        )
    return value


def _validate_roots(value: Any, guard: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GuardsConfigError(
            [
                _error(
                    "invalid_type",
                    f"guard {guard} 的 roots 必须是字符串数组",
                    guard=guard,
                    path=path,
                )
            ]
        )
    roots: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise GuardsConfigError(
                [
                    _error(
                        "invalid_type",
                        f"guard {guard} 的 roots 元素必须是字符串，实际为 {raw!r}",
                        guard=guard,
                        path=path,
                    )
                ]
            )
        text = raw.strip()
        if not text:
            raise GuardsConfigError(
                [
                    _error(
                        "invalid_root",
                        f"guard {guard} 的 roots 含空路径",
                        guard=guard,
                        path=path,
                    )
                ]
            )
        normalized = text.replace("\\", "/")
        if normalized.startswith("/") or _DRIVE_PREFIX_RE.match(normalized):
            raise GuardsConfigError(
                [
                    _error(
                        "roots_escape",
                        f"guard {guard} 的 roots 不允许绝对路径或盘符：{text}",
                        guard=guard,
                        path=path,
                    )
                ]
            )
        segments = normalized.split("/")
        if any(segment == ".." for segment in segments):
            raise GuardsConfigError(
                [
                    _error(
                        "roots_escape",
                        f"guard {guard} 的 roots 不允许 .. 越界：{text}",
                        guard=guard,
                        path=path,
                    )
                ]
            )
        parts = [part for part in segments if part not in ("", ".")]
        roots.append("/".join(parts))
    return tuple(roots)


def _validate_file_max(value: Any, guard: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuardsConfigError(
            [
                _error(
                    "invalid_file_max",
                    f"guard {guard} 的 file_max 必须是正整数，实际为 {value!r}",
                    guard=guard,
                    path=path,
                )
            ]
        )
    return value


def _parse_guard_entry(name: str, raw: Any, path: Path) -> GuardEntry:
    if not isinstance(raw, dict):
        raise GuardsConfigError(
            [
                _error(
                    "invalid_type",
                    f"guard {name} 的配置必须是 JSON 对象",
                    guard=name,
                    path=path,
                )
            ]
        )
    mode = _validate_mode(raw.get("mode", MODE_OFF), name, path)
    options: dict[str, Any] = {}
    if "roots" in raw:
        options["roots"] = _validate_roots(raw["roots"], name, path)
    if "file_max" in raw:
        options["file_max"] = _validate_file_max(raw["file_max"], name, path)
    for key, value in raw.items():
        if key in ("mode", "roots", "file_max"):
            continue
        options[key] = value
    return GuardEntry(name=name, mode=mode, options=MappingProxyType(options))


def _off_entries(declared_by_name: Mapping[str, GuardEntry]) -> tuple[GuardEntry, ...]:
    return tuple(
        declared_by_name.get(name) or GuardEntry(name=name, mode=MODE_OFF) for name in GUARD_NAMES
    )


def parse_guards_config(data: Any, *, path: str | Path | None = None) -> GuardsConfig:
    """把已解析的 JSON 对象校验并归一化为 :class:`GuardsConfig`。

    纯函数（不读盘）：供单测直接喂字典，也供 :func:`load_guards_config` 复用。
    """
    cfg_path = Path(path) if path is not None else Path(CONFIG_REL)
    if not isinstance(data, dict):
        raise GuardsConfigError([_error("invalid_type", "配置根必须是 JSON 对象", path=cfg_path)])

    errors: list[dict[str, str]] = []
    for key in data:
        if key not in _TOP_LEVEL_KEYS:
            errors.append(
                _error(
                    "unknown_key",
                    f"未知的顶层键：{key}（仅允许 schema_version / guards）",
                    path=cfg_path,
                )
            )

    raw_version = data.get("schema_version", SCHEMA_VERSION)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        errors.append(
            _error(
                "schema_version",
                f"schema_version 必须是整数 {SCHEMA_VERSION}",
                path=cfg_path,
            )
        )
    elif raw_version != SCHEMA_VERSION:
        errors.append(
            _error(
                "schema_version",
                f"不支持的 schema_version={raw_version}（当前仅 {SCHEMA_VERSION}）",
                path=cfg_path,
            )
        )

    guards_raw = data.get("guards", {})
    if guards_raw is None:
        guards_raw = {}
    if not isinstance(guards_raw, dict):
        errors.append(_error("invalid_type", "guards 必须是 JSON 对象", path=cfg_path))
        guards_raw = {}

    declared: dict[str, GuardEntry] = {}
    for name, raw in guards_raw.items():
        if name not in GUARD_NAMES:
            errors.append(
                _error(
                    "unknown_guard",
                    f"未知 guard：{name}（已登记：{'、'.join(GUARD_NAMES)}）",
                    path=cfg_path,
                )
            )
            continue
        try:
            declared[name] = _parse_guard_entry(name, raw, cfg_path)
        except GuardsConfigError as exc:
            errors.extend(exc.errors)

    if errors:
        raise GuardsConfigError(errors)

    return GuardsConfig(
        path=cfg_path,
        present=True,
        schema_version=raw_version,
        entries=_off_entries(declared),
        declared=tuple(declared),
    )


def load_guards_config(project_root: str | Path) -> GuardsConfig:
    """读取 ``<project_root>/.orchd/shared/guards.json`` 并校验。

    只读单文件：缺文件返回「全 off」视图（``present=False``）；空文件按空配置
    处理；解析/校验失败抛 :class:`GuardsConfigError`（结构化错误，不静默）。
    """
    root = Path(project_root)
    cfg_path = root / ".orchd" / SHARED_DIR_NAME / GUARDS_FILE_NAME
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return GuardsConfig(
            path=cfg_path,
            present=False,
            schema_version=None,
            entries=_off_entries({}),
        )
    except UnicodeDecodeError as exc:
        # 非 UTF-8 配置（Windows PowerShell 重定向默认 UTF-16LE）必须给结构化错误：
        # UnicodeDecodeError ⊂ ValueError 而非 OSError，漏捕会让调用方裸 traceback。
        raise GuardsConfigError(
            [
                _error(
                    "unreadable",
                    f"guards 配置不是 UTF-8 文本（{exc.encoding} 解码失败）："
                    "请以 UTF-8 保存 .orchd/shared/guards.json",
                    path=cfg_path,
                )
            ]
        ) from exc
    except OSError as exc:
        raise GuardsConfigError(
            [
                _error(
                    "unreadable",
                    f"guards 配置不可读：{type(exc).__name__}: {exc}",
                    path=cfg_path,
                )
            ]
        ) from exc
    if not text.strip():
        return GuardsConfig(
            path=cfg_path,
            present=True,
            schema_version=SCHEMA_VERSION,
            entries=_off_entries({}),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GuardsConfigError(
            [
                _error(
                    "invalid_json",
                    f"guards 配置不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}",
                    path=cfg_path,
                )
            ]
        ) from exc
    return parse_guards_config(data, path=cfg_path)


def _guard_payload(cfg: GuardsConfig, entry: GuardEntry) -> dict[str, Any]:
    if not cfg.configured:
        reason = "no_config"
    elif entry.enabled:
        reason = "enabled"
    elif entry.name in cfg.declared:
        reason = "disabled"
    else:
        reason = "not_configured"
    payload: dict[str, Any] = {
        "name": entry.name,
        "enabled": entry.enabled,
        "mode": entry.mode,
        "reason": reason,
    }
    if entry.options:
        payload["options"] = dict(entry.options)
    return payload


def guards_list_payload(project_root: str | Path) -> dict[str, Any]:
    """``orchd guards list`` 的只读响应体（配置非法时 ok=False + 结构化错误）。"""
    try:
        cfg = load_guards_config(project_root)
    except GuardsConfigError as exc:
        return {
            "ok": False,
            "config_path": CONFIG_REL,
            "errors": [dict(item) for item in exc.errors],
            "guards": [],
        }
    return {
        "ok": True,
        "config_path": CONFIG_REL,
        "present": cfg.present,
        "configured": cfg.configured,
        "schema_version": cfg.schema_version,
        "guards": [_guard_payload(cfg, entry) for entry in cfg.entries],
    }


def doctor_echo(project_root: str | Path) -> dict[str, Any]:
    """``orchd doctor`` 的只读回显视图（只读、零子进程；非法配置原样带错误）。"""
    payload = guards_list_payload(project_root)
    if not payload.get("ok"):
        return payload
    enabled = [item for item in payload["guards"] if item["enabled"]]
    return {
        "ok": True,
        "config_path": CONFIG_REL,
        "present": payload["present"],
        "configured": payload["configured"],
        "enabled": enabled,
        "guards": payload["guards"],
    }
