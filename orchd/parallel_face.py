"""orchd/parallel_face.py — 共享核心面与子域级并行面。

W7-C（task-split2-w7-c-parallel-face-subdomain）从 ``pool.py`` 抽出，两个职责：

1. **共享核心面** :data:`SHARED_CORE_FILES`：多 agent 并行改动时冲突最密集的引擎文件集合。
   子包递归 rglob + 散文件硬编码（拆包后不会真空）。
2. **子域级并行面** :data:`SHARED_CORE_SUBDOMAINS` 与四个纯函数：把「整包」粒度下沉到
   「子域」，使「改同一包不同子域」可以判定为**可并行**。

为什么要抽出：pool.py 若继续承载这两张表会涨到 650+ 行（棘轮禁止），且这两张表是**数据 +
纯函数**，与池构建/排序逻辑无耦合，独立成域后 pool.py 只保留消费方。

消费面：`orchd/pool.py` 导入 :data:`SHARED_CORE_FILES`；W7-Z1 接线后
（task-split2-w7-d-parallel-face-wiring）`detect_file_conflict` 的冲突判定单一真源化为
:func:`parallel_safe`（判据异常 ⇒ 按冲突处理，fail-closed），`subdomain_overlap` 作冲突
细节回退——四个函数自此进入 claim E010 与 request `_filter_conflicts` 的调度面
（接线前的「仅测试消费」状态结束）。

已知边界：import 期做两趟全树 rglob（面 + 子域）；四个函数不缓存**登记表本身**（负控制要能
monkeypatch 换表），但 :func:`_subdomain_modules` 的扁平清单按登记表身份（id）缓存——接线进
request 热路径后的性能前置；换表（monkeypatch.setattr / 变异脚本改源）即新 id ⇒ 缓存失效重算。

依赖方向：parallel_face.py → orchd（仅取包根，不导入 ledger / onboard / cli）。
"""

from __future__ import annotations

from pathlib import Path
import orchd

# importance 权重映射在 pool.py；本模块只放面（face）相关的数据与纯函数。

# 共享核心文件集合（task-concurrency-hardening + 拆包前置）：
# 多 agent 并行改动这些引擎/测试基础设施文件时冲突最密集。改为递归扫描
# 避免拆包后真空：_SHARED_CORE_PACKAGES 子包递归 rglob + _SHARED_CORE_MODULES 硬编码散文件，
# 迁移期双留旧路径保证兼容。
_SHARED_CORE_PACKAGES = (
    "gitops", "gitops_ops", "onboard", "cli", "ledger", "spec", "report", "worktree",
    "intake", "split", "review",
    # W8-2：split.py 子包化（task-split2-w8-2-split-pkg）；照 intake 先例删单文件条目、
    # 包级递归覆盖，不缩面
    # W8-4：review.py 子包化（task-split2-w8-4-review-pkg），同款包级递归覆盖
    # W7-C：补三个覆盖缺口——doctor / guide 此前**不在**共享核心面内（按 churn 属并行热点，
    # 改它们的任务在面上与任何任务都无差别）；ledger_sync 虽是子域根却漏在包元组外 ⇒
    # 「子域在册、文件不在面内」的自相矛盾（审查第三轮实测：6 个 .py 掉在面外）。
    "doctor", "guide", "ledger_sync",
)
_SHARED_CORE_MODULES = frozenset({
    "orchd/errors.py",
    "orchd/pool.py",
    # W7-C：本模块（面自身）也进面——与顶层四个散文件同口径，避免「登记了却不在面内」
    "orchd/parallel_face.py",
})
# W6-C6：`orchd/gitops.py` / `orchd/onboard.py` / `orchd/cli.py` 三条「迁移期双留旧路径」
# 已随之删除——三个文件早已不存在，留着只是永久不可能命中的死登记（且与
# ``tests/test_packaging.py`` 的「拆分前单文件不得回归」口径自相矛盾）。三者整体由
# ``_SHARED_CORE_PACKAGES`` 的包级递归覆盖，删条目**不缩小**共享核心面。
# W6-C7：同一 shape 在 intake 上复现——`orchd/intake.py` 子包化后若不把 ``intake`` 加入
# 包元组，六域子树整体掉出 ``SHARED_CORE_FILES``（冻结期实测：清单里只剩死条目
# ``orchd/intake.py``）。修：包元组加 ``intake``、删旧单文件路径。


