"""lifecycle.probe —— 自 ``orchd/worktree/lifecycle.py`` 拆出。

worktree 注册状态探测、有效性判定与孤儿清理。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.worktree.layout import _GIT_TIMEOUT

def _worktree_registered_paths(project_root: Path) -> set[str] | None:
    """`git worktree list --porcelain` 登记路径集合（normcase 归一化）。

    返回 None 表示 git 不可用/解析失败（调用方保守降级：视为有效、不删）。
    按行前缀解析（W-16 口径），路径经 resolve + normcase 归一化。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    paths: set[str] = set()
    for ln in (proc.stdout or "").splitlines():
        if not ln.startswith("worktree "):
            continue
        p = ln[len("worktree "):].strip()
        if p:
            try:
                paths.add(os.path.normcase(str(Path(p).resolve())))
            except OSError:
                paths.add(os.path.normcase(p))
    return paths


def _is_effective_worktree(project_root: Path, wt_path: Path) -> bool:
    """判定是否为有效 worktree（doctor/worktree 单一真源）。

    有效 ⇔ git 登记含该路径 **且** `git rev-parse --verify HEAD` 可解析。
    任一 git 探测失败 → 保守返回 True（视为有效，绝不误删）。
    无 `.git` 标记 → 返回 False（由调用方按无标记残留路径处理）。
    """
    if not (wt_path / ".git").exists():
        return False
    registered = _worktree_registered_paths(project_root)
    if registered is None:
        return True
    try:
        key = os.path.normcase(str(wt_path.resolve()))
    except OSError:
        return True
    if key not in registered:
        return False
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True
    return head.returncode == 0 and bool((head.stdout or "").strip())


_MAX_EVIDENCE_ENTRIES = 2000


def _probe_cleanup_state(project_root: Path, wt_path: Path) -> str:
    """清理前状态：effective / invalid / no_git / unknown（git 不可判定）。"""
    if not (wt_path / ".git").exists():
        return "no_git"
    registered = _worktree_registered_paths(project_root)
    if registered is None:
        return "unknown"
    try:
        key = os.path.normcase(str(wt_path.resolve()))
    except OSError:
        return "unknown"
    if key not in registered:
        return "invalid"
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"
    if head.returncode != 0 or not (head.stdout or "").strip():
        return "invalid"
    return "effective"


