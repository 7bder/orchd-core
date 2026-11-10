"""git 命令执行底层（整块迁移，无逻辑改动）。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from orchd.gitops._const import _GIT_ENCODING, _GIT_ERRORS, _GIT_TIMEOUT


_REPO_PROBE_ARGS = frozenset({
    ("rev-parse", "--is-inside-work-tree"),
    ("rev-parse", "--git-dir"),
    ("rev-parse", "--git-common-dir"),
    ("rev-parse", "--show-toplevel"),
})


def _run_git(
    project_root: Path,
    args: list[str],
    timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """以 UTF-8 解码运行 git 命令（cwd 限定 project_root）。"""
    from orchd.gitops.query.cache import _PROBE_CACHE, _probe_cache_key

    def _spawn() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=timeout,
        )

    key_args = tuple(args)
    if key_args not in _REPO_PROBE_ARGS:
        return _spawn()
    base = _probe_cache_key(project_root)
    env = (os.environ.get("GIT_DIR"), os.environ.get("GIT_WORK_TREE"))
    if base is not None and base[1] == "nage":
        # 否定结果缓存（2026-10-31 用户裁决改设计，原「否定结果永不缓存」口径
        # 见 design/guard-drift-prevention-20261029.md §1.9）：无 .git 的
        # "nage" 形态根，rc=128（not a git repository）按 (探针, base, env)
        # 缓存——base 自带自体 mtime 新鲜度（git init 必改自体 mtime → 键变化
        # 自动失效，test_init_after_negative_probe_is_visible 锁定新鲜度）。
        # 祖先成功结果与 dir/file/odd 形态失败结果仍永不缓存（HEAD 键不含
        # refs 面变化，负缓存有 stale 面，保守不放开）。
        nkey = ("repo_probe_neg", key_args, base, env)
        hit = _PROBE_CACHE.get(nkey)
        if hit is not None:
            return hit  # 共享缓存对象：调用方不得原地修改其属性
        res = _spawn()
        if res.returncode == 128:
            _PROBE_CACHE[nkey] = res
        return res
    if base is None or base[1] not in ("dir", "file"):
        return _spawn()
    ckey = ("repo_probe", key_args, base, env)
    hit = _PROBE_CACHE.get(ckey)
    if hit is not None:
        return hit  # 共享缓存对象：调用方不得原地修改其属性
    res = _spawn()
    if res.returncode == 0:
        _PROBE_CACHE[ckey] = res
    return res


def _shell_quote(value: str) -> str:
    """shell 单引号转义：' 替换为 '\''（单引号闭合-转义-重开）。

    用于把文件名安全嵌入 shell 脚本字面量，防 shell 注入
    （hook 模板中文件名来自任务定义，属可信输入，但仍按防御性处理）。
    """
    return "'" + value.replace("'", "'\\''") + "'"
