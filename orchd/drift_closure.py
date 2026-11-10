"""orchd/drift_closure.py — done 时按真实改动集算「缺失镜像」（B1）。

口径见 ``design/guard-drift-prevention-20261029.md`` §5.1（B1 出口判据）/ §5.2 判据 3：done 只
**消费已算好的** changed_paths（分支 diff ∪ 工作树改动，由 ``entry_guard`` 一次算出），判断「改了
真源却没同批带上镜像」。本模块不遍历目录树、不起子进程、不改 ``shared_entries.py``、不新增映射表；
宿主侧的「真源 → 镜像」映射经 :func:`host_mapping_gaps` 显式注入，同样不构造第二张硬编码表。

**三处只读映射源**（AC1）：① §6「第二真源」列（解析器**复用** ``tests/test_docs.py::_table_rows``，
不在此复制实现，identity 由 test_drift_closure 锁）；② ``orchd/docsgen/generators.py`` 的输出
清单（AST 读 ``GENERATORS`` + 各 ``gen_*`` 函数体的 orchd 导入 / ``ROOT / …`` 路径拼接）；
③ ``orchd/shared_entries.py::SHARED_ENTRY_TESTS``（共享入口 ← 登记测试）。
``dependency-graph.md`` 的真源是全部 ``orchd/**``（宽面，按它硬触发会把任意代码改动都算缺口），
改由 §6 同格的 ``.orchd/shared/deps.json`` 精确登记。

**硬触发只四类**（§5.2 判据 3）：``schema`` / ``cli``（``parser.py`` 注册面）/ ``generated``
（``docs/_generated``）/ ``census``；其余（「无副本（已单源）」域、prose 与保留域、共享入口类、
``cli_surface`` 命令子面、同源多镜像的 ``mirror_partial``）只出 warning。warning 只进
``degraded_guards``（E030），不改变 done 结果；hard 档命中硬触发才阻断，且复用 E039 通道 +
``details.rule="drift_closure"``（不新增错误码 / 通道登记 / guide 词表）。
硬类判定走**真源路径前缀 + 生成器边**（:func:`_classify`），§6 分类列（生成/派生/保留/删除）只服务
软类提示（:func:`co_change_gaps`）——分类列描述的是「落点是否手工维护」，不是真源类别。
§6「第二真源」列支持**生成器-产物分组**写法（组内 ``真源 → 镜像``、组间 ``；``；无箭头 = 整格按旧
口径一组）：:func:`co_change_gaps` 逐组、**单向**判定（真源改动才提示镜像，镜像侧单独改动不反向
提示）；单路径组 = 无镜像副本的触发面，**不参与**共变提示——原先 ``.githooks/pre-push`` /
``.github/workflows/ci.yml`` / ``tests/STABLE_LANES.md`` 并列一行，改 ``ci.yml`` 的注释性维护也会
提示「应同步」，分组后这类噪音归零。
``cli`` 只挂 ``parser.py``：``commands.md`` 的内容只有 ``parser._actions`` 的顶层命令名
（生成器事实），命令子模块注册的二级子命令不进该生成物 ⇒ 硬判会把日常改动整体误阻断
（见 :func:`cli_surface_gaps`）。

**与 E039 的分工**：E039 判「改了共享入口但 verify_command 没覆盖登记测试」（fail-closed），本模块
判「改动集缺镜像」；同一 key 由前者判为缺口时调用方传进 ``skip_shared_keys``，不再重复报。

**已知边界（如实声明）**：① 默认只判改动集合，不做内容等价判定（镜像改了但改错属 review 面）；
纯注释/空白改动的过滤由调用方经 ``semantic_changed`` / ``patch`` 注入
（:func:`semantic_paths_from_patch` 给出 unified diff → 语义改动集的实现）——done 链路的
``entry_guard._drift_patch`` 取本任务「分支 diff ∪ 工作树改动」的 ``git diff -U0`` 文本注入，
取不到时回落路径级判定（向后兼容）；
② ``census`` 只抓「棘轮根内新增/删除 ``.py``」这类必然要收基线的形状，体量增长由 verify 的
``file_census.py --check`` 兜底（本模块不扫目录树）；③ §6 目录级落点与 glob 跳过不猜；④ 映射源
不可读/解析出空 ⇒ 该类跳过（不报假阳性），但**不静默**：由 :func:`mapping_problems` 记 degraded。
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.constants import ORCHD_DRIFT_CLOSURE_ENV

MODE_WARN, MODE_HARD, MODE_OFF = "warn", "hard", "off"
MODES, MODE_DEFAULT = (MODE_WARN, MODE_HARD, MODE_OFF), MODE_WARN

#: 硬触发四类（§5.2 判据 3）；其余类别只产生 warning。
HARD_CLASSES = frozenset({"schema", "cli", "generated", "census"})

DESIGN_REL = "design/guard-drift-prevention-20261029.md"
GENERATOR_REL = "orchd/docsgen/generators.py"
MANIFEST_REL = "orchd/docs_manifest.py"
GENERATED_DIR = "docs/_generated/"
CENSUS_BASELINE_REL = "file-census-baseline.json"
CENSUS_RATCHET_ROOTS = ("orchd/", "scripts/")
SUBDOMAIN_REGISTRY_REL = "orchd/subdomain_census.py"
CLI_PARSER_REL = "orchd/cli/parser.py"
CLI_COMMANDS_DIR = "orchd/cli/commands"

MIRROR_HEADER = "镜像域"  # 与 tests/test_docs.py::_MIRROR_HEADER 同值（表定位靠它）
MIRROR_COLUMNS = 7  # 域 / 第二真源 / 校验面 / 分类 / owner / 触发 / 负控
_BROAD_OUTPUTS = frozenset({"dependency-graph.md"})  # 真源=全域 AST，不按函数体派生


@dataclass(frozen=True)
class MirrorRow:
    """§6 决策表的一行：``domain`` / ``category``（生成·派生·保留·删除）/ ``paths``。

    ``groups`` 是「生成器-产物分组」（每组 ``(真源, *镜像)``，见 :func:`_cell_groups`）：
    空 = 旧口径（同一行内**对称**：任一落点改动提示其缺席的兄弟落点）；非空时共变判定
    **逐组、单向**（见 :func:`co_change_gaps`）。
    """

    domain: str
    category: str
    paths: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class MirrorGap:
    """一条「缺镜像」发现：``klass``（硬类四选一 / 软类 mirror·mirror_partial·shared_entry·
    cli_surface）+ ``domain``（§6 域名 / census 面名 / 共享入口 key）+ ``source``（真源）
    + ``mirrors``。"""

    klass: str
    domain: str
    source: str
    mirrors: tuple[str, ...]

    @property
    def hard(self) -> bool:
        """是否属硬触发四类（hard 档据此阻断；warn 档一并不阻断）。"""
        return self.klass in HARD_CLASSES

    def as_dict(self) -> dict[str, Any]:
        """结构化视图（degraded_guards.context / E039 details 共用）。"""
        return {
            "class": self.klass,
            "domain": self.domain,
            "source": self.source,
            "mirrors": list(self.mirrors),
            "hard": self.hard,
        }


def resolve_mode(value: str | None = None) -> str:
    """解析 ``ORCHD_DRIFT_CLOSURE`` 三档；**非法值回落 warn**（拼错不得改变 done 行为）。"""
    raw = os.environ.get(ORCHD_DRIFT_CLOSURE_ENV, "") if value is None else value
    mode = (raw or "").strip().lower()
    return mode if mode in MODES else MODE_DEFAULT


def _table_rows_impl() -> Any:
    """取 §6 表格解析器（**复用** ``tests/test_docs.py::_table_rows``，不复制实现）。

    ``tests/`` 是命名空间包（CLI 与测试进程都已把仓库根放进 ``sys.path``）；导入失败（裁剪发布态
    等）返回 ``None`` ⇒ 该映射源跳过，由 :func:`mapping_problems` 留痕。
    """
    try:
        from tests.test_docs import _table_rows
    except Exception:  # noqa: BLE001 - 缺测试包时降级，不因映射源缺失让 done 失败
        return None
    return _table_rows


def parse_mirror_table(text: str, rows_impl: Any = None) -> list[MirrorRow]:
    """解析 §6 决策表；表缺失 / 行残缺 ⇒ 返回空列表（不报假阳性）。"""
    parser = rows_impl if rows_impl is not None else _table_rows_impl()
    if parser is None or not text:
        return []
    out: list[MirrorRow] = []
    for cells in parser(text, MIRROR_HEADER) or []:
        if len(cells) != MIRROR_COLUMNS:
            continue
        out.append(
            MirrorRow(cells[0], cells[3], tuple(_cell_paths(cells[1])), _cell_groups(cells[1]))
        )
    return out


_PATH_TOKEN = re.compile(r"`([^`]+)`")
#: 分组语法：组间用 ``；``/``;``，组内用 ``→``/``->`` 表达「真源 → 镜像」。
GROUP_SEPARATOR_RE = re.compile(r"[；;]")
GROUP_ARROW_RE = re.compile(r"(?:→|->)")


def _cell_paths(cell: str) -> list[str]:
    """§6 单元格 → 可精确比对的仓库相对路径（去 ``::符号``、跳过目录级/glob/非路径）。"""
    out: list[str] = []
    for token in _PATH_TOKEN.findall(cell or ""):
        path = token.split("::", 1)[0].strip()
        if not path or path.endswith("/") or "/" not in path or any(ch in path for ch in "*?<>"):
            continue
        if path not in out:
            out.append(path)
    return out


def _cell_groups(cell: str) -> tuple[tuple[str, ...], ...]:
    """§6 单元格 → **生成器-产物分组**：每组 ``(真源/触发面, *镜像)``。

    组间 ``；``（或 ``;``）分隔，组内 ``→``（或 ``->``）表达方向；**没有箭头**的单元格返回 ``()``
    = 整格按旧口径一组（向后兼容）。单路径组 = 无镜像副本的触发面，共变判定不提示。去重只在
    **组内**做：同一路径可以出现在多个组里当真源/镜像（``a → b；a → c`` 两组都保留 ``a``）。
    """
    if not GROUP_ARROW_RE.search(cell or ""):
        return ()
    groups: list[tuple[str, ...]] = []
    for segment in GROUP_SEPARATOR_RE.split(cell or ""):
        paths: list[str] = []
        for part in GROUP_ARROW_RE.split(segment):
            for path in _cell_paths(part):
                if path not in paths:
                    paths.append(path)
        if paths:
            groups.append(tuple(paths))
    return tuple(groups)


def _generator_outputs(text: str) -> dict[str, str]:
    """``GENERATORS`` 字典：生成函数名 → 输出文件名（AST 只读，不 import 生成器）。

    ``ast.parse`` 对 NUL 字节等非法输入抛 ``ValueError``，与 ``SyntaxError`` 同按不可解析处理，
    否则异常会一路逃出 done（E999）。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    outputs: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "GENERATORS" for t in node.targets):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if isinstance(value, ast.Name):
                    outputs[value.id] = key.value
    return outputs