def _collect_cleanup_evidence(project_root: Path, wt_path: Path) -> dict[str, Any]:
    """删除/保留前取证：git status（含 ignored）、HEAD、目录清单与 sha256。

    ``manifest_sha256`` 只覆盖排序后的「路径/类型/大小」清单，不含时间戳，
    因此同一目录重复采集可复现；``.git`` 目录内部不展开（worktree 的 ``.git``
    通常是 gitfile，展开元数据既昂贵也无助于判定用户文件）。
    """
    import hashlib
    from datetime import datetime, timezone

    status: dict[str, Any]
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--ignored=matching",
             "--untracked-files=all"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        status = {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        status = {"returncode": None, "stdout": "", "stderr": str(exc)}
    head: str | None = None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0:
            head = (proc.stdout or "").strip() or None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        head = None
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        for path in sorted(wt_path.rglob("*")):
            rel = path.relative_to(wt_path).as_posix()
            if rel == ".git":
                size = 0 if path.is_dir() else path.stat().st_size
                entries.append({"path": ".git", "type": "dir" if path.is_dir() else "file",
                                "size": size})
                continue
            if rel.startswith(".git/"):
                continue
            if path.is_dir():
                entries.append({"path": rel, "type": "dir", "size": 0})
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                entries.append({"path": rel, "type": "file", "size": size})
            if len(entries) >= _MAX_EVIDENCE_ENTRIES:
                truncated = True
                break
    except OSError:
        pass
    manifest = "\n".join(
        f"{entry['path']}\t{entry['type']}\t{entry['size']}" for entry in entries)
    return {
        "status": status,
        "head": head,
        "entries": entries,
        "entries_truncated": truncated,
        "manifest_sha256": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_probe_evidence(project_root: Path, wt_path: Path, evidence: dict[str, Any],
                          action: str, reason: str) -> Path | None:
    """把取证与处置结果落到 rescue 目录，并写一条 recycle log。"""
    import json
    import time

    from orchd.worktree.recycle import _log_recycle, _recycle_actor
    try:
        base = project_root / ".orchd-runtime" / "probe-evidence"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{wt_path.name}-{time.time_ns()}.json"
        payload = {
            "target": str(wt_path),
            "action": action,
            "reason": reason,
            **evidence,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        _log_recycle([{"action": action, "target": str(wt_path), "reason": reason,
                       "evidence": None, "evidence_write_failed": True,
                       "actor": _recycle_actor()}])
        return None
    _log_recycle([{"action": action, "target": str(wt_path), "reason": reason,
                   "evidence": str(path), "actor": _recycle_actor()}])
    return path


def _cleanup_stale_task_wt(
    project_root: Path, wt_path: Path,
) -> dict[str, Any] | None:
    """best-effort 清理 worktree add 失败残留的半成品元数据（孤儿 worktree）。

    - ``git worktree prune``：移除指向已消失目录的失效 git 注册（安全）。
    - 有效 worktree 绝不删除；git 元数据不可判定（unknown）时保留原地。
    - 删除/保留前采集证据（git status 含 ignored、HEAD、目录清单与 sha256），
      证据路径与处置结果写入 recycle log。
    - 仅「无有效 ``.git`` 且无用户文件」的半成品目录才 rmtree；有未跟踪/
      ignored 用户文件时返回 ``preserved_with_evidence``，交 doctor/人工处置。
    任何失败静默跳过，不阻断降级主流程。
    """
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    if not wt_path.exists():
        return None
    state = _probe_cleanup_state(project_root, wt_path)
    if state == "effective":
        return {"action": "kept_effective", "target": str(wt_path)}
    evidence = _collect_cleanup_evidence(project_root, wt_path)
    user_files = [
        entry["path"] for entry in evidence["entries"]
        if entry["type"] == "file" and entry["path"] != ".git"
    ]
    dirty = bool((evidence["status"].get("stdout") or "").strip())
    if state == "unknown" or user_files or dirty:
        reason = ("metadata_unknown" if state == "unknown"
                  else "user_files_present" if user_files
                  else "git_status_not_clean")
        evidence_path = _write_probe_evidence(
            project_root, wt_path, evidence, "preserved_with_evidence", reason)
        return {"action": "preserved_with_evidence", "target": str(wt_path),
                "reason": reason, "evidence": str(evidence_path) if evidence_path else None}
    evidence_path = _write_probe_evidence(
        project_root, wt_path, evidence, "removed", "empty_half_product")
    try:
        wt_path.rmdir()
    except OSError:
        try:
            shutil.rmtree(str(wt_path), ignore_errors=True)
        except Exception:
            pass
    return {"action": "removed", "target": str(wt_path),
            "evidence": str(evidence_path) if evidence_path else None}


def _verify_task_wt(wt_path: Path, expected_branch: str) -> dict[str, Any]:
    """创建期不变量校验（W-4）：任务 worktree 检出分支恰为 task/<id> 且 HEAD 可解析。

    Returns:
        ``{"ok": True}`` 或 ``{"ok": False, "reason": str}``（best-effort，绝不抛异常）。
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return {"ok": False, "reason": f"任务 worktree HEAD 无法解析（{wt_path}）"}
        cur = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        actual = cur.stdout.strip() if cur.returncode == 0 else ""
        if actual != expected_branch:
            return {"ok": False,
                    "reason": f"任务 worktree 检出分支 {actual or '<null>'}，期望 {expected_branch}"}
        return {"ok": True}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"ok": False, "reason": f"worktree 校验异常: {exc}"}


def _task_branch_divergence(
    project_root: Path, branch: str, base: str
) -> dict[str, int] | None:
    """任务分支相对主分支的超前/落后计数（best-effort，测不到返回 None）。

    - ``ahead``：分支独有提交数（``base..branch``）；
    - ``behind``：主分支独有提交数（``branch..base``）。
    任一 git 调用失败 → None（调用方按"无法判定"处理，不阻断复用）。
    """
    try:
        ahead = subprocess.run(
            ["git", "rev-list", "--count", f"{base}..{branch}"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        behind = subprocess.run(
            ["git", "rev-list", "--count", f"{branch}..{base}"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if ahead.returncode != 0 or behind.returncode != 0:
            return None
        return {"ahead": int(ahead.stdout.strip()), "behind": int(behind.stdout.strip())}
    except (subprocess.SubprocessError, FileNotFoundError, OSError, ValueError):
        return None


def _worktree_binding_ok(wt_path: Path, branch: str) -> bool:
    """``.git`` 存在且 HEAD 确实绑定到 ``branch`` 才算 restored。

    ``git worktree remove`` 返回非零时 worktree 通常仍在，但"路径还在"不等于
    "绑定仍有效"（可能是空目录 / 已 detached / 已指向别的分支）。只有两者都
    成立才允许调用方按"复用有效 worktree"处置；否则必须保留
    ``restored=False``，由上层走半状态/人工处置，避免把错误 checkout 交付给 agent。
    """
    if not (wt_path / ".git").exists():
        return False
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0 and (proc.stdout or "").strip() == branch


def _recreate_task_wt_from_base(
    project_root: Path, wt_path: Path, branch: str, base: str
) -> dict[str, Any]:
    """纯陈旧分支的安全重建（仅调用方确认 ahead == 0 且工作区干净后调用）。

    ``git worktree remove --force`` + ``git branch -d``（安全删除：ahead == 0
    意味着分支 tip 已合入 base，``-d`` 必成功；用 ``-d`` 不用 ``-D``，语义即断言）。

    Returns:
        ``{"ok": True}``：重建成功，调用方落创建流；
        ``{"ok": False, "reason": str, "half_state": bool, "restored": bool}``：任一步
        失败——**禁止半状态**：worktree 已移除但分支安全删除失败时，先用
        ``git worktree add <wt> <branch>`` 把绑定装回（``restored=True`` /
        ``half_state=False``，调用方须再补容器标记与副本抑制，见
        :func:`ensure_task_wt`）；装回也失败才置 ``half_state=True``，调用方**不得**
        再以该路径作为 ``worktree`` 交付（路径已不存在）。
    """
    try:
        rm = subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if rm.returncode != 0:
            detail = (rm.stderr or "").strip()[:200]
            binding_ok = _worktree_binding_ok(wt_path, branch)
            reason = f"worktree 移除失败：{detail}"
            reason += ("；原 worktree 绑定仍有效，按复用处置" if binding_ok
                       else "；原 worktree 绑定已失效（.git 缺失或 HEAD 未指向任务分支），"
                            "需人工处置")
            return {"ok": False, "reason": reason,
                    "half_state": not binding_ok, "restored": binding_ok}
        delete = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if delete.returncode == 0:
            return {"ok": True}
        detail = (delete.stderr or "").strip()[:200]
        # 分支删除失败：worktree 已移除 → 必须把绑定装回，否则留下「路径存在但不再
        # 指向 checkout / worktree 注册已删」的半状态（后续复用会绑到空目录）。
        restore = subprocess.run(
            ["git", "worktree", "add", str(wt_path), branch],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        restored = restore.returncode == 0 and (wt_path / ".git").exists()
        reason = f"分支安全删除失败：{detail}"
        if restored:
            # remove+add 会丢掉 worktree 级运行态（sparse-checkout 抑制、容器标记、
            # vendored 引擎副本），调用方须重跑这些入口后文案才配称「装回」。
            reason += "；worktree 绑定已装回原分支（运行态由调用方重建）"
        else:
            detail2 = (restore.stderr or "").strip()[:200]
            reason += (
                "；worktree 绑定装回亦失败（需人工处置：git worktree add <路径> "
                f"<分支>）：{detail2}"
            )
        return {"ok": False, "reason": reason,
                "half_state": not restored, "restored": restored}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        # 异常可能发生在 remove 之前（超时 / git 不可用）——worktree 仍可能完好。
        # 只有绑定真的不在了才算半状态，否则必须按"复用有效 worktree"处置：
        # 误报半状态会把调用方踢回主工作树（container 布局下是共享根，写脏/串卡）。
        intact = (wt_path / ".git").exists()
        return {"ok": False, "reason": f"重建异常：{exc}",
                "half_state": not intact, "restored": intact}
