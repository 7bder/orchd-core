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
from .probe import (
    _cleanup_stale_task_wt,
    _recreate_task_wt_from_base,
    _task_branch_divergence,
    _verify_task_wt,
)
from .suppress import _master_suppression_entry


def _check_stale_baseline(
    project_root: Path, branch: str, wt_path: Path
) -> dict[str, Any] | None:
    """复用路径的基线新鲜度判定（task-claim-stale-baseline-gate）。

    Returns:
        - ``None``：新鲜（behind == 0）或无法判定（base 未知 / git 异常）——
          调用方照常复用；
        - ``{"action": "reuse", "warning": {...}}``：分叉且含独有提交——调用方挂
          ``stale_baseline_warning`` 后复用；
        - ``{"action": "preserve", "reason": "dirty" | "dirty_unknown",
          "warning": {...}}``：纯陈旧指针但 worktree 内有未提交改动 / 脏度无法判定
          ——调用方保留 worktree（**不重建**）并挂 ``stale_baseline_warning``；
        - ``{"action": "recreate", "base": str, "behind": int}``：纯陈旧指针且
          worktree 干净——调用方安全重建后落创建流。

    ``wt_path`` 用于脏度取证（``git status --porcelain=v1 --untracked-files=all``）：
    ``git worktree remove --force`` 会连未提交改动一起删掉，故重建前必须确认干净。
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
    # 脏守卫（P0 task-cont-stale-baseline-dirty-guard）：纯陈旧指针的「安全重建」以
    # ``git worktree remove --force`` 落地，会把该 worktree 里所有未提交改动**静默**
    # 一起删掉（tracked 修改与未跟踪新文件都不在 git 对象里，事后无法找回）。故重建
    # 前必须取证 ``git status --porcelain=v1 --untracked-files=all``：
    # - 有输出（tracked 改动 / untracked 新文件 / 冲突态）→ 保留 worktree，处置权交
    #   agent（提交或走受管 restore 回滚无用改动后重领）；
    # - 取证失败（git 不可用 / 非零退出）→ **同样保留**：无法判定脏度时按「可能丢数据」
    #   处理（fail-closed），不得默认干净；
    # - 仅确认干净才允许重建。
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(wt_path), capture_output=True, encoding="utf-8",
            errors="replace", timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        status = None
    unknown = status is None or status.returncode != 0
    entries = [] if unknown else [
        ln for ln in (status.stdout or "").splitlines() if ln.strip()
    ]
    # 引擎自产工件不计入脏度：``.orchd/.layout.json``（container 标记）与
    # ``.orchd/orchd/``（宿主布局的 vendored 引擎副本）由本函数调用序内的
    # ``_propagate_container_marker`` / ``_merge_vendored_engine_entry`` 写入，属引擎
    # 维护面而非 agent 实现。正常布局下它们已被 ``.orchd/.gitignore`` 忽略、不会出现在
    # porcelain 里；本豁免是**宿主未忽略 ``.orchd/``** 时的兜底（不兜底则该布局下每次
    # 复用都被判脏、重建永不发生）。排除仅限**未跟踪**（``??``）形态：这些路径若被
    # 跟踪且被改，仍算真实改动。
    engine_owned = {".orchd/.layout.json", ".orchd/_master.json"}
    engine_prefixes = (".orchd/orchd/",)
    rows = [
        (e[:2], (e[3:] if len(e) > 3 else e).strip()) for e in entries
    ]
    files = [
        path for code, path in rows
        if not (code == "??" and (
            path in engine_owned or path.startswith(engine_prefixes)
        ))
    ]
    if unknown or files:
        if unknown:
            hint = (
                f"任务分支 {branch} 落后 {base} {div['behind']} 提交，但 worktree "
                "脏度无法判定（git status 失败）：已保留 worktree 不重建——"
                "先修复 git 可用性后重试 `python .orchd/__main__.py status`"
            )
        else:
            hint = (
                f"任务分支 {branch} 落后 {base} {div['behind']} 提交，但 worktree "
                f"有 {len(files)} 处未提交改动：已保留 worktree 不重建（重建会静默"
                "删除这些改动）。先提交，或对无用改动执行 `python .orchd/__main__.py "
                "restore --path <文件>` 回滚，再重领本任务"
            )
        return {
            "action": "preserve",
            "reason": "dirty_unknown" if unknown else "dirty",
            "warning": {
                "dirty": bool(files),
                "dirty_unknown": unknown,
                "dirty_count": len(files),
                "dirty_files": files[:20],
                "behind": div["behind"],
                "ahead": 0,
                "branch": branch,
                "base": base,
                "hint": hint,
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
            # - behind > 0 且 ahead == 0 → 纯陈旧指针（无独有提交）：先验脏
            #   （task-cont-stale-baseline-dirty-guard）——worktree 脏 / 脏度无法判定
            #   → **保留**（remove --force 会静默删掉未提交改动）；
            #   干净才安全重建：worktree 移除 + 分支安全删除（-d，已合入必成功）后
            #   **落到下方创建流**从当前 base 重建；重建失败则回退复用 + degraded 留痕；
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
            if stale.get("action") == "preserve":
                # 脏 / 脏度未知：保留 worktree，不执行 remove --force / branch -d
                # （task-cont-stale-baseline-dirty-guard P0：重建会静默删除未提交改动）。
                warn = stale["warning"]
                result["stale_baseline_warning"] = warn
                result["stale_baseline_dirty_preserved"] = {
                    "reason": stale.get("reason"),
                    "dirty_count": warn["dirty_count"],
                    "dirty_files": warn["dirty_files"],
                    "behind": warn.get("behind"),
                    "ahead": warn.get("ahead", 0),
                    "base": warn.get("base"),
                    "hint": warn.get("hint"),
                }
                print(
                    "orchd ▸ [worktree] {\"action\": \"stale_baseline_dirty_preserved\", "
                    f"\"task_branch\": \"{branch}\", "
                    f"\"reason\": \"{stale.get('reason')}\", "
                    f"\"dirty_count\": {warn['dirty_count']}, "
                    f"\"behind\": {warn['behind']}" + "}",
                    file=sys.stderr,
                )
                return result
            rec = _recreate_task_wt_from_base(
                project_root, wt_path, branch, stale["base"]
            )
            if rec["ok"]:
                print(
                    "orchd ▸ [worktree] {\"action\": \"stale_baseline_recreated\", "
                    f"\"task_branch\": \"{branch}\", "
                    f"\"behind\": {stale['behind']}" + "}",
                    file=sys.stderr,
                )
                # 落到下方创建流：从当前 base 重建（created: True 即为重建证据，
                # 与首建同形，辅以本 stderr 留痕区分）。
            else:
                prev = result.get("degraded_reason")
                detail = rec.get("reason", "")
                if rec.get("half_state"):
                    # 绑定装回失败：该路径已不存在，**不得**再作为 worktree 交付
                    # （调用方 onboard/claim 会把它当 cwd 绑定给 agent）。改走同函数
                    # 其它降级分支同形（worktree=主工作树 / separate=False），并把失效
                    # 路径单列 worktree_invalid 供人工处置。
                    _reason = (
                        f"陈旧基线重建失败（{detail}）：原 worktree 路径已失效，"
                        "已回退主工作树语义，需人工处置后再 claim；"
                        "见 stale_baseline_warning"
                    )
                    print(
                        "orchd ▸ [worktree] {\"action\": \"stale_baseline_half_state\", "
                        f"\"task_branch\": \"{branch}\", "
                        f"\"invalid_path\": \"{wt_path}\", "
                        f"\"behind\": {stale['behind']}" + "}",
                        file=sys.stderr,
                    )
                    return {
                        "worktree": project_root,
                        "separate": False,
                        "created": False,
                        "worktree_invalid": str(wt_path),
                        "degraded": True,
                        # ``reason`` 是 claim 侧 degraded_warning 的读取键
                        # （onboard/claim/execute.py），只写 degraded_reason 会得到
                        # "worktree degraded: None"，人工处置提示到不了 agent。
                        "reason": _reason,
                        "degraded_reason": f"{prev}；{_reason}" if prev else _reason,
                        "stale_baseline_warning": {
                            "behind": stale["behind"], "ahead": 0,
                            "branch": branch, "base": stale["base"],
                            "hint": _reason,
                        },
                    }
                # 绑定已装回：``remove`` + ``add`` 会丢掉 worktree 级运行态，按创建流
                # 同序重建（容器标记 → 副本抑制 → vendored 引擎），否则装回后的工作树
                # 读不到布局标记（回退本地空账本）、master 双副本漂移复活。
                _propagate_container_marker(wt_path, project_root)
                result.update(_master_suppression_entry(wt_path))
                _merge_vendored_engine_entry(result, project_root, wt_path)
                _reason = (
                    f"陈旧基线重建失败（{detail}）：已回退复用陈旧分支并重建"
                    "容器标记 / 副本抑制 / vendored 引擎，见 stale_baseline_warning"
                )
                result["degraded"] = True
                result["reason"] = _reason
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
