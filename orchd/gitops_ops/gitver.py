"""gitops_ops gitver 域 — git 版本探测与 git 输出文本切分。

来源：``orchd/gitops_ops.py（1220 行）``（W6-C6 纯搬移）。

**叶子域**：零 orchd 内部依赖。``_split_nul`` 是取路径的唯一无损通道
（``-z`` 不转义非 ASCII），冲突检测的集合交集必须走它。
"""

from __future__ import annotations

import re
import subprocess


# ``merge-tree --write-tree`` 需 git >= 2.38；低版本降级为 merge 预演。
_MERGE_TREE_MIN_VERSION: tuple[int, int] = (2, 38)


_merge_tree_supported: bool | None = None


def _git_version_tuple() -> tuple[int, ...]:
    """探测 git 版本（best-effort；不可用 / 解析失败返回空元组）。"""
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            return ()
        match = re.search(r"(\d+)\.(\d+)", proc.stdout or "")
        return tuple(int(x) for x in match.groups()) if match else ()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ()


def merge_tree_supported() -> bool:
    """是否支持 ``git merge-tree --write-tree``（git >= 2.38，进程级缓存）。

    低版本（< 2.38）走 :func:`reconcile_with_main` 的 merge 预演降级路径。
    """
    global _merge_tree_supported
    if _merge_tree_supported is None:
        _merge_tree_supported = _git_version_tuple() >= _MERGE_TREE_MIN_VERSION
    return _merge_tree_supported


def _split_nonempty_lines(text: str | None) -> list[str]:
    """按行切分并丢弃空行（git ``--name-only`` 输出的通用解析）。"""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _split_nul(text: str | None) -> list[str]:
    """按 NUL 切分 git ``-z`` 输出（丢弃空项、不做 strip——文件名可含空格）。

    ``-z`` 是取路径的**唯一无损通道**（task-pass13-p3-pack-recheck ①）：不加时
    ``core.quotePath``（默认 true）会把非 ASCII / 含特殊字符的路径转义为
    ``"\\346\\226\\207..."``，位于同一 diff 的集合交集会**漏判**（冲突检测误判
    no_overlap 而静默放行）；git ``-z`` 以 NUL 分隔、路径原样不转义。
    """
    return [chunk for chunk in (text or "").split("\0") if chunk]
