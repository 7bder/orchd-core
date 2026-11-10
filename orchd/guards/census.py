"""宿主 guard ``census``：文件 / 函数体量棘轮（宿主可配置）。

把引擎 ``scripts/file_census.py`` 的尺寸棘轮泛化为宿主可声明的普查策略：宿主在
``.orchd/shared/guards.json`` 为 ``census`` 登记 ``roots`` / ``file_max`` / ``baseline``
等选项，done 期按该策略扫描宿主 ``.py`` 并与宿主基线比对，报出已登记文件回升 / 新增
文件超阈值 / 基线幽灵条目 / 棘轮根总行数回升；函数级超限只作为 warnings 报告。

配置示例（选项字段在本 guard 内校验，非法即降级留痕、不阻断）::

    {"guards": {"census": {"mode": "warn", "roots": ["src", "tests"],
                           "file_max": 400, "baseline": "file-census-baseline.json"}}}

可选键见下方 ``OPTION_*`` 常量，缺省值与引擎仓一致。

纪律：只读（不写盘、不起子进程）；基线缺失视为「尚未登记棘轮」，返回空
（``not_applicable``）不误红；扫描 / 解析异常降级留痕、不阻断 done。写基线默认只许
收紧，调高 / 新增大文件须显式 ``force_raise``。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.guards import (
    GuardEvalContext,
    GuardFinding,
    load_guards_config,
    register_guard_evaluator,
)

# 基线 schema 版本、默认普查目录 / 只降不升目录 / 阈值 / 基线路径，均与引擎同构。
SCHEMA_VERSION = 1
DEFAULT_ROOTS = ("orchd", "tests", "scripts")
DEFAULT_RATCHET_ROOTS = ("orchd", "scripts")
DEFAULT_FILE_MAX = 500
DEFAULT_FUNCTION_MAX = 300
DEFAULT_CLASS_MAX = 500
DEFAULT_BASELINE_REL = "file-census-baseline.json"

# 配置选项键（未知键视为配置错误，避免拼写造成静默漏跑）。
OPTION_ROOTS = "roots"
OPTION_RATCHET_ROOTS = "ratchet_roots"
OPTION_FILE_MAX = "file_max"
OPTION_FUNCTION_MAX = "function_max"
OPTION_CLASS_MAX = "class_max"
OPTION_BASELINE = "baseline"

KNOWN_OPTIONS = frozenset(
    {OPTION_ROOTS, OPTION_RATCHET_ROOTS, OPTION_FILE_MAX,
     OPTION_FUNCTION_MAX, OPTION_CLASS_MAX, OPTION_BASELINE}
)


class CensusGuardConfigError(ValueError):
    """``census`` guard 选项非法（降级留痕用；不是引擎错误码）。"""


class CensusWriteBlocked(ValueError):
    """写基线会放宽棘轮且未显式 ``force_raise``（``raised`` / ``new_over`` 记明细）。"""

    def __init__(self, message: str, *, raised: list[str], new_over: list[str]) -> None:
        super().__init__(message)
        self.raised = raised
        self.new_over = new_over


@dataclass(frozen=True)
class CensusPolicy:
    """一次普查使用的策略（全部字段来自宿主配置或默认值）。"""

    roots: tuple[str, ...] = DEFAULT_ROOTS
    ratchet_roots: tuple[str, ...] = DEFAULT_RATCHET_ROOTS
    file_max: int = DEFAULT_FILE_MAX
    function_max: int = DEFAULT_FUNCTION_MAX
    class_max: int = DEFAULT_CLASS_MAX
    baseline_rel: str = DEFAULT_BASELINE_REL

    def as_baseline_policy(self) -> dict[str, Any]:
        """转成基线 ``policy`` 段（与引擎基线同构）。"""
        return {
            "roots": list(self.roots),
            "ratchet_roots": list(self.ratchet_roots),
            "file_max": self.file_max,
            "function_max": self.function_max,
            "class_max": self.class_max,
        }


# 引擎仓默认策略（``scripts/file_census.py`` 直接复用）。
ENGINE_POLICY = CensusPolicy()


def _normalize_rel_path(raw: Any, *, field: str) -> str:
    """校验仓库相对路径（POSIX 归一、拒绝绝对路径 / 盘符 / ``..``）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise CensusGuardConfigError(f"{field} 必须是非空字符串（仓库相对路径）")
    text = raw.strip().replace("\\", "/")
    if text.startswith("/"):
        raise CensusGuardConfigError(f"{field} 必须是仓库相对路径：{raw!r}")
    if ":" in text:
        raise CensusGuardConfigError(f"{field} 必须是仓库相对路径：{raw!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise CensusGuardConfigError(f"{field} 必须是仓库相对路径：{raw!r}")
    return "/".join(parts)


def _normalize_roots(raw: Any, *, field: str) -> tuple[str, ...]:
    """校验目录清单：非空字符串列表，逐项归一为相对目录。"""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise CensusGuardConfigError(f"{field} 必须是非空字符串列表")
    roots: list[str] = []
    for item in raw:
        normalized = _normalize_rel_path(item, field=field)
        if normalized not in roots:
            roots.append(normalized)
    return tuple(roots)


def _normalize_positive_int(raw: Any, *, field: str) -> int:
    """校验正整数阈值（``bool`` 是 ``int`` 子类，须显式拒绝）。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CensusGuardConfigError(f"{field} 必须是正整数：{raw!r}")
    if raw <= 0:
        raise CensusGuardConfigError(f"{field} 必须是正整数：{raw!r}")
    return raw


def parse_policy(options: Mapping[str, Any] | None) -> CensusPolicy:
    """把 ``guards.json`` 里 ``census`` 的选项解析为 :class:`CensusPolicy`。

    Raises:
        CensusGuardConfigError: 未知选项键 / 类型或取值非法。
    """
    if options is None:
        return ENGINE_POLICY
    if not isinstance(options, Mapping):
        raise CensusGuardConfigError("census 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise CensusGuardConfigError(f"census 存在未知选项：{unknown}")
    roots = (
        _normalize_roots(options[OPTION_ROOTS], field=OPTION_ROOTS)
        if OPTION_ROOTS in options
        else DEFAULT_ROOTS
    )
    ratchet_roots = (
        _normalize_roots(options[OPTION_RATCHET_ROOTS], field=OPTION_RATCHET_ROOTS)
        if OPTION_RATCHET_ROOTS in options
        else DEFAULT_RATCHET_ROOTS
    )
    file_max = (
        _normalize_positive_int(options[OPTION_FILE_MAX], field=OPTION_FILE_MAX)
        if OPTION_FILE_MAX in options
        else DEFAULT_FILE_MAX
    )
    function_max = (
        _normalize_positive_int(options[OPTION_FUNCTION_MAX], field=OPTION_FUNCTION_MAX)
        if OPTION_FUNCTION_MAX in options
        else DEFAULT_FUNCTION_MAX
    )
    class_max = (
        _normalize_positive_int(options[OPTION_CLASS_MAX], field=OPTION_CLASS_MAX)
        if OPTION_CLASS_MAX in options
        else DEFAULT_CLASS_MAX
    )
    baseline_rel = (
        _normalize_rel_path(options[OPTION_BASELINE], field=OPTION_BASELINE)
        if OPTION_BASELINE in options
        else DEFAULT_BASELINE_REL
    )
    return CensusPolicy(
        roots=roots,
        ratchet_roots=ratchet_roots,
        file_max=file_max,
        function_max=function_max,
        class_max=class_max,
        baseline_rel=baseline_rel,
    )


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def root_totals(files: Mapping[str, int], ratchet_roots: tuple[str, ...]) -> dict[str, int]:
    """棘轮根内 ``.py`` 总行数（判据⑤的量）：按 ``rel`` 前缀归根求和。"""
    return {
        root: sum(v for k, v in files.items() if k.startswith(f"{root}/"))
        for root in ratchet_roots
    }


def scan_python(project_root: Path, policy: CensusPolicy) -> dict[str, Any]:
    """扫描 ``policy.roots`` 下的 ``.py``，返回文件 / 函数 / 类三级体量。

    口径同引擎 ``scan``：文件级 ``len(src.splitlines())``；函数 / 类级
    ``end_lineno - lineno + 1``；跳过 ``__pycache__`` 与语法错文件。
    """
    files: dict[str, int] = {}
    functions: dict[str, int] = {}
    classes: dict[str, int] = {}
    for name in policy.roots:
        base = project_root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = _relative(path, project_root)
            source = path.read_text(encoding="utf-8")
            files[rel] = len(source.splitlines())
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                key = f"{rel}::{node.name}"
                if isinstance(node, ast.ClassDef):
                    classes[key] = span
                else:
                    functions[key] = span
    return {
        "files": files,
        "functions": functions,
        "classes": classes,
        "totals": root_totals(files, policy.ratchet_roots),
    }


def in_ratchet(rel: str, ratchet_roots: tuple[str, ...]) -> bool:
    """``rel`` 是否落在棘轮根内（函数键按 ``路径::名`` 取路径部分）。"""
    path = rel.split("::", 1)[0]
    return any(path.startswith(f"{root}/") for root in ratchet_roots)


def build_baseline(census: Mapping[str, Any], policy: CensusPolicy) -> dict[str, Any]:
    """由一次普查生成基线（``files`` 为全量快照，``functions`` 只登记超阈值项）。"""
    functions = census.get("functions") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": policy.as_baseline_policy(),
        "files": {rel: lines for rel, lines in sorted((census.get("files") or {}).items())},
        "functions": {
            key: lines
            for key, lines in sorted(functions.items())
            if lines > policy.function_max and in_ratchet(key, policy.ratchet_roots)
        },
        "totals": dict(
            census.get("totals") or root_totals(census.get("files") or {}, policy.ratchet_roots)
        ),
    }


def load_baseline_file(path: Path) -> dict[str, Any]:
    """读取基线 JSON（不做 schema 深校验；判据按 ``get`` 宽容读取）。"""
    return json.loads(path.read_text(encoding="utf-8"))


def dump_baseline_file(baseline: Mapping[str, Any], path: Path) -> None:
    """写基线 JSON（与引擎同格式：``indent=2``、键序保持、末尾换行）。"""
    path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def check_census(
    census: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    file_max: int | None = None,
    ratchet_roots: tuple[str, ...] | None = None,
) -> list[str]:
    """棘轮校验，返回违规串列表（空表示通过）。

    ``file_max`` / ``ratchet_roots`` 缺省取基线 ``policy`` 段，再退回引擎默认；
    违规串前缀为 ``[file-up]`` / ``[file-new-over]`` / ``[file-gone]`` / ``[total-up]``。
    """
    policy = baseline.get("policy") or {}
    effective_file_max = int(
        file_max if file_max is not None else policy.get("file_max", DEFAULT_FILE_MAX)
    )
    effective_ratchet = tuple(
        ratchet_roots
        if ratchet_roots is not None
        else policy.get("ratchet_roots") or DEFAULT_RATCHET_ROOTS
    )
    base_files: dict[str, int] = baseline.get("files") or {}
    files: Mapping[str, int] = census.get("files") or {}
    violations: list[str] = []

    # ① 已登记文件只降不升（棘轮根内）
    for rel, cap in sorted(base_files.items()):
        current = files.get(rel)
        if current is None:
            continue  # 存在性由 ③ 判（避免同一事实报两次）
        if current > cap and in_ratchet(rel, effective_ratchet):
            violations.append(f"[file-up] {rel}: {current} > 基线 {cap}（体量只许降不许升）")

    # ② 新增文件（未在基线快照里）≤ file_max（对全部 roots 生效）
    for rel, current in sorted(files.items()):
        if rel in base_files:
            continue
        if current > effective_file_max:
            violations.append(
                f"[file-new-over] {rel}: {current} > {effective_file_max}（新增文件不得超阈值）"
            )

    # ③ 基线条目必须仍存在
    for rel in sorted(base_files):
        if rel not in files:
            violations.append(f"[file-gone] {rel}: 基线登记的文件已不存在（须重写基线）")

    # ⑤ 棘轮根内 .py 总行数只降不升
    base_totals: dict[str, int] = baseline.get("totals") or {}
    current_totals: dict[str, int] = census.get("totals") or {}
    for root in sorted(base_totals):
        current = current_totals.get(root)
        if current is None:
            continue
        cap = base_totals[root]
        if current > cap:
            violations.append(
                f"[total-up] {root}: 总行数 {current} > 基线 {cap}"
                "（净增代码须走拆分抵偿或显式 force-raise）"
            )

    return violations


def function_warnings(
    census: Mapping[str, Any],
    baseline: Mapping[str, Any],
    *,
    function_max: int | None = None,
    ratchet_roots: tuple[str, ...] | None = None,
) -> list[str]:
    """函数级超限只报告：列出棘轮根内所有超过 ``function_max`` 的函数。"""
    policy = baseline.get("policy") or {}
    effective_max = int(
        function_max
        if function_max is not None
        else policy.get("function_max", DEFAULT_FUNCTION_MAX)
    )
    effective_ratchet = tuple(
        ratchet_roots
        if ratchet_roots is not None
        else policy.get("ratchet_roots") or DEFAULT_RATCHET_ROOTS
    )
    functions = census.get("functions") or {}
    return [
        f"[func-over] {key}: {cur} > {effective_max}（仅报告）"
        for key, cur in sorted(functions.items())
        if cur > effective_max and in_ratchet(key, effective_ratchet)
    ]


def _raise_plan(
    previous: Mapping[str, Any],
    fresh: Mapping[str, Any],
    policy: CensusPolicy,
) -> tuple[list[str], list[str]]:
    """对比新旧基线，返回 ``(raised, new_over)``：会放宽棘轮的条目。"""
    raised: list[str] = []
    new_over: list[str] = []
    previous_files: dict[str, int] = previous.get("files") or {}
    fresh_files: dict[str, int] = fresh.get("files") or {}
    for key, cap in sorted(previous_files.items()):
        current = fresh_files.get(key)
        if current is not None and current > cap and in_ratchet(key, policy.ratchet_roots):
            raised.append(f"raise files:{key} {cap} -> {current}")
    for key, current in sorted(fresh_files.items()):
        if key not in previous_files and current > policy.file_max:
            new_over.append(f"new-over files:{key} {current} > {policy.file_max}")
    previous_totals: dict[str, int] = previous.get("totals") or {}
    fresh_totals: dict[str, int] = fresh.get("totals") or {}
    for root, current in sorted(fresh_totals.items()):
        cap = previous_totals.get(root)
        if cap is not None and current > cap:
            raised.append(f"raise totals:{root} {cap} -> {current}")
    return raised, new_over


def write_baseline(
    project_root: Path,
    policy: CensusPolicy,
    *,
    force_raise: bool = False,
) -> dict[str, Any]:
    """按当前磁盘普查结果重写基线（**默认只许收紧 / 登记新文件**）。

    与引擎 ``--write`` 同语义：调高既有配额或登记超阈值新文件会放宽棘轮，必须显式
    ``force_raise=True``，否则抛 :class:`CensusWriteBlocked`（带 ``raised`` / ``new_over``）。
    """
    baseline_path = project_root / policy.baseline_rel
    census = scan_python(project_root, policy)
    fresh = build_baseline(census, policy)
    raised: list[str] = []
    new_over: list[str] = []
    if baseline_path.is_file():
        previous = load_baseline_file(baseline_path)
        raised, new_over = _raise_plan(previous, fresh, policy)
    if (raised or new_over) and not force_raise:
        raise CensusWriteBlocked(
            "写基线会放宽棘轮（默认只许收紧；调高须显式 force_raise）",
            raised=raised,
            new_over=new_over,
        )
    dump_baseline_file(fresh, baseline_path)
    return {
        "baseline": policy.baseline_rel,
        "raised": raised,
        "new_over": new_over,
        "force_raise": force_raise,
    }


def _finding_context(message: str) -> dict[str, str]:
    """从违规串里抽 ``kind`` / ``target`` 供 findings 结构化消费。"""
    kind = message.split("]", 1)[0].lstrip("[")
    target = ""
    if " " in message:
        head = message.split(" ", 1)[1]
        target = head.split(":", 1)[0]
    return {"kind": kind, "target": target}


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """评估宿主 ``census`` 棘轮：按宿主配置扫描并与宿主基线比对。

    无配置项 / 基线缺失返回空元组（``not_applicable``，宿主未登记棘轮）；配置非法或
    扫描异常抛 ``RuntimeError``（done 期降级留痕）。
    """
    project_root = context.project_root
    if project_root is None:
        return ()
    root = Path(project_root)
    config = load_guards_config(root)
    entry = next((item for item in config.entries if item.name == "census"), None)
    if entry is None or not entry.enabled:
        return ()
    if not entry.options:
        return ()  # 无配置项 ⇒ not_applicable（宿主未声明棘轮策略）
    try:
        policy = parse_policy(entry.options)
    except CensusGuardConfigError as exc:
        raise RuntimeError(f"census guard 配置非法：{exc}") from exc
    baseline_path = root / policy.baseline_rel
    if not baseline_path.is_file():
        return ()  # 基线缺失 ⇒ 尚未登记棘轮，not_applicable
    baseline = load_baseline_file(baseline_path)
    census = scan_python(root, policy)
    violations = check_census(
        census,
        baseline,
        file_max=policy.file_max,
        ratchet_roots=policy.ratchet_roots,
    )
    return tuple(
        GuardFinding(guard="census", message=message, context=_finding_context(message))
        for message in violations
    )


def register() -> None:
    """把 ``census`` 评估器注册进全局注册表（import 期与懒加载都会调用）。"""
    register_guard_evaluator("census", evaluate)


register()

__all__ = (
    "CensusGuardConfigError", "CensusPolicy", "CensusWriteBlocked", "DEFAULT_BASELINE_REL",
    "DEFAULT_CLASS_MAX", "DEFAULT_FILE_MAX", "DEFAULT_FUNCTION_MAX", "DEFAULT_RATCHET_ROOTS",
    "DEFAULT_ROOTS", "ENGINE_POLICY", "KNOWN_OPTIONS", "OPTION_BASELINE", "OPTION_CLASS_MAX",
    "OPTION_FILE_MAX", "OPTION_FUNCTION_MAX", "OPTION_RATCHET_ROOTS", "OPTION_ROOTS",
    "SCHEMA_VERSION", "build_baseline", "check_census", "dump_baseline_file", "evaluate",
    "function_warnings", "in_ratchet", "load_baseline_file", "parse_policy", "register",
    "root_totals", "scan_python", "write_baseline",
)
