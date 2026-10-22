"""lifecycle.create —— 自 ``orchd/worktree/lifecycle.py`` 拆出。

任务 worktree 创建与重建主流程（ensure_task_wt 及其分支/基线判断）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from orchd.worktree.bindings import _task_wt_name
from orchd.worktree.layout import _GIT_TIMEOUT, detect_layout

from .engine import _merge_vendored_engine_entry, _propagate_container_marker
from .mainhead import _check_main_head_unchanged, _read_main_worktree_head, _restore_main_head
from .probe import _cleanup_stale_task_wt, _verify_task_wt
from .suppress import _master_suppression_entry

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
