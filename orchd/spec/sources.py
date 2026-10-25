"""orchd/spec/sources.py — 任务来源与引用完整性校验域（W4-S3 拆分，纯搬移）。

来源：``orchd/spec/__init__.py``（起点 204aaa5a）的 L777-1102：validate_source /
额外来源 / idea 与 roadmap 引用
拆分记录见 design/file-split-program-20261015.md §4.5.3。

依赖方向：sources.py → errors.py / spec.structure / contracts 包。
"""

from pathlib import Path
from typing import Any

from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import ErrorCode
from orchd.spec.structure import Master, ValidationError


def _validate_additional_sources(
    t: dict[str, Any],
    i: int,
    tid: str,
    workspace_root: Path,
    project_root: Path,
    terminal_ids: frozenset[str] = frozenset(),
) -> list["ValidationError"]:
    """additional_sources 独立遍历（task-additional-sources-standalone-validation）。

    与主 ``source`` 共享同一检查函数（``_check_idea_reference`` /
    ``_check_roadmap_reference``）与错误路径语义（``path`` 前缀仍为
    ``$.tasks[i].additional_sources[j]``、错误码 E025、消息口径与 source 一致）。

    独立性：由调用方置于主 source 短路（无 source / 非 str / 终态任务 /
    source 格式非法）**之前**执行，故四类短路路径下附加引用仍被逐条校验。
    本函数自身不做任何短路（含终态任务亦校验）。

    终态豁免（task-additional-sources-terminal-exempt）：``tid`` 在终态集合时，
    跳过**存在性**检查（条目在场 + pending / 章节头命中）——归档机制会把已完结
    条目搬走，存在性失败属正常生命周期而非引用非法（与主 source 的 P2-2 豁免
    同理）。类型 / 格式检查恒执行（新卡防丢与补挂卫生不受影响）；工作区文件
    缺失照常报错（环境问题，非归档所致）。终态补挂新引用仅验格式（已知窄边界，
    见 review 记录）。
    """
    import re as _re

    # spec.py 依赖方向为 errors.py，此处惰性导入 ledger 纯路径 helper
    # （与 validate_source 同模式，无循环依赖：ledger 不导入 spec）。
    from orchd.ledger import resolve_roadmap_path

    terminal = tid in (terminal_ids or frozenset())

    errors: list["ValidationError"] = []
    additional = t.get("additional_sources")
    if additional and isinstance(additional, list):
        for j, asrc in enumerate(additional):
            if not isinstance(asrc, str):
                errors.append(ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].additional_sources[{j}]",
                    message=f"task '{tid}' additional_sources[{j}] 非字符串",
                ))
                continue
            if not _re.fullmatch(r"(idea|roadmap|debug):[a-z0-9-]+", asrc):
                errors.append(ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].additional_sources[{j}]",
                    message=(f"task '{tid}' additional_sources[{j}] '{asrc}' 格式非法"
                             "（须 ^(idea|roadmap|debug):[a-z0-9-]+$）"),
                ))
                continue
            aprefix, _, aref = asrc.partition(":")
            aref = aref.strip()
            base_path = f"$.tasks[{i}].additional_sources[{j}]"
            if aprefix == "idea":
                ideas_path = workspace_root / "IDEAS.md"
                if not ideas_path.exists():
                    errors.append(ValidationError(
                        code=ErrorCode.E025,
                        path=base_path,
                        message=f"task '{tid}' additional_sources 引用 IDEAS.md 但文件缺失",
                    ))
                    continue
                if terminal:
                    continue
                for se in _check_idea_reference(tid, i, aref, ideas_path):
                    se.path = base_path
                    errors.append(se)
            elif aprefix == "roadmap":
                rpath = resolve_roadmap_path(project_root)
                if not rpath.exists():
                    errors.append(ValidationError(
                        code=ErrorCode.E025,
                        path=base_path,
                        message=f"task '{tid}' additional_sources 引用 ROADMAP.md 但文件缺失",
                    ))
                    continue
                if terminal:
                    continue
                for se in _check_roadmap_reference(tid, i, aref, rpath):
                    se.path = base_path
                    errors.append(se)
            # debug: 前缀无文件引用校验，与 source 一致
    return errors


