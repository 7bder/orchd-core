"""``orchd.doctor`` 运行态检查域（W2-D3，file-split-program 20261015 §4.3）。

职责：对 orchd 运行态做只读诊断——hung git 进程、lessons 迁移/陈旧、checkpoint 一致性
（files / sqlite 后端）、事件 schema 一致性（files / sqlite 后端）、存储后端迁移一致性。

承载 22 个顶层定义（14 个设计稿符号 + 8 个随域常量）：``_list_git_processes`` /
``_windows_pids_persist`` / ``_check_hung_git_processes`` / ``_lessons_oldest_age_s`` /
``_check_lessons_transit`` / ``_check_lessons_runtime`` / ``_check_checkpoint_consistency``
（+ ``_files`` / ``_sqlite``） / ``_check_migration_consistency`` / ``_event_schema_problem``
/ ``_check_event_schema``（+ ``_files`` / ``_sqlite``）与随域常量
``_HUNG_GIT_AGE_S`` / ``_WIN_*`` / ``_LESSONS_STAGED_AGING_S`` /
``_CHECKPOINT_LAG_FAIL_THRESHOLD`` / ``_EVENT_REQUIRED_FIELDS`` / ``_EVENT_OPTIONAL_FIELDS``。
函数体与搬迁前基线**逐字一致**（等价性判据见 tests/test_doctor_pkg.py）。

依赖方向：标准库（``json`` / ``os`` / ``subprocess`` / ``sys`` / ``time`` / ``pathlib``）
+ ``orchd.doctor.runtime``（``_make_check`` / ``_make_check_unavailable``）；对
``orchd.ledger`` / ``orchd.lessons`` / ``orchd.storage`` / ``orchd.storage.sqlite`` 保持
**函数级惰性导入**（搬迁不改取用方式，保持 ``orchd.ledger.<名>`` 的 monkeypatch 补丁面）。
本模块**不得**导入 ``orchd.doctor`` 聚合入口（否则成加载期环）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from orchd.doctor.runtime import _make_check, _make_check_unavailable


# 僵持 git 进程阈值（task-doctor-proc，F8/E-11）：
# git 命令均为短命进程；POSIX 存活超本阈值即疑似僵持（并发 full-regression
# 实测遗留 13 个）。Windows 无廉价进程年龄通道（tasklist 不暴露启动时间），
# 改用“持久性”判定：两次采样（间隔数秒）交集仍在的同批 PID 即僵持——瞬时
# 并发 Spitze（xdist 并行建 worktree）两次采样 PID 集合不同，不误伤；
# 真僵持（锁死 indefinately）PID 纹丝不动，必命中。首样本门槛取 10
# （E-11 实测 13 个；xdist -n 8 瞬时峰值通常低于此，免除多数无谓等待）。
_HUNG_GIT_AGE_S = 600
_WIN_FIRST_SAMPLE_MIN = 10
_WIN_PERSIST_FAIL_MIN = 5
_WIN_RESAMPLE_DELAY_S = 3


def _list_git_processes() -> list[dict[str, Any]] | None:
    """枚举系统 git 进程（task-doctor-proc；纯标准库 subprocess，禁 psutil）。

    - Windows：``tasklist /FI IMAGENAME eq git.exe /FO CSV`` 取 PID（无年龄）。
    - POSIX：``ps -eo pid,etimes,comm`` 取 PID + 存活秒（etimes 数值型易解析）；
      进程名精确匹配 ``git``（与 guard._count_git_processes 的 pgrep -x 同口径；
      git-remote-https 等帮手进程不在本轮口径内）。

    Returns:
        ``[{pid, age_s|None}]``；工具缺失 / 超时 / 解析失败返回 None
        （调用方按“未探明”跳过，不判 fail——见 _check_hung_git_processes）。
    """
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq git.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        pids: list[dict[str, Any]] = []
        for line in (proc.stdout or "").splitlines():
            parts = [p.strip().strip('"') for p in line.split(",")]
            if (len(parts) >= 2 and parts[0].lower() == "git.exe"
                    and parts[1].isdigit()):
                pids.append({"pid": int(parts[1]), "age_s": None})
        return pids
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,etimes,comm"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines()[1:]:
        parts = line.split()
        if (len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit()
                and parts[2].split("/")[-1] == "git"):
            out.append({"pid": int(parts[0]), "age_s": int(parts[1])})
    return out


def _windows_pids_persist(first: set[int]) -> bool:
    """Windows 二次采样确认（task-doctor-proc）：间隔后同批 PID 仍全部存活。

    首样本不足 _WIN_FIRST_SAMPLE_MIN 时调用方已直接 ok，不会走到这里。
    二次采样失败（工具异常）→ 保守返回 False（不判 fail，未探明≠有问题）。
    """
    try:
        time.sleep(_WIN_RESAMPLE_DELAY_S)
        second = _list_git_processes()
    except Exception:
        return False
    if not second:
        return False
    return len(first & {p["pid"] for p in second}) >= _WIN_PERSIST_FAIL_MIN


def _check_hung_git_processes() -> list[dict[str, str]]:
    """僵持 git 进程盘点（task-doctor-proc，F8/E-11；只读盘点 + 人工清理出口）。

    - 确认僵持（POSIX 年龄超阈）或高度疑似（并发计数达阈，年龄未知）→ fail，
      附 PID + 存活时长 + 精确 kill 命令；``doctor --fix`` 不自动杀进程
      （fix 仅处理 detect_residues 项，本检查天然只报告——杀进程恒为人工动作）。
    - 探针失败（无 tasklist/ps）→ ok + 未探明注记后跳过：INV-4a 要求检测不可用
      不静默，但本探针是环境相关 best-effort——fail 会在精简容器（无 ps）上
      误红整个 doctor，故以 ok 注记收敛（确认僵持仍 fail，无漏报）。
    - 时钟口径注记（F7 余波）：会话僵死另有两套时钟（ledger TTL 24h——doctor 僵尸
      判定与之同源 / watchdog 认领 60min，见 rules/session.md）——
      本项只看 git 进程存活，与会话 stuck 判定无关。
    """
    try:
        procs = _list_git_processes()
    except Exception:
        procs = None
    if procs is None:
        return [_make_check(
            "hung_git_processes", "ok",
            "git 进程盘点未探明（缺 tasklist/ps 或执行失败），已跳过；"
            "疑似僵持请人工核对（Windows: tasklist /FI \"IMAGENAME eq git.exe\"；"
            "POSIX: ps -eo pid,etimes,comm | grep git）")]
    hung = [p for p in procs
            if p["age_s"] is not None and p["age_s"] >= _HUNG_GIT_AGE_S]
    if hung:
        if sys.platform == "win32":
            kill = "taskkill /F /PID " + " /PID ".join(str(p["pid"]) for p in hung)
        else:
            kill = "kill -9 " + " ".join(str(p["pid"]) for p in hung)
        detail = ("僵持 git 进程（存活超 "
                  f"{_HUNG_GIT_AGE_S}s）："
                  + "、".join(f"PID {p['pid']}（{p['age_s']}s）" for p in hung))
    elif (sys.platform == "win32" and len(procs) >= _WIN_FIRST_SAMPLE_MIN
            and _windows_pids_persist({p["pid"] for p in procs})):
        kill = ("taskkill /F /PID "
                + " /PID ".join(str(p["pid"]) for p in procs))
        detail = (f"{len(procs)} 个 git 进程两次采样（间隔 "
                  f"{_WIN_RESAMPLE_DELAY_S}s）PID 完全相同，判定僵持"
                  "（Windows 无年龄通道，以持久性为据；瞬时并发 Spitze 两次"
                  "采样集合不同，不会误判）")
    else:
        return [_make_check(
            "hung_git_processes", "ok",
            f"{len(procs)} 个存活 git 进程，均在年龄/持久性阈值内")]
    return [_make_check(
        "hung_git_processes", "fail",
        f"{detail}。确认僵持后人工清理：{kill}；doctor --fix 不自动杀进程。"
        "（会话僵死另有两套时钟：ledger TTL 24h（doctor 僵尸判定同源）/ "
        "watchdog 认领 60min——本项只看 git 进程存活。）")]


# lessons 暂存积压的告警年龄阈值（秒，取 30 天）：判据是「超龄从未晋升」而非
# 「暂存区有条目」——stage 之后等人工裁决本就是新经验的正常入口（lessons.py:193
# 的 review_timeout_minutes=60 是**单次 review 的超时**，不能当积压年龄用），短窗口
# 内不算停滞；30 天明显超出真实审查节奏，用于发现「stage 后从未 review」这一长期
# 空转（pass13 P1-6：2026-09-01 的暂存条目 35 天未晋升，期间注入恒 0）。
_LESSONS_STAGED_AGING_S = 30 * 24 * 3600


def _lessons_oldest_age_s(rows: list[dict[str, Any]], path: Path,
                          now_s: float) -> tuple[float | None, str]:
    """暂存条目中最老一条的 ``(年龄秒, 时间标识)``。

    条目 ``ts`` 形如 ``2026-09-01T04:26:39+0800``；任一可解析即取最老，并把它的 ts
    回带作标识（告警要能指名「是哪条在积压」，只报天数无从下手）。全部不可解析
    （历史条目 / 手写条目）→ 退化为文件 mtime（标识写明是 mtime）；连 mtime 也取
    不到 → ``(None, "")``（**不判**，宁可不报也不误报）。
    """
    oldest_s: float | None = None
    oldest_ts = ""
    for row in rows:
        ts = row.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            secs = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError:
            continue
        if oldest_s is None or secs < oldest_s:
            oldest_s, oldest_ts = secs, ts
    if oldest_s is None:
        try:
            mtime_s = path.stat().st_mtime
        except OSError:
            return None, ""
        mtime_label = datetime.fromtimestamp(mtime_s).strftime("%Y-%m-%d")
        return max(0.0, now_s - mtime_s), f"文件 mtime {mtime_label}"
    return max(0.0, now_s - oldest_s), oldest_ts


def _check_lessons_transit(canonical: Path) -> list[dict[str, str]]:
    """旧布局无残留时的另一半判据：运行时根自身是否在空转（pass13 P1-6 甲案）。

    既有 ``if not legacy: return ok`` 只看「有没有旧布局残留」，于是「库不存在 /
    暂存从未晋升」这一真实症状落在 ok 分支里——同一个症状换一个盲区继续存在。
    本函数把停滞与损坏分档（warn 为非阻断档，见文件头 INV-4a 第 5 条）：
    - 库缺失/为空 **且** 暂存非空 → warn（有输入而无晋升 ⇒ lookup 恒 0）；
    - 库有内容 **且** 暂存超龄未晋升 → warn（积压，附最老年龄）；
    - 其余（全新仓：库缺且暂存亦空 / 库有内容且无积压 / 未超龄）→ ok，零误报。
    """
    lib_path = canonical / "lessons.jsonl"
    staged_path = canonical / "lessons.staged.jsonl"
    try:
        # 复用 lessons 域唯一的容错 JSONL 读（缺文件 → []、损坏行跳过）：另立
        # 一份读实现就会与 lookup 的可见集合漂移。
        from orchd.lessons import _read_jsonl

        lib_rows = _read_jsonl(lib_path)
        staged_rows = _read_jsonl(staged_path)
    except Exception as exc:
        return [_make_check_unavailable(
            "lessons_runtime", exc, "经验库读取")]
    review_cmd = (
        "python .orchd/__main__.py lesson review --task <task_id> "
        "--approve-all（晋升）或 --reject <n>（驳回，0-based 序号）")
    if staged_rows and not lib_rows:
        return [_make_check(
            "lessons_runtime", "warn",
            f"lessons 已启用但运行时库不存在或为空（{lib_path}），而暂存 "
            f"{len(staged_rows)} 条从未晋升 ⇒ lookup 恒 0、guidance 注入空转；"
            f"请裁决暂存：{review_cmd}，或直接入库："
            "python .orchd/__main__.py lesson add --trigger <E0xx> "
            "--symptom \"…\" --solution \"…\"；确不使用 lessons 则由用户经 amend "
            "通道置 config.lessons.enabled=false（勿手改快照）")]
    if lib_rows and staged_rows:
        age_s, oldest_label = _lessons_oldest_age_s(
            staged_rows, staged_path, time.time())
        if age_s is not None and age_s > _LESSONS_STAGED_AGING_S:
            return [_make_check(
                "lessons_runtime", "warn",
                f"lessons 暂存积压：{len(staged_rows)} 条待审，最久 "
                f"{int(age_s // 86400)} 天未晋升（最老 {oldest_label}，阈值 "
                f"{_LESSONS_STAGED_AGING_S // 86400} 天）⇒ 新经验长期不进库；"
                f"请裁决暂存：{review_cmd}")]
    if not lib_rows:
        tail = "（库为空且无待审 ⇒ 尚无经验沉淀，注入 0 条属预期而非故障）"
    elif not staged_rows:
        tail = "（库有内容且无待审 ⇒ 回灌通道通畅）"
    else:
        tail = "（暂存未超龄 ⇒ 等待人工裁决属正常节奏）"
    return [_make_check(
        "lessons_runtime", "ok",
        f"无旧布局经验库残留；运行时库 {len(lib_rows)} 条、待审暂存 "
        f"{len(staged_rows)} 条" + tail)]


def _check_lessons_runtime(project_root: Path) -> list[dict[str, str]]:
    """lessons 运行时观测（task-lessons-runtime-migrate-observe，只读告警）。

    覆盖既往零观测的断层：开关开启（is_lessons_enabled）但经验库不在运行时根
    ——旧布局（flat 时代 ``<main>/.orchd/lessons*.jsonl``）在 container 重定向后
    成孤儿，lookup 恒为 0。判据（零误报优先，既有健康仓不断裂）：
    - 无 .orchd / 开关关闭 / flat 无重定向 → ok（无迁移观测项）；
    - 重定向生效且旧布局经验库文件仍在 → fail，附迁移 hint；运行时库缺失
      （lookup 恒为 0）与仅部分文件残留（半迁移）只在 hint 文案中区分，状态
      同为 fail（告警）；不碰其它检查项 hint。
    - 无旧布局残留 → 转 :func:`_check_lessons_transit`（pass13 §11-9 甲案补的
      另一半判据：库不存在 / 暂存从未晋升 ⇒ 空转，warn 非阻断档）。
    检测自身异常 → 不可用 fail（INV-4a），不静默。
    """
    orchd_dir = Path(project_root) / ".orchd"
    if not orchd_dir.is_dir():
        return [_make_check(
            "lessons_runtime", "ok", "无 .orchd 目录，跳过 lessons 运行时观测")]
    try:
        from orchd.lessons import is_lessons_enabled

        enabled = is_lessons_enabled(orchd_dir)
    except Exception as exc:
        return [_make_check_unavailable(
            "lessons_runtime", exc, "lessons 开关解析")]
    if not enabled:
        return [_make_check(
            "lessons_runtime", "ok", "lessons 未启用，跳过运行时观测")]
    try:
        from orchd.ledger import resolve_store_dir

        canonical = Path(resolve_store_dir(orchd_dir))
        try:
            redirected = canonical.resolve() != orchd_dir.resolve()
        except OSError:
            redirected = canonical != orchd_dir
    except Exception as exc:
        return [_make_check_unavailable(
            "lessons_runtime", exc, "账本根解析")]
    if not redirected:
        return [_make_check(
            "lessons_runtime", "ok",
            "flat 布局：经验库与账本同根，无迁移观测项")]
    legacy = sorted(
        name for name in ("lessons.jsonl", "lessons.staged.jsonl")
        if (orchd_dir / name).is_file()
    )
    if not legacy:
        return _check_lessons_transit(canonical)
    try:
        lib_missing = not (canonical / "lessons.jsonl").is_file()
    except OSError:
        lib_missing = True
    if lib_missing:
        state = "运行时库缺失（lookup 恒为 0）且旧布局残留"
    else:
        state = "旧布局残留（半迁移：运行时库已就位但旧布局仍有数据）"
    hint = (
        f"lessons 经验回灌已启用但{state}：{', '.join(legacy)}"
        f" 仍在 {orchd_dir}；迁移已由引擎在 claim 的工作树就绪段自动执行"
        "（orchd/onboard/claim.py → orchd/migrate.py，幂等、持 lessons 锁、"
        "拿不到锁 reason=lock_busy 不清阻塞），下次 claim 后本项自愈；"
        "需立即执行可运行 python -c \"from orchd.migrate import "
        "migrate_lessons_to_runtime; migrate_lessons_to_runtime('.orchd')\"；"
        "迁移后残留自动消除（或运行 orchd doctor --fix 备份清理）"
    )
    return [_make_check("lessons_runtime", "fail", hint)]


# checkpoint 滞后容忍行数（DR-14）：引擎按**写命令**惰性落盘 checkpoint，因此
# 「滞后若干行」是设计内状态（增量 replay 会补齐），不能一有滞后就报不一致；
# 仅当滞后超过本阈值才判 fail——阈值取明显超出正常写命令间隔的量级，用于发现
# 「checkpoint 长期未更新」（写路径异常 / 长时间只读）这一真异常。
_CHECKPOINT_LAG_FAIL_THRESHOLD = 1000

# 账本事件必填字段与期望类型（DR-14）：与引擎写路径 make_event 的产出对齐。
_EVENT_REQUIRED_FIELDS: tuple[tuple[str, type], ...] = (
    ("event_id", str),
    ("type", str),
    ("timestamp", str),
)
# 可选字段：存在时必须是声明类型（防止 hand-written / 旧版本事件字段类型漂移）。
_EVENT_OPTIONAL_FIELDS: tuple[tuple[str, type], ...] = (
    ("task_id", str),
    ("agent_id", str),
    ("verdict", str),
    ("review_type", str),
    # v4（2026-09-15 停服升级）：REVIEW_CLAIMED / REVIEW_SUBMITTED 的自审标注
    ("is_self_review", bool),
    # task-review-rework-scope：REVIEW_SUBMITTED 打回范围（spec/code 缺省不写）
    ("rework_scope", str),
)


def _check_checkpoint_consistency(project_root: Path) -> list[dict[str, str]]:
    """checkpoint ↔ ledger 一致性检查（DR-14）。

    判据（只判结构性不可能 + 超阈值滞后，不做无依据的猜测）：
    - checkpoint / ledger 任一缺失 → ok（写命令惰性建立 checkpoint，属正常）；
    - ``checkpoint.ledger_line > ledger 总行数`` → **fail**：checkpoint 声称的
      增量起点越界（checkpoint 超前 / 账本被截断），增量 replay 会漏事件；
    - ``checkpoint.schema_version`` 与引擎常量不符 → **fail**（旧 checkpoint
      需全量重建，replay 会退化为全量并告警）；
    - ``ledger_line < 总行数`` → 滞后：``<= _CHECKPOINT_LAG_FAIL_THRESHOLD`` 视为
      惰性校准内（ok，报告滞后行数供观测），超过阈值判 fail。

    后端分发（task-doctor-checkpoint-sqlite-port，B-max 阶段 2）：
    按 ``ORCHD_STORAGE_BACKEND``（与 ``ledger._select_backend`` 同口径）分流——
    ``file`` 走文件直读（行为与改造前逐行一致），``sqlite`` 走 StorageBackend
    端口（checkpoint 取自 meta 表，计数走 ``event_count``，含归档口径）；
    非法取值抛错，由外层统一判 fail（不静默按 file 检查）。
    """
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.ledger import _STORAGE_BACKEND_ENV, resolve_backend_kind, resolve_store_dir

        store_dir = resolve_store_dir(orchd_dir)
        backend_kind = resolve_backend_kind()
        if backend_kind == "sqlite" and (store_dir / "_ledger.sqlite3").is_file():
            return _check_checkpoint_consistency_sqlite(orchd_dir, store_dir)
        if backend_kind != "file" and backend_kind != "sqlite":
            raise ValueError(
                f"storage_backend_unknown: {_STORAGE_BACKEND_ENV}="
                f"{backend_kind!r} 非法（取值仅 file / sqlite，缺省 sqlite）"
            )
        # task-readonly-no-migrate：库缺失（含未迁移的 jsonl 部署）一律走文件
        # 直读——file 分支无 Store 构造，绝不建库 / 迁移 / 改名；任一缺失即 ok。
        return _check_checkpoint_consistency_files(store_dir)
    except Exception as exc:  # NEW-DR4：检查自身异常（账本不可读）→ fail（消盲）
        # 旧实现此处返回 ok（"检查跳过"），账本坏了 doctor 反而报健康。
        # 「未能判定」必须阻断（INV-4a），而 warn 档只承载「已判定为停滞」，
        # 故此处用 fail（强于 AC 的"至少 warn"）使 repo_ok=False、CLI exit 1，盲区真正消除。
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint 一致性无法判定（{type(exc).__name__}: {exc}）"
                "——账本可能不可读，需人工检查账本完整性",
            )
        ]


def _check_migration_consistency(project_root: Path) -> list[dict[str, str]]:
    """迁移备份一致性（task-migrate-backup-atomicity，只读）。

    sqlite 缺省后端下：库文件存在 + 活名 ``_ledger.jsonl`` /
    ``_checkpoint.json`` 仍在 + 非待迁移态（``needs_migration`` 为假，即库
    里已有数或处歧义残留）→ **fail**（mismatch：疑似两步改名中断 / 回滚残留，
    附手工收敛 hint）。待迁移态（fresh / resume，下次写命令自动收敛）与
    file 后端（活名文件属正常载体）、无库部署一律 ok。
    只读：经 ``Store(..., readonly=True)`` 判定，不建库 / 不迁移 / 不改名。
    """
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.ledger import Store, resolve_backend_kind, resolve_store_dir

        if resolve_backend_kind() != "sqlite":
            return [_make_check("migration_consistency", "ok",
                                "file 后端：活名文件属正常载体")]
        store_dir = resolve_store_dir(orchd_dir)
        if not (store_dir / "_ledger.sqlite3").is_file():
            return [_make_check("migration_consistency", "ok", "无库文件：尚未迁移")]
        store = Store(orchd_dir, readonly=True)
        live = [n for n in ("_ledger.jsonl", "_checkpoint.json")
                if (store_dir / n).is_file()]
        from orchd.storage.sqlite import SqliteBackend

        assert isinstance(store.backend, SqliteBackend)
        if live and not store.backend.needs_migration():
            return [
                _make_check(
                    "migration_consistency",
                    "fail",
                    f"库已导入却残留活名文件 {live}（疑似两步改名中断或回滚残留）"
                    "——首选跑 `orchd storage-recutover` 把活名增量并账入库"
                    "（event_id 差集 + 原子替换 + 复验 + 改名备份，收敛后本项自动转 ok）；"
                    "若确认增量为空或已并入，再手动改名活名文件为备份或删除；"
                    "切回 file 后端前须先 export 导出",
                )
            ]
        return [_make_check("migration_consistency", "ok", "迁移备份一致")]
    except Exception as exc:
        return [
            _make_check(
                "migration_consistency",
                "fail",
                f"迁移一致性无法判定（{type(exc).__name__}: {exc}）"
                "——需人工检查账本完整性",
            )
        ]


def _check_checkpoint_consistency_files(store_dir: Path) -> list[dict[str, str]]:
    """file 后端分支：直读 ``_ledger.jsonl`` / ``_checkpoint.json``（行为冻结）。

    由 :func:`_check_checkpoint_consistency` 分发进入；判据与历史实现逐行一致，
    逻辑行 = 归档解析成功数 + 活跃文件非空行（与 ``FilesystemBackend.event_count``
    同口径，见 task-ledger-archive-compact）。
    """
    import json

    from orchd.ledger import _CHECKPOINT_SCHEMA_VERSION

    ledger_path = store_dir / "_ledger.jsonl"
    ckpt_path = store_dir / "_checkpoint.json"
    if not ledger_path.is_file() or not ckpt_path.is_file():
        return [
            _make_check(
                "checkpoint_consistency",
                "ok",
                "无 checkpoint 或账本（checkpoint 由写命令惰性建立）",
            )
        ]
    try:
        raw = json.loads(ckpt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"{ckpt_path.name} 无法解析为 JSON（replay 将退化为全量重建）",
            )
        ]
    if not isinstance(raw, dict):
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"{ckpt_path.name} 顶层不是 JSON 对象（replay 将退化为全量重建）",
            )
        ]
    from orchd.storage import archive_event_count

    total = archive_event_count(store_dir) + len([
        ln for ln in ledger_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ])
    ckpt_line = raw.get("ledger_line")
    if not isinstance(ckpt_line, int) or isinstance(ckpt_line, bool):
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"{ckpt_path.name} 缺少合法的 ledger_line 字段"
                f"（实际 {ckpt_line!r}）",
            )
        ]
    if ckpt_line > total:
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint 超前账本：ledger_line={ckpt_line} > 账本 {total} 行"
                "（增量起点越界，replay 会漏事件；需全量重建 checkpoint）",
            )
        ]
    version = raw.get("schema_version")
    if version != _CHECKPOINT_SCHEMA_VERSION:
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint schema_version={version!r} 与引擎常量"
                f" {_CHECKPOINT_SCHEMA_VERSION} 不符（需全量重建）",
            )
        ]
    lag = total - ckpt_line
    if lag > _CHECKPOINT_LAG_FAIL_THRESHOLD:
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint 落后账本 {lag} 行"
                f"（超过阈值 {_CHECKPOINT_LAG_FAIL_THRESHOLD}）："
                "写路径可能长期未落 checkpoint，增量 replay 成本持续放大",
            )
        ]
    note = (f"ledger_line={ckpt_line}/{total} 行" +
            (f"（滞后 {lag} 行，惰性校准内）" if lag else "（完全对齐）"))
    return [_make_check("checkpoint_consistency", "ok", note)]


def _check_checkpoint_consistency_sqlite(
    orchd_dir: Path, store_dir: Path
) -> list[dict[str, str]]:
    """sqlite 后端分支：checkpoint / 计数走 StorageBackend 端口（只读）。

    纯 sqlite 部署下 ``_ledger.jsonl`` 缺失，旧文件逻辑恒报"无账本" ok——
    此处改经 ``Store``（与 ``ledger._select_backend`` 同一构造路径，尊重
    ``ORCHD_STORAGE_BACKEND`` 并复用首次 ``import_jsonl`` 迁移语义）取
    ``backend.load_checkpoint()``（meta 表）与 ``backend.event_count()``
    （归档 + 活跃逻辑总数），其余判据（超前 / 版本错配 / 滞后超阈）与
    文件分支同口径。

    只读无副作用：库文件缺失时由分发层改走文件直读，此处被调时库必存在；
    构造一律 ``Store(..., readonly=True)``（不建库、不迁移、不改名）。
    meta checkpoint 缺失沿用"任一缺失 → ok"语义（惰性建立，属正常）。
    """
    from orchd.ledger import _CHECKPOINT_SCHEMA_VERSION, Store

    db_path = store_dir / "_ledger.sqlite3"
    if not db_path.is_file():
        return [
            _make_check(
                "checkpoint_consistency",
                "ok",
                "无 checkpoint 或账本（checkpoint 由写命令惰性建立）",
            )
        ]
    store = Store(orchd_dir, readonly=True)
    raw = store.backend.load_checkpoint()
    if raw is None:
        return [
            _make_check(
                "checkpoint_consistency",
                "ok",
                "无 checkpoint 或账本（checkpoint 由写命令惰性建立）",
            )
        ]
    if not isinstance(raw, dict):
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                "sqlite checkpoint(meta) 顶层不是 JSON 对象"
                "（replay 将退化为全量重建）",
            )
        ]
    # 接触即纠·只读通道（task-counter-cache-reconcile D3/D4）：比较口径取
    # 实体真值——缓存漂移不再把本分支误判成 ok/fail（V6' 实锤形态：hint 里
    # 「账本 1 行」直接吃撒谎缓存，checkpoint 被判越界冤案）。readonly 不写回，
    # 漂移明细归入下方 counter_reconcile 哨兵观测项。
    rec = store.reconcile_counters()
    truth = rec.get("to")
    total = int(truth) if truth is not None else store.backend.event_count()
    stats = store.counter_reconcile_stats()
    ckpt_line = raw.get("ledger_line")
    if not isinstance(ckpt_line, int) or isinstance(ckpt_line, bool):
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                "sqlite checkpoint(meta) 缺少合法的 ledger_line 字段"
                f"（实际 {ckpt_line!r}）",
            )
        ]
    if ckpt_line > total:
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint 超前账本：ledger_line={ckpt_line} > 账本 {total} 行"
                "（增量起点越界，replay 会漏事件；需全量重建 checkpoint）",
            )
        ]
    version = raw.get("schema_version")
    if version != _CHECKPOINT_SCHEMA_VERSION:
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint schema_version={version!r} 与引擎常量"
                f" {_CHECKPOINT_SCHEMA_VERSION} 不符（需全量重建）",
            )
        ]
    lag = total - ckpt_line
    if lag > _CHECKPOINT_LAG_FAIL_THRESHOLD:
        return [
            _make_check(
                "checkpoint_consistency",
                "fail",
                f"checkpoint 落后账本 {lag} 行"
                f"（超过阈值 {_CHECKPOINT_LAG_FAIL_THRESHOLD}）："
                "写路径可能长期未落 checkpoint，增量 replay 成本持续放大",
            )
        ]
    note = (f"ledger_line={ckpt_line}/{total} 行" +
            (f"（滞后 {lag} 行，惰性校准内）" if lag else "（完全对齐）"))
    # counter_reconcile 哨兵（D4）：当前漂移（只读通道未归位）或历史有纠偏
    # 记录 → warn（非阻断，INV-4a 数据停滞档）；全干净 → ok。持续 warn =
    # 有新写路径在制造漂移，当场现形、末次明细可回溯。
    drift_now = bool(rec.get("drift"))
    hist_bits = f"累计归位 {stats.get('count', 0)} 次"
    last = stats.get("last")
    if isinstance(last, dict):
        hist_bits += (
            f"；末次 {last.get('from')}→{last.get('to')}"
            f" @ {last.get('at')}"
        )
    hint = (
        ("当前漂移：缓存 "
         f"{rec.get('from')} ≠ 实体 {rec.get('to')}"
         "（只读通道未归位，可写进程接触即自愈）；"
         if drift_now else "") + hist_bits
    )
    return [
        _make_check("checkpoint_consistency", "ok", note),
        _make_check(
            "counter_reconcile",
            "warn" if (drift_now or stats.get("count")) else "ok",
            hint,
        ),
    ]


def _event_schema_problem(ev: dict[str, Any]) -> str | None:
    """单事件字段级校验（DR-14；file / sqlite 双分支共用，判据逐字一致）。

    只判字段（必填 ``event_id`` / ``type`` / ``timestamp`` 齐备且类型正确，
    可选字段存在时类型正确）；**不校验事件类型取值**（未知类型保守放行，
    避免阻塞未来事件扩展）。JSON 解析与非对象判据保留在 file 分支
    （sqlite 行由事务保证完整，``read_events`` 只产出已解析 dict）。

    Returns:
        问题描述；无问题返回 None。
    """
    for field, ftype in _EVENT_REQUIRED_FIELDS:
        val = ev.get(field)
        if not isinstance(val, ftype) or not str(val).strip():
            return f"缺少/类型错误必填字段 {field}={val!r}"
    for field, ftype in _EVENT_OPTIONAL_FIELDS:
        if field in ev and not isinstance(ev[field], ftype):
            return f"可选字段 {field} 类型错误（{ev[field]!r}）"
    return None


def _check_event_schema(project_root: Path) -> list[dict[str, str]]:
    """账本事件 schema 合法性检查（DR-14，只读）。

    逐行校验（不修改账本）：JSON 可解析、为对象、必填字段
    （``event_id`` / ``type`` / ``timestamp``）齐备且为声明类型，可选字段存在时
    类型正确。**不校验事件类型取值**——与 :func:`ledger.validate_transition`
    的「未知类型保守放行」一致，避免阻塞未来事件扩展。

    发现坏行 → fail（报告条数 + 前 5 个行号样例）；账本缺失 / 全部合法 → ok。

    后端分发（task-doctor-event-schema-sqlite-port，B-max 阶段 2）：
    按 ``ORCHD_STORAGE_BACKEND``（与 ``ledger._select_backend`` 同口径）分流——
    ``file`` 走文件逐行直读（行为与改造前逐行一致），``sqlite`` 走 StorageBackend
    端口（``read_events`` 逐条同判据校验，行号为逻辑序号； ``corrupt_lines``
    的归档段损坏一并计入坏行）；非法取值抛错，由外层统一判 fail。
    """
    orchd_dir = Path(project_root) / ".orchd"
    try:
        from orchd.ledger import _STORAGE_BACKEND_ENV, resolve_backend_kind, resolve_store_dir

        store_dir = resolve_store_dir(orchd_dir)
        backend_kind = resolve_backend_kind()
        if backend_kind == "sqlite" and (store_dir / "_ledger.sqlite3").is_file():
            return _check_event_schema_sqlite(orchd_dir, store_dir)
        if backend_kind != "file" and backend_kind != "sqlite":
            raise ValueError(
                f"storage_backend_unknown: {_STORAGE_BACKEND_ENV}="
                f"{backend_kind!r} 非法（取值仅 file / sqlite，缺省 sqlite）"
            )
        # task-readonly-no-migrate：库缺失（含未迁移的 jsonl 部署）一律走文件
        # 逐行直读——撕裂行按 file 判据 fail（迁移洗白前先被看见），零副作用。
        return _check_event_schema_files(store_dir)
    except Exception as exc:  # NEW-DR4：检查自身异常（账本不可读）→ fail（消盲）
        # 同 _check_checkpoint_consistency：「未能判定」不可用非阻断的 warn 承载，
        # 用 fail 使盲区可见。
        return [
            _make_check(
                "event_schema",
                "fail",
                f"事件 schema 无法判定（{type(exc).__name__}: {exc}）"
                "——账本可能不可读，需人工检查账本完整性",
            )
        ]


def _check_event_schema_files(store_dir: Path) -> list[dict[str, str]]:
    """file 后端分支：逐行直读 ``_ledger.jsonl``（行为冻结）。

    由 :func:`_check_event_schema` 分发进入；字段判据委托
    :func:`_event_schema_problem`（与 sqlite 分支逐字一致），行号为文件物理行号。
    """
    import json

    ledger_path = store_dir / "_ledger.jsonl"
    if not ledger_path.is_file():
        return [_make_check("event_schema", "ok", "无账本文件，跳过")]
    bad: list[str] = []
    total = 0
    for lineno, line in enumerate(
            ledger_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        total += 1
        try:
            ev = json.loads(line)
        except ValueError:
            bad.append(f"L{lineno}: 非法 JSON")
            continue
        if not isinstance(ev, dict):
            bad.append(f"L{lineno}: 非 JSON 对象")
            continue
        problem = _event_schema_problem(ev)
        if problem:
            bad.append(f"L{lineno}: {problem}")
    if bad:
        return [
            _make_check(
                "event_schema",
                "fail",
                f"账本 {total} 条事件中有 {len(bad)} 条 schema 不合法：" +
                "；".join(bad[:5]) + ("..." if len(bad) > 5 else "") +
                "（replay 会跳过坏行并告警，需人工修正账本）",
            )
        ]
    return [_make_check("event_schema", "ok", f"{total} 条事件 schema 合法")]


def _check_event_schema_sqlite(
    orchd_dir: Path, store_dir: Path
) -> list[dict[str, str]]:
    """sqlite 后端分支：经 StorageBackend 端口逐条校验（只读）。

    纯 sqlite 部署下 ``_ledger.jsonl`` 缺失，旧文件逻辑直接 ok 跳过——此处改经
    ``Store``（与 ``ledger._select_backend`` 同一构造路径）取
    ``backend.read_events()`` 逐条做字段级校验（与 file 分支同判据，行号为逻辑
    序号）；``backend.corrupt_lines``（归档段损坏 + 迁移期跳过的源文件撕裂行）
    一并计入坏行。sqlite 行由事务保证完整，无 file 的撕裂行概念。

    只读无副作用：库文件缺失时由分发层改走文件直读，此处被调时库必存在；
    构造一律 ``Store(..., readonly=True)``（不建库、不迁移、不改名）。
    """
    from orchd.ledger import Store

    db_path = store_dir / "_ledger.sqlite3"
    if not db_path.is_file():
        return [_make_check("event_schema", "ok", "无账本，跳过")]
    store = Store(orchd_dir, readonly=True)
    events = store.backend.read_events()
    bad: list[str] = []
    for lineno, ev in enumerate(events, start=1):
        problem = _event_schema_problem(ev)
        if problem:
            bad.append(f"L{lineno}: {problem}")
    raw_corrupt = getattr(store.backend, "corrupt_lines", [])
    corrupt_entries = raw_corrupt if isinstance(raw_corrupt, list) else []
    for entry in corrupt_entries:
        if isinstance(entry, dict):
            line = entry.get("line", "?")
            bad.append(f"L{line}: {entry.get('message', '损坏行')}")
    total = len(events)
    if bad:
        return [
            _make_check(
                "event_schema",
                "fail",
                f"账本 {total} 条事件中有 {len(bad)} 条 schema 不合法：" +
                "；".join(bad[:5]) + ("..." if len(bad) > 5 else "") +
                "（replay 会跳过坏行并告警，需人工修正账本）",
            )
        ]
    return [_make_check("event_schema", "ok", f"{total} 条事件 schema 合法")]
