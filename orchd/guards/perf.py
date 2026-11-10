"""宿主遥测 guard：git spawn 计数与 g1 站点分布的采集口径 + 只读门禁结论。

定位（task-host-guard-perf）：把 ``scripts/spawn_census.py`` / ``scripts/g1_site_census.py``
的**采集结果口径**收敛到本模块一份实现，并给宿主一个可配置的只读门禁：

宿主在 ``guards.json`` 的 ``perf`` 段声明（全部键可省）::

    {"schema_version": 1,
     "guards": {"perf": {
         "mode": "warn",
         "census": ".orchd/perf-census.json",
         "spawn_max": 40,
         "probe_max": 120,
         "top": 10
     }}}

- ``census``：已采集的普查 JSON 路径（项目内相对路径）。评估器**只读**它，不起子进程；
  没有该文件 → ``RuntimeError``（done 期降级留痕，不误红）。
- ``spawn_max`` / ``probe_max``：git spawn 总数 / g1 探针总数的可配上限；``0``（默认）
  表示**只测量不判红**，显式配置正数才启用。spawn 侧读 ``total_spawns``，probe 侧优先读
  ``total_probe_count``、缺该键时按 ``sites`` 各站点 count 求和；故同一个 census 文件可同时
  含两项并被两个上限各自判一次。
- ``top``：结论里保留的 TopN 站点行数（只影响可读性，不影响判据）。

评估器对畸形产物一律 ``RuntimeError``（done 期记 ``degraded_guards.status=failed``），
不把异常漏成 AttributeError；计数按「向上取整」归一，避免 40.9 被截断绕过上限。

采集侧契约（两个脚本共用，结果结构与引擎版逐字一致）：

- :func:`spawn_payload`：``{"total_spawns", "total_seconds", "rows": [{site,count,seconds}]}``；
- :func:`merge_shards`：把 g1 的 pid 分片合并成 ``{"kind :: site": rec}``；
- 脚本只写 ``--json`` 指定路径（宿主任意位置），不写仓内其它文件。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from orchd.guards import GuardEvalContext, GuardFinding, GuardsConfigError, load_guards_config

__all__ = (
    "DEFAULT_PROBE_MAX",
    "DEFAULT_SPAWN_MAX",
    "DEFAULT_TOP",
    "KNOWN_OPTIONS",
    "PerfGuardConfigError",
    "PerfPolicy",
    "evaluate",
    "judge_census",
    "load_perf_policy",
    "merge_shards",
    "parse_policy",
    "register",
    "spawn_payload",
)

DEFAULT_SPAWN_MAX = 0  # 0 = 只测量不判红（避免与实测锚量级错配造成假红）
DEFAULT_PROBE_MAX = 0
DEFAULT_TOP = 10
KNOWN_OPTIONS = frozenset({"census", "spawn_max", "probe_max", "top"})


class PerfGuardConfigError(ValueError):
    """perf guard 配置非法（未知键 / 类型不符 / 路径越界）。"""


@dataclass(frozen=True)
class PerfPolicy:
    """宿主遥测策略（``census`` 为空表示尚未声明普查产物）。"""

    census: str = ""
    spawn_max: int = DEFAULT_SPAWN_MAX
    probe_max: int = DEFAULT_PROBE_MAX
    top: int = DEFAULT_TOP


def _rel_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PerfGuardConfigError(f"{field_name} 必须是非空字符串")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise PerfGuardConfigError(f"{field_name} 必须是项目内相对路径：{value!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise PerfGuardConfigError(f"{field_name} 不能为空或含 .. 越界：{value!r}")
    return "/".join(parts)


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PerfGuardConfigError(f"{field_name} 必须是正整数，实际为 {value!r}")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    """上限专用：``0`` 表示不启用（只测量），其余必须为正整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PerfGuardConfigError(f"{field_name} 必须是 0 或正整数，实际为 {value!r}")
    return value


