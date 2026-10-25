"""orchd ideas — 归档主流程域（archive_resolved_ideas / 待归档指纹记录）（W6-C4 拆分，纯搬移）。

来源：``orchd/ideas.py``（1211 行，W6-C4 纯搬移）。
"""

from __future__ import annotations

import json
import re as _re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchd.names import (
    IDEAS_ARCHIVE_FILE,
    IDEAS_FILE,
)
from orchd.contracts.events import TERMINAL_STATUSES
from orchd.errors import OrchdError
from orchd.intake import (
    _atomic_write_text,
    _find_idea_entry,
    _intake_guard,
    _list_idea_headers,
    _resolve_lock_orchd_dir,
)
# 行式台账文本原语（task-idea-archive-hardening）：读盘兜底与自由文本单行化收敛到
# ledger 层（依赖方向见 .orchd/shared/conventions.md——ideas 已模块级导入 intake，
# 刻意不从 intake 反向取；ledger 是本模块与其共用的下层）。
from orchd.ledger import read_text_or_error, single_line
from .parse import (
    extract_entry_block,
    parse_ideas,
)

from .comments import (
    comment_visibility,
    unclosed_comment_line,
)

from .classify import (
    find_archivable_entries,
    find_comment_hidden_entries,
    find_deferred_unbalanced_entries,
)

from .blocks import (
    _archive_has_identical_block,
    _canonical_block,
    _remove_blocks,
)



# 待归档标记文件名（账本根运行时文件；task-archive-lock-unavailable-recovery：
# 锁不可用时的兜底留痕，与 _ledger.jsonl 同级，不随工作区文档走 git）
_ARCHIVE_PENDING_FILE = "archive-pending.json"


# 归档文件头注释（新建时写入）
_ARCHIVE_HEADER = (
    "# IDEAS Archive\n\n"
    "本文件由 orchd 引擎自动维护：已完结 idea 条目（对应任务全部进入终态）与用户已"
    "裁决放弃的条目（``- status: dropped``，仍被非终态任务引用的除外）从 IDEAS.md "
    "自动移入此处，保留原文与审计可追溯性。勿手动编辑。\n"
)



