"""orchd doctor 共享辅助域（W2-D1，file-split-program 20261015 §4.3）。

职责：doctor 包内**无状态的共享辅助原语**，供聚合入口 ``__init__`` 与后续 D2–D5 的
``checks_repo`` / ``checks_runtime`` / ``residue`` / ``fix`` 取用：

- git 子进程辅助：``_run_git`` / ``_resolve_git_dir`` / ``_cat_file_batch``（+ ``_GIT_*`` 常量）；
- 诊断项与残留项构造器：``_make_check`` / ``_make_check_unavailable`` /
  ``_unavailable_residue`` / ``_unreadable_residue``（+ INV-4a「检测不可用 / 不可判定」
  词表常量 ``_DETECTION_UNAVAILABLE_RESIDUE`` / ``_UNREADABLE_RESIDUE`` / ``_UNREADABLE_ACTION``）；
- 保护白名单与运行根：``_SOURCE_ASSETS`` / ``_RuntimeStateFiles`` / ``_RUNTIME_STATE_FILES`` /
  ``_PROTECTED_PATHS`` / ``_is_protected_path`` / ``_resolve_runtime_dir`` / ``_probe_is_file``；
- orchd 自有运行时锁命名白名单：``_LOCK_FILE_PATTERNS`` / ``_is_lock_like_name`` /
  ``_pre_delete_lock_recheck``。

依赖方向：本模块是 doctor 包的**叶子**——只依赖标准库（``re`` / ``subprocess`` /
``pathlib``），运行期零 ``orchd`` 内部导入（``orchd.ledger.resolve_store_dir`` 与
``orchd.lockfile.ExclusiveFileLock`` 保持原有的**函数级惰性导入**站点不变，搬迁不改取用
方式）；本模块不反向导入聚合入口或其它兄弟模块（否则成加载期环）。

补丁面注意（D1 实测口径，W2-D5 更正）：``_run_git`` / ``_cat_file_batch`` 内的
``subprocess`` 以**本模块**的全局名解析，故 ``monkeypatch.setattr("orchd.doctor.subprocess",
...)`` 不再影响它们（现无用例如此使用，实测全集见 tests/test_doctor_pkg.py）；
D5 之后 ``orchd.doctor._run_git`` 本体的补丁亦已失效——消费点全部落在子模块命名空间
（``fix/`` 子包 / ``locks.py`` / ``residue.py`` / ``checks_repo.py``），补丁须改指实际消费模块；
W8-6 把 ``fix.py`` 拆为 ``fix/`` 七域后，消费点再下沉一层，补丁须打 ``orchd.doctor.fix.<域>``。
"""

from __future__ import annotations

from orchd.names import (
    CHECKPOINT_FILE,
    FULL_REGRESSION_FILE,
    IDEAS_ARCHIVE_FILE,
    IDEAS_FILE,
    LAYOUT_FILE,
    LEDGER_FILE,
    LESSONS_FILE,
    LESSONS_STAGED_FILE,
    MASTER_FILE,
    MERGE_ACKS_FILE,
    ROADMAP_FILE,
    SESSION_WORKTREES_FILE,
)


import re
import subprocess
from pathlib import Path
from typing import Any



# git 输出统一按 UTF-8 解码（与 gitops.py 一致）
_GIT_ENCODING = "utf-8"
_GIT_ERRORS = "replace"
_GIT_TIMEOUT = 10


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


# 保护白名单：--fix 绝不触碰这些文件/目录。
# 硬编码在 doctor 包内，确保即使调用方误用也不会损伤核心状态。
# 分两类：源码资产（与位置无关恒保护）与运行时状态（canonical 账本根保护、
# legacy 位置可清——container 布局下 main/.orchd 的 flat 遗留属于可清残留，
# 见 _detect_legacy_flat_residues）。
# 源码资产：与位置无关恒保护。_full_regression.json 是发版元数据产物（full-regression
# 命令与 sync_orchd_core.sh 恒读写 <源码仓库>/.orchd/_full_regression.json，不论布局），
# 归 _SOURCE_ASSETS 而非 _RUNTIME_STATE_FILES，避免 container 布局下被
# _detect_legacy_flat_residues 误判为 flat 遗留而搬走（task-doctor-full-regression-alignment）。
_SOURCE_ASSETS = frozenset({
    MASTER_FILE,
    IDEAS_FILE,
    IDEAS_ARCHIVE_FILE,
    ROADMAP_FILE,
    FULL_REGRESSION_FILE,
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
#   ② 本包 _detect_legacy_flat_residues / _is_protected_path（canonical 保护、
#      legacy 可清）。
# 约束：源码内不得另立第二份成员枚举（负控制见
# tests/test_nogit_changedet.py::TestSingleSourceRuntimeList）。
# .layout.json 是布局标记（布局解析权威，不可清理）：legacy 扫描须跳过
# （见 _detect_legacy_flat_residues），仅快照排除与 canonical 保护消费它。
_RUNTIME_STATE_FILES = _RuntimeStateFiles({
    LEDGER_FILE,
    CHECKPOINT_FILE,
    SESSION_WORKTREES_FILE,
    MERGE_ACKS_FILE,
    LESSONS_FILE,
    LESSONS_STAGED_FILE,
    LAYOUT_FILE,
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
