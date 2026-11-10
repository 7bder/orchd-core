"""orchd CLI 路由：docs 契约文档产物子命令组（task-docsgen-manifest / gate）。

  - ``docs list``：输出 manifest 清单（id/kind/artifact/generator/sources/sync/gate/host）
    与命令元数据（name/args/read_only/requires_lock/purpose），只读。
  - ``docs check``：逐产物比对生成器输出与磁盘内容（``file`` 整文件 / ``block`` 生成块）；
    退出码 0 干净 / 1 漂移 / 2 配置错。宿主布局读 ``<host>/.orchd/docs/_generated``，
    无 deps.json 时跳过 dependency-graph（不误报空图漂移）。
  - ``docs write``：按 manifest 写回产物；``--changed`` 只写受影响产物。
  - ``docs sync --point {done,merge}``：按同步点单写（done = 稳定产物与正文生成块；
    merge = dependency-graph，避免 done 抢写波动数据）。

只读边界：``list`` / ``check`` 不写盘；``write`` / ``sync`` 只写 manifest 登记的产物。
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from orchd import docs_manifest
from orchd.cli._util import _find_orchd_dir
from orchd.cli.manifest import command_payload
from orchd.docsgen import blocks
from orchd.repo_role import is_engine_repo

__all__ = ("register",)

#: 无 deps.json 时不做内容比对的产物（空图与真图必然不同，避免宿主误报）。
_DEPS_DEPENDENT = frozenset({"dependency-graph"})
#: 宿主安装器快照清单（相对 ``.orchd/`` 的 path → sha256）。
_INSTALL_MANIFEST = ".install-manifest.json"
#: 生成块协议非法类 reason（配置错，退出码 2）。
_BLOCK_CONFIG_REASONS = frozenset({"block_invalid", "block_markers_missing"})


def _project_root_from_cwd() -> Path:
    """与其它 CLI 命令同口径：先找 ``.orchd``，项目根为其父目录。"""
    return Path(_find_orchd_dir()).parent


def _host_layout(project_root: Path) -> bool:
    """是否宿主布局（引擎装在 ``.orchd/orchd``，产物快照在 ``.orchd/docs``）。"""
    return not is_engine_repo(project_root) and (project_root / ".orchd" / "orchd").is_dir()


def _artifact_root(project_root: Path) -> Path:
    """产物根：引擎仓 = 项目根；宿主布局 = ``<host>/.orchd``。"""
    return project_root / ".orchd" if _host_layout(project_root) else project_root


def _artifact_path(project_root: Path, entry: docs_manifest.DocArtifact) -> Path:
    """单产物落盘路径（宿主布局自动落到 ``.orchd/docs/_generated``）。"""
    return _artifact_root(project_root) / entry.artifact


def _deps_json_path(project_root: Path) -> Path:
    """deps 真源路径（宿主同样在 ``.orchd/shared/deps.json``）。"""
    return project_root / ".orchd" / "shared" / "deps.json"


def _skip_without_deps(project_root: Path, entry: docs_manifest.DocArtifact) -> bool:
    """是否跳过该产物。

    - ``engine-only`` 产物（dependency-graph）：宿主仓一律跳过——即使宿主跑了
      ``deps init`` 生成了 deps.json，也不执行 engine-only 生成器；
    - 其余产物：无 deps.json 时同样跳过（空图与真图不可比）。
    """
    if entry.host == "engine-only" and _host_layout(project_root):
        return True
    return entry.id in _DEPS_DEPENDENT and not _deps_json_path(project_root).is_file()


def _install_manifest_files(project_root: Path) -> dict[str, str] | None:
    """读宿主安装器快照清单；缺失/损坏返回 ``None``（调用方按缺快照 skip）。"""
    path = project_root / ".orchd" / _INSTALL_MANIFEST
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else None


def _file_sha256(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _check_host_entry(entry: docs_manifest.DocArtifact, root: Path) -> dict | None:
    """宿主只读快照校验：只比对安装器清单 sha256，不执行生成器。

    清单核对先于磁盘读取：缺安装清单 / 清单无该路径 → ``docs_snapshot_missing``
    （调用方按 skip 处理）。正文生成块不进安装器快照清单（随安装文档分发），
    无键一律 skip，不因宿主文档缺失误报漂移。
    """
    files = _install_manifest_files(root)
    expected = (files or {}).get(entry.artifact)
    if not expected:
        return {
            "id": entry.id, "artifact": entry.artifact,
            "reason": "docs_snapshot_missing",
            "hint": (
                f"安装器快照清单缺少 {entry.artifact}；"
                "重新运行安装器（或 --update）生成 .orchd/.install-manifest.json"
            ),
        }
    path = _artifact_path(root, entry)
    actual = _file_sha256(path)
    if actual is None:
        return {
            "id": entry.id, "artifact": entry.artifact,
            "reason": "artifact_missing",
        }
    if actual == expected:
        return None
    return {
        "id": entry.id,
        "artifact": entry.artifact,
        "reason": "content_drift",
        "expected_sha256": expected,
        "actual_sha256": actual,
        "hint": "宿主产物已被修改；重新运行安装器 --update 覆盖快照",
    }


def _read_text(path: Path) -> str | None:
    """只读文本；缺失 / 不可读 / 非 UTF-8 返回 ``None``。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_block(text: str, artifact_id: str) -> str | None:
    """取生成块内容（不含标记行；缺失标记返回 ``None``）。"""
    return blocks.extract_block(text, artifact_id)