def archive_resolved_ideas(project_root, master) -> dict[str, Any]:
    """主入口：把已完结 / 已放弃的 idea 条目从 IDEAS.md 移入 IDEAS-archive.md。

    判据单一入口 :func:`find_archivable_entries`（resolved ∪ dropped，且块边界不得跨
    注释标记），锁外预扫描与锁内判定两处都走它——一处新一处旧会让 lock_unavailable
    待归档标记的计数与实际搬走的块不一致。被配对不变量挡下的候选经
    :func:`find_deferred_unbalanced_entries`（同一个核的另一列）随结果返回，不静默少搬。

    并发与原子性（task-intake-atomic-lock-consistent AC2）：IDEAS.md 的「读-改-写」
    与 IDEAS-archive.md 的追加写入整体在 ``.intake.lock`` 持有范围内完成（与 amend /
    intake / idea propose-confirm 共用同一把准入写锁）；两次写入统一走
    ``intake._atomic_write_text``（tmp + ``os.replace``），崩溃不留半截文件。

    Args:
        project_root: 项目根目录。
        master: 已加载的 Master 对象。

    Returns:
        结构化结果，永不抛异常：
        - ``{"archived": [...], "kept": n}`` 成功归档；archived 为标题列表。
        - ``{"archived": [...], "kept": n, "deduped": m}`` 同上，但有 m 块因归档件里
          已有同 id 同正文块而跳写（键仅在此时出现，无跳写时形态与既有一致）。
        - 两形态下都可能出现 ``"deferred_comment_unbalanced": [...]``（标题列表）：
          命中离场判据、但块内注释标记不自配因而**没搬**的条目；键仅在非空时出现。
        - 同样两形态下可能出现 ``"comment_hidden_candidate": [...]``：每项为
          ``{"title", "line", "hidden_by"}``（line 为 1 基原文行号；hidden_by 取
          ``unclosed_comment`` = 被未闭合 opener 延伸到文件末尾罩住 /
          ``closed_comment`` = 标题落在成对注释区间内），以及 ``"comment_unclosed_at":
          <行号>``（**仅当文本里确有未闭合 ``<!--``** 时出现）。命中的都是"本该离场却
          被注释区间冻结"的条目；成因逐条点明，故不与 ``ideas list`` 的可见性规则相左。
        - ``{"archived": [], "kept": 0, "skipped": "<原因>"}`` 无 IDEAS.md /
          读取失败 / 无可归档条目 / 写入失败 / 准入锁不可用（best-effort 降级，
          绝不无锁并发归档）。
    """
    project_root = Path(project_root)
    # AC3（task-12-engine-path-abstraction）：工作区文档（IDEAS.md /
    # IDEAS-archive.md）走统一工作区根 helper（默认 .orchd/，兼容旧根路径）。
    # Store 的账本根仍由 ORCHD_HOME 解析（与文档根分离）。
    # canonical 共享读（task-canonical-workspace-docs，2026-08-25）：container 布局
    # 下 resolve_workspace_root 解析到 canonical 主工作树根，归档源（IDEAS.md）与
    # 归档目标（IDEAS-archive.md）统一在主工作树读写，任务 worktree 本地副本不参与。
    from orchd.ledger import (
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
        resolve_workspace_root,
    )

    # canonical 工作区根（task-canonical-workspace-docs，2026-08-25）：
    # resolve_workspace_root 先解析到 canonical 主工作树根（container 布局返回
    # main/，flat 返回本地），IDEAS.md / IDEAS-archive.md 以主工作树副本为权威，
    # 避免任务 worktree 本地 .orchd/ 拷贝过期导致归档/引导不一致。
    workspace_root = resolve_workspace_root(project_root)
    ideas_path = workspace_root / IDEAS_FILE
    if not ideas_path.exists():
        return {"archived": [], "kept": 0, "skipped": "no_ideas_file"}

    # 读取任务终态（best-effort：ledger 不可用则无证据，不归档）
    # 注：终态快照在取锁前计算（只读，轻微陈旧最多让条目延后一轮归档），使锁窗口
    # 只覆盖「读 IDEAS.md → 写归档 + 改写 IDEAS.md」，不扩大到账本重放。
    task_status: dict[str, str] = {}
    try:
        from orchd.ledger import Store

        orchd_dir = project_root / ".orchd"
        store = Store(orchd_dir)
        task_status = {
            tid: ts.status for tid, ts in store.replay().items()
        }
    except Exception:
        pass

    # 取锁前的只读预扫描（task-archive-lock-unavailable-recovery）：计算当前可归档
    # 条目，供锁不可用时写入待归档标记（计数/留痕用，不参与实际写入）。锁内仍以
    # 重新读取的最新 IDEAS.md 为准做归档（读-改-写在锁窗口内），预扫描只读、轻微
    # 陈旧最多让标记计数延后一轮，不引入并发写。
    pre_resolved: list[dict[str, Any]] = []
    try:
        pre_text = ideas_path.read_text(encoding="utf-8")
        pre_entries = parse_ideas(pre_text)
        pre_resolved = find_archivable_entries(
            master, pre_entries, task_status, text=pre_text
        )
    except (OSError, IOError, UnicodeDecodeError):
        pass

    # AC2（task-intake-atomic-lock-consistent）：归档全流程纳入准入写锁。此前
    # 「读 IDEAS.md → 改主文件 + 写归档」无锁，与 amend / intake / idea
    # propose-confirm 等文档写者并发时可用旧文本覆盖对方改动（丢写）。锁被占/超时
    # 则降级为不归档（best-effort 跳过，绝不无锁并发归档），保持本函数永不抛异常。
    lock_dir = _resolve_lock_orchd_dir(project_root)
    try:
        lk = intake_lock_acquire(lock_dir, resolve_agent_id(lock_dir))
    except (OrchdError, OSError) as exc:
        # 兜底重试标记（task-archive-lock-unavailable-recovery）：锁不可用时不静默
        # 丢弃积压——写入待归档标记（时间戳 / 可归档条目数 / 原因），下次归档触发
        # 时优先重试；绝不无锁写 IDEAS.md / IDEAS-archive.md（硬约束保持）。
        _record_archive_pending(
            project_root,
            reason=(
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            ),
            pending_count=len(pre_resolved),
            pending_ids=[(e.get("id") or "") for e in pre_resolved],
        )
        return {"archived": [], "kept": 0, "skipped": "lock_unavailable"}
    try:
        # 读盘兜底走唯一原语（task-idea-archive-hardening）：此前同一形状的 except 在本域
        # 写了两份（本处与归档件读），遗漏一处即"永不抛异常"失实——正是本卡要收的根因面。
        text, text_err = read_text_or_error(ideas_path)
        if text_err is not None:
            return {"archived": [], "kept": 0, "skipped": "read_error"}

        entries = parse_ideas(text)
        resolved = find_archivable_entries(master, entries, task_status, text=text)
        # 诊断列走同一个核（只是另一列返回值），不与放行面各写一遍判据；多算一次的
        # 代价换"挡下了什么"必须可见。
        deferred = [
            e.get("title") or ""
            for e in find_deferred_unbalanced_entries(
                master, entries, task_status, text=text
            )
        ]
        # 第三列诊断（task-idea-archive-diagnostics）：被注释区间冻结的候选 + 未闭合
        # opener 的行号。两者同源派生，故"谁在第几行开了没收尾"与"哪几条被它罩住"
        # 不可能互相矛盾。
        # 逐条带行号与**成因**（spec 审查 MAJOR-1）：候选可能来自"未闭合 opener 延伸到
        # 文件末尾"，也可能来自"标题落在成对注释区间内"——后者按 list 的同一条掩码规则
        # 本就不算条目，两条通道若都只报"冻结"就会各说各话。故逐条点明 hidden_by。
        frozen_lines = comment_visibility(text)[1]
        frozen = [
            {
                "title": e.get("title") or "",
                "line": e["start_line"] + 1,
                "hidden_by": (
                    "unclosed_comment"
                    if e["start_line"] in frozen_lines
                    else "closed_comment"
                ),
            }
            for e in find_comment_hidden_entries(
                master, entries, task_status, text=text
            )
        ]
        unclosed_at = unclosed_comment_line(text)
        if not resolved:
            # 无待归档条目：若存在上次锁不可用遗留的积压标记，说明条目已被并发/
            # 手动处理，清除标记（幂等），避免残留造成巡检误报。
            _clear_archive_pending(project_root)
            idle: dict[str, Any] = {"archived": [], "kept": len(entries)}
            if deferred:
                idle["deferred_comment_unbalanced"] = deferred
            if frozen:
                idle["comment_hidden_candidate"] = frozen
                if unclosed_at is not None:
                    idle["comment_unclosed_at"] = unclosed_at
            return idle

        blocks: list[str] = []
        for e in resolved:
            # 判重键与落档字节共用同一份 canonical 形式（task-idea-archive-hardening 起
            # 收敛到 _canonical_block 单源）：此前这里是内联补尾换行、人工裁决通道则原样
            # 搬，两通道对同一正文可能写出不同字节 ⇒ 去重判不中、归档留双块。
            blocks.append(_canonical_block(extract_entry_block(text, e)))
        new_ideas = _remove_blocks(text, entries, resolved)

        archive_path = workspace_root / IDEAS_ARCHIVE_FILE
        try:
            if archive_path.exists():
                # 同上：归档件读也走唯一原语（此前这处 except 只捕 OSError/IOError，
                # 非 UTF-8 的 UnicodeDecodeError 会穿到 CLI——同族第四处）。
                archive_text, archive_err = read_text_or_error(archive_path)
                if archive_err is not None:
                    return {
                        "archived": [],
                        "kept": len(entries),
                        "skipped": "read_error",
                    }
            else:
                archive_text = _ARCHIVE_HEADER
            if archive_text and not archive_text.endswith("\n"):
                archive_text += "\n"
            # 追加前逐块过去重键（ideas-dropped-exit AC6）：判据扩到 dropped 之后，
            # 「归档件里已有这一块、主文件里还留着这一块」从不可达变成可达——半写重试
            # （归档已写成、主文件回写失败）与人工/自动两通道撞同一块都会落到这个态。
            # 无脑追加的后果是归档件里出现第二份同 id 同正文块：审计面翻倍，后续任何
            # 按 id 的判据（E031 落地、roadmap-land 判重）都会读到两份历史。
            # 跳写但仍删主文件——内容已在档，删主文件正是"离场"的本意。
            deduped = 0
            for eid, block in zip((e.get("id") or "" for e in resolved), blocks):
                if _archive_has_identical_block(archive_text, eid, block):
                    deduped += 1
                    continue
                archive_text += block

            # 先写归档文件（成功后再改主文件，避免主文件已删而归档丢失）；两次写入
            # 均原子（tmp + os.replace，AC2）——任一步失败主文件保持原状，不留半截。
            _atomic_write_text(archive_path, archive_text)
            _atomic_write_text(ideas_path, new_ideas)
        except (OSError, IOError):
            return {"archived": [], "kept": len(entries), "skipped": "write_error"}

        # 归档成功：清除积压标记（幂等；不存在则无动作）
        _clear_archive_pending(project_root)
        out: dict[str, Any] = {
            "archived": [e["title"] for e in resolved],
            "kept": len(entries) - len(resolved),
        }
        if deduped:
            # 仅在真发生跳写时附加（既有调用方断言 archived/kept/skipped 三键，
            # 无新键即零回归）：让人能看见"这一轮有几块是收敛而非新增"。
            out["deduped"] = deduped
        if deferred:
            out["deferred_comment_unbalanced"] = deferred
        if frozen:
            # 候选逐条带行号与成因；文件级未闭合行号只在确有未闭合 opener 时出现
            # （成对注释区间导致的冻结没有"未闭合行"可指，报 null 会误导消费方）。
            out["comment_hidden_candidate"] = frozen
            if unclosed_at is not None:
                out["comment_unclosed_at"] = unclosed_at
        return out
    finally:
        intake_lock_release(lk)



