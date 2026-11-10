"""``orchd.doctor`` 残渣域（W2-D4c，file-split-program 20261015 §4.3）。

职责：工作树残留的只读盘点与聚合——陈旧 worktree 登记（``_detect_stale_worktree_registry``）、
目录类残留（``_dir_contains_real_content`` / ``_classify_residual_dir`` /
``_detect_residual_dirs``）、旧扁平布局（``_detect_legacy_flat_residues``）、hook 强制缺口
（``_detect_hook_enforcement_gaps``）、统一编排（``_run_detector`` / ``detect_residues``：调用本域
15 个检测器 + locks 域 4 个 + proposals 域 3 个 + continuity 域 7 个）、幽灵任务
（``_ghost_detection_store`` /
``_detect_ghost_tasks`` / ``_ghost_retract_target``）与残渣处置映射（``_residue_disposition``
+ 3 个自动清洁常量）。

承载 20 个顶层定义（15 函数 + 5 常量：3 个分级常量 + 2 个构建缓存词表；会话接续包
检测器拆至 ``orchd/doctor/continuity.py``，本模块经 from-import 接线，wiring 与判据面零变化）；
函数体与搬迁前基线**逐字一致**（AST 等价性与字节级判据见 tests/test_doctor_pkg.py）。
**漏搬面**：``_GHOST_TERMINAL_STATUSES`` 是原聚合入口夹在函数之间的模块级 import
绑定（不在 def/Assign census 口径内），随本卡移入模块头。

依赖方向：标准库（``pathlib`` / ``typing`` / ``time``）+ ``orchd.doctor.runtime``（8 名）+
``orchd.doctor.locks``（4 个锁检测器）+ ``orchd.doctor.proposals``（3 个提案适配器）+
``orchd.doctor.continuity``（7 个会话接续包检测器，task-cont-autoclean-detectors +
task-cont-autoclean-criteria）+
``orchd.contracts.events``（``TERMINAL_STATUSES`` 别名）；对 ``orchd.worktree`` /
``orchd.worktree.layout`` / ``orchd.ledger`` / ``orchd.gitops`` / ``orchd.spec`` /
保持**函数级
惰性导入**（搬迁不改取用方式，保持 monkeypatch 补丁面）。本模块**不得**导入 ``orchd.doctor``
聚合入口。
"""

from __future__ import annotations

from pathlib import Path

from orchd.names import (
    LAYOUT_FILE,
)
from orchd.constants import (
    ORCHD_BAKED_PY_ENV, ORCHD_BAKED_SRC_ENV, ORCHD_HOME_ENV,
    ORCHD_LINE_TASK_PREFIX_ENV,
)
from typing import TYPE_CHECKING, Any, Callable

from orchd.contracts.events import (
    EVENT_FORCE_STATUS,
    EVENT_RETRACT,
    STATUS_CLAIMED,
    TERMINAL_STATUSES as _GHOST_TERMINAL_STATUSES,
)
from orchd.doctor.locks import (
    _detect_git_tracked_locks, _detect_orphan_session_locks, _detect_residual_intake_locks,
    _detect_zombie_sessions,
)
from orchd.doctor.continuity import (
    _BUILD_CACHE_DIR_NAMES, _detect_known_build_caches, _detect_merged_review_dirs,
    _detect_stale_doctor_backups, _detect_stale_rescue_refs, _detect_stale_runtime_logs,
    _detect_stale_verify_tmps, _detect_stale_unregistered_proposals,
)
from orchd.doctor.proposals import (
    _detect_proposals_dir_entries, _detect_stale_proposals, _detect_stray_draft_files,
)
from orchd.doctor.runtime import (
    _DETECTION_UNAVAILABLE_RESIDUE, _RUNTIME_STATE_FILES, _UNREADABLE_ACTION, _probe_is_file,
    _resolve_runtime_dir, _run_git, _unavailable_residue, _unreadable_residue,
)

if TYPE_CHECKING:  # 仅类型检查期导入（doctor 保持叶子性：运行期惰性导入 ledger）
    from orchd.ledger import Store


def _detect_stale_worktree_registry(
        project_root: Path) -> list[dict[str, str]]:
    """检测 git worktree 注册中的 prunable 项（有注册、工作目录已不存在）。

    解析 ``git worktree list --porcelain`` 输出，找含 ``prunable`` 标记的
    worktree。返回 [{path, reason}] 列表；git 不可用时返回空（不干扰其他检查）。
    """
    result = _run_git(project_root, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return []
    stale: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current is not None and "prunable" in current:
                stale.append(current)
            current = {"path": line[len("worktree "):].strip()}
        elif line.startswith("prunable") and current is not None:
            current["prunable"] = line[len("prunable"):].strip() or "prunable"
            current["reason"] = current["prunable"]
    if current is not None and "prunable" in current:
        stale.append(current)
    return stale


def _dir_contains_real_content(path: Path) -> bool:
    """目录树内是否存在「非可再生杂项」条目（真实工作痕迹）（DR-3）。

    杂项判据复用 ``worktree._is_junk_entry``（单一事实源，禁止第二份解析）；任何
    不确定情形（导入失败 / OSError / 无法判定类型）**保守返回 True**——宁可少清一个
    残留目录，也不误删含真实工作的目录树。
    """
    try:
        from orchd.worktree import _is_junk_entry
    except Exception:
        return True
    try:
        entries = list(path.iterdir())
    except OSError:
        return True
    for entry in entries:
        if _is_junk_entry(entry.name):
            continue
        try:
            is_dir = entry.is_dir() and not entry.is_symlink()
        except OSError:
            return True
        if not is_dir:
            return True  # 非杂项文件 / 符号链接 → 真实内容
        if _dir_contains_real_content(entry):
            return True
    return False


# 字节码缓存（``__pycache__``）内容白名单：只认可再生的字节码文件。
_BYTECODE_CACHE_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})
# 工具缓存（``.pytest_cache`` / ``.mypy_cache`` / ``.ruff_cache`` / ``.hypothesis``）
# 里若出现这些**源码**后缀，视为「借缓存目录名藏用户内容」——交回保守判定，
# 不因目录名命中已知集合而放松用户内容保护。
_CACHE_FORBIDDEN_SUFFIXES = frozenset({
    ".py", ".pyx", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php",
})


def _is_build_cache_payload(path: Path, *, strict: bool | None = None) -> bool:
    """目录名已命中 ``_BUILD_CACHE_DIR_NAMES`` 时，校验其内容确为构建缓存产物。

    这是构建缓存处置的**收紧判据**（V163）：只有目录名精确命中已知缓存集合时
    才调用，非缓存目录绝不经过此快速通道，仍按
    :func:`_dir_contains_real_content` 的保守口径判定（用户内容保护不变）。

    分级口径：
    - ``__pycache__``（含其子目录）：**严格**——只允许 ``.pyc`` / ``.pyo`` / ``.pyd``，
      借名存放其它文件即判非缓存；
    - 其它工具缓存目录：产物形态多样（标记 / 索引 / 条目文件，含 ``.json`` 与无后缀
      条目），不做文件名白名单；仅当出现源码后缀文件时判为「借名藏用户内容」。

    Args:
        path: 目录名已命中 ``_BUILD_CACHE_DIR_NAMES`` 的目录。
        strict: 是否按字节码严格口径判定；None 时按 ``path.name == "__pycache__"``
            推导（递归子目录沿用父目录口径）。

    Returns:
        True 表示内容全部为缓存产物（可安全走 ``junk_only`` 快速通道）；任何
        不确定（OSError / 不允许的条目）返回 False，交回保守判定（宁留勿删）。
    """
    if strict is None:
        strict = path.name == "__pycache__"
    try:
        entries = list(path.iterdir())
    except OSError:
        return False
    for entry in entries:
        try:
            is_dir = entry.is_dir() and not entry.is_symlink()
        except OSError:
            return False
        if is_dir:
            # 子目录沿用父口径：__pycache__ 的嵌套树同样只允许字节码。
            if not _is_build_cache_payload(entry, strict=strict):
                return False
            continue
        suffix = entry.suffix.lower()
        if strict:
            if suffix not in _BYTECODE_CACHE_SUFFIXES:
                return False
        elif suffix in _CACHE_FORBIDDEN_SUFFIXES:
            return False
    return True


