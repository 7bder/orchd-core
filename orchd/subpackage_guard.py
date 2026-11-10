"""拆包子包入口登记守卫（task-decl-subpackage-test-anchor）。

W6-C8 实测事故：``orchd/gitops/hook.py`` 拆成子包后，原单文件键不再命中任何改动，
E039 对该域静默失明（当时只能手工逐子域补键，其余 33 个包入口仍无键）。本模块把
「拆包子包入口必须做出决定」变成机判：入口要么有 E039 键（精确/通配）、要么在
豁免表、要么子树有锚点、要么是纯重导出聚合入口。

依赖方向：subpackage_guard.py → shared_entries.py（只读登记表）/ 标准库 ast。
"""

from __future__ import annotations

import ast
from pathlib import Path

from orchd.shared_entries import (
    CLI_COMMAND_MODULE_EXEMPT,
    SHARED_ENTRY_TESTS,
    shared_entry_key_matches,
)

# 拆包子包入口（非顶层 ``orchd/**/__init__.py``）的显式豁免：
# ``{仓库相对 POSIX 路径: 理由}``，判据见 :func:`subpackage_entries_unaccounted`。
# 只收「含实现逻辑且无 E039 锚点」的入口——纯重导出聚合入口由 AST 自动判定通过，
# 不必逐条写理由（否则豁免表会退化成走过场）。
SUBPACKAGE_ENTRY_EXEMPT: dict[str, str] = {
    "orchd/guide/__init__.py": (
        "GUIDANCE_STEPS 词表真源（纯数据无行为），对外面由 tests/test_guide.py 覆盖"
    ),
    "orchd/onboard/__init__.py": (
        "入口承载向后兼容 re-export 与生命周期聚合类，"
        "行为面由 tests/test_onboard.py 覆盖"
    ),
    "orchd/storage/__init__.py": (
        "StorageBackend 端口 ABC 就近定义（端口/适配器分层），"
        "行为面由 tests/test_storage_port_ledger.py 等 storage 系列覆盖"
    ),
    "orchd/guards/__init__.py": (
        "宿主 guard 分派骨架（评估器注册表 + 上下文/命中数据类型，"
        "task-host-guard-done-wiring）：配置层行为面由 tests/test_host_guard_config.py、"
        "分派行为面由 tests/test_host_guard_wiring.py 覆盖；入口仅承载纯数据结构与注册表，"
        "不自动加载评估器、无 import 期副作用"
    ),
}


def _is_literal_all(value: ast.expr | None) -> bool:
    """``__all__`` 右值必须是字面量 list/tuple of str。

    审查返修（spec review Q3）：旧判据只看赋值目标名、不看右值，于是
    ``__all__ = [p.name for p in plugins if p.enabled]`` 与
    ``__all__: list[str] = _register_and_build()`` 这类**导入期执行逻辑**的写法会被
    误判为纯重导出，从而绕过本守卫的决策要求。
    """
    if not isinstance(value, (ast.List, ast.Tuple)):
        return False
    return all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in value.elts
    )


def _is_pure_reexport_module(path: Path) -> bool:
    """模块顶层只有 import / docstring / 字面量 ``__all__`` ⇒ 纯重导出聚合入口。

    W5-W8 拆包的统一形态：聚合入口零逻辑、只 re-export 子域；其行为面必然落在
    子域，故不构成「需要登记测试」的决策单元。语法不可解析时保守返回 ``False``
    （当作含逻辑，要求显式决策）。

    审查返修收紧（spec review Q3）：
    - ``__all__`` 右值必须是字面量（见 :func:`_is_literal_all`），带推导式/函数调用的
      右值视为**含逻辑**；
    - 裸 ``import orchd...`` 视为可能触发注册副作用（如 ``import
      orchd.cli.commands.session`` 触发 ``_REGISTER_ORDER``），不判纯重导出；
      标准库/第三方裸 import 不影响判定（不会有 orchd 侧注册副作用）。

    显式取舍（spec 复审 Q3 残余）：``from orchd.x import y`` **仍放行**——它是拆包聚合
    入口的统一 re-export 形态（25 个现有纯重导出入口全部如此），副作用式注册以裸
    import 书写；把 ImportFrom 一并判非纯会让拆包聚合入口全部失去「自动通过」，退化成
    25 条逐条豁免。若将来出现「以 ImportFrom 承载注册副作用」的入口，应改为裸 import
    或进 SUBPACKAGE_ENTRY_EXEMPT，而非放宽本判据。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return False
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            continue
        if isinstance(node, ast.Import):
            if any(
                alias.name == "orchd" or alias.name.startswith("orchd.")
                for alias in node.names
            ):
                return False
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names == ["__all__"] and _is_literal_all(node.value):
                continue
            return False
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id == "__all__"
                and _is_literal_all(node.value)
            ):
                continue
            return False
        return False
    return True


def subpackage_entries_unaccounted(repo_root: Path) -> list[str]:
    """拆包子包入口「既无锚点也未决策」清单（task-decl-subpackage-test-anchor）。

    决策单元 = 非顶层 ``orchd/**/__init__.py``。任一条件成立即视为已决策：

    1. 入口本身在 ``SHARED_ENTRY_TESTS`` 有精确键或通配键；
    2. 入口在 ``CLI_COMMAND_MODULE_EXEMPT`` / ``SUBPACKAGE_ENTRY_EXEMPT`` 内；
    3. 该包子树内任一文件有精确键或通配键（行为面已在子域登记）；
    4. 入口是纯重导出（见 :func:`_is_pure_reexport_module`）。

    都满足不了 ⇒ 返回该路径：拆包后无人负责的入口，E039 对整棵子树静默失明
    （W6-C8 的原始事故形态）。返回值用仓库相对 POSIX 路径。
    """
    root = Path(repo_root)
    decided = (
        set(SHARED_ENTRY_TESTS)
        | set(CLI_COMMAND_MODULE_EXEMPT)
        | set(SUBPACKAGE_ENTRY_EXEMPT)
    )
    wildcards = tuple(entry for entry in decided if entry.endswith("/"))

    def _has_anchor(rel: str) -> bool:
        if rel in decided:
            return True
        # 键匹配语义复用 shared_entries 单一真源（code 审查 Q2a：不再第二份前缀实现）。
        return any(shared_entry_key_matches(entry, rel) for entry in wildcards)

    unaccounted: list[str] = []
    for init in sorted((root / "orchd").rglob("__init__.py")):
        rel = init.relative_to(root).as_posix()
        if rel == "orchd/__init__.py":
            continue
        if _has_anchor(rel):
            continue
        subtree = (
            child.relative_to(root).as_posix()
            for child in init.parent.rglob("*.py")
            if child != init
        )
        if any(_has_anchor(child) for child in subtree):
            continue
        if _is_pure_reexport_module(init):
            continue
        unaccounted.append(rel)
    return unaccounted