def _archive_pending_path(project_root) -> Path:
    """解析待归档标记文件路径（账本根运行时文件）。

    账本根经 ``ledger.resolve_store_dir`` 解析（ORCHD_HOME 优先 → container 布局
    ``<容器>/.orchd-runtime/`` → flat 回退 ``.orchd/``），与 ``_ledger.jsonl``
    同级；标记是运行时状态，不随工作区文档（IDEAS.md / IDEAS-archive.md）走 git，
    也不参与仓库 diff。
    """
    from orchd.ledger import resolve_store_dir

    orchd_dir = _resolve_lock_orchd_dir(project_root)
    return resolve_store_dir(orchd_dir) / _ARCHIVE_PENDING_FILE



def _record_archive_pending(
    project_root,
    *,
    reason: str,
    pending_count: int,
    pending_ids: list[str],
) -> None:
    """锁不可用时写入待归档标记（best-effort，失败静默降级）。

    标记内容：UTC 时间戳（ISO-8601）、可归档条目数、原因、待归档条目 id。
    写入原子化（tmp + os.replace，复用 intake 契约）；**不触碰** IDEAS.md /
    IDEAS-archive.md——标记只是运行时留痕，供下次归档触发时优先重试，以及
    doctor/状态命令巡检发现「长期不归档」积压。
    """
    marker = {
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pending_count": pending_count,
        "pending_ids": pending_ids,
    }
    try:
        _atomic_write_text(
            _archive_pending_path(project_root),
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        )
    except (OSError, IOError, TypeError):
        pass



def _clear_archive_pending(project_root) -> None:
    """清除待归档标记（幂等：不存在即无动作；失败静默降级）。

    归档成功、或确认无待归档条目后调用——积压已被处理，残留标记会造成巡检
    误报，必须清除。
    """
    try:
        _archive_pending_path(project_root).unlink(missing_ok=True)
    except OSError:
        pass



def read_archive_pending(project_root) -> dict[str, Any] | None:
    """结构化读取待归档标记（供 doctor/状态命令巡检接线）。

    Args:
        project_root: 项目根目录。

    Returns:
        标记内容 dict（含 reason / timestamp / pending_count / pending_ids）；
        标记不存在或文件损坏 / 读取失败时返回 None（安全降级，永不抛异常）。
    """
    path = _archive_pending_path(project_root)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, IOError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data