def _classify_residual_dir(path: Path) -> str:
    """残留目录内容分级：``empty`` / ``junk_only`` / ``has_content``（DR-3）。

    只有 ``empty`` / ``junk_only`` 允许被清理通道移除；``has_content``（含非杂项
    文件/链接的真实工作痕迹，典型：丢了 ``.git`` 指针但工作文件仍在的 worktree）
    一律不删，单列人工处置档——原实现直接 ``_rmtree_force`` 递归永久抹除。

    构建缓存快速通道（V163）：目录名**精确命中** :data:`_BUILD_CACHE_DIR_NAMES`
    且内容确为缓存产物（判据见 :func:`_is_build_cache_payload`）时直接判
    ``junk_only``——宿主安装引擎的 ``.orchd/orchd/**/__pycache__`` 不再以
    ``has_content`` 被 --fix / auto_clean 拒绝。该小门只对已知缓存目录名开放，
    非缓存目录名仍走 :func:`_dir_contains_real_content`，``residual_dir_manual``
    行为零变化（不放松用户内容保护）。
    """
    if not path.is_dir() or path.is_symlink():
        return "has_content"  # 非普通目录（异常形态）→ 保守按含内容处理
    try:
        if not any(path.iterdir()):
            return "empty"
    except OSError:
        return "has_content"
    if path.name in _BUILD_CACHE_DIR_NAMES and _is_build_cache_payload(path):
        return "junk_only"
    return "has_content" if _dir_contains_real_content(path) else "junk_only"


def _detect_residual_dirs(project_root: Path) -> list[dict[str, Any]]:
    """检测残留任务 worktree 目录（P0-19 类，无 .git 登记 + 无活跃绑定）。

    判定与 prune_orphans 的 P0-19 分支一致：目录存在但无 .git 文件
    （Windows git worktree remove 不完整）且无 session-worktrees 绑定。
    container 布局下任务 worktree 与主工作树平级（task_wt_root）；flat 布局
    无任务 worktree 概念 → 返回空。

    内容分级（DR-3）：检出后再按 :func:`_classify_residual_dir` 分级——
    ``empty`` / ``junk_only`` → ``action="delete_dir"``（清理通道移入备份区）；
    ``has_content`` → ``action="residual_dir_manual"``（**不自动删**，单列人工处置，
    防止「丢 .git 指针但含真实工作」的目录被递归永久抹除）；两项均带 ``content``
    字段，供 dry-run 预览如实呈现可清 / 需人工。
    """
    residues: list[dict[str, Any]] = []
    try:
        from orchd.worktree import detect_layout, load_bindings
        from orchd.ledger import resolve_store_dir

        layout = detect_layout(Path(project_root))
        if layout.get("layout") != "container":
            return residues
        task_wt_root = Path(layout["task_wt_root"])
        bindings = load_bindings(
            resolve_store_dir(Path(project_root) / ".orchd"))
    except Exception as exc:  # INV-4a：残留目录检测不可用（此前静默空 = 无残留）
        return [_unavailable_residue("residual_dirs", exc, project_root)]

    try:
        if not task_wt_root.is_dir():
            return residues
        # iterdir 也在 try 内：容器根（container 布局的 task_wt_root）任一不可
        # stat 项不得让巡检整体崩溃（层 2，见 _unreadable_residue）。
        entries = sorted(task_wt_root.iterdir())
    except OSError as exc:
        return [_unreadable_residue("residual_dirs", exc, task_wt_root,
                                    stage="iterdir")]
    for entry in entries:
        try:
            # 逐项 stat 容错：不可遍历 reparse point（WinError 448）/ 断链 /
            # 权限不足的**单个**目录项只产出一条 unreadable 留痕，其余项照常检出。
            # 判据顺序保持 is_dir() 在首位（与旧实现同语义），只补 try/except。
            if not entry.is_dir():
                continue
        except OSError as exc:
            residues.append(_unreadable_residue("residual_dirs", exc, entry,
                                                stage="entry_stat"))
            continue
        if not (entry.name.startswith("task-")
                and entry.name not in bindings):
            continue
        try:
            has_git_marker = (entry / ".git").exists()
        except OSError as exc:
            residues.append(_unreadable_residue("residual_dirs", exc, entry,
                                                stage="git_marker"))
            continue
        if has_git_marker:
            # 有 .git 标记：与 worktree 侧同源判定（_is_effective_worktree）——
            # 有效 worktree 绝不报残留；登记失效/HEAD 不可解析的半检出以
            # delete_dir 报出（经备份区处置，可回滚），消除“无出口需人工”。
            try:
                from orchd.worktree import _is_effective_worktree

                effective = _is_effective_worktree(Path(project_root), entry)
            except Exception:
                continue
            if effective:
                continue
            residues.append({
                "path": str(entry),
                "type": "residual_dir",
                "content": "stale_half_checkout",
                "reason": ("残留半检出 worktree 目录（有 .git 标记但 git 登记"
                           "失效或 HEAD 不可解析）"),
                "action": "delete_dir",
            })
            continue
        try:
            content = _classify_residual_dir(entry)
        except OSError as exc:
            residues.append(_unreadable_residue("residual_dirs", exc, entry,
                                                stage="content_classify"))
            continue
        if content == "has_content":
            residues.append({
                "path":
                str(entry),
                "type":
                "residual_dir",
                "content":
                content,
                "reason": ("残留任务 worktree 目录（无 .git 登记且无活跃绑定），"
                           "但目录内含非杂项内容（疑似真实工作残留）"
                           "——不自动删除，需人工确认后处置"),
                "action":
                "residual_dir_manual",
            })
            continue
        residues.append({
            "path": str(entry),
            "type": "residual_dir",
            "content": content,
            "reason": "残留任务 worktree 目录（无 .git 登记且无活跃绑定）",
            "action": "delete_dir",
        })
    return residues


def _detect_legacy_flat_residues(project_root: Path) -> list[dict[str, Any]]:
    """检测 flat 布局遗留的状态文件（container / ORCHD_HOME 重定向生效时）。

    ``resolve_store_dir(orchd_dir) != orchd_dir`` 说明账本根已重定向
    （container → ``<容器>/.orchd-runtime``，或 ORCHD_HOME 指定），此时
    ``orchd_dir`` 下的运行时状态文件（_ledger.jsonl / _checkpoint.json 等）是
    历史 flat 遗留，--fix 备份后删除。flat 布局（无重定向）→ 零操作。
    """
    residues: list[dict[str, Any]] = []
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.ledger import resolve_store_dir

        canonical = resolve_store_dir(orchd_dir)
    except Exception as exc:  # INV-4a：账本根解析不可用（此前静默空 = 无遗留）
        return [_unavailable_residue("legacy_flat_residues", exc, orchd_dir)]
    if canonical.resolve() == orchd_dir.resolve():
        return residues  # flat：无重定向，canonical 即 orchd_dir，零操作
    if not orchd_dir.is_dir():
        return residues
    # 布局标记（.layout.json）永不 legacy 清理：它是布局解析权威（container 下
    # 主工作树 .orchd/ 的标记被搬走即布局错乱）。成员名不另枚举，取
    # worktree.layout._LAYOUT_MARKER 单一事实源（惰性导入保 doctor 叶子性）。
    try:
        from orchd.worktree.layout import _LAYOUT_MARKER as _layout_marker
    except Exception:
        _layout_marker = LAYOUT_FILE
    for name in sorted(_RUNTIME_STATE_FILES):
        if name == _layout_marker:
            continue
        p = orchd_dir / name
        if p.is_file():
            residues.append({
                "path":
                str(p),
                "type":
                "legacy_flat_residue",
                "reason":
                (f"container 布局下 flat 遗留状态文件（canonical 在 {canonical}），"
                 "--fix 备份后删除"),
                "action":
"delete",
            })
    return residues


