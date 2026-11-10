"""Orchd 任务生命周期管理 - _config 域。

迁移自 orchd/onboard.py（task-split-onboard-control-config）：
  - _load_config_blocked: 从 master config 读取文档单阶段黑名单
  - _is_doc_single_stage: Q2 review 分级判定
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.ledger import Store
from orchd.worktree import resolve_master_path as _master_path


def _load_config_blocked(store: Store) -> set[str] | None:
    """从 master config 读取 doc_single_stage_blocked（C5 配置化）。

    读 ``.orchd/_master.json`` 顶层 config.doc_single_stage_blocked（string[]）；
    config 缺失、空对象或无该键时返回 None（调用方回退硬编码默认集合）。
    best-effort：master 缺失/解析失败返回 None（不抛异常）。
    """
    try:
        master_path = _master_path(store)
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

# done 响应的审查路径维度（task-engine-defect-fix-unified-u13-review-created-mode）：
# 历史 ``review_created`` 只有 type，而 type=code 同时承载「双阶段项目下的纯文档单阶段」
# 与「code 范围返工直达」两种互不相干的原因，消费方无法区分来源。故拆出正交的
# ``mode`` + ``reason``；``type`` 保留原值，历史消费方零改动。
#   - mode=unified                    项目 project.review_mode=unified（单阶段审查）
#   - mode=two_phase_single_stage_doc 双阶段项目下的纯文档单阶段（跳过 spec）
#   - mode=two_phase                  双阶段项目（默认；含 code 返工直达）
REVIEW_CREATED_MODE_UNIFIED = "unified"
REVIEW_CREATED_MODE_TWO_PHASE = "two_phase"
REVIEW_CREATED_MODE_TWO_PHASE_SINGLE_STAGE_DOC = "two_phase_single_stage_doc"
REVIEW_CREATED_MODES = (
    REVIEW_CREATED_MODE_UNIFIED,
    REVIEW_CREATED_MODE_TWO_PHASE,
    REVIEW_CREATED_MODE_TWO_PHASE_SINGLE_STAGE_DOC,
)


def resolve_review_created(
    store: Store,
    task_def: dict[str, Any],
    task_id: str,
) -> dict[str, str]:
    """done 响应 ``review_created`` 的单一推导真源：``{type, mode, reason}``。

    判定顺序（与历史 done 行为逐字一致，仅补 mode/reason 维度）：
    1. ``project.review_mode=unified`` → type=unified / mode=unified；
    2. 双阶段项目下 ``files_to_edit`` 全为文档白名单且不碰约定/状态文件 →
       type=code / mode=two_phase_single_stage_doc（跳过 spec 直达 code 终审）；
    3. 上轮 code 范围打回（``rework_scope=code``）→ type=code / mode=two_phase
       （返工直达 code，跳过 spec）；
    4. 其余 → type=spec / mode=two_phase（默认双阶段）。

    ``mode`` 回答「哪条审查路径」，``reason`` 回答「为什么走这条路径」，
    两者与 ``type``（下游兼容字段）正交；REVIEW_READY 事件字段不变。

    Args:
        store: 账本 Store（读 project.review_mode / config / rework_scope）。
        task_def: 任务定义（读 files_to_edit）。
        task_id: 任务 id（读最新 rework_scope）。

    Returns:
        ``{"type": str, "mode": str, "reason": str}``。
    """
    from orchd.contracts.task_schema import FILES_TO_EDIT_FIELD
    from orchd.ledger import latest_rework_scope, resolve_review_mode

    if resolve_review_mode(store.orchd_dir) == "unified":
        return {
            "type": "unified",
            "mode": REVIEW_CREATED_MODE_UNIFIED,
            "reason": "项目 project.review_mode=unified：单阶段审查，一次 APPROVED 即 merge",
        }
    files_to_edit = task_def.get(FILES_TO_EDIT_FIELD) or []
    if _is_doc_single_stage(files_to_edit, blocked=_load_config_blocked(store)):
        return {
            "type": "code",
            "mode": REVIEW_CREATED_MODE_TWO_PHASE_SINGLE_STAGE_DOC,
            "reason": (
                "files_to_edit 全为文档白名单且未触碰约定/状态文件："
                "双阶段项目下跳过 spec 直达 code 终审"
            ),
        }
    if latest_rework_scope(store, task_id) == "code":
        return {
            "type": "code",
            "mode": REVIEW_CREATED_MODE_TWO_PHASE,
            "reason": "上轮 code 范围打回（rework_scope=code）：返工直达 code 审查，跳过 spec",
        }
    return {
        "type": "spec",
        "mode": REVIEW_CREATED_MODE_TWO_PHASE,
        "reason": "默认双阶段：先 spec 后 code",
    }