def _module_file(module: str, project_root: Path) -> str | None:
    """``orchd.x.y`` → ``orchd/x/y.py`` 或 ``orchd/x/y/__init__.py``（不存在则 None）。"""
    if not module.startswith("orchd"):
        return None
    base = module.replace(".", "/")
    for candidate in (base + ".py", base + "/__init__.py"):
        if (project_root / candidate).is_file():
            return candidate
    return None


def _binop_path(node: ast.AST) -> str | None:
    """``ROOT / "a" / "b.json"`` → ``a/b.json``（只认 ``/`` 拼接与 ROOT 根）。"""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = _binop_path(node.left), node.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        text = right.value.replace("\\", "/").strip("/")
        if not text:
            return None
        return f"{left}/{text}" if left else text
    if isinstance(node, ast.Name) and node.id == "ROOT":
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace("\\", "/").strip("/") or None
    return None


def _function_sources(fn: ast.AST, project_root: Path) -> list[str]:
    """一个生成函数体内可判定的真源（orchd 模块文件 + ``ROOT / ...`` 数据文件）。"""
    modules: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    out = [rel for rel in (_module_file(m, project_root) for m in modules) if rel]
    for node in ast.walk(fn):
        rel = _binop_path(node) if isinstance(node, ast.BinOp) else None
        if rel and (project_root / rel).is_file():
            out.append(rel)
    return list(dict.fromkeys(out))


