"""misc.evidence —— 自 ``orchd/cli/commands/misc.py`` 拆出。

全量回归证据与 CI 记录（full-regression / ci-record）。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

def _commit_regression_evidence(project_root: Path) -> dict:
    """把刚写入的 ``.orchd/_full_regression.json`` 随同提交到 default 分支。

    为什么必须由引擎提交：该文件被 ``.gitignore`` 以 ``!.orchd/_full_regression.json``
    反忽略入库（task-regression-evidence-in-repo，4dddf264「回归证据入库可审」）。
    写盘而不提交会同时造成两件事——① HEAD 读到的仍是上一版 ``last_pass_commit``，
    ``sync_orchd_core.sh`` 的发版覆盖检查据旧值判定；② 这条**已跟踪**改动使下一次
    intake/amend/idea propose/claim 的前置守卫判脏（实踩：``idea propose`` 返回
    ``reason=dirty_workspace``、``dirty_files=[".orchd/_full_regression.json"]``）。
    非 default 分支只写盘不提交，降级为可检索的 ``not_on_main``（对齐 amend/intake
    口径，不把发版证据提交到任务分支的 diff 里）。detached HEAD 同样只写盘不提交
    （``detached_head``：该状态下的提交没有任何分支指向、任何分支都读不到，报
    「已入库」是假陈述——审查四态之「空输入」）。git 缺失 / 非仓库由
    ``_probe_git_repo_ready`` 先行结构化降级，不与 ``detached_head`` 混为一谈。
    """
    from orchd.gitops import _probe_git_repo_ready, ensure_committed, get_current_branch
    from orchd.line_ctx import resolve_trunk_for

    probe = _probe_git_repo_ready(project_root)
    if probe is not None:
        return probe
    branch = get_current_branch(project_root)
    trunk = resolve_trunk_for(project_root)
    if branch is None:
        return {
            "performed": False,
            "reason": "detached_head",
            "hint": (
                "当前是 detached HEAD（无分支指向）：提交只会落在游离提交上，"
                "记录已写盘未提交；请先 checkout default 分支再跑全量"
            ),
        }
    if branch != trunk:
        return {
            "performed": False,
            "reason": "not_on_main",
            "branch": branch,
            "hint": (
                f"回归证据仅在 default（{trunk}）分支入库；"
                f"当前 {branch}，记录已写盘未提交"
            ),
        }
    return ensure_committed(
        project_root,
        [".orchd/_full_regression.json"],
        "chore(regression): 刷新全量回归证据（orchd full-regression）",
    )


# 「按设计就不提交」的降级原因（不发假绿告警）；不在此列且 performed=false 即
# 意味着证据没进 HEAD，而命令仍回 ok=true——发版门只剩 stdout 一个出口。
_EVIDENCE_NONCOMMIT_OK = frozenset({
    "not_on_main", "detached_head", "no_changes", "not_a_git_repo", "git_unavailable",
})


def _warn_evidence_not_committed(commit_result: dict) -> None:
    """把「全量通过但证据未入库」打到 stderr（stdout 仍是纯 JSON 契约）。"""
    _reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        print(
            f"orchd ▸ [full-regression] 全量通过但回归证据未入库"
            f"（commit.reason={commit_result.get('reason')!r}）：HEAD 上仍是上一版 "
            f"last_pass_commit，sync_orchd_core.sh 的发版覆盖检查会据旧值判定；"
            f"判语全文见响应 commit.message。",
            file=sys.stderr)
    except Exception:
        pass


def _cmd_full_regression(args) -> tuple[dict, int]:
    """跑全量 pytest 并通过后写 .orchd/_full_regression.json（task-full-regression-gate-r2）。

    全量回归通过 → 记录 last_pass_commit=当前 HEAD + passed_at；失败不写通过标记、
    返回非零退出码（供 sync_orchd_core.sh 发版前检查消费）。

    test-suite-slim §5.3 修复三处缺陷：
    - Python 解释器路径反斜杠转正斜杠，避免 Windows Git Bash 双引号内反斜杠被
      当转义符吞掉（嵌套引号 bug）；
    - basetemp 落**系统临时目录**下的固定可复用子目录（task-release-chain-hardening），
      不能放项目内：旧版 ``build/fullreg-basetemp`` 使 pytest ``tmp_path`` 进入仓库，
      canonical root 解析据此爬到真实 ``.orchd``，同一条命令实测 140 failed；出仓后
      0 failed。固定子目录而非 ``$$``：full-regression 是发版门禁、一次一人跑，
      固定路径可复用，避免 ``$$`` 每次新建且从不清理导致临时目录膨胀；
    - 显式 ``-c pyproject.toml`` 确保读到 addopts 的 ``-n auto --dist=loadscope``
      并行配置，不依赖 shell cwd 推断 rootdir。
    - 显式 ``-n 8 --max-worker-restart=5``（task-suite-slow-guard，对齐
      design/full-regression-parallel-crash-fix-20260830.md §三B/C）：addopts 的
      ``-n auto`` 在本机起 16 worker，Windows 文件系统竞争下单次 git 操作可被拖过
      超时且历史上批量 worker down 挂死；回归是独占通道，固定 8 worker + 崩溃自愈。
      命令行参数后于 ini addopts 生效，故 ``-n 8`` 覆盖 ``-n auto``。
    """
    import subprocess
    import time
    from datetime import datetime, timezone

    from orchd.subproc import run_shell
    from orchd.gitops_ops import (
        verify_output_summary as _verify_output_summary,
    )

    project_root = Path(args.path).resolve() if args.path else Path.cwd()
    orchd_dir = project_root / ".orchd"
    # basetemp 出仓到系统临时目录（task-release-chain-hardening）：若落在项目内，
    # pytest tmp_path 进入仓库会污染 canonical root 解析（实测 140 failed）。
    basetemp = Path(tempfile.gettempdir()) / "orchd-fullreg-basetemp"
    basetemp.mkdir(parents=True, exist_ok=True)
    # Git Bash 双引号内反斜杠会被当转义符；统一正斜杠（POSIX 路径本无反斜杠，无副作用）
    py = sys.executable.replace("\\", "/")
    basetemp_arg = str(basetemp).replace("\\", "/")
    reg_cmd = (
        f'"{py}" -m pytest tests/ -q -c pyproject.toml '
        f"--basetemp={basetemp_arg} -n 8 --max-worker-restart=5"
    )
    reg_started = time.monotonic()
    # 预算（task-flaky-hunt-freeze-gate，2026-09-22 实测）：旧值 600s 小于本机
    # 16 worker 并行实测时长（528~646s），导致 M1 冻结 B1/B2 从未盖章（命令自身
    # 超时 + 未写 last_pass_commit）。给足 2.5x 余量覆盖负载抖动；这是**发版门禁
    # 单次预算**，非测试自身超时，超时仍显式失败不留假绿。
    regression_budget = 1500
    try:
        reg_result = run_shell(reg_cmd, str(project_root), regression_budget)
    except subprocess.TimeoutExpired:
        reg_elapsed = round(time.monotonic() - reg_started, 1)
        return {
            "ok": False,
            "code": "full_regression_timeout",
            "message": f"全量回归超时（{regression_budget}s）after {reg_elapsed}s",
        }, 1
    reg_elapsed = round(time.monotonic() - reg_started, 1)
    if reg_result.returncode != 0:
        # task-recycle-observability：失败透传 output_summary（与
        # onboard/lifecycle/regression.py 同源复用 _verify_output_summary）——
        #此前仅 exit code，失败原因须另跑全量才可见。
        return {
            "ok": False,
            "code": "full_regression_failed",
            "message": f"exit code {reg_result.returncode} after {reg_elapsed}s",
            "details": {
                "returncode": reg_result.returncode,
                "elapsed_seconds": reg_elapsed,
                "output_summary": _verify_output_summary(
                    reg_result.stdout, reg_result.stderr),
            },
        }, 1
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(project_root),
        capture_output=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    payload = {
        "last_pass_commit": head,
        "passed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": "python -m pytest tests/ -q -c pyproject.toml -n 8 --max-worker-restart=5",
    }
    from orchd.ledger import _write_text_atomic

    orchd_dir.mkdir(exist_ok=True)
    _write_text_atomic(
        orchd_dir / "_full_regression.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    commit_result = _commit_regression_evidence(project_root)
    response = {
        "ok": True,
        "last_pass_commit": head,
        "commit": commit_result,
        "elapsed_seconds": reg_elapsed,
        "output_summary": _verify_output_summary(
            reg_result.stdout, reg_result.stderr),
        "note": f"已写入 {orchd_dir / '_full_regression.json'}",
        # task-milestone-check-gate：本次记录是 M1（单机内核冻结）B 组判据的输入，
        # 刷新后可直接查门禁（避免"刷了记录却不知道下一步判什么"）。
        "milestone_gate": "python .orchd/__main__.py milestone-check freeze",
    }
    # 假绿防线：提交被拦（E020/E017 等）而命令仍回 ok=true 时，除响应里的结构化
    # commit 字段外再给一个可按原因检索的 warning + stderr 可见性。
    if not commit_result.get("performed") and (
        commit_result.get("reason") not in _EVIDENCE_NONCOMMIT_OK
    ):
        response["warning"] = "regression_evidence_not_committed"
        _warn_evidence_not_committed(commit_result)
    return response, 0


def _cmd_ci_record(args) -> tuple[dict, int]:
    """把当前 HEAD 的**远端 CI 结论**写入 .orchd/_full_regression.json（卡 A）。

    task-ci-evidence-authority：里程碑 F1 此前只认本地 ``last_pass_commit``，于是
    「本地绿 + 提交未推 + CI 停在旧 HEAD」也能盖章。本命令提供远端权威字段的**唯一写入口**：
    结论一律来自 ``gh run list --commit <HEAD>``，不接受手工值；写入是「读-改-写 + 原子替换」
    且保留既有字段（不覆盖 ``last_pass_commit`` / ``last_done_run``）。

    非零退出的三类（均**不写半截字段**）：gh 缺失、查询失败（网络/未登录/非仓库）、
    该 HEAD 尚无 CI run 或 run 未完成。判据侧对缺字段是 fail-closed（nil 即 not_ready），
    故本命令绝不落一个「看起来有值实则未验证」的记录。

    CLI 参数: args.path（项目根，默认 cwd）。
    返回: (结构化结果, 退出码)；成功 ``{"ok": True, ci_head_sha, ci_run_id,
    ci_conclusion, ci_recorded_at, note, milestone_gate}``。
    """
    import json as _json
    import shutil as _shutil
    import subprocess
    from datetime import datetime, timezone

    from orchd.ledger import _write_text_atomic

    project_root = Path(args.path).resolve() if args.path else Path.cwd()
    orchd_dir = project_root / ".orchd"
    if _shutil.which("gh") is None:
        return {
            "ok": False,
            "code": "gh_unavailable",
            "message": "未找到 gh CLI——远端 CI 结论的唯一权威来源",
            "hint": "安装 GitHub CLI 并 gh auth login 后重试；本字段不接受手工值",
        }, 1
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(project_root),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if head.returncode != 0:
        return {
            "ok": False,
            "code": "head_unresolved",
            "message": (head.stderr or "").strip()[:200] or "git rev-parse HEAD 失败",
        }, 1
    sha = (head.stdout or "").strip()
    try:
        proc = subprocess.run(
            ["gh", "run", "list", "--commit", sha, "--limit", "1",
             "--json", "databaseId,conclusion,status,headSha"],
            cwd=str(project_root), capture_output=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as exc:
        return {
            "ok": False, "code": "ci_query_failed",
            "message": f"{type(exc).__name__}: {exc}",
            "hint": "网络/gh 会话不可用时不落记录：缺 ci 字段时 F1 判 not_ready（fail-closed）",
        }, 1
    if proc.returncode != 0:
        return {
            "ok": False, "code": "ci_query_failed",
            "message": (proc.stderr or "").strip()[:200] or "gh run list 失败",
        }, 1
    try:
        runs = _json.loads(proc.stdout or "[]")
    except ValueError:
        return {
            "ok": False, "code": "ci_query_failed",
            "message": "gh run list 输出不是 JSON",
        }, 1
    if not runs:
        return {
            "ok": False, "code": "ci_run_not_found", "ci_head_sha": sha,
            "hint": "该 HEAD 尚无 CI run：确认已推送到上游且 workflow 已触发",
        }, 1
    run = runs[0]
    if str(run.get("status") or "") != "completed":
        return {
            "ok": False, "code": "ci_run_incomplete", "ci_head_sha": sha,
            "status": run.get("status"),
            "hint": "CI 仍在跑：等该 HEAD 的 run 结束后重跑 ci-record（不落半截字段）",
        }, 1
    path = orchd_dir / "_full_regression.json"
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = _json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data.update({
        "ci_head_sha": sha,
        "ci_run_id": run.get("databaseId"),
        "ci_conclusion": run.get("conclusion"),
        "ci_recorded_at": recorded_at,
    })
    orchd_dir.mkdir(exist_ok=True)
    _write_text_atomic(path, _json.dumps(data, ensure_ascii=False, indent=2))
    return {
        "ok": True,
        "ci_head_sha": sha,
        "ci_run_id": run.get("databaseId"),
        "ci_conclusion": run.get("conclusion"),
        "ci_recorded_at": recorded_at,
        "note": f"已写入 {path}",
        "milestone_gate": "python .orchd/__main__.py milestone-check split-ready",
    }, 0
