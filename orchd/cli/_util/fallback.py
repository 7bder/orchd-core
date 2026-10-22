"""cli 通用工具域（W6-C8）：cwd/命令名/master 加载（paths）、IDEAS 归档主流程（archive）、
源码回收后的子进程兜底（fallback）。

域：fallback（W6-C8 从 orchd/cli/_util.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any



def _orchd_source_recycled() -> bool:
    """本进程 orchd 包源码目录是否已被回收（container 终态回收删除任务 worktree）。

    container 布局下进程以 ``python .orchd/__main__.py``（cwd = 任务 worktree）
    启动，``orchd.__file__`` 指向任务 worktree 内的引擎副本；review code APPROVED
    的终态回收会删除该目录，此后**未绑定**模块的懒加载都会
    ``ModuleNotFoundError``（模块已加载时自身仍可运行，仅源码目录消失）。

    Returns:
        True = 源码目录已消失（懒加载不可用，需子进程兜底）；无法判定时 False。
    """
    module = sys.modules.get("orchd")
    src_file = getattr(module, "__file__", None) if module is not None else None
    if not src_file:
        return False
    try:
        return not Path(src_file).parent.is_dir()
    except OSError:
        return False



def _archive_ideas_via_subprocess(orchd_dir: Path) -> dict:
    """源码已回收时的归档兜底：在 canonical 主工作树起子进程执行 ideas-archive。

    子进程以 ``<canonical 主工作树>/.orchd/__main__.py`` 重新加载引擎源码，
    归档结果经 stdout JSON 返回（只取 archived / kept / skipped / error / commit /
    deduped / deferred_comment_unbalanced / comment_hidden_candidate /
    comment_unclosed_at 契约字段，形状与进程内归档一致——进程内归档自
    ideas-dropped-exit 起会在真发生跳写时附 deduped、有条目被注释配对不变量挡下时附
    deferred_comment_unbalanced，task-idea-archive-diagnostics 起有条目被注释区间
    冻结时附 comment_hidden_candidate / comment_unclosed_at；这里不跟着转发就等于
    "兜底面的形状与主面不一致"，而那正是本函数 docstring 承诺的事）。失败（入口缺失 / 非零退出 /
    输出非 JSON / 超时）一律返回 ``_archive_error_result`` 结构化降级——不阻断
    调用方、不静默。

    Args:
        orchd_dir: canonical 主工作树的 ``.orchd`` 目录。

    Returns:
        子进程归档结果或结构化降级结果。
    """
    import json
    import subprocess

    entry = orchd_dir / "__main__.py"
    if not entry.is_file():
        return _archive_error_result(
            "subprocess", FileNotFoundError(f"canonical 入口不存在: {entry}")
        )
    try:
        proc = subprocess.run(
            [sys.executable, str(entry), "ideas-archive"],
            cwd=str(orchd_dir.parent), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
        )
    except Exception as exc:
        return _archive_error_result("subprocess", exc)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        detail = tail[-1][:200] if tail else ""
        return _archive_error_result(
            "subprocess", RuntimeError(f"exit={proc.returncode}: {detail}")
        )
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        return _archive_error_result("subprocess_parse", exc)
    if not isinstance(payload, dict):
        return _archive_error_result(
            "subprocess_parse",
            ValueError(f"unexpected payload type: {type(payload).__name__}"),
        )
    return _forward_archive_payload(payload)



def _forward_archive_payload(payload: dict) -> dict:
    """把子进程归档 JSON 过滤成契约字段（进程内 / 子进程两条面同键）。

    task-idea-archive-diagnostics：抽成纯函数是为了让"两条转发面同键"这件事**可测**
    ——此前过滤是内联在子进程兜底里的，只有真起子进程才走得到，于是新键是否同步
    只能靠人读代码（本仓 deduped 键当年就漏过一次）。
    """
    result: dict[str, Any] = {
        "archived": payload.get("archived", []),
        "kept": payload.get("kept", 0),
    }
    for key in (
        "skipped",
        "error",
        "commit",
        "deduped",
        "deferred_comment_unbalanced",
        "comment_hidden_candidate",
        "comment_unclosed_at",
    ):
        if key in payload:
            result[key] = payload[key]
    return result



def _preimport_archive_deps() -> None:
    """在可能触发 worktree 终态回收的动作之前预导入终态归档依赖（AC1）。

    ``orchd.ideas`` 是终态归档的全仓唯一懒加载点（``_maybe_archive_ideas``）。
    review code APPROVED 会终态回收任务 worktree、连带删除本进程的 orchd 源码
    目录；源码消失后再懒加载即 ``ModuleNotFoundError``（归档静默失效）。在回收
    动作之前调用本函数，令模块绑定进 ``sys.modules``，回收后归档调用不再触盘。

    依赖方向：``cli._util → orchd.ideas`` 已是 conventions.md 登记的既有边
    （ideas 包非零内部依赖：ideas → errors / intake，模块级导入
    _atomic_write_text / _resolve_lock_orchd_dir，W6-C7 起落在 intake.parse / intake.guard），
    故预导入放在本模块而非调用方，
    避免新增未登记的依赖边。

    best-effort：预导入失败不抛异常——``_maybe_archive_ideas`` 的子进程兜底
    （``_archive_ideas_via_subprocess``）会接管。
    """
    try:
        import orchd.ideas  # noqa: F401
    except Exception:
        pass



def _archive_error_result(stage: str, exc: BaseException) -> dict:
    """归档失败的结构化降级结果（永不阻断调用方，AC4 可审计）。"""
    return {
        "archived": [],
        "kept": 0,
        "skipped": "archive_error",
        "error": {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc)[:300],
        },
    }
