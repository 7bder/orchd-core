"""orchd/guide/paths.py — 引导路由与路径域（W5-G1 拆分，纯搬移）。

来源：``orchd/guide.py`` 起点 e3f21d0b 的 L206-569（路径存在性过滤 / front-matter
解析 / 路由缓存与 read-template 反向生成 / read 版本标注）。拆分记录见
design/file-split-program-20261015.md §4.6.1。

依赖方向：仅标准库（os / pathlib / typing），不导入任何 orchd 内部模块——
聚合入口从本域重导出，故本域不得反向导入 ``orchd.guide``（否则成加载期环）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _resolve_paths(
    paths: list[str],
    base_dir: str | os.PathLike[str] | None,
) -> list[str]:
    """将 read/template 路径过滤为实际存在的文件路径（知识路由闭环）。

    路由闭环（task-guide-routing-loop）：guidance 的 read/template 字段由静态
    字符串路径升级为**可执行闭环**——agent 收到 guidance 后按 read 数组读规则
    文件、按 template 数组加载模板。为此引擎侧须保证字段指向实际存在的文件，
    否则 agent 按路径读取会落空。

    规则：
    - ``paths`` 中的每条解析候选位置：``base_dir`` 下及其**父目录**下（规则在
      ``.orchd/rules/``、模板在项目根 ``templates/``，故双候选兜底）；任一命中
      实际文件即保留，都不存在则剔除（best-effort，不抛异常）——剔除项由
      :func:`resolve_read_paths` 写 ``degraded`` 留痕，不再静默。
    - 绝对路径直接判定，不做拼接。
    - ``base_dir`` 为 None 或空 → 不做存在性校验，原样返回（纯函数 / 单测场景）。
    - 空数组 → 返回空数组（无害，向下兼容）。

    Args:
        paths: read 或 template 路径数组。
        base_dir: 规则/模板所在根目录（.orchd/）；None 时跳过校验。

    Returns:
        过滤后只含实际存在文件的路径数组（顺序保持）。
    """
    kept, _dropped = _split_paths(paths, base_dir)
    return kept


def _split_paths(
    paths: list[str],
    base_dir: str | os.PathLike[str] | None,
) -> tuple[list[str], list[str]]:
    """把 read/template 路径拆成 ``(存在的, 不存在的)``（存在性判定单一实现）。

    task-guide-step-vocab-and-routing-fix：原先「不存在即静默剔除」让路由漂移
    不可见（缺哪个规则文件无人知道）。拆出 dropped 供 :func:`resolve_read_paths`
    写 ``degraded`` 降级标记留痕（对齐 conflict_policy 的 degraded_guards 约定），
    同时保持 :func:`_resolve_paths` 的既有签名与语义不变。
    """
    if not paths or base_dir is None:
        return list(paths), []
    root = os.path.abspath(os.fspath(base_dir))
    parent = os.path.dirname(root)
    kept: list[str] = []
    dropped: list[str] = []
    for p in paths:
        if os.path.isabs(p):
            (kept if os.path.isfile(p) else dropped).append(p)
            continue
        candidates = (os.path.join(root, p), os.path.join(parent, p))
        (kept if any(os.path.isfile(c) for c in candidates) else dropped).append(p)
    return kept, dropped


def resolve_read_paths(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """将 guidance 的 read/template 字段解析为指向实际存在文件的路径（纯函数）。

    知识路由闭环的引擎侧接口（task-guide-routing-loop）：调用方（cli.py
    ``_attach_guidance`` 注入点）在附加 guidance 后透传 ``base_dir``，本函数
    返回**新增 ``read``/``template`` 键**的 guidance 副本，指向实际存在的规则/
    模板文件，不存在时剔除并写 ``degraded`` 留痕（不再静默）。
    空数组 / base_dir None 时原样返回（无害降级）。

    Args:
        guidance: 原始 guidance 字典（含 read/template 数组）。
        base_dir: 规则/模板根目录（.orchd/）；None 时跳过校验。

    Returns:
        过滤后的 guidance 字典（加法式，仅调整 read/template 两键，并在发生
        剔除时附加 ``degraded`` 标记；不碰 step/command/hint 结构；含
        agent_view/project_view 时递归过滤，保证双视角与顶层 read/template 一致）。
    """
    out = dict(guidance)
    read_kept, read_dropped = _split_paths(guidance.get("read") or [], base_dir)
    tpl_kept, tpl_dropped = _split_paths(guidance.get("template") or [], base_dir)
    out["read"] = read_kept
    out["template"] = tpl_kept
    dropped = sorted(set(read_dropped) | set(tpl_dropped))
    if dropped:
        # AC6 降级留痕：路径缺失不再静默——写 degraded 标记，由 slim 层并入 hint。
        out["degraded"] = {"kind": "read_paths_missing", "dropped": dropped}
    # 双视角（task-guidance-dual-view-engine）：递归过滤子视角的 read/template，
    # 避免顶层已过滤而 agent_view/project_view 仍指向不存在路径的不一致。
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = resolve_read_paths(sub, base_dir)
    return out


# ---------------------------------------------------------------------------
# 路由元驱动（task-guide-routing-meta / F5）：read/template 路由表反向生成。
#
# 此前各 builder 手写 read/template 字面量（~20 处），新增规则文件极易漏配
# （漏路由只能靠行为事故发现）。现路由知识收归各文件头部的 front-matter，
# builder 一律经 ``read_for`` / ``template_for`` 生成——新手写字面量即被
# AST 门禁（tests/test_guide_routing_meta.py）拦下，漏路由在结构上不可能发生。
#
# 作用域边界（刻意）：错误码域兜底表 ``_FALLBACK_READ_BY_DOMAIN`` 不在此列——
# 它按错误码域（verify/git/intake/session）而非 guidance 场景分发，与本机制
# 正交，保留手写（见模块 review 记录）。
# ---------------------------------------------------------------------------

#: 路由场景闭词汇（front-matter ``guide:`` 的合法键；未知键 fail-closed，防笔误）。
GUIDANCE_SITUATIONS = frozenset({
    "first_time", "empty_project", "stop_wait", "lesson_review", "check_status",
    "claim_review", "submit_review", "rework_first", "request_impl", "done",
    "wait_review", "optional_cancel", "done_all", "audit_merge",
    "claimed_impl", "done_submitted", "approved_completed", "cancelled",
    "changes_requested", "warning_unmapped",
})

#: 路由缓存：init_routing 填充；read_for/template_for 只读。
_ROUTING_CACHE: dict[str, Any] | None = None


def _parse_front_matter(path: Path) -> dict[str, Any] | None:
    """解析文件头部 front-matter（task-guide-routing-meta）。

    严格子集解析（标准库，无 yaml 依赖）：
    - 文件首行须为 ``---``，否则视为无 front-matter（返回空 dict）；
    - 块以首个 ``---`` / ``...`` 行结束；未闭合 → ValueError（点名文件与行号）；
    - 顶层键仅允许 ``guide:``（场景→优先级 int）与 ``guide_template:``
      （``roles: [...]`` / ``modes: [...]`` / ``order: int``）；未知键 → ValueError
      （防 ``giude:`` 类笔误静默失效）；
    - ``guide:`` 下未知场景键 / 非 int 优先级 → ValueError；
    - ``guide_template`` 的 roles/modes 须为 ``[a, b]`` 内联列表，order 须为 int；
      缺 roles → ValueError（模板不知给谁）；缺 modes 视为全 modes；缺 order
      视为 99。

    Returns:
        ``{"guide": {situation: order}, "guide_template": {...} | None}``；
        无块文件返回 None（调用方跳过；结构测试另行断言块存在性）。
        空块（``guide: {}`` 且无 ``guide_template:``）= 显式未路由。
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: front-matter 读取失败: {exc}")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        # 无块 = 未声明路由：运行时跳过该文件（best-effort；fixture 极简项目 /
        # legacy 宿主）。漏写由结构测试在合入前拦截（块存在性断言），运行时不炸。
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        raise ValueError(f"{path}: front-matter 未闭合（缺结束 ---）")
    guide: dict[str, int] = {}
    template: dict[str, Any] | None = None
    section: str | None = None
    for lineno, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            if stripped in ("guide:", "guide: {}"):
                section = "guide"
                continue
            if stripped == "guide_template:":
                section = "template"
                template = {"roles": None, "modes": None, "order": 99}
                continue
            raise ValueError(f"{path}:{lineno}: 未知顶层键 {stripped!r}")
        if section is None:
            raise ValueError(f"{path}:{lineno}: 缩进行游离（缺 guide:/guide_template:）")
        if section == "guide":
            key, sep, val = raw.strip().partition(":")
            if not sep:
                raise ValueError(f"{path}:{lineno}: 非法映射行 {raw.strip()!r}")
            key, val = key.strip(), val.strip()
            if key not in GUIDANCE_SITUATIONS:
                raise ValueError(f"{path}:{lineno}: 未知场景键 {key!r}")
            try:
                guide[key] = int(val)
            except ValueError:
                raise ValueError(
                    f"{path}:{lineno}: 优先级须为整数（{raw.strip()!r})")
        else:
            assert template is not None
            key, sep, val = raw.strip().partition(":")
            if not sep:
                raise ValueError(f"{path}:{lineno}: 非法映射行 {raw.strip()!r}")
            key, val = key.strip(), val.strip()
            if key in ("roles", "modes"):
                if not (val.startswith("[") and val.endswith("]")):
                    raise ValueError(
                        f"{path}:{lineno}: {key} 须为内联列表（如 [a, b])")
                template[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",")
                                 if v.strip()]
                if key == "roles" and not template[key]:
                    raise ValueError(f"{path}:{lineno}: roles 不可为空")
            elif key == "order":
                try:
                    template["order"] = int(val)
                except ValueError:
                    raise ValueError(
                        f"{path}:{lineno}: order 须为整数")
            else:
                raise ValueError(f"{path}:{lineno}: 未知模板键 {key!r}")
    if template is not None and not template.get("roles"):
        raise ValueError(f"{path}: guide_template 缺 roles（不知给谁）")
    return {"guide": guide, "guide_template": template}