def generated_edges(text: str, project_root: Path) -> list[tuple[str, str]]:
    """生成物 ← 真源边（``(source, mirror)``，去重保序）。宽面生成物（``dependency-graph.md``）
    不在此列（真源是全部 ``orchd/**``，按它硬触发会把任意代码改动都算缺口），由 §6 同格的
    ``deps.json`` 精确登记（见 :func:`_cell_edges`）。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    outputs = _generator_outputs(text)
    edges: list[tuple[str, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        name = outputs.get(fn.name)
        if not name or name in _BROAD_OUTPUTS:
            continue
        mirror = GENERATED_DIR + name
        for source in _function_sources(fn, project_root):
            if (source, mirror) not in edges:
                edges.append((source, mirror))
    return edges


def _manifest_edges(project_root: Path | None) -> list[tuple[str, str]]:
    """manifest 登记的真源 → 产物边（仅保留项目内可达的真源）。"""
    if project_root is None:
        return []
    try:
        from orchd import docs_manifest
    except Exception:  # noqa: BLE001 - 清单不可读 ⇒ 该类跳过（由 mapping_problems 留痕）
        return []
    out: list[tuple[str, str]] = []
    for source, mirror in docs_manifest.edges():
        if (project_root / source).is_file():
            out.append((source, mirror))
    return out


def _cell_edges(rows: Sequence[MirrorRow]) -> list[tuple[str, str]]:
    """§6 同格「json 数据真源 → 生成物」边：二者并排登记（``dependency-graph.md`` +
    ``deps.json``）——json 侧改动必然改变生成物内容（该生成物还内嵌 deps.json 的 sha256）。"""
    edges: list[tuple[str, str]] = []
    for row in rows:
        docs = [p for p in row.paths if p.startswith(GENERATED_DIR)]
        for data in row.paths:
            if data.startswith(GENERATED_DIR) or not data.endswith(".json"):
                continue
            for doc in docs:
                if (data, doc) not in edges:
                    edges.append((data, doc))
    return edges


def _classify(source: str) -> str:
    """真源 → 硬触发类别（前三类；``census`` 由 :func:`census_gaps` 单独产）。"""
    if source.startswith("schema/"):  # schema 面：字段表生成物的真源目录
        return "schema"
    if source == CLI_PARSER_REL:
        return "cli"
    return "generated"


def _domain_of(mirror: str, rows: Sequence[MirrorRow]) -> str:
    """镜像落点 → §6 域名（找不到时回落到生成物类）。"""
    return next((row.domain for row in rows if mirror in row.paths), "生成物新鲜度")


def hard_gaps(
    rows: Sequence[MirrorRow],
    edges: Sequence[tuple[str, str]],
    changed: Sequence[str],
    *,
    semantic_changed: Iterable[str] | None = None,
) -> list[MirrorGap]:
    """真源在改动集时的镜像缺口，按**真源**收敛（``edges`` 上游已去重）。

    ``semantic_changed`` 非 ``None`` 时按它过滤改动集（纯注释/空白改动不算真源改动，见
    :func:`semantic_paths_from_patch`）；``None`` = 旧口径（只判路径集合）。

    生成器输出是内容驱动的（``contracts/events.py`` 同时派生 ``events.md`` 与
    ``statemachine.md``，只改 ``CONTRACTS`` 时后者字节不变），故逐条边要求镜像必现会假阻断。
    分两档：
    * **一个镜像都没动** ⇒ 该真源类别（硬类四类之一），hard 档阻断；
    * **部分镜像动了** ⇒ ``mirror_partial`` 软提示（同源必有内容变化的另一份没跟上，
      但改动集层面无法区分「另一份不参与本次内容」与「漏 regen」，不能硬阻断、也不能静默）。
    """
    changed_set = set(changed)
    if semantic_changed is not None:
        changed_set &= set(semantic_changed)
    by_source: dict[str, list[str]] = {}
    for source, mirror in edges:
        if source in changed_set:
            by_source.setdefault(source, []).append(mirror)
    gaps: list[MirrorGap] = []
    for source, mirrors in by_source.items():
        missing = tuple(mirror for mirror in mirrors if mirror not in changed_set)
        if not missing:
            continue
        if len(missing) == len(mirrors):
            gaps.append(MirrorGap(_classify(source), _domain_of(missing[0], rows), source, missing))
        else:
            domain = _domain_of(missing[0], rows)
            gaps.append(MirrorGap("mirror_partial", domain, source, missing))
    return gaps


def host_mapping_gaps(
    mappings: Sequence[tuple[str, Sequence[str]]],
    changed: Sequence[str],
) -> list[MirrorGap]:
    """宿主配置「真源 → 镜像」映射的缺失镜像判定（B1 判据的宿主泛化入口）。

    ``mappings`` 为 ``(source, mirrors)`` 序列：source 在 ``changed`` 内而 mirrors 未同批出现
    即命中。判定本体复用 :func:`hard_gaps`（``rows`` 传空 ⇒ ``domain`` 回落「生成物新鲜度」、
    ``klass`` 走 :func:`_classify`，属硬触发类），因此宿主侧与内建 drift closure 不会出现两套
    收敛口径；宿主映射来自 ``.orchd/shared/guards.json``，**不在此新增第二张硬编码映射表**。
    """
    edges = [(source, mirror) for source, mirrors in mappings for mirror in mirrors]
    return hard_gaps((), edges, changed)


def co_change_gaps(
    rows: Sequence[MirrorRow],
    changed: Sequence[str],
    reported_mirrors: Iterable[str] = (),
    *,
    semantic_changed: Iterable[str] | None = None,
) -> list[MirrorGap]:
    """软类缺口：§6 同一「生成器-产物组」只动了真源、镜像没跟上（warn，不阻断）。

    只对分类为**生成/保留**的行生效（派生不落盘、删除已结束）；逐组判定，组内首元素是真源/触发面：
    真源在改动集、镜像缺席 ⇒ 一条提示；**单向**——只有镜像侧改动不反向提示真源（改镜像不蕴含
    「应同步真源」，反向只会是噪音）；单路径组（无镜像副本的触发面，如 ``ci.yml`` 的注释性维护）
    不提示；``reported_mirrors`` 已由硬类报过的不重复报；``semantic_changed`` 非 ``None`` 时按它
    过滤改动集（纯注释/空白改动不算真源改动）。含生成物（``docs/_generated``）的组只在**触发面是
    生成物**时提示（手工改生成物 = 最典型的漏改真源）。
    """
    changed_set = set(changed)
    if semantic_changed is not None:
        changed_set &= set(semantic_changed)
    reported = set(reported_mirrors)
    gaps: list[MirrorGap] = []
    for row in rows:
        if row.category not in ("生成", "保留"):
            continue
        if not row.groups:
            # 旧口径（无分组信息）：整行一组、对称——任一落点改动即提示其余缺席落点。
            touched = [p for p in row.paths if p in changed_set]
            missing = [p for p in row.paths if p not in changed_set]
            if not touched or not missing:
                continue
            if all(p in reported for p in missing):
                continue
            if any(p.startswith(GENERATED_DIR) for p in row.paths) and not any(
                p.startswith(GENERATED_DIR) for p in touched
            ):
                continue
            gaps.append(MirrorGap("mirror", row.domain, touched[0], tuple(missing)))
            continue
        for group in row.groups:
            if len(group) < 2:
                continue  # 单路径组：无镜像副本的触发面，不参与共变提示
            trigger, *mirrors = group
            if trigger not in changed_set:
                continue  # 单向：镜像侧单独改动不反向提示真源
            if any(p.startswith(GENERATED_DIR) for p in group) and not trigger.startswith(
                GENERATED_DIR
            ):
                continue
            missing = tuple(p for p in mirrors if p not in changed_set and p not in reported)
            if missing:
                gaps.append(MirrorGap("mirror", row.domain, trigger, missing))
    return gaps


#: 行注释语法表（按扩展名）；未列出的扩展名没有可证的注释语法 ⇒ 保守按「非空行即语义」。
_COMMENT_PREFIXES: dict[str, tuple[str, ...]] = {
    ".py": ("#",),
    ".sh": ("#",),
    ".bash": ("#",),
    ".yml": ("#",),
    ".yaml": ("#",),
    ".toml": ("#",),
    ".cfg": ("#",),
    ".ini": ("#",),
    ".ps1": ("#",),
    ".mk": ("#",),
}
_GIT_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def semantic_paths_from_patch(patch: str) -> frozenset[str]:
    """unified diff → 「有语义改动」的仓库相对路径集合（纯注释/空白改动**不计入**）。

    判定口径（保守优先「算语义」——宁可提示，不可把真改动静默掉）：

    * 每个 hunk 内把 ``-`` / ``+`` 行做「去首尾空白 → 丢空行 → 丢纯注释行」归一后比较；
      两侧相同 ⇒ 只是空白/注释重排，不计入语义改动；
    * 未知扩展名没有可证的注释语法 ⇒ 非空行一律算语义改动（不猜）；
    * 新增/删除文件：一侧为空、另一侧有非注释内容 ⇒ 语义改动。

    纯函数：不读盘、不起子进程；调用方把 ``git diff -U0``（含未提交改动）的文本传进来即可。
    每个 hunk **独立**归一比较（跨 hunk 移动同一行仍算语义改动）。done 链路经
    ``entry_guard._drift_patch`` 取本任务 ``git diff -U0`` 文本注入（:func:`mirror_gaps` 的
    ``patch=``），也可由调用方直接传 ``semantic_changed=``。
    """
    semantic: set[str] = set()
    path: str | None = None
    file_semantic = False
    in_hunk = False
    removed: list[str] = []
    added: list[str] = []

    def flush_hunk() -> None:
        nonlocal file_semantic, removed, added
        if path is not None and not file_semantic and (removed or added):
            if _hunk_is_semantic(path, removed, added):
                file_semantic = True
        removed, added = [], []

    def flush_file() -> None:
        nonlocal path, file_semantic, in_hunk
        flush_hunk()
        if path is not None and file_semantic:
            semantic.add(path)
        path, file_semantic, in_hunk = None, False, False

    for raw in (patch or "").splitlines():
        if raw.startswith("diff --git "):
            flush_file()
            match = _GIT_DIFF_HEADER_RE.match(raw)
            path = _diff_path(match.group("b")) if match else None
            continue
        if raw.startswith("@@"):
            # 每个 hunk 独立归一比较：先结算上一个 hunk。否则同文件「hunk1 删一行、hunk2 原样
            # 加回」会被累加成同一对增删，从而把跨 hunk 移动行误判成非语义改动（漏报）。
            # 注意必须先于 ``in_hunk`` 分支判断：hunk 内的下一段 ``@@`` 也走这里结算。
            flush_hunk()
            in_hunk = True
            continue
        if in_hunk:  # hunk 内只看增删行（含以 ``--``/``++`` 开头的行，不能误当文件头）
            if raw.startswith("\\"):  # "\ No newline at end of file"
                continue
            if raw.startswith("-"):
                removed.append(raw[1:])
            elif raw.startswith("+"):
                added.append(raw[1:])
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target.startswith("b/"):
                path = _diff_path(target[2:])
        elif raw.startswith("--- "):
            target = raw[4:].strip()
            if path is None and target.startswith("a/"):
                path = _diff_path(target[2:])
    flush_file()
    return frozenset(semantic)


def _diff_path(raw: str) -> str:
    """diff 头里的路径 → 仓库相对 POSIX 路径（去引号 / 反斜杠归一）。"""
    path = raw.strip()
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    return path.replace("\\", "/")


def _hunk_is_semantic(path: str, removed: Sequence[str], added: Sequence[str]) -> bool:
    """一个 hunk 的增删行是否构成语义改动（注释/空白归一后不等才算）。"""
    prefixes = _COMMENT_PREFIXES.get(Path(path).suffix.lower(), ())
    return _semantic_lines(removed, prefixes) != _semantic_lines(added, prefixes)


def _semantic_lines(lines: Sequence[str], comment_prefixes: Sequence[str]) -> list[str]:
    """去首尾空白、丢空行、丢注释后的有效行（注释/空白等价判定用）。

    注释剥离分两步：整行注释直接丢；行尾注释按「标记在行首或前面是空白」剥离，**引号未闭合时不剥**
    （避免把字符串里的 ``#`` 当注释，从而把真实的字符串改动误判成非语义）。
    """
    out: list[str] = []
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if comment_prefixes:
            text = _strip_trailing_comment(text, comment_prefixes)
            if not text:
                continue
        out.append(text)
    return out


def _strip_trailing_comment(text: str, comment_prefixes: Sequence[str]) -> str:
    """剥掉行尾注释（``#`` 类标记）；引号奇偶不平衡时按代码处理（保守「算语义」）。"""
    for prefix in comment_prefixes:
        idx = text.find(prefix)
        while idx > 0 and not text[idx - 1].isspace():
            idx = text.find(prefix, idx + 1)
        if idx < 0:
            continue
        head = text[:idx]
        if head.count('"') % 2 or head.count("'") % 2:
            return text
        return head.rstrip()
    return text