def validate_source(
    master: Master,
    project_root: Path | None = None,
) -> list[ValidationError]:
    """source 字段溯源校验（E025，加法式：不改变 validate_structure/references 行为）。

    task 可选 ``source`` 字段（``^(idea|roadmap|debug):[a-z0-9-]+$``）声明任务来源：
    - ``idea:<id>``：引用 IDEAS.md 中 ``- id: <id>`` **精确匹配**的条目，且该条目
      ``status: pending``（已 covered/完成的条目不可作为新任务来源；
      日期词/标题词 ref 因无对应条目 id 必然被拒）；
    - ``roadmap:<id>``：引用 ROADMAP.md 中 ``## 版本`` 章节头包含的规划 id。

    校验规则：
    - 无 source 字段的任务直接通过（向后兼容存量）。
    - source 格式非法（不匹配正则）→ E025。
    - 对应文件（IDEAS.md / ROADMAP.md）缺失 → E025（文件缺失即无法溯源）。
    - idea 引用条目不存在或 status 非 pending → E025。
    - roadmap 引用章节头不包含 id → E025。

    Args:
        master: 已加载的 Master 对象。
        project_root: 项目根目录（默认取 master.source_path 的上级上级，
            即 .orchd/ 的父目录）。为 None 时基于 source_path 推导。

    Returns:
        ValidationError 列表（E025）；合法时返回空列表。
    """
    errors: list[ValidationError] = []
    tasks = master.tasks

    if project_root is None:
        project_root = master.source_path.parent.parent
    project_root = Path(project_root)

    # AC3（task-12-engine-path-abstraction）：IDEAS.md / ROADMAP.md 走统一工作区根
    # helper（默认 .orchd/，兼容旧根路径）。spec.py 依赖方向为 errors.py，此处
    # 采用函数内惰性导入 ledger 的纯路径 helper（无循环依赖：ledger 不导入 spec）。
    from orchd.ledger import resolve_roadmap_path, resolve_workspace_root
    workspace_root = resolve_workspace_root(project_root)

    # P2-2（2026-08-19 审查）：对终态任务（completed/cancelled）豁免 source 校验。
    # ideas-archive 归档机制会把已完结的 IDEAS 条目移入 IDEAS-archive.md，终态任务的
    # source 条目必然已被归档，全量调用（未来巡检/接入）不应报 E025 误报。
    # 惰性加载 ledger 状态；拿不到（无 ledger / replay 异常）时保持全量校验（不豁免）。
    terminal_ids: frozenset[str] = frozenset()
    try:
        from orchd.ledger import Store
        store = Store(workspace_root)
        _terminal: set[str] = set()
        for _tid, _ts in store.replay().items():
            if _ts.status in TERMINAL_STATUSES:
                _terminal.add(_tid)
        terminal_ids = frozenset(_terminal)
    except Exception:
        terminal_ids = frozenset()

    for i, t in enumerate(tasks):
        tid = t.get("id", "")
        # task-additional-sources-standalone-validation：附加引用独立遍历——
        # 先于主 source 的全部短路（无 source / 非 str / 终态 / 格式非法）执行，
        # 四类路径下仍逐条校验（E025 形同虚设的旁路消除）。终态集合透传供存在性
        # 豁免（归档搬走的存量引用，见本函数 docstring）。
        errors.extend(
            _validate_additional_sources(t, i, tid, workspace_root, project_root,
                                         terminal_ids))
        source = t.get("source")
        if not source or not isinstance(source, str):
            continue
        if tid in terminal_ids:
            # P2-2：终态任务（completed/cancelled）已关闭，来源条目归档属正常生命周期
            continue

        prefix, _, ref_id = source.partition(":")
        ref_id = ref_id.strip()

        # 格式校验（正则已在 schema 层，但 validate_source 独立可调用时也要保证）
        import re as _re
        if not _re.fullmatch(r"(idea|roadmap|debug):[a-z0-9-]+", source):
            errors.append(
                ValidationError(
                    code=ErrorCode.E025,
                    path=f"$.tasks[{i}].source",
                    message=(f"task '{tid}' source '{source}' 格式非法"
                             "（须 ^(idea|roadmap|debug):[a-z0-9-]+$）"),
                ))
            continue

        if prefix == "idea":
            ideas_path = workspace_root / "IDEAS.md"
            if not ideas_path.exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E025,
                        path=f"$.tasks[{i}].source",
                        message=f"task '{tid}' 引用 IDEAS.md 但文件缺失（无法溯源）",
                    ))
                continue
            errors.extend(_check_idea_reference(tid, i, ref_id, ideas_path))
        elif prefix == "roadmap":
            # task-roadmap-root-resolution：ROADMAP 走独立定位（宿主根优先、
            # .orchd/ 回退），与 roadmap-land / intake 同一份文件——此前由工作区
            # 文档根拼出（.orchd/ 下存在 IDEAS/SKILL 即锁定 .orchd/），与 validate
            # 系 _find_workspace_file 的根回退定位分裂。
            roadmap_path = resolve_roadmap_path(project_root)
            if not roadmap_path.exists():
                errors.append(
                    ValidationError(
                        code=ErrorCode.E025,
                        path=f"$.tasks[{i}].source",
                        message=f"task '{tid}' 引用 ROADMAP.md 但文件缺失（无法溯源）",
                    ))
                continue
            errors.extend(
                _check_roadmap_reference(tid, i, ref_id, roadmap_path))
        # debug: 前缀标记外部来源手工注册的任务，不校验文件引用

    return errors


