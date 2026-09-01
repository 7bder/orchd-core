"""Orchd 任务生命周期管理 - bootstrap 域。

迁移自 orchd/onboard.py（task-split-onboard-bootstrap-request）：
  - _resolve_resource_root: 解析引擎资源根
  - bootstrap: 输出分解套件
  - _find_project_root: 从 cwd 向上搜索项目根
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError


# 默认分解指南：当项目 docs/decomposition-guide.md 不存在时，
# bootstrap() 使用此内置文本作为 guide 字段，确保 LLM 始终有基本的分解原则可参考。
_FALLBACK_GUIDE = (
    "分解指南文件缺失。基本原则：任务粒度 0.5-6h，每个任务 1-4 个文件，"
    "2-5 条可量化验收标准，依赖链 ≤ 4 层，测试文件列入 files_to_edit。"
)


def _resolve_resource_root(project_root: Path) -> Path:
    """解析引擎资源根（schema / templates / docs 所在目录，task-12-engine-path-abstraction）。

    双态兼容：
    - 根布局（开发态）：资源在项目根（``project_root/schema``）→ 返回 ``project_root``。
    - 发布态布局（自包含 ``.orchd/``）：资源归置 ``.orchd/``（``.orchd/schema``）→
      返回 ``project_root / ".orchd"``。
    判定以 ``schema/`` 是否存在为准；两者都无 → 回退 ``project_root``
    （保持既有 E001「缺失报错」语义，缺失时由调用方抛错）。
    """
    project_root = Path(project_root)
    if (project_root / "schema").is_dir():
        return project_root
    orchd_dir = project_root / ".orchd"
    if (orchd_dir / "schema").is_dir():
        return orchd_dir
    return project_root


def bootstrap(project_root: Path | None = None) -> dict[str, Any]:
    """输出分解套件 JSON：schema + prompt + guide + next_step。

    不调用 LLM、不访问网络、不写文件。
    注意：此函数仅读取项目静态文件（schema / templates / docs），
    不要求 .orchd/ 目录已存在——它可在项目初始化（python .orchd/__main__.py init）之前安全调用。

    资源根双态兼容（task-12-engine-path-abstraction，AC1）：
    开发态在项目根（``schema/`` 等），发布态在 ``.orchd/``（引擎资源归置）。

    Raises:
        OrchdError E001: schema 或 architect.md 缺失。
    """
    if project_root is None:
        project_root = _find_project_root()
    resource_root = _resolve_resource_root(project_root)

    schema_path = resource_root / "schema" / "_master.schema.json"
    prompt_path = resource_root / "templates" / "architect.md"
    guide_path = resource_root / "docs" / "decomposition-guide.md"

    if not schema_path.exists():
        raise OrchdError(
            ErrorCode.E001,
            f"file not found: {schema_path}",
            [{"path": str(schema_path), "message": "schema 文件缺失"}],
        )
    if not prompt_path.exists():
        raise OrchdError(
            ErrorCode.E001,
            f"file not found: {prompt_path}",
            [{"path": str(prompt_path), "message": "architect prompt 模板缺失"}],
        )

    schema_text = schema_path.read_text(encoding="utf-8")
    prompt_text = prompt_path.read_text(encoding="utf-8")

    if guide_path.exists():
        guide_text = guide_path.read_text(encoding="utf-8")
    else:
        guide_text = _FALLBACK_GUIDE

    return {
        "schema": schema_text,
        "prompt": prompt_text,
        "guide": guide_text,
        "next_step": "请根据以上 schema、prompt 和 guide 对项目进行任务分解，输出符合 _master.schema.json 的 JSON。",
    }


def _find_project_root() -> Path:
    """从 cwd 向上逐层搜索项目根目录。

    搜索策略：依次检查 cwd → cwd.parent → cwd.parent.parent → …，
    返回第一个含有 ``.orchd/`` 目录的祖先路径（task-12-engine-path-abstraction，
    AC2：按 .orchd/ 定位项目根，不再依赖根含 schema/——发布态自包含 .orchd/
    工作空间同样命中）；若所有祖先均不匹配则回退到 cwd 本身。
    这使得在子目录中运行 CLI 也能正确定位到项目根。
    """
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".orchd").is_dir():
            return parent
    return cwd