def _detect_hook_enforcement_gaps(project_root: Path) -> list[dict[str, Any]]:
    """强制层 hook 安装与烘焙新鲜度自检（task-enforcement-degrade-audit-trail）。

    只告警、不处置：``action`` 取 ``hook_manual``（∈ ``_MANUAL_ACTIONS``），
    --fix 与 auto_clean 恒跳过——删 hook 等于自毁强制层。

    零误报门控：仅当 pre-commit 存在本引擎归属标记（claim 活跃仓）时才检查；
    裸 git 仓 / CI 新克隆（无 hook 安装）直接返回空——未启用引擎 hook 的仓库
    里「强制层不存在」不是异常。

    两项检查（均为可观测留痕，不改变放行语义）：
    - hook_installed：pre-commit 在位但 reference-transaction hook 缺失或无
      引擎归属标记 → 引用事务无强制层，有推送权的 git 直写不受限；
    - hook_baked_fresh：已安装但烘焙值死亡（解释器/源码根已不存在）而当前
      可定位引擎，或项目已多线但缺 ORCHD_LINE_TASK_PREFIX → 重装恢复。
      刻意不比较「烘焙值与当前重算值是否逐字相等」：hooks 目录多 worktree
      共享，逐字比对会在任务 worktree 里恒报（烘焙指向主工作树），属噪声；
      只判「死亡」与「缺失」两类真失效。
    """
    try:
        from orchd.gitops import hook as hook_mod

        root = Path(project_root)
        pre_commit = hook_mod._get_hook_path(root)
        if not (pre_commit.is_file() and hook_mod._is_orchd_hook(pre_commit)):
            return []
        ref_path = hook_mod._get_ref_tx_hook_path(root)
        try:
            head = ref_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            head = []
        scan = "\n".join(head[:hook_mod._HOOK_MARKER_SCAN_LINES])
        try:
            head_pc = pre_commit.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            head_pc = []
        if hook_mod._REF_TX_HOOK_MARKER not in scan:
            return [{
                "path": str(ref_path),
                "type": "hook_not_installed",
                "reason": ("强制层 reference-transaction hook 缺失或无引擎归属标记"
                           "（pre-commit 引擎 hook 在位）：引用事务无强制层，"
                           "有推送权的 git 直写不受限；重装 hook 恢复"),
                "action": "hook_manual",
            }]

        def _unquote(value: str) -> str:
            value = value.strip()
            if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
                return value[1:-1].replace("'\\''", "'")
            return value.strip("'\"")

        baked: dict[str, str] = {}
        for line in head:
            if line.startswith(f"{ORCHD_BAKED_PY_ENV}="):
                baked["py"] = _unquote(line.split("=", 1)[1])
            elif line.startswith(f"{ORCHD_BAKED_SRC_ENV}="):
                baked["src"] = _unquote(line.split("=", 1)[1])
        causes: list[str] = []
        exp_py, exp_src = hook_mod._baked_hook_endpoints(root)
        if "py" not in baked and exp_py is not None and exp_src is not None:
            causes.append("安装期未烘焙解释器与引擎路径，而当前可定位（重装恢复快道）")
        else:
            if baked.get("py") and not Path(baked["py"]).is_file():
                causes.append(f"烘焙解释器已不存在：{baked['py']}")
            src = baked.get("src")
            if src and not (Path(src) / "orchd" / "gitops" / "ref_tx_hook.py").is_file():
                causes.append(f"烘焙引擎源码根已失效：{src}")
        exp_line, _exp_trunk = hook_mod._line_ref_tx_env(root)
        if exp_line and ORCHD_LINE_TASK_PREFIX_ENV not in "\n".join(head):
            causes.append("项目已启用多线但 hook 未烘焙线命名空间前缀（回退单线口径）")
        # task-precommit-hook-bake-guard：**pre-commit** 也纳入烘焙新鲜度判据面——
        # 此前只判 ref-tx，pre-commit 的烘焙引擎路径死亡不报警（本卡根因的 doctor 盲区）。
        # 判据按「candidate 链语义」：hook 文本内 ${_ORCHD_SRC:-<baked>} 的兜底值，
        # 或当前仓候选（<root>/.orchd 与仓库根 <root> 两处）任一含 orchd 包即视为可救；
        # 两者皆无才报 stale。镜像 shell 侧候选链第三支 $_ROOT，避免根布局自托管误报。
        import re as _re

        _pc_head = "\n".join(head_pc)
        _m = _re.search(r"\$\{_ORCHD_SRC:-([^}]*)\}", _pc_head)
        if _m:
            _baked_pc = _m.group(1).strip().strip('"').strip("'")
            # 口径（task-hook-degrade-hint-truth ③）：doctor 判「可否救援」取更强证据——
            # 用 `orchd/__init__.py`（文件存在）严格判；与 py 候选链（templates.py 用
            # `orchd/` 目录宽松判）有意不同；两候选都无才报 stale。
            _cur_ok = ((root / ".orchd" / "orchd" / "__init__.py").is_file()
                       or (root / "orchd" / "__init__.py").is_file())
            _baked_ok = bool(_baked_pc) and (
                Path(_baked_pc) / "orchd" / "__init__.py").is_file()
            if _baked_pc and not _cur_ok and not _baked_ok:
                causes.append(f"pre-commit 烘焙引擎路径已失效且当前仓无引擎：{_baked_pc}")

        if not causes:
            return []
        return [{
            "path": str(ref_path),
            "type": "hook_baked_stale",
            "reason": "强制层 hook 烘焙陈旧：" + "；".join(causes),
            "action": "hook_manual",
        }]
    except Exception:
        return []


