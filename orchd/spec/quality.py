"""orchd/spec/quality.py — 质量校验与终态告警豁免域（W4-S3 拆分，纯搬移）。

来源：``orchd/spec/__init__.py``（起点 204aaa5a）的 L375-774：validate_quality /
终态豁免表 / 告警过滤
拆分记录见 design/file-split-program-20261015.md §4.5.3。

依赖方向：quality.py → errors.py / spec.declarations / spec.schema / spec.structure /
spec.quality_terms / spec.verify_cmd。
"""

from typing import Any

from orchd.errors import ErrorCode
from orchd.spec.declarations import derive_related_test_files
from orchd.spec.quality_terms import (
    _DECL_COUNT_RE,
    _GRANULARITY_MAX_AC,
    _GRANULARITY_MAX_FILES,
    _GRANULARITY_MAX_HOURS,
    _STRUCTURAL_TERM_ALLOWLIST,
    _STRUCTURAL_TERM_RE,
    _VAGUE_WORDS,
    _VAGUE_WORDS_WHITELIST,
    _ac_repo_claim,
    _build_term_corpus,
    _is_doc_task,
    _project_root_from_master,
    _tests_root_from_master,
    _verify_command_path_tokens,
    _verify_has_grep,
    _verify_has_repo_grep,
    _verify_has_run_ref,
)
from orchd.spec.schema import jsonschema_available
from orchd.spec.structure import Master, ValidationError
from orchd.spec.verify_cmd import (
    _basetemp_platform_issues,
    _runs_pytest,
    _verify_unsafe_reasons,
)