def _scan_core_files() -> frozenset[str]:
    """扫描核心文件集合：子包递归 + 散文件。

    W7-A：基准目录取 ``orchd.__file__``（包根）而非本文件层级——本模块一旦被拆成子包，
    ``Path(__file__).parent`` 会指向子包而非 ``orchd/``，整面子树静默掉出（与 C8 的
    ``parent.parent.parent`` 同族故障）。同时不依赖 CWD。
    """
    base = Path(orchd.__file__).resolve().parent
    files: set[str] = set(_SHARED_CORE_MODULES)
    for pkg in _SHARED_CORE_PACKAGES:
        pkg_dir = base / pkg
        if pkg_dir.is_dir():
            for p in pkg_dir.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
                # 相对 orchd/ 父目录的路径，如 orchd/gitops/file.py
                try:
                    rel = p.relative_to(base.parent).as_posix()
                    files.add(rel)
                except ValueError:
                    continue
        else:
            # 子包目录不存在（如拆包前），跳过，硬编码已覆盖
            continue
    if not files:
        raise RuntimeError("SHARED_CORE_FILES 扫描结果为空（fail-closed）")
    return frozenset(files)


SHARED_CORE_FILES = _scan_core_files()

# --------------------------------------------------------------------------- #
# W7-C：并行面从「整包」下沉到「子域」。
#
# 粒度问题：`SHARED_CORE_FILES` 是整包递归的集合，两个任务改同一包的不同子域（如
# `orchd/cli/_util/` 与 `orchd/cli/commands/`）在该面上**没有区别** ⇒ 只能一律当共享核心，
# 无法据此判定「可并行 / 必串行」。此处引入子域级并行单元：
#   · 落在已声明子域实证清单内的模块 ⇒ 每个 `.py` 独立成项（子域内不同文件可并行）；
#   · 其余模块 ⇒ **包级兜底**（同一包根下的散文件视为同一子域，保守串行）。
# 因此「把清单退回整包」会让细粒度并行的判定失效（守卫对此有负控制）。
# --------------------------------------------------------------------------- #
_SHARED_CORE_SUBDOMAIN_ROOTS = (
    "orchd/cli/commands",
    "orchd/cli/_util",
    "orchd/gitops/hook",
    "orchd/gitops/guard",
    "orchd/gitops_ops",
    "orchd/onboard/lifecycle/core",
    "orchd/intake",
    "orchd/worktree/layout",
    "orchd/worktree/recycle",
    "orchd/ledger_sync",
    "orchd/doctor",
    "orchd/guide",
)


def _scan_subdomain_modules() -> dict[str, tuple[str, ...]]:
    """子域根 → 该子域（递归）实证模块清单。

    fail-closed：任一已声明子域扫不到 `.py`（拆包漂移 / 目录改名）即抛 RuntimeError——
    静默变空会让「每个 .py 独立成项」退化成「整包子域」，并行判定随之失真。
    """
    base = Path(orchd.__file__).resolve().parent
    result: dict[str, tuple[str, ...]] = {}
    for root in _SHARED_CORE_SUBDOMAIN_ROOTS:
        root_dir = base / root[len("orchd/"):]
        modules: list[str] = []
        if root_dir.is_dir():
            for p in root_dir.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
                try:
                    modules.append(p.relative_to(base.parent).as_posix())
                except ValueError:
                    # 与 _scan_core_files 同口径：越界路径跳过（链接指向仓外等）
                    continue
        modules.sort()
        if not modules:
            raise RuntimeError(
                f"并行面子域 {root} 扫不到任何模块（拆包漂移，fail-closed）"
            )
        result[root] = tuple(modules)
    return result


SHARED_CORE_SUBDOMAINS: dict[str, tuple[str, ...]] = _scan_subdomain_modules()

# 扁平清单缓存（键 = 登记表 dict 的 id；见 _subdomain_modules docstring）
_FLAT_MODULES_CACHE: dict[int, frozenset[str]] = {}


