"""Orchd CLI 路由：guidance 注入/输出。

迁移自 orchd/cli.py（task-split-cli-guidance）：
  - _attach_guidance: 命令 JSON 响应统一附加 guidance 字段
  - _emit_guidance: guidance 人类可读提示块打印到 stderr
  - resolve_guidance_paths: guidance read/template 路径解析为文件条目
  - _guidance_stderr_enabled: config.guidance_stderr 开关读取（默认 true）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


# 3a 收尾（task-split-cli-remove-legacy）：直接从子包导入，替代 legacy 延迟委托
from orchd.cli._util import _find_orchd_dir
from orchd.cli.identity import _resolve_agent_id


def _attach_guidance(data: Any, command: str = "", guidance_mode: str = "slim") -> Any:
    """为命令 JSON 响应统一附加 guidance 字段（task-guide-seamless-guidance）。

    无感引导契约：
    - **加法式**：只新增 ``guidance`` 键（含转换感知上下文新键），不删不改既有
      字段（next_action 等保留）。
    - **幂等**：已有 guidance（如 stop_wait 预设）不覆盖既有字段，仅追加转换感知
      上下文新键。
    - **best-effort**：读取 ledger / 构造引导的任何异常静默跳过，不阻塞主流程。
    - **知识路由闭环**（task-guide-routing-loop）：附加 guidance 后透传
      ``orchd_dir`` 调用 ``resolve_read_paths`` 过滤 read/template 路径，
      只保留指向实际存在文件的路径，不存在时静默跳过。
    - **转换感知**（task-guide-transition-aware）：每次命令响应统一附加
      ``branch_ctx``（当前分支纪律）与 ``transition``（聚焦任务的状态由来），
      agent 无需任何额外操作即可获得分支切换/状态机转换后的引导。

    仅对 dict 响应生效；非 dict（如字符串/None）原样返回。
    """
    if not isinstance(data, dict):
        return data
    # W-1 分级（task-guide-tiering）：T0 读/健康命令省略 guidance 键，直接返回，
    # 不进入状态推导（避免读/健康命令也被灌入引导信息）。
    # 契约（task-audit-guidance-contract-unify）：无内容省略键，不用空串模拟缺失。
    try:
        from orchd.guide import guidance_tier
        tier = guidance_tier(command)
        if tier == 0:
            data.pop("guidance", None)
            return data
    except Exception:
        tier = 2
    try:
        from orchd.ledger import Store
        from orchd.spec import load_master
        from orchd.worktree import resolve_master_path_from_dir

        orchd_dir = _find_orchd_dir()
        state = Store(orchd_dir).replay()
        # master 任务定义统一从 canonical 主工作树读（task-canonical-project-root），
        # 规则收敛到底座（task-cli-master-rule-single-source）。
        master_path = resolve_master_path_from_dir(orchd_dir)
        tasks = load_master(master_path).tasks if master_path.exists() else []

        from orchd.guide import (
            next_guidance, resolve_read_paths, attach_rule_summaries,
            context_guidance, slim_guidance, apply_guidance_mode, _classify,
            attach_read_versions,
        )
        # task-guidance-dual-view-engine：传 agent_id（_resolve_agent_id 解析）与
        # has_master（master_path.exists()），支撑双视角与未初始化/空项目区分。
        # review-unify-r2：传 review_mode，in_review 模板按 unified/two_phase 分流。
        from orchd.ledger import resolve_review_mode
        from orchd.gitops import get_current_branch
        agent_id = _resolve_agent_id(orchd_dir)
        has_master = master_path.exists()
        review_mode = resolve_review_mode(orchd_dir)
        # W-4 登记表权威分支：cwd 仅作最后一次兜底，不再优先。先算聚焦任务，再从
        # session-worktrees.json 登记表取该任务 worktree 的真实分支；无绑定才回退
        # 调用进程 cwd 分支。保证 branch_ctx 描述的是引擎即将操作的 worktree，
        # 而非工具进程 cwd（复盘 P1：主 worktree cwd 误判成"当前在 task 分支"）。
        cls = _classify(state, tasks, agent_id, has_master, review_mode)
        focus_tid = cls.get("focus_tid")
        cwd_branch = get_current_branch(Path.cwd()) or get_current_branch(orchd_dir.parent)
        branch = cwd_branch
        if focus_tid:
            try:
                from orchd.ledger import resolve_store_dir
                from orchd.worktree import resolve_task_branch
                task_branch = resolve_task_branch(resolve_store_dir(orchd_dir), focus_tid)
                if task_branch:
                    branch = task_branch
            except Exception:
                pass  # best-effort：登记表解析失败回退 cwd

        guidance = data.get("guidance")
        if not isinstance(guidance, dict):
            guidance = resolve_read_paths(
                next_guidance(state, tasks, agent_id=agent_id, has_master=has_master,
                              review_mode=review_mode),
                orchd_dir,
            )
            # task-guidance-read-versions：read[] 附加 {mtime,size} 版本标注
            # （加法式新键 read_versions，read[] 契约不变），best-effort。
            guidance = attach_read_versions(guidance, orchd_dir)
            # task-guidance-rule-summary：read 过滤后追加 rules 键（TL;DR 摘要）
            guidance = attach_rule_summaries(guidance, orchd_dir)

        # task-guide-transition-aware：分支纪律 + 聚焦任务状态由来（加法式新键，
        # 已有 guidance 的既有字段保持不变）。command 透传（task-audit-guidance-
        # branch-ctx-rollout）：claim/done/review 的 branch_ctx.hint 按命令差异化。
        ctx = context_guidance(state, tasks, agent_id=agent_id,
                               review_mode=review_mode, branch=branch,
                               command=command)
        if ctx:
            guidance.update(ctx)
            # 双视角同步（与 resolve_read_paths 递归语义一致；project 视角以
            # agent_id=None 推导）
            for view_key, view_agent in (("agent_view", agent_id),
                                         ("project_view", None)):
                view = guidance.get(view_key)
                if isinstance(view, dict):
                    vctx = context_guidance(state, tasks, agent_id=view_agent,
                                            review_mode=review_mode, branch=branch,
                                            command=command)
                    if vctx:
                        view.update(vctx)
            guidance = resolve_read_paths(guidance, orchd_dir)
            guidance = attach_read_versions(guidance, orchd_dir)
            guidance = attach_rule_summaries(guidance, orchd_dir)

        # W-1 精简（task-guide-tiering）：最终收敛为分级精简结构（单视角 5 键、
        # 去 agent_view/project_view/template、hint 单行、read≤2）。
        # 契约（task-audit-guidance-contract-unify）：slim 为空 dict 时省略
        # guidance 键（不用空串/空对象模拟缺失）。
        slim = apply_guidance_mode(guidance, ctx, tier, mode=guidance_mode)
        # task-engine-cli-friction-fix：done 成功后 guidance 追加 cwd_switch 命令，
        # 指引 agent 回主工作树，缓解任务 worktree 被回收后 cwd 失效。
        if slim and command == "done" and data.get("done") is True:
            post_cwd = data.get("post_done_cwd", "")
            if post_cwd:
                # task-done-cwd-hook-hygiene AC2：cwd_switch.hint 条件化。
                # done 响应时任务通常处于 in_review（worktree 仍保留、待 review/merge
                # 后回收），不得声称「已回收」；仅当任务真正到达终态
                # （completed/cancelled，worktree 已回收）才称已回收。
                _recycled = False
                try:
                    _ts = state.get(focus_tid) if focus_tid else None
                    _recycled = _ts is not None and _ts.status in ("completed", "cancelled")
                except Exception:
                    _recycled = False
                if _recycled:
                    _hint = (
                        "done 已回收任务 worktree，请切回主工作树后再执行后续命令"
                        "（避免 cwd does not exist）"
                    )
                else:
                    _hint = (
                        "done 完成，请回主工作树（任务 worktree 仍保留至 review/merge 后回收，"
                        "避免 cwd 指向已回收 worktree 失效）"
                    )
                slim["cwd_switch"] = {
                    "command": f"Set-Location -Path {post_cwd}",
                    "hint": _hint,
                }
        if slim:
            data["guidance"] = slim
        else:
            data.pop("guidance", None)
    except Exception:
        # best-effort：引导失败静默跳过，绝不阻塞命令主流程
        pass
    return data


def _emit_guidance(data: Any) -> None:
    """将 guidance 的人类可读提示块打印到 stderr（task-guide-seamless-guidance）。

    设计契约：
    - **不污染 stdout**：stdout 保持纯 JSON，供机器/agent 解析；人看的"下一步"提示块
      打在 stderr，紧跟 JSON 之后，终端中人机同时可见。
    - **醒目可辨**：用分隔线围成块状，一眼可区分是 orchd 系统输出而非命令结果。
    - **best-effort**：非 dict / 无 guidance / 无 hint 时静默跳过，不影响主流程。
    - **可配置开关**（task-guide-block-config）：config.guidance_stderr 为 false 时
      跳过提示块打印；缺失/读取失败回退默认 true（向后兼容）。
    """
    if not isinstance(data, dict):
        return
    g = data.get("guidance")
    if not isinstance(g, dict):
        return
    hint = g.get("hint") or g.get("recovery")
    if not hint:
        return
    if not _guidance_stderr_enabled():
        return
    command = g.get("command") or "<无命令>"
    read = g.get("read") or []
    branch_ctx = g.get("branch_ctx")
    sep = "─" * 40
    lines = [
        "",
        sep,
        f"orchd ▸ {hint}",
        f"建议执行：{command}",
    ]
    # W-1 精简（task-guide-tiering）：transition/红线已并入 hint，顶层只保留
    # branch_ctx 作一块；read[] 指向需按需读取的文件清单（不展开规则内容）。
    if isinstance(branch_ctx, dict) and branch_ctx.get("hint"):
        lines.append(f"orchd ▸ [分支] {branch_ctx['hint']}")
    if read:
        lines.append("按需读取：")
        lines.extend(f"  · {r}" for r in read)
    # 经验回灌注入（设计 §8.4）：错误响应命中 lesson cases 时打印历史经验参考。
    cases = g.get("cases")
    if cases:
        lines.append("历史经验参考：")
        for c in cases:
            tag = "（未验证·参考）" if c.get("status") == "proposed" else ""
            drift = c.get("drift_note")
            drift_text = f" [版本漂移:{drift}]" if drift and drift != "same" else ""
            lines.append(f"  · [{c.get('id')}] {c.get('symptom')}{tag}{drift_text}")
            lines.append(f"    解法：{c.get('solution')}")
    lines.extend([sep, ""])
    block = "\n".join(lines)
    # task-guidance-block-budget-root-fix：渲染层只做拼接与打印，不丢弃任何
    # 已由数据层交付的行。预算由 guide.py 的 _BLOCK_MAX 求和不等式管理。
    # 渲染体包 try/except（真 best-effort）：本函数由 __init__.py 在 except
    # OrchdError 处理器内部调用，渲染异常不得冒泡成 traceback。
    try:
        print(block, file=sys.stderr)
    except Exception:
        pass


def resolve_guidance_paths(
    guidance: dict[str, Any] | None,
    orchd_dir: Path | None = None,
) -> dict[str, Any]:
    """将 guidance 的 read/template 路径解析为实际存在的文件条目（知识路由闭环）。

    知识路由闭环的 agent 侧解析接口（task-guide-routing-loop）：agent 收到
    guidance 后按 ``read`` 数组读规则文件、按 ``template`` 数组加载模板。
    ``resolve_read_paths`` 已把两数组过滤为实际存在的路径；本接口进一步把每条
    路径解析为**可读文件条目**（绝对路径 + 是否存在），供 agent 直接据以读取，
    不要求引擎在此处自动读取文件内容（只提供可解析的路由数据，读取由 agent
    按需进行）。

    契约：
    - 返回值与 guidance 同构：``{read: [{path, abs_path, exists}], template: [...]}``；
      guidance 为空 / 缺键时对应数组为空（无害，向下兼容）。
    - 空数组 / orchd_dir 缺失时原样返回空结构，不抛异常（best-effort）。

    Args:
        guidance: 含 read/template 数组的 guidance 字典（可为 None）。
        orchd_dir: 规则/模板根目录（.orchd/）；None 时自动查找。

    Returns:
        解析后的 read/template 文件条目字典。
    """
    if orchd_dir is None:
        orchd_dir = _find_orchd_dir()
    root = str(orchd_dir)
    parent = str(orchd_dir.parent)

    def _entries(paths: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in paths:
            if os.path.isabs(p):
                abs_path = p
            else:
                candidate = os.path.join(root, p)
                if not os.path.isfile(candidate):
                    candidate = os.path.join(parent, p)
                abs_path = candidate
            out.append({
                "path": p,
                "abs_path": abs_path,
                "exists": os.path.isfile(abs_path),
            })
        return out

    g = guidance or {}
    return {
        "read": _entries(g.get("read") or []),
        "template": _entries(g.get("template") or []),
    }


def _guidance_stderr_enabled() -> bool:
    """读取 config.guidance_stderr 决定是否打印 stderr 提示块（task-guide-block-config）。

    契约：
    - 默认 true：config 缺失、键缺失或读取失败时回退 true（向后兼容，不影响旧项目）。
    - 显式 false：跳过提示块打印，stdout 纯 JSON 契约不受影响。
    """
    try:
        from orchd.spec import load_master
        from orchd.worktree import resolve_master_path_from_dir

        orchd_dir = _find_orchd_dir()
        master_path = resolve_master_path_from_dir(orchd_dir)
        if not master_path.exists():
            return True
        master = load_master(master_path)
        return bool(master.config.get("guidance_stderr", True))
    except Exception:
        # best-effort：读取失败回退默认 true，绝不阻塞主流程
        return True