def _detect_stray_ledgers(project_root: Path) -> list[dict[str, Any]]:
    """空壳/野账本（P2 自愈）：容器根、主工作树根、task worktree 根、孤儿 runtime
    副本与 flat 遗留；canonical 账本根不计（活账本）。auto_clean 移入回收桶，只搬不删。"""
    from orchd.ledger import resolve_store_dir
    from orchd.names import (LEDGER_ARCHIVE_FILE, LEDGER_FILE, LEDGER_SQLITE_FILE,
                             RUNTIME_DIR)
    from orchd.worktree import detect_layout

    def _files(directory: Path) -> list[Path]:
        return [p for p in (directory / n for n in
                            (LEDGER_FILE, LEDGER_ARCHIVE_FILE, LEDGER_SQLITE_FILE))
                if p.is_file()]

    def _item(path: Path, reason: str) -> dict[str, Any]:
        return {"path": str(path), "type": "stray_ledger",
                "action": "move_to_backup",
                "reason": f"{reason}；auto_clean 移入 .recycle-journal 回收桶"}

    residues: list[dict[str, Any]] = []
    orchd_dir = (Path(project_root) / ".orchd").resolve()
    try:
        layout = detect_layout(Path(project_root))
        canonical = resolve_store_dir(orchd_dir).resolve()
    except Exception as exc:  # INV-4a：布局/账本根解析不可用
        return [_unavailable_residue("stray_ledgers", exc, orchd_dir)]
    if canonical != orchd_dir:
        residues.extend(
            _item(p, "flat 遗留野账本（账本根已重定向）")
            for p in _files(orchd_dir) if p.name != LEDGER_FILE)
    roots = [Path(project_root).resolve()]
    if layout.get("layout") == "container":
        main_wt = Path(layout["main_worktree"]).resolve()
        roots += [main_wt, main_wt.parent]
        try:
            roots += [p for p in sorted(main_wt.parent.iterdir())
                      if p.is_dir() and p.name.startswith("task-")]
        except OSError:
            pass
    for root in dict.fromkeys(roots):
        dirs = [root]
        if ((root / RUNTIME_DIR).is_dir()
                and (root / RUNTIME_DIR).resolve() != canonical):
            dirs.append(root / RUNTIME_DIR)
        for directory in dirs:
            if directory.resolve() == canonical:
                continue
            residues.extend(
                _item(p, "不在 canonical 账本根的野账本")
                for p in _files(directory))
    return residues


def _run_detector(residues: list[dict[str, Any]], detector: str,
                  project_root: Path,
                  call: Callable[[], list[dict[str, Any]]]) -> None:
    """执行单个检测器并把结果并入 ``residues``：**单项异常不得中断整轮巡检**。

    task-doctor-master-dir-contract 层 2 的口径是「巡检全程容错」：任一检测器因
    不可 stat / 不可遍历抛出 ``OSError`` 时，其余检测器照常执行，故障以
    ``unreadable`` 残留项留痕（卫生门禁 waived、不判失败）；非 ``OSError`` 的故障
    按既有 INV-4a 口径记为 ``detection_unavailable``（判失败——「没查成」不等于
    「无问题」）。

    两者都不再让 :func:`detect_residues` 整体抛异常：旧行为下任一检测器的单点故障
    会让卫生门禁整轮 12 类检测全部丢失（脚本侧统一转成一条 FAIL），连已检出的
    残留与留痕都看不到——正是本卡要消除的盲区。
    """
    try:
        items = call()
    except OSError as exc:
        residues.append(_unreadable_residue(detector, exc, project_root,
                                            stage="detector"))
        return
    except Exception as exc:  # INV-4a：检测器没跑起来 ≠ 无残留
        residues.append(_unavailable_residue(detector, exc, project_root))
        return
    residues.extend(items)


