"""orchd/line_sync.py — 跨线回移通道（task-line-sync）。

设计真源：``design/line-split-design-20260923.md`` §6。四步协议：

1. **定位源提交**：校验 ``source_sha`` 可达 ``source_line`` 的 trunk（不可达即拒绝，
   防搬运幽灵提交）；
2. **目标线建回移任务**：生成回移任务**提案**（携带 ``backport={source_sha, source_line}``），
   落 ``.orchd/proposals/<task_id>.json``；
3. 走目标线自己的审查与回归（由正常任务管线承担，本模块不越权）；
4. **双线各留痕**：返回回移记录 ``{task_id, source_sha, target_line, event}``。

**禁影子改动**：本模块只生成提案，绝不直接向目标线 trunk 写提交。
**不新增事件类型**（事件语义属停服边界）——回移留痕由提案 + 任务自身事件承担。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.line import line_registry

_PROPOSAL_DIR = ("proposals",)


def _git(project_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(project_root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def _load_registry(project_root: Path) -> dict[str, str]:
    """读 canonical master 的 line → trunk 映射；无 master / 未配置 → 单线 default。"""
    from orchd.spec import load_master
    from orchd.worktree import resolve_master_path_from_dir

    master_path = resolve_master_path_from_dir(Path(project_root) / ".orchd")
    if not master_path.is_file():
        return line_registry(None)
    return line_registry(load_master(master_path).project)


def _require_line(registry: dict[str, str], line: str, role: str) -> str:
    if line not in registry:
        raise OrchdError(
            ErrorCode.E005,
            f"line-sync {role} 线 '{line}' 未在 project.lines 登记",
            [{"line": line, "role": role, "known_lines": sorted(registry)}],
        )
    return registry[line]


def _changed_files(project_root: Path, sha: str) -> list[str]:
    """源提交改动的文件清单（``git diff-tree --root`` 只读；根提交也能列出文件）。"""
    proc = _git(
        project_root, "diff-tree", "--no-commit-id", "--name-only", "--root", "-r", sha
    )
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _default_module(project_root: Path) -> str:
    """目标项目的默认 module（``modules[0].id``）；无登记 → E007（提示显式 --module）。"""
    from orchd.spec import load_master
    from orchd.worktree import resolve_master_path_from_dir

    path = resolve_master_path_from_dir(Path(project_root) / ".orchd")
    if path.is_file():
        modules = load_master(path).modules
        if modules and modules[0].get("id"):
            return str(modules[0]["id"])
    raise OrchdError(
        ErrorCode.E007,
        "line-sync 无法确定 module：目标项目无 modules[] 登记，请显式传 --module",
        [{"project_root": str(project_root)}],
    )


def plan_backport(
    project_root: Path,
    *,
    source_line: str,
    target_line: str,
    source_sha: str,
    task_id: str | None = None,
    title: str | None = None,
    module: str | None = None,
    source: str | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """定位源提交并生成目标线回移任务提案。

    Args:
        project_root: 项目根（主工作树）。
        source_line: 源线名（须已登记）。
        target_line: 目标线名（须已登记）。
        source_sha: 待回移的源提交 sha。
        task_id: 回移任务 id（缺省由 sha 派生）。
        title: 回移任务标题（缺省按 sha 生成）。
        write: 是否落盘提案（测试可置 False 只计算）。

    Returns:
        回移记录 + 提案路径；``backport`` 字段与 ``line-drill.json`` 同构。

    Raises:
        OrchdError E005: 源 / 目标线未登记。
        OrchdError E007: 源提交不可达源线 trunk（幽灵提交）。
    """
    project_root = Path(project_root)
    registry = _load_registry(project_root)
    source_trunk = _require_line(registry, source_line, "源")
    target_trunk = _require_line(registry, target_line, "目标")

    # ① 定位源提交（C-13）：先经 rev-parse 归一为完整 sha，再校验可达源线 trunk
    normalized = _git(project_root, "rev-parse", "--verify", f"{source_sha}^{{commit}}")
    if normalized.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            f"line-sync 源提交 '{source_sha}' 无法解析为 commit",
            [{"source_sha": source_sha, "hint": "传入完整 sha 或可达的 committish"}],
        )
    source_sha = normalized.stdout.strip()
    reach = _git(project_root, "merge-base", "--is-ancestor", source_sha, source_trunk)
    if reach.returncode != 0:
        raise OrchdError(
            ErrorCode.E007,
            f"line-sync 源提交 '{source_sha}' 不可达源线 '{source_line}' 的 trunk "
            f"'{source_trunk}'（拒绝搬运幽灵提交）",
            [{
                "source_sha": source_sha,
                "source_line": source_line,
                "source_trunk": source_trunk,
                "hint": "确认 sha 属于源线 trunk 的历史（git merge-base --is-ancestor）",
            }],
        )

    # ② 源提交改动（C-14）：为空（merge commit / 空改动）即硬拒绝，不写哨兵占位串进声明域
    files = _changed_files(project_root, source_sha)
    if not files:
        raise OrchdError(
            ErrorCode.E007,
            f"line-sync 无法从源提交 {source_sha[:7]} 派生 files_to_edit（merge commit 或空改动）",
            [{
                "source_sha": source_sha,
                "hint": "改用非 merge 的具体提交，或手工构造回移任务（line-sync 只做提案与留痕）",
            }],
        )

    tid = task_id or f"task-backport-{source_sha[:8]}"
    short = source_sha[:7]
    proposal: dict[str, Any] = {
        "id": tid,
        "name": title or f"回移 {short}",
        "brief": (
            f"把源线 '{source_line}' 的提交 {short} 回移到目标线 '{target_line}'，"
            f"走目标线自己的审查与回归（line-sync 回移通道）。"
        ),
        "module": module or _default_module(project_root),
        "depends_on": [],
        "estimated_hours": 2,
        "importance": "high",
        "difficulty": "medium",
        "requires": ["python", "git"],
        "source": source if source is not None else "debug:line-sync-backport",
        "files_to_edit": files,
        "acceptance_criteria": [
            f"回移源提交 {short}（{source_line}）到目标线 {target_line}，改动内容与源一致",
            "走目标线自己的 verify_command 与审查（spec + code）后合并入目标线 trunk",
            f"回移留痕：backport={{source_sha: {source_sha}, source_line: {source_line}}}",
        ],
        "backport": {"source_sha": source_sha, "source_line": source_line},
    }

    proposal_path: str | None = None
    if write:
        from orchd.worktree import resolve_master_path_from_dir

        orchd_dir = resolve_master_path_from_dir(project_root / ".orchd").parent
        target_dir = orchd_dir.joinpath(*_PROPOSAL_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{tid}.json"
        path.write_text(
            json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        proposal_path = str(path)

    backport = {
        "task_id": tid,
        "source_sha": source_sha,
        "source_line": source_line,
        "target_line": target_line,
        "event": "line_sync_proposed",
    }
    return {
        "ok": True,
        "task_id": tid,
        "source_sha": source_sha,
        "source_line": source_line,
        "target_line": target_line,
        "source_trunk": source_trunk,
        "target_trunk": target_trunk,
        "files_to_edit": files,
        "proposal_path": proposal_path,
        "backport": backport,
        "hint": (
            f"回移任务提案已生成（{proposal_path}）：确认后执行 "
            f"python .orchd/__main__.py amend --register {proposal_path} 注册，"
            f"再以 ORCHD_LINE={target_line} 走 claim → done → review → merge"
            f"（目标线自己的审查与回归）；未设 ORCHD_LINE 会落默认线（C-12）。"
        ),
    }
