"""merge 冲突解决标记解析（_resolve_review_resolve_markers），叶域。"""

from __future__ import annotations

from pathlib import Path
import subprocess

from orchd.ledger import _write_text_atomic


def _resolve_review_resolve_markers(workdir: Path, conflict_files: list[str]) -> bool:
    """[fallback] 审查者授权解冲突：只解 REVIEW-RESOLVE 标记段，保留双方内容。

    task-merge-tests-union-and-reviewer-fallback：仅在显式
    authorize_reviewer_resolve=True 时由调用方触发。逐文件扫描
    ``<<<<<<< REVIEW-RESOLVE`` 标记段，去掉标记符、保留双方内容（Both sides
    preserved），不改业务逻辑。只 git add 冲突文件并提交，不触碰其他文件。

    Returns:
        True 表示全部标记段已解并提交；False 表示失败（调用方回退 E015）。
    """
    import re as _re

    marker_start = _re.compile(r"^<<<<<<< REVIEW-RESOLVE\s*$", _re.MULTILINE)
    marker_sep = _re.compile(r"^=======\s*$", _re.MULTILINE)
    marker_end = _re.compile(r"^>>>>>>>.*$", _re.MULTILINE)

    for rel in conflict_files:
        target = workdir / rel
        if not target.exists():
            return False
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        # 逐段替换 REVIEW-RESOLVE 标记
        pos = 0
        result_parts: list[str] = []
        while True:
            m_start = marker_start.search(text, pos)
            if not m_start:
                result_parts.append(text[pos:])
                break
            result_parts.append(text[pos:m_start.start()])
            m_sep = marker_sep.search(text, m_start.end())
            if not m_sep:
                return False  # 标记不完整，拒绝
            m_end = marker_end.search(text, m_sep.end())
            if not m_end:
                return False
            # 保留双方内容（去掉标记符），中间加空行分隔
            left = text[m_start.end():m_sep.start()].strip("\n")
            right = text[m_sep.end():m_end.start()].strip("\n")
            result_parts.append(f"{left}\n\n{right}\n")
            pos = m_end.end()
        merged = "".join(result_parts)
        # 确认无残留冲突标记
        if "<<<<<<<" in merged or ">>>>>>>" in merged:
            return False
        _write_text_atomic(target, merged)
        add = subprocess.run(
            ["git", "-C", str(workdir), "add", rel],
            capture_output=True,
        )
        if add.returncode != 0:
            return False
    # 只提交冲突文件
    commit = subprocess.run(
        ["git", "-C", str(workdir), "commit", "-q",
         "-m", "chore(merge): reviewer-authorized resolve — Both sides preserved"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return commit.returncode == 0
