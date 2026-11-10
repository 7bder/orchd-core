"""declare 撤回日志与共享面声明判定（_log_decl_withdraw/_decl_withdraw_branch_overlap/
_*_decl_path/_surface_hit/共享面准入与刷新闸门），叶域。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError


def _log_decl_withdraw(
    task_id: str,
    withdrawn: list[dict[str, str]],
    not_present: list[dict[str, str]],
    branch_overlap: list[str],
) -> None:
    """声明撤回留痕（stderr，结构化；task-decl-withdraw-channel）。

    撤回是声明域变更，必须可追溯——尤其 ``branch_overlap``：被撤回的声明仍出现在
    该任务分支改动集中时，review 期 E010 声明完整性会反向告警。与 session/amend
    降级留痕同风格，不受 ``ORCHD_QUIET`` 抑制；任何异常静默跳过，不影响补丁主流程。
    """
    import json
    import sys

    payload: dict[str, Any] = {
        "action": "decl_withdraw",
        "task_id": task_id,
        "withdrawn": withdrawn,
    }
    if not_present:
        payload["not_present"] = not_present
    if branch_overlap:
        payload["branch_overlap"] = branch_overlap
        payload["hint"] = ("被撤回的声明仍出现在该任务分支改动集中：撤回会使 review 期 E010 "
                           "声明完整性反向告警，请确认是否应先处理该分支再撤回")
    # 跨 mypy 版本免疫：getattr 取回 Any，无需 type-ignore（旧 ignore 码
    # [attr-defined] 在新版报 union-attr，pass8 F3）；缺失/异常仍静默跳过。
    _reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        print(
            f"orchd ▸ [amend-decl] {json.dumps(payload, ensure_ascii=False)}",
            file=sys.stderr)
    except Exception:
        pass


def _decl_withdraw_branch_overlap(project_root: Path, task_id: str,
                                  paths: list[str]) -> list[str]:
    """被撤回的声明路径中，仍出现在该任务分支改动集里的部分（best-effort）。

    ``git diff --name-only <base>...task/<id>``（三点 = 自分叉点起的改动）与本批
    撤回路径求交。非 git / 分支不存在 / git 不可用 / 探测超时 → 返回空列表（保守
    跳过：绝不因探测失败而误报重叠）。
    """
    import subprocess

    if not paths:
        return []
    # task-line-diag-wiring：分支名与 base 按当前线解析（单线恒 task/{id} / main）
    from orchd.line_ctx import resolve_task_branch_for, resolve_trunk_for

    branch = resolve_task_branch_for(project_root, task_id)
    try:
        exists = subprocess.run(
            [
                "git", "-C",
                str(project_root), "rev-parse", "--verify", "--quiet", branch
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if exists.returncode != 0:
            return []
        base = resolve_trunk_for(project_root)
        probe = subprocess.run(
            [
                "git", "-C",
                str(project_root), "rev-parse", "--verify", "--quiet", base
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if probe.returncode != 0:
            base = "master"
        diff = subprocess.run(
            [
                "git", "-C",
                str(project_root), "diff", "--name-only", f"{base}...{branch}"
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if diff.returncode != 0:
            return []
        changed = {ln.strip() for ln in diff.stdout.splitlines() if ln.strip()}
        return sorted(changed & set(paths))
    except (OSError, subprocess.SubprocessError):
        return []


# ── 共享面准入闸门（task-shared-surface-admission，S3）──────────────────────
# 注册/修订**新增**的写集声明若命中共享面路径，按面模式分档处置：
#   fragments → 硬拒，并给出结构化替代通道（done --surface-entry / changelog 片段）；
#   generated → 硬拒，无逃生口（唯一写通道是生成器命令）；
#   serial    → 仅 warning，附建议 depends_on（把同面写者串行化）。
# 例外通道：--allow-shared-write <path> --reason "<非空理由>"（fragments 视为串行例外；
# generated 不因该旗标放行）。只检查**新增**声明（previous = 快照存量定义）：纯撤回
# （--remove-files-to-edit）天然放行；历史 pending 卡的既有声明不由本闸门拦截（迁移由
# S3 迁移脚本单独完成）。本闸门属 surface-admission 域，E007 只在这些独立 helper 内
# raise，**不进入** amend 分段承运集登记（tests/test_control_amend_split.py 的
# ALL_CARRIERS 与 tests/test_e007_reasons.py 的 REASON_SITES 仅覆盖 amend 拆分段）。
_ACTIVE_SURFACE_DECLARERS = frozenset({"pending", "claimed", "in_review"})


def _normalize_decl_path(raw: Any) -> str:
    """声明路径归一化：``\\``→``/``、去 ``./`` 前缀、折叠 ``//``/``..``、casefold。"""
    if not isinstance(raw, str):
        return ""
    import posixpath

    text = raw.strip().replace("\\", "/").casefold()
    while text.startswith("./"):
        text = text[2:]
    while "//" in text:
        text = text.replace("//", "/")
    if not text:
        return ""
    return posixpath.normpath(text).strip("/")