def _replace_block(text: str, artifact_id: str, body: str) -> str:
    """替换生成块内容（保留标记；协议非法时抛 :class:`blocks.BlockError`）。"""
    return blocks.replace_block(text, artifact_id, body)


def _artifact_payload(entry: docs_manifest.DocArtifact, root: Path) -> dict:
    path = _artifact_path(root, entry)
    return {
        "id": entry.id,
        "kind": entry.kind,
        "artifact": entry.artifact,
        "generator": entry.generator,
        "sources": list(entry.sources),
        "sync": list(entry.sync),
        "gate": entry.gate,
        "host": entry.host,
        "exists": path.is_file(),
    }


def _expected(entry: docs_manifest.DocArtifact) -> str:
    func = docs_manifest.generator_for(entry.generator)
    if func is None:
        raise ValueError(f"生成器不可用：{entry.generator}")
    return func()


def _check_entry(entry: docs_manifest.DocArtifact, root: Path) -> dict | None:
    """单产物比对；一致返回 ``None``，否则返回 drift 条目。"""
    expected = _expected(entry)
    path = _artifact_path(root, entry)
    disk = _read_text(path)
    if disk is None:
        return {
            "id": entry.id, "artifact": entry.artifact,
            "reason": "artifact_missing",
        }
    if entry.kind == "block":
        problems = blocks.validate_blocks(disk)
        if problems:
            return {
                "id": entry.id, "artifact": entry.artifact,
                "reason": "block_invalid",
                "message": "; ".join(problems[:8]),
            }
    try:
        actual = _extract_block(disk, entry.id) if entry.kind == "block" else disk
    except blocks.BlockError as exc:
        return {
            "id": entry.id, "artifact": entry.artifact,
            "reason": "block_invalid", "message": str(exc),
        }
    if actual is None:
        return {
            "id": entry.id, "artifact": entry.artifact,
            "reason": "block_markers_missing",
        }
    if actual == expected:
        return None
    return {
        "id": entry.id,
        "artifact": entry.artifact,
        "reason": "content_drift",
        "expected_sha256": _sha(expected),
        "actual_sha256": _sha(actual),
    }


def _cmd_docs_list(args: argparse.Namespace) -> tuple[dict, int]:
    """``docs list``：只读输出清单与命令元数据。"""
    del args
    root = _project_root_from_cwd()
    entries, problems = docs_manifest.resolve_manifest()
    payload = {
        "ok": not problems,
        "command": "docs list",
        "artifacts": [_artifact_payload(entry, root) for entry in entries],
        "commands": command_payload(),
    }
    if problems:
        payload["errors"] = problems
    return payload, 0 if not problems else 2


def _cmd_docs_check(args: argparse.Namespace) -> tuple[dict, int]:
    """``docs check``：逐产物内容比对（0 干净 / 1 漂移 / 2 配置错）。

    宿主布局只读比对安装器快照（``.orchd/.install-manifest.json`` 的 sha256），
    不执行任何生成器；缺快照的产物按 skip 处理并给 ``docs_snapshot_missing`` 提示。
    生成块协议非法（``block_invalid`` / ``block_markers_missing``）属配置错，退出码 2。
    """
    del args
    root = _project_root_from_cwd()
    entries, problems = docs_manifest.resolve_manifest()
    if problems:
        return {"ok": False, "command": "docs check", "errors": problems}, 2
    host = _host_layout(root)
    drift: list[dict] = []
    skipped: list[str] = []
    snapshot_warnings: list[dict] = []
    checked = 0
    for entry in entries:
        if _skip_without_deps(root, entry):
            skipped.append(entry.artifact)
            continue
        try:
            item = (
                _check_host_entry(entry, root)
                if host
                else _check_entry(entry, root)
            )
        except Exception as exc:  # noqa: BLE001 - 生成器/产物求值错误按配置错
            return {
                "ok": False,
                "command": "docs check",
                "errors": [f"{entry.id}: {type(exc).__name__}: {exc}"],
            }, 2
        if item is not None and item.get("reason") == "docs_snapshot_missing":
            skipped.append(entry.artifact)
            snapshot_warnings.append(item)
            continue
        # 只统计真正完成比对的产物：快照缺失按 skip 计，不进 checked。
        checked += 1
        if item is not None:
            drift.append(item)
    block_config_error = any(
        item.get("reason") in _BLOCK_CONFIG_REASONS for item in drift
    )
    payload: dict = {
        "ok": not drift,
        "command": "docs check",
        "mode": "host-snapshot" if host else "engine-generated",
        "checked": checked,
        "skipped": skipped,
        "drift": drift,
    }
    if snapshot_warnings:
        payload["warnings"] = snapshot_warnings
    if block_config_error:
        payload["errors"] = [
            f"{item['id']}: {item.get('reason')} {item.get('message', '')}".strip()
            for item in drift
            if item.get("reason") in _BLOCK_CONFIG_REASONS
        ]
    code = 0
    if block_config_error:
        code = 2
    elif drift:
        code = 1
    return payload, code