def census_gaps(
    changed: Sequence[str],
    *,
    baseline_files: Iterable[str] | None,
    exists: Any,
    registered_top: Iterable[str] | None,
) -> list[MirrorGap]:
    """census 类缺口：棘轮根内新增/删除 ``.py``、未登记的 ``orchd/*.py`` 顶层模块。

    ``baseline_files`` / ``registered_top`` 为 ``None`` 表示对应**映射源不可读** ⇒ 该类整体跳过
    （否则每个改动的 ``.py`` 都被误判成「新文件未收基线」/「未登记」，而 census 是硬类会假阻断）；
    ``exists`` 为注入谓词，便于纯函数测试。
    """
    base: set[str] = set(baseline_files or ())
    registered = set(registered_top or ())
    gaps: list[MirrorGap] = []
    for path in changed if baseline_files is not None else ():
        if not path.endswith(".py") or not path.startswith(CENSUS_RATCHET_ROOTS):
            continue
        if (path not in base and exists(path)) or (path in base and not exists(path)):
            gaps.append(MirrorGap("census", "文件体量普查", path, (CENSUS_BASELINE_REL,)))
    for path in changed if registered_top is not None else ():
        # 顶层单文件模块 = ``orchd/<单文件>.py``（更深层级由包级登记负责）
        top_level = path.startswith("orchd/") and "/" not in path[len("orchd/") :]
        if top_level and path.endswith(".py") and path not in registered and exists(path):
            gaps.append(MirrorGap("census", "子域完备性", path, (SUBDOMAIN_REGISTRY_REL,)))
    return gaps