def detect_residues(project_root: Path) -> list[dict[str, Any]]:
    """扫描 orchd 运行时残留，返回待清理项列表。

    检测十八类残留：
    1. 孤儿 session 锁文件（对应 worktree 已不存在）
    2. 僵尸 session runtime 文件（超 TTL 未更新的 session 记录）
    3. 残留 intake 标记（.intake.lock 文件，无 live flock 持锁）
    4. 已误提交入 git 的锁文件（.git 目录外的 .lock 文件出现在 git ls-files 中）
    5. 幽灵任务（账本/checkpoint 派生存在但不在 ``_master.json``）
    6. stale worktree 注册（prunable，目录已删但 git 仍登记）
    7. 残留任务 worktree 目录（无 .git 登记 + 无活跃绑定，P0-19 类）
    8. flat 遗留状态文件（container 重定向生效时 main/.orchd 的历史 runtime 文件）
    9. 陈旧提案草稿（``.orchd/proposals/*.json`` 的 stem 或全部内含 id 命中已注册 id，
       判据见 :func:`classify_proposals`）
    10. 草案目录内的过程产物（``.orchd/proposals/`` 下后缀非 ``.json`` 的条目，
        判据见 :func:`stray_draft_files`）
    11. 草案目录内的非普通文件条目（目录 / 断链符号链接，判据见
        :func:`proposals_dir_entries`）——按条目类型分派、恒手动，不自动搬整树
    12. 强制层 hook 安装与烘焙新鲜度（判据见
        :func:`_detect_hook_enforcement_gaps`）——只告警，恒手动
    13. 活跃认领期的 hook 守卫缺口（判据见
        :func:`_detect_hook_guard_gaps`）——门控为「账本有活跃认领」，覆盖
        pre-commit 被卸载/被顶替/形态失效三态；只告警，恒手动
    14. 空壳/野账本（容器根 / 主工作树根 / task worktree 根 / 孤儿
        ``.orchd-runtime`` 副本 / flat 遗留）——auto_clean 移入回收桶
    15. 过期 verify tmp（``.orchd/tmp`` 下 verify basetemp 遗留，命名 + 超龄双闸，
        task-cont-autoclean-detectors）
    16. 已合并任务的 ``review/<task_id>/`` 证据目录（仅终态任务；活跃与用户内容
        跳过，task-cont-autoclean-detectors）
    17. 旧 runtime 日志（顶层 ``*.log`` 超 7 天）与超保留期 doctor 备份桶
        （保留最近 N 份；桶仅报告，真删由 ``_prune_stale_backups`` 统一执行）
    18. 已知构建缓存（限 ``.orchd/``、runtime 根与 task worktree 子树；源码树
        warm cache 不扫）
    19. 未注册超 14 天的提案草案（在途列单一真源 inflight_proposals 叠加 14 天
        mtime 闸；归档候选，task-cont-autoclean-criteria）
    20. rescue ref 只读报告（超 30 天先报告暂不自动删，task-cont-autoclean-criteria）

    运行时残留（1/2/3/8）以**共享账本根**为扫描根（与 Store 同根，见
    :func:`_resolve_runtime_dir`）：container 布局扫 ``<容器>/.orchd-runtime/``，
    flat 扫 ``<project_root>/.orchd``。

    所有检测均为只读，不执行任何写操作。返回的每项包含：
    - path: 残留文件绝对路径
    - type: 残留类型（orphan_session_lock / zombie_session / intake_lock /
      git_tracked_lock / ghost_task / stale_worktree_registry / residual_dir /
      legacy_flat_residue / stale_proposal / stray_draft_file / proposals_dir_entry /
      hook_not_installed / hook_baked_stale / hook_missing_while_active /
      hook_invalid_while_active / stray_ledger / unreadable /
      stale_verify_tmp / merged_review_dir / stale_runtime_log /
      stale_doctor_backup / build_cache / stale_rescue_ref）
    - reason: 判定依据
    - action: 建议动作（delete / delete_dir / move_to_backup / residual_dir_manual /
      git_rm_cached_then_delete / git_worktree_prune / retract_ghost /
      ghost_task_manual / hook_manual / unreadable_manual / prune_backup_manual）

    ``unreadable``（task-doctor-master-dir-contract 层 2）是「不可判定」档：单项
    路径不可 stat（不可遍历 reparse point / 断链 / 权限）时留痕，与「不存在」
    （合法态、不产项）严格区分；它恒 manual 且**不进任何自动清理通道**。
    - disposition: 处置档（auto_clean / legacy_move / manual），判据与自动清理
      通道同源（``_AUTO_CLEAN_TYPES`` / ``_LEGACY_MOVE_TYPES``，且 ``action`` 命中
      ``_MANUAL_ACTIONS`` 时恒 manual）；卫生门禁据此只对 manual 档判失败，
      auto_clean / legacy_move 档交由清理通道处置。``stale_proposal`` /
      ``stray_draft_file`` / ``proposals_dir_entry`` 恒 manual（见 :func:`_detect_stale_proposals`
      与 :func:`_detect_proposals_dir_entries` docstring 的 ①②；三者同属草案目录生命周期，
      读路径不得顺手清理）
    """
    residues: list[dict[str, Any]] = []
    orchd_dir = Path(project_root) / ".orchd"
    # 运行时残留根 = 共享账本根（container → <容器>/.orchd-runtime；flat → .orchd）
    runtime_dir = _resolve_runtime_dir(orchd_dir)

    # 1) 孤儿 session 锁文件：session-gate-*.lock 无对应活跃 worktree
    _run_detector(residues, "orphan_session_locks", project_root,
                  lambda: _detect_orphan_session_locks(project_root,
                                                       runtime_dir))

    # 2) Zombie session runtime files: sessions/*.json 判死口径同 ledger
    _run_detector(residues, "zombie_sessions", project_root,
                  lambda: _detect_zombie_sessions(runtime_dir))

    # 3) Residual intake locks: .intake.lock 无 live flock
    _run_detector(residues, "residual_intake_locks", project_root,
                  lambda: _detect_residual_intake_locks(runtime_dir))

    # 4) Git-tracked lock files: .lock files committed to git
    _run_detector(residues, "git_tracked_locks", project_root,
                  lambda: _detect_git_tracked_locks(project_root))

    # 5) 幽灵任务：账本/checkpoint 派生存在、但不在 _master.json（task-runtime-hygiene AC1）
    _run_detector(residues, "ghost_tasks", project_root,
                  lambda: _detect_ghost_tasks(project_root))

    # 6) stale worktree 注册（prunable）：--fix 执行 git worktree prune
    _run_detector(residues, "stale_worktree_registry", project_root,
                  lambda: [{
                      "path": s["path"],
                      "type": "stale_worktree_registry",
                      "reason": s.get("reason", "prunable"),
                      "action": "git_worktree_prune",
                  } for s in _detect_stale_worktree_registry(project_root)])

    # 7) 残留任务 worktree 目录（P0-19，--fix 关闭 doctor 断链：worktree_residual → delete_dir）
    _run_detector(residues, "residual_dirs", project_root,
                  lambda: _detect_residual_dirs(project_root))

    # 8) flat 遗留状态文件（container 布局下 main/.orchd 的历史 runtime 文件）
    _run_detector(residues, "legacy_flat_residues", project_root,
                  lambda: _detect_legacy_flat_residues(project_root))

    # 9) 陈旧提案草稿（已注册任务 id 的 .orchd/proposals/*.json）：判据单一事实源
    #    classify_proposals() 的陈旧列，卫生门禁的 [proposals] 检查消费本项，
    #    不再各自实现比较。
    _run_detector(residues, "stale_proposals", project_root,
                  lambda: _detect_stale_proposals(project_root))

    # 10) 草案目录内的过程产物（非 .json 条目，如一次性注册脚本）：同目录同生命周期，
    #     处置复用 9) 的 move_to_backup 档（不新增第三种处置机制）。
    _run_detector(residues, "stray_draft_files", project_root,
                  lambda: _detect_stray_draft_files(project_root))

    # 11) 草案目录内的非普通文件条目（目录 / 断链 symlink）：按条目类型分派、恒手动，
    #     不自动搬整树（task-proposals-dir-entry-disposition）。
    _run_detector(residues, "proposals_dir_entries", project_root,
                  lambda: _detect_proposals_dir_entries(project_root))

    # 12) 强制层 hook 安装与烘焙新鲜度（task-enforcement-degrade-audit-trail）：
    #     只告警（action hook_manual ∈ _MANUAL_ACTIONS），--fix 与 auto_clean
    #     恒跳过。门控为 pre-commit 引擎 hook 在位（claim 活跃仓）；裸 git 仓 /
    #     CI 新克隆无 hook 时静默（未启用引擎 hook 不是异常）。
    _run_detector(residues, "hook_enforcement_gaps", project_root,
                  lambda: _detect_hook_enforcement_gaps(project_root))

    # 13) 活跃认领期的 hook 守卫缺口（task-debug-doctor-hook-check）：门控换成
    #     「账本有活跃认领」——12) 的门控是「pre-commit 在位」，被卸载时它恒静默，
    #     恰好漏掉并行期 fail-open 这一最该报的形态。只告警、恒手动。
    _run_detector(residues, "hook_guard_gaps", project_root,
                  lambda: _detect_hook_guard_gaps(project_root))

    # 14) 空壳/野账本（P2 自愈）：auto_clean 移入 .recycle-journal 回收桶
    _run_detector(residues, "stray_ledgers", project_root,
                  lambda: _detect_stray_ledgers(project_root))

    # 15) 过期 verify tmp（task-cont-autoclean-detectors）：.orchd/tmp 下
    #     verify basetemp 遗留（命名 + 超龄双闸）；命名不匹配的用户内容跳过
    _run_detector(residues, "stale_verify_tmps", project_root,
                  lambda: _detect_stale_verify_tmps(project_root))

    # 16) 已合并任务的 review/ 证据目录：终态任务才可清；活跃/账本不可读/
    #     非任务命名的用户内容一律跳过（fail-closed）
    _run_detector(residues, "merged_review_dirs", project_root,
                  lambda: _detect_merged_review_dirs(project_root))

    # 17) 旧 runtime 日志 + 超保留期 doctor 备份桶：日志走回收桶（可回滚），
    #     备份桶仅报告（真删由 _prune_stale_backups 统一执行）
    _run_detector(residues, "stale_runtime_logs", project_root,
                  lambda: _detect_stale_runtime_logs(project_root))
    _run_detector(residues, "stale_doctor_backups", project_root,
                  lambda: _detect_stale_doctor_backups(project_root))

    # 18) 已知构建缓存（限 .orchd/、runtime 根与 task worktree 子树；源码树
    #     warm cache 不扫）
    _run_detector(residues, "known_build_caches", project_root,
                  lambda: _detect_known_build_caches(project_root))

    # 19) 未注册超 14 天的提案草案 → 归档候选（task-cont-autoclean-criteria，
    #     设计 §6.2 补口）：在途列单一真源 inflight_proposals 叠加 14 天 mtime 闸；
    #     archive_proposal 手动归档通道（只归档不删除）
    _run_detector(residues, "stale_unregistered_proposals", project_root,
                  lambda: _detect_stale_unregistered_proposals(project_root))

    # 20) rescue ref 只读报告（task-cont-autoclean-criteria，设计 §6.2）：
    #     超 30 天先报告暂不自动删（action rescue_ref_manual ∈ _MANUAL_ACTIONS）
    _run_detector(residues, "stale_rescue_refs", project_root,
                  lambda: _detect_stale_rescue_refs(project_root))

    # 与自动清理通道判据对齐（task-takeover-residue-alignment AC3）：为每项标注处置档
    # （auto_clean / legacy_move / manual），单一事实源为 _AUTO_CLEAN_TYPES /
    # _LEGACY_MOVE_TYPES——卫生门禁据此区分「清理器即将处理」与「需人工处置」，不再把
    # auto-clean 级残留当卫生失败报红（消除检测器与清理通道抢跑的抖动）。
    for item in residues:
        item.setdefault(
            "disposition",
            _residue_disposition(item.get("type"), item.get("action")),
        )
    return residues


