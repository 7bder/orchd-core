"""lifecycle.engine —— 自 ``orchd/worktree/lifecycle.py`` 拆出。

vendored 引擎传播与容器标记（_engine_manifest / _propagate_* / _git_minor_version）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchd.worktree.layout import write_layout

def _engine_manifest(eng_dir: Path) -> dict[str, tuple[int, str]] | None:
    """引擎目录内容指纹（task-vendored-engine-integrity）：``{相对路径: (size, sha256)}``。

    主从 worktree 的引擎是否同版，不能看 ``orchd.__version__``（包元数据随环境，
    两侧恒同）。manifest 按 ``*.py`` 相对路径 + 大小 + 内容 sha256 判定：
    升级新增/改写任一文件即失配（mtime 不可靠：拷贝/检出会刷新时间戳）。
    目录缺失 → None。
    """
    try:
        import hashlib

        root = Path(eng_dir)
        if not (root / "__init__.py").is_file():
            return None
        out: dict[str, tuple[int, str]] = {}
        for p in sorted(root.rglob("*.py")):
            try:
                data = p.read_bytes()
                out[p.relative_to(root).as_posix()] = (
                    len(data), hashlib.sha256(data).hexdigest())
            except OSError:
                continue
        return out
    except OSError:
        return None


def _propagate_vendored_engine(main_wt: Path, task_wt: Path) -> dict[str, Any]:
    """把主工作树 vendored 引擎同步进任务 worktree（best-effort，永不抛异常）。

    背景（task-worktree-vendored-engine-propagate，2026-09-21 实测）：宿主项目
    的引擎唯一副本是安装器组装的 ``.orchd/orchd/``，而安装器 ``.orchd/.gitignore``
    （``/*`` + 豁免集，不含 ``orchd/``）使其不入库；``git worktree add`` 只带已跟踪
    文件 → 新 worktree 内 ``python .orchd/__main__.py`` 因 ``import orchd`` 失败
    （ModuleNotFoundError → E999），引擎在 worktree 内跑不动自己。

    语义：
    - 主工作树无源（自托管仓：根 ``orchd/`` 已跟踪，worktree 自带分支引擎）→ 无动作；
    - 目标缺引擎 → 整目录拷贝（排除 ``__pycache__``），不入库；
    - 目标已有引擎 → 比对内容指纹（task-vendored-engine-integrity）：失配
      （主工作树升级后长寿 worktree 陈旧）即删后重拷；一致则跳过。
    拷贝物恒为 untracked（安装器忽略契约；宿主若自行跟踪则目标已存在走跳过分支），
    故不产生已跟踪改动、不触发 E017。

    Returns:
        ``{"ok": bool, "method": "copied"|"exists"|"not_needed"|"recopied", "reason": str|None}``；
        拷贝失败 → ``{"ok": False, ...}``（调用方以 degraded 透出，禁止静默）。
    """
    try:
        src = Path(main_wt) / ".orchd" / "orchd"
        dst = Path(task_wt) / ".orchd" / "orchd"
        if not (src / "__init__.py").is_file():
            return {"ok": True, "method": "not_needed",
                    "reason": "主工作树无 vendored 引擎（自托管布局，根 orchd/ 已跟踪）"}
        if (dst / "__init__.py").is_file():
            if _engine_manifest(src) == _engine_manifest(dst):
                return {"ok": True, "method": "exists", "reason": None}
            try:
                shutil.rmtree(dst)
            except OSError as exc:
                return {"ok": False, "method": "recopy_failed",
                        "reason": f"陈旧引擎删除失败：{exc}"[:200]}
            method = "recopied"
        else:
            method = "copied"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            str(src), str(dst),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        return {"ok": True, "method": method, "reason": None}
    except (OSError, shutil.Error) as exc:
        return {"ok": False, "method": "copy_failed", "reason": str(exc)[:200]}


def _propagate_container_marker(task_wt: Path, main_wt: Path) -> None:
    """把 container 布局标记写入任务 worktree 的 ``.orchd/``（best-effort）。

    端到端修复（task-14-layout-migrate-junk-clean 实测暴露）：任务 worktree 是
    git checkout，布局标记（``.layout.json``）被 gitignore 未跟踪 → 从 worktree
    执行 orchd 命令时 ``resolve_store_dir`` 读不到标记，回退到 worktree 本地空
    账本，共享任务状态不可见（done 报 "not in claimed"）。写入标记后 worktree
    自识别 container 布局 → 共享账本根（``<容器>/.orchd-runtime/``）生效。
    失败静默降级（仍可经 ORCHD_HOME 指向共享账本根）。

    ROADMAP 不再物化（task-roadmap-no-materialize）：ROADMAP 是宿主资产、
    唯一源 = 宿主项目根（``ledger.resolve_roadmap_path`` 先 canonical 化），
    消费者（roadmap_landing_warnings / roadmap-land / intake / E025 溯源）
    均已走该单一真源，不读 worktree 本地副本。历史拷贝块已删除——它在
    ROADMAP 被 git 跟踪的宿主下会覆写任务分支已提交的基线回写，再被
    done 的 ensure_committed 当改动提交固化。worktree 自带的 tracked
    检出副本保留原样（不删不碰），仅不再覆盖。
    """
    try:
        orchd = task_wt / ".orchd"
        orchd.mkdir(parents=True, exist_ok=True)
        write_layout(orchd, "container", main_wt)
    except Exception:
        pass


def _git_minor_version() -> int:
    """解析 git 版本为 ``major*100 + minor``（如 2.25 → 225、3.0 → 300）。

    W-14（2026-09-15）：旧实现只返回次版本号（``2.25 → 25``），丢主版本号 ——
    git 3.x 时 ``3.0 → 0`` 会把 ``version >= 25`` 判为**假**，静默关闭
    sparse-checkout（能力探测方向反了）。改口径后比较须用 ``>= 225``。
    解析失败返回 0（按不可用处理）。
    """
    try:
        out = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        ).stdout or ""
        m = re.search(r"(\d+)\.(\d+)", out)
        if not m:
            return 0
        return int(m.group(1)) * 100 + int(m.group(2))
    except Exception:
        return 0


def _merge_vendored_engine_entry(
    result: dict[str, Any], main_wt: Path, wt_path: Path
) -> None:
    """把 vendored 引擎同步段并入 ``ensure_task_wt`` 结果（就地）。

    ``degraded`` 取或、原因串拼接——禁止吞掉 ``_master_suppression_entry`` 的抑制
    告警（两段共用 ``degraded`` / ``degraded_reason`` 键，直接 update 会互吞）。
    同步失败（源存在但拷不过去）才置 degraded；``exists`` / ``not_needed`` /
    ``copied`` 仅留 ``vendored_engine`` 明细段。
    """
    prop = _propagate_vendored_engine(main_wt, wt_path)
    result["vendored_engine"] = prop
    if prop.get("ok") is False:
        result["degraded"] = True
        reason = (
            f"vendored 引擎同步失败（{prop.get('method')}）：{prop.get('reason')}"
        )
        prev = result.get("degraded_reason")
        result["degraded_reason"] = f"{prev}；{reason}" if prev else reason