def shared_entry_gaps(
    changed: Sequence[str],
    registry: Mapping[str, Sequence[str]] | None = None,
    skip_keys: Iterable[str] = (),
) -> list[MirrorGap]:
    """软类缺口：改了共享入口、登记测试未随改动集出现（warn，不阻断）。

    E039 只保证登记测试**被执行**，不保证它被**同步更新**；``skip_keys`` 里由 E039 判为覆盖缺口
    的 key 直接跳过（同一事实不重复报）。
    """
    from orchd.shared_entries import SHARED_ENTRY_TESTS, shared_entry_key_matches

    table = SHARED_ENTRY_TESTS if registry is None else registry
    skip = set(skip_keys)
    changed_list = list(changed)
    changed_set = set(changed_list)
    gaps: list[MirrorGap] = []
    for entry, tests in table.items():
        if entry in skip:
            continue
        if not any(shared_entry_key_matches(entry, path) for path in changed_list):
            continue
        missing = tuple(test for test in tests if test not in changed_set)
        if missing:
            gaps.append(MirrorGap("shared_entry", entry, entry, missing))
    return gaps


def _read_text(path: Path) -> str:
    """只读文本（缺失 / 不可读 / 非 UTF-8 ⇒ 空串，映射源据此静默跳过，不得让 done 裸崩）。"""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _load_baseline_files(project_root: Path) -> set[str] | None:
    """``files`` 键集合；**不可读 / 坏 JSON / 空表 ⇒ None**（调用方跳过该源，不报假阳性）。

    「空表」与「读不到」同处理：空集合在 :func:`census_gaps` 里语义反转成「所有改动的 orchd/scripts
    ``.py`` 都是新增文件」，而 census 属硬类 ⇒ hard 档会假阻断正常任务。
    """
    text = _read_text(project_root / CENSUS_BASELINE_REL)
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    files = data.get("files") if isinstance(data, dict) else None
    return set(files) if isinstance(files, dict) and files else None


