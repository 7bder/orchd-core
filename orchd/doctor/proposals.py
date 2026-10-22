"""``orchd.doctor`` 提案与草案域（W2-D4a，file-split-program 20261015 §4.3）。

职责：``.orchd/proposals`` 目录的只读判定——草案名与内容 id（``_is_draft_name`` /
``_draft_content_ids``）、三列分划（``classify_proposals`` → ``stale_proposals`` /
``inflight_proposals``）、形态判据（``stray_draft_files`` / ``proposals_dir_entries``）、
三个检测器适配器（``_detect_stale_proposals`` / ``_detect_stray_draft_files`` /
``_detect_proposals_dir_entries``），以及 master 登记 id 读取（``_load_master_task_ids``）。

承载 14 个顶层定义（11 函数 + 3 随域常量）；函数体与搬迁前基线**逐字一致**（AST 等价性与
字节级判据见 tests/test_doctor_pkg.py）。

依赖方向：标准库（``json`` / ``pathlib`` / ``typing``）+ ``orchd.doctor.runtime``
（``_unavailable_residue``：INV-4a「检测不可用」留痕）；对 ``orchd.spec`` /
``orchd.worktree`` 保持**函数级惰性导入**（``_load_master_task_ids``，搬迁不改取用方式，
保持 monkeypatch 补丁面）。本模块**不得**导入 ``orchd.doctor`` 聚合入口。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchd.doctor.runtime import _unavailable_residue


def _load_master_task_ids(orchd_dir: Path) -> set[str] | None:
    """经单一真源解析 ``_master.json`` 登记的任务 id 集合。

    - 解析路径走 ``orchd.worktree.resolve_master_path_from_dir``（本地优先 →
      canonical 主工作树回退）：container 容器根/任务 worktree 视角取到
      canonical 权威 id 集合，不再裸读本地副本。
    - 解析出的 master **不存在** → 返回 None（不过滤）：合法最小场景
      （无 master 的 bare 项目），保留既有语义。
    - master **存在但不可解析** → 抛异常（调用方转「检测不可用」留痕，
      INV-4a：不得静默返回 None 充当无问题）。
    """
    from orchd.spec import load_master
    from orchd.worktree import resolve_master_path_from_dir

    master_path = resolve_master_path_from_dir(orchd_dir)
    if not master_path.exists():
        return None
    master = load_master(master_path)
    ids: set[str] = set()
    for task in master.tasks:
        tid = task.get("id")
        if isinstance(tid, str):
            ids.add(tid)
    return ids


def _is_draft_name(path: Path) -> bool:
    """条目是否算「草案」——:func:`classify_proposals` 与 :func:`stray_draft_files` 共用的
    唯一形态判据（两条判据取它的正反面，否则同一文件会被两门各点一次）。

    为什么不用 ``glob("*.json")``（spec review F1，2026-10-07 实测）：pathlib 的 glob
    在大小写不敏感文件系统上连 ``.JSON`` 一起命中，而 ``Path.suffix`` 比较是大小写
    敏感的——一个认得上、一个认不上，``Draft.JSON`` 就同时进「草案列」与「过程产物
    列」，两行 FAIL 给出的理由还互斥（既说它是已注册未清理的草案，又说它是非草案）。
    后果不对称：内容判据认得出它是草案，却挡不住产物判据经 ``move_to_backup`` 把用户
    仍在用的草案正文搬进备份桶。判据结论不得随宿主文件系统变 ⇒ 统一按
    ``suffix.lower()`` 判，并只认普通文件（名为 ``task-x.json`` 的**目录**不属草案）。
    非普通文件条目（目录 / 断链符号链接）由 :func:`proposals_dir_entries` 按**条目类型**
    单独点名、恒手动处置（不自动搬整树），见 task-proposals-dir-entry-disposition。
    """
    return path.is_file() and path.suffix.lower() == ".json"


def _draft_content_ids(path: Path) -> set[str] | None:
    """草稿内的任务 id 集合；``None`` = 内容判据不成立（不得据此判陈旧）。

    形态与 ``amend --register`` 的注册契约同口径：单任务对象或任务数组，每项须带
    字符串 ``id``。任一形态不符（不可解析 / 非对象项 / 缺 id / id 集合为空）一律
    返回 ``None``——**「判不了内容」不等于「判它陈旧」**，误判会删掉用户尚未注册的
    草案正文（草稿被 ``.gitignore`` 的 ``.orchd/*`` 忽略、不入库，删了就没有第二份）。

    只对 :func:`_is_draft_name` 认可的条目调用（形态不合格的条目连「读内容」都不必）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    items = data if isinstance(data, list) else [data]
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            return None
        tid = item.get("id")
        if not isinstance(tid, str) or not tid:
            return None
        ids.add(tid)
    return ids or None


def classify_proposals(orchd_dir: Path) -> tuple[list[Path], list[Path]]:
    """把 ``<orchd_dir>/proposals/`` 内的合格草案条目分成 ``(陈旧, 在途)`` 两列。

    本域判定的单一事实源：条目集合本身由 :func:`_is_draft_name` 决定（与
    :func:`stray_draft_files` 共用同一判据的正反面）——本函数只负责把「已算是草案的」
    分成两列，不负责裁定「什么算草案」。

    陈旧判据为**语义并集**（task-review-retention-and-draft-bundle-judgment AC1）：

      ① stem 命中：文件名去扩展名 ∈ 已注册任务 id；
      ② 内容命中：草稿内 id 集合非空 **且全部** ∈ 已注册 id。

    取并集而非替换 ⇒ 只有 ① 时已能点名的形态（前置任务实测归零的 162 条）判据零变化，
    本任务是**单调收紧**；②新增覆盖两类 ① 漏掉的形态：多 id 批次文件（文件名是批次名，
    如 ``pass9-review-fixes-batch.json`` 内含 11 个已注册 id）与 ``_*.json`` 别名形态
    （stem 因前导下划线与内容 id 不同名）。

    ② 用「全部」而非「任一个」，与 ``amend --register`` 的消费语义严格对齐（前置任务
    AC1 实测「批量任一项失败整批不写不删」）：部分注册的批次草案里仍有未注册的草案
    正文，判陈旧就会吞掉它——同一事实在消费侧与清理侧必须同口径，否则一侧说「还不能删」
    另一侧说「已经陈旧」。

    两列都按文件名排序，目录条目数本身不构成判据。

    消费方（不得另写一份同语义实现，两门点名的必须是同一文件集合）：
      ① :func:`stale_proposals` → :func:`_detect_stale_proposals` →
         :func:`detect_residues`（``doctor`` 报告与 ``doctor --fix`` 的受管清理入口）；
      ② ``scripts/verify_project_hygiene.py`` 的 ``[proposals]`` 失败行与
         ``[proposals-inflight]`` 留痕行——前者经 ① 汇入同一份残留清单，后者取本函数
         的第二列。

    fail-open 边界：``_master.json`` 不存在 → ``([], [])``（bare 项目无「已注册」可言，
    也没有可判陈旧的依据）；存在但不可解析 → :func:`_load_master_task_ids` 抛异常，
    由调用方转「检测不可用」留痕（INV-4a：不得静默当成「无陈旧草稿」）。
    """
    registered = _load_master_task_ids(orchd_dir)
    if not registered:
        return [], []
    proposals_dir = orchd_dir / "proposals"
    if not proposals_dir.is_dir():
        return [], []
    stale: list[Path] = []
    inflight: list[Path] = []
    for path in sorted(p for p in proposals_dir.iterdir() if _is_draft_name(p)):
        content_ids = _draft_content_ids(path)
        if path.stem in registered or (
                content_ids is not None and content_ids <= registered):
            stale.append(path)
        else:
            inflight.append(path)
    return stale, inflight


def stale_proposals(orchd_dir: Path) -> list[Path]:
    """已注册任务的历史提案草稿（判据见 :func:`classify_proposals` 的陈旧列）。

    保留本函数名是契约的一部分：前置任务的两侧消费方（``_detect_stale_proposals`` 与
    卫生门禁）以及既有逐字用例都点它，扩判据只动列的实现、不动入口。
    """
    stale, _inflight = classify_proposals(orchd_dir)
    return stale


def inflight_proposals(orchd_dir: Path) -> list[Path]:
    """未注册的在途草稿（判据见 :func:`classify_proposals` 的在途列）。

    这一列**没有清理通道也不得有**：草案正文是用户尚未确认/尚未注册的意图载体，
    intake 规则「只有用户可置 dropped」是硬边界。本列只供留痕（卫生门的
    ``[proposals-inflight]`` WAIVED 行），:func:`doctor_fix` 对其零操作。
    """
    _stale, inflight = classify_proposals(orchd_dir)
    return inflight


def stray_draft_files(orchd_dir: Path) -> list[Path]:
    """``.orchd/proposals/`` 内**不是草案**的文件条目 —— 草案目录内容纪律的判据。

    条目集合与 :func:`classify_proposals` 取同一个谓词的正反面（:func:`_is_draft_name`），
    故「草案两列 ∪ 本列」恒等于目录内全部文件条目，且两两不相交：同一份文件不可能
    既被 ``[proposals]`` 又被 ``[proposals-stray]`` 点名（spec review F1 打回的正是这一点，
    当时草案列走 ``glob("*.json")``、本列走 ``suffix != ".json"``，大小写敏感性不一致）。

    该目录的合法内容只有任务草案。历史上一条 ``_*.py`` 形态的一次性注册脚本被
    ``_MAIN_JUNK_GLOBS`` 的 ``(".orchd/proposals", "_*.py")`` 判 FAIL，但同目录实测
    存在无前导下划线的 ``build-proposal.py``（其自身 docstring 写「一次性注册脚本
    （task-amend-additional-sources-field），执行后删除」，而该任务确已注册完成）——
    形态判据漏判。改判「后缀不是 .json 即过程产物」后覆盖面是原模式的超集（单调收紧），
    且与原 ``_*.py`` 判据不能并存，否则同一文件被两门各点一次（重复计数）。

    非普通文件条目（目录 / 断链符号链接）不在本列（也只认普通文件）：它们由
    :func:`proposals_dir_entries` 单独点名（task-proposals-dir-entry-disposition），
    处置恒手动、不自动搬整树——搬整棵目录需要另一套处置语义（既有
    ``_dispose_residual_dir`` 对非空目录直接拒绝），超出本域 ``move_to_backup`` 的单文件
    范围。
    """
    proposals_dir = orchd_dir / "proposals"
    if not proposals_dir.is_dir():
        return []
    return sorted(p for p in proposals_dir.iterdir()
                  if p.is_file() and not _is_draft_name(p))


def proposals_dir_entries(orchd_dir: Path) -> list[Path]:
    """``.orchd/proposals/`` 内的**非普通文件条目**（目录 / 断链符号链接）。

    按**条目类型**分派而非按名字（task-proposals-dir-entry-disposition，源自 spec review
    F1 探针）：``_is_draft_name`` 只认普通文件，于是名为 ``task-1.json`` 的目录曾同时漏出
    草案/产物两列（修复前更糟：``glob("*.json")`` 会把它拉进陈旧列，``shutil.move`` 对目录
    是整树搬移）。本函数与 :func:`classify_proposals`/:func:`stray_draft_files` 互补：
    三列覆盖普通文件，本列出「非普通文件」形态，二者不相交。

    - 目录（含符号链接指向的目录，``is_dir()`` 跟随）：一律点名；
    - 断链符号链接（``is_symlink() ∧ ¬is_file()``）：同族，按类型点名；
    - 指向普通文件的符号链接：``is_file()`` 为真，仍按普通文件走草案/产物列（不在此列）。

    处置恒手动（不自动搬整树）：见 :func:`_detect_proposals_dir_entries`。
    """
    proposals_dir = orchd_dir / "proposals"
    if not proposals_dir.is_dir():
        return []
    entries: list[Path] = []
    for path in sorted(proposals_dir.iterdir()):
        if path.is_dir() or (path.is_symlink() and not path.is_file()):
            entries.append(path)
    return entries


_PROPOSALS_DIR_ENTRY_REASON = (
    "草稿目录内的非普通文件条目（目录 / 断链符号链接）——按条目类型分派，"
    "不自动搬整树；请人工确认后处置（移出或删除）"
)


def _detect_proposals_dir_entries(project_root: Path) -> list[dict[str, Any]]:
    """检出草案目录内的非普通文件条目（目录 / 断链 symlink）。

    处置恒 ``manual``（action ``proposals_dir_manual`` ∈ ``_MANUAL_ACTIONS``）：
    **保守取向——任何目录形态都不自动搬整树**（非空/空都不搬），只点名交人工。
    理由：把目录整树 ``shutil.move`` 进备份桶需要额外的逐字节可回滚证明，而草稿目录
    被 ``.gitignore`` 忽略、没有第二份；在证明到位前，宁可让用户显式处置。该取向也守
    rules/safety.md「清理面三条边界」——不新造无界目录、不写死命名形态假设。
    """
    orchd_dir = Path(project_root) / ".orchd"
    return [
        {
            "path": str(p),
            "type": "proposals_dir_entry",
            "reason": _PROPOSALS_DIR_ENTRY_REASON,
            "action": "proposals_dir_manual",
        } for p in proposals_dir_entries(orchd_dir)
    ]


_STRAY_DRAFT_REASON = "草稿目录内的非草案文件（一次性脚本等过程产物），执行后未删除"

_STALE_PROPOSAL_REASON = "对应任务已注册，草稿未清理"


def _detect_stray_draft_files(project_root: Path) -> list[dict[str, Any]]:
    """检出草案目录内的过程产物（AC3②）。

    处置复用前置任务建好的 ``move_to_backup`` 档（不新增第三种处置机制）：整文件移入
    既有 ``.doctor-backup/<ts>/`` 桶，逐字节可回滚。该目录本身被 ``.gitignore`` 忽略、
    不入库，故移桶是「保住唯一副本」而非「留两份」。

    处置档同 9) 恒 ``manual``（type 不入 ``_AUTO_CLEAN_TYPES``）：草案目录属准入域，
    读路径（status/watchdog）不得顺手清理，只有显式 ``doctor --fix`` 才动文件。
    """
    orchd_dir = Path(project_root) / ".orchd"
    return [
        {
            "path": str(p),
            "type": "stray_draft_file",
            "reason": _STRAY_DRAFT_REASON,
            "action": "move_to_backup",
        } for p in stray_draft_files(orchd_dir)
    ]


def _detect_stale_proposals(project_root: Path) -> list[dict[str, Any]]:
    """检出陈旧提案草稿（task-hygiene-gate-executor-and-proposal-lifecycle AC2）。

    处置档恒 ``manual``：type 既不入 ``_AUTO_CLEAN_TYPES`` 也不入
    ``_LEGACY_MOVE_TYPES``，故 ① 状态收敛通道（status/watchdog 的 auto_clean）永不
    顺手删除准入产物，只有显式 ``doctor --fix`` 才动文件；② 卫生门禁判 FAIL 而非
    「清理器即将处理」豁免（若归 auto_clean，门禁会豁免掉本域全部存量，注册路径
    失守重新变成不可见——正是本任务要堵的洞）。

    ``action=move_to_backup`` 由 :func:`_dispose_file_to_backup` 处理：整文件搬入既有
    ``.doctor-backup/<ts>/`` 桶（保持相对路径），源消失、内容逐字节可回滚。
    """
    orchd_dir = Path(project_root) / ".orchd"
    try:
        paths = stale_proposals(orchd_dir)
    except Exception as exc:  # INV-4a：master 不可解析 → 留痕，不静默空
        return [_unavailable_residue("stale_proposals", exc, orchd_dir)]
    return [
        {
            "path": str(p),
            "type": "stale_proposal",
            "task_id": p.stem,
            "reason": _STALE_PROPOSAL_REASON,
            "action": "move_to_backup",
        } for p in paths
    ]
