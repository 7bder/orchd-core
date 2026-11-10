"""契约文档生成器（随引擎包发布）。

实现位于 :mod:`orchd.docsgen.generators`；``scripts/gen_contract_docs.py`` 仅保留
兼容入口。宿主安装后可直接 ``python -m orchd.docsgen --write <out_dir>``。
"""

from .generators import (
    GENERATORS,
    ROOT,
    _FILE_BUDGET,
    _layer_counts,
    _owner_of,
    _targets_of,
    gen_command_table_md,
    gen_compat_gates_md,
    gen_constraints_md,
    gen_deps_summary_md,
    gen_drift_matrix_md,
    gen_invariants_md,
    gen_commands_md,
    gen_dependency_graph_md,
    gen_error_codes_md,
    gen_events_md,
    gen_fields_md,
    gen_statemachine_md,
    main,
    write_docs,
)

__all__ = (
    "GENERATORS",
    "ROOT",
    "gen_command_table_md",
    "gen_compat_gates_md",
    "gen_constraints_md",
    "gen_deps_summary_md",
    "gen_drift_matrix_md",
    "gen_invariants_md",
    "gen_commands_md",
    "gen_dependency_graph_md",
    "gen_error_codes_md",
    "gen_events_md",
    "gen_fields_md",
    "gen_statemachine_md",
    "main",
    "write_docs",
)