def _registered_top_level() -> set[str] | None:
    """``subdomain_census.TOP_LEVEL_MODULE_REASONS`` 键集合；导入失败 ⇒ ``None``（该类跳过）。

    **不能返回空集**：``census_gaps`` 的判据是 ``path not in registered``，空集语义会反转成「所有
    改动过的顶层模块都未登记」，而 census 属硬触发四类 ⇒ hard 档假阻断 done。``None`` 由
    :func:`mapping_problems` 留痕。
    """
    try:
        from orchd.subdomain_census import TOP_LEVEL_MODULE_REASONS
    except Exception:  # noqa: BLE001 - 登记面不可读 ⇒ 该类跳过，不报假阳性
        return None
    return set(TOP_LEVEL_MODULE_REASONS) or None


def mirror_gaps(
    changed: Iterable[str],
    *,
    project_root: Path | str | None = None,
    skip_shared_keys: Iterable[str] = (),
    semantic_changed: Iterable[str] | None = None,
    patch: str | None = None,
) -> list[MirrorGap]:
    """按改动集算缺失镜像（三处映射源聚合；纯读，不写盘、不起子进程）。

    ``changed`` 是已算好的改动集（分支 diff ∪ 工作树改动）；``project_root`` 为 ``None`` 时只剩
    不依赖磁盘的共享入口类判定；``skip_shared_keys`` 是 E039 已判覆盖缺口的 key。返回硬类在前、
    软类在后的缺口列表；空列表 = 无缺口或无可用映射源。

    **语义过滤**（纯注释/空白改动不算真源改动）：``semantic_changed`` 传已算好的语义改动集，
    或 ``patch`` 传 unified diff 文本由 :func:`semantic_paths_from_patch` 现算（二者取其一，
    ``semantic_changed`` 优先）；都不传 = 旧口径（只判路径集合，向后兼容）。过滤只作用于硬类与
    共变类（``census`` / ``cli_surface`` 是「新增文件 / 注册面」判定，不看内容等价）。
    """
    # 统一成仓库相对 POSIX 路径（去 ``./``、去重保序）
    normalized = [str(p).replace("\\", "/").strip().removeprefix("./") for p in changed]
    paths = [p for p in dict.fromkeys(normalized) if p]
    if not paths:
        return []
    semantic: set[str] | None = None
    if semantic_changed is not None:
        semantic = {str(p).replace("\\", "/").strip().removeprefix("./") for p in semantic_changed}
    elif patch:
        semantic = set(semantic_paths_from_patch(patch))
    root = Path(project_root) if project_root else None
    rows: list[MirrorRow] = (
        parse_mirror_table(_read_text(root / DESIGN_REL)) if root is not None else []
    )
    # 生成物映射改为读 orchd.docs_manifest（单一真源）；§6 行只保留非生成物共变判定。
    edges: list[tuple[str, str]] = _manifest_edges(root)
    for edge in _cell_edges(rows):
        if edge not in edges:
            edges.append(edge)
    gaps = hard_gaps(rows, edges, paths, semantic_changed=semantic)
    gaps.extend(
        co_change_gaps(
            rows,
            paths,
            reported_mirrors=[m for g in gaps for m in g.mirrors],
            semantic_changed=semantic,
        )
    )
    if root is not None:  # 两个映射源的「不可读」由 mapping_problems 留痕，这里只管判定
        gaps.extend(
            census_gaps(
                paths,
                baseline_files=_load_baseline_files(root),
                exists=lambda rel: (root / rel).is_file(),
                registered_top=_registered_top_level(),
            )
        )
    gaps.extend(cli_surface_gaps(paths, rows, root))
    gaps.extend(shared_entry_gaps(paths, skip_keys=skip_shared_keys))
    return gaps


