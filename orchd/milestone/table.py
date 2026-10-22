"""MILESTONES 单一真源表（里程碑 → 判据列表），DAG 汇聚点。"""

from __future__ import annotations

from typing import Any

from .context import _Check
from .m1 import (
    _m1_a1_active_clear, _m1_a2_pool_priority_clear, _m1_a3_dep_closure, _m1_b1_full_regression,
    _m1_b2_baseline_fresh, _m1_b3_validate, _m1_b4_merge_audit, _m1_c1_kernel_contract,
    _m1_c2_roadmap_milestones, _m1_d1_gate_self,
)
from .m2 import (
    _m2_a1_line_capability, _m2_b1_line_sync_channel, _m2_c1_release_per_line,
    _m2_d1_drill_artifact, _m2_e1_gate_self, _m2_f1_behavior, _m2_f2_perf, _m2_f3_naming,
    _m2_f4_robustness,
)


MILESTONES: dict[str, dict[str, Any]] = {
    "freeze": {
        "title": "M1 · 单机内核冻结",
        "groups": [
            ("A", "范围", [
                _Check("A1.active_clear", "A", "活跃任务清零",
                       _m1_a1_active_clear),
                _Check("A2.pool_priority_clear", "A", "池内无 P0/P1 待领项",
                       _m1_a2_pool_priority_clear),
                _Check("A3.dep_closure", "A", "依赖闭合", _m1_a3_dep_closure),
            ]),
            ("B", "质量（复用既有门禁）", [
                _Check("B1.full_regression_head", "B", "全量回归覆盖 HEAD",
                       _m1_b1_full_regression),
                _Check("B2.baseline_fresh", "B", "测试基线新鲜且零红",
                       _m1_b2_baseline_fresh),
                _Check("B3.validate_errors_zero", "B", "validate 零错误",
                       _m1_b3_validate),
                _Check("B4.merge_audit_clean", "B", "merge audit 零告警",
                       _m1_b4_merge_audit),
            ]),
            ("C", "契约", [
                _Check("C1.kernel_contract", "C", "冻结契约文档在册",
                       _m1_c1_kernel_contract),
                _Check("C2.roadmap_milestones", "C", "ROADMAP 里程碑节引用本命令",
                       _m1_c2_roadmap_milestones),
            ]),
            ("D", "机制", [
                _Check("D1.gate_self_check", "D", "判据机制化自检", _m1_d1_gate_self),
            ]),
        ],
    },
    "split-ready": {
        "title": "M2 · 分线就绪",
        "groups": [
            ("A", "引擎", [
                _Check("A1.line_capability", "A", "多主干 line 能力", _m2_a1_line_capability),
            ]),
            ("B", "回移", [
                _Check("B1.line_sync_channel", "B", "回移通道 + 留痕",
                       _m2_b1_line_sync_channel),
            ]),
            ("C", "治理", [
                _Check("C1.release_per_line", "C", "发布按线 / 按 tag 归属",
                       _m2_c1_release_per_line),
            ]),
            ("D", "演练", [
                _Check("D1.drill_artifact", "D", "双线并行演练通过",
                       _m2_d1_drill_artifact),
            ]),
            ("E", "机制", [
                _Check("E1.gate_self_check", "E", "判据机制化自检", _m2_e1_gate_self),
            ]),
            ("F", "单机零回归口径", [
                _Check("F1.behavior", "F", "行为：全量回归覆盖 HEAD", _m2_f1_behavior),
                _Check("F2.perf", "F", "性能：预算上限不得上调", _m2_f2_perf),
                _Check("F3.naming", "F", "命名：单线保持 task/{id}", _m2_f3_naming),
                _Check("F4.robustness", "F", "健壮性：演练用例可重放", _m2_f4_robustness),
            ]),
        ],
    },
}