def _absolute_decl_path(raw: Any) -> bool:
    """绝对路径/盘符路径声明：直接阻断（禁借未归一化路径绕过共享面比对）。"""
    if not isinstance(raw, str):
        return False
    text = raw.strip().replace("\\", "/")
    return text.startswith("/") or (len(text) > 1 and text[1] == ":")


def _declared_paths(task: Any, *, include_exempt: bool = True) -> list[tuple[str, str]]:
    """任务声明的共享面候选路径 → [(原始, 归一化)]。

    ``include_exempt`` 控制是否把 ``exempt_files`` 计入：它是「越界提交豁免」而非
    写集声明。注册新卡时以 ``files_to_edit`` 为本任务的写集真源；对既有任务补登
    豁免仍需检查（见 :func:`_surface_admission_gate` 的 ``exempt_policy``）。
    """
    if not isinstance(task, dict):
        return []
    out: list[tuple[str, str]] = []
    fields = ("files_to_edit", "exempt_files") if include_exempt else ("files_to_edit",)
    for field in fields:
        values = task.get(field)
        if not isinstance(values, list):
            continue
        for raw in values:
            key = _normalize_decl_path(raw)
            if key:
                out.append((str(raw), key))
    return out


def _surface_hit(path_key: str, specs: dict[str, Any]) -> tuple[Any, bool] | None:
    """归一化声明键命中共享面 → (SurfaceSpec, 是否整目录声明)；未命中返回 None。"""
    for spec in specs.values():
        if path_key == _normalize_decl_path(getattr(spec, "path", "")):
            return spec, False
        surface_dir = getattr(spec, "dir", None)
        dir_key = _normalize_decl_path(surface_dir) if surface_dir else ""
        if dir_key and (path_key == dir_key or path_key.startswith(dir_key + "/")):
            return spec, True
    return None


def _active_declarer_statuses(store: Any) -> dict[str, str]:
    """账本投影中的任务状态（无 store / 空账本时为空 dict → 建议面退化为全量）。"""
    if store is None:
        return {}
    try:
        state = store.replay()
    except Exception:
        return {}
    return {tid: getattr(st, "status", "") for tid, st in state.items()}


def _serial_depends_on(
    master: Any, spec: Any, task_id: str, statuses: dict[str, str],
) -> list[str]:
    """serial 面建议 depends_on：其它活动任务中声明了同一路径的任务 id（排序）。"""
    target = _normalize_decl_path(getattr(spec, "path", ""))
    out: list[str] = []
    for other in getattr(master, "tasks", None) or []:
        other_id = other.get("id", "") if isinstance(other, dict) else ""
        if not other_id or other_id == task_id:
            continue
        from orchd.contracts.events import TERMINAL_STATUSES

        other_status = statuses.get(other_id, "") if statuses else ""
        if other_status in TERMINAL_STATUSES:
            continue
        if statuses and other_status not in _ACTIVE_SURFACE_DECLARERS:
            continue
        if any(key == target for _raw, key in _declared_paths(other)):
            out.append(other_id)
    out.sort()
    return out