def _ghost_detection_store(project_root: Path) -> "Store":
    """构造幽灵检测所用 store：``orchd_dir`` 必须是 **master 目录**（含 ``_master.json``）。

    契约（task-doctor-master-dir-contract 层 1）：``Store.__init__`` 的入参语义是
    「master 目录」，账本根由内部 ``_select_backend`` → ``resolve_store_dir`` 自行
    重定向。旧实现先 ``Store(resolve_store_dir(orchd_dir))`` 把**账本根**喂进去，
    于是 ``store.orchd_dir`` 变成共享账本根，而 :func:`resolve_master_path` 是按
    「store.orchd_dir 是 master 目录」消费的 → canonical 回退取
    ``store.orchd_dir.parent`` 得**容器根**（不是主工作树）→ 解析出
    ``<容器>/.orchd/_master.json``（本机不存在且不可 stat）→ 幽灵检测恒空返回。

    container 布局下 master 真源在 canonical 主工作树，故此处先归位
    :func:`resolve_canonical_project_root`（flat 下返回自身 ⇒ 零回归）。账本文件
    路径与锁路径由 Store 内部派生，与旧写法逐字一致（等价性见
    ``tests/test_doctor.py::TestStoreContractNormalization``）。
    """
    from orchd.ledger import Store
    from orchd.worktree import resolve_canonical_project_root

    orchd_dir = resolve_canonical_project_root(Path(project_root)) / ".orchd"
    return Store(orchd_dir, readonly=True)


def _detect_ghost_tasks(project_root: Path) -> list[dict[str, Any]]:
    """检测幽灵任务：账本/checkpoint 派生存在、但不在 ``_master.json`` 的任务。

    成因（2026-09-10 附录 D 补录）：测试夹具（如 pytest 泄漏的 ``task-2``）的
    CLAIMED 事件落进共享账本根，但任务从未注册进 ``_master.json``；于是
    ``status`` 不可见，``watchdog`` 却按僵死判定返回 exit 1，而
    ``force-status`` / ``retract`` 的常规通道对"不在 master 的 id"不可用
    （retract 另有跨 agent 归属守卫 E034）。

    本检测为**只读**；清理走 ``doctor --fix`` 的 ``retract_ghost`` 分支——
    以引擎保留的 ``admin`` 控制面撤回该任务的孤儿事件，使 replay 与 master 一致。

    终态排除（DR-5 修正，2026-09-15）：``completed`` / ``cancelled`` 任务**不是**
    幽灵——master 归档移除历史任务（或测试夹具清理）后，账本里保留的终态是该任务
    的最终事实，归档只影响任务清单可见性。旧实现不排除终态，``--fix`` 会级联撤回
    其 DONE / REVIEW 事件，抹除合法完成历史（不可逆）。

    兼容性：仅当解析出的 master（单一真源，task-master-path-single-source：
    本地副本优先 + canonical 主工作树回退）存在且可读时才检测；master **不存在**
    属正常（无 master 的最小场景）→ 返回空。ledger / master **读取失败**则不是
    「无幽灵」，而是「检测不可用」→ 返回 ``detection_unavailable`` 残留项
    （INV-4a：旧实现静默返回空，账本损坏时幽灵任务彻底不可见，且 --fix 无从提示）。
    container 布局下本地副本缺失不再静默空返回——经 resolve_master_path
    归位 canonical 主工作树后判定，与共享账本根口径对齐。

    store 契约（task-doctor-master-dir-contract 层 1）：经
    :func:`_ghost_detection_store` 构造，入参是 **master 目录**（container 下 =
    canonical 主工作树 ``.orchd``），不再是共享账本根。

    「不存在」与「不可解析」分道（层 2）：master 路径 stat 抛 ``OSError``
    （WinError 448 类不可遍历项）时返回 ``unreadable`` 残留项**留痕**，不再当作
    「本仓库无 master」静默放行。
    """
    residues: list[dict[str, Any]] = []
    try:
        from orchd.worktree import resolve_master_path

        store = _ghost_detection_store(Path(project_root))
        master_path = resolve_master_path(store)
    except Exception as exc:  # INV-4a：检测不可用（此前静默空 → 幽灵不可见）
        return [_unavailable_residue("ghost_tasks", exc, Path(project_root))]
    is_file, stat_exc = _probe_is_file(master_path)
    if stat_exc is not None:
        # 不可 stat ≠ 不存在：把 448 当「无 master」正是静默失明的成因
        return [_unreadable_residue("ghost_tasks", stat_exc, master_path,
                                    stage="master_stat")]
    if not is_file:
        return residues
    try:
        from orchd.spec import load_master

        master = load_master(master_path)
        master_ids = {t["id"] for t in master.tasks}
        state = store.replay()
        events = store.backend.read_events()
    except Exception as exc:  # INV-4a：账本/master 不可读 → 幽灵检测不可用（非"无幽灵"）
        return [_unavailable_residue("ghost_tasks", exc, master_path)]

    # 按任务归集事件，用于定位可撤回的起点事件
    events_by_task: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        _tid = ev.get("task_id")
        if _tid:
            events_by_task.setdefault(_tid, []).append(ev)

    for task_id in sorted(state):
        if task_id in master_ids:
            continue
        if getattr(state[task_id], "status", None) in _GHOST_TERMINAL_STATUSES:
            # DR-5：终态任务不是幽灵（master 归档只影响清单可见性，账本终态是最终
            # 事实）。不报告 → --fix 不会级联撤回其 DONE/REVIEW，合法完成历史得以保留。
            continue
        target_event_id = _ghost_retract_target(events_by_task.get(
            task_id, []))
        if target_event_id is None:
            # 仅含 FORCE_STATUS（防篡改不可撤）→ 无法自动清理，单列需人工处置
            residues.append({
                "path":
                f"ledger#{task_id}",
                "task_id":
                task_id,
                "type":
                "ghost_task",
                "reason": (f"任务 {task_id} 存在于账本但不属于 _master.json，且其事件"
                           f"均为不可撤的 FORCE_STATUS（防篡改保护）——需人工/admin 专案处置"),
                "action":
                "ghost_task_manual",
            })
            continue
        residues.append({
            "path":
            f"ledger#{task_id}",
            "task_id":
            task_id,
            "type":
            "ghost_task",
            "reason": (f"任务 {task_id} 存在于账本/checkpoint 但不在 _master.json"
                       f"（幽灵任务，watchdog 会误判僵死）"),
            "action":
            "retract_ghost",
            "event_id":
            target_event_id,
        })

    # 孤儿模块快照（debug-pack C2）：<store_root>/<mod_id>/spec.json 的模块已不在
    # master.modules——快照目录无账本可依，原检测以 master 为视野，看不见这类残留。
    try:
        from orchd.ledger import resolve_store_dir

        _master_modules = {m.get("id", "") for m in getattr(master, "modules", [])}
        _store_root = resolve_store_dir(Path(project_root) / ".orchd")
        if _store_root.is_dir():
            for _d in sorted(_store_root.iterdir()):
                if not _d.is_dir() or not (_d / "spec.json").is_file():
                    continue
                if _d.name in _master_modules:
                    continue
                residues.append({
                    "path": str(_d / "spec.json"),
                    "type": "orphan_module_snapshot",
                    "reason": (f"模块快照 {_d.name}/spec.json 不在 master.modules"
                               f"（已移出 master 的旧模块快照残留）"),
                    "action": "orphan_module_manual",
                })
    except Exception as exc:  # INV-4a：检测不可用须留痕，不得静默当"无残留"
        residues.append(_unavailable_residue("orphan_module_snapshots", exc,
                                              Path(project_root)))

    return residues