def _entries_for_changed(
    args: argparse.Namespace, entries: list[docs_manifest.DocArtifact]
) -> list[docs_manifest.DocArtifact]:
    changed = [str(item) for item in (getattr(args, "changed", None) or [])]
    if not changed:
        return entries
    return [entry for entry in entries if docs_manifest.matches_changed(entry, changed)]


def _write_entry(entry: docs_manifest.DocArtifact, root: Path) -> str:
    expected = _expected(entry)
    path = _artifact_path(root, entry)
    if entry.kind == "block":
        disk = _read_text(path)
        text = blocks.upsert_block(disk or "", entry.id, expected)
    else:
        text = expected
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return entry.artifact


def _cmd_docs_write(args: argparse.Namespace) -> tuple[dict, int]:
    """``docs write``：按 manifest 写回；``--changed`` 只写受影响产物。"""
    root = _project_root_from_cwd()
    entries, problems = docs_manifest.resolve_manifest()
    if problems:
        return {"ok": False, "command": "docs write", "errors": problems}, 2
    selected = _entries_for_changed(args, entries)
    written: list[str] = []
    errors: list[str] = []
    for entry in selected:
        try:
            written.append(_write_entry(entry, root))
        except Exception as exc:  # noqa: BLE001 - 单产物写失败结构化返回
            errors.append(f"{entry.id}: {type(exc).__name__}: {exc}")
    payload = {
        "ok": not errors,
        "command": "docs write",
        "written": written,
        "skipped": [entry.artifact for entry in entries if entry not in selected],
    }
    if errors:
        payload["errors"] = errors
        return payload, 2
    return payload, 0


def _cmd_docs_sync(args: argparse.Namespace) -> tuple[dict, int]:
    """``docs sync --point <done|merge>``：按同步点单写产物。

    ``done`` 写稳定 file 产物与正文生成块；``merge`` 只写 dependency-graph
    （波动数据不在 done 抢写）。无 deps.json 时 dependency-graph 跳过（宿主快照保持只读）。
    """
    root = _project_root_from_cwd()
    entries, problems = docs_manifest.resolve_manifest()
    if problems:
        return {"ok": False, "command": "docs sync", "errors": problems}, 2
    point = args.point
    selected = [
        entry for entry in entries if docs_manifest.sync_point(entry) == point
    ]
    written: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for entry in selected:
        if _skip_without_deps(root, entry):
            skipped.append(entry.artifact)
            continue
        try:
            written.append(_write_entry(entry, root))
        except Exception as exc:  # noqa: BLE001 - 单产物写失败结构化返回
            errors.append(f"{entry.id}: {type(exc).__name__}: {exc}")
    payload = {
        "ok": not errors,
        "command": "docs sync",
        "point": point,
        "written": written,
        "skipped": skipped,
    }
    if errors:
        payload["errors"] = errors
        return payload, 2
    return payload, 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """注册 ``docs`` 二级子命令组（list / check / write / sync）。"""
    parser = subparsers.add_parser(
        "docs",
        help="契约文档产物清单与生成：docs list / check / write / sync",
    )
    sub = parser.add_subparsers(dest="docs_command", metavar="<子命令>")

    list_parser = sub.add_parser("list", help="输出 manifest 清单与命令元数据（只读）")
    list_parser.set_defaults(func=_cmd_docs_list)

    check_parser = sub.add_parser(
        "check", help="逐产物比对（0 干净 / 1 漂移 / 2 配置错）"
    )
    check_parser.set_defaults(func=_cmd_docs_check)

    write_parser = sub.add_parser("write", help="按 manifest 写回产物")
    write_parser.add_argument(
        "--changed", nargs="*", default=[],
        help="只写受影响产物（产物自身或真源命中改动集）",
    )
    write_parser.set_defaults(func=_cmd_docs_write)

    sync_parser = sub.add_parser(
        "sync", help="按同步点单写产物（done = 稳定产物与正文生成块；merge = dependency-graph）"
    )
    sync_parser.add_argument(
        "--point",
        choices=sorted(docs_manifest.SYNC_POINTS - {"manual"}),
        required=True,
        help="同步点（done / merge）",
    )
    sync_parser.set_defaults(func=_cmd_docs_sync)
