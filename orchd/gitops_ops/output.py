"""gitops_ops output 域 — 子进程输出解码与 verify 摘要。

来源：``orchd/gitops_ops.py（1220 行）``（W6-C6 纯搬移）。

**叶子域**：零 orchd 内部依赖（纯标准库），可被任意模块复用。
"""

from __future__ import annotations


def decode_subprocess_output(raw: bytes) -> str:
    """稳健解码子进程输出：UTF-8 优先，GBK 回退（Windows 默认代码页），最后有损 UTF-8。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _truncate_by_lines(text: str, limit: int) -> str:
    """按行对齐截断：从尾部取完整行，超限时在行首加省略标记，不从词中间切开。"""
    lines = text.splitlines()
    if not lines:
        return ""
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > limit and kept:
            break
        kept.insert(0, line)
        total += len(line) + 1
    result = "\n".join(kept)
    if len(kept) < len(lines):
        result = "…(前略)\n" + result
    return result


def _extract_json_objects(text: str) -> list[dict]:
    """从文本中提取所有可解析的顶层 JSON 对象（括号匹配，容忍嵌套）。"""
    import json
    objects: list[dict] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            in_string = False
            escape = False
            for j in range(i, n):
                c = text[j]
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == "\"":
                    in_string = not in_string
                elif not in_string:
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                obj = json.loads(text[i:j + 1])
                                if isinstance(obj, dict):
                                    objects.append(obj)
                            except Exception:
                                pass
                            i = j + 1
                            break
            else:
                i += 1
        else:
            i += 1
    return objects


def _extract_validate_summary(text: str) -> str | None:
    """从 verify 输出中提取 validate 的 valid/errors/warnings 计数（降噪）。"""
    for obj in _extract_json_objects(text):
        if "valid" in obj and "errors" in obj and "warnings" in obj:
            errors = obj.get("errors", [])
            warnings = obj.get("warnings", [])
            return (
                f"valid={obj.get('valid')}, "
                f"errors={len(errors) if isinstance(errors, list) else '?'}, "
                f"warnings={len(warnings) if isinstance(warnings, list) else '?'}"
            )
    return None


def verify_output_summary(stdout: bytes, stderr: bytes, limit: int = 400) -> str:
    """从 verify_command 子进程输出提取可读摘要。

    task-verify-summary-structured：
    - 逐命令结论行聚合（ruff / pytest / validate），不再只反映最后一条命令
    - 按行对齐截断，不从词中间切开，超限时有省略标记
    - validate 存量 warning 降级为计数摘要，原文不进入 output_summary
    - 无已知工具结论时回退到按行截断的原始输出（兼容简单命令）
    """
    import re
    out = decode_subprocess_output(stdout)
    err = decode_subprocess_output(stderr)
    parts: list[str] = []

    if out.strip():
        conclusions: list[str] = []
        # 1) 逐行扫描 ruff / pytest 结论行
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            # ruff: All checks passed! / Found N errors
            if "All checks passed" in s:
                conclusions.append(f"ruff: {s}")
            elif re.search(r"Found \d+ error", s):
                conclusions.append(f"ruff: {s}")
            # pytest: N passed / N passed, M skipped / N failed
            elif re.search(r"\d+ passed", s) or re.search(r"\d+ failed", s):
                conclusions.append(f"pytest: {s}")
        # 2) validate 降噪：提取计数摘要
        validate_summary = _extract_validate_summary(out)
        if validate_summary:
            conclusions.append(f"validate: {validate_summary}")
        # 3) 有结论行则聚合；否则回退按行截断
        if conclusions:
            parts.append(" | ".join(conclusions))
        else:
            parts.append(_truncate_by_lines(out.rstrip(), limit))

    # 4) stderr 同样按行截断
    if err.strip():
        parts.append("stderr: " + _truncate_by_lines(err.strip(), 200))

    return " | ".join(parts)