def _subdomain_modules() -> frozenset[str]:
    """实证清单的扁平集合（成员判断用）：清单内的模块各自独立成项。

    按当前 :data:`SHARED_CORE_SUBDOMAINS` 现算，但结果**按登记表身份（id）缓存**——
    接线进 detect_file_conflict（W7-Z1）后本函数处于 claim/request 调用热路径，
    每次重建 ~百元级 frozenset 会放大成 O(候选数 × 在途数 × 清单规模)；
    缓存键取 ``id(SHARED_CORE_SUBDOMAINS)``：monkeypatch.setattr 换表 / 变异脚本
    改源重导入都会产生新 dict（新 id）⇒ 缓存自然失效重算，负控制语义不变。
    旧 id 条目残留（每次换表一条，量级为个位数）不回收，量级无害。
    """
    key = id(SHARED_CORE_SUBDOMAINS)
    cached = _FLAT_MODULES_CACHE.get(key)
    if cached is None:
        cached = frozenset(
            m for modules in SHARED_CORE_SUBDOMAINS.values() for m in modules
        )
        _FLAT_MODULES_CACHE[key] = cached
    return cached


def subdomain_of(rel: str) -> str:
    """路径 → 并行单元（子域）。

    - 在已声明子域的实证清单内 ⇒ **该 `.py` 自身**（细粒度，可并行）；
    - 否则 ⇒ **包级兜底**：`orchd/<pkg>/<file>.py` 归 `orchd/<pkg>`；`orchd/<file>.py` 归自身。
    """
    if rel in _subdomain_modules():
        return rel
    parts = rel.rstrip("/").split("/")
    if len(parts) >= 3 and parts[0] == "orchd" and parts[-1].endswith(".py"):
        return "/".join(parts[:2])
    return rel.rstrip("/")


def subdomains_of_declared(declared: str) -> set[str]:
    """声明项（文件 / 目录）→ 它占用的并行单元集合。

    目录声明（`orchd/cli/`）展开为其下全部子域：清单内模块各自成项，其余走包级兜底。
    """
    stem = declared.rstrip("/")
    if not declared.endswith("/"):
        return {subdomain_of(stem)}
    units: set[str] = {stem}
    for core in SHARED_CORE_FILES:
        if core.startswith(stem + "/"):
            units.add(subdomain_of(core))
    return units


def _declared_covers(declared: str, unit: str) -> bool:
    """目录声明是否覆盖某个并行单元（纯前缀判定，与是否在共享核心面内无关）。

    为什么单独判：`subdomains_of_declared` 的目录展开只遍历 :data:`SHARED_CORE_FILES`，
    **非 orchd 目录**（``src/``）在那里没有任何条目 ⇒ 只靠「单元集合求交」会让
    ``src/`` 与 ``src/foo.py`` 判成可并行（漏报）。前缀覆盖补上这一层。

    不区分「是否带尾斜杠」：``orchd`` 与 ``orchd/`` 语义上都指整包，若让无斜杠形态只等于
    自身单元，``(["orchd"], ["orchd/cli/commands/"])`` 会误判可并行——本判定取
    **fail-closed**（多判相撞只会保守串行，不会放行真冲突）。
    """
    stem = declared.rstrip("/")
    return unit == stem or unit.startswith(stem + "/")


def subdomain_overlap(files_a: list[str] | set[str],
                      files_b: list[str] | set[str]) -> list[str]:
    """两组声明**在同一子域内**相撞的子域（排序；空 = 跨子域）。

    两路判定：① 并行单元求交（核心面内按子域细粒度）；② 目录声明对其侧单元的前缀覆盖
    （补 ``src/`` vs ``src/foo.py`` 这类「目录不在核心面内」的漏报）。
    """
    units_a = {f: subdomains_of_declared(f) for f in files_a}
    units_b = {f: subdomains_of_declared(f) for f in files_b}
    hits: set[str] = set()
    for set_a in units_a.values():
        for set_b in units_b.values():
            hits |= set_a & set_b
    for decl_a, set_a in units_a.items():
        for unit_b in {u for s in units_b.values() for u in s}:
            if _declared_covers(decl_a, unit_b):
                hits.add(unit_b)
    for decl_b, set_b in units_b.items():
        for unit_a in {u for s in units_a.values() for u in s}:
            if _declared_covers(decl_b, unit_a):
                hits.add(unit_a)
    return sorted(hits)


def parallel_safe(files_a: list[str] | set[str],
                  files_b: list[str] | set[str]) -> bool:
    """两组声明是否可并行：无同一子域相撞即安全（跨子域 = 可并行）。"""
    return not subdomain_overlap(files_a, files_b)


