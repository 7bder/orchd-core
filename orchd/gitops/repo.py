"""RepositoryBackend 端口：变更检测单一真源的接口抽象（task-repo-backend-port）。

把散落在各消费点的变更检测调用收敛到一个端口：
``RepositoryBackend.changed_paths(task_id)``。git 与快照双后端实现同一
D/R 口径契约（kernel-contract INV-1）：

- 删除(D)：报缺失路径；
- 重命名(R，内容同一)：折叠只报新路径（对齐 git ``--name-only``）；
- 重命名 + 改写：快照后端按精确内容同一性判定，退化为「旧(删) + 新(增)」，
  为 git 结果的超集（只可能更严、绝不漏检）——已知且有意保留的边界。

调用迁移见 task-repo-migration-callsites；本模块只建不管迁。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class RepositoryBackend(ABC):
    """变更检测后端接口：任务相对基线的实际改动文件路径（posix相对路径，排序）。"""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root)

    @abstractmethod
    def changed_paths(self, task_id: str) -> list[str]:
        """返回任务相对基线的变更路径集合（口径见模块 docstring）。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """后端名（诊断用）：``"git"`` / ``"snapshot"``。"""
        raise NotImplementedError


class GitBackend(RepositoryBackend):
    """git 后端：复用既有 ``worktree._git_diff_names``（同一函数，零回归）。"""

    @property
    def backend_name(self) -> str:
        return "git"

    def changed_paths(self, task_id: str) -> list[str]:
        from orchd.worktree import _git_diff_names

        return _git_diff_names(self.project_root, task_id)


class SnapshotBackend(RepositoryBackend):
    """快照后端：复用 ``nogit`` manifest 差分（同一函数，零回归）。

    无基线快照 → 空列表（调用方自行判定降级语义，与
    :func:`orchd.nogit.snapshot_changed_paths` 的 None 语义对齐，此处收敛为空）。
    """

    def __init__(self, project_root: Path | str, label: str = "base") -> None:
        super().__init__(project_root)
        self.label = label

    @property
    def backend_name(self) -> str:
        return "snapshot"

    def changed_paths(self, task_id: str) -> list[str]:
        from orchd.nogit import manifest_changed_paths, read_manifest
        from orchd.nogit import hash_tree, snapshot_dir

        base = read_manifest(snapshot_dir(self.project_root, task_id, self.label))
        if base is None:
            return []
        return manifest_changed_paths(base, hash_tree(self.project_root))


def for_project(project_root: Path | str) -> RepositoryBackend:
    """按 git 可用性分发后端（task-nogit-changedet-core 单一真源语义）。

    - git 可用 → :class:`GitBackend`；
    - 否则 → :class:`SnapshotBackend`（基线由 claim 建立）。
    判定经 ``git_available``（``check_workspace_state`` 单一真源），惰性导入。
    """
    from orchd.nogit import git_available

    root = Path(project_root)
    if git_available(root):
        return GitBackend(root)
    return SnapshotBackend(root)


def backend_info(project_root: Path | str) -> dict[str, Any]:
    """诊断用：当前分发结果（后端名 + 项目根）。"""
    backend = for_project(project_root)
    extra: dict[str, Any] = {"project_root": str(Path(project_root).resolve())}
    if isinstance(backend, SnapshotBackend):
        extra["label"] = backend.label
    return {"backend": backend.backend_name, **extra}