def _fragments_usage(task_id: str) -> str:
    """fragments 面的结构化替代通道用法（错误 details 用）。"""
    return (
        "python .orchd/__main__.py done --task "
        f"{task_id} --changelog-category added --changelog-summary \"<一句摘要>\""
        "（等价结构化入口：done --surface-entry "
        "'{\"surface\": \"changelog\", \"category\": \"added\", "
        "\"summary\": \"...\"}'）"
    )



def _surface_refresh_gate(
    *,
    master: Any,
    project_root: Path,
    scope_task: str | None = None,
) -> list[dict[str, Any]]:
    """generated 面刷新声明闸门：归一化 + 全套校验，返回信息型 warning。

    ``surface_refresh`` 只允许 generated 面、reason 非空、path/generator 与注册表
    一致、不与 files_to_edit/exempt_files 重复；不合规聚合 raise E007（不落盘）。
    命中时写 ``surface_refresh_declared`` warning（可审计：哪个任务刷新哪个面）。
    """
    from orchd import surfaces as _surfaces

    specs = _surfaces.load_surfaces(project_root)
    warnings: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for task in getattr(master, "tasks", None) or []:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id", "")
        if scope_task is not None and task_id != scope_task:
            continue
        entries = task.get(_surfaces.SURFACE_REFRESH_FIELD)
        if not entries:
            continue
        try:
            normalized = _surfaces.normalize_refresh_entries(
                specs, entries,
                declared_paths=[raw for raw, _key in _declared_paths(task)],
            )
        except OrchdError as exc:
            details = exc.details if isinstance(exc.details, list) else []
            for detail in details:
                item = dict(detail) if isinstance(detail, dict) else {}
                item.setdefault("task_id", task_id)
                item.setdefault("rule", "surface_refresh")
                blocking.append(item)
            if not details:
                blocking.append({
                    "rule": "surface_refresh",
                    "task_id": task_id,
                    "reason": exc.message,
                })
            continue
        task[_surfaces.SURFACE_REFRESH_FIELD] = normalized
        for entry in normalized:
            warnings.append({
                "type": "surface_refresh_declared",
                "task_id": task_id,
                "surface": entry["surface"],
                "path": entry["path"],
                "reason": entry["reason"],
                "message": (f"generated 面 {entry['path']} 由本任务受控刷新"
                            f"（{entry['generator']}）：{entry['reason']}"),
            })
    if blocking:
        raise OrchdError(
            ErrorCode.E007,
            f"surface_refresh_blocked: {len(blocking)} 条刷新声明未通过校验",
            blocking,
        )
    return warnings