def _exact_ref_match(ref_id: str, title: str) -> bool:
    """ref_id 是否为 title 中的一个完整词（子串/前缀误命中防护，P3 2026-08-13）。

    匹配规则：ref_id 在标题中出现，且前后边界均为「非字母数字/连字符/下划线」
    （开头/结尾视为合法边界），避免 ``2026-08-1`` 误命中 ``2026-08-10``。
    支持 ref_id 出现在标题任意位置（roadmap id 形如 ``id: snapshotstore-m-p0``）。
    """
    start = 0
    while True:
        pos = title.find(ref_id, start)
        if pos == -1:
            return False
        before_ok = pos == 0 or (not title[pos - 1].isalnum()
                                 and title[pos - 1] not in ("-", "_"))
        after = pos + len(ref_id)
        after_ok = after == len(title) or (not title[after].isalnum()
                                           and title[after] not in ("-", "_"))
        if before_ok and after_ok:
            return True
        start = pos + 1


def _check_idea_reference(tid: str, task_idx: int, ref_id: str,
                          ideas_path: Path) -> list[ValidationError]:
    """核对 IDEAS.md：存在 ``- id: <ref_id>`` 精确匹配的条目且 status 为 pending。

    ideas-archive-exact-match（2026-08-22）：idea ref 只匹配条目 ``- id: == ref``
    （精确相等），废除标题完整词匹配——日期词 ref（如 ``idea:2026-08-22``）因无
    对应条目 id 必然被拒，从数据模型杜绝同日条目标题词误命中。

    idea-write-gate（2026-08-15）：status 仅 ``pending`` 可作为任务来源；``study``
    （论证中，idea propose 写入）不可作为任务来源——须先 confirm 升 pending 才能引用。
    """
    errors: list[ValidationError] = []
    text = ideas_path.read_text(encoding="utf-8")
    # 解析条目：## 标题行 + 后续行中的 status / id 字段（支持列表与裸两种格式）
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current:
                entries.append(current)
            current = {"title": stripped[3:].strip(), "status": "", "id": ""}
        elif current is not None:
            # 匹配 "- status: pending" 或 "status: pending"
            for marker in ("- status:", "status:"):
                if stripped.startswith(marker):
                    current["status"] = stripped[len(marker):].strip()
                    break
            # 匹配 "- id: <slug>" 或 "id: <slug>"（显式 id 强约束锚点）
            for marker in ("- id:", "id:"):
                if stripped.startswith(marker):
                    current["id"] = stripped[len(marker):].strip()
                    break
    if current:
        entries.append(current)

    # ideas-archive-exact-match：只按条目 `- id:` 精确匹配（废除标题完整词匹配）
    matched = next(
        (e for e in entries if (e.get("id") or "").strip() == ref_id),
        None,
    )
    if matched is None:
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=
                f"task '{tid}' 引用 idea '{ref_id}' 但 IDEAS.md 中无匹配条目（- id: == {ref_id}）",
            ))
        return errors
    if matched["status"] != "pending":
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=
                (f"task '{tid}' 引用 idea '{ref_id}' 但该条目 status='{matched['status']}'"
                 "（须为 pending 才能作为新任务来源）"),
            ))
    return errors


def _roadmap_section_headers(text: str) -> list[str]:
    """提取 ROADMAP.md 章节头（单一来源，roadmap-ref 校验与 roadmap-land 定位共用）。

    识别 ``## `` 与 ``### `` 两级标题（ROADMAP 自结构重构后版本章节位于
    ``## 近期规划`` / ``## 远期规划`` / ``## 派生分支`` 之下、改用 ``### `` 层级，
    如 ``### 1.4.5 · 架构演进（id: arch-evolution-145）``）；header 剥掉全部
    前导 ``#`` 与空格。两份 ROADMAP 解析（_check_roadmap_reference /
    _parse_roadmap_sections）必须都经本函数取 header，禁止各自实现解析
    （对齐 shared/conventions.md:118「同一事实禁止两份解析」教训）。
    """
    headers: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            headers.append(stripped.lstrip("#").strip())
    return headers


def _check_roadmap_reference(tid: str, task_idx: int, ref_id: str,
                             roadmap_path: Path) -> list[ValidationError]:
    """核对 ROADMAP.md：存在 ``## / ### 版本`` 章节头且包含引用 id。"""
    errors: list[ValidationError] = []
    text = roadmap_path.read_text(encoding="utf-8")
    section_headers = _roadmap_section_headers(text)
    # P3（2026-08-13 full-audit-v2）：精确匹配（完整词），避免前缀误命中
    matched = any(
        _exact_ref_match(ref_id, header) for header in section_headers)
    if not matched:
        errors.append(
            ValidationError(
                code=ErrorCode.E025,
                path=f"$.tasks[{task_idx}].source",
                message=(f"task '{tid}' 引用 roadmap '{ref_id}' 但 ROADMAP.md 的"
                         "## / ### 版本 章节头均不包含该 id"),
            ))
    return errors