def validate_quality(
    master: Master,
    terminal_ids: frozenset[str] | None = None,
) -> list[ValidationError]:
    """任务定义质量校验（弱 LLM 兜底）。

    加法式校验：不改变 validate_structure / validate_references 的既有行为，
    仅在它们之后追加质量层面的告警。

    校验项：
    1. E022 —— verify_command 缺失（warning；代码类任务在 amend 注册点按
       ``_is_doc_task`` 判定阻断，文档/基础设施类仅 warning）。
    2. E023 —— acceptance_criteria 含模糊词（warning），白名单豁免常见可验证语义。
    3. E037 —— verify_command 引用的仓库内路径既未声明（files_to_edit /
       exempt_files）又不存在 → 阻断级：声明口径一致性（task-decl-consistency-error-code；
       终态任务由调用方按 TERMINAL_EXEMPT_QUALITY_CODES 豁免）。
    4. E038 —— brief 的「files_to_edit 控 N」与实际声明数不一致 → warning
       （同任务；**非拆分建议**，专用码从 E029 拆出）。
    5. E040 —— AC 声称「零残留 / 全仓」可判定性质但 verify_command 无对应
       grep 覆盖 → warning（全仓声称要求 repo 级 grep，文件级不算；manual 类跳过）。
    6. E024 / E026 / E029 其余项 —— 见各代码块内注释（basetemp / 测试连带 / 粒度锚点）。
    7. E030 —— jsonschema 缺依赖时结构校验降级提示（warning，不阻断；
       task-spec-lazy-import-degrade：validate CLI 把本函数输出渲染为
       warnings，valid 仍为 True，退出码为 0 且降级可检索）。

    Args:
        master: 任务池 master。
        terminal_ids: 终态（completed/cancelled）任务 id 集合（P2-2 模式，
            task-validate-terminal-quiet）：集合内任务跳过 E026/E027（已关闭
            任务的缺陷不再重复上报）；为 None/空时行为与旧版完全一致。其余
            终态豁免码（E023/E029/E037/E038）仍由调用方经
            :func:`filter_terminal_quality_warnings` 处理，本函数不扩大口径。

    Returns:
        ValidationError 列表（E022/E023/E024/E026/E027/E029/E030/E037/E038/E040…）；
        合法时返回空列表（jsonschema 缺依赖时含一条 E030 降级提示，见校验项 6）。
    """
    errors: list[ValidationError] = []
    # 缺依赖降级提示（task-spec-lazy-import-degrade，加法式，warning 不阻断）：
    # jsonschema 缺失时 validate_structure 跳过 schema 校验——此处记一条 E030
    # 告警（errors.py 口径：模块导入失败至少升级为 E030 告警，绝不静默），使
    # validate CLI（warnings 档，valid 仍 True）在响应体明确报告
    # 「缺依赖，校验未执行」，退出码保持 0。依赖齐备时零输出，行为零变化。
    if not jsonschema_available():
        errors.append(
            ValidationError(
                code=ErrorCode.E030,
                path="$.schema_version",
                message=(
                    "jsonschema 缺依赖，结构校验未执行（已降级跳过）："
                    "validate_structure 仅执行声明路径控制字符检查；"
                    "pip install jsonschema 后重跑以启用完整校验"
                ),
            ))
    tasks = master.tasks
    # 声明路径存在性的解析基准（校验①用）：与 E026 的 tests/ 推导同源，单一真源。
    project_root = _project_root_from_master(master)

    # task-ac-term-resolvability：结构术语候选预扫描（纯正则、零 IO）。无候选时
    # 不建语料、不走文件系统，行为与旧版逐字一致。
    _term_candidates: dict[tuple[int, int], list[str]] = {}
    for _i, _t in enumerate(tasks):
        for _j, _ac in enumerate(_t.get("acceptance_criteria", []) or []):
            if not isinstance(_ac, str):
                continue
            _seen: set[str] = set()
            _uniq: list[str] = []
            for _h in _STRUCTURAL_TERM_RE.findall(_ac):
                if _h not in _seen:
                    _seen.add(_h)
                    _uniq.append(_h)
            if _uniq:
                _term_candidates[(_i, _j)] = _uniq
    _term_corpus: str | None = None

    def _corpus() -> str:
        nonlocal _term_corpus
        if _term_corpus is None:
            _term_corpus = _build_term_corpus(project_root) if _term_candidates else ""
        return _term_corpus

    for i, t in enumerate(tasks):
        tid = t.get("id", "")
        # F6（task-validate-terminal-quiet，P2-2 同式）：终态任务跳过 E026/E027。
        terminal = tid in (terminal_ids or frozenset())

        # E022: verify_command 必填
        # R5（task-constraint-quality-checks）：缺 verify_command 是否阻断，取决于任务类型。
        #   代码类（files_to_edit 含非文档文件）→ amend 注册点阻断；
        #   文档/基础设施类（全部文档后缀）→ 维持 warning。
        verify_cmd = t.get("verify_command")
        if not verify_cmd or (isinstance(verify_cmd, str)
                              and not verify_cmd.strip()):
            is_code = not _is_doc_task(t)
            errors.append(
                ValidationError(
                    code=ErrorCode.E022,
                    path=f"$.tasks[{i}].verify_command",
                    message=
                    (f"task '{tid}' missing verify_command (required for automated validation)"
                     +
                     ("" if not is_code else "；代码类任务缺 verify_command，注册被阻断")),
                ))

        # R4（task-constraint-quality-checks）：任务拆解粒度启发式越界（warning 级）。
        # 越界即提示拆分，不做注册阻断（触碰 §9.2 内容域，硬阻断留待人工决策）。
        files_edit = [
            f for f in (t.get("files_to_edit") or []) if isinstance(f, str)
        ]
        if len(files_edit) > _GRANULARITY_MAX_FILES:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].files_to_edit",
                    message=
                    (f"task '{tid}' files_to_edit 数量 {len(files_edit)} 超过粒度锚点 "
                     f"{_GRANULARITY_MAX_FILES}（建议拆分，warning 不阻断）"),
                ))
        # E038（task-decl-consistency-error-code）：brief 的数量口径
        # 「files_to_edit 控 N」与实际声明数不一致 → warning（不阻断，**非拆分建议**）。
        # 专用码从 E029 拆出，避免与「粒度越界/建议拆分」语义串味。
        brief_text = t.get("brief")
        if isinstance(brief_text, str) and brief_text:
            count_m = _DECL_COUNT_RE.search(brief_text)
            if count_m and int(count_m.group(1)) != len(files_edit):
                errors.append(
                    ValidationError(
                        code=ErrorCode.E038,
                        path=f"$.tasks[{i}].brief",
                        message=(
                            f"task '{tid}' brief 声明的 files_to_edit 数量口径 "
                            f"{count_m.group(1)} 与实际声明 {len(files_edit)} 不一致"
                            "（warning 不阻断；此处非拆分建议：请对齐 brief 文案与声明集合）"),
                    ))

        est_hours = t.get("estimated_hours")
        if isinstance(est_hours,
                      (int, float)) and est_hours > _GRANULARITY_MAX_HOURS:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].estimated_hours",
                    message=(
                        f"task '{tid}' estimated_hours {est_hours} 超过粒度锚点 "
                        f"{_GRANULARITY_MAX_HOURS}（建议拆分，warning 不阻断）"),
                ))
        ac_list2 = [
            a for a in (t.get("acceptance_criteria") or [])
            if isinstance(a, str)
        ]
        if len(ac_list2) > _GRANULARITY_MAX_AC:
            errors.append(
                ValidationError(
                    code=ErrorCode.E029,
                    path=f"$.tasks[{i}].acceptance_criteria",
                    message=
                    (f"task '{tid}' acceptance_criteria 数量 {len(ac_list2)} 超过粒度锚点 "
                     f"{_GRANULARITY_MAX_AC}（建议拆分，warning 不阻断）"),
                ))

        # E023: acceptance_criteria 模糊词检测
        ac_list = t.get("acceptance_criteria", [])
        for j, ac in enumerate(ac_list):
            if not isinstance(ac, str):
                continue
            # 检查白名单豁免
            if any(phrase in ac for phrase in _VAGUE_WORDS_WHITELIST):
                continue
            # 检查模糊词
            for vague in _VAGUE_WORDS:
                if vague in ac:
                    errors.append(
                        ValidationError(
                            code=ErrorCode.E023,
                            path=f"$.tasks[{i}].acceptance_criteria[{j}]",
                            message=
                            f"task '{tid}' acceptance_criteria[{j}] contains vague term '{vague}' (use quantifiable criteria)",
                        ))
                    break  # 一条 AC 只报一次
            else:
                # 无模糊词命中 → 结构术语可解析性（task-ac-term-resolvability，
                # 并入 E023 家族：同一条 AC 只报一次，去重不叠加）。
                for _term in _term_candidates.get((i, j), []):
                    if _term in _STRUCTURAL_TERM_ALLOWLIST:
                        continue
                    if _term in _corpus():
                        continue
                    errors.append(
                        ValidationError(
                            code=ErrorCode.E023,
                            path=f"$.tasks[{i}].acceptance_criteria[{j}]",
                            message=(
                                f"task '{tid}' acceptance_criteria[{j}] contains "
                                f"undefined structural term '{_term}' (no definition "
                                "found in repo docs; define it or register in "
                                "shared/conventions.md glossary)"
                            ),
                        ))
                    break

        # E040（task-ac-verify-coverage）：AC 若声称「零残留 / 全仓」可判定性质，
        # 则 verify_command 必须真有对应检查手段（grep 族；全仓声称要求 repo 级
        # grep，文件级 grep 不算覆盖）——否则声称与验证脱节。warning 不阻断；
        # 终态由调用方经 TERMINAL_EXEMPT_QUALITY_CODES / filter_terminal_quality_warnings
        # 豁免（与 E023/E029/E037/E038 同口径）。
        if verify_cmd and not _verify_has_run_ref(verify_cmd):
            for j, ac in enumerate(ac_list):
                claimed, repo_wide = _ac_repo_claim(ac)
                if not claimed:
                    continue
                covered = (
                    _verify_has_repo_grep(verify_cmd)
                    if repo_wide else _verify_has_grep(verify_cmd)
                )
                if covered:
                    continue
                need = (
                    "请补 repo 级 grep（如 grep -r / rg / git grep）到 verify_command"
                    if repo_wide
                    else "请补 grep 族检查到 verify_command"
                )
                errors.append(
                    ValidationError(
                        code=ErrorCode.E040,
                        path=f"$.tasks[{i}].acceptance_criteria[{j}]",
                        message=(
                            f"task '{tid}' acceptance_criteria[{j}] 声称"
                            f"「{'全仓' if repo_wide else ''}零残留 / 全仓可判定」性质，"
                            f"但 verify_command 缺乏对应覆盖：{need}；"
                            "或把该 AC 降级为文件级 / manual 并明确标注"
                        ),
                    ))

        # E024: verify_command 含 pytest 但缺 --basetemp（沙箱坑，warning）
        # 2026-08-06 实踩 3 例：pytest 默认落 C:\Temp 触发 SAFE_DELETE_BULK_CONFIRM_REQUIRED → E014
        # 2026-08-08 精确化：仅匹配"真正执行 pytest 子进程"的命令段
        # （python -m pytest / pytest 命令行），python -c 内容断言（字符串含
        # pytest 字样但不跑 pytest）不再命中。
        if verify_cmd and _runs_pytest(
                verify_cmd) and "--basetemp" not in verify_cmd:
            errors.append(
                ValidationError(
                    code=ErrorCode.E024,
                    path=f"$.tasks[{i}].verify_command",
                    message=
                    (f"task '{tid}' verify_command 含 pytest 但缺 --basetemp"
                     "（pytest 默认落系统 Temp 触发沙箱拦截 → done E014；"
                     "按 SKILL.md 自检约定加 --basetemp=\"${TMPDIR:-/tmp}/orchd-vf-$$\"）"
                     ),
                ))

        # E027: verify_command 不安全/不兼容（warning，amend 注册点阻断）
        # 2026-08-08 实踩 task-release-pipeline 三类：
        #   a) cmd 不兼容分隔符（; 或 2>&1;）——Windows cmd shell=True 下 ; 非分隔符
        #   b) 重命令段（python -m build / pip install / venv / 全量 pytest 无 -k/-p）
        #      ——引擎 verify 上限 120s，build+venv 段实测 144.7s 超时
        #   c) 嵌套 python -c "..."——JSON→cmd→shell 三层转义易失效 SyntaxError
        if verify_cmd:
            unsafe_reasons = _verify_unsafe_reasons(verify_cmd)
            # 2026-08-12（task-cross-platform-validation）：--basetemp 路径平台性校验。
            # 与 E027 同源（不安全/不兼容），计入 unsafe_reasons 一并上报。
            unsafe_reasons += _basetemp_platform_issues(verify_cmd)
            # F6（task-validate-terminal-quiet）：终态任务跳过 E027。
            if unsafe_reasons and not terminal:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E027,
                        path=f"$.tasks[{i}].verify_command",
                        message=(f"task '{tid}' verify_command 含不安全/不兼容段"
                                 f"（{'；'.join(unsafe_reasons)}）"),
                    ))

            # E037（task-decl-consistency-error-code）：声明口径一致性——
            # verify_command 引用的仓库内路径必须 ∈ files_to_edit ∪ exempt_files，或
            # 磁盘已存在。两者皆不满足时 verify 在 done 期必然失败（引用已删除/拼错的
            # 文件），属**确定性错误**，注册点即拒。终态任务由调用方按
            # TERMINAL_EXEMPT_QUALITY_CODES 豁免。
            declared_paths = {
                f
                for f in (t.get("files_to_edit") or []) if isinstance(f, str)
            } | {
                f
                for f in (t.get("exempt_files") or []) if isinstance(f, str)
            }
            missing_refs = sorted({
                tok
                for tok in _verify_command_path_tokens(verify_cmd)
                if tok not in declared_paths and not (project_root /
                                                      tok).exists()
            })
            if missing_refs:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E037,
                        path=f"$.tasks[{i}].verify_command",
                        message=(
                            f"task '{tid}' verify_command 引用的路径既未声明、也不存在："
                            f"{missing_refs}（声明口径一致性：请将路径加入 files_to_edit / "
                            "exempt_files，或把 verify_command 改指向实际存在的文件）"),
                    ))

        # E026: 引擎源码变更但对应测试未声明（warning，intake 期预警）
        # 2026-08-08 实踩：errors.py 新增错误码必然连带 tests/test_errors.py 计数断言，
        # 但该文件不在 files_to_edit 被 E020 拦截——声明 exempt_files 或加入 files_to_edit 即消除。
        # task-decl-concession-autoregister：同名测试推导收敛到单一来源
        # derive_related_test_file（与 done 越界分诊 is_concession_file 共用，防双写漂移）。
        # task-roadmap-section-parse-fix（AC6）：存在性兜底——tests/ 下实际不存在的
        # 派生测试文件（如 orchd/gitops_ops.py → tests/test_gitops_ops.py 不存在）不产生
        # 无法满足的预警（E026 跳过）；tests/ 目录缺失时同样跳过（无从验证即不预警）。
        files_edit = [
            f for f in (t.get("files_to_edit") or []) if isinstance(f, str)
        ]
        exempts = [
            f for f in (t.get("exempt_files") or []) if isinstance(f, str)
        ]
        tests_root = _tests_root_from_master(master)
        # F6（task-validate-terminal-quiet）：终态任务跳过 E026。
        # 其余终态豁免码（E023/E029/E037/E038）口径不变，仍由调用方过滤。
        if terminal:
            continue
        for fe in files_edit:
            # task-e026-related-test-candidates：同 stem 全部候选逐一预警——
            # 任一未声明即一条 warning（control.py 域声明 test_cli_control.py
            # 后仍提示 test_control.py），同域多候选不再漏报。
            for expect_test in derive_related_test_files(fe, tests_root):
                if (expect_test not in files_edit
                        and expect_test not in exempts
                        and any(f.startswith("tests/") for f in files_edit)):
                    errors.append(
                        ValidationError(
                            code=ErrorCode.E026,
                            path=f"$.tasks[{i}].exempt_files",
                            message=(
                                f"task '{tid}' 修改 {fe} 但对应测试 {expect_test} 未在 "
                                "files_to_edit 或 exempt_files 声明（必要连带文件须声明，"
                                "否则 E020 hook 会拦截）"),
                        ))

    return errors


