"""Orchd _master.json 加载、结构校验与跨引用校验（聚合入口）。

W4 起本包按域拆分，本文件只做**重导出**（无任何实现）：
- ``spec/declarations.py``：连带文件自动登记（E026 推导 / 越界分诊白名单 / 目录式声明检出）
- ``spec/schema.py``：JSON Schema 版本解析、加载、校验器构建与缺依赖降级
- ``spec/structure.py``：ValidationError / Master / load_master / 结构校验与阶段值域检查
- ``spec/references.py``：跨引用完整性校验（ID 唯一性 / 引用存在性 / DAG 无环）
- ``spec/quality_terms.py``：模糊词与术语语料、文档/代码任务判定、verify 路径 token、
  AC 覆盖启发式
- ``spec/quality.py``：validate_quality、终态质量告警豁免与过滤
- ``spec/sources.py``：任务来源校验（额外来源 / idea / roadmap 引用）
- ``spec/roadmap.py``：ROADMAP 章节解析与落地/布局告警
- ``spec/verify_cmd.py``：verify_command 安全性与跨平台判定

补丁面口径（实测）：``load_master`` 等由外部按名从 ``orchd.spec`` 导入的入口，打点打在
包属性上即命中（真实消费点是函数级 ``from orchd.spec import ...``）；只被子模块内部消费的
名字（如 ``_build_validator``）查找空间是子模块命名空间，打点键须跟随落点。

依赖方向：spec（聚合入口）→ errors.py / contracts 包 / 各子模块（仅重导出）。

错误码登记（claim 路径引用；本包不抛这些码，登记供调用方与脚本按码检索）：
- E007: invalid_state（状态不合法，如 phase_mismatch / not_designated_reviewer）
- E008: task_not_ready（任务未就绪，如非 pending/in_review 状态）
- E009: already_claimed（任务已被其他 agent 认领）
- E010: file_conflict（文件冲突，与在握任务 files_to_edit 重叠）
- E011: agent_busy（agent 已持有其他任务）
- E016: self_review_blocked（实现者不得审查自己的实现，确保审查独立性）
"""

from __future__ import annotations


from orchd.spec.declarations import (
    derive_related_test_file,
    derive_related_test_files,
    detect_dir_or_glob_declarations,
    is_concession_file,
    is_path_covered,
)

from orchd.spec.schema import (
    _DEFAULT_SCHEMA_PATH,
    _SCHEMA_DIR,
    _build_validator,
    _load_schema,
    _resolve_schema_path,
    jsonschema_available,
    spec_degraded_guard,
)

from orchd.spec.structure import (
    _TASK_PHASE_CONTROL_CHARS,
    Master,
    ValidationError,
    _check_declared_path_control_chars,
    _check_task_phase,
    load_master,
    validate_structure,
)

from orchd.spec.references import (
    validate_references,
)

from orchd.spec.quality_terms import (
    _VAGUE_WORDS_WHITELIST,
    _VAGUE_WORDS,
    _DOC_SUFFIXES,
    _GRANULARITY_MAX_FILES,
    _GRANULARITY_MAX_HOURS,
    _GRANULARITY_MAX_AC,
    _STRUCTURAL_TERM_RE,
    _STRUCTURAL_TERM_ALLOWLIST,
    _TERM_CORPUS_MAX_FILE_BYTES,
    _TERM_CORPUS_MAX_TOTAL_BYTES,
    _TERM_CORPUS_MAX_FILES,
    _TERM_CORPUS_SKIP_DIRS,
    _build_term_corpus,
    _is_doc_task,
    is_code_task,
    _tests_root_from_master,
    _DECL_COUNT_RE,
    _VERIFY_PATH_SUFFIXES,
    _VERIFY_PATH_SKIP_CHARS,
    _project_root_from_master,
    _verify_command_path_tokens,
    _AC_ZERO_RESIDUAL_RE,
    _AC_REPO_WIDE_RE,
    _AC_ALL_TASKS_RE,
    _AC_CHECK_VERB_RE,
    _AC_MANUAL_RE,
    _VERIFY_GREP_FAMILY_RE,
    _VERIFY_GREP_REPO_RE,
    _VERIFY_RUN_REF_RE,
    _ac_repo_claim,
    _verify_has_grep,
    _verify_has_repo_grep,
    _verify_has_run_ref,
)

from orchd.spec.quality import (
    validate_quality,
    TERMINAL_EXEMPT_QUALITY_CODES,
    _quality_warning_code_name,
    _quality_warning_task_index,
    filter_terminal_quality_warnings,
)

from orchd.spec.sources import (
    _validate_additional_sources,
    validate_source,
    _exact_ref_match,
    _check_idea_reference,
    _roadmap_section_headers,
    _check_roadmap_reference,
)

from orchd.spec.roadmap import (
    _parse_roadmap_sections,
    _find_workspace_file,
    roadmap_landing_warnings,
    _section_landed,
    _e031_warning,
    layout_marker_warnings,
)

from orchd.spec.verify_cmd import (
    _CROSS_PLATFORM_BASETEMP,
    _runs_pytest,
    _verify_unsafe_reasons,
    _strip_single_quoted_segments,
    _dangerous_shell_reasons,
    verify_command_dangerous_reasons,
    _basetemp_platform_issues,
)