def init_routing(
    orchd_dir: str | os.PathLike[str] | None,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """扫描 front-matter 并填充路由缓存（task-guide-routing-meta）。

    扫描面（输出路径写法与既有字面量逐字一致）：``<orchd>/rules/*.md`` →
    ``"rules/<name>"``；``<orchd>/shared/*.md`` → ``"shared/<name>"``；
    ``<orchd>/skill-lesson.md`` → ``"skill-lesson.md"``；
    ``<project>templates/*.md``（project_root 缺省为 orchd 之父）→
    ``"templates/<name>"``。

    - ``orchd_dir`` 为 None → 空降级缓存（bootstrap 无 .orchd 形态；read_for
      全返 []，与既有空列表输出一致）。
    - ``rules/`` 缺失 → 跳过（保留既有缓存；fixture 极简项目与 legacy 并存，
      不降级覆盖）。
    - 同目录重复初始化直接返回（幂等；review_submit 的防御性重入零开销）。

    Returns:
        ``{"situations": n, "templates": m}`` 摘要。
    """
    global _ROUTING_CACHE
    if orchd_dir is None:
        _ROUTING_CACHE = {"empty": True, "dirs": (), "situations": {},
                          "templates": []}
        return {"situations": 0, "templates": 0}
    orchd = Path(os.fspath(orchd_dir))
    proj = Path(os.fspath(project_root)) if project_root is not None else orchd.parent
    key = (str(orchd.resolve()) if orchd.exists() else str(orchd),
           str(proj.resolve()) if proj.exists() else str(proj))
    if _ROUTING_CACHE is not None and _ROUTING_CACHE.get("dirs") == key:
        cached = _ROUTING_CACHE
        return {"situations": len(cached.get("situations", {})),
                "templates": len(cached.get("templates", []))}
    rules_dir = orchd / "rules"
    if not rules_dir.is_dir():
        return {"situations": 0, "templates": 0}
    situations: dict[str, list[tuple[int, str]]] = {}
    templates: list[dict[str, Any]] = []
    for base, prefix in [(rules_dir, "rules"), (orchd / "shared", "shared")]:
        if not base.is_dir():
            continue
        for fp in sorted(base.glob("*.md")):
            meta = _parse_front_matter(fp)
            if meta is None:
                continue
            for situation, order in meta["guide"].items():
                situations.setdefault(situation, []).append(
                    (order, f"{prefix}/{fp.name}"))
    skill = orchd / "skill-lesson.md"
    if skill.is_file():
        meta = _parse_front_matter(skill)
        if meta is not None:
            for situation, order in meta["guide"].items():
                situations.setdefault(situation, []).append((order, "skill-lesson.md"))
    tpl_dir = proj / "templates"
    if tpl_dir.is_dir():
        for fp in sorted(tpl_dir.glob("*.md")):
            meta = _parse_front_matter(fp)
            if meta is None or meta["guide_template"] is None:
                continue
            templates.append({"rel": f"templates/{fp.name}",
                              **meta["guide_template"]})
    resolved = {s: [rel for _, rel in sorted(items)]
                for s, items in situations.items()}
    _ROUTING_CACHE = {"empty": False, "dirs": key, "situations": resolved,
                      "templates": templates}
    return {"situations": len(resolved), "templates": len(templates)}


def _require_routing() -> dict[str, Any]:
    """取已初始化缓存；未初始化 → 结构化失败（fail-closed，防静默手写回退）。"""
    if _ROUTING_CACHE is None:
        raise RuntimeError(
            "guide 路由未初始化：须先 init_routing(orchd_dir) "
            "（CLI 入口自动执行；库调用/测试须显式初始化）")
    return _ROUTING_CACHE


def read_for(situation: str) -> list[str]:
    """反向生成某场景的 read 列表（task-guide-routing-meta）。

    按 front-matter 优先级排序；未知场景 → []；空降级缓存 → []。
    """
    cache = _require_routing()
    if cache.get("empty"):
        return []
    return list(cache["situations"].get(situation, []))


def template_for(role: str, mode: str | None = None) -> list[str]:
    """反向生成某角色/模式的 template 列表（task-guide-routing-meta）。

    roles 必含 role；modes 缺省视为全 modes；按 (order, 路径) 排序。
    空降级缓存 → []。
    """
    cache = _require_routing()
    if cache.get("empty"):
        return []
    out = [
        t for t in cache.get("templates", [])
        if role in (t.get("roles") or [])
        and (not t.get("modes") or mode in (t.get("modes") or []))
    ]
    return [t["rel"] for t in sorted(out, key=lambda t: (t.get("order", 99), t["rel"]))]


def attach_read_versions(
    guidance: dict[str, Any],
    base_dir: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """为 guidance 的 read[] 每条路径附加 {mtime, size} 版本标注（纯函数，加法式）。

    task-guidance-read-versions：配合读取纪律，agent 依据版本标注判断规则/模板
    文件是否变化，未变则不重读。本函数**只读 stat，不读文件内容**。

    - best-effort：文件缺失 / stat 异常跳过该条（不抛异常，不阻塞）。
    - 加法式：新增顶层键 ``read_versions``（dict[path, {mtime, size}]），
      ``read[]`` 保持字符串数组契约不变。
    - 双视角递归：agent_view / project_view 若为 dict 则同样附加。
    - 空 read / base_dir None 时 read_versions={}（无害降级）。

    Args:
        guidance: 已通过 resolve_read_paths 过滤的 guidance 字典。
        base_dir: 规则/模板根目录（.orchd/）；None 时跳过 stat。

    Returns:
        新增 read_versions 键的 guidance 副本（不修改入参）。
    """
    out = dict(guidance)
    versions: dict[str, dict[str, float | int]] = {}
    if base_dir is not None:
        base = Path(base_dir)
        for rel in guidance.get("read") or []:
            if not isinstance(rel, str):
                continue
            try:
                st = (base / rel).stat()
                versions[rel] = {"mtime": st.st_mtime, "size": st.st_size}
            except (OSError, ValueError):
                continue  # best-effort：缺失/异常跳过该条
    out["read_versions"] = versions
    # 双视角递归
    for key in ("agent_view", "project_view"):
        sub = guidance.get(key)
        if isinstance(sub, dict):
            out[key] = attach_read_versions(sub, base_dir)
    return out