# 终态任务质量告警豁免口径（task-amend-quality-warning-terminal-filter，单一真源）：
# 质量类告警 E023/E026/E027/E029/E037/E038/E040 对终态（completed/cancelled）任务
# 一律豁免——拆分/改写终态任务定义无意义（E007 终态保护无法改写）。validate 与
# amend 共用；E022（代码类缺 verify_command 注册阻断）与 E024 不在豁免之列。
TERMINAL_EXEMPT_QUALITY_CODES = frozenset({"E023", "E026", "E027", "E029", "E037", "E038", "E040"})


def _quality_warning_code_name(w: Any) -> str:
    """质量告警条目的错误码名（兼容 ValidationError 与 dict 两种形态）。"""
    code = w.code if hasattr(w, "code") else w.get("code")
    name = code.name if hasattr(code, "name") else str(code)
    return name.split(".")[-1]


def _quality_warning_task_index(path: Any) -> int | None:
    """告警 path（$.tasks[i]…）的任务下标；非任务级路径返回 None。"""
    if not isinstance(path, str) or not path.startswith("$.tasks["):
        return None
    head, sep, _ = path[len("$.tasks["):].partition("]")
    return int(head) if sep and head.isdigit() else None


def filter_terminal_quality_warnings(
    warnings: list[Any],
    tasks: list[dict[str, Any]],
    terminal_ids: set[str] | None,
) -> tuple[list[Any], int]:
    """过滤终态任务的质量类告警（validate/amend 共用，task-amend-quality-warning-terminal-filter）。

    Args:
        warnings: ValidationError 列表或同形 dict 列表（{"code", "path"}，
            code 接受 "E026" 或 "ErrorCode.E026" 两种形态）。
        tasks: path 下标对应的任务定义列表（顺序须与判据产出一致，
            即 master.tasks 顺序）。
        terminal_ids: 终态任务 id 集合；为 None 时（ledger 不可用/replay 失败）
            不过滤（回退现行为）。

    Returns:
        (kept, exempted_count)：保留条目与本次豁免条数（调用方负责可见输出，
        不得静默吞掉计数）。
    """
    if terminal_ids is None:
        return list(warnings), 0
    kept: list[Any] = []
    exempted = 0
    for w in warnings:
        if _quality_warning_code_name(w) not in TERMINAL_EXEMPT_QUALITY_CODES:
            kept.append(w)
            continue
        path = w.path if hasattr(
            w, "path") else (w.get("path") if isinstance(w, dict) else None)
        idx = _quality_warning_task_index(path)
        tid = tasks[idx].get(
            "id") if idx is not None and 0 <= idx < len(tasks) else None
        if tid is not None and tid in terminal_ids:
            exempted += 1
            continue
        kept.append(w)
    return kept, exempted
