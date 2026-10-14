"""orchd doctor：git 仓库完整性检测 + 残留清理入口（--fix / --dry-run）。

task-git-doctor-command（2026-08-12，来源 IDEAS L295）：
把 2026-08-08 仓库事故（.git/refs/ 被删 + loose objects 丢失）沉淀的诊断步骤
工具化，供 session 三连检查脚本化复用。

task-audit-doctor-fix（2026-08-30，来源 idea:audit-engine-hardening-2026-08）：
把 doctor 升级为统一清理入口：先 dry-run 预览，再显式 --fix 执行，删除前自动备份。
当前清理入口高度散落：intake_lock_clear、session 锁清理、watchdog（只报告不执行）、
layout-migrate 清理，以及最原始的手工删文件。doctor 目前只有只读检测（check_repo），
无任何修复能力。

只读检测（check_repo）不触碰状态机 / 事件格式 / 既有 CLI 契约语义。
--fix 的**删除**动作受严格白名单约束：只清理引擎识别的运行时残留
（锁文件 / session runtime 文件），绝不**删除** _master.json / IDEAS.md /
_ledger.jsonl / _checkpoint.json，且该白名单须有测试守护。

白名单只防删除、不防追加（DR-9 契约修订，2026-09-15）：ghost 任务修复走
``retract_ghost`` 通道，会向 canonical 账本**追加** RETRACT 事件（事件级撤回，
不是文件删除）——这是本命令唯一的账本写副作用，且幂等有界（撤回后目标任务即
从 replay 派生结果消失，重复 --fix 不再追加，见 :func:`_ghost_retract_target`
与 :func:`doctor_fix` 的幂等守卫）。文件删除白名单不适用于该通道。

清理面的三条破坏范围收敛（2026-09-14 审核 DR-1 / DR-3 / DR-10，破坏范围不得越界）：
① 误提交锁只认 **orchd 自有锁**（``_LOCK_FILE_PATTERNS`` 命名白名单或 ``.orchd*``
   自有目录），仓库根第三方生态锁（``poetry.lock`` / ``Cargo.lock`` 等）是项目源码
   资产，绝不 untrack / 删除（见 :func:`_is_orchd_lock_path`）；
② 残留任务目录与陈旧提案草稿先移入 ``.doctor-backup``（可回滚），且**含真实工作
   内容者一律不删**（目录档见 :func:`_dispose_residual_dir` /
   :func:`_classify_residual_dir`；整文件搬移档见 :func:`_dispose_file_to_backup`，
   用于 ``action=move_to_backup`` 的注册后残留草稿——它是准入产物，不走 copy2 后
   删除的通用档，以免降级重命名把内容推进系统 temp 而脱离本桶保留期）；
③ 文件删除统一走 ``gitops.cleanup._safe_delete``（沙箱把 ``Path.unlink`` 劫持为
   「移入回收站」且回收站不可用时 FAIL_CLOSED 抛 OSError，裸 unlink 会让清理静默失败）。

与 gitops.py 同语义：任何 git 不可用 / 异常均 best-effort 降级为 fail 项，
不抛异常。

依赖方向：doctor.py → 标准库（subprocess / pathlib / shutil / json / time /
datetime）。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # 仅类型检查期导入（doctor 保持叶子性：运行期惰性导入 ledger）
    from orchd.ledger import Store

# git 输出统一按 UTF-8 解码（与 gitops.py 一致）
_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"
_GIT_TIMEOUT = 10

# refs/ 根目录扫描忽略的 OS 自动生成文件（macOS Finder 会在目录内生成
# .DS_Store、Windows 生成 Thumbs.db）：此类文件由文件系统自动再生，不属于
# 非法 loose ref，不应误报污染健康检查；与 worktree.py _JUNK_NAMES 中
# OS 杂项语义一致（doctor 为叶子模块，零 orchd 内部依赖，故在本文件内定义）。
_REFS_ROOT_IGNORE = frozenset({".DS_Store", "Thumbs.db"})

# 残留锁文件命名模式：**orchd 自有**运行时锁的权威白名单（单一事实源，消费方
# _is_orchd_lock_path → _detect_git_tracked_locks）。这些文件是 orchd 运行时产生
# 的临时文件，不属于项目源码，误提交入 git 后应 untrack + 删除。
# 名称来源核对：.intake.lock（ledger._INTAKE_LOCK_FILENAME）、.session.lock /
# .session.gate.lock（gitops.session_lock._SESSION_LOCK_FILENAME /
# _SESSION_GATE_FILENAME）、.session-<wt>.lock / .session-gate-<wt>.lock
# （session_lock 的 worktree 维度锁，见其 reclaim glob）。
# **绝不**用于识别仓库根第三方生态锁（poetry.lock 等），见 _THIRD_PARTY_LOCK_NAMES。
_LOCK_FILE_PATTERNS = (
    ".intake.lock",
    ".session.lock",
    ".session-*.lock",
    ".session-gate-*.lock",
    ".session.gate.lock",
)


def _is_lock_like_name(name: str) -> bool:
    """名称是否为 orchd 自有运行时锁（判据单一事实源：``_LOCK_FILE_PATTERNS``）。

    前导点容错：历史/兼容命名可能无前导点（如 ``session-gate-x.lock``），
    与 :func:`_detect_orphan_session_locks` 的双模式 glob 保持一致。
    """
    import fnmatch

    base = name.lstrip(".")
    return any(
        fnmatch.fnmatch(base, pat.lstrip(".")) for pat in _LOCK_FILE_PATTERNS)


def _pre_delete_lock_recheck(path: Path) -> str | None:
    """删除前复检 flock 活性（DR-7 单一事实源，--fix 与 auto_clean 共用）。

    检测与执行之间存在并发 claim / 准入写窗口，仍被持有说明存在活跃持有者
    （会话刚起 / 准入写进行中）→ 本轮不删，留待下轮判定，绝不按旧快照误删
    活动状态。非锁类名恒返回 None（可删）。

    Returns:
        None = 可删；否则为 skip 原因文案（含复检异常的 fail-closed 保守跳过）。
    """
    try:
        from orchd.lockfile import ExclusiveFileLock

        held_now = bool(
            _is_lock_like_name(path.name)
            and ExclusiveFileLock(path).check().get("held", False))
        held_error = None
    except Exception as exc:
        # fail-closed（task-lock-probe-fail-closed）：复检本身异常
        # （锁路径瞬断 / 权限异常）无法判定 → 保守视为持有并跳过删除。
        held_now = True
        held_error = f"{type(exc).__name__}: {exc}"
    if not held_now:
        return None
    if held_error:
        return f"删除前复检异常（{held_error}），保守视为持有，本轮跳过"
    return "删除前复检发现锁仍被持有（并发窗口），本轮跳过"


# 第三方生态锁文件白名单（DR-1）：这些是**项目源码资产**，与 orchd 运行时无关，
# doctor 绝不 untrack / 删除（原实现按 ``*.lock`` 全量判定，会把它们越界销毁）。
_THIRD_PARTY_LOCK_NAMES = frozenset({
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "yarn.lock",
    "composer.lock",
    "packages.lock.json",
    "flake.lock",
    "bun.lock",
})

# 残留 session runtime 文件模式（sessions/ 目录下的 JSON 文件）。
# 这些是 session 心跳/状态记录。**判死口径不在此处定义**：单一真源是
# ``ledger.is_session_expired`` / ``session_ttl_minutes``（见 _detect_zombie_sessions
# 的 docstring）。此处曾自带一个 1800 秒（30 分钟）的死 TTL 常量并按文件 mtime
# 判死，与 ledger 的默认 1440 分钟相差 48 倍 ⇒ 会删掉账本仍判活的会话身份文件。
_SESSION_FILE_PREFIX = "sessions/"
_SESSION_FILE_SUFFIX = ".json"

# 保护白名单：--fix 绝不触碰这些文件/目录。
# 硬编码在 doctor.py 内，确保即使调用方误用也不会损伤核心状态。
# 分两类：源码资产（与位置无关恒保护）与运行时状态（canonical 账本根保护、
# legacy 位置可清——container 布局下 main/.orchd 的 flat 遗留属于可清残留，
# 见 _detect_legacy_flat_residues）。
# 源码资产：与位置无关恒保护。_full_regression.json 是发版元数据产物（full-regression
# 命令与 sync_orchd_core.sh 恒读写 <源码仓库>/.orchd/_full_regression.json，不论布局），
# 归 _SOURCE_ASSETS 而非 _RUNTIME_STATE_FILES，避免 container 布局下被
# _detect_legacy_flat_residues 误判为 flat 遗留而搬走（task-doctor-full-regression-alignment）。
_SOURCE_ASSETS = frozenset({
    "_master.json",
    "IDEAS.md",
    "IDEAS-archive.md",
    "ROADMAP.md",
    "_full_regression.json",
})
class _RuntimeStateFiles(frozenset):
    """运行时文件集合 + 原子写 pid-tmp 谓词（task-sqlite-robustness-batch）。

    精确成员即构造传入的 11 个状态文件名；``in`` 另对「已知状态文件的
    ``.<pid>.tmp`` 形」（如 ``_master.json.12345.tmp``，原子写崩溃残留）返回
    True——变更检测（nogit hash，经同一对象别名引用）忽略它们，git 侧本就
    被 ``.orchd/*`` 忽略。迭代 / 并集保持精确成员语义：legacy 残留扫描只走
    精确名，``_is_protected_path`` 对疑似存活 tmp 保持保护（绝不删可能仍在
    写的 tmp，宁留不可见残留）。
    """

    _PID_TMP_RE = re.compile(r"^(?P<stem>.+)\.(?P<pid>\d+)\.tmp$")

    def __contains__(self, name: object) -> bool:
        if super().__contains__(name):
            return True
        if isinstance(name, str):
            m = self._PID_TMP_RE.match(name)
            if m and m.group("stem") in (_SOURCE_ASSETS | frozenset(self)):
                return True
        return False


# 引擎运行时文件清单（task-nogit-runtime-list-single-source：单一真源）。
# 本集合是「.orchd/ 子树内引擎自写状态文件」的唯一成员枚举，供两处消费：
#   ① nogit.hash_tree 快照排除（经 orchd.nogit._HASH_EXCLUDED_FILES 别名引用，
#      同一对象）——无 git 单目录下引擎自写（merge-ack 清单 / 布局标记 /
#      课程库）不得被判成项目改动；
#   ② 本文件 _detect_legacy_flat_residues / _is_protected_path（canonical 保护、
#      legacy 可清）。
# 约束：源码内不得另立第二份成员枚举（负控制见
# tests/test_nogit_changedet.py::TestSingleSourceRuntimeList）。
# .layout.json 是布局标记（布局解析权威，不可清理）：legacy 扫描须跳过
# （见 _detect_legacy_flat_residues），仅快照排除与 canonical 保护消费它。
_RUNTIME_STATE_FILES = _RuntimeStateFiles({
    "_ledger.jsonl",
    "_checkpoint.json",
    "session-worktrees.json",
    "merge-acks.json",
    "lessons.jsonl",
    "lessons.staged.jsonl",
    ".layout.json",
    # sqlite 迁移备份（task-runtime-list-migration-backups）：改名残留的旧载体，
    # 同属引擎运行时状态——无 git 快照不得计入项目变更（此前漏成员，与旧事故同型）。
    "_ledger.jsonl.pre-sqlite-bak",
    "_checkpoint.json.pre-sqlite-bak",
    # storage-recutover 消歧备份（task-storage-recutover-merge-channel）：回滚残留
    # 并账后改名的活名快照，同为引擎自写且长期保留——漏登记即重演上一条同型事故。
    "_ledger.jsonl.recutover-bak",
    "_checkpoint.json.recutover-bak",
})
_PROTECTED_PATHS = _SOURCE_ASSETS | _RUNTIME_STATE_FILES


def _run_git(project_root: Path,
             args: list[str]) -> subprocess.CompletedProcess[str]:
    """以 UTF-8 解码运行 git 命令（cwd 限定 project_root），超时降级。"""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        # git 不可用 / 超时 / 路径异常：返回失败态，由调用方判 fail
        return subprocess.CompletedProcess(["git", *args],
                                           returncode=1,
                                           stdout="",
                                           stderr="")


def _make_check(name: str, status: str, hint: str) -> dict[str, str]:
    """构造单个诊断项。

    status 三值：``ok``（无问题）/ ``fail``（查出问题或未能判定 → 进 issues、
    repo_ok=False、CLI 退出码 1）/ ``warn``（**非阻断**告警：数据停滞一类，
    不进 issues、不翻 repo_ok、退出码不变，只经 doctor().warnings 透出——
    见文件头 INV-4a 词汇表节的第 5 条）。hint 为提示。
    """
    return {"name": name, "status": status, "hint": hint}


# ---------------------------------------------------------------------------
# 「检测不可用」（detection unavailable）统一语义
# task-detection-fail-closed-doctor-ledger（第三轮审查 INV-4a）
# ---------------------------------------------------------------------------
# 问题：检测/审计助手曾普遍以 `except Exception: return []` 把「检测没能跑起来」
# 伪装成「检测结果 = 无问题」。假阴性比误报危险（errors.py 门禁三分类亦明写
# 「校验故障 → 必须阻断或至少升级为 E030 告警，绝不静默」），且与同文件已有口径
# 冲突：_check_checkpoint_consistency 按 NEW-DR4 已把「无法判定」判 fail。
#
# 定稿语义（**复用现有词汇表**：不新增错误码、不新增 status 取值；第 5 条是后续
# 任务对该词汇表的一次显式扩张，改的是「停滞」一档，本块 1-4 的「未能判定 = fail」不变）：
#   1. 检查通道（check_repo 家族）：检测异常 → _make_check_unavailable()，
#      status="fail"（与 NEW-DR4 同口径）→ doctor().repo_ok=False → 只读模式
#      CLI 非零退出码；hint 以「检测不可用（<异常类型>: <消息>）……结果不可信
#      （可能漏报）」措辞，与「查出问题」在文本上可区分。
#   2. 残留通道（detect_residues 家族）：检测异常 → _unavailable_residue()，
#      type/action = detection_unavailable（action 属 _MANUAL_ACTIONS → 恒 manual 档）
#      → 随既有 manual 通道计入 doctor().issues → repo_ok=False；doctor --fix 将其
#      归入 skipped_manual **不执行任何删除**（关键安全属性：未知 action 在 fix
#      循环里会落到默认删除分支，故必须显式登记为人工档）。
#   3. 结果级结构化表达：doctor() 透出 detection_unavailable（检测名列表，来源 =
#      不可用检查项名 + 不可用残留项的 detector）与 degraded（bool）——供 agent
#      区分「查出问题该修」与「没查成该人工介入」。二者都由 repo_ok 统一兜底。
#      检查项自身的标记靠 ``unavailable`` 键（值 = 异常摘要，非空即「未能判定」）；
#      检查项 dict 维持 ``dict[str, str]``（不因标记字段把类型面拉宽）。
#   4. ledger 侧（Store._corrupt_line_warnings）：E030 warning 通道按既有契约
#      「仅告警、不阻断」，可见性靠条目内 detection_unavailable=True 标记；它不改
#      退出码，与 doctor 通道强度不同但都**不再静默**。
#   5. 词汇表扩张（task-lessons-transit-migrate-wired-doctor-warn，pass13 §11-9
#      甲案）：新增第三值 ``warn`` = 非阻断告警。理由——lessons 的「库不存在 /
#      暂存从未晋升 ⇒ 注入恒 0」是**数据停滞**，不是仓库完整性故障；判 fail 会把
#      停滞与损坏同档（三连检查脚本化消费的是退出码，见 query.py 只读模式
#      ``repo_ok`` → 1），判 ok 则延续假阴性（P1-6 症状正是「同一个症状换一个
#      盲区继续存在」）。实现上 warn 的强度由聚合唯一处天然保证：
#      ``issues = [c for c if status == "fail"]`` 不收 warn → repo_ok / 退出码不变，
#      可见性靠 doctor() 新增顶层 ``warnings`` 清单（与 issues 对称，不改 checks 项形状）。
#
# 同族「已评估（本轮不改造）」清单——避免下一轮重复盘点（AC4）：
#   - doctor.py:170 `_detect_stale_worktree_registry`：`git worktree list` 非零 →
#     []。git 不可用是**合法环境态**（非 git 仓库 / 超时），判不可用会在非 git
#     目录与 CI 最小场景制造误报；其盲区由 check_repo 的 git_dir/refs_dir 检查兜底。
#   - doctor.py:1025 `_detect_residual_dirs` 内 `_is_effective_worktree` 异常 →
#     continue（**逐项**判定失败）：属「该项无法判定」，保守跳过不误报；改为不可用
#     会把单个坏项放大成整轮检测失败，待评估（同类：1225 锁活性复检已记 reason）。
#   - doctor.py `_detect_zombie_sessions` 单文件读取/解析失败 → continue：单文件无法
#     判定，文件本体仍在（不构成"检测不可用"盲区）。
#   - doctor.py:1304/1313/1329/1331/1336 `_detect_residual_intake_locks` /
#     `_detect_git_tracked_locks`：逐项解析/stat 失败 → continue，同上属项级，
#     phase 留待「项级不可用」语义统一时评估。
#   - doctor.py:220 `_run_git` 失败 → `{n: "missing"}`：已有下游 fail 判据
#     （check_repo 据此报 git_dir / refs_dir fail），出口已可见。
#   - doctor.py:661 `_check_event_schema` 非法 JSON 行 → 记入 bad 列表：非法行本身
#     就是**被检测对象**（正常分支），非检测失败。
#   - doctor.py:780 `_load_master_task_ids`：master **不存在** → None（不过滤，
#     合法最小场景）；master 存在但不可解析 → 抛异常，调用方转「检测不可用」
#     留痕（INV-4a，不静默）。语义是放宽过滤（仍全量扫描）或显式不可用，
#     不产生盲区。
#   - ledger.py:267/638/1630/1796 解析辅助返回 None：值级不可用，调用方显式处理。
_DETECTION_UNAVAILABLE_RESIDUE = "detection_unavailable"

# 不可 stat / 不可判定残留项（task-doctor-master-dir-contract 层 2）：
# 与 detection_unavailable 同属「没能判定」家族，但**粒度与强度不同**——
#   detection_unavailable = 整个检测器没跑起来（结果整体不可信 → 判失败，INV-4a）；
#   unreadable            = 单项路径不可 stat（不可遍历 reparse point / 断链 /
#                           权限不足），其余项照常检出（结果仍可信）→ **留痕豁免、
#                           不判失败**，且绝不进任何自动清理通道（不可判定项被搬移
#                           或删除 = 不可逆损失）。
# 区分「不存在」与「不可解析」是本类型的全部意义：前者是合法态（旧实现据此返回
# 空），后者必须留痕——把 448 当「文件不存在」正是本机幽灵检测静默失明的成因。
_UNREADABLE_RESIDUE = "unreadable"
_UNREADABLE_ACTION = "unreadable_manual"


def _make_check_unavailable(name: str, exc: BaseException,
                            subject: str) -> dict[str, str]:
    """构造「检测不可用」检查项：status=fail（→ repo_ok=False / 非零退出码）。

    与 NEW-DR4 在 :func:`_check_checkpoint_consistency` 的处置同口径——「未能判定」
    一律以 fail 表达（warn 档只承载「已判定为数据停滞」，不承载「没查成」，见 INV-4a
    第 5 条）；「没查成」与「查出问题」的结构区分由
    ``unavailable`` 键（值 = 异常摘要，非空即「未能判定」）与 :func:`doctor` 结果中的
    ``detection_unavailable`` / ``degraded`` 共同承担。
    """
    reason = f"{type(exc).__name__}: {exc}"
    return {
        "name": name,
        "status": "fail",
        "hint": (f"{subject}：检测不可用（{reason}）"
                 "——结果不可信（可能漏报），需人工核对"),
        "unavailable": reason,
    }


def _unavailable_residue(detector: str, exc: BaseException,
                         path: str | Path) -> dict[str, Any]:
    """构造「检测不可用」残留项（type/action = detection_unavailable，恒 manual 档）。

    ``detector`` 为检测器名（由 :func:`doctor` 汇总进 ``detection_unavailable``）；
    ``path`` 取检测根，便于人工核对。action 已登记进 ``_MANUAL_ACTIONS``，故
    ``doctor --fix`` 归入 ``skipped_manual``、**不执行删除**（未知 action 在 fix
    循环中会落到默认删除分支，登记人工档是必须的安全约束，见 tests 负控制）。
    """
    return {
        "path": str(path),
        "type": _DETECTION_UNAVAILABLE_RESIDUE,
        "detector": detector,
        "reason": (f"{detector} 检测不可用（{type(exc).__name__}: {exc}）"
                   "——结果不可信（可能漏报），需人工核对"),
        "action": _DETECTION_UNAVAILABLE_RESIDUE,
    }


def _unreadable_residue(detector: str, exc: BaseException, path: str | Path,
                        *, stage: str) -> dict[str, Any]:
    """构造「不可 stat / 不可判定」残留项（type=unreadable，恒 manual 档）。

    与 :func:`_unavailable_residue` 的区别见 ``_UNREADABLE_RESIDUE`` 注释：本项
    表示**单个**路径不可判定，同批其余项已正常检出；故卫生门禁按 waived 留痕
    （可见、不判失败），而不是像 detection_unavailable 那样判失败。

    ``stage`` 记录不可判定发生的具体阶段（如 ``master_stat`` / ``entry_stat`` /
    ``iterdir``），供人工定位；``error`` 保留原始异常报文（WinError 448 这类平台
    报文是唯一的现场证据，不得被概括掉）。

    ``action`` 登记进 ``_MANUAL_ACTIONS`` → ``doctor --fix`` 归入 skipped_manual，
    **不执行任何搬移/删除**（不可判定项进清理通道 = 不可逆）。
    """
    return {
        "path": str(path),
        "type": _UNREADABLE_RESIDUE,
        "detector": detector,
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}",
        "reason": (f"路径不可 stat / 不可判定（{stage}：{type(exc).__name__}: {exc}）"
                   "——不可遍历 reparse point / 断链 / 权限不足，需人工确认；"
                   "不进自动清理通道"),
        "action": _UNREADABLE_ACTION,
    }


def _probe_is_file(path: Path) -> tuple[bool | None, BaseException | None]:
    """判定路径是否为常规文件，**区分「不存在」与「不可判定」**。

    Returns:
        - ``(True, None)``  ：是常规文件；
        - ``(False, None)`` ：不存在或不是常规文件（**合法态**，不是故障）；
        - ``(None, exc)``   ：stat 抛 ``OSError``（不可遍历 reparse point /
          断链 / 权限）→ **不可判定**，调用方必须留痕。

    存在意义（task-doctor-master-dir-contract 层 2）：``Path.is_file()`` 在
    Windows 不可遍历的 Mount Point 上抛 ``OSError [WinError 448]`` 而不是返回
    False；旧实现直接把它当「不存在」→ 幽灵检测静默空返回（INV-4a 反例）。
    """
    try:
        return path.is_file(), None
    except OSError as exc:
        return None, exc


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


def _resolve_git_dir(project_root: Path, git_dir: str) -> Path:
    """把 git rev-parse 输出的 gitdir 路径解析为绝对路径。

    git 输出可能是绝对路径（worktree 场景）或相对 project_root 的路径
    （普通仓库输出 ``.git``）。统一解析为绝对路径供后续文件访问。
    """
    p = Path(git_dir)
    if not p.is_absolute():
        p = Path(project_root) / p
    return p.resolve()


def _cat_file_batch(project_root: Path, names: list[str]) -> dict[str, str]:
    """一次 `git cat-file --batch-check` 批量查询多个对象的类型（P2a 优化）。

    相比逐个 `git cat-file -t` 派生 N 次子进程，仅派 1 次子进程，O(N)→O(1)。
    从 stdin 逐行读入对象名，解析 `<name> <type> <size>`；type 为 missing 或
    子进程失败/超时时，对应对象判为不可达。

    Returns:
        {name: type} 映射；type 为 "missing" 表示对象不可达。
    """
    if not names:
        return {}
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch-check"],
            cwd=str(project_root),
            input="".join(f"{n}\n" for n in names),
            capture_output=True,
            encoding=_GIT_ENCODING,
            errors=_GIT_ERRORS,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        # git 不可用 / 超时：全部判为不可达
        return {n: "missing" for n in names}
    result: dict[str, str] = {}
    if proc.returncode != 0:
        return {n: "missing" for n in names}
    # git cat-file --batch-check 对每个输入按序输出一行 `<obj> <type> <size>`，
    # 对象缺失时输出 `<obj> missing`。第一列可能是解析后的对象名（hash），
    # 因此按输出顺序与输入名 zip 对齐，取第二列（type）归属到对应输入。
    for name, line in zip(names, proc.stdout.splitlines()):
        parts = line.split()
        if len(parts) >= 2:
            result[name] = parts[1]
    # 未出现在输出中的对象（git 对非法输入可能静默）判为不可达
    for n in names:
        result.setdefault(n, "missing")
    return result


def check_repo(project_root: Path) -> list[dict[str, str]]:
    """检测 git 仓库完整性，返回诊断项列表（ok/fail + 提示）。

    覆盖四类检查（2026-08-08 仓库事故沉淀的诊断步骤）：
    1. 仓库有效（git rev-parse 可解析，兼容普通仓库与 git worktree）；
    2. common refs/ 目录存在（事故：refs/ 目录被删）；
    3. HEAD 可解析（HEAD 文件存在、指向的 ref / 对象可达）；
    4. 引用对象可达（refs/heads/* 与 reflog 最新哈希可 cat-file -t，
       覆盖 loose objects 丢失场景）。

    仓库定位完全依赖 git 解析（rev-parse --git-common-dir），而非假定
    `<root>/.git` 一定是目录：在 git worktree 中 `.git` 是指向真实 gitdir
    的指针文件（内容 `gitdir: ...`），普通仓库才是目录。用 is_dir() 判断
    会让 worktree 健康仓库被误判为"损坏"（2026-08-13 二次审核发现）。
    """
    checks: list[dict[str, str]] = []

    # 1) 仓库有效（兼容普通仓库 / worktree / refs 被删的损坏场景）。
    #    注：`--is-inside-work-tree` 在 refs/ 被删后可能失败，故不以它为唯一判据；
    #    以 `--git-common-dir` 能否解析定位真实 gitdir 为准。
    common = _run_git(project_root, ["rev-parse", "--git-common-dir"])
    if common.returncode != 0 or not common.stdout.strip():
        # 区分"非 git 目录"与"含 .git 但解析失败（疑似 refs/ 被删的损坏）"。
        # 前者判 git_dir fail；后者定位到 refs_dir fail（更贴近 2026-08-08 事故模式），
        # 并回退按 `<root>/.git` 判定 refs 缺失。
        dotgit = Path(project_root) / ".git"
        if dotgit.exists():
            refs_dir_candidate = dotgit / "refs"
            if not refs_dir_candidate.is_dir():
                return [
                    _make_check(
                        "refs_dir",
                        "fail",
                        "git refs/ 目录缺失——2026-08-08 事故模式，恢复见 SKILL.md 仓库事故恢复 SOP",
                    )
                ]
            return [
                _make_check(
                    "git_dir",
                    "fail",
                    "git rev-parse 无法解析且 .git 存在（仓库损坏）",
                )
            ]
        return [
            _make_check(
                "git_dir",
                "fail",
                "git rev-parse 无法解析（非 git 仓库或仓库损坏）",
            )
        ]
    git_dir = _resolve_git_dir(project_root, common.stdout.strip())

    # 2) common refs/ 目录存在
    refs_dir = git_dir / "refs"
    if not refs_dir.is_dir():
        checks.append(
            _make_check(
                "refs_dir",
                "fail",
                "git refs/ 目录缺失——2026-08-08 事故模式，恢复见 SKILL.md 仓库事故恢复 SOP",
            ))
    else:
        checks.append(_make_check("refs_dir", "ok", "git refs/ 目录存在"))

    # 3) HEAD 可解析
    head_path = git_dir / "HEAD"
    head_target: str | None = None
    if not head_path.exists():
        checks.append(_make_check("head", "fail", "git HEAD 缺失，无法定位当前分支"))
    else:
        head_text = head_path.read_text(encoding="utf-8",
                                        errors="replace").strip()
        if head_text.startswith("ref: "):
            head_target = head_text[5:].strip()
        rev = _run_git(project_root,
                       ["rev-parse", "--verify", "--quiet", "HEAD"])
        if rev.returncode == 0 and rev.stdout.strip():
            checks.append(
                _make_check("head", "ok",
                            f"HEAD 可解析（{head_target or 'detached'}）"))
        else:
            checks.append(
                _make_check(
                    "head",
                    "fail",
                    f"HEAD 无法解析（{head_text}）——指向的 ref 或对象已丢失",
                ))

    # 4a) refs/ 根目录非法 loose ref 检测（task-p1-doctor-refs-scan）
    # refs/ 根目录下的非目录文件（如 .DS_Store、临时文件）属于非法 loose ref，
    # 会污染 refs 命名空间；合法的直接子项只有 heads/tags/remotes 等目录。
    # 注：仅扫描 refs/ 根目录一层，不递归到 refs/heads/* 等子目录（那是合法 ref）。
    illegal_refs_root: list[str] = []
    if refs_dir.is_dir():
        for entry in sorted(refs_dir.iterdir()):
            if entry.is_dir():
                # 合法子目录（heads/tags/remotes/... 或命名空间目录），跳过
                continue
            # OS 自动生成文件（.DS_Store / Thumbs.db）不属于非法 loose ref，跳过
            if entry.name in _REFS_ROOT_IGNORE:
                continue
            # 内容为合法 40 位 hex 对象哈希的 loose ref（如 refs/stash 等 git 合法
            # 伪 ref）属于合法 ref，跳过；仅内容非哈希的才是非法 loose ref
            try:
                if re.fullmatch(r"[0-9a-fA-F]{40}",
                                entry.read_text(encoding="ascii").strip()):
                    continue
            except OSError:
                pass
            # 非目录文件且非合法 loose ref 即为非法（临时文件 / 手工乱建等）
            illegal_refs_root.append(entry.name)
    if illegal_refs_root:
        checks.append(
            _make_check(
                "refs_root",
                "fail",
                "refs/ 根目录存在非法 loose ref（非目录文件）：" +
                "、".join(illegal_refs_root) + "——详见 SKILL.md 仓库事故恢复 SOP",
            ))
    else:
        checks.append(_make_check("refs_root", "ok", "refs/ 根目录无非法 loose ref"))

    # 4) 引用对象可达（refs/heads/* + reflog 最新哈希）
    # P2a：收集全部候选对象，一次 `git cat-file --batch-check` 批量查询（O(N)→O(1) 子进程）。
    # 注：worktree 的 reflog 位于其专属 gitdir（worktrees/<name>/logs/HEAD），
    # 与 common refs 分离；reflog 若有则读取，缺失不判错（普通精简仓库无 reflog）。
    reachable = True
    ref_hints: list[str] = []
    object_names: list[str] = []
    ref_labels: list[str] = []
    if refs_dir.is_dir():
        heads_dir = refs_dir / "heads"
        if heads_dir.is_dir():
            for ref_file in sorted(heads_dir.glob("*")):
                if not ref_file.is_file():
                    # 命名空间子目录（如 refs/heads/task/xxx），非直接 ref，跳过
                    continue
                ref_name = f"refs/heads/{ref_file.name}"
                object_names.append(ref_name)
                ref_labels.append(ref_name)
    # worktree 专属 gitdir 的 reflog（若存在）
    wtree_gitdir = _resolve_git_dir(
        project_root,
        _run_git(project_root, ["rev-parse", "--git-dir"]).stdout.strip())
    reflog = wtree_gitdir / "logs"
    if reflog.is_dir():
        head_log = reflog / "HEAD"
        if head_log.exists():
            lines = [
                ln.strip()
                for ln in head_log.read_text(encoding="utf-8",
                                             errors="replace").splitlines()
                if ln.strip()
            ]
            if lines:
                parts = lines[-1].split()
                if len(parts) >= 2:
                    last_hash = parts[1]
                    object_names.append(last_hash)
                    ref_labels.append(f"reflog 最新提交 {last_hash}")
    types = _cat_file_batch(project_root, object_names)
    for name, label in zip(object_names, ref_labels):
        if types.get(name) in (None, "missing"):
            reachable = False
            ref_hints.append(f"{label} 对象不可达")

    if reachable:
        checks.append(_make_check("objects", "ok", "refs/reflog 引用对象均可达"))
    else:
        checks.append(
            _make_check(
                "objects",
                "fail",
                "引用对象不可达：" + "；".join(ref_hints) or "对象丢失",
            ))

    # 5) P0-19：残留任务 worktree 空目录检测（Windows git worktree remove 不完整）。
    # 扫描根用 detect_layout 的 task_wt_root（与 prune_orphans 一致）：container →
    # 容器根；flat 无任务 worktree 概念 → 直接 ok（不再用 main_wt.parent 硬编码，
    # 避免 flat 布局误报主工作树父目录的无关 task-* 目录）。
    try:
        from orchd.worktree import detect_layout

        layout = detect_layout(Path(project_root))
        task_wt_root = layout.get("task_wt_root")
        if layout.get("layout") != "container" or task_wt_root is None:
            checks.append(
                _make_check("worktree_residual", "ok",
                            "flat 布局无任务 worktree，跳过"))
        elif Path(task_wt_root).is_dir():
            # DR-4：判定与清理范围**单一事实源**——复用 _detect_residual_dirs
            # （含 session-worktrees 活跃绑定过滤 + 内容分级），不再各自手写扫描；
            # 旧实现只按「无 .git 登记」判定，会把活跃绑定的 worktree 报成残留
            # （检测说 A、修复做 B），且对含真实内容的目录给出与 --fix 相反的口径。
            items = _detect_residual_dirs(Path(project_root))
            removable = [i for i in items if i.get("action") == "delete_dir"]
            manual = [
                i for i in items if i.get("action") == "residual_dir_manual"
            ]
            if items:
                names = "、".join(Path(i["path"]).name for i in items[:10])
                suffix = "..." if len(items) > 10 else ""
                detail = []
                if removable:
                    detail.append(f"{len(removable)} 个可自动清理"
                                  "（运行 orchd doctor --fix，先移入 .doctor-backup）")
                if manual:
                    detail.append(f"{len(manual)} 个含真实内容需人工确认（--fix 不自动删除）")
                checks.append(
                    _make_check(
                        "worktree_residual",
                        "fail",
                        f"发现 {len(items)} 个残留任务 worktree 目录"
                        f"（无 .git 登记且无活跃绑定）：{names}{suffix}；" +
                        "；".join(detail) + "。",
                    ))
            else:
                checks.append(
                    _make_check("worktree_residual", "ok",
                                "无残留任务 worktree 目录"))
        else:
            checks.append(
                _make_check("worktree_residual", "ok", "task_wt_root 不存在"))
    except OSError:
        pass

    # 5b) P0-19b：stale worktree 注册（有 git 登记、工作目录已不存在 = prunable）。
    # 与 worktree_residual 方向相反：后者扫「目录存在但无 .git 登记」，本条扫
    # 「.git/worktrees 有登记但目录已删」。doctor --fix 执行 git worktree prune。
    stale = _detect_stale_worktree_registry(project_root)
    if stale:
        paths = [s["path"] for s in stale]
        checks.append(
            _make_check(
                "worktree_stale_registry",
                "fail",
                f"发现 {len(stale)} 个 prunable worktree 注册（目录已不存在但 git 仍登记）：" +
                "、".join(Path(p).name for p in paths[:10]) +
                ("..." if len(paths) > 10 else "") +
                "。运行 orchd doctor --fix 执行 git worktree prune 清理。",
            ))
    else:
        checks.append(
            _make_check("worktree_stale_registry", "ok",
                        "无 prunable worktree 注册"))

    # 6) in_review 任务 worktree/分支完整性（2026-08-30 分支丢失复盘 §3）：
    # in_review 是审查等待期（任务可能 idle 数小时），恰是误删高危窗口；
    # 任一 in_review 任务分支/worktree 缺失即 fail，附重建命令模板（只读不修）。
    checks.extend(_check_in_review_worktree_integrity(project_root))

    # 7) container 任务 worktree 不变量：._master.json 单副本（唯一权威 = 主工作树）
    checks.extend(_check_master_single_copy(project_root))

    # 7b) checkpoint ↔ ledger 一致性（DR-14）：滞后/超前/版本不符
    checks.extend(_check_checkpoint_consistency(project_root))

    # 7c) 账本事件 schema 合法性（DR-14）：坏行 / 缺字段 / 字段类型错误
    checks.extend(_check_event_schema(project_root))

    # 7c2) 迁移备份一致性（task-migrate-backup-atomicity）：库已导入却残留
    # 活名文件（中断迁移 / 回滚残留）即 mismatch fail，可观测不静默
    checks.extend(_check_migration_consistency(project_root))

    # 7d) 僵持 git 进程盘点（task-doctor-proc，F8/E-11）：并发 full-regression
    # 曾遗留 13 个僵持 git 进程拖慢 checkout，而检测面长期无进程维度。
    checks.extend(_check_hung_git_processes())

    # 7e) 契约漂移节（P5-②）：生成物新鲜度 + manifest checker 缺失
    checks.extend(_check_contract_drift(project_root))

    # 7f) lessons 运行时观测（task-lessons-runtime-migrate-observe，只读告警）
    checks.extend(_check_lessons_runtime(project_root))

    return checks


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


# 生成物目录（P5-②）：与 scripts/gen_contract_docs.py::GENERATORS 逐字三锁
# （本元组 / GENERATORS 键 / docs/_generated 落盘文件，不一致即
# tests/contract/test_doctor_drift.py 元测试变红）。
_GENERATED_DOCS = (
    "error-codes.md",
    "commands.md",
    "fields.md",
    "events.md",
    "statemachine.md",
)

_GENERATED_MARKER = "生成物，勿手改"


def _check_contract_drift(project_root: Path) -> list[dict[str, str]]:
    """契约漂移节（P5-②，只读）：生成物新鲜度 + manifest checker 缺失。

    - 生成物：``docs/_generated/<name>`` 存在、非空、含生成物标记；
      缺失/空/无标记 → fail（内容逐字新鲜度由 test 生成物断言 + CI
      freshness 步骤覆盖，本节只守存在性结构）。
    - manifest：``tests/contract/contract_manifest.py`` 中登记的测试文件
      逐个存在；缺失 → fail。登记表解析用 AST 取字面量（不 import 测试包，
      避免引擎依赖测试代码）。
    """
    import ast

    root = Path(project_root)
    problems: list[str] = []
    manifest = root / "tests" / "contract" / "contract_manifest.py"
    if not manifest.is_file():
        return [_make_check(
            "contract_drift", "ok",
            "非契约开发仓（无 manifest 登记表），漂移节跳过")]
    generated_dir = root / "docs" / "_generated"
    for name in _GENERATED_DOCS:
        path = generated_dir / name
        if not path.is_file():
            problems.append(f"生成物缺失：docs/_generated/{name}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"生成物不可读：docs/_generated/{name}（{exc}）")
            continue
        if not text.strip() or _GENERATED_MARKER not in text:
            problems.append(f"生成物异常（空或无标记）：docs/_generated/{name}")
    try:
        tree = ast.parse(manifest.read_text(encoding="utf-8"))
        referenced = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("tests/")
        }
        for rel in sorted(referenced):
            if not (root / rel).is_file():
                problems.append(f"checker 缺失：{rel}")
    except (OSError, SyntaxError) as exc:
        problems.append(f"登记表不可解析：{exc}")
    if problems:
        return [_make_check(
            "contract_drift", "fail",
            f"契约漂移 {len(problems)} 项：" + "；".join(problems[:5]) +
            ("..." if len(problems) > 5 else ""))]
    return [_make_check(
        "contract_drift", "ok",
        f"生成物 {len(_GENERATED_DOCS)} 个在册且带标记；manifest 登记测试文件齐备")]


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


def _check_master_single_copy(project_root: Path) -> list[dict[str, str]]:
    """container 任务 worktree 残留 ``.orchd/_master.json`` 巡检（task-master-single-copy）。

    唯一权威 = 主工作树的 ``.orchd/_master.json``；container 布局下任务 worktree
    由 sparse-checkout/skip-worktree 抑制副本。若某任务 worktree 仍存在
    ``.orchd/_master.json`` → 不变量被破坏（副本漂移风险），报 fail 并附修复 hint。
    flat 布局本就不建任务 worktree（单 worktree，副本在主工作树）→ 直接 ok。
    只读检测；**无**容器标记 → ok（正常不适用）；标记/字段解析异常或目录扫描异常
    → 「检测不可用」fail（INV-4a：此前 `return []` 使该检查从报告中静默消失）。
    """
    try:
        from orchd.worktree import detect_layout
    except Exception as exc:  # INV-4a：检测不可用（此前 return [] 静默消失）
        return [_make_check_unavailable("master_single_copy", exc, "布局探测不可用")]
    try:
        layout = detect_layout(project_root)
    except Exception as exc:  # INV-4a：与「非 container 布局」区分（旧实现混在同一 except）
        return [_make_check_unavailable("master_single_copy", exc, "布局解析异常")]
    if layout.get("layout") != "container":
        return [
            _make_check("master_single_copy", "ok",
                        "flat 布局：无任务 worktree，无需单副本巡检")
        ]
    try:
        task_root = Path(layout["task_wt_root"])
        canonical = Path(layout["main_worktree"]).resolve()
    except Exception as exc:  # INV-4a：标记字段缺失/非法 → 不可用（判据本身未改）
        return [_make_check_unavailable("master_single_copy", exc, "布局字段缺失/非法")]
    if not task_root.is_dir():
        return [
            _make_check("master_single_copy", "ok", "无任务 worktree 目录，单副本不变量成立")
        ]
    residual: list[str] = []
    try:
        for child in task_root.iterdir():
            if not child.is_dir():
                continue
            # 唯一权威 = 主工作树，其自身的 .orchd/_master.json 必在，不算残留
            if child.resolve() == canonical:
                continue
            wt_master = child / ".orchd" / "_master.json"
            if wt_master.exists():
                residual.append(str(child))
    except OSError as exc:  # INV-4a：扫描异常 = 盲区，不得报 ok（此前"扫描异常，跳过"）
        return [
            _make_check_unavailable("master_single_copy", exc,
                                    "任务 worktree 目录扫描异常")
        ]
    if not residual:
        return [
            _make_check(
                "master_single_copy", "ok",
                "container 任务 worktree 无 .orchd/_master.json 副本（单副本不变量成立）")
        ]
    return [
        _make_check(
            "master_single_copy",
            "fail",
            f"{len(residual)} 个任务 worktree 残留 .orchd/_master.json（唯一权威 = 主工作树 "
            f"{canonical}/.orchd/_master.json）：{'；'.join(residual)}。"
            "修复：cd <任务worktree> && git sparse-checkout init --no-cone && "
            "git sparse-checkout set '/*' '!/.orchd/' '/.orchd/*' '!/.orchd/_master.json'；"
            "或在主工作树手工清理残留副本后重跑 claim",
        )
    ]


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


def _check_in_review_worktree_integrity(
        project_root: Path) -> list[dict[str, str]]:
    """in_review 任务 worktree/分支完整性检查（2026-08-30 分支丢失复盘 §3）。

    对每个状态为 ``in_review`` 且已绑定 worktree 的任务校验：
    1. ``task/<id>`` 分支存在且可解析（核心失效模式：分支引用被删）；
    2. 绑定 worktree 目录存在且含有效 ``.git`` 元数据；
    3. 任务 worktree 的 ``.layout.json`` main_worktree 与主工作树一致
       （检出容器根执行导致的布局标记污染）。

    任一缺失 → fail 项（附重建命令模板）。只读、不自动修复。无账本 /
    无 in_review 任务 → ok 项（best-effort，不误报）；依赖导入失败 /
    账本重放、绑定读取异常 → 「检测不可用」fail（INV-4a：本检查守的是
    「分支被误删」这一核心失效模式，检测器自身故障时恰恰不能表现为"无问题"）。
    """
    try:
        from orchd.ledger import Store, resolve_store_dir
        from orchd.worktree import detect_layout, load_bindings, read_layout
    except Exception as exc:  # INV-4a：检测不可用（此前 return [] 静默消失）
        return [_make_check_unavailable("in_review_integrity", exc, "依赖导入失败")]
    try:
        layout = detect_layout(project_root)
        orchd_dir = Path(project_root) / ".orchd"
        store_root = resolve_store_dir(orchd_dir)
        state = Store(orchd_dir, readonly=True).replay()
        bindings = load_bindings(store_root)
    except Exception as exc:  # INV-4a：账本/绑定不可读 → 不得当作「无 in_review 问题」
        return [
            _make_check_unavailable("in_review_integrity", exc,
                                    "账本重放/绑定读取异常")
        ]
    main_wt = Path(layout["main_worktree"]).resolve()
    # 仅巡检**引擎登记任务**：账本中残留的非登记 id（测试夹具 task-1/task-2 等
    # 写入共享账本的噪音、或已从 master 移除的任务）本就没有分支与 worktree，
    # 纳入会把健康仓库误判为 fail。master 不存在时不做过滤（保持 best-effort，
    # 兼容无 _master.json 的最小场景）；master 存在但不可解析时检测不可用
    # （INV-4a：不得静默不过滤）。
    try:
        master_ids = _load_master_task_ids(orchd_dir)
    except Exception as exc:  # INV-4a：master 存在但不可解析 → 检测不可用
        return [
            _make_check_unavailable("in_review_integrity", exc,
                                    "master 存在但不可解析")
        ]
    reviewed = 0
    problems_by_task: dict[str, list[str]] = {}
    for tid, ts in state.items():
        if master_ids is not None and tid not in master_ids:
            continue
        # AC5（task-review-baseline-and-worktree-recycle-fix）：巡检面由 in_review
        # 扩展到 claimed。claimed 是「实现中」长时窗（可跨数小时 / 数天），与
        # in_review 同为分支误删高危期；2026-09-10 事故即发生在 claimed 期
        # （refs/heads/task/ 目录被删、三支分支丢失）。
        if ts.status not in ("in_review", "claimed"):
            continue
        reviewed += 1
        entry = bindings.get(tid)
        wt = Path(entry["worktree"]).resolve() if (
            entry and entry.get("worktree")) else None
        # task-line-diag-wiring：分支名按当前线命名空间
        from orchd.line_ctx import resolve_task_branch_for

        branch = resolve_task_branch_for(Path(project_root), tid)
        problems: list[str] = []
        # 1) 分支存在且可解析（branch/task-<id> 引用丢失 = 核心失效模式）
        rev = _run_git(
            project_root,
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
        if rev.returncode != 0 or not rev.stdout.strip():
            problems.append(f"分支 {branch} 缺失/不可解析")
        # 4) 任务 worktree HEAD 指向的 ref 存在（AC5）
        if wt is not None and (wt / ".git").exists():
            head = _run_git(wt, ["symbolic-ref", "-q", "HEAD"])
            head_ref = head.stdout.strip() if head.returncode == 0 else ""
            if not head_ref:
                problems.append(f"worktree HEAD 不可解析（{wt}）")
            else:
                ref_rev = _run_git(
                    project_root,
                    ["rev-parse", "--verify", "--quiet", head_ref])
                if ref_rev.returncode != 0 or not ref_rev.stdout.strip():
                    problems.append(f"worktree HEAD 指向的 ref 不存在（{head_ref}）")
        # 2) 绑定 worktree 目录有效
        if wt is None:
            problems.append("账本绑定缺失 worktree 路径")
        elif not (wt / ".git").exists():
            problems.append(f"worktree 目录 {wt} 无效（缺 .git 元数据）")
        # 3) 布局标记 main_worktree 与主工作树一致（容器根污染检出）
        if wt is not None:
            marker = read_layout(wt / ".orchd")
            if marker is not None:
                mm = Path(marker["main_worktree"]).resolve()
                if mm != main_wt:
                    problems.append(
                        f"布局标记 main_worktree（{mm}）与主工作树（{main_wt}）不一致")
        if problems:
            problems_by_task[tid] = problems
    if not problems_by_task:
        if reviewed == 0:
            return [
                _make_check("in_review_integrity", "ok",
                            "无 in_review/claimed 任务（或无账本，跳过）")
            ]
        return [
            _make_check("in_review_integrity", "ok",
                        f"{reviewed} 个 in_review/claimed 任务 worktree/分支完整")
        ]
    # 惰性导入：doctor 为叶子模块（零顶层 orchd 依赖），依赖一律函数内导入。
    try:
        from orchd.worktree import branch_reflog_tip
    except Exception:

        def branch_reflog_tip(_root, _branch):  # type: ignore[misc]
            return None

    hints = []
    for tid, problems in sorted(problems_by_task.items()):
        entry = bindings.get(tid)
        try:
            from orchd.worktree import _task_wt_name

            default_wt = Path(project_root).parent / _task_wt_name(tid)
        except Exception:
            default_wt = Path(project_root).parent / (
                f"task-{tid[5:] if tid.startswith('task-') else tid}")
        wt = Path(entry["worktree"]) if (
            entry and entry.get("worktree")) else default_wt
        # AC5：引用自愈模板——refs 被删但 reflog 存活时（2026-08-08 / 2026-09-10
        # 两次同型事故），reflog 末行 tip 即分支最后落点，可直接用于重建。
        # task-line-diag-wiring：自愈提示分支名按当前线命名空间
        from orchd.line_ctx import resolve_task_branch_for

        _branch = resolve_task_branch_for(Path(project_root), tid)
        tip = branch_reflog_tip(Path(project_root), _branch)
        if tip:
            rebuild = (f"git branch {_branch} {tip}（reflog tip 自愈："
                       f"git reflog show --format=%H {_branch} 末行）")
        else:
            rebuild = (f"git branch {_branch} <悬空sha>"
                       "（git fsck --lost-found 找回；无 reflog 时用此兜底）")
        hints.append(
            f"{tid}：{'；'.join(problems)}。重建：{rebuild}; "
            f"git worktree add {wt} {_branch}; "
            "并补回任务 worktree 的 .orchd/.layout.json（_propagate_container_marker 语义）"
        )
    return [_make_check("in_review_integrity", "fail", "；".join(hints))]


# ---------------------------------------------------------------------------
# 残留检测（task-audit-doctor-fix）
# ---------------------------------------------------------------------------


def _resolve_runtime_dir(orchd_dir: Path) -> Path:
    """解析运行时残留根目录（与 Store 同根）。

    container 布局 → ``resolve_store_dir`` 返回 ``<容器>/.orchd-runtime/``
    （锁 / session / intake 标记均落于此）；flat 或解析失败 →
    回退 ``orchd_dir``（``<project_root>/.orchd``），零回归。

    task-runtime-hygiene 路径盲区修复：此前 detect_residues 一律用
    ``<project_root>/.orchd``，container 布局下扫不到 .orchd-runtime 内的
    运行时残留（doctor --fix 恒报 0 项、僵尸 session 持续堆积）。
    """
    try:
        from orchd.ledger import resolve_store_dir

        return Path(resolve_store_dir(orchd_dir))
    except Exception:
        return orchd_dir


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


def _classify_residual_dir(path: Path) -> str:
    """残留目录内容分级：``empty`` / ``junk_only`` / ``has_content``（DR-3）。

    只有 ``empty`` / ``junk_only`` 允许被清理通道移除；``has_content``（含非杂项
    文件/链接的真实工作痕迹，典型：丢了 ``.git`` 指针但工作文件仍在的 worktree）
    一律不删，单列人工处置档——原实现直接 ``_rmtree_force`` 递归永久抹除。
    """
    if not path.is_dir() or path.is_symlink():
        return "has_content"  # 非普通目录（异常形态）→ 保守按含内容处理
    try:
        if not any(path.iterdir()):
            return "empty"
    except OSError:
        return "has_content"
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
        _layout_marker = ".layout.json"
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
            if line.startswith("ORCHD_BAKED_PY="):
                baked["py"] = _unquote(line.split("=", 1)[1])
            elif line.startswith("ORCHD_BAKED_SRC="):
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
        if exp_line and "ORCHD_LINE_TASK_PREFIX" not in "\n".join(head):
            causes.append("项目已启用多线但 hook 未烘焙线命名空间前缀（回退单线口径）")
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

    检测十二类残留：
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

    运行时残留（1/2/3/8）以**共享账本根**为扫描根（与 Store 同根，见
    :func:`_resolve_runtime_dir`）：container 布局扫 ``<容器>/.orchd-runtime/``，
    flat 扫 ``<project_root>/.orchd``。

    所有检测均为只读，不执行任何写操作。返回的每项包含：
    - path: 残留文件绝对路径
    - type: 残留类型（orphan_session_lock / zombie_session / intake_lock /
      git_tracked_lock / ghost_task / stale_worktree_registry / residual_dir /
      legacy_flat_residue / stale_proposal / stray_draft_file / proposals_dir_entry /
      hook_not_installed / hook_baked_stale / unreadable）
    - reason: 判定依据
    - action: 建议动作（delete / delete_dir / move_to_backup / residual_dir_manual /
      git_rm_cached_then_delete / git_worktree_prune / retract_ghost /
      ghost_task_manual / hook_manual / unreadable_manual）

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


def _detect_orphan_session_locks(project_root: Path,
                                 orchd_dir: Path) -> list[dict[str, Any]]:
    """检测孤儿 session 锁文件（无对应活跃 worktree 的 .session-gate-*.lock）。

    ``.session-gate-<task-id>.lock`` 在 claim 时创建（见 session_lock 的
    worktree 维度锁），任务完成后应被清理。若锁文件存在但对应任务 worktree
    不存在，则属于孤儿锁，可安全删除。

    判据修正（DR-2，2026-09-15）：旧实现 glob ``session-gate-*.lock`` **缺前导点**，
    与真实文件名 ``.session-gate-*.lock`` 永不匹配 → 检测恒空属死代码；本函数改为
    按 ``_LOCK_FILE_PATTERNS`` 中的真实模式（含前导点）glob，并兼容历史无点名称。

    双层活性判据（DR-7 配套）：
    ① worktree 维度——目录存在且有 ``.git`` → 视为活跃；
    ② flock 维度——对候选做 flock 探活，仍被持有（活跃会话）→ 不报残留。
    删除前的复检见 :func:`doctor_fix` / :func:`auto_clean`（账本锁内二次探活）。
    """
    residues: list[dict[str, Any]] = []
    if not orchd_dir.is_dir():
        return residues

    candidates: list[Path] = []
    for pattern in (".session-gate-*.lock", "session-gate-*.lock"):
        candidates.extend(p for p in sorted(orchd_dir.glob(pattern))
                          if p.is_file())
    if not candidates:
        return residues

    from orchd.lockfile import ExclusiveFileLock

    for lock_file in sorted(set(candidates)):
        # 从文件名提取任务 ID: .session-gate-task-xxx.lock → task-xxx
        name = lock_file.name.lstrip(".")
        if not (name.startswith("session-gate-") and name.endswith(".lock")):
            continue
        task_id = name[len("session-gate-"):-len(".lock")]
        # ① worktree 维度：对应任务 worktree 存在且有效 → 锁可能是活跃的
        worktree_path = (project_root.parent /
                         task_id if task_id.startswith("task-") else None)
        if (worktree_path is not None and worktree_path.is_dir()
                and (worktree_path / ".git").exists()):
            continue
        # ② flock 维度：仍被持有 = 有活跃持有者（会话/进程未退出）→ 不是孤儿
        try:
            if ExclusiveFileLock(lock_file).check().get("held"):
                continue
        except Exception:
            continue  # 无法判定持有态时保守跳过（不误报、不误删）
        residues.append({
            "path":
            str(lock_file),
            "type":
            "orphan_session_lock",
            "reason":
            (f"session 锁 {lock_file.name} 无对应活跃 worktree 且无 live flock"),
            "action":
            "delete",
        })

    return residues


def _live_session_lock_holders(runtime_dir: Path) -> tuple[set[str], bool]:
    """返回 ``(被 live flock 持有的 session_id 集合, 是否存在身份读不出的活锁)``。

    口径同 :func:`_detect_orphan_session_locks` 的 ②（``ExclusiveFileLock.check()``）
    与 :func:`_detect_residual_intake_locks`（「被 live flock 持有 = 活跃，非残留」）：
    flock 由内核托管，进程退出/崩溃即释放，是强于任何时间戳的活性信号——持锁的会话
    必然活着，其身份文件不得删。

    持有者身份取自锁文件 JSON 的 ``session_id``（写入方
    ``session_lock._prepare_session_lock_payload``）。不能改用
    ``session_lock_check(orchd_dir)``：它按调用方 worktree 解析出**单一**锁名
    （``_get_session_lock_path`` 会给 linked worktree 加 ``.session-<wt>.lock`` 后缀），
    而本函数是共享账本根上的全局巡检，须覆盖所有 worktree 的会话锁。
    ``ExclusiveFileLock.check()`` 自身只返回 ``{held, by_current_process, depth}``，
    不含身份，故持锁与读身份两步分开做。

    读身份必须先走 ``read_locked_text``（复用本进程持锁 fd）：Windows 下 msvcrt 字节锁
    会阻止新句柄读取，直读恒失败（同 ``session_lock.session_lock_check`` 的处置）。
    仍读不出（= **他进程**持活锁，身份不可知）时返回 ``unknown=True``，调用方据此
    整体放弃本轮僵尸判定：无法区分「死会话的文件」与「那个活会话的文件」，而
    ``zombie_session`` 属真删档，宁可漏清不可误删。

    锁名取自 doctor 自有的 ``_LOCK_FILE_PATTERNS``（本模块「orchd 自有运行时锁」命名的
    单一事实源，:func:`_is_orchd_lock_path` 同源消费），过滤出会话锁并排除 gate 锁
    （``.session-gate-*.lock`` / ``.session.gate.lock`` 属并发闸门、不登记 session_id）。
    不从 ``gitops.session_lock`` 取常量：那里只有基础名 ``.session.lock``，覆盖不到
    linked worktree 的 ``.session-<wt>.lock``，且会给依赖图新增一条
    doctor → gitops.session_lock 的跨层边（tests/contract/test_dependency_direction.py）。
    含前导点的真实命名与历史无点名称都要扫（同 :func:`_detect_orphan_session_locks`
    的兼容约定）：漏扫一把活锁的代价是误删一个活会话的身份文件，正是本函数要防的事。
    """
    holders: set[str] = set()
    unknown = False
    try:
        from orchd.lockfile import ExclusiveFileLock, read_locked_text

        patterns = tuple(
            pat for pat in _LOCK_FILE_PATTERNS
            if pat.lstrip(".").startswith("session")
            and not pat.lstrip(".").startswith(("session-gate", "session.gate"))
        )
        # 含前导点的真实命名 + 历史无点名称，两种都扫（同
        # _detect_orphan_session_locks 的约定）：漏扫一把活锁 = 误删一个活会话的身份文件。
        globs = {g for pat in patterns for g in (pat, pat.lstrip("."))}
        candidates = sorted({p for pat in globs for p in runtime_dir.glob(pat)})
        for lock_file in candidates:
            if lock_file.name.lstrip(".").startswith(("session-gate", "session.gate")):
                continue
            if not lock_file.is_file():
                continue
            try:
                if not ExclusiveFileLock(lock_file).check().get("held"):
                    continue
                content = read_locked_text(lock_file)
                if content is None:
                    content = lock_file.read_text(encoding="utf-8")
                data = json.loads(content)
            except Exception:
                unknown = True
                continue
            session_id = data.get("session_id") if isinstance(data, dict) else ""
            if session_id:
                holders.add(str(session_id))
            else:
                unknown = True
    except Exception:
        return set(), True
    return holders, unknown


def _detect_zombie_sessions(runtime_dir: Path) -> list[dict[str, Any]]:
    """检测僵尸 session runtime 文件（``sessions/*.json``）。

    判死口径**单一真源在 ledger**：:func:`orchd.ledger.is_session_expired`
    （``last_seen + session_ttl_minutes()``，默认 1440 分钟、``ORCHD_SESSION_TTL_MIN``
    可覆盖），与 ``session current``（ledger.py:867/:917）和 watchdog 的 stale 判定
    （report.py:986）同源。

    此前本函数自带 1800 秒（30 分钟）死 TTL 常量且只看文件 **mtime**，与 ledger 的
    1440 分钟相差 48 倍，实测后果：一个空闲超 30 分钟、账本仍判活的会话，其身份文件被
    自动清洁删除（``zombie_session`` ∈ ``_AUTO_CLEAN_TYPES``），随后引擎命令返回
    E033 ``session_identity_missing``，而账本里该 session 的 CLAIMED 事件仍在。
    mtime **不再参与判死**：它只被写命令刷新（``_touch_session_last_seen``），长只读
    阶段（跑全量测试、复核取证）会让活会话的 mtime 老化，是假信号。

    活性判据（任一成立即非残留，顺序 = 信号强度）：

    1. 该 session 正持有任一 ``.session*.lock`` 的 live flock
       （:func:`_live_session_lock_holders`）；
    2. ``active`` 为真且 :func:`is_session_expired` 判未过期——``last_seen`` 缺失/非法时
       ledger 保守判未过期，此处沿用（缺字段不构成删除依据）。

    存在身份读不出的活锁（他进程持锁，Windows 字节锁阻止读 JSON）时**整轮放弃判定**：
    无法区分死会话文件与该活会话的文件，而本类型属真删档（宁漏清不误删）。

    ``active`` 显式为假 = 会话已正常 ``end``，runtime 无后续用途（账本留有审计轨迹），
    判为可回收。读不出/解析不了的文件一律跳过：无法判定之物不删。
    """
    residues: list[dict[str, Any]] = []
    # 目录名与后缀取自 _SESSION_FILE_PREFIX / _SESSION_FILE_SUFFIX（单一事实源，
    # 消除散落字面量——两常量此前为未引用死常量，DR-12）。
    sessions_dir = runtime_dir / _SESSION_FILE_PREFIX
    if not sessions_dir.is_dir():
        return residues

    from orchd.ledger import is_session_expired, session_ttl_minutes

    holders, holder_unknown = _live_session_lock_holders(runtime_dir)
    if holder_unknown:
        return residues
    ttl_minutes = session_ttl_minutes()
    for session_file in sorted(sessions_dir.glob(f"*{_SESSION_FILE_SUFFIX}")):
        if not session_file.is_file():
            continue
        try:
            data = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("session_id") or session_file.stem)
        if session_id in holders:
            continue
        active = bool(data.get("active", True))
        if active and not is_session_expired(data):
            continue
        why = (
            "active=false（会话已 end，runtime 可回收）"
            if not active
            else f"last_seen 超 TTL（{ttl_minutes} 分钟，口径 = ledger.is_session_expired）"
        )
        residues.append({
            "path": str(session_file),
            "type": "zombie_session",
            "reason": f"session 文件 {session_file.name} 已判死：{why}",
            "action": "delete",
        })

    return residues


def _detect_residual_intake_locks(orchd_dir: Path) -> list[dict[str, Any]]:
    """检测残留 intake 标记（.intake.lock 无 live flock 且已超时）。

    .intake.lock 是准入锁文件，正常 acquire/release 不删除文件（task-intake-lock-path-fix
    AC5：准入写释放后留下的新鲜标记不得立即判残留，否则卫生门禁每次准入写后闪红约 120s）。
    判据与 ``ledger.intake_lock_check`` 的 timeout 语义对齐：仅当无 live flock 持有
    **且**标记年龄 >= ``_INTAKE_LOCK_TIMEOUT``（默认 120s）时才算残留；新鲜标记
    （刚释放，flock 已放但文件保留）返回空。超时残留仍可被 ``intake_lock_check``
    自动清除（AC3 timeout_cleaned 语义不回归）。

    时间源优先用标记内 JSON ``timestamp``（acquire 写入的诊断标记），缺失/非法时
    回退文件 mtime；均不可得时保守跳过（不误报）。

    已释放标记（task-intake-lock-released-marker）：``released: true`` 的标记是
    ``intake_lock_release`` 显式改写的留档（无 live flock 属正常态）→ 直接判非残留，
    不产生 residue 项；无该字段的旧格式标记保持原判据（向后兼容）。
    """
    residues: list[dict[str, Any]] = []
    if not orchd_dir.is_dir():
        return residues

    # 检查运行时根的 .intake.lock（调用方已解析到共享账本根，container/flat 兼容）
    intake_lock = orchd_dir / ".intake.lock"
    if not intake_lock.is_file():
        return residues
    try:
        from orchd.lockfile import ExclusiveFileLock
        held = ExclusiveFileLock(intake_lock).check().get("held", False)
    except Exception:
        return residues  # 无法判定持有态时保守跳过，避免误报
    if held:
        return residues  # 被 live flock 持有 = 活跃锁，非残留

    try:
        from orchd.ledger import _INTAKE_LOCK_TIMEOUT

        timeout_s = float(_INTAKE_LOCK_TIMEOUT)
    except Exception:
        timeout_s = 120.0
    age_s: float | None = None
    try:
        content = intake_lock.read_text(encoding="utf-8")
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("released") is True:
                # 已释放标记（task-intake-lock-released-marker）：intake_lock_release
                # 在 flock 释放后显式改写为 released=true 的**留档**标记，「无 live
                # flock」正是其正常态 → 不是残留（不进 auto_clean.manual_notice、
                # 不被卫生门禁判 FAIL）。无 released 字段的旧格式标记保持现判据。
                return residues
            ts = float(data.get("timestamp", 0))
            if ts > 0:
                age_s = time.time() - ts
        except (ValueError, TypeError, AttributeError):
            pass
    except (OSError, IOError):
        return residues
    if age_s is None:
        try:
            age_s = time.time() - intake_lock.stat().st_mtime
        except OSError:
            return residues
    if age_s < timeout_s:
        return residues  # 新鲜标记：准入写刚释放，门禁不闪红
    residues.append({
        "path":
        str(intake_lock),
        "type":
        "intake_lock",
        "reason": (".intake.lock 无 live flock 且已超时 "
                   f"（age {age_s:.1f}s >= {timeout_s:.0f}s，进程已退出）"),
        "action":
        "delete",
    })

    return residues


def _is_third_party_lock_name(name: str) -> bool:
    """是否为第三方生态锁文件名（项目源码资产，doctor 绝不 untrack / 删除）。"""
    return name in _THIRD_PARTY_LOCK_NAMES


def _is_orchd_lock_path(file_path: Path, project_root: Path) -> bool:
    """是否为 orchd **自有**运行时锁（DR-1 判据收敛）。

    两条判据任一成立即认定：
    ① 文件名命中 ``_LOCK_FILE_PATTERNS``（orchd 锁命名白名单）；
    ② 相对路径中任一级目录以 ``.orchd`` 开头（``.orchd/`` / ``.orchd-runtime/``
       等 orchd 自有目录下的锁一律视为运行时残留）。

    第三方生态锁（``_THIRD_PARTY_LOCK_NAMES``）恒排除：``poetry.lock`` /
    ``Cargo.lock`` 等是**项目源码资产**，原实现按 ``*.lock`` 全量判定会把它们
    ``git rm --cached`` + 删文件（越界破坏，2026-09-14 审核 DR-1）。
    """
    if _is_third_party_lock_name(file_path.name):
        return False
    if any(
            fnmatch.fnmatchcase(file_path.name, pat)
            for pat in _LOCK_FILE_PATTERNS):
        return True
    try:
        rel_parts = file_path.relative_to(project_root).parts
    except ValueError:
        rel_parts = file_path.parts
    return any(part.startswith(".orchd") for part in rel_parts[:-1])


def _detect_git_tracked_locks(project_root: Path) -> list[dict[str, Any]]:
    """检测已误提交入 git 的 **orchd 自有**锁文件（DR-1 判据收敛）。

    判定：``git ls-files --cached`` 中已跟踪、且路径命中 :func:`_is_orchd_lock_path`
    （orchd 锁命名白名单或 ``.orchd*`` 自有目录）的 ``*.lock`` 文件。仓库根第三方
    生态锁与普通 ``*.lock`` 源码文件**不在此列**（原实现按 ``*.lock`` 全量判定，
    会把 ``poetry.lock`` 等源码资产 untrack + 删文件，属越界破坏）。

    命中的文件应先从 git 索引移除（``git rm --cached``）再删除；untrack 与删除之间
    是**未提交的半修状态**，须后续 commit 收口——故每项附
    ``follow_up="need_commit_after_untrack"``，由 :func:`doctor_fix` 在清理结果与
    摘要中显式提示（DR-11）。
    """
    residues: list[dict[str, Any]] = []
    # git ls-files 列出追踪的 .lock 文件（判据在白名单侧收敛，不靠 pathspec）
    result = _run_git(project_root, ["ls-files", "--cached", "--", "*.lock"])
    if result.returncode != 0:
        return residues

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        # 只处理项目根目录下的 .lock 文件（不在 .git/ 内）
        file_path = Path(project_root) / line
        if ".git" in file_path.parts:
            continue
        if not _is_orchd_lock_path(file_path, Path(project_root)):
            continue  # 非 orchd 自有锁（第三方生态锁 / 普通 *.lock）：绝不触碰
        if file_path.is_file():
            residues.append({
                "path":
                str(file_path),
                "type":
                "git_tracked_lock",
                "reason": (f"orchd 锁文件 {line} 已被误提交入 git（运行时临时文件不应追踪）"),
                "action":
                "git_rm_cached_then_delete",
                "follow_up":
                "need_commit_after_untrack",
            })

    return residues


# DR-5：终态任务（completed / cancelled）不是幽灵——master 归档只影响清单可见性，
# 账本里的终态是该任务的最终事实，不得被 --fix 级联撤回。
# （task-terminal-single-p2：终态词表收敛 contracts 单一真源，原为硬编码元组）
from orchd.contracts.events import TERMINAL_STATUSES as _GHOST_TERMINAL_STATUSES


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
        for ev in events if ev.get("type") == "RETRACT"
    }
    for ev in events:
        if ev.get("type") in ("RETRACT", "FORCE_STATUS"):
            continue
        if ev.get("event_id") in already_retracted:
            continue
        return ev.get("event_id")
    return None


def _is_protected_path(path: Path, project_root: Path) -> bool:
    """检查路径是否在保护白名单中（--fix 绝不触碰）。

    源码资产（_master.json / IDEAS.md 等）与位置无关恒保护；运行时状态文件
    （_ledger.jsonl / _checkpoint.json 等）仅当位于 canonical 账本根时保护——
    container 布局下 main/.orchd 的 flat 遗留属于可清残留（不保护，由
    _detect_legacy_flat_residues 检出后备份清理）。
    """
    name = path.name
    if name in _SOURCE_ASSETS:
        return True
    if name in _RUNTIME_STATE_FILES:
        orchd_dir = Path(project_root) / ".orchd"
        canonical = _resolve_runtime_dir(orchd_dir)
        try:
            path.resolve().relative_to(canonical.resolve())
            return True
        except ValueError:
            return False
    # 完整相对路径保护（保持向后兼容：白名单内完整路径也保护）
    try:
        rel = path.relative_to(project_root)
        return str(rel) in _PROTECTED_PATHS
    except ValueError:
        return False


def doctor(project_root: Path) -> dict[str, Any]:
    """执行完整仓库健康检查（结构检查 + 残留/幽灵摘要）。

    DR-8（2026-09-15）：旧实现只调 :func:`check_repo`，不含 residue / ghost，于是
    同一异常会出现「doctor 报健康、watchdog 报 stuck」的判定不一致。现并入
    :func:`detect_residues` 摘要，并按 disposition 归入 ``issues``——判据与卫生
    门禁同源（``AUTO_CLEAN_DISPOSITIONS``）：

    - ``auto_clean`` / ``legacy_move``：交给自动清理通道处置 → 不计入失败
      （避免「检测器报清理器即将删除之物」的抢跑抖动）；
    - ``manual``：无自动通道、需人工处置 → 计入 ``issues`` 且 ``repo_ok=False``。

    INV-4a（task-detection-fail-closed-doctor-ledger）：「检测不可用」不再静默——
    残留扫描整体异常时产出 ``residue_scan`` 不可用检查项（而非空残留=健康）；各检测
    器内部的异常由其自身转成 ``detection_unavailable`` 残留项或不可用检查项（见模块内
    「检测不可用」语义块）。

    Returns:
        ``{repo_ok, degraded, detection_unavailable, checks, issues, warnings, repo,
        residues, residue_summary}``；
        ``repo_ok`` 为 False 表示存在任一 fail 检查项（含「检测不可用」项）或 manual
        档残留（调用方可据此设非零退出码）；
        ``warnings`` 为 ``status == "warn"`` 检查项的 hint 清单（**非阻断**档：数据
        停滞一类，不进 ``issues``、不翻 ``repo_ok``、不改退出码，见模块头 INV-4a
        词汇表节第 5 条）；
        ``degraded`` 为 True 表示本轮**至少一项检测未能判定**；
        ``detection_unavailable`` 为这些检测的名称列表（不可用检查项名 + 不可用残留项
        的 ``detector``）——agent 据此区分「查出问题该修」与「没查成该人工介入」。
    """
    checks = check_repo(project_root)
    try:
        residues = detect_residues(project_root)
    except Exception as exc:  # INV-4a：残留扫描整体失败 → 不可用检查项（非"无残留"）
        residues = []
        checks = [
            *checks,
            _make_check_unavailable("residue_scan", exc, "残留扫描整体不可用"),
        ]
    issues = [c["hint"] for c in checks if c["status"] == "fail"]
    # warn = 非阻断档（文件头 INV-4a 第 5 条）：不进 issues、不翻 repo_ok、不改
    # 退出码，只经顶层 warnings 透出——「数据停滞」与「仓库完整性故障」分档。
    warnings = [c["hint"] for c in checks if c["status"] == "warn"]
    by_disposition: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in residues:
        disp = str(item.get("disposition", "unknown"))
        by_disposition[disp] = by_disposition.get(disp, 0) + 1
        rtype = str(item.get("type", "unknown"))
        by_type[rtype] = by_type.get(rtype, 0) + 1
    manual = [
        r for r in residues
        if r.get("disposition") not in AUTO_CLEAN_DISPOSITIONS
    ]
    for item in manual:
        issues.append(f"[residue/{item.get('type')}] {item.get('reason', '')}"
                      f"（path={item.get('path')}）")
    # INV-4a：本轮「没能判定」的检测名（检查项 unavailable 键 + 残留项 detector）
    unavailable = sorted({
        str(c["name"]) for c in checks if c.get("unavailable")
    } | {
        str(r.get("detector"))
        for r in residues
        if r.get("type") == _DETECTION_UNAVAILABLE_RESIDUE and r.get("detector")
    })
    return {
        "repo_ok": len(issues) == 0,
        "degraded": bool(unavailable),
        "detection_unavailable": unavailable,
        "checks": checks,
        "issues": issues,
        "warnings": warnings,
        "repo": str(Path(project_root).resolve()),
        "residues": residues,
        "residue_summary": {
            "total": len(residues),
            "by_disposition": by_disposition,
            "by_type": by_type,
            "manual_failures": len(manual),
        },
    }


def _delete_file(path: Path, project_root: Path) -> None:
    """沙箱安全删除单个文件（DR-10：doctor 文件删除的唯一入口）。

    裸 ``Path.unlink`` 在沙箱下被劫持为「移入回收站」，回收站不可用时 FAIL_CLOSED
    抛 OSError → 清理静默失败（残留永远清不掉）。统一走
    ``gitops.cleanup._safe_delete``：底层 OS 直接删除优先 → ``unlink`` → 降级重命名
    到系统 temp（语义等效删除）。路径不存在时直接返回（幂等）。
    """
    from orchd.gitops.cleanup import _safe_delete

    if not path.exists():
        return
    _safe_delete(path, Path(project_root))


def _dispose_residual_dir(path: Path,
                          backup_root: Path) -> tuple[bool, dict[str, Any]]:
    """处置残留任务目录：先移入备份区（可回滚），含真实内容则拒绝处置（DR-3）。

    顺序：① :func:`_classify_residual_dir` 分级——``has_content`` 直接拒绝（不删也
    不移），由调用方归入人工处置档；② ``empty`` / ``junk_only`` 先 ``shutil.move``
    到 ``backup_root/<目录名>``（可回滚，且完全避开递归删除）。

    NEW-DR3（task-doctor-fail-open-blindspots）：搬移失败（跨设备 copy+rm 中途
    失败等）→ 显式标记失败并保留源，**绝不**降级为 ``_rmtree_force`` 强删——
    否则源已删、备份残缺，宣称的「可回滚」失真。Windows 句柄占用等搬移失败
    同样走失败标记（留待下轮/人工），不静默强删。

    Args:
        path: 待处置的残留目录。
        backup_root: 备份落点目录（调用方给出，按时间戳分桶）。

    Returns:
        ``(ok, record)``：``ok=True`` 时 record 含 ``path`` / ``disposition``
        （``moved_to_backup``）与 ``backup``；``ok=False`` 时 record 含
        ``path`` / ``error`` 与原因（``move_failed=True`` + ``source_preserved``
        标记源是否仍存在）。不抛异常（调用方按处置档归类）。
    """
    content = _classify_residual_dir(path)
    if content == "has_content":
        return False, {
            "path":
            str(path),
            "content":
            content,
            "error": ("含非杂项内容（疑似真实工作残留）——拒绝自动删除；"
                      "请人工确认后处置（确认无用可手工移入 .doctor-backup 或删除）"),
        }
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / path.name
        if dest.exists():
            dest = backup_root / f"{path.name}-{int(time.time())}"
        shutil.move(str(path), str(dest))
        return True, {
            "path": str(path),
            "content": content,
            "backup": str(dest),
            "disposition": "moved_to_backup",
        }
    except OSError as exc:
        return False, {
            "path": str(path),
            "content": content,
            "error": (f"备份迁移失败（{type(exc).__name__}: {exc}）——源已保留，"
                      "未执行强删；请人工确认后处置"),
            "move_failed": True,
            "source_preserved": path.exists(),
        }


def _dispose_file_to_backup(path: Path, backup_root: Path,
                            project_root: Path) -> tuple[bool, dict[str, Any]]:
    """单文件整份搬入备份桶（``action=move_to_backup`` 的执行体，可回滚）。

    与通用 ``delete`` 档（:func:`_doctor_fix_impl` 内 copy2 后删除）的差别是**副本
    份数**：move 全程只有一份内容存在（先在桶内、后源消失），copy2 则先落两份。
    陈旧提案草稿属准入产物、注册前含独立信息量（草稿正文尚未入库，``.orchd/*`` 被
    gitignore），不得出现「备份未写成而源已删」的窗口，也不得落到 :func:`_safe_delete`
    的降级重命名（那会把内容推进系统 temp 的 ``orchd-trash-*``，脱离 ``.doctor-backup``
    既有保留期——本桶由 :func:`_prune_stale_backups` 收敛，不新造无界目录）。

    落点保持相对 ``project_root`` 的路径结构（同名冲突追加时间戳）。搬移失败
    （OSError，含跨设备 copy+rm 中途失败）→ 显式失败 + ``source_preserved``，
    绝不降级强删（NEW-DR3 同语义）。

    Args:
        path: 待搬移文件。
        backup_root: 桶内基准目录（调用方按时间戳分桶给出）。
        project_root: 相对路径基准。

    Returns:
        ``(ok, record)``：``ok=True`` 时 record 含 ``path`` / ``backup`` /
        ``disposition=moved_to_backup``；``ok=False`` 时含 ``error`` +
        ``move_failed`` + ``source_preserved``。不抛异常。
    """
    try:
        try:
            rel = path.relative_to(project_root)
        except ValueError:
            rel = Path(path.name)
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / rel
        if dest.exists():
            dest = dest.with_name(f"{dest.name}-{int(time.time())}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return True, {
            "path": str(path),
            "backup": str(dest),
            "disposition": "moved_to_backup",
        }
    except OSError as exc:
        return False, {
            "path": str(path),
            "error": (f"备份迁移失败（{type(exc).__name__}: {exc}）——源已保留，"
                      "未执行强删；请人工确认后处置"),
            "move_failed": True,
            "source_preserved": path.exists(),
        }


def doctor_fix(
    project_root: Path,
    *,
    dry_run: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """执行残留清理（DR-7：破坏动作在**账本锁内**执行）。

    与并发 claim / done / amend 的账本写动作互斥，消除「按旧快照删掉刚建立的活动
    状态」的 TOCTOU（旧实现不持锁：活跃 session 心跳文件 / 刚被 claim 建立的锁可能
    在检测与删除之间变为活跃）。锁内执行对 retract_ghost 分支无害——底层
    ``ExclusiveFileLock`` 同进程可重入（共享 fd），``retract`` 内部再取锁不会自死锁。

    Args:
        project_root: 项目根目录。
        dry_run: True 时只输出待清理清单，不执行任何写操作。
        backup_dir: 删除前自动备份到此目录。None 时自动使用
            <project_root>/.orchd/.doctor-backup/<timestamp>/。

    Returns:
        {dry_run, backup_dir, detected, skipped_protected, skipped_manual,
         skipped_clean, cleaned, errors, summary}
    """
    from orchd.ledger import Store

    # task-readonly-no-migrate：此外层 Store 仅作锁柄（acquire/release），
    # 不经它读写账本——只读构造，不触发建库 / 迁移 / 改名。
    # 契约（task-doctor-master-dir-contract）：入参是 **master 目录**；账本根与
    # 锁路径由 Store 内部 resolve_store_dir 派生，与旧写法逐字一致（等价性由
    # tests/test_doctor.py::TestStoreContractNormalization 钉住）。
    store = Store(Path(project_root) / ".orchd", readonly=True)
    store.acquire_lock()
    try:
        return _doctor_fix_impl(project_root,
                                dry_run=dry_run,
                                backup_dir=backup_dir)
    finally:
        store.release_lock()


def _doctor_fix_impl(
    project_root: Path,
    *,
    dry_run: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """残留清理实现（调用方须已持账本锁，见 :func:`doctor_fix`）。

    Args:
        project_root: 项目根目录。
        dry_run: True 时只输出待清理清单，不执行任何写操作。
        backup_dir: 删除前自动备份到此目录。None 时自动使用
            <project_root>/.orchd/.doctor-backup/<timestamp>/。

    Returns:
        {
            "dry_run": bool,
            "backup_dir": str | None,
            "detected": [...],   # detect_residues 原始输出
            "skipped_protected": [...],  # 被白名单保护的路径
            "skipped_manual": [...],     # 无自动清理通道、需人工处置的项
            "skipped_clean": [...],  # 复检跳过未删的项（本轮未删，不计入 cleaned）
            "cleaned": [...],    # 已清理的项（dry_run=True 时为 []）
            "errors": [...],     # 清理失败的项
            "summary": str,
        }
    """
    detected = detect_residues(project_root)
    skipped_protected: list[dict[str, Any]] = []
    to_clean: list[dict[str, Any]] = []

    # 白名单过滤：绝不触碰核心状态文件
    for item in detected:
        path = Path(item["path"])
        if _is_protected_path(path, Path(project_root)):
            skipped_protected.append({
                **item,
                "reason":
                item.get("reason", "") + " [SKILLED: 保护白名单拦截]",
            })
        else:
            to_clean.append(item)

    # 不可自动处置项单列（事件全为 FORCE_STATUS 的幽灵任务 / 含真实内容的残留目录），
    # 避免 --fix 报伪失败；判据为恒人工处置动作集合（单一事实源 _MANUAL_ACTIONS）
    skipped_manual: list[dict[str, Any]] = [
        i for i in to_clean if i.get("action") in _MANUAL_ACTIONS
    ]
    if skipped_manual:
        to_clean = [
            i for i in to_clean if i.get("action") not in _MANUAL_ACTIONS
        ]

    # dry-run 模式：只报告不执行
    if dry_run:
        return {
            "dry_run":
            True,
            "backup_dir":
            None,
            "detected":
            detected,
            "skipped_protected":
            skipped_protected,
            "skipped_manual":
            skipped_manual,
            "skipped_clean": [],
            "cleaned": [],
            "errors": [],
            "summary": (f"[dry-run] 发现 {len(detected)} 项残留"
                        f"（{len(skipped_protected)} 项被白名单保护跳过，"
                        f"{len(skipped_manual)} 项需人工处置，"
                        f"{len(to_clean)} 项可清理）。"
                        f"使用 --fix 执行实际清理。"),
        }

    # 执行模式：先备份再删除（dry-run 已在上面提前返回 → 此处必为具体路径）
    if backup_dir is None:
        backup_path: Path = (Path(project_root) / ".orchd" / ".doctor-backup" /
                             f"{int(time.time())}")
    else:
        backup_path = Path(backup_dir)

    cleaned: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    # task-doctor-fix-cleaned-bucket：复检跳过未删单列——lock_held 与复检异常
    # 同走 _pre_delete_lock_recheck 文案，均入此桶不再进 cleaned（未删除不得计
    # 入已清理）。already_absent（幽灵零写幂等）保留在 cleaned：零操作成功态，
    # 非"未删"，不在本卡范围。
    skipped_clean: list[dict[str, Any]] = []
    untracked: list[str] = []  # 已 git rm --cached 但未提交（半修，需 commit 收口）

    # DR-13：全局 git worktree prune 每轮只执行**一次**（prune 本身是全局动作，
    # 旧实现 N 个 stale 注册项各跑一次：重复执行浪费 + cleaned 计数虚高）。
    # 本轮所有 git_worktree_prune 项共享此结果。
    prune_failed: str | None = None
    if any(i.get("action") == "git_worktree_prune" for i in to_clean):
        _prune = _run_git(project_root, ["worktree", "prune"])
        if _prune.returncode != 0:
            prune_failed = _prune.stderr.strip()

    for item in to_clean:
        path = Path(item["path"])
        action = item.get("action", "delete")

        # 幽灵任务清理（task-runtime-hygiene AC1/AC5）：不走文件删除路径，改用
        # 引擎保留的 admin 控制面撤回其在账本中的孤儿事件，使 replay 与 master
        # 一致（force-status / retract 常规通道对不在 _master.json 的 id 不可用，
        # 故此处是该类残留的专用处置通道）。
        if action == "retract_ghost":
            try:
                from orchd.ledger import Store
                from orchd.onboard import retract as _retract

                # 契约（task-doctor-master-dir-contract）：入参是 master 目录；
                # retract 内部对 store.orchd_dir 的消费一律按 master 目录语义
                # （load_bindings / 会话身份等再各自 resolve_store_dir）。
                ghost_store = Store(Path(project_root) / ".orchd")
                ghost_task_id = item.get("task_id")
                # DR-6 幂等守卫（前置）：任务已不在 replay 派生状态中（先前 --fix 已
                # 撤回 / 账本侧已无活跃事件）→ 零写操作，绝不重复追加 RETRACT。
                if ghost_task_id and ghost_task_id not in ghost_store.replay():
                    cleaned.append({
                        **item,
                        "retracted": False,
                        "skipped": "already_absent",
                        "backup": None,
                    })
                    continue
                ghost_res = _retract(
                    ghost_store,
                    "admin",
                    target_event_id=item.get("event_id"),
                    reason=(f"幽灵任务清理（doctor --fix）："
                            f"{item.get('task_id')} 不在 _master.json"),
                    project_root=Path(project_root),
                )
                # DR-6 幂等守卫（后验）：撤回后目标任务必须已从 replay 派生状态消失；
                # 若仍在（例如链上仍有不可撤事件），**不再重复追加** RETRACT，降级为
                # 需人工处置项（manual 档），避免每次 --fix 反复写账本。
                if ghost_task_id and ghost_task_id in ghost_store.replay():
                    errors.append({
                        **item,
                        "action":
                        "ghost_task_manual",
                        "error": ("retract_incomplete: 撤回后任务仍在 replay 派生状态中"
                                  "（疑似链上仍有不可撤事件）——已停止重复撤回，转人工处置"),
                    })
                    continue
                cleaned.append({
                    **item,
                    "retracted":
                    bool(ghost_res.get("retracted")),
                    "backup":
                    None,
                })
            except Exception as exc:  # 清理失败不中断其余项
                errors.append({
                    **item,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            continue

        if action == "git_worktree_prune":
            # DR-13：prune 已在循环外执行一次，此处只登记结果（不再逐项调用）
            if prune_failed is None:
                cleaned.append({**item, "backup": None, "pruned_once": True})
            else:
                errors.append({
                    **item,
                    "error":
                    f"git worktree prune 失败: {prune_failed}",
                })
            continue

        if action == "delete_dir":
            # 残留任务 worktree 目录：先移入 .doctor-backup/residual/<ts>/（可回滚）；
            # 搬移失败显式标记失败并保留源（NEW-DR3，不强删）；含真实内容一律不删（DR-3）。
            ok, record = _dispose_residual_dir(path, backup_path / "residual")
            if ok:
                cleaned.append({**item, **record})
            else:
                # 检出期为可清、执行期发现内容（竞态 / 判定漂移）→ 不删且失败可见
                errors.append({**item, **record})
            continue

        if action == "move_to_backup":
            # 陈旧提案草稿：整文件搬入既有备份桶（保持相对路径），源消失、内容
            # 逐字节可回滚。此处必须显式分支——未知 action 会落到下面的通用删除
            # 分支（copy2 + _safe_delete），草稿将失去「只有一份副本」的保证并可能
            # 降级进系统 temp（见 _dispose_file_to_backup docstring）。
            ok, record = _dispose_file_to_backup(path, backup_path,
                                                 Path(project_root))
            if ok:
                cleaned.append({**item, **record})
            else:
                errors.append({**item, **record})
            continue

        try:
            # DR-7 单一事实源（_pre_delete_lock_recheck）：--fix 与 auto_clean
            # 删除前共用同一 flock 复检，堵检测→执行并发窗口。
            skip_reason = _pre_delete_lock_recheck(path)
            if skip_reason is not None:
                skipped_clean.append({
                    **item,
                    "skipped": "lock_held",
                    "backup": None,
                    "reason": skip_reason,
                })
                continue
            needs_commit = action == "git_rm_cached_then_delete"
            if needs_commit:
                # 先从 git 移除追踪，再删除文件。untrack 与删除之间是**未提交的
                # 半修状态**，须后续 commit 收口 → 结果项与摘要显式提示（DR-11）。
                git_result = _run_git(
                    project_root,
                    ["rm", "--cached", str(path)])
                if git_result.returncode != 0:
                    errors.append({
                        **item,
                        "error":
                        f"git rm --cached 失败: {git_result.stderr.strip()}",
                    })
                    continue

            # 备份（仅当文件存在时）
            if path.exists() and backup_path is not None:
                backup_path.mkdir(parents=True, exist_ok=True)
                # 保持相对路径结构，避免同名文件冲突
                try:
                    rel = path.relative_to(Path(project_root))
                    dest = backup_path / rel
                except ValueError:
                    dest = backup_path / path.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(path), str(dest))

            # 执行删除（DR-10：统一走 _delete_file → _safe_delete；裸 unlink 在沙箱
            # 下被劫持为「移入回收站」，回收站不可用时 FAIL_CLOSED 致清理静默失败）
            _delete_file(path, Path(project_root))

            record = {
                **item,
                "backup": str(backup_path) if backup_path else None,
            }
            if needs_commit:
                record["follow_up"] = "need_commit_after_untrack"
                record["hint"] = ("已从 git 索引移除（git rm --cached）但未提交：属半修状态，"
                                  "须 git commit 收口——否则该路径持续呈现「已删除未提交」")
                untracked.append(str(path))
            cleaned.append(record)
        except OSError as exc:
            errors.append({
                **item,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # A1（task-hostfix-pack-b）：收尾顺带收敛备份保留期（best-effort）。
    _pruned = _prune_stale_backups(Path(project_root) / ".orchd" / ".doctor-backup")
    result: dict[str, Any] = {
        "dry_run":
        False,
        "backup_dir":
        str(backup_path) if backup_path else None,
        "detected":
        detected,
        "skipped_protected":
        skipped_protected,
        "skipped_manual":
        skipped_manual,
        "skipped_clean":
        skipped_clean,
        "cleaned":
        cleaned,
        "errors":
        errors,
        "summary":
        (f"清理完成：{len(cleaned)} 项已清理"
         f"（{len(skipped_protected)} 项被白名单保护跳过，"
         f"{len(skipped_manual)} 项需人工处置，"
         f"{len(skipped_clean)} 项复检跳过未删，"
         f"{len(errors)} 项失败）。"
         f"备份目录：{backup_path}" +
         (f"；{len(untracked)} 项已从 git 索引移除但未提交，"
          f"需 git commit 收口：{', '.join(untracked)}" if untracked else "") +
         (f"；过期备份已清理 {len(_pruned)} 项" if _pruned else "")),
    }
    if _pruned:
        result["backups_pruned"] = _pruned
    return result


# ---------------------------------------------------------------------------
# 自动清洁（task-doctor-auto-clean）
# ---------------------------------------------------------------------------
# 分级模型（与手动 doctor --fix 共享 detect_residues 单一事实源）：
#   Auto-Clean（低风险，直接处理）：
#     orphan_session_lock / zombie_session → 直接删文件（可再生，无备份）
#     residual_dir → 移入 .doctor-backup/residual/<ts>/（可回滚；搬移失败显式
#                   标记失败并保留源，不强删；含真实内容者不处置，转 manual 档）
#     stale_worktree_registry → git worktree prune
#   Legacy move（可回滚）：legacy_flat_residue → 移入 .doctor-backup/legacy/<ts>/ 滚动区
#   Manual notice（高风险 / 无自动通道，仅报告不执行）：
#     ghost_task / git_tracked_lock / intake_lock
# 环境变量 ORCHD_AUTO_CLEAN ∈ {off, report} → 全部降级为只报告（disabled=True）。
_AUTO_CLEAN_TYPES = frozenset({
    "orphan_session_lock",
    "zombie_session",
    "residual_dir",
    "stale_worktree_registry",
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
})

# 卫生门禁豁免档位（单一事实源，消费方 scripts/verify_project_hygiene.py）：
# 属这两档的残留由自动清理通道处置，不与清理器抢跑、不计入卫生失败判定。
AUTO_CLEAN_DISPOSITIONS: tuple[str, ...] = ("auto_clean", "legacy_move")


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


# A1（task-hostfix-pack-b）：备份保留期。`.doctor-backup/` 只增不减会长期积累
# （auto_clean 每次残留处置都新建时间戳桶）。策略：早于 30 天**且**不在各分类
# 最新 5 个之内的桶才删；非时间戳命名一律不动；best-effort（删失败静默跳过，
# 不阻断主流程）。只删自家备份目录，不碰其他内容。
_BACKUP_RETENTION_DAYS = 30
_BACKUP_RETENTION_KEEP_NEWEST = 5


def _prune_stale_backups(backup_base: Path) -> list[str]:
    """清理过期备份桶，返回已删路径（相对名，供留痕）。"""
    pruned: list[str] = []
    try:
        import time as _time

        base = Path(backup_base)
        if not base.is_dir():
            return pruned
        now = _time.time()
        # 顶层时间戳桶 + residual/legacy 分类桶逐个收敛。
        buckets: list[Path] = []
        for child in sorted(base.iterdir()):
            if child.is_dir() and child.name.isdigit():
                buckets.append(child)
            elif child.is_dir() and child.name in ("residual", "legacy"):
                buckets.extend(
                    sorted(p for p in child.iterdir()
                           if p.is_dir() and p.name.isdigit()))
        if not buckets:
            return pruned
        by_parent: dict[str, list[Path]] = {}
        for b in buckets:
            by_parent.setdefault(str(b.parent), []).append(b)
        for siblings in by_parent.values():
            ordered = sorted(siblings, key=lambda p: int(p.name), reverse=True)
            for b in ordered[_BACKUP_RETENTION_KEEP_NEWEST:]:
                try:
                    age_days = (now - int(b.name)) / 86400
                except ValueError:
                    continue
                if age_days < _BACKUP_RETENTION_DAYS:
                    continue
                try:
                    import shutil as _shutil

                    _shutil.rmtree(str(b), ignore_errors=False)
                    if not b.exists():
                        pruned.append(str(b))
                except OSError:
                    pass
    except Exception:
        pass
    return pruned


def _auto_clean_item(project_root: Path,
                     item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """执行单个 Auto-Clean 残留的清理（best-effort，失败返回 (False, 带 error)）。

    删除动作与 ``doctor --fix`` 同源：文件删除走 :func:`_delete_file`
    （``_safe_delete``，DR-10）；残留目录走 :func:`_dispose_residual_dir`
    （先移入 ``.doctor-backup/residual/<ts>/``，含真实内容则拒绝处置，DR-3）。
    """
    path = Path(item["path"])
    action = item.get("action", "delete")
    try:
        if action == "git_worktree_prune":
            res = _run_git(project_root, ["worktree", "prune"])
            if res.returncode != 0:
                return False, {**item, "error": res.stderr.strip()}
            return True, {**item, "disposition": "pruned"}
        if action == "delete_dir":
            # DR-3 + NEW-DR3：先移入 .doctor-backup/residual/<ts>/（可回滚）；
            # 搬移失败显式标记失败并保留源（不强删）；含真实内容返回
            # (False, 带 error) → 调用方归入 manual_notice（不删、不发 stderr，
            # 避免读路径噪声）。
            backup_root = (Path(project_root) / ".orchd" / ".doctor-backup" /
                           "residual" / f"{int(time.time())}")
            return _dispose_residual_dir(path, backup_root)
        # DR-7 对齐（task-autoclean-recheck-align）：auto_clean 删除文件前同样
        # 复检 flock 活性，与 --fix 共用 _pre_delete_lock_recheck 单一事实源；
        # 跳过项以 (False, skipped) 返回，调用方归入 manual_notice（可见不静默）。
        skip_reason = _pre_delete_lock_recheck(path)
        if skip_reason is not None:
            return False, {
                **item, "skipped": "lock_held", "reason": skip_reason}
        _delete_file(path, project_root)
        return True, {**item, "disposition": "deleted"}
    except Exception as exc:
        return False, {**item, "error": f"{type(exc).__name__}: {exc}"}


def _auto_move_legacy(project_root: Path,
                      item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """把 flat 遗留状态文件移入 ``.orchd/.doctor-backup/legacy/<ts>/`` 滚动备份区。

    移动而非删除：早期 flat 布局的运行时文件可能仍含历史状态，保留供人工回滚。
    备份根按时间戳分桶，同名不冲突；移动失败返回 (False, 带 error)。
    """
    path = Path(item["path"])
    backup_root = (Path(project_root) / ".orchd" / ".doctor-backup" /
                   "legacy" / f"{int(time.time())}")
    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        dest = backup_root / path.name
        if path.exists():
            shutil.move(str(path), str(dest))
        return True, {**item, "backup": str(dest), "disposition": "moved"}
    except Exception as exc:
        return False, {**item, "error": f"{type(exc).__name__}: {exc}"}


def auto_clean(project_root: Path,
               *,
               emit_stderr: bool = True) -> dict[str, Any]:
    """doctor 自动清洁入口（DR-7：破坏动作在账本锁内，best-effort 取锁）。

    读路径（status / watchdog）调用，故取锁策略与 ``doctor --fix`` 不同：**拿不到
    账本锁**（并发写动作进行中）时**不执行任何破坏动作**，仅返回 ``deferred=True``
    的报告结果——宁可不清理，也不冒「按旧快照误删活动状态」的 TOCTOU 风险。

    Returns:
        ``_auto_clean_impl`` 的结果；锁不可得时附 ``deferred=True``。
    """
    from orchd.ledger import Store

    # task-readonly-no-migrate：此外层 Store 仅作锁柄（acquire/release），
    # 不经它读写账本——只读构造，不触发建库 / 迁移 / 改名。
    # 契约（task-doctor-master-dir-contract）：入参是 **master 目录**；账本根与
    # 锁路径由 Store 内部 resolve_store_dir 派生，与旧写法逐字一致（等价性由
    # tests/test_doctor.py::TestStoreContractNormalization 钉住）。
    store = Store(Path(project_root) / ".orchd", readonly=True)
    locked = False
    try:
        store.acquire_lock()
        locked = True
    except Exception:
        locked = False
    try:
        if not locked:
            deferred = detect_residues(Path(project_root))
            return {
                "auto_cleaned": [],
                "auto_moved": [],
                "manual_notice": [{
                    **item, "disposition": "reported_only",
                    "deferred_reason": "ledger_lock_busy"
                } for item in deferred],
                "disabled":
                False,
                "deferred":
                True,
            }
        return _auto_clean_impl(project_root, emit_stderr=emit_stderr)
    finally:
        if locked:
            store.release_lock()


def _auto_clean_impl(project_root: Path,
                     *,
                     emit_stderr: bool = True) -> dict[str, Any]:
    """doctor 自动清洁实现（调用方须已持账本锁，见 :func:`auto_clean`）：读路径
    （status / watchdog）自动执行的低风险残留清理。

    分级判定与手动 ``doctor --fix`` 共享 ``detect_residues`` 单一事实源：
    - Auto-Clean 类型（orphan_session_lock / zombie_session / residual_dir /
      stale_worktree_registry）直接删/prune，无备份；
    - legacy_flat_residue 移入 ``.orchd/.doctor-backup/legacy/<ts>/`` 滚动备份区
      （可回滚）；
    - 高风险项（ghost_task / git_tracked_lock / intake_lock）仅计入
      ``manual_notice``，不自动执行。

    环境变量 ``ORCHD_AUTO_CLEAN`` ∈ {off, report} 时降级为只报告不执行
    （``disabled=True``）。

    stderr 留痕与开关契约（task-doctor-auto-clean 返工）：
    - ``emit_stderr=False`` 时完全静默（status/watchdog 挂载处按
      ``config.guidance_stderr`` 传入），结果仍并入返回 JSON 的 ``auto_clean`` 字段；
    - 仅对**真正发生**的 sanitize/move 动作（含失败留痕）写 ``[回收]`` stderr，
      ``manual_notice`` / ``reported_only`` 这类未做任何清理的项**不发** stderr，
      避免读路径噪声。
    全程 best-effort，单项失败不中断其余项，亦不抛异常。

    Args:
        project_root: canonical 项目根。
        emit_stderr: 是否写 ``orchd ▸ [回收]`` stderr（默认 True）。

    Returns:
        {
            "auto_cleaned": [...],   # 已自动清理的残留项
            "auto_moved": [...],     # 已移入备份区的残留项
            "manual_notice": [...],  # 仅报告不执行的项
            "disabled": bool,        # ORCHD_AUTO_CLEAN 关闭时为 True
        }
    """
    project_root = Path(project_root).resolve()
    mode = os.environ.get("ORCHD_AUTO_CLEAN", "").strip().lower()
    disabled = mode in ("off", "report")

    from orchd.worktree import _log_recycle, _recycle_actor

    actor = _recycle_actor()
    auto_cleaned: list[dict[str, Any]] = []
    auto_moved: list[dict[str, Any]] = []
    manual_notice: list[dict[str, Any]] = []

    # DR-13：全局 git worktree prune 每轮只执行一次（与 doctor --fix 同口径），
    # 本轮所有 prune 项共享结果，避免逐项调用导致重复执行与计数虚高。
    scanned = detect_residues(project_root)
    prune_failed: str | None = None
    if any(i.get("action") == "git_worktree_prune" for i in scanned):
        _prune = _run_git(project_root, ["worktree", "prune"])
        if _prune.returncode != 0:
            prune_failed = _prune.stderr.strip()

    for item in scanned:
        rtype = item.get("type")
        target = str(Path(item.get("path", "")))
        if disabled:
            # 关闭 / 只报告：不执行、不留痕，仅计数报告
            manual_notice.append({**item, "disposition": "reported_only"})
            continue
        if rtype in _AUTO_CLEAN_TYPES:
            if item.get("action") == "git_worktree_prune":
                # DR-13：prune 已在循环外执行一次，此处只登记结果
                if prune_failed is None:
                    auto_cleaned.append({
                        **item,
                        "disposition": "pruned",
                        "pruned_once": True,
                    })
                else:
                    manual_notice.append({
                        **item,
                        "disposition":
                        "manual",
                        "error":
                        f"git worktree prune 失败: {prune_failed}",
                    })
                continue
            ok, record = _auto_clean_item(project_root, item)
            if ok:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_clean",
                        "type": rtype,
                        "target": target,
                        "disposition": record.get("disposition"),
                        "reason": item.get("reason", ""),
                        "actor": actor,
                    }])
                auto_cleaned.append(record)
            else:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_clean_failed",
                        "type": rtype,
                        "target": target,
                        "error": record.get("error", ""),
                        "actor": actor,
                    }])
                manual_notice.append(record)
        elif rtype in _LEGACY_MOVE_TYPES:
            ok, record = _auto_move_legacy(project_root, item)
            if ok:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_move",
                        "type": rtype,
                        "target": target,
                        "disposition": "moved_to_backup",
                        "backup": record.get("backup"),
                        "reason": item.get("reason", ""),
                        "actor": actor,
                    }])
                auto_moved.append(record)
            else:
                if emit_stderr:
                    _log_recycle([{
                        "action": "auto_move_failed",
                        "type": rtype,
                        "target": target,
                        "error": record.get("error", ""),
                        "actor": actor,
                    }])
                manual_notice.append(record)
        else:
            # ghost_task / git_tracked_lock / intake_lock 等高风险项：仅报告、
            # 零 stderr（未执行任何清理动作，读写路径均不发噪声）
            manual_notice.append({**item, "disposition": "manual"})

    # A1（task-hostfix-pack-b）：收尾顺带收敛备份保留期（best-effort；有删除才补字段）。
    _pruned = _prune_stale_backups(Path(project_root) / ".orchd" / ".doctor-backup")
    result = {
        "auto_cleaned": auto_cleaned,
        "auto_moved": auto_moved,
        "manual_notice": manual_notice,
        "disabled": disabled,
    }
    if _pruned:
        result["backups_pruned"] = _pruned
    return result