def _surface_admission_gate(
    *,
    master: Any,
    existing_tasks: dict[str, dict[str, Any]],
    project_root: Path,
    store: Any = None,
    allow_shared_write: list[str] | None = None,
    surface_reason: str | None = None,
    scope_task: str | None = None,
    exempt_policy: str = "all",
) -> list[dict[str, Any]]:
    """共享面准入闸门（只拦本次新增声明），返回放行类 warning 列表。

    硬拒项（fragments / generated / 缺 reason 的 allow）聚合后一次 raise E007；
    details 每项含 surface/mode/path/declared/usage/hint（Windows 声明路径已归一化）。

    ``exempt_policy`` 决定 ``exempt_files`` 的检查口径：

    - ``"all"``（默认）：两个声明域都查（revive / 迁移 / 单测直调的严格口径）；
    - ``"amend_only"``：新注册任务的 ``exempt_files`` 视作「越界提交豁免」而**不是**
      写集声明，只查既有任务的豁免新增。注册期即按整份写集声明的只有
      ``files_to_edit``；否则「信封透传 optional 字段」会被误判为片段面整文件声明。
    """
    from orchd import surfaces as _surfaces

    specs = _surfaces.load_surfaces(project_root)
    allowed = {
        key for key in (_normalize_decl_path(p) for p in (allow_shared_write or []))
        if key
    }
    reason_text = (
        surface_reason.strip() if isinstance(surface_reason, str) else ""
    )
    statuses_cache: dict[str, dict[str, str]] = {}

    def _statuses() -> dict[str, str]:
        if "value" not in statuses_cache:
            statuses_cache["value"] = _active_declarer_statuses(store)
        return statuses_cache["value"]

    warnings: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []

    for task in getattr(master, "tasks", None) or []:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id", "")
        if scope_task is not None and task_id != scope_task:
            continue
        previous = existing_tasks.get(task_id) or {}
        check_exempt = exempt_policy == "all" or bool(previous)
        previous_keys = {
            key for _raw, key in _declared_paths(previous, include_exempt=check_exempt)
        }
        added: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw, key in _declared_paths(task, include_exempt=check_exempt):
            if key in previous_keys or key in seen:
                continue
            seen.add(key)
            added.append((raw, key))
        for raw, key in added:
            if _absolute_decl_path(raw):
                blocking.append({
                    "rule": "surface_admission",
                    "task_id": task_id,
                    "reason": "declaration_path_not_relative",
                    "declared": raw,
                    "channel": "amend" if previous else "register",
                    "hint": ("文件写集路径必须是项目内相对路径；绝对路径/盘符路径"
                             "无法与共享面注册表归一化比对，直接阻断"),
                })
                continue
            hit = _surface_hit(key, specs)
            if hit is None:
                continue
            spec, whole_dir = hit
            mode = getattr(spec, "mode", "")
            detail: dict[str, Any] = {
                "rule": "surface_admission",
                "task_id": task_id,
                "surface": spec.name,
                "mode": mode,
                "path": getattr(spec, "path", ""),
                "declared": raw,
                "channel": "amend" if previous else "register",
                "allow_shared_write": key in allowed,
            }
            if whole_dir:
                detail["declared_dir"] = getattr(spec, "dir", "") or ""
            if mode == "generated":
                command = _surfaces.generator_command(getattr(spec, "generator", None))
                detail.update({
                    "reason": "generated_surface_rejected",
                    "allow_shared_write_permitted": False,
                    "generator_command": command,
                    "usage": command,
                    "hint": ("generated 面由生成器独占写入，禁止在文件写集里直接声明；"
                             "需刷新时执行上述生成器命令（或交由对应生成任务交付）"),
                })
                blocking.append(detail)
                continue
            if key in allowed and not reason_text:
                detail.update({
                    "reason": "allow_shared_write_reason_missing",
                    "hint": ("--allow-shared-write 必须配非空 --reason \"<理由>\"；"
                             "该旗标仅放行 fragments / serial 的串行例外"),
                })
                blocking.append(detail)
                continue
            if mode == "fragments":
                if key in allowed:
                    warnings.append({
                        **detail,
                        "type": "surface_declaration_override",
                        "allow_reason": reason_text,
                        "message": (f"{spec.path} 属 fragments 面，经 --allow-shared-write "
                                    "显式放行（串行例外）"),
                    })
                    continue
                detail.update({
                    "reason": "fragments_surface_rejected",
                    "allow_shared_write_permitted": True,
                    "usage": _fragments_usage(task_id),
                    "hint": ("fragments 面按片段交付，不得以任务写集声明整份文件；"
                             "确需串行例外时加 --allow-shared-write <path> --reason <理由>"),
                })
                blocking.append(detail)
                continue
            # serial 面：默认只 warning（不阻断），并给串行化建议。
            if key in allowed:
                warnings.append({
                    **detail,
                    "type": "surface_declaration_override",
                    "allow_reason": reason_text,
                    "message": (f"{spec.path} 属 serial 面，经 --allow-shared-write "
                                "显式放行（串行写入）"),
                })
                continue
            warnings.append({
                **detail,
                "type": "serial_surface_declaration",
                "suggested_depends_on": _serial_depends_on(
                    master, spec, task_id, _statuses()),
                "message": (f"{spec.path} 属 serial 面；建议用 depends_on 让同面写者串行，"
                            "或加 --allow-shared-write <path> --reason <理由>"),
            })

    if blocking:
        raise OrchdError(
            ErrorCode.E007,
            (f"surface_admission_blocked: {len(blocking)} 条共享面声明未通过准入"
             "（按 details 改走片段/生成器通道，或撤回该声明后重试）"),
            blocking,
        )
    return warnings