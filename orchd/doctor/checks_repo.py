"""``orchd.doctor`` 仓库级检查域（W2-D2，file-split-program 20261015 §4.3）。

职责：``orchd doctor`` 的仓库面检查与聚合——契约漂移（``_check_contract_drift``）、master
单副本（``_check_master_single_copy``）、in_review 工作树完整性
（``_check_in_review_worktree_integrity``），以及把运行态检查（checks_runtime 5 个）与
残渣检测（residue 2 个）聚合成统一报告的 ``check_repo``。

承载 7 个顶层定义（4 函数 + 3 随域常量）；函数体与搬迁前基线**逐字一致**（AST 等价性与
字节级判据见 tests/test_doctor_pkg.py）。

依赖方向：标准库（``re`` / ``pathlib``）+ ``orchd.doctor.runtime``（5 名）+
``orchd.doctor.checks_runtime``（5 个检查）+ ``orchd.doctor.residue``（2 个检测器）+
``orchd.doctor.proposals``（``_load_master_task_ids``）——全部为先做的卡，故为 DAG 前向边；
对 ``orchd.worktree`` / ``orchd.ledger`` / ``orchd.line_ctx`` 保持**函数级惰性导入**
（搬迁不改取用方式，保持 monkeypatch 补丁面）。本模块**不得**导入 ``orchd.doctor`` 聚合入口。
"""

from __future__ import annotations

import re
from pathlib import Path

from orchd.names import (
    MASTER_FILE,
)
from orchd.doctor.checks_runtime import (
    _check_checkpoint_consistency, _check_event_schema, _check_hung_git_processes,
    _check_lessons_runtime, _check_migration_consistency,
)
from orchd.doctor.proposals import _load_master_task_ids
from orchd.doctor.residue import _detect_residual_dirs, _detect_stale_worktree_registry
from orchd.doctor.runtime import (
    _cat_file_batch, _make_check, _make_check_unavailable, _resolve_git_dir, _run_git,
)


# refs/ 根目录扫描忽略的 OS 自动生成文件（macOS Finder 会在目录内生成
# .DS_Store、Windows 生成 Thumbs.db）：此类文件由文件系统自动再生，不属于
# 非法 loose ref，不应误报污染健康检查；与 worktree.py _JUNK_NAMES 中
# OS 杂项语义一致（doctor 为叶子模块，零 orchd 内部依赖，故在本文件内定义）。
_REFS_ROOT_IGNORE = frozenset({".DS_Store", "Thumbs.db"})







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
            wt_master = child / ".orchd" / MASTER_FILE
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
