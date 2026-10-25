"""intake.idea —— 自 ``orchd/intake.py`` 拆出。

IDEAS 生命周期动作（idea_propose / _idea_transition / idea_confirm）。

「放弃 / 离场」自 task-intake-exit-verb-close 起不再有 drop 动作：统一出口
``idea close --reason`` 的后端住在 ``orchd/ideas/decision.py``（搬块归档），
本包不再提供状态降级为 dropped 的通道。

纯搬移（W6-C7）：函数体逐字保留，仅 import 头与落点变化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .guard import _intake_guard, _resolve_lock_orchd_dir
from .parse import (
    IDEA_STATUS_PENDING,
    IDEA_STATUS_STUDY,
    _atomic_write_text,
    _find_idea_entry,
    _list_idea_headers,
    _parse_title_id,
)

def idea_propose(project_root: Path, title: str, feasibility: str) -> dict[str, Any]:
    """灵感写入 IDEAS 写入门禁：追加 status: study 条目（idea-write-gate，propose）。

    执行权：agent 可执行（记录论证中的灵感，待用户 confirm 升 pending 或 close 归档）。
    并发：IDEAS.md 的「读-改-写」（锁内读 → 查重 → 追加）整体在 ``.intake.lock``
    持有范围内完成，与其它准入写者（amend / intake / roadmap-land / idea confirm）
    互斥（task-intake-atomic-lock-consistent AC1）。

    Args:
        project_root: 仓库根目录。
        title: 灵感标题。
        feasibility: 可行性论证（写入 ``- 论证:`` 字段）。

    Returns:
        结构化结果，永不抛异常：
        - 前置失败: ``{"proposed": False, "reason": "not_on_main" | "dirty_workspace", ...}``
        - 重复标题: ``{"proposed": False, "reason": "duplicate_title", ...}``
          （命中面含 IDEAS-archive.md，hint 指名命中文件与该条目 ``- status``；
          命中历史 dropped（仅可能出现在归档件里）时 hint 另给重议路径）
        - 归档件不可读: ``{"proposed": False, "reason": "archive_unreadable", ...}``
          （判重面缺一角即 fail-closed，不写主文件）
        - 主文件不可读: ``{"proposed": False, "reason": "read_error", ...}``（同上：读不到
          IDEAS.md 就无从查重，照常追加可能把已离场条目重提成第二份）
        - 缺显式 id: ``{"proposed": False, "reason": "missing_idea_id", ...}``
        - 成功: ``{"proposed": True, "title", "commit": {...}, 可选 "commit_warning"}``
    """
    from orchd.gitops import ensure_committed
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        read_text_or_error,
        resolve_agent_id,
        single_line,
    )

    project_root = Path(project_root)
    # 行式台账安全网（task-idea-archive-hardening）：标题与论证都是「前缀 + 自由文本
    # 拼一行」，多行输入能伪造出假 ## 小节头。归一必须发生在**查重之前**，否则查重
    # 比的是未归一标题、写入的是归一标题，两边说的不是同一件事。
    title = single_line(title)
    feasibility = single_line(feasibility)
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        # guard 返回 committed 键，转换为该动作的键（proposed），保留 reason 与明细
        return {"proposed": False, **{k: v for k, v in guard_err.items() if k != "committed"}}

    import datetime

    # 读-改-写整体纳入准入写锁（task-intake-atomic-lock-consistent AC1）：此前查重用的
    # IDEAS.md 文本在取锁**之前**读取，锁内又用这份旧 existing 回写——两个并发 propose
    # 都读到旧文本时，后写者以旧文本覆盖前者条目（TOCTOU 丢写）。现在
    # 取锁 → 锁内新读 → 查重 → 写，全程持锁（对齐 roadmap_land 的「锁内新读」语义）。
    orchd_dir = _resolve_lock_orchd_dir(project_root)
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        ws = resolve_workspace_root(project_root)
        ideas = ws / "IDEAS.md"
        text, text_err = (
            read_text_or_error(ideas) if ideas.exists() else ("", None)
        )
        if text_err is not None:
            # 判重面缺一角即 fail-closed（与下方归档件读同族）：读不到主文件就无从
            # 查重，照常追加可能把已存在（含已裁决放弃）的条目重提成第二份。
            return {
                "proposed": False,
                "reason": "read_error",
                "title": title,
                "hint": (
                    "IDEAS.md 不可按 UTF-8 读出（权限 / 是否被换成目录 / 编码）："
                    "先修好该文件再 propose"
                ),
            }
        # 查重面 = IDEAS.md ∪ IDEAS-archive.md（ideas-dropped-exit）：条目离场后正文
        # 原样留在归档面，只查 IDEAS.md 等于把"这条已被用户裁决放弃"的记忆随块一起
        # 抹掉——同一标题可被无声重提，同 id 更会在归档面与主文件分叉成两份历史。
        # 面仅此两文件，与 roadmap_land 的判重面同形（task-roadmap-land-dedup-across-archive）。
        hit = _find_idea_entry(text, title)
        hit_file, hit_status = "IDEAS.md", (hit or {}).get("status") or ""
        if hit is None:
            archive = ws / "IDEAS-archive.md"
            # 归档件不存在时不必二次查表：语义等价（空文本必不命中），且让"取锁后
            # 只查一次"的既有锁序探针（test_intake_atomic_consistent）在净库上继续成立。
            if archive.exists():
                try:
                    a_text = archive.read_text(encoding="utf-8")
                except (OSError, IOError, UnicodeDecodeError) as exc:
                    # fail-closed：判重面缺一角就不写。读不到归档件却照常追加，等于把
                    # 已离场（含已裁决放弃）的条目重提成第二份——那正是本次扩面要防的事。
                    # 本函数契约是"永不抛异常"（docstring），故异常在此收口成结构化拒绝，
                    # 不退化成 E999（同族缺陷见 pending 卡 idea-archive-hardening 第④项，
                    # 那条管 roadmap_land 的既有裸读，不在本卡范围）。
                    return {
                        "proposed": False,
                        "reason": "archive_unreadable",
                        "title": title,
                        "hint": (
                            f"IDEAS-archive.md 不可读（{type(exc).__name__}）：判重面缺一角，"
                            "此时写入可能把已离场的条目重提成第二份，故拒绝。处置：先确认该"
                            "文件能按 UTF-8 读出（权限 / 是否被换成了目录 / 编码），修好前不要"
                            "手改其正文，必要时走 doctor 或重新归档收敛，再重跑 propose。"
                        ),
                    }
                hit = _find_idea_entry(a_text, title)
                if hit is not None:
                    hit_file = "IDEAS-archive.md"
                    hit_status = hit.get("status") or ""
        if hit is not None:
            hint = f"{hit_file} 已存在标题为 '{title}' 的条目"
            if hit_file != "IDEAS.md":
                hint += f"（该条目已离场留档，status: {hit_status or '未声明'}）"
            if hit_status == "dropped":
                # dropped 已从活跃白名单下线（task-intake-exit-verb-close），但归档件里的
                # 历史条目仍带该状态——它们不被校验读取，只作为判重记忆存在，故重议
                # 路径的 hint 必须保留，否则「搬走」会连「这条已被裁决放弃」一起抹掉。
                hint += (
                    "处置：放弃是用户对**这一条**的裁决，不是对同主题永久封嘴——确要"
                    "重议请另立新 id 重新 propose，并在新条目正文引用旧 id（如"
                    "「重议 <旧 id>」）说明裁决改变了什么；沿用原标题会被本判据持续拒绝。"
                )
            return {
                "proposed": False,
                "reason": "duplicate_title",
                "title": title,
                "hint": hint,
            }

        # 显式 id 强校验（fail-closed）：- id: 是 E025 溯源校验与自动归档的权威锚点。
        # 缺 id 的条目后续必然失败（amend 报 E025 source 引用不存在）且永不自动归档，
        # 故在此早失败，而非让问题推迟到注册阶段才暴露（对齐引擎硬约束取向）。
        # 判定顺序保持「重复标题」在前（与既有优先级一致，零回归）。
        idea_id = _parse_title_id(title)
        if not idea_id:
            return {
                "proposed": False,
                "reason": "missing_idea_id",
                "title": title,
                "hint": (
                    "标题须含显式 id，写法：`<标题>（id: <slug>）`；"
                    "slug 仅允许字母 / 数字 / 连字符 / 下划线，且以字母或数字开头。"
                    "例：`引擎硬化整改（id: audit-engine-hardening）`。"
                    "原因：- id: 是 E025 溯源校验与自动归档的权威锚点，缺 id 的条目"
                    "既无法被 source 引用（amend 必报 E025），也永不自动归档。"
                ),
            }

        date = datetime.date.today().isoformat()
        entry = (
            f"## {date} {title}\n"
            f"- status: {IDEA_STATUS_STUDY}\n"
            f"- id: {idea_id}\n"
            f"- 论证: {feasibility}\n"
            f"- notes: 由 orchd idea propose 写入（idea-write-gate），待用户 confirm 升 pending 或 close 归档。\n"
        )
        if ideas.exists():
            existing = text
            if not existing.endswith("\n"):
                existing += "\n"
            _atomic_write_text(ideas, existing + "\n" + entry)
        else:
            _atomic_write_text(ideas, "# IDEAS\n" + "\n" + entry)

        commit = ensure_committed(
            project_root,
            [str(ideas)],
            f"chore(idea): orchd idea propose — {title}",
        )
        result: dict[str, Any] = {
            "proposed": True,
            "title": title,
            "id": idea_id,
            "commit": commit,
        }
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"idea propose commit 未执行（{commit.get('reason')}）：IDEAS.md 改动"
                    "可能未入库，请人工核对"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def _idea_transition(
    project_root: Path,
    title: str,
    target_status: str,
    action: str,
) -> dict[str, Any]:
    """idea confirm 后端：把 status: study 条目改写为 target_status。

    执行权：仅用户执行（confirm 是裁决动作，agent 不得代行）。

    Returns:
        结构化结果，永不抛异常（结果键用动作过去式：confirm→confirmed）：
        - 前置失败: ``{"{key}": False, "reason": "not_on_main" | "dirty_workspace"}``
        - 主文件不可读: ``{"{key}": False, "reason": "read_error"}``（fail-closed，不改写）
        - 不存在: ``{"{key}": False, "reason": "not_found"}``
        - 非 study: ``{"{key}": False, "reason": "not_study", "current_status": ...}``
        - 成功: ``{"{key}": True, "title", "new_status", "commit": {...}}``
    """
    from orchd.gitops import ensure_committed
    from orchd.ledger import (
        resolve_workspace_root,
        intake_lock_acquire,
        intake_lock_release,
        resolve_agent_id,
    )

    # 结果键固定 confirmed：drop 通道随 task-intake-exit-verb-close 下线，
    # _idea_transition 只服务 confirm（放弃的出路改由 idea close 一次归档承担）。
    key = "confirmed"

    project_root = Path(project_root)
    guard_err = _intake_guard(project_root)
    if guard_err is not None:
        # guard 返回 committed 键，转换为该动作的过去式键，保留 reason 与明细
        return {key: False, **{k: v for k, v in guard_err.items() if k != "committed"}}

    # 读-改-写整体纳入准入写锁（task-intake-atomic-lock-consistent AC1）：此前条目定位
    # 与状态校验用锁外读到的 text，锁内再按该 text 的**行区间**改写——锁外读与锁内写之间
    # 若有并发写者追加/改写条目，行区间错位会覆盖并发改动（TOCTOU）。现在
    # 取锁 → 锁内新读 → 定位/校验 → 改写，全程持锁。
    orchd_dir = _resolve_lock_orchd_dir(project_root)
    lk = intake_lock_acquire(orchd_dir, resolve_agent_id(orchd_dir))
    try:
        ws = resolve_workspace_root(project_root)
        ideas = ws / "IDEAS.md"
        if not ideas.exists():
            return {key: False, "reason": "not_found", "title": title}
        from orchd.ledger import read_text_or_error

        # 读盘兜底走唯一原语（task-idea-archive-hardening）：本函数 docstring 同样
        # 声称"永不抛异常"，非 UTF-8 / 目录形 IDEAS.md 此前会让异常穿到 CLI。
        text, text_err = read_text_or_error(ideas)
        if text_err is not None:
            return {key: False, "reason": "read_error", "title": title}
        entry = _find_idea_entry(text, title)
        if entry is None:
            import difflib
            all_headers = _list_idea_headers(text)
            candidates = difflib.get_close_matches(title, all_headers, n=3, cutoff=0.3)
            return {
                key: False,
                "reason": "not_found",
                "title": title,
                "hint": (
                    "标题须含「（id: <slug>）」后缀（propose 写入的完整标题格式为 "
                    "「YYYY-MM-DD <标题>（id: <slug>）」）。可传完整标题、去日期前缀标题，"
                    "或裸 slug（<slug> 与条目内 - id: 字段精确相等）。查看完整标题："
                    "python .orchd/__main__.py ideas list，或直接读 .orchd/IDEAS.md。"
                ),
                "candidates": candidates,
            }
        if entry["status"] != IDEA_STATUS_STUDY:
            return {
                key: False,
                "reason": "not_study",
                "title": title,
                "current_status": entry["status"],
                "hint": f"仅 status: study 条目可 {action}（当前 '{entry['status']}'）",
            }

        lines = entry["lines"]
        new_lines: list[str] = []
        replaced = False
        for ln in lines:
            s = ln.strip()
            if s.startswith("- status:") or s.startswith("status:"):
                indent = ln[: len(ln) - len(ln.lstrip())]
                new_lines.append(f"{indent}- status: {target_status}")
                replaced = True
            else:
                new_lines.append(ln)
        if not replaced:
            # 理论上不会走到（_find_idea_entry 已确认含 status），防御性兜底
            new_lines.append(f"- status: {target_status}")

        all_lines = text.splitlines()
        new_content_lines = all_lines[: entry["header_line"]] + new_lines + all_lines[entry["end"]:]
        _atomic_write_text(ideas, "\n".join(new_content_lines) + "\n")

        commit = ensure_committed(
            project_root,
            [str(ideas)],
            f"chore(idea): orchd idea {action} — {title}",
        )
        result: dict[str, Any] = {
            key: True,
            "title": title,
            "new_status": target_status,
            "commit": commit,
        }
        if commit.get("performed") is False and commit.get("reason") != "no_changes":
            result["commit_warning"] = {
                "reason": commit.get("reason"),
                "message": (
                    f"idea {action} commit 未执行（{commit.get('reason')}）：IDEAS.md 改动"
                    "可能未入库，请人工核对"
                ),
            }
        return result
    finally:
        intake_lock_release(lk)


def idea_confirm(project_root: Path, title: str) -> dict[str, Any]:
    """把 status: study 条目升为 pending（idea-write-gate，confirm，仅用户执行）。"""
    return _idea_transition(project_root, title, IDEA_STATUS_PENDING, "confirm")