def cli_surface_gaps(
    changed: Sequence[str],
    rows: Sequence[MirrorRow] = (),
    project_root: Path | str | None = None,
) -> list[MirrorGap]:
    """软类缺口：``orchd/cli/commands/**`` 里出现 ``add_parser`` 的模块改动后提示确认 commands.md。

    **只软不硬**：``gen_commands_md`` 只输出 ``parser._actions`` 的顶层 ``choices``
    （commands.md 就是 23 个顶层命令名），命令子模块注册的是二级子命令、不进该生成物 ⇒ 改子模块的
    实现/help/嵌套子命令时 commands.md 内容不变，判硬类会把日常改动全部误阻断（§6「命令面」的触发
    真源也只写了 parser.py）。
    仍保留软提示的理由：新增/重命名顶层命令必须先改 parser.py 再 regen，子面改动是最常见的「顺手忘了
    另一头」入口。三条收敛：① 判定面收窄到「文件内出现 ``add_parser``」；② **commands.md 已在改动集
    ⇒ 静默**（否则「已 regen」也算缺口 = 假阳性）；③ 每任务一条（多模块并列进 source），否则 warn 档
    刷 N 条淹掉真信号；无 ``project_root`` 时读不到内容 ⇒ 不判定（不猜）。
    """
    if project_root is None:
        return []
    root = Path(project_root)
    mirror = GENERATED_DIR + "commands.md"
    if mirror in set(changed):
        return []
    modules = [
        path
        for path in changed
        if path.startswith(CLI_COMMANDS_DIR)
        and path.endswith(".py")
        and "add_parser" in _read_text(root / path)
    ]
    if not modules:
        return []
    return [MirrorGap("cli_surface", _domain_of(mirror, rows), ", ".join(modules), (mirror,))]


def mapping_problems(project_root: Path | str | None) -> list[str]:
    """映射源健康度（空列表 = 健康，done 期记 degraded）：§6 多一列（``MIRROR_COLUMNS`` 不符 ⇒
    整表丢弃）、``tests`` 不可导入、``GENERATORS`` 变更、普查基线不可读，都会让对应类别
    **静默恒绿**或**无痕关闭**，响应里看不出门禁没在守。
    """
    if not project_root:
        return []
    root = Path(project_root)
    design = _read_text(root / DESIGN_REL)
    rows = parse_mirror_table(design)
    problems: list[str] = []
    if not design:
        problems.append(f"§6 设计文档不可读：{DESIGN_REL}")
    elif _table_rows_impl() is None:
        problems.append("§6 解析器不可用（tests.test_docs 不可导入）")
    elif not rows:
        problems.append(f"§6 决策表解析出 0 行（表头/列数变更？）：{DESIGN_REL}")
    if not _manifest_edges(root):
        problems.append(
            f"生成器输出清单派生 0 条边（manifest 真源不可达？）：{MANIFEST_REL}"
        )
    if _load_baseline_files(root) is None:
        problems.append(f"体量普查基线不可读或为空（收基线判定本次关闭）：{CENSUS_BASELINE_REL}")
    if _registered_top_level() is None:
        problems.append(f"子域登记表不可导入（顶层登记判定本次关闭）：{SUBDOMAIN_REGISTRY_REL}")
    return problems


# ── 生成物新鲜度正交判据（B2，task-guard-drift-b2-generated-stale-gate）────────
#: 单次 done 评估的生成物比对成本上限（秒）：contract freshness 测试实测 ~0.7s，2s 为裕度。
GENERATED_STALE_BUDGET_S = 2.0