def _ghost_retract_target(events: list[dict[str, Any]]) -> str | None:
    """为幽灵任务挑选可撤回的**最早**事件 id（retract 自该事件起级联撤回其后事件）。

    DR-6 修正（2026-09-15）：旧实现优先返回首个 ``CLAIMED``——若该 CLAIMED 已被
    撤回（撤回后重领场景：``CLAIMED#1 → … → RETRACT(CLAIMED#1) → CLAIMED#2 …``），
    再撤一次已撤回事件只会**追加重复 RETRACT**（账本膨胀），且新链上的事件仍在
    replay 派生结果中 → 幽灵不收敛，每次 ``--fix`` 都再写一轮。

    现判据：
    - 跳过 ``RETRACT`` 事件本身与已被 RETRACT 撤回的事件（``target_event_id`` 集合）；
    - 跳过 ``FORCE_STATUS``（引擎防篡改，retract 恒拒 E007）；
    - 返回**剩余事件中最早的一条**，保证级联撤回后该 ``task_id`` 从 replay 消失。

    事件全为 RETRACT / FORCE_STATUS 时返回 None，调用方降级为需人工处置。
    """
    already_retracted = {
        ev.get("target_event_id")
        for ev in events if ev.get("type") == EVENT_RETRACT
    }
    for ev in events:
        if ev.get("type") in (EVENT_RETRACT, EVENT_FORCE_STATUS):
            continue
        if ev.get("event_id") in already_retracted:
            continue
        return ev.get("event_id")
    return None


# ---------------------------------------------------------------------------
# 自动清洁（task-doctor-auto-clean）
# ---------------------------------------------------------------------------
# 分级模型（与手动 doctor --fix 共享 detect_residues 单一事实源）：
#   Auto-Clean（低风险，直接处理）：
#     orphan_session_lock / zombie_session → 直接删文件（可再生，无备份）
#     residual_dir → 移入 .doctor-backup/residual/<ts>/（可回滚；搬移失败显式
#                   标记失败并保留源，不强删；含真实内容者不处置，转 manual 档）
#     stale_worktree_registry → git worktree prune
#     stray_ledger → 移入 <store_root>/.recycle-journal/stray-ledger/<ts>/（可回滚；
#                    只搬不删，同桶 manifest 记 sha256 供逐字节回滚）
#   Legacy move（可回滚）：legacy_flat_residue → 移入 .doctor-backup/legacy/<ts>/ 滚动区
_AUTO_CLEAN_TYPES = frozenset({
    "orphan_session_lock",
    "zombie_session",
    "residual_dir",
    "stale_worktree_registry",
    "stray_ledger",
    # task-cont-autoclean-detectors 五类扩展（均可回滚处置，详见各检测器 docstring）
    "stale_verify_tmp",
    "merged_review_dir",
    "stale_runtime_log",
    "build_cache",
})
_LEGACY_MOVE_TYPES = frozenset({"legacy_flat_residue"})

# 恒需人工处置的动作（disposition 固定 manual，不受 type 分级影响）：
#   detection_unavailable 必须在此集合内：doctor --fix 的分派链对**未登记**的 action
#   会落到末尾默认删除分支（见 doctor_fix 循环尾部），而该残留项的 path 是检测根
#   （可能是 .orchd/ 或项目根目录）→ 不登记等于给 --fix 埋一颗删除地雷。
#   task-detection-fail-closed-doctor-ledger 的负控制用例钉住此约束。
#   ghost_task_manual    — 事件全为 FORCE_STATUS 的幽灵任务（防篡改保护，无自动通道）；
#   residual_dir_manual  — 含真实工作痕迹的残留目录（DR-3：自动通道明确拒绝删除）。
_MANUAL_ACTIONS = frozenset({
    "ghost_task_manual", "residual_dir_manual",
    # 草案目录内非普通文件条目（目录 / 断链 symlink）：保守取向，任何目录形态都不
    # 自动搬整树（task-proposals-dir-entry-disposition）。
    "proposals_dir_manual",
    # INV-4a：检测不可用项（无自动通道、只能人工核对；且必须在 fix 分派前被截住，
    # 否则会落入默认删除分支）
    _DETECTION_UNAVAILABLE_RESIDUE,
    # 不可 stat / 不可判定项（task-doctor-master-dir-contract 层 2）：不可判定
    # 的目录项/路径绝不能被自动搬走或删除——登记人工档是硬约束，不是策略选择。
    _UNREADABLE_ACTION,
    # 强制层 hook 自检项（task-enforcement-degrade-audit-trail）：只告警，
    # --fix 与 auto_clean 恒跳过（删 hook 等于自毁强制层）。
    "hook_manual",
    # 孤儿模块快照（debug-pack C2）：已移出 master 的 mod-*/spec.json；清理走
    # re-init 对账（C3）与人工确认，--fix 不自动删（防误删在用快照）。
    "orphan_module_manual",
    # 超 N 份 doctor 备份桶（task-cont-autoclean-detectors）：报告面——真删由
    # auto_clean 收尾的 _prune_stale_backups 统一执行，读路径与 --fix 都不动作。
    "prune_backup_manual",
    # rescue ref 只读报告（task-cont-autoclean-criteria，设计 §6.2）：L2 脏数据
    # 救援的唯一副本不入库不推送，误删不可恢复——先报告暂不自动删，人工确认
    # 后处置（未登记 action 会落 --fix 默认删除分支，登记是硬安全约束）。
    "rescue_ref_manual",
})


def _residue_disposition(rtype: str | None, action: str | None = None) -> str:
    """残留项处置档：``auto_clean`` / ``legacy_move`` / ``manual``。

    判据与自动清理通道同源（``_AUTO_CLEAN_TYPES`` / ``_LEGACY_MOVE_TYPES``）：
    - ``auto_clean``  ：读路径（status / watchdog）的 ``auto_clean`` 会直接处置；
    - ``legacy_move`` ：移入 ``.doctor-backup/legacy/<ts>/`` 滚动备份区；
    - ``manual``      ：无自动通道、仅报告（ghost_task / git_tracked_lock /
      intake_lock，以及 ``action`` 命中 ``_MANUAL_ACTIONS`` 的项）。

    ``action`` 命中 ``_MANUAL_ACTIONS`` 时**恒 manual**，即使其 ``type`` 属 auto_clean
    档——例如含真实内容的残留目录（``residual_dir`` + ``residual_dir_manual``）：检测
    得到、但自动通道明确拒绝处置，若按 type 归入 auto_clean 会被卫生门禁豁免而永久
    静默滞留（「检测说可清、清理器不做」）。故此处以 action 覆盖 type 分级。

    卫生门禁（``scripts/verify_project_hygiene.py``）只对 ``manual`` 档失败，
    避免「检测器报清理器即将删除之物」的抖动（residue-report-autoclean-alignment）。

    ``detection_unavailable``（INV-4a）未登记进 type 分级集合，落默认 ``manual`` 档
    ——「没能判定」不能被当成可自动清理，也不能被卫生门禁豁免。
    """
    if action in _MANUAL_ACTIONS:
        return "manual"
    if rtype in _AUTO_CLEAN_TYPES:
        return "auto_clean"
    if rtype in _LEGACY_MOVE_TYPES:
        return "legacy_move"
    return "manual"


