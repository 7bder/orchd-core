"""任务 worktree 生命周期（task-14-worktree-lifecycle）：vendored 引擎 +
建（ensure）+ 头守卫 + 旧版清理。依赖方向：同包 layout/bindings 原语。
"""


from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchd.worktree.bindings import _task_wt_name
from orchd.worktree.layout import _GIT_TIMEOUT, detect_layout, write_layout


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


def _task_branch_divergence(
    project_root: Path, branch: str, base: str
) -> dict[str, int] | None:
    """任务分支相对主分支的超前/落后计数（best-effort，测不到返回 None）。

    - ``ahead``：分支独有提交数（``base..branch``）；
    - ``behind``：主分支独有提交数（``branch..base``）。
    任一 git 调用失败 → None（调用方按"无法判定"处理，不阻断复用）。
    """
    try:
        ahead = subprocess.run(
            ["git", "rev-list", "--count", f"{base}..{branch}"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        behind = subprocess.run(
            ["git", "rev-list", "--count", f"{branch}..{base}"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if ahead.returncode != 0 or behind.returncode != 0:
            return None
        return {"ahead": int(ahead.stdout.strip()), "behind": int(behind.stdout.strip())}
    except (subprocess.SubprocessError, FileNotFoundError, OSError, ValueError):
        return None


def _recreate_task_wt_from_base(
    project_root: Path, wt_path: Path, branch: str, base: str
) -> bool:
    """纯陈旧分支的安全重建（仅调用方确认 ahead == 0 后调用）。

    ``git worktree remove --force`` + ``git branch -d``（安全删除：ahead == 0
    意味着分支 tip 已合入 base，``-d`` 必成功；用 ``-d`` 不用 ``-D``，语义即断言）。
    任一步失败 → False（调用方回退复用 + degraded 留痕，禁止半截状态）。
    """
    try:
        rm = subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if rm.returncode != 0:
            return False
        delete = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        return delete.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _check_stale_baseline(
    project_root: Path, branch: str, wt_path: Path
) -> dict[str, Any] | None:
    """复用路径的基线新鲜度判定（task-claim-stale-baseline-gate）。

    Returns:
        - ``None``：新鲜（behind == 0）或无法判定（base 未知 / git 异常）——
          调用方照常复用；
        - ``{"action": "reuse", "warning": {...}}``：分叉且含独有提交——调用方挂
          ``stale_baseline_warning`` 后复用；
        - ``{"action": "recreate", "base": str, "behind": int}``：纯陈旧指针——
          调用方安全重建后落创建流。

    ``wt_path`` 仅作签名对称（未来可校验 worktree 实指分支），当前未使用。
    """
    try:
        from orchd.gitops import get_default_branch

        base = get_default_branch(project_root)
    except Exception:
        return None
    if not base:
        return None
    div = _task_branch_divergence(project_root, branch, base)
    if div is None or div["behind"] == 0:
        return None
    if div["ahead"] > 0:
        return {
            "action": "reuse",
            "warning": {
                "behind": div["behind"],
                "ahead": div["ahead"],
                "branch": branch,
                "base": base,
                "hint": (
                    f"任务分支 {branch} 落后 {base} {div['behind']} 提交、"
                    f"含独有提交 {div['ahead']} 个：已复用陈旧基线（实现数据在分支上，"
                    "引擎不自动重建）。判据：git rev-list --count HEAD..main。建议先用 "
                    "`git log --oneline HEAD..main -- <改动文件>` 确认 main 侧进展，"
                    "必要时在任务 worktree 内 merge main 同步基线后再实现。"
                ),
            },
        }
    return {"action": "recreate", "base": base, "behind": div["behind"]}


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


# _master.json 副本抑制（task-master-single-copy）：container 任务 worktree 不再
# 保留 .orchd/_master.json，唯一权威 = 主工作树。用 git sparse-checkout --no-cone
# 仅排除该文件（.orchd/ 其余文件与全仓库仍正常检出）。4 条 pattern 已实证：
# worktree 内文件不存在、git status 干净、git add -A/commit 不记录删除、主工作树
# 前进后 merge 不复活（skip-worktree 方案 merge 时会复活，故为主方案）。
_MASTER_IGNORE_PATTERNS = ["/*", "!/.orchd/", "/.orchd/*", "!/.orchd/_master.json"]


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


def _suppress_task_master_copy(wt_path: Path) -> dict[str, Any]:
    """抑制任务 worktree 内 ``.orchd/_master.json`` 副本（best-effort，降级可审计）。

    必须经 Python ``subprocess.run`` 以参数列表直传（``shell=False``），避免
    Git for Windows 的 MSYS 路径转换改写 ``!/...`` pattern（``!/.orchd/`` 等
    会被当作参数做路径归一化，导致 pattern 失效）。

    Returns:
        ``{"ok": bool, "method": str, "reason": str|None}``；``method`` ∈
        ``sparse-checkout`` / ``skip-worktree`` / ``none``。sparse 不可用
        （git < 2.25 或失败）→ 降级 skip-worktree；再失败 → 保留副本 + 告警
        （``ok=False, method="none"``，由调用方以 degraded 透出）。
    """
    version = _git_minor_version()
    if version >= 225:  # W-14：口径 major*100+minor（2.25 → 225；git 3.0 → 300）
        init = subprocess.run(
            ["git", "sparse-checkout", "init", "--no-cone", str(wt_path)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(wt_path),
        )
        # 陈旧 sparse 状态：残余 in-cone 标记/禁用；先 reset（--no-cone 下不保留
        # 先前 pattern），保证 set 前为纯 no-cone 基线。
        if init.returncode == 0:
            setp = subprocess.run(
                ["git", "sparse-checkout", "set", *_MASTER_IGNORE_PATTERNS],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                cwd=str(wt_path),
            )
            if setp.returncode == 0:
                # 收敛校验：明确的抑制目标文件应已从 worktree 消失。
                if not (wt_path / ".orchd" / "_master.json").exists():
                    return {"ok": True, "method": "sparse-checkout", "reason": None}
                return {
                    "ok": False, "method": "none",
                    "reason": "sparse-checkout set 成功但 .orchd/_master.json 仍存在",
                }
            reason = (setp.stderr or "").strip()[:200]
            return {
                "ok": False, "method": "none",
                "reason": f"sparse-checkout set 失败: {reason or 'exit' + str(setp.returncode)}",
            }
    # 降级：skip-worktree（merge 时副本可能复活，仍优于保留双副本）
    sw = subprocess.run(
        ["git", "update-index", "--skip-worktree", ".orchd/_master.json"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        cwd=str(wt_path),
    )
    if sw.returncode == 0:
        return {
            "ok": True, "method": "skip-worktree",
            "reason": "sparse-checkout 不可用，回退 skip-worktree（merge 时副本可能复活）",
        }
    return {
        "ok": False, "method": "none",
        "reason": "sparse-checkout / skip-worktree 均不可用，保留副本（双副本漂移风险）",
    }


def _master_suppression_entry(wt_path: Path) -> dict[str, Any]:
    """生成 ``ensure_task_wt`` 返回字典中的 ``master_suppression`` 段。"""
    supp = _suppress_task_master_copy(wt_path)
    # 抑制失败 → 副本残留，随 claim 透出 degraded 供 agent 可见（禁止静默）
    return {
        "master_suppression": supp,
        "degraded": supp.get("ok") is False,
        "degraded_reason": (
            f"master 副本抑制失败（{supp.get('method')}）：{supp.get('reason')}"
            if supp.get("ok") is False else None
        ),
    }


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


def ensure_task_wt(project_root: Path, task_id: str) -> dict[str, Any]:
    """创建/复用任务 worktree（best-effort，幂等）。

    container 布局 → 建独立任务 worktree（``git worktree add <task_wt_root>/task-<id>
    task/<id>``，已存在幂等复用）；flat（既有项目）→ 降级返回主工作树
    （任务 worktree 即主工作树本身，维持现状零回归）。

    Returns:
        ``{"worktree": <Path>, "separate": bool, "created": bool}``；
        container 下独立 worktree 创建失败时返回 ``{"worktree": 主工作树,
        "separate": False, "created": False, "degraded": True, "reason": str}``
        （不抛异常、不阻断 claim，但**降级原因必须显式记录**，禁止静默）。
    """
    project_root = Path(project_root).resolve()
    layout = detect_layout(project_root)

    # 仅 container 布局（推荐并发形态）才建独立任务 worktree；flat（既有项目 /
    # 遗留 merge-wt 多 worktree 场景）保持降级——任务 worktree 即主工作树，
    # 维持现状行为零回归（S2 flat 降级路径）。
    separate = layout["layout"] == "container"
    if not separate:
        return {"worktree": project_root, "separate": False, "created": False}

    from orchd.line_ctx import resolve_task_branch_for

    branch = resolve_task_branch_for(project_root, task_id)
    wt_path = layout["task_wt_root"] / _task_wt_name(task_id)
    # AC4（task-review-baseline-and-worktree-recycle-fix）：目录名单一来源与不变量。
    # 禁止用含 / 的分支名（task/<id>）拼路径——否则容器根出现 task/task-<id>
    # 嵌套路径（2026-09-10 事故实测形态：task/task-release-docs-version-sync）。
    if wt_path.name != _task_wt_name(task_id) or "/" in wt_path.name or "\\" in wt_path.name:
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": (
                f"worktree 目录名非法（{wt_path.name}）：须由 _task_wt_name(task_id) "
                "生成，禁止使用含分隔符的分支名拼路径"
            ),
        }
    try:
        if (wt_path / ".git").exists():
            # 已存在且是 worktree → 幂等复用（补写布局标记 + 抑制副本）
            # W-4（2026-09-15）：复用路径**必须同样过创建期不变量校验**——「见 .git
            # 即判幂等」会把半检出（HEAD 无法解析）/ 元数据丢失 / 检出其它分支的目录
            # 当作有效 worktree 绑定，agent 随后在错误分支或残缺检出上作业（静默）。
            verify = _verify_task_wt(wt_path, branch)
            if not verify["ok"]:
                _cleanup_stale_task_wt(project_root, wt_path)
                return {
                    "worktree": project_root,
                    "separate": False,
                    "created": False,
                    "degraded": True,
                    "reason": f"复用路径校验失败：{verify['reason']}",
                }
            _propagate_container_marker(wt_path, project_root)
            result = {"worktree": wt_path, "separate": True, "created": False}
            result.update(_master_suppression_entry(wt_path))
            _merge_vendored_engine_entry(result, project_root, wt_path)
            # 陈旧基线门禁（task-claim-stale-baseline-gate）：复用路径须验分支相对
            # 主分支的新鲜度。retract 只解绑不清分支（见 onboard/control.py），重领
            # 会静默复用落后基线（2026-09-21 实测落后 10 提交还浑然不觉）。
            # - behind == 0 → 新鲜，照常复用；
            # - behind > 0 且 ahead == 0 → 纯陈旧指针（无独有提交）：安全重建——
            #   worktree 移除 + 分支安全删除（-d，已合入必成功）后**落到下方创建流**
            #   从当前 base 重建；重建失败则回退复用 + degraded 留痕；
            # - behind > 0 且 ahead > 0 → 含独有实现：**禁止自动重建**（删分支即丢
            #   数据），挂 stale_baseline_warning 告警后复用，处置权交 agent；
            # - 测不到（base 未知 / git 异常）→ 维持旧行为（无门禁），不静默降级以外的
            #   任何假设（门禁按"有判据才生效"，见函数 docstring）。
            stale = _check_stale_baseline(project_root, branch, wt_path)
            if stale is None or stale.get("action") == "reuse":
                if stale is not None:
                    result["stale_baseline_warning"] = stale["warning"]
                    print(
                        "orchd ▸ [worktree] {\"action\": \"stale_baseline_reused\", "
                        f"\"task_branch\": \"{branch}\", "
                        f"\"behind\": {stale['warning']['behind']}, "
                        f"\"ahead\": {stale['warning']['ahead']}" + "}",
                        file=sys.stderr,
                    )
                return result
            if _recreate_task_wt_from_base(
                project_root, wt_path, branch, stale["base"]
            ):
                print(
                    "orchd ▸ [worktree] {\"action\": \"stale_baseline_recreated\", "
                    f"\"task_branch\": \"{branch}\", "
                    f"\"behind\": {stale['behind']}" + "}",
                    file=sys.stderr,
                )
                # 落到下方创建流：从当前 base 重建（created: True 即为重建证据，
                # 与首建同形，辅以本 stderr 留痕区分）。
            else:
                result["degraded"] = True
                prev = result.get("degraded_reason")
                _reason = (
                    "陈旧基线重建失败（worktree 移除或分支安全删除未成功）："
                    "已回退复用陈旧分支，见 stale_baseline_warning"
                )
                result["degraded_reason"] = f"{prev}；{_reason}" if prev else _reason
                result["stale_baseline_warning"] = {
                    "behind": stale["behind"], "ahead": 0,
                    "branch": branch, "base": stale["base"],
                    "hint": _reason,
                }
                return result
        # 创建期不变量硬化（W-4，复盘 P1 孤儿分支修复）：任务分支必须从**主分支**
        # fork（`git worktree add -b task/<id> <path> <main>`），杜绝因主工作树当
        # 前检出的非 main 分支而生成孤儿/悬空分支。base 解析失败（无 main/master）
        # 则回退当前 HEAD（best-effort），仍可创建但依赖探测结果。
        from orchd.gitops import get_default_branch
        base = get_default_branch(project_root)
        # AC4：建/认领 worktree 只写 .git/worktrees/<name>/HEAD，绝不改写主工作树
        # .git/HEAD（事故形态：主工作树 HEAD 被写成 ref: refs/heads/task/... →
        # 主工作树 unborn、git status 整树 staged、done 被 E017 误拦）。
        main_head_before = _read_main_worktree_head(project_root)
        add_cmd = ["git", "worktree", "add", "-b", branch, str(wt_path)]
        if base:
            add_cmd.append(base)
        proc = subprocess.run(
            add_cmd,
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        if proc.returncode == 0 and (wt_path / ".git").exists():
            # AC4：HEAD 落点不变量校验（对照组为上面采集的 main_head_before）。
            head_drift = _check_main_head_unchanged(project_root, main_head_before)
            if head_drift:
                _cleanup_stale_task_wt(project_root, wt_path)
                return {
                    "worktree": project_root,
                    "separate": False,
                    "created": False,
                    "degraded": True,
                    "reason": head_drift,
                    "head_restored": (_restore_main_head(project_root, main_head_before) if main_head_before is not None else False),
                }
            # 创建后校验不变量（W-4）：任务 worktree 检出恰为 task/<id> 分支且 HEAD
            # 解析正常。校验失败即使 add 成功也视为创建异常 → 走降级告警（禁止静默
            # 绑错分支/跑错目录，落地"静默降级禁止"硬约束）。
            verify = _verify_task_wt(wt_path, branch)
            if not verify["ok"]:
                _cleanup_stale_task_wt(project_root, wt_path)
                return {
                    "worktree": project_root,
                    "separate": False,
                    "created": False,
                    "degraded": True,
                    "reason": verify["reason"],
                }
            # 任务 worktree 自识别容器布局（共享账本根），见 _propagate_container_marker
            _propagate_container_marker(wt_path, project_root)
            result = {"worktree": wt_path, "separate": True, "created": True}
            result.update(_master_suppression_entry(wt_path))
            _merge_vendored_engine_entry(result, project_root, wt_path)
            return result
        # worktree add 失败（如分支已在别处 checkout）→ best-effort 降级主工作树，
        # 但降级原因显式记录（供 claim 告警 / 后续排查，禁止静默降级）。
        reason = (proc.stderr or "").strip()[:300] or (
            f"git worktree add {str(wt_path)} 失败（exit {proc.returncode}）"
        )
        # 2026-08-28 bug4 修复：add 失败可能残留半成品 worktree 元数据（git 注册 /
        # 残留目录）。降级前 best-effort 清理孤儿 worktree，避免污染 git worktree
        # list / doctor / 后续终态回收；仅清理「无效 worktree」，绝不误删有效 worktree。
        _cleanup_stale_task_wt(project_root, wt_path)
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": reason,
        }
    except subprocess.TimeoutExpired as exc:
        # W-5（2026-09-15）：超时被杀（SubprocessError 子类）走的是通用 except，
        # 旧实现**不清理**半创建目录/git 注册（对比 returncode≠0 路径会清）→
        # 残留半成品既污染 doctor/worktree list，又可能被后续复用路径当有效工作区。
        _cleanup_stale_task_wt(project_root, wt_path)
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": (
                f"git worktree add 超时（{exc}）：已清理半成品目录并降级主工作树"
            ),
        }
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {
            "worktree": project_root,
            "separate": False,
            "created": False,
            "degraded": True,
            "reason": f"worktree add 异常: {exc}",
        }


def _read_main_worktree_head(project_root: Path) -> str | None:
    """读取主工作树 ``.git/HEAD`` 原文（best-effort）。

    AC4（HEAD 落点加固）的对照组采集：仅当 project_root 确为主工作树
    （``.git`` 是目录且含 HEAD 文件）时返回内容；linked worktree（``.git`` 为
    gitfile）/ 读取异常 → None（守卫自动失效，不误报）。
    """
    try:
        head = Path(project_root) / ".git" / "HEAD"
        if head.is_file():
            return head.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return None


def _check_main_head_unchanged(project_root: Path, before: str | None) -> str | None:
    """比对主工作树 HEAD 是否被改写；未漂移返回 None，漂移返回原因串。"""
    if not before:
        return None
    after = _read_main_worktree_head(project_root)
    if after is None or after == before:
        return None
    return (
        "HEAD 落点异常：git worktree add 改写了主工作树 .git/HEAD"
        f"（{before.strip()} → {after.strip()}）；已回滚本次 worktree 创建"
        "（任务 worktree 只写 .git/worktrees/<name>/HEAD）"
    )


def _restore_main_head(project_root: Path, before: str) -> bool:
    """best-effort 还原主工作树 HEAD（原文须为 symbolic ref，否则不动作）。"""
    ref = before.strip()
    if not ref.startswith("ref: "):
        return False
    try:
        proc = subprocess.run(
            ["git", "symbolic-ref", "HEAD", ref[len("ref: "):]],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _worktree_registered_paths(project_root: Path) -> set[str] | None:
    """`git worktree list --porcelain` 登记路径集合（normcase 归一化）。

    返回 None 表示 git 不可用/解析失败（调用方保守降级：视为有效、不删）。
    按行前缀解析（W-16 口径），路径经 resolve + normcase 归一化。
    """
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    paths: set[str] = set()
    for ln in (proc.stdout or "").splitlines():
        if not ln.startswith("worktree "):
            continue
        p = ln[len("worktree "):].strip()
        if p:
            try:
                paths.add(os.path.normcase(str(Path(p).resolve())))
            except OSError:
                paths.add(os.path.normcase(p))
    return paths


def _is_effective_worktree(project_root: Path, wt_path: Path) -> bool:
    """判定是否为有效 worktree（doctor/worktree 单一真源）。

    有效 ⇔ git 登记含该路径 **且** `git rev-parse --verify HEAD` 可解析。
    任一 git 探测失败 → 保守返回 True（视为有效，绝不误删）。
    无 `.git` 标记 → 返回 False（由调用方按无标记残留路径处理）。
    """
    if not (wt_path / ".git").exists():
        return False
    registered = _worktree_registered_paths(project_root)
    if registered is None:
        return True
    try:
        key = os.path.normcase(str(wt_path.resolve()))
    except OSError:
        return True
    if key not in registered:
        return False
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return True
    return head.returncode == 0 and bool((head.stdout or "").strip())


def _cleanup_stale_task_wt(project_root: Path, wt_path: Path) -> None:
    """best-effort 清理 worktree add 失败残留的半成品元数据（孤儿 worktree）。

    - ``git worktree prune``：移除指向已消失目录的失效 git 注册（安全）。
    - 残留目录仅在其**非有效 worktree**时删除：无 ``.git`` 标记的沿旧路径；
      有 ``.git`` 标记的按 :func:`_is_effective_worktree` 判定——登记失效或
      HEAD 不可解析的半检出可回收，有效 worktree 绝不误删（与 doctor 残留
      判定同源）。
    任何失败静默跳过，不阻断降级主流程。
    """
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(project_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    if not wt_path.exists():
        return
    if (wt_path / ".git").exists() and _is_effective_worktree(project_root, wt_path):
        return
    try:
        wt_path.rmdir()
    except OSError:
        try:
            shutil.rmtree(str(wt_path), ignore_errors=True)
        except Exception:
            pass


def _verify_task_wt(wt_path: Path, expected_branch: str) -> dict[str, Any]:
    """创建期不变量校验（W-4）：任务 worktree 检出分支恰为 task/<id> 且 HEAD 可解析。

    Returns:
        ``{"ok": True}`` 或 ``{"ok": False, "reason": str}``（best-effort，绝不抛异常）。
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return {"ok": False, "reason": f"任务 worktree HEAD 无法解析（{wt_path}）"}
        cur = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
        actual = cur.stdout.strip() if cur.returncode == 0 else ""
        if actual != expected_branch:
            return {"ok": False,
                    "reason": f"任务 worktree 检出分支 {actual or '<null>'}，期望 {expected_branch}"}
        return {"ok": True}
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {"ok": False, "reason": f"worktree 校验异常: {exc}"}
