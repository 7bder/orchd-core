"""orchd/spec/schema.py — schema 解析 / 加载 / 校验器 / 缺依赖降级域（W4-S1 拆分，纯搬移）。

来源：``orchd/spec.py`` 起点 84e1a085 的 L33-34（TYPE_CHECKING 惰性标注）、L36-41
（目录布局注释 + 两条常量）、L200-261（5 个函数），拆分记录见 §4.5.1。

**唯一非逐字处（落点修正）**：``_SCHEMA_DIR`` 的**推导方式**与拆分前不同（解析值相同）——
W4-S1 曾按「本文件在第几层」回退（``parents[2]``），W7-A 改为 ``orchd.__file__`` 锚点：
包根与本文件所在层级**解耦**，文件在包内再搬深/搬浅都不会静默错根（C8 实锤：
``parent.parent.parent`` 型深度推导拆包后少算一层 ⇒ 运行期 ``No module named 'orchd'``）。
``tests/test_spec_pkg.py`` 断言其与旧口径解析到同一 ``<repo>/schema``；
``tests/test_w7_depth_paths.py`` 断言本模块不得再出现深度链。

依赖方向：schema.py → errors.py（不新增其它边）。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orchd
from orchd.errors import ErrorCode

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行时惰性导入
    import jsonschema

# schema 相关文件的目录布局：
#   <项目根>/schema/                    ← _SCHEMA_DIR（默认 schema 根目录）
#   <项目根>/schema/_master.schema.json ← _DEFAULT_SCHEMA_PATH（无版本号时的回退 schema）
#   <项目根>/schema/v{N}/               ← 版本化 schema 子目录（按 schema_version 加载）
# W7-A：锚点为 ``orchd.__file__``（包根），与本文件所在层级无关——不再按层级回退。
_SCHEMA_DIR = Path(orchd.__file__).resolve().parent.parent / "schema"
_DEFAULT_SCHEMA_PATH = _SCHEMA_DIR / "_master.schema.json"


def _resolve_schema_path(version: int) -> Path:
    """根据版本号加载对应版本的 schema 文件。

    优先查找 schema/v{version}/_master.schema.json，
    如果不存在则回退到 schema/_master.schema.json。
    """
    version_path = _SCHEMA_DIR / f"v{version}" / "_master.schema.json"
    if version_path.exists():
        return version_path
    return _DEFAULT_SCHEMA_PATH


@lru_cache(maxsize=8)
def _load_schema(version: int) -> dict[str, Any]:
    """按版本缓存 schema 内容（P2b：避免每次 validate_structure 重复 json.loads）。"""
    schema_path = _resolve_schema_path(version)
    return json.loads(schema_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _build_validator(version: int) -> jsonschema.Draft202012Validator:
    """按版本缓存 Draft202012Validator（P2b：避免重复构建校验器）。

    jsonschema 惰性导入（task-spec-lazy-import-degrade）：顶层不再硬依赖，
    缺库时此处抛 ImportError，由 validate_structure 降级处理；模块本身
    （及整条 CLI）可无依赖导入，不再整条崩溃。
    """
    import jsonschema

    return jsonschema.Draft202012Validator(_load_schema(version))


def jsonschema_available() -> bool:
    """jsonschema 是否可用（缺依赖降级探针，task-spec-lazy-import-degrade）。

    Returns:
        True 表示完整结构校验可用；False 表示 validate_structure /
        validate_quality 将走降级路径（跳过 schema 校验但明确留痕）。
    """
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return False
    return True


def spec_degraded_guard() -> dict[str, Any]:
    """构造 jsonschema 缺依赖降级条目（既有 degraded_guards 通道，不新造字段）。

    形状与 ``orchd.gitops.guard.record_degraded_guard`` 一致
    （guard / code=E030 / severity / status / reason / hint），便于 agent
    与 doctor 按 E030 统一检索「哪些校验没在跑」。此处不导入 guard 模块，
    以保持 spec.py 依赖方向（spec → errors，不新增 spec → gitops 边）。
    """
    return {
        "guard": "jsonschema",
        "code": ErrorCode.E030.name,
        "severity": "warning",
        "status": "not_applicable",
        "reason": "jsonschema 缺依赖，结构校验未执行（已降级跳过）",
        "hint": "pip install jsonschema 后重跑以启用完整结构校验",
    }
