"""orchd 账本路径解析域（file-split-program W1-L5，2026-10-15）。

本模块自 ``orchd/ledger/__init__.py`` 整块搬出路径解析族（7 函数 + 2 私有常量，语句级等价，
零逻辑改动）：``.orchd`` 目录定位、共享账本根解析（flat / container 双布局）、审查模式、
工作区根与 ROADMAP 路径解析（含 legacy 路径迁移提示）。

依赖方向：顶层只依赖标准库；对 ``orchd.worktree`` 的引用（``find_orchd_dir_within_git_boundary``
/ ``read_layout`` / ``resolve_master_path_from_dir`` / ``resolve_canonical_project_root``）沿用
搬移前的**函数级惰性导入**，故顶层不导入 ``orchd.ledger*`` 也不导入 orchd.worktree——
无加载期环，聚合入口 ``orchd/ledger/__init__.py`` 可安全重导出。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _find_orchd_dir() -> Path:
    """从当前工作目录向上定位 .orchd 目录（发布态自包含布局）。

    仓库边界（task-canonical-root-boundary-guard，AC1/AC2）：与 CLI 侧
    ``orchd.cli._util._find_orchd_dir`` **同源**委托
    :func:`orchd.worktree.find_orchd_dir_within_git_boundary`——不越过起点所属
    的最近 git 仓库根：内层独立 git 仓库无 ``.orchd`` 时返回 ``cwd/.orchd``
    （flat/自身），绝不爬到宿主真实 ``.orchd``；非 git 目录维持逐级向上（零回归）。
    """
    from orchd.worktree import find_orchd_dir_within_git_boundary

    return find_orchd_dir_within_git_boundary(Path.cwd())


def resolve_store_dir(orchd_dir: Path) -> Path:
    """解析账本根目录（task-orchd-home-redirect，roadmap:snapshotstore-m-p0）。

    账本（ledger / checkpoint / lock / mod-*）由环境变量 ``ORCHD_HOME`` 重定向到
    外部目录；未设置时回退到传入的 ``orchd_dir``（默认 ``<cwd 向上找的 .orchd/>``）。

    1.4 共享账本默认（task-14-worktree-lifecycle，R3）：未设 ``ORCHD_HOME`` 时，
    若主工作树存在 **container** 布局标记（``.orchd/.layout.json`` layout=container）
    → 默认布局级 runtime 根（``<容器>/.orchd-runtime/``，多会话共享账本）；
    flat（含标记与未迁移项目）→ 维持 ``orchd_dir`` 现状零回归
    （flat 单会话账本仍在主工作树 .orchd/，共享账本留待 container 形态落地）。

    注意：``orchd_dir`` 语义是「master 目录」（含 ``_master.json`` + ``shared/``，
    走 git，不入 backend）；返回的账本根仅用于 FilesystemBackend 派生账本文件路径。
    """
    home = os.environ.get("ORCHD_HOME")
    if home:
        return Path(home)
    # task-14-worktree-lifecycle：仅 container 布局 → 布局级 runtime 根（共享账本默认）
    # task-pass9-ledger-lock-root（pass9 F3）：不再套 except Exception——布局标记
    # 读取的异常处理收敛在 read_layout（缺失/损坏 → None，flat 回退）；此处若仍
    # 有异常逸出（如标记 JSON 结构非对象触发 AttributeError）属非预期故障，必须
    # 上抛（fail-closed）：账本根解析是系统真相源的路径决策，静默回退本地
    # .orchd 会让 container 布局的并发进程分裂成两个账本。
    from orchd.worktree import read_layout

    marker = read_layout(orchd_dir)
    if isinstance(marker, dict) and marker.get("layout") == "container":
        main_wt = Path(marker["main_worktree"])
        return main_wt.parent / ".orchd-runtime"
    return orchd_dir


def resolve_review_mode(orchd_dir: Path) -> str:
    """解析项目审查模式（review-unify-r2：unified / two_phase）。

    读 ``.orchd/_master.json`` 顶层 ``project.review_mode``：
    - ``"unified"``  → 单阶段审查：一次 APPROVED 即 merge；
    - ``"two_phase"``/缺失/非法值 → 两阶段审查（spec → code），保持旧行为。

    缺省 two_phase 保证观察期兼容：旧项目 / 测试 / 老事件均不受影响，
    显式配置 ``project.review_mode: "unified"`` 才启用单阶段链路。
    best-effort：master 缺失/解析失败返回 ``"two_phase"``（不抛异常）。

    master 路径经 ``orchd.worktree.resolve_master_path_from_dir`` 单一真源解析
    （本地优先 → canonical 主工作树回退）：container 容器根视角读到 canonical
    权威配置，不再裸读本地副本静默回落。函数内惰性导入（ledger 不在顶层依赖
    worktree，避免循环）。
    """
    try:
        from orchd.worktree import resolve_master_path_from_dir

        master_path = resolve_master_path_from_dir(orchd_dir)
        if not master_path.exists():
            return "two_phase"
        import json as _json

        master = _json.loads(master_path.read_text(encoding="utf-8"))
        mode = (master.get("project") or {}).get("review_mode")
        if mode == "unified":
            return "unified"
        return "two_phase"
    except (OSError, ValueError):
        return "two_phase"


_WORKSPACE_DOCS = ("IDEAS.md", "ROADMAP.md", "SKILL.md")


def resolve_workspace_root(project_root: Path) -> Path:
    """解析工作区文档根目录（IDEAS.md / ROADMAP.md / SKILL.md 所在目录）。

    task-12-engine-path-abstraction 双态兼容：
    - 根布局（开发态）：工作区文档在项目根 → 返回 ``project_root``。
    - 发布态布局（自包含 ``.orchd/``）：工作区文档归置 ``.orchd/`` → 返回
      ``project_root / ".orchd"``。

    task-canonical-workspace-docs（2026-08-25）canonical 化：入口先经
    ``resolve_canonical_project_root`` 解析到 canonical 主工作树根——
    container 布局返回 ``<容器>/main/``（布局标记权威），flat 布局返回本地。
    再按上述布局规则定位文档根：intake/ideas/amend 在任务 worktree 内调用时
    仍统一从主工作树读 IDEAS/ROADMAP/SKILL，避免 worktree 本地 ``.orchd/``
    拷贝（引擎传播的 SKILL.md 等）过期导致摄入/引导不一致。

    仓库边界（task-canonical-root-boundary-guard，AC2）：本函数不自行扫祖先，
    一律经 ``resolve_canonical_project_root`` 归位，故同源遵守「不得越过起点
    所属最近 git 仓库根」——内层独立 git 仓库不会被误归到宿主工作区文档根。

    判定：``.orchd/`` 下已存在任一工作区文档 → 发布态；否则若项目根存在 → 开发态；
    两者都无 → 默认返回 ``.orchd/``（发布态默认，AC3）。

    调用方：``validate_source``（spec.py）、``archive_resolved_ideas``（ideas.py）、
    ``amend``/``ideas_archive``（cli.py）——统一经本 helper 解析 IDEAS/ROADMAP/SKILL 路径。
    """
    project_root = Path(project_root)
    # task-canonical-workspace-docs（2026-08-25）：统一共享读入口，container 布局
    # 解析到 canonical 主工作树根（flat 返回本地），worktree 本地副本不参与文档定位。
    from orchd.worktree import resolve_canonical_project_root

    project_root = resolve_canonical_project_root(project_root)
    project_root = Path(project_root)
    orchd_dir = project_root / ".orchd"
    if any((orchd_dir / name).exists() for name in _WORKSPACE_DOCS):
        return orchd_dir
    if any((project_root / name).exists() for name in _WORKSPACE_DOCS):
        return project_root
    return orchd_dir


_ROADMAP_DOC = "ROADMAP.md"


def resolve_roadmap_path(project_root: Path) -> Path:
    """定位 ROADMAP.md——**唯一源 = 宿主项目根**，与布局无关，无回退候选。

    task-roadmap-single-source（用户裁定 2026-09-17）：ROADMAP 是**宿主资产**不是
    引擎产物，唯一源是宿主项目根 ``ROADMAP.md``（flat 布局即仓库根，container 布局
    为 canonical 主工作树 ``<容器>/main/``）；``.orchd/ROADMAP.md`` 不应存在，本函数
    **不读它**（旧布局残留时仅 stderr 留痕提示归根，见 :func:`_log_legacy_roadmap`）。

    演进：task-roadmap-root-resolution 曾实现为「宿主根优先 + .orchd/ 回退」，回退
    分支等于给引擎产物留后门（同一份文档有两个可能位置），与裁定冲突，故此处去掉。

    本 helper 仍是 ROADMAP 定位的**唯一真源**：roadmap-land / intake_commit /
    E025 溯源 / roadmap_landing_warnings / 任务 worktree 同步统一复用。

    与 :func:`resolve_workspace_root` 的分工：后者按 IDEAS / SKILL / ROADMAP 三份文档
    的捆绑判定返回**工作区文档根**（IDEAS / SKILL / 归档继续复用，语义零回归）；
    ROADMAP 的路径一律走本函数，不再由工作区文档根拼出。

    Args:
        project_root: 起点目录（任务 worktree 亦可，内部先 canonical 化）。

    Returns:
        宿主项目根的 ROADMAP.md 路径；**不保证存在**——消费点按需判 ``exists()``。
    """
    from orchd.worktree import resolve_canonical_project_root

    root = Path(resolve_canonical_project_root(Path(project_root)))
    root_doc = root / _ROADMAP_DOC
    legacy = root / ".orchd" / _ROADMAP_DOC
    # 旧布局残留（.orchd/ROADMAP.md）可观测：不读、不搬，仅留痕提示宿主处置。
    # 不自动搬家的原因：ROADMAP 是宿主资产，移动与提交归属由宿主决定
    # （一次性迁移点由安装器代搬，见 release/install.py::_roadmap_disposition）。
    # 判据按**内容**而非「根文件是否存在」（task-installer-legacy-roadmap-nonmask AC2）：
    # 旧判据 `legacy.exists() and not root.exists()` 会被空模板落根抑制 → 遮蔽反而更静默。
    if legacy.exists() and _legacy_roadmap_actionable(legacy, root_doc):
        _log_legacy_roadmap(root, legacy, root_doc=root_doc)
    return root_doc


def _legacy_roadmap_actionable(legacy: Path, root_doc: Path) -> bool:
    """旧布局副本是否仍需宿主处置：唯一源缺失，或两份内容已不一致。

    内容一致（安装器迁移后的稳态）视为「已处置」→ 不再重复告警，避免引擎热路径噪声；
    任何读取异常按「需处置」返回（宁可多报，不可静默）。CRLF/LF 差异不算分歧。
    """
    if not root_doc.exists():
        return True
    try:
        legacy_bytes = legacy.read_bytes()
        root_bytes = root_doc.read_bytes()
    except OSError:
        return True
    if legacy_bytes == root_bytes:
        return False
    return legacy_bytes.replace(b"\r\n", b"\n") != root_bytes.replace(b"\r\n", b"\n")


def _log_legacy_roadmap(root: Path, legacy: Path, *, root_doc: Path | None = None) -> None:
    """旧布局 ``.orchd/ROADMAP.md`` 留痕（禁静默）：ROADMAP 唯一源已改为宿主项目根。

    触发判据见 :func:`_legacy_roadmap_actionable`：唯一源缺失（``root_missing``）或与
    副本内容不一致（``content_diverges``）；``reason`` 区分可操作场景
    （task-installer-legacy-roadmap-nonmask AC2：不再被「宿主根已有文件」抑制）。

    与 ``worktree._log_recycle`` 同型：结构化 JSON 单行写 stderr，任何异常静默不阻断
    主流程。宿主据此把文件移到项目根（安装器在一次性迁移点代做），或删除已确认的旧副本。
    """
    try:
        target = root_doc if root_doc is not None else (root / _ROADMAP_DOC)
        reason = "root_missing" if not target.exists() else "content_diverges"
        record = {
            "action": "legacy_orchd_roadmap_ignored",
            "reason": reason,
            "expected": str(target),
            "legacy": str(legacy),
            "hint": (
                "ROADMAP 唯一源为宿主项目根：请把该文件移到项目根后重试"
                if reason == "root_missing"
                else "唯一源已就绪，.orchd/ROADMAP.md 为旧布局副本且内容已不一致："
                     "确认无待迁移内容后删除该副本"
            ),
        }
        print(f"orchd ▸ [roadmap] {json.dumps(record, ensure_ascii=False)}",
              file=sys.stderr)
    except Exception:
        pass