def _load_generators(project_root: Path):
    """读 manifest 登记的 GENERATORS（``orchd.docs_manifest`` → ``orchd.docsgen``）。

    返回 ``(module, problem)``：``module=None`` 时 ``problem`` 为降级原因文本。
    """
    try:
        from orchd import docs_manifest

        problems = docs_manifest.validate()
        if problems:
            return None, "；".join(problems)
        return docs_manifest.as_namespace(), None
    except Exception as exc:  # noqa: BLE001 - 清单/生成器不可读 ⇒ 降级留痕，不阻断 done
        return None, f"{type(exc).__name__}: {exc}"


def compare_generated_artifacts(
    pairs: Iterable[tuple[str, Callable[[], str]]],
    project_root: Path | None,
    *,
    budget_s: float = GENERATED_STALE_BUDGET_S,
    domain: str = "生成物新鲜度",
    source: str = GENERATOR_REL,
) -> tuple[list[str], list[dict[str, Any]], float]:
    """逐产物内容比对（引擎 B2 与宿主 ``generated`` guard 共用的**唯一判据**）。

    ``pairs`` 为 ``(artifact_rel, 生成函数)`` 序列——导入/定位生成器由调用方负责
    （引擎侧读 ``orchd.docsgen.GENERATORS``，宿主侧读
    ``.orchd/shared/guards.json`` 的 ``artifacts`` 清单），本函数只做
    「调函数 → 读磁盘 → 内容不等即过期」+ 降级留痕 + 成本预算。

    返回 ``(stale_artifacts, degradates, elapsed_s)``：

    * ``stale_artifacts``：内容不一致的产物路径（仓库相对）；
    * ``degradates``：生成器求值 / 磁盘读取失败、或总耗时超 ``budget_s`` 的留痕条目
      （超预算时 ``stale_artifacts`` 清空 = 全部降级 warn-only，不阻断）；
    * ``elapsed_s``：``time.perf_counter`` 实测总耗时。

    只读纪律：只读 ``pairs`` 声明的产物路径，不遍历目录树、不起子进程。
    """
    import time

    degradates: list[dict[str, Any]] = []
    if project_root is None:
        return [], degradates, 0.0
    start = time.perf_counter()
    stale: list[str] = []
    for artifact, generator in pairs:
        try:
            expected = generator()
            disk = (project_root / artifact).read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - 单产物故障降级留痕，不报假阳性
            degradates.append(
                {
                    "reason": "生成产物求值或磁盘读取失败，该产物本次跳过",
                    "artifact": artifact,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if expected != disk:
            stale.append(artifact)
    elapsed = time.perf_counter() - start
    if elapsed > budget_s:
        degradates.append(
            {
                "reason": f"{domain}评估超预算，全部命中降级为 warn-only",
                "budget_s": budget_s,
                "elapsed_s": round(elapsed, 3),
                "stale": [
                    {
                        "class": "generated",
                        "domain": domain,
                        "source": source,
                        "mirrors": [artifact],
                        "hard": True,
                    }
                    for artifact in stale
                ],
            }
        )
        return [], degradates, elapsed
    return stale, degradates, elapsed


def generated_stale_gaps(
    project_root: Path | None,
    budget_s: float = GENERATED_STALE_BUDGET_S,
) -> tuple[list[MirrorGap], list[dict[str, Any]], float]:
    """生成物新鲜度：逐产物比对 ``gen_*_md()`` 与磁盘内容（B2 正交判据）。

    B1 判「改了真源是否同批带了镜像」（改动集驱动，宽面真源被 ``_BROAD_OUTPUTS``
    排除且默认 warn）——漂移只能靠 contract 测试事后发现。本判据**不看改动集**，
    done 时直接内容比对：磁盘产物 ≠ 生成器输出即命中 ``generated_stale``，与 B1 正交。

    比对本体在 :func:`compare_generated_artifacts`（宿主 ``generated`` guard 复用同一判据）；
    本函数只负责解析引擎硬编码的 ``GENERATORS`` 清单并把命中映射成 :class:`MirrorGap`。

    返回 ``(gaps, degradates, elapsed_s)``：

    * ``gaps``：新鲜度命中（klass=``generated``，属硬触发类；warn 档仍只留痕）；
    * ``degradates``：留痕条目（生成器不可读 / 单产物求值或读取异常 /
      **成本超限**——超限时全部命中降级为 warn-only 并在此留痕，不阻断）；
    * ``elapsed_s``：``time.perf_counter`` 实测总耗时（调用方可断言预算）。

    只读纪律：仅读 ``GENERATORS`` 键对应的已知产物路径，不遍历目录树、不起子进程。
    """
    if project_root is None:
        return [], [], 0.0
    gen, problem = _load_generators(project_root)
    if gen is None:
        return (
            [],
            [{"reason": "生成器不可读，生成物新鲜度本次无法判定", "error": problem}],
            0.0,
        )
    pairs = [
        (f"{GENERATED_DIR}{filename}", func)
        for filename, func in sorted(getattr(gen, "GENERATORS", {}).items())
    ]
    stale, degradates, elapsed = compare_generated_artifacts(
        pairs, project_root, budget_s=budget_s
    )
    gaps = [
        MirrorGap("generated", "生成物新鲜度", GENERATOR_REL, (artifact,))
        for artifact in stale
    ]
    return gaps, degradates, elapsed
