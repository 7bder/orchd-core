"""orchd/spec/roadmap.py — ROADMAP 章节解析与落地/布局告警域（W4-S3 拆分，纯搬移）。

来源：``orchd/spec/__init__.py``（起点 204aaa5a）的 L1105-1261：_parse_roadmap_sections /
roadmap_landing_warnings / _e031_warning / layout_marker_warnings
拆分记录见 design/file-split-program-20261015.md §4.5.3。

依赖方向：roadmap.py → spec.sources（复用 _roadmap_section_headers 单一解析原语）。
"""

from pathlib import Path
from typing import Any

from orchd.spec.sources import _roadmap_section_headers


def _parse_roadmap_sections(text: str) -> list[dict[str, Any]]:
    """解析 ROADMAP.md 的 ``## / ### 版本 · 标题（id: xxx）`` 章节头（roadmap-land / validate 兜底复用）。

    Returns:
        [{version, header, id, historical}]：version 为章节头首个词（如 ``1.3``）；
        id 为 ``id: xxx`` 内的 id（无则 None）；historical 为标题含"历史"。
    """
    import re as _re

    sections: list[dict[str, Any]] = []
    for header in _roadmap_section_headers(text):
        tokens = header.split()
        version = tokens[0] if tokens else ""
        id_m = _re.search(r"id:\s*([\w-]+)", header)
        sections.append({
            "version": version,
            "header": header,
            "id": id_m.group(1) if id_m else None,
            "historical": "历史" in header,
        })
    return sections


def _find_workspace_file(orchd_dir: Path, name: str) -> Path | None:
    """在 .orchd 布局先、根布局次的顺序定位 IDEAS/ROADMAP（不依赖 ledger，spec 保持零依赖）。"""
    in_orchd = orchd_dir / name
    if in_orchd.is_file():
        return in_orchd
    root = orchd_dir.parent / name
    return root if root.is_file() else None


def roadmap_landing_warnings(orchd_dir: Path) -> list[dict[str, Any]]:
    """validate 落地兜底（intake-dual-path）：带 id 且非历史的规划章节须有 IDEAS 落地条目。

    IDEAS 落地判据：IDEAS.md **或 IDEAS-archive.md** 存在引用该章节的条目
    （detail 含 ``§版本``）——规划章节必须曾进入执行层，或被显式标记历史；否则提醒。缺失 → warning
    （不判 invalid，对齐 E022/E023/E024 质量告警语义）。ROADMAP.md 缺失时返回空（跳过）。
    """
    # task-roadmap-root-resolution：ROADMAP 定位与其余工作区文档解耦——统一走
    # resolve_roadmap_path（宿主根优先、.orchd/ 回退），与 roadmap-land / E025
    # 溯源定位到**同一份文件**（IDEAS / IDEAS-archive 仍走 _find_workspace_file）。
    # 入参形态兼容：orchd_dir 名为 .orchd 时，项目根为其父级。
    from orchd.ledger import resolve_roadmap_path

    project_root = orchd_dir.parent if orchd_dir.name == ".orchd" else orchd_dir
    roadmap_candidate = resolve_roadmap_path(project_root)
    roadmap = roadmap_candidate if roadmap_candidate.is_file() else None
    if roadmap is None:
        return []
    ideas = _find_workspace_file(orchd_dir, "IDEAS.md")
    ideas_text = ideas.read_text(encoding="utf-8") if ideas is not None else ""
    # 判据扩展（task-roadmap-section-parse-fix）：IDEAS.md 或 IDEAS-archive.md 含 §版本
    # 均视为「已落地」——archive_resolved_ideas 会在条目全部终态后把条目移入
    # IDEAS-archive.md（先写归档、再删主文件），已落地且已实现的章节不得「回弹告警」。
    archive = _find_workspace_file(orchd_dir, "IDEAS-archive.md")
    archive_text = archive.read_text(
        encoding="utf-8") if archive is not None else ""
    warnings: list[dict[str, Any]] = []
    for sec in _parse_roadmap_sections(roadmap.read_text(encoding="utf-8")):
        if sec["historical"] or not sec["id"]:
            continue
        if _section_landed(ideas_text, sec) or _section_landed(archive_text, sec):
            continue
        warnings.append(_e031_warning(sec))
    return warnings


