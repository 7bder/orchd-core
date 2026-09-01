"""Orchd 任务生命周期管理 - _config 域。

迁移自 orchd/onboard.py（task-split-onboard-control-config）：
  - _load_config_blocked: 从 master config 读取文档单阶段黑名单
  - _is_doc_single_stage: Q2 review 分级判定
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.ledger import Store


def _load_config_blocked(store: Store) -> set[str] | None:
    """从 master config 读取 doc_single_stage_blocked（C5 配置化）。

    读 ``.orchd/_master.json`` 顶层 config.doc_single_stage_blocked（string[]）；
    config 缺失、空对象或无该键时返回 None（调用方回退硬编码默认集合）。
    best-effort：master 缺失/解析失败返回 None（不抛异常）。
    """
    try:
        master_path = store.orchd_dir / "_master.json"
        if not master_path.exists():
            return None
        import json as _json
        master = _json.loads(master_path.read_text(encoding="utf-8"))
        blocked = (master.get("config") or {}).get("doc_single_stage_blocked")
        if not isinstance(blocked, list) or not blocked:
            return None
        return {str(b) for b in blocked if isinstance(b, str)}
    except (OSError, ValueError):
        return None


# 文档白名单：仅这些明确的文档类型判为「文档单阶段」；schema / 构建配置 / CI /
# 任何代码文件（含非 Python：.js / .go / .toml / .yml 等）一律走双阶段审查
# （P2 §4.1 收紧——此前「非 .py / 非 orchd/ / 非 tests/ 即文档」过宽）。
_DOC_SINGLE_STAGE_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt")


def _is_doc_single_stage(
    files_to_edit: list[str],
    blocked: set[str] | None = None,
) -> bool:
    """Q2 review 分级判定：纯文档且不碰约定/状态文件 → 单阶段（跳过 spec）。

    满足以下条件返回 True（文档类单阶段 code 终审）：
    - files_to_edit 全部为「真文档」（白名单后缀 .md/.mdx/.markdown/.rst/.txt，
      含 docs/、doc/ 目录下的文档文件——目录不再无条件放行，见下）
    - 不包含约定/状态文件：SKILL.md、.orchd/SKILL.md、
      .orchd/shared/conventions.md、.orchd/_master.json
      （这些属于"约定改变"，须保持双阶段审查；.orchd/SKILL.md 为发布态布局）

    白名单语义（2026-08-13 全面审核 §4.1 收紧）：schema JSON、pyproject.toml、
    CI yml、任何代码文件（含非 Python）都是高风险变更，一律双阶段终审。

    目录 + 后缀双重判断（2026-08-13 remaining-issues 遗留项 1）：docs/、doc/
    目录下的**代码/数据文件**（如 docs/example.py、docs/schema.json、doc/x.js）
    同样必须命中后缀白名单，否则双阶段——「目录放行」不得绕过后缀白名单，
    防止示例代码 / JSON 契约 / 脚本被静默单阶段终审。

    C5（ROADMAP 1.1.1）：blocked 集合可配置——从 master config 的
    ``doc_single_stage_blocked`` 读取，缺省回退硬编码集合；新增约定文件
    加入 config 后，含该文件的任务不再被判定单阶段。

    Args:
        files_to_edit: 任务声明的 files_to_edit 列表。
        blocked: blocked 文件集合（可选，缺省用硬编码默认集合）。

    Returns:
        True 表示单阶段（跳过 spec）；False 表示常规双阶段。
    """
    if not files_to_edit:
        return False
    if blocked is None:
        blocked = {
            "SKILL.md",
            ".orchd/SKILL.md",
            ".orchd/shared/conventions.md",
            ".orchd/_master.json",
        }
    for f in files_to_edit:
        if f in blocked:
            return False
        lower = f.lower()
        # 只认后缀白名单：docs/ 目录下文件也须命中后缀（目录 + 后缀双重判断），
        # 避免 docs/example.py 等代码/数据文件被误判为文档单阶段。
        if not lower.endswith(_DOC_SINGLE_STAGE_SUFFIXES):
            return False
    return True
