"""orchd/guide/rules.py — 截断/预算原语与规则摘要域（W5-G3 拆分，纯搬移）。

来源：``orchd/guide/__init__.py`` L146-267（截断原语、块预算自检与规则摘要）+ ``flow.py`` 的
预算/前缀常量块（随消费者迁入，见 §4.6.3）。依赖方向：无 orchd 内部依赖（仅标准库）；
``flow`` 从本域取截断原语与预算常量，故本域不得反向导入。
"""

from __future__ import annotations

import os
from typing import Any


# 红线并入 hint 的前缀（task-guide-tiering：只留 ≤1 条最强红线）
_REDLINE_PREFIX: str = "红线："
# 降级留痕并入 hint 的前缀（AC6：路径缺失 / 兜底分支不再静默）
_DEGRADED_PREFIX: str = "引导降级："
# hint 预算（task-guidance-completeness-gate AC4）：合并结果总量上限。
_HINT_MAX: int = 300

# task-guidance-block-budget-root-fix：stderr 提示块预算单一真源。
_BLOCK_MAX: int = 1600
_MAX_COMMAND_LEN: int = 200
_MAX_READ_LEN: int = 500
_MAX_CASES_LEN: int = 400


def _truncate(text: str, limit: int) -> str:
    """超限时保留前缀 + 省略号（省略号单字符，结果不超 limit）。

    task-guidance-completeness-gate：hint 预算的统一截断原语（红线段与总量共用），
    避免两处各写一份截断逻辑导致口径漂移。
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _block_layout_overhead() -> int:
    """渲染布局固定开销（sep*2 + 前缀 + 换行 + 空行）。"""
    sep = "─" * 40
    return (
        1 + len(sep) + 1 +
        len("orchd ▸ ") + 1 +
        len("建议执行：") + 1 +
        len(sep) + 1 + 1
    )


def _assert_budget_consistency() -> None:
    """常量自洽断言：布局开销 + 各内容预算 <= _BLOCK_MAX。"""
    total = _block_layout_overhead() + _HINT_MAX + _MAX_COMMAND_LEN + _MAX_READ_LEN + _MAX_CASES_LEN
    assert total <= _BLOCK_MAX, (
        f"guidance block budget overflow: {total} > {_BLOCK_MAX}"
    )


_assert_budget_consistency()


def _truncate_redline_semantic(text, budget, read_paths, rule_count=1):
    """红线语义截断：按点分割只装整点，装不下退化为红线 N 条（见路径）。"""
    if len(text) <= budget:
        return text
    import re
    points = re.split(r'(?<=[；。;])', text)
    fitted = []
    current = ""
    for p in points:
        if not p:
            continue
        if len(current) + len(p) <= budget - 1:
            current += p
            fitted.append(p)
        else:
            break
    if fitted:
        return current + "…"
    read_hint = read_paths[0] if read_paths else "rules/review.md"
    return f"{_REDLINE_PREFIX}{rule_count} 条（见 {read_hint}）"


def _extract_tldr(path: str) -> str | None:
    """读取 rule 文件，提取 TL;DR 段文本（I/O 透传，不做内容域解析）。

    匹配以 ``> TL;DR:`` 开头的行，返回 ``:`` 后的内容（strip）；无该行或
    内容为空返回 None。文件读取异常（OSError）返回 None（best-effort 降级）。
    """
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("> TL;DR:"):
                    text = line[len("> TL;DR:"):].strip()
                    return text or None
    except OSError:
        return None
    return None


def _summarize_rules(
    read_paths: list[str],
    base_dir: str | os.PathLike[str] | None,
) -> list[str]:
    """把 read 路径数组映射为 ``<文件名>: <TL;DR>`` 摘要数组（纯函数）。

    - 相对路径按 base_dir 拼接定位；绝对路径直接读。
    - 无 TL;DR 段的文件跳过（不产生元素）。
    - base_dir 为 None 或 read 为空 → 返回空数组（无害降级）。
    """
    if not read_paths or base_dir is None:
        return []
    root = os.path.abspath(os.fspath(base_dir))
    rules: list[str] = []
    for p in read_paths:
        path = p if os.path.isabs(p) else os.path.join(root, p)
        tldr = _extract_tldr(path)
        if tldr:
            rules.append(f"{os.path.splitext(os.path.basename(p))[0]}: {tldr}")
    return rules


def attach_rule_summaries(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
    max_rules: int = 1,
) -> dict[str, Any]:
    """对 guidance 的 read 数组生成 rules 键（加法式，纯函数）。

    知识路由闭环的摘要层（task-guidance-rule-summary）：调用方（cli.py
    ``_attach_guidance`` 注入点）在 ``resolve_read_paths`` 之后透传 base_dir，
    本函数返回**新增 ``rules`` 键**的 guidance 副本，内容为 read 数组对应
    规则文件的 TL;DR 摘要（``<文件名>: <TL;DR>``），无 TL;DR 段跳过。

    W-1 红线上限（task-guide-tiering）：仅保留前缀 ``max_rules`` 条摘要
    （默认 1 条），其余规则交由 ``read[]`` 指向文件按需读——避免"红线列表"
    信息超载同时保留关键约束提示。

    - 顶层新增 rules 键；agent_view / project_view 递归处理（与
      resolve_read_paths 同构），保证双视角与顶层 rules 一致。
    - 不碰 step/read/template/command/hint 既有字段（加法式）。
    - read 为空 / base_dir None 时返回 rules=[]（无害降级）。
    """
    out = dict(guidance)
    out["rules"] = _summarize_rules(guidance.get("read") or [], base_dir)[:max_rules]
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = attach_rule_summaries(sub, base_dir, max_rules=max_rules)
    return out