def parse_policy(options: object) -> PerfPolicy:
    """把 ``guards.json`` 的 perf options 解析为 :class:`PerfPolicy`。"""
    if options is None:
        return PerfPolicy()
    if not isinstance(options, Mapping):
        raise PerfGuardConfigError("perf 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise PerfGuardConfigError(f"perf 存在未知配置键：{', '.join(unknown)}")
    census = _rel_path(options["census"], "census") if "census" in options else ""
    spawn_max = (
        _non_negative_int(options["spawn_max"], "spawn_max")
        if "spawn_max" in options
        else DEFAULT_SPAWN_MAX
    )
    probe_max = (
        _non_negative_int(options["probe_max"], "probe_max")
        if "probe_max" in options
        else DEFAULT_PROBE_MAX
    )
    top = _positive_int(options["top"], "top") if "top" in options else DEFAULT_TOP
    return PerfPolicy(census=census, spawn_max=spawn_max, probe_max=probe_max, top=top)


def load_perf_policy(project_root: str | Path) -> PerfPolicy:
    """读取宿主配置并解析为 :class:`PerfPolicy`（无配置即全默认）。

    Raises:
        PerfGuardConfigError: guards.json 非法或 perf 段非法。
    """
    try:
        config = load_guards_config(project_root)
    except GuardsConfigError as exc:
        raise PerfGuardConfigError(f"guards 配置非法：{exc}") from exc
    entry = next((item for item in config.entries if item.name == "perf"), None)
    return parse_policy(entry.options if entry is not None else None)


def spawn_payload(
    counts: Mapping[str, int], spent: Mapping[str, float]
) -> dict[str, Any]:
    """spawn 普查结果（与 ``scripts/spawn_census.py`` 原输出结构逐字一致）。

    ``rows`` 按耗时降序、字段名 ``site`` / ``count`` / ``seconds``；JSON 始终全量
    （打印面由调用方切，不进契约）。
    """
    sites = sorted(set(counts) | set(spent), key=lambda site: -spent.get(site, 0.0))
    rows = [
        {"site": site, "count": counts.get(site, 0), "seconds": round(spent.get(site, 0.0), 3)}
        for site in sites
    ]
    return {
        "total_spawns": sum(counts.values()),
        "total_seconds": round(sum(spent.values()), 3),
        "rows": rows,
    }


def _empty_shard() -> dict[str, Any]:
    return {"count": 0, "wait": 0.0, "rc": {}, "cwd": {}, "timeout": {}, "exceptions": {}}


def merge_shards(shard_dir: Path) -> dict[str, dict[str, Any]]:
    """合并 g1 pid 分片：跨 worker 累加 count/wait，字典维度并集计数。

    分片键形如 ``"<kind>|<site>"``（采集侧字符串化），损坏分片跳过不抛。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for shard in sorted(Path(shard_dir).glob("g1-census-*.json")):
        try:
            data = json.loads(shard.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, Mapping):
            continue
        for key, rec in data.items():
            if not isinstance(rec, Mapping):
                continue
            # 先验后建槽：损坏记录不得留下 count=0 的幻影站点，也不得只并入半个记录。
            try:
                count = _as_count(rec.get("count", 0), "shard.count")
                wait = float(rec.get("wait", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            kind, _, site = str(key).partition("|")
            slot = merged.setdefault((kind, site), _empty_shard())
            slot["count"] += count
            slot["wait"] += wait
            for dim in ("rc", "cwd", "timeout", "exceptions"):
                values = rec.get(dim)
                if not isinstance(values, Mapping):
                    continue
                for name, value in values.items():
                    try:
                        slot[dim][name] = slot[dim].get(name, 0) + int(value)
                    except (TypeError, ValueError):
                        continue
    return {
        f"{kind} :: {site}": rec
        for (kind, site), rec in sorted(merged.items(), key=lambda kv: -kv[1]["wait"])
    }


def _as_count(value: object, field_name: str) -> int:
    """计数归一：数字原样（浮点/数字串**向上取整**，宁可误红不放过半单位越界），bool 拒绝。"""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} 不能是布尔值")
    try:
        return math.ceil(float(cast(Any, value)))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} 不是计数：{value!r}") from exc


def _site_count(key: str, rec: Mapping[str, Any]) -> int:
    """站点记录的计数（结构已在 :func:`judge_census` 前置校验，此处只做归一）。"""
    return _as_count(rec.get("count", 0), f"sites[{key}].count")


def _total_probes(sites: Mapping[str, Mapping[str, Any]]) -> int:
    return sum(_site_count(key, rec) for key, rec in sites.items())


def _top_sites(sites: Mapping[str, Mapping[str, Any]], top: int) -> list[dict[str, Any]]:
    """按计数降序取 TopN 站点（含 ``site`` / ``count``，与 spawn 侧行对齐）。"""
    rows = [{"site": key, "count": _site_count(key, rec)} for key, rec in sites.items()]
    rows.sort(key=lambda row: -int(cast(int, row["count"])))
    return rows[:top]


def _sites(payload: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    """取 ``sites`` 并做结构校验（``None`` 视为空；其它非对象/坏记录一律 TypeError）。"""
    raw = payload.get("sites")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TypeError("sites 必须是对象")
    broken = [key for key, rec in raw.items() if not isinstance(rec, Mapping)]
    if broken:
        raise TypeError("sites 记录必须是对象：" + ", ".join(map(str, broken)))
    for key, rec in raw.items():
        _as_count(rec.get("count", 0), f"sites[{key}].count")
    return raw


def _rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """取 ``rows``（必须是数组；非对象行丢弃），供 finding context 使用。"""
    raw = payload.get("rows", [])
    if not isinstance(raw, list):
        raise TypeError("rows 必须是数组")
    return [row for row in raw if isinstance(row, Mapping)]


def judge_census(
    payload: Mapping[str, Any], policy: PerfPolicy
) -> tuple[GuardFinding, ...]:
    """对一个普查结果给出结论（超限即 finding，附带 TopN 站点行）。"""
    findings: list[GuardFinding] = []
    try:
        spawns = _as_count(payload.get("total_spawns", 0), "total_spawns")
        sites = _sites(payload)
        if "total_probe_count" in payload:
            probes = _as_count(payload["total_probe_count"], "total_probe_count")
        else:
            probes = _total_probes(sites)
        rows = _rows(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"perf guard 降级（bad_census）：{exc}") from exc
    if policy.spawn_max and spawns > policy.spawn_max:
        findings.append(
            GuardFinding(
                guard="perf",
                message=(
                    f"git spawn 总数 {spawns} 超过上限 {policy.spawn_max}"
                    "（宿主可用 perf.spawn_max 调整或收敛调用点）"
                ),
                context={
                    "kind": "spawn_over",
                    "total": spawns,
                    "max": policy.spawn_max,
                    "rows": rows[: policy.top],
                },
            )
        )
    if policy.probe_max and probes > policy.probe_max:
        findings.append(
            GuardFinding(
                guard="perf",
                message=(
                    f"g1 探针总数 {probes} 超过上限 {policy.probe_max}"
                    "（宿主可用 perf.probe_max 调整或收敛探针调用）"
                ),
                context={
                    "kind": "probe_over",
                    "total": probes,
                    "max": policy.probe_max,
                    "sites": _top_sites(sites, policy.top),
                },
            )
        )
    return tuple(findings)


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """只读评估宿主普查产物（零子进程、不写盘）；未声明/不可读 → RuntimeError 降级。

    ``perf`` 默认 ``off``：没有配置时评估器根本不会被调用（done 期零额外子进程）。
    """
    if context.project_root is None:
        return ()
    try:
        policy = load_perf_policy(context.project_root)
    except PerfGuardConfigError as exc:
        raise RuntimeError(f"perf guard 配置非法：{exc}") from exc
    if not policy.census:
        raise RuntimeError(
            "perf guard 降级（no_census）：宿主未声明 perf.census 普查产物路径"
        )
    census_path = Path(context.project_root) / policy.census
    try:
        payload = json.loads(census_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"perf guard 降级（bad_census）：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("perf guard 降级（bad_census）：普查产物必须是 JSON 对象")
    return judge_census(payload, policy)


def register() -> None:
    """把本评估器登记到 guards 注册表（幂等）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator("perf", evaluate)


register()
