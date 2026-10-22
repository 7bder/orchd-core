"""claim 域 —— 范围预警（scope warning）。

自 ``orchd/onboard/claim.py`` 拆出（task-split2-w8-1-claim-pkg，纯搬移）：

- :func:`build_scope_warning`：brief/acceptance 提及未声明文件的 best-effort 预警。
  **单一真源**——实认领路径（``run.claim``）与 CLI 预览路径（claim_preview）共用，
  经聚合入口 ``orchd.onboard.claim`` 重导出保持可达。
- :func:`_is_high_risk`：高风险领域判定（共享上下文按需附 conventions.md）。
- :func:`_extract_previous_changes`：从 ledger 提取最近一次 DONE 的 changes。

叶子域：不导入同包其它域。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orchd.ledger import Store, TaskDerived
from orchd.review import extract_last_done as _extract_last_done


def _is_high_risk(task_def: dict[str, Any]) -> bool:
    """高风险领域判定：任务触碰引擎核心（状态机分支 / CLI 契约 / 锁协议）。

    用于「共享上下文按需」：这类任务的实现默认自动附 conventions.md
    （编码规范 + 自检约定），降低越界/违规概率；architecture.md 不自动附，
    仅任务自身 files_to_read 显式引用或 --with-context 显式开启时提供。

    双态兼容（task-12-engine-path-abstraction，AC4）：files_to_edit 命中
    ``orchd/``（开发态根布局）或 ``.orchd/orchd/``（发布态自包含 .orchd 布局）
    均判高风险；``.orchd/_master.json`` 固定资产同样高风险。
    """
    module = task_def.get("module", "")
    if module == "mod-core":
        return True
    for f in task_def.get("files_to_edit", []):
        if (
            f.startswith("orchd/")
            or f.startswith(".orchd/orchd/")
            or f == ".orchd/_master.json"
        ):
            return True
    return False


def _extract_previous_changes(
    store: Store, task_id: str, derived: TaskDerived | None = None
) -> str | None:
    """从 ledger 中提取该任务最近一次 DONE 的 changes_description。"""
    return _extract_last_done(store, task_id, derived)[1]


# brief/acceptance_criteria 中常见文件路径模式（task-amend-scope-add 连带文件预警）。
# task-claim-ghost-dot-strip（批次 4a）：前导点必须随匹配保留——旧形态对
# ".orchd/IDEAS.md" 从 "orchd" 起匹配，捕获串丢前导点，(project_root/"orchd/IDEAS.md")
# 不存在 → 把真实存在的 .orchd/... 报成幽灵。`(?<![\w.])\.?` 保证只在前导点前的字符
# 非词字符/非点时（字符串首 / 空白 / 引号 / 括号）才起匹配，且把该点纳入捕获串。
_SCOPE_FILE_RE = re.compile(
    r"(?<![\w.])\.?(?:orchd|tests|scripts|docs|rules|templates|design)"
    r"/[\w./-]+\.(?:py|md|json|yaml|yml|txt)"
)


def build_scope_warning(
    task_def: dict[str, Any], project_root: Path | None = None
) -> dict[str, Any] | None:
    """扫描 brief/acceptance_criteria 提及但未在 files_to_edit 声明的文件（task-amend-scope-add）。

    单一来源（task-fix-claim-scope-warning）：实认领路径 ``claim`` 与 CLI 预览路径
    ``claim_preview`` 共用本函数，消除原先两份重复实现。best-effort——任何异常一律
    返回 ``None``，不阻断认领/预览。

    真伪分诊（task-claim-preview-and-scope-triage）：
    - 纯词法扫描会把 brief/AC 中**引用**的文件（如 _master.json、rules/*.md）误报为
      "需改但未声明"，其中还可能包含**不存在的幽灵文件**。
    - 修复：① 加磁盘存在性校验，不存在的文件归入 ``ghost_files`` 仅提示不预警；
      ② 存在但未声明的文件标注为 ``referenced_files``（引用，非必然需改），
      hint 改为保守提示"确认是否真的需要修改"，不再直接生成 amend 命令诱导污染声明。

    Args:
        task_def: ``_master.json`` tasks[] 中的单个任务定义。
        project_root: 可选项目根路径，用于存在性校验的基准目录；为 None 时跳过
            存在性校验（向后兼容测试直调场景），所有提及未声明文件均归入 referenced。

    Returns:
        存在需关注文件时返回含 ``type`` / ``message`` / ``referenced_files`` /
        ``ghost_files`` / ``hint`` 的 scope_warning 字典；无遗漏或扫描失败时返回 ``None``。
    """
    try:
        declared_files = set(task_def.get("files_to_edit", []))
        text_to_scan = (task_def.get("brief", "") or "") + " " + " ".join(
            task_def.get("acceptance_criteria", []) or []
        )
        mentioned = set(_SCOPE_FILE_RE.findall(text_to_scan))
        # 目录式声明感知差集（task-decl-dir-match-guards）：mentioned 文件若被
        # declared_files 中任一路径覆盖（精确相等或目录式前缀匹配），则视为已声明。
        from orchd.pool import _is_path_covered
        undeclared = [
            m for m in mentioned
            if not any(_is_path_covered(d, m) for d in declared_files)
        ]
        if not undeclared:
            return None
        # 存在性分诊：磁盘存在 → referenced（引用，需确认是否真改）；
        # 不存在 → ghost（幽灵文件，仅提示）
        referenced: list[str] = []
        ghost: list[str] = []
        for f in sorted(undeclared):
            if project_root is not None and (project_root / f).exists():
                referenced.append(f)
            elif project_root is None:
                referenced.append(f)  # 无 project_root 时不做存在性校验，保守归入 referenced
            else:
                ghost.append(f)
        if not referenced and not ghost:
            return None
        return {
            "type": "files_to_edit_referenced",
            "message": (
                f"brief/acceptance 中提到 {len(referenced) + len(ghost)} "
                "个文件未在 files_to_edit 中声明"
                + (f"（其中 {len(ghost)} 个磁盘不存在，可能是失效引用）" if ghost else "")
                + "；这些是**引用**文件，不必然需要修改"
            ),
            "referenced_files": referenced,
            "ghost_files": ghost,
            "hint": (
                "上述文件是 brief/acceptance 中**引用**的路径（如规则文档、配置、测试入口），"
                "不代表实现时必须修改。若确认某文件确需改动，请在认领后回到主工作树执行 "
                "amend --files-to-edit 补声明；若仅是引用则无需处理。幽灵文件（磁盘不存在）"
                "通常是 brief 中的失效引用，建议修正 brief 而非补声明。"
            ),
        }
    except Exception:
        return None  # best-effort，预警失败不阻断认领/预览