def _section_landed(text: str, sec: dict[str, Any]) -> bool:
    """该章节在这份文本里是否已落地：``- id:`` 精确命中 **或** ``§版本`` 字面命中。

    task-caliber-residue-and-surface-gate AC6：判据以「章节 id 命中即算已落地」为真源，
    与 ``orchd.intake.roadmap_land`` 的幂等判据同口径。此前只看 ``§版本`` 字面，于是
    「id 命中但 detail 引旧版本号」的形态下 land 已跳过、E031 却恒告警且**清不掉**
    （重跑 land 被 id 短路、不写文件）——两条通道对同一份台账各说各话。

    为何不 import ``orchd.intake._entry_id_exists``：本模块只登记
    ``spec.py → errors.py / ledger.py / worktree 包 / registry / contracts 包``
    （见 .orchd/shared/conventions.md），新增 spec→intake 边须改登记表、不在本卡声明面；
    故按同一语义本地实现（行级精确比对，``- id:`` / ``id:`` 两种写法），两处口径由
    tests/test_cli_ideas.py::TestRoadmapLandE031Combination 的组用例钉在一起。
    """
    if f"§{sec['version']}" in text:
        return True
    sid = sec["id"]
    for line in text.splitlines():
        s = line.strip()
        if (s.startswith("- id:") and s[len("- id:"):].strip() == sid) or (
            s.startswith("id:") and s[len("id:"):].strip() == sid
        ):
            return True
    return False


def _e031_warning(sec: dict[str, Any]) -> dict[str, Any]:
    """构造单条 E031 落地告警（通道 C：经 structured_error 挂 details+guidance）。

    保留 ``{code, path, message}`` 既有键（消费方断言面不变），加法式附加
    ``details``（list 契约）与 ``guidance``（按码指引，command 指向 roadmap-land）。
    """
    message = (f"规划章节 ROADMAP §{sec['version']}（id: {sec['id']}）尚无 IDEAS 落地条目："
               "IDEAS.md 与 IDEAS-archive.md 均缺引用该章节的 detail；处置二选一——"
               "① 运行 `orchd roadmap-land <版本>` 落地为 IDEAS study（待 confirm 升 pending）；"
               "② 若该版本已发布或已放弃，标记历史或移出 ROADMAP")
    # hint 的唯一真源是 orchd/guide.py 的 E031 注册表条目（task-caliber-residue-and-
    # surface-gate AC1）：这里只把**版本号**交出去，由 attach_error_guidance 在渲染处代入
    # `<版本>` 占位。此前本函数自带一份 hint 覆盖注册表，同一件事两份措辞、改一处另一处
    # 照旧（"落地为 IDEAS pending" 假口径即由此活下来）。
    try:
        from orchd.ledger import structured_error

        resp = structured_error(
            "E031",
            message,
            [{
                "path": f"roadmap §{sec['version']}",
                "version": sec["version"],
            }],
            None,
        )
        err = resp.get("error", {})
        return {
            "code": err.get("code", "E031"),
            "path": f"roadmap §{sec['version']}",
            "message": message,
            "details": err.get("details", []),
            "severity": err.get("severity", "warning"),
            "guidance": resp.get("guidance"),
        }
    except Exception:
        # 通道 C best-effort：指引挂接失败不击穿 validate 告警链
        return {
            "code": "E031",
            "path": f"roadmap §{sec['version']}",
            "message": message,
        }


def layout_marker_warnings(project_root: Path) -> list[dict[str, Any]]:
    """validate 布局标记校验（task-14-worktree-layout，AC2）。

    布局标记（``.orchd/.layout.json``）缺失或主工作树不一致时返回告警
    （不判 invalid，对齐 E031 告警语义），并附自动探测结果——不静默跑错目录。
    标记存在且有效 → 空列表。
    """
    from orchd.worktree import detect_layout

    project_root = Path(project_root)
    layout = detect_layout(project_root)
    warnings: list[dict[str, Any]] = []
    for msg in layout.get("warnings", []):
        warnings.append({
            "code": "LAYOUT",
            "path": ".orchd/.layout.json",
            "message": msg,
        })
    return warnings
