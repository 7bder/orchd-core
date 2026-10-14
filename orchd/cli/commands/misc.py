"""Orchd CLI 路由：misc 命令 handler。

迁移自 orchd/cli.py（task-split-cli-cmds-query-init-misc）：
  - _cmd_full_regression: full-regression 命令（全量回归并记录 last_pass_commit）
  - _cmd_layout_migrate: layout-migrate 命令（flat → container 布局迁移）
  - _cmd_intake: intake 命令（提交摄入产物并校验状态合法性）
  - _cmd_roadmap_land: roadmap-land 命令（ROADMAP 规划章节 → IDEAS study 落地）
  - _cmd_migrate: migrate 命令（W1-③ schema 版本前移；v3 起恒建 definition_archive 空骨架）

3a 阶段说明：本模块是 misc 域的目标落点。当前 cli.py（legacy）仍
保留同名函数为运行时主实现（兼容层透传 / monkeypatch 打点依赖），
本模块随 3a 收尾（删除 orchd/cli.py）后接管。函数体逐字一致
（AST 校验 IDENTICAL，仅允许 import 行变化），零逻辑变化。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode, OrchdError
from orchd.cli._util import (
    _command_name,
    _find_orchd_dir,
    _flatten_nargs,
    _reject_container_root_cwd,
    _resolve_text_arg,
)



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
    return {
        "ok": True,
        "last_pass_commit": head,
        "elapsed_seconds": reg_elapsed,
        "output_summary": _verify_output_summary(
            reg_result.stdout, reg_result.stderr),
        "note": f"已写入 {orchd_dir / '_full_regression.json'}",
        # task-milestone-check-gate：本次记录是 M1（单机内核冻结）B 组判据的输入，
        # 刷新后可直接查门禁（避免"刷了记录却不知道下一步判什么"）。
        "milestone_gate": "python .orchd/__main__.py milestone-check freeze",
    }, 0

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


def _cmd_layout_migrate(args) -> dict:
    """flat → container 布局迁移（task-14-worktree-layout，AC4）。

    CLI 参数: args.path（flat 主工作树根，默认当前目录）。
    返回: 迁移结果字典（migrated / main_worktree / moved / marker /
    runtime_root / 可选 reason + hint）。
    """
    from orchd.worktree import layout_migrate

    return layout_migrate(Path(args.path))

def _cmd_intake(args) -> dict:
    """提交摄入产物（IDEAS.md / ROADMAP.md）并校验条目状态（intake-commit-enforcement）。

    CLI 参数: 无（项目根由 .orchd/ 定位）。
    返回: 提交结果字典（committed / commit / 可选 status_warnings / commit_warning）。
    """
    from orchd.intake import intake_commit

    orchd_dir = _find_orchd_dir()
    return intake_commit(orchd_dir.parent)

def _cmd_roadmap_land(args) -> dict:
    """为 ROADMAP 规划章节生成 IDEAS study 落地条目（intake-dual-path）。

    CLI 参数: args.version — 规划章节版本（如 1.3）。
    返回: 落地结果字典（landed / version / section_id / commit）。
    """
    from orchd.intake import roadmap_land

    orchd_dir = _find_orchd_dir()
    return roadmap_land(orchd_dir.parent, args.version)


def _cmd_context_digest(args) -> dict:
    """输出必读面内容哈希与字节数（task-context-digest-command，只读 T0 命令）。

    CLI 参数: 无（项目根由 .orchd/ 定位）。
    返回: 必读面 digest 字典（files[] 每项 path/sha256/bytes/exists + 聚合
    digest + root；无时间戳/pid/mtime 等易变字段，同一工作区重复调用字节稳定）。
    """
    from orchd.guide import context_digest

    orchd_dir = _find_orchd_dir()
    return context_digest(orchd_dir.parent)


def _cmd_ledger_compact(args) -> dict:
    """终态任务事件归档压实（task-ledger-archive-compact）。

    CLI 参数: args.dry_run（只算搬运计划，不写文件）。
    返回: compact 摘要（moved_events / archived_tasks / active_before-after /
    checkpoint_lines / journal_recovered / dry_run）——后者为 c2（双文件归档）
    决策的增长曲线输入。
    """
    from orchd.ledger import open_store

    orchd_dir = _find_orchd_dir()
    store = open_store(orchd_dir)
    return store.compact_archive(dry_run=bool(getattr(args, "dry_run", False)))


def _cmd_storage_rollback(args) -> dict:
    """sqlite → file 回滚导出 + 复验（task-storage-rollback-cmd）。

    把库内逻辑事件原子导出回 ``_ledger.jsonl``，再经文件后端直读复验事件
    数与 event_id 集合；一致即报告可切 ``ORCHD_STORAGE_BACKEND=file``
    （命令不改进程外环境变量，只负责把回滚素材备齐并证明对得上）。
    非 sqlite 后端调用 → E007（回滚无对象）。

    CLI 参数：无。返回：exported / verified / match / hint。
    """
    from orchd.ledger import open_store, resolve_store_dir
    from orchd.storage.filesystem import FilesystemBackend
    from orchd.storage.sqlite import SqliteBackend

    orchd_dir = _find_orchd_dir()
    store = open_store(orchd_dir)
    if not isinstance(store.backend, SqliteBackend):
        raise OrchdError(
            ErrorCode.E007,
            "storage_rollback_requires_sqlite: 当前非 sqlite 后端，无需回滚",
            [{"hint": "仅 sqlite 缺省部署需要导出；file 后端账本即活名文件"}],
        )
    store.acquire_lock()
    try:
        exported = store.backend.export_jsonl()
        file_backend = FilesystemBackend(resolve_store_dir(orchd_dir))
        disk_ids = [e.get("event_id") for e in file_backend.read_events()]
        mem_ids = [e.get("event_id") for e in store.backend.read_events()]
    finally:
        store.release_lock()
    match = exported == len(disk_ids) and set(disk_ids) == set(mem_ids)
    return {
        "backend": "sqlite",
        "exported": exported,
        "verified": len(disk_ids),
        "match": match,
        "hint": ("一致时可设 ORCHD_STORAGE_BACKEND=file 切回文件后端"
                 if match else "导出与复验不一致，不得切换；检查账本完整性"),
    }


#: 并账消歧备份后缀（task-storage-recutover-merge-channel）。不复用
#: ``_MIGRATED_BACKUP_SUFFIX``：切库迁移已占用该目标名，复用会永远撞
#: ``_backup_migrated_files`` 的 fail-closed 预检。
_RECUTOVER_BACKUP_SUFFIX = ".recutover-bak"


def _recutover_backup_live_files(store_dir: Path) -> list[str]:
    """把活名账本文件改名备份，消解 doctor migration_consistency 的并存歧义。

    纪律与 ``SqliteBackend._backup_migrated_files`` 一致：改名**前**预检两个
    目标都不存在，任一被占用即 E007 fail-closed（不静默覆盖回滚素材）；改名
    经 ``orchd.ledger._atomic_replace`` 原语（不另造原子实现）。返回实际改名的
    文件名列表；调用方须已持 store 锁。
    """
    from orchd.ledger import _atomic_replace

    names = ("_ledger.jsonl", "_checkpoint.json")
    pending = [n for n in names if (store_dir / n).is_file()]
    for name in pending:
        target = store_dir / (name + _RECUTOVER_BACKUP_SUFFIX)
        if target.is_file():
            raise OrchdError(
                ErrorCode.E007,
                f"storage_recutover_backup_occupied: {target} 已存在",
                [{"path": str(target),
                  "hint": "并账数据已入库；备份目标被占用不覆盖，"
                          "人工核对后清理或改名再重试"}],
            )
    for name in pending:
        src = store_dir / name
        target = src.with_name(name + _RECUTOVER_BACKUP_SUFFIX)
        try:
            _atomic_replace(src, target)
        except (OrchdError, OSError) as exc:
            raise OrchdError(
                ErrorCode.E007,
                f"storage_recutover_backup_failed: {src} 改名备份失败",
                [{"path": str(src), "target": str(target),
                  "error": f"{type(exc).__name__}: {exc}",
                  "hint": "并账数据已入库；手动改名该文件后重试"
                          "（本次不再改动账本）"}],
            ) from exc
    return pending


def _cmd_storage_recutover(args) -> dict:
    """回滚残留并账：活名 file 增量按 event_id 差集并入 sqlite（task-storage-recutover-merge-channel）。

    触发形态：切库 → ``storage-rollback`` 导出 → 以
    ``ORCHD_STORAGE_BACKEND=file`` 追加事件 → 切回缺省 sqlite。此时库内停在
    切库时刻，活名 ``_ledger.jsonl`` 多出的新事件对引擎隐身
    （``needs_migration`` 把「活名 + 库内已有数」判为并存歧义态而返回假），
    唯一拦截是 doctor ``migration_consistency`` fail——此前其 hint 只教改名
    或删除、无并账通道。本命令补该通道：

    1. 读活名 jsonl（``FilesystemBackend``）与库内逻辑事件，取 event_id 差集；
    2. 库内活跃段（``read_events(from_line=归档段 + 1)``）与差集拼接，经
       ``replace_active_events`` 单事务原子替换活跃事件表（计数按「归档数 +
       活跃数」口径同步，归档段不重复写进活跃表）；
    3. 复验：库内 event_id 集合 == 原库 ∪ 活名、且条数一致；
    4. **仅当复验一致（match=True）**时把活名 ``_ledger.jsonl`` /
       ``_checkpoint.json`` 改名备份为 ``.recutover-bak``——消歧后 doctor
       ``migration_consistency`` 随之转 ok（回滚素材仍在备份文件里）；
       match=False 时**保留**活名文件不动，避免 doctor 因「活名文件不在场」
       把一次失败并账洗成绿灯。后缀不复用 ``_MIGRATED_BACKUP_SUFFIX``：该
       目标名已被切库迁移占用，复用必撞 ``_backup_migrated_files`` 的
       fail-closed 预检（实测确认）；预检纪律与之一致（目标占用即 E007、
       改名失败包 E007 并带「并账数据已入库」hint）。

    ``--dry-run`` 只算差集与计划，不写库、不改名。差集为空即幂等返回
    ``performed=False``（``no_missing_events``）。非 sqlite 后端 → E007
    （与 ``storage-rollback`` 对称：file 后端下活名文件即账本本体）。

    CLI 参数：args.dry_run（只算计划）。返回：backend / performed / dry_run /
    reason / live_events / db_events / missing_events / missing_event_ids /
    merged_events / verified / match / backed_up / hint。语义边界：
    ``performed`` = **检测到差集并尝试并账**（不代表生效——``match`` 才是一致性
    判据，mismatch 时 ``performed=True`` 但库未收敛）；``backed_up`` = 实际改名的
    活名文件（仅 ``match=True`` 时可能非空）；``merged_events`` = 计划并账后的
    活跃段条数（mismatch 时与实际的 ``verified`` 分叉属预期）；``verified`` =
    并账后库内**逻辑**事件总数（含归档段）。
    """
    from orchd.ledger import open_store, resolve_store_dir
    from orchd.storage import read_archive_events
    from orchd.storage.filesystem import FilesystemBackend
    from orchd.storage.sqlite import SqliteBackend

    orchd_dir = _find_orchd_dir()
    store = open_store(orchd_dir)
    backend = store.backend
    if not isinstance(backend, SqliteBackend):
        raise OrchdError(
            ErrorCode.E007,
            "storage_recutover_requires_sqlite: 当前非 sqlite 后端，"
            "活名文件即账本本体，无需并账",
            [{"hint": "并账对象是「缺省 sqlite 下被隐身的 file 增量」；"
                      "file 后端直接用文件账本或 storage-rollback"}],
        )
    dry_run = bool(getattr(args, "dry_run", False))
    store_dir = resolve_store_dir(orchd_dir)
    store.acquire_lock()
    try:
        live_events = list(FilesystemBackend(store_dir).read_events())
        db_events = list(backend.read_events())
        db_ids = {e.get("event_id") for e in db_events if e.get("event_id")}
        missing = [e for e in live_events
                   if e.get("event_id") and e["event_id"] not in db_ids]
        live_ids = {e.get("event_id") for e in live_events if e.get("event_id")}
        arch, _ = read_archive_events(store_dir)
        active = list(backend.read_events(from_line=len(arch) + 1))
        result: dict[str, Any] = {
            "backend": "sqlite",
            "dry_run": dry_run,
            "live_events": len(live_events),
            "db_events": len(db_events),
            "missing_events": len(missing),
            "missing_event_ids": [e["event_id"] for e in missing],
            "merged_events": len(active) + len(missing),
        }
        if dry_run:
            result.update({
                "performed": False,
                "reason": "dry_run",
                "verified": None,
                "match": None,
                "backed_up": [],
                "hint": (f"待并入 {len(missing)} 条活名增量并备份活名文件；"
                         "去掉 --dry-run 执行"),
            })
            return result
        if missing:
            backend.replace_active_events(active + missing)
        after = list(backend.read_events())
        verified_ids = {e.get("event_id") for e in after if e.get("event_id")}
        expected_ids = db_ids | live_ids
        # 第二子句按「有 event_id 的事件数」口径：len(after) 计全部事件，若库内
        # 存在无 event_id 事件（import_jsonl 逐行原样入库、不校验契约）或重复 id，
        # 拿 len(after) 比 len(expected_ids) 会让 match 恒假、命令永不收敛。
        match = (verified_ids == expected_ids
                 and len(verified_ids) == len(expected_ids))
        backed_up: list[str] = []
        if not match:
            # 复验不一致：**不得**改名活名文件——doctor 的 migration_consistency
            # 以「活名文件在场」为触发条件（orchd/doctor.py），改名会把一次失败
            # 并账洗成绿灯，状态面说谎。
            hint = ("并账后复验不一致（match=False）：活名文件保持原样、不得切库，"
                    "检查账本完整性后重跑")
        else:
            backed_up = _recutover_backup_live_files(store_dir)
            hint = ("并账完成且活名文件已改名备份，"
                    "doctor migration_consistency 应转 ok"
                    if missing else "库内已含活名全部事件，活名文件已改名备份")
        result.update({
            "performed": bool(missing),
            "reason": None if missing else "no_missing_events",
            "verified": len(after),
            "match": match,
            "backed_up": backed_up,
            "hint": hint,
        })
        return result
    finally:
        store.release_lock()


def _cmd_migrate(args) -> dict:
    """schema 版本前移（W1-③，停服迁移通道）。

    CLI 参数: args.path（master 文件，默认 canonical 主工作树
    .orchd/_master.json）；args.dry_run（只算计划，不写文件）；
    args.retreat（回退到最早版本，回滚通道）。
    返回: 迁移摘要（from_version / to_version / changed / dry_run）。
    改 schema_version 并**恒建 definition_archive 空骨架**（v3 为破坏性变更：
    顶层 required 收紧，master.tasks 语义收窄为「活跃任务定义」）；账本零触碰，
    归档内容搬迁属 master-archive 通道；回滚 = retreat 回 1（非空骨架时 fail-closed 拒绝）。
    """
    from orchd.migrate import migrate_master_file
    from orchd.worktree import resolve_master_path_from_dir

    orchd_dir = _find_orchd_dir()
    default = resolve_master_path_from_dir(orchd_dir)
    target = Path(getattr(args, "path", None) or default)
    return migrate_master_file(
        target,
        dry_run=bool(getattr(args, "dry_run", False)),
        retreat=bool(getattr(args, "retreat", False)),
    )


#: 首次建立归档清单时写入的归档相对路径（相对项目根）。**这是该默认值的唯一
#: 落点**：orchd/definition_archive.py 刻意不含任何归档文件名字面量，归档位置
#: 一律由 master 的 definition_archive.path 解析（单一真源）。
MASTER_ARCHIVE_DEFAULT_RELPATH = ".orchd/_master-archive.json"


def _cmd_master_archive(args) -> dict:
    """终态任务定义归档（task-master-archive-write-channel）。

    CLI 参数: args.path（master 文件，默认 canonical 主工作树 .orchd/_master.json）；
    args.archive_path（首次建立归档清单时的相对路径，之后一律以清单为准）；
    args.dry_run（只算计划，零落盘）。

    返回: 执行摘要（to_archive / remaining / archive_count_after / manifest /
    verified 等）。低版本 master 与账本不可重建两种情况均 fail-closed 拒绝
    （E007，带修复指引）——把不可用静默当「无终态任务」会让全部终态定义留在
    master 且清单写成空，是本通道最危险的失败模式。
    """
    from orchd.cli._util import archive_ideas_before_master_archive
    from orchd.definition_archive import run_master_archive
    from orchd.worktree import resolve_master_path_from_dir

    orchd_dir = _find_orchd_dir()
    default = resolve_master_path_from_dir(orchd_dir)
    target = Path(getattr(args, "path", None) or default)
    relpath = getattr(args, "archive_path", None) or MASTER_ARCHIVE_DEFAULT_RELPATH
    dry_run = bool(getattr(args, "dry_run", False))
    # IDEAS 止血（task-ideas-archive-archived-def-source）：摘除终态任务定义会让
    # 归档判据的真源（活跃定义）塌缩 ⇒ 摘除前先归档 IDEAS 可归档条目；判据不可信或
    # 仍有积压则 fail-closed 拒绝摘除（否则那些条目永久失明且零诊断）。--dry-run
    # 语义是「只算计划、零落盘」，故不触发止血。
    # 顺序要点：止血必须**晚于**零落盘预检——否则命令因低版本 master / 账本不可重建
    # 等前置失败而报错时，IDEAS 文档已被改写并提交，破坏「失败 ⇒ 零改动」契约。
    ideas_archive = None
    if not dry_run:
        run_master_archive(target, archive_relpath=relpath, dry_run=True)
        # 止血对象跟随 --path 指向的**文件**（target）与项目（target.parent），与摘除
        # 同一目标：--path 的语义是 master 文件、文件名不必然是 _master.json，若只传
        # 目录而让止血内部重新解析，会落到不存在的 <dir>/_master.json 上并整条静默
        # 旁路（MAJOR-1）。故显式透传 master_path=target。
        ideas_archive = archive_ideas_before_master_archive(
            target.parent, master_path=target
        )
    result = run_master_archive(target, archive_relpath=relpath, dry_run=dry_run)
    if ideas_archive is not None:
        result["ideas_archive"] = ideas_archive
    return result


def _cmd_git(args) -> dict:
    """git 写操作代理（task-git-write-proxy）：红线 #1/#2 引擎化拦截。
    返回: 代理载荷——只读子命令透传执行；任务分支 ``commit`` 放行；无 git 模式
          降级（``commit`` 推进 committed 快照，其余 no-op）。
    异常: 写操作被拒 → E007（红线 #1/#2）；``commit`` 不在任务分支 → E018。
    """
    from orchd.gitops.proxy import run_git_proxy
    from orchd.ledger import resolve_agent_id

    orchd_dir = _find_orchd_dir()
    return run_git_proxy(
        list(getattr(args, "git_args", None) or []),
        project_root=orchd_dir.parent,
        orchd_dir=orchd_dir,
        agent_id=resolve_agent_id(orchd_dir),
    )


def register(sub) -> None:
    """注册 misc 模块的子命令。"""
    # layout-migrate（task-14-worktree-layout）：flat → container 迁移
    p = sub.add_parser(
        "layout-migrate", help="flat → container 布局迁移（保留 git 历史、可回滚）"
    )
    p.add_argument("--path", default=".", help="flat 主工作树根，默认当前目录")
    p.set_defaults(func=_cmd_layout_migrate)

    # full-regression（task-full-regression-gate-r2）：全量回归并记录 last_pass_commit
    p = sub.add_parser(
        "full-regression",
        help="跑全量 pytest 并通过后写 .orchd/_full_regression.json（last_pass_commit）；失败不写通过标记",
    )
    p.add_argument("--path", default=None, help="项目根目录，默认当前目录")
    p.set_defaults(func=_cmd_full_regression)

    # ci-record（卡 A / task-ci-evidence-authority）：把当前 HEAD 的远端 CI 结论写入
    # .orchd/_full_regression.json，供里程碑 F1 的三重判定离线复算（唯一写入口）。
    p = sub.add_parser(
        "ci-record",
        help="把当前 HEAD 的远端 CI 结论写入 .orchd/_full_regression.json（F1 远端证据源）",
    )
    p.add_argument("--path", default=None, help="项目根目录，默认当前目录")
    p.set_defaults(func=_cmd_ci_record)

    # intake（2026-08-14 intake-commit-enforcement）
    p = sub.add_parser("intake", help="提交摄入产物（IDEAS.md + 宿主根 ROADMAP.md + .orchd/_master.json）并校验状态合法性；被 gitignore 忽略的路径由 commit 层剔除")
    p.set_defaults(func=_cmd_intake)

    # roadmap-land（2026-08-15 intake-dual-path）：ROADMAP 规划章节 → IDEAS study 落地
    p = sub.add_parser("roadmap-land", help="为 ROADMAP 规划章节生成 IDEAS study 落地条目")
    p.add_argument("version", help="规划章节版本（如 1.3，匹配 ROADMAP ## 版本 章节头）")
    p.set_defaults(func=_cmd_roadmap_land)

    # context-digest（task-context-digest-command）：必读面内容哈希只读命令
    p = sub.add_parser("context-digest", help="输出必读面内容哈希与字节数（未变即跳过重读的机械判据）")
    p.set_defaults(func=_cmd_context_digest)

    # ledger-compact（task-ledger-archive-compact）：终态任务事件归档压实
    p = sub.add_parser(
        "ledger-compact",
        help="终态任务事件归档至 _ledger.archive.jsonl 并重写 checkpoint（幂等；--dry-run 只算计划）",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="只计算搬运计划，不写任何文件")
    p.set_defaults(func=_cmd_ledger_compact)

    # migrate（W1-③）：schema 版本前移（改版本号 + v3 恒建归档空骨架；--dry-run 只算计划）
    p = sub.add_parser(
        "migrate",
        help="schema 版本前移至最新：改 schema_version 并恒建 definition_archive 空骨架（v3 破坏性变更），账本零触碰；--dry-run 只算计划",
    )
    p.add_argument("--path", default=None, help="master 文件，默认 canonical 主工作树 .orchd/_master.json")
    p.add_argument("--dry-run", action="store_true", help="只计算迁移计划，不写文件")
    p.add_argument("--retreat", action="store_true", help="回退到最早版本（回滚通道）")
    p.set_defaults(func=_cmd_migrate)

    # master-archive（task-master-archive-write-channel）：终态任务**定义**归档
    # 写入通道，与 ledger-compact（终态任务**事件**归档）对称。
    p = sub.add_parser(
        "master-archive",
        help="终态任务定义归档：把终态任务定义摘入归档文件并更新 definition_archive 清单（幂等 + 原子 + 自复验）",
    )
    p.add_argument("--path", default=None,
                   help="master 文件，默认 canonical 主工作树 .orchd/_master.json")
    p.add_argument("--archive-path", default=None,
                   help="首次建立归档清单时使用的归档相对路径（相对项目根），"
                        f"默认 {MASTER_ARCHIVE_DEFAULT_RELPATH}；"
                        "清单已存在时一律以清单 path 为准")
    p.add_argument("--dry-run", action="store_true",
                   help="只计算搬运计划，不写任何文件")
    p.set_defaults(func=_cmd_master_archive)

    # storage-rollback（task-storage-rollback-cmd）：sqlite→file 回滚导出+复验
    p = sub.add_parser(
        "storage-rollback",
        help="sqlite→file 回滚：库内事件原子导出回 _ledger.jsonl 并经文件后端复验一致",
    )
    p.set_defaults(func=_cmd_storage_rollback)

    # storage-recutover（task-storage-recutover-merge-channel）：回滚残留并账入库
    p = sub.add_parser(
        "storage-recutover",
        help="回滚残留并账：活名 file 增量按 event_id 差集并入 sqlite + 原子替换 + 复验（--dry-run 只算计划）",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="只计算差集与并账计划，不写库、不改名活名文件")
    p.set_defaults(func=_cmd_storage_recutover)

    # git 代理（task-git-write-proxy）：红线 #1/#2 引擎化拦截
    p = sub.add_parser(
        "git",
        help=(
            "git 写操作代理：只读透传 / 任务分支 commit 放行 / 其余写操作按红线 #1/#2 "
            "结构化拒绝（无 git 模式降级 no-op）"
        ),
    )
    p.add_argument(
        "git_args",
        nargs=argparse.REMAINDER,
        help="git 参数原样透传，如 status / log --oneline -5 / commit -m 'msg'",
    )
    p.set_defaults(func=_cmd_git)

