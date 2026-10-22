"""orchd/spec/references.py — 跨引用完整性校验域（W4-S2 拆分，纯搬移）。

来源：``orchd/spec/__init__.py`` 起点 bd808744 的 L292-480（``validate_references``：
ID 唯一性 / 引用存在性 / DAG 无环），拆分记录见
design/file-split-program-20261015.md §4.5.2。

依赖方向：references.py → errors.py / spec.structure。
"""

from __future__ import annotations

from collections import deque

from orchd.errors import ErrorCode
from orchd.spec.structure import Master, ValidationError

def validate_references(master: Master) -> list[ValidationError]:
    """跨引用完整性校验：ID 唯一性、引用存在性、DAG 无环。

    校验顺序：
    1. E006 —— task / module 的 ID 唯一性检查。
    2. E005 —— task.module 和 task.depends_on 的引用目标是否存在；
       以及 shared 文件的存在性（仅当 master 位于 .orchd/ 目录时检查）。
    3. E004 —— 使用 Kahn 拓扑排序算法检测 task 依赖图中的环：
       逐步移除入度为零的节点，若最终仍有剩余节点则说明存在环。

    错误采用累积策略，不会因单个错误而提前终止，以便一次性返回所有问题。
    返回 E004/E005/E006 错误列表；合法时返回空列表。

    归档回读合并（task-master-archive-readback-merge）：``depends_on`` 的存在性
    真源是 ``master.tasks ∪ 归档 ids`` 而非仅 ``master.tasks``——终态定义归档后，
    活跃任务对历史任务的引用仍是**合法引用**（归档只改变定义的存放位置，不使引用
    失效）。    归档 id 经 ``orchd.definition_archive.read_archive_ids`` 取得（函数内
    惰性导入，与既有 ledger 惰性导入同模式）。清单声明归档在场但**不可读**（缺文件
    / 不可解析 / ``count``/``sha256`` 不符 / 条目 id 重复）→ 追加**一条** E003
    （path ``$.definition_archive``），且 ``depends_on`` 存在性检查**整体判「不适用」
    并跳过**（真源不完整时逐条判定必然误报——与「账本不可用时终态类检查整体不适用」
    同口径），**不静默当作「无归档」**。无清单或空骨架时本函数行为逐字不变。
    """

    errors: list[ValidationError] = []
    tasks = master.tasks
    modules = master.modules

    # --- E006: ID 唯一性 ---
    task_ids: list[str] = []
    module_ids: list[str] = []

    for i, t in enumerate(tasks):
        tid = t.get("id", "")
        if tid in task_ids:
            errors.append(
                ValidationError(
                    code=ErrorCode.E006,
                    path=f"$.tasks[{i}].id",
                    message=f"duplicate task_id: '{tid}'",
                ))
        else:
            task_ids.append(tid)

    for i, m in enumerate(modules):
        mid = m.get("id", "")
        if mid in module_ids:
            errors.append(
                ValidationError(
                    code=ErrorCode.E006,
                    path=f"$.modules[{i}].id",
                    message=f"duplicate module_id: '{mid}'",
                ))
        else:
            module_ids.append(mid)

    task_id_set = set(task_ids)
    module_id_set = set(module_ids)

    # --- E003: 归档清单可读性（引用解析真源的完整性）---
    # （task-master-archive-readback-merge）清单在场则归档必须可读，否则
    # depends_on 的真源不完整——fail-closed：报 E003 且**不**退化为「无归档」。
    archive_ids: frozenset[str] = frozenset()
    #: 引用目标全集是否可完整判定（归档不可读 ⇒ False ⇒ depends_on 检查整体不适用）
    reference_universe_complete = True
    # 惰性导入（与 validate_source 惰性导入 ledger 同模式）：spec.py 依赖方向为
    # errors.py，本模块不因引用解析而把定义归档模块提到顶层。
    from orchd.definition_archive import ARCHIVE_KEY as _archive_key
    from orchd.definition_archive import read_archive_ids

    if isinstance(master.raw, dict) and master.raw.get(_archive_key):
        from orchd.definition_archive import DefinitionArchiveError

        try:
            archive_ids = read_archive_ids(master.raw, master.source_path)
        except DefinitionArchiveError as exc:
            reference_universe_complete = False
            errors.append(
                ValidationError(
                    code=ErrorCode.E003,
                    path=f"$.{_archive_key}",
                    message=(
                        f"definition_archive 在场但不可读，depends_on 引用解析"
                        f"真源不完整（该项检查整体不适用，不逐条误报）: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                ))
    resolvable_ids = task_id_set | set(archive_ids)

    # --- E005: 引用存在性 ---
    for i, t in enumerate(tasks):
        # module 引用
        mod = t.get("module", "")
        if mod and mod not in module_id_set:
            errors.append(
                ValidationError(
                    code=ErrorCode.E005,
                    path=f"$.tasks[{i}].module",
                    message=f"module '{mod}' not found in modules[]",
                ))
        # depends_on 引用（真源 = master.tasks ∪ 归档 ids，见本函数 docstring）。
        # 引用全集不可完整判定时本项整体不适用并跳过（不逐条误报）——已在上面
        # 以 E003 留痕。
        if reference_universe_complete:
            for j, dep in enumerate(t.get("depends_on", [])):
                if dep not in resolvable_ids:
                    errors.append(
                        ValidationError(
                            code=ErrorCode.E005,
                            path=f"$.tasks[{i}].depends_on[{j}]",
                            message=
                            f"depends_on references unknown task_id: '{dep}'",
                        ))

    # --- E005: default_line 须为 lines 的键（前移 line.py 运行时口径） ---
    # pass7 P2-7 残留收口：line.py 已是执行点（非法 E005 硬拒），validate 在此
    # 前移，同错早暴露。单线（无 lines）或未声明 default_line 时跳过。
    project = master.raw.get("project", {}) if isinstance(master.raw, dict) else {}
    if isinstance(project, dict):
        lines = project.get("lines", {})
        declared = project.get("default_line", "")
        if isinstance(lines, dict) and lines and isinstance(declared, str) and declared:
            if declared not in lines:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E005,
                        path="$.project.default_line",
                        message=(
                            f"default_line '{declared}' not found in "
                            f"project.lines ({sorted(lines)})"
                        ),
                    ))

    # --- E005: shared 文件存在性 ---
    # shared 中声明的文件由 BOOTSTRAP 阶段负责写入，路径为相对于项目根的相对路径。
    # 只有当 _master.json 位于标准的 .orchd/ 目录下时，才能通过 parent.parent 可靠
    # 推算出项目根目录；对于从任意路径加载的 master（如临时校验场景），无法对文件
    # 系统布局做假设，因此跳过此检查。
    shared = master.shared
    if shared and master.source_path.parent.name == ".orchd":
        project_root = master.source_path.parent.parent
        for key, rel in shared.items():
            if not (project_root / rel).exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E005,
                        path=f"$.shared.{key}",
                        message=(f"shared file not found: '{rel}'"
                                 f"（BOOTSTRAP 声明了 shared.{key} 但未写入该文件）"),
                    ))

    # --- E004: DAG 环检测（Kahn 拓扑排序） ---
    # 构建邻接表与入度表（仅使用已存在的 ID，避免 E005 干扰）
    in_degree: dict[str, int] = {tid: 0 for tid in task_id_set}
    dependents: dict[str, list[str]] = {tid: [] for tid in task_id_set}

    for t in tasks:
        tid = t.get("id", "")
        if tid not in task_id_set:
            continue
        for dep in t.get("depends_on", []):
            if dep in task_id_set:
                in_degree[tid] += 1
                dependents[dep].append(tid)

    queue: deque[str] = deque(tid for tid, deg in in_degree.items()
                              if deg == 0)
    visited_count = 0

    while queue:
        node = queue.popleft()
        visited_count += 1
        for child in dependents[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if visited_count < len(task_id_set):
        # 找出参与环的节点
        cycle_nodes = [tid for tid, deg in in_degree.items() if deg > 0]
        errors.append(
            ValidationError(
                code=ErrorCode.E004,
                path="$.tasks",
                message=
                f"dependency cycle detected involving: {', '.join(sorted(cycle_nodes))}",
            ))

    return errors