def _detect_hook_guard_gaps(project_root: Path) -> list[dict[str, Any]]:
    """活跃认领期的 hook 守卫缺口（task-debug-doctor-hook-check）。

    与 :func:`_detect_hook_enforcement_gaps` 的分工：那条以「pre-commit 在位」为门控，
    判强制层缺失/烘焙陈旧；本检测器把门控改为**账本是否存在活跃认领**——因为
    「pre-commit 被卸载」恰恰是本类事故的形态，旧门控在这种情况下静默 ``return []``，
    把最该报的状态判成无问题（2026-10-30 实测：并行会话 merge 完成即删共享 hook，
    窗口期 L3 越界校验 fail-open，而 doctor 全程无感）。

    三态（只告警，``action=hook_manual`` 恒手动；--fix / auto_clean 不得删 hook）：
    - **实现者 claimed** + pre-commit 缺失 → ``hook_missing_while_active``；
    - **实现者 claimed** + 在位但内容自检失败 / 无引擎归属标记 / POSIX 下无执行位
      → ``hook_invalid_while_active``；
    - **实现者 claimed** + hook 运行时不可用（无 sh / sh -c 探测失败）或存在
      degraded 认领记录 → ``hook_runtime_unavailable``（不报「缺失」）；
    - **reviewer-only in_review**（实现者已 done）→ 不产项：该阶段不再有实现期提交；
    - 无实现者认领 → 不产项（空池常态）；
    - git 答不出 hooks 目录（权限拒绝 / 超时 / 非仓库）→ 不判「缺失」（那是真相为
      **未配置**时的结论），改以 ``detection_unavailable`` 留痕，人工核对。

    Args:
        project_root: 仓库根（``.orchd`` 所在层）。

    Returns:
        残留项列表（0 或 1 项）；任何异常按 ``_run_detector`` 口径留痕，不在此吞掉。
    """
    import os as _os

    from orchd.gitops import hook as hook_mod
    from orchd.gitops.hook import paths as hook_paths
    # 经已登记的 ``gitops.hook`` 边取 install 域（子模块属性），避免新增依赖边。
    hook_install = hook_mod.install
    HOOK_RUNTIME_UNAVAILABLE = hook_install.HOOK_RUNTIME_UNAVAILABLE

    root = Path(project_root)
    # ``ORCHD_HOME`` 在 ``resolve_store_dir`` 里**无条件短路**（ledger/paths.py），优先于
    # 任何布局推导：此时读到的可能是别仓的账本，用它判本仓 hook 会假红（别仓有活跃认领
    # → 本仓报「缺失」），而 canonical 对齐管不到这条短路。显式不适用，优于误报。
    if _os.environ.get(ORCHD_HOME_ENV):
        return []
    try:
        from orchd.gitops.hook.active_gate import active_claim_tasks
        from orchd.worktree import resolve_canonical_project_root

        # 归位 canonical 主工作树：container 布局下账本真源在容器级 runtime 根，
        # 从任务 worktree 或容器根调用时 `<root>/.orchd` 里没有账本 → 直接判「无活跃」
        # 会恒漏报（正是本卡要消除的盲区）。范式同同文件 `_ghost_detection_store`。
        # 判定谓词单一来源 = active_gate.active_claim_tasks（含只读探测）。
        canonical = resolve_canonical_project_root(root)
        available, active, _error = active_claim_tasks(canonical / ".orchd")
    except Exception:
        return []
    if not available or not active:
        return []
    # AC3：只对**实现者 claimed** 报缺失——reviewer-only in_review 阶段实现者已 done，
    # 不再有实现期提交，报 missing 属误报（旧实现把 in_review+review_claimed 当活跃）。
    implementers = [t for t in active if t.get("status") == STATUS_CLAIMED]
    if not implementers:
        return []
    active_ids = [str(t.get("task_id", "")) for t in implementers]
    # hook 路径按 canonical 主工作树解析：container 下 core.hooksPath 是绝对路径、
    # 各工作树同指一处；从容器根（非 git）调用时按 root 会回退 `<容器>/.git/hooks`
    # 而恒报缺失（假红）。
    hooks_dir, fail = hook_paths._resolve_hooks_dir_checked(canonical, "doctor_hook_guard")
    if fail is not None:
        if not (canonical / ".git").exists():
            # nogit 单目录模式（task-nogit-single-dir-pivot）：无 git hook 强制层可言。
            return []
        # 「检测不可用」而非「hook 缺失」：探测失败时按 .git/hooks 猜，两种真假都可能
        # 误判——AC③ 要求 hook_missing_while_active 只在真相为未配置时触发。
        return [_unavailable_residue("hook_guard_gaps",
                                     RuntimeError(fail.get("error") or "git 探测失败"),
                                     canonical)]
    pre_commit = Path(hooks_dir) / hook_mod._HOOK_FILENAME
    listed = "、".join(active_ids)
    # 运行时不可用与「缺失」分道（AC3）：hook 由 sh 执行，运行时不可用时即便文件在位
    # git 也会静默跳过（fail-open）——此时报 hook_runtime_unavailable 而非 missing。
    degraded = hook_install.read_hook_runtime_degraded(canonical)
    if degraded is not None and str(degraded.get("task_id") or "") in active_ids:
        return [{
            "path": str(pre_commit),
            "type": HOOK_RUNTIME_UNAVAILABLE,
            "reason": (
                f"账本仍有实现者认领（{listed}），且存在 degraded 认领记录"
                f"（task={degraded.get('task_id')}，recorded_at="
                f"{degraded.get('recorded_at')}）：hook 运行时不可用"
                f"（{degraded.get('error') or 'sh 不可用'}）——L3 越界校验（E020）"
                "当前 fail-open；修复 sh 后重新 claim 重烘焙"
            ),
            "action": "hook_manual",
        }]
    runtime = hook_install.probe_hook_runtime()
    if not runtime.get("ok"):
        return [{
            "path": str(pre_commit),
            "type": HOOK_RUNTIME_UNAVAILABLE,
            "reason": (
                f"账本仍有实现者认领（{listed}），但 hook 运行时不可用"
                f"（{runtime.get('error') or 'sh 不可用'}）：git 会静默跳过 pre-commit，"
                "L3 越界校验（E020）等于 fail-open；安装 sh 或设置 ORCHD_HOOK_SHELL "
                "后重新 claim 重烘焙"
            ),
            "action": "hook_manual",
        }]
    if not pre_commit.is_file():
        return [{
            "path": str(pre_commit),
            "type": "hook_missing_while_active",
            "reason": (
                f"账本仍有活跃认领（{listed}），但共享 pre-commit hook 不存在："
                "活跃期提交的越界校验（E020）等于 fail-open；重新 claim 任意任务即重烘焙"
            ),
            "action": "hook_manual",
        }]
    try:
        content = pre_commit.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{
            "path": str(pre_commit),
            "type": "hook_invalid_while_active",
            "reason": (f"账本仍有活跃认领（{listed}），但 pre-commit 不可读：{exc}"),
            "action": "hook_manual",
        }]
    problems: list[str] = []
    check = hook_mod._validate_hook_content(content)
    if not check.get("ok"):
        problems.append(
            f"形态自检未通过（{check.get('reason') or 'invalid'}）：{check.get('error', '')}")
    # 注：归属标记 / shebang / NUL / CR 已由 _validate_hook_content 统一判定
    # （missing_marker / missing_shebang / nul_byte / cr_present），此处不得再写一份
    # ——重复判据会让「删掉其一仍全绿」的假守卫（变异自证 M4 实测）。宿主 hook 顶替
    # 的识别因此也走同一判据（missing_marker），不在本检测器另立分支。
    if _os.name == "posix" and not _os.access(pre_commit, _os.X_OK):
        problems.append("无执行位（git 会静默跳过该 hook，等于未启用）")
    if not problems:
        return []
    return [{
        "path": str(pre_commit),
        "type": "hook_invalid_while_active",
        "reason": (f"账本仍有活跃认领（{listed}），但 pre-commit hook 不可用："
                   + "；".join(problems) + "；重装 hook 恢复"),
        "action": "hook_manual",
    }]
