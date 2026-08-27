"""Orchd 统一错误处理模块。

本模块提供三个核心组件：
- ErrorCode 枚举：定义 E001-E030 + E033-E034 共 32 个错误码（E031/E032/E035 为告警/拒绝码，不入枚举）。
- OrchdError 异常类：携带错误码、人类可读消息及结构化详情的业务异常基类。
- to_json_response 格式化函数：将 OrchdError 转换为 CLI 统一 JSON 错误响应字典。

依赖方向：errors.py 为最底层模块，不导入项目内其他模块。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(Enum):
    """32 个错误码（E001-E030 + E033-E034）。"""

    E001 = "file_not_found"
    E002 = "invalid_json"
    E003 = "schema_validation_failed"
    E004 = "dag_cycle"
    E005 = "reference_not_found"
    E006 = "duplicate_id"
    E007 = "invalid_state"
    E008 = "task_not_ready"
    E009 = "already_claimed"
    E010 = "file_conflict"
    E011 = "agent_busy"
    E012 = "lock_timeout"
    E013 = "not_initialized"
    E014 = "verify_command_failed"
    E015 = "merge_conflict"
    E016 = "self_review_blocked"
    E017 = "dirty_workspace"
    E018 = "wrong_branch"
    E019 = "workspace_busy"
    E020 = "out_of_scope_commit"
    E021 = "identity_mismatch"
    E022 = "missing_verify_command"
    E023 = "vague_acceptance_criteria"
    E024 = "verify_command_missing_basetemp"
    # E025 与 task-source-field-schema 同源（该任务 in_review 未 merge，
    # 本任务为保证码序完整同步补充；merge 时定义一致不冲突）
    E025 = "source_reference_not_found"
    E026 = "unexempted_test_coupling"
    E027 = "verify_command_unsafe"  # 含不安全/不兼容段；2026-08-12 起含 --basetemp 路径非跨平台
    E028 = "dry_run_assertion_mismatch"
    E029 = "granularity_overflow"  # 任务拆解粒度越界（R4，warning 级）
    E030 = "runtime_file_integrity"  # 运行时文件完整性校验失败（红线 8 R3，warning 级）
    E033 = "session_identity_missing"  # 写命令需会话身份，但宿主未注入 ORCHD_SESSION_ID（session-id-fingerprint）
    E034 = "retract_not_authorized"  # 撤认归属守卫：仅事件作者本人或 admin 可撤回，跨 agent 撤认他人事件被拒（task-retract-ownership-guard）


# warning 级错误码（设计 §5）：不阻断操作，默认不触发 lesson 注入，仅 agent 主动打点。
WARNING_CODES = frozenset({
    "E023",  # vague_acceptance_criteria
    "E026",  # unexempted_test_coupling
    "E028",  # dry_run_assertion_mismatch
    "E029",  # granularity_overflow
    "E030",  # runtime_file_integrity
})

# 引擎预判「值得上报」的 warning 码（设计 §5.1 信号 A：suggest_report=true）。
SUGGEST_REPORT_CODES = frozenset({
    "E030",  # runtime_file_integrity：深层问题征兆，允许主动 lesson 沉淀
})


def is_warning_code(code_name: str) -> bool:
    """错误码是否为 warning 级（§5）。"""
    return code_name in WARNING_CODES


def is_suggest_report_code(code_name: str) -> bool:
    """错误码是否引擎预判值得上报（§5.1 信号 A）。"""
    return code_name in SUGGEST_REPORT_CODES


class OrchdError(Exception):
    """Orchd 业务异常。

    Attributes:
        code: ErrorCode 枚举成员。
        message: 人类可读的错误描述。
        details: 结构化上下文列表，每项为 dict（如 {"path": ..., "message": ...}）。

    Example:
        >>> raise OrchdError(
        ...     ErrorCode.E001,
        ...     "找不到任务文件",
        ...     [{"path": "/tasks/t1.json"}],
        ... )
        orchd.errors.OrchdError: [E001] 找不到任务文件
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details: list[dict[str, Any]] = details if details is not None else []
        super().__init__(f"[{code.name}] {message}")


def to_json_response(error: OrchdError) -> dict[str, Any]:
    """将 OrchdError 转换为 CLI 统一 JSON 错误响应格式。

    注意：该函数由 cli.py 统一调用，用于将所有业务异常转换为标准 JSON 输出，
    确保 CLI 的退出码与响应体格式一致。

    设计 §5 扩展：错误响应附加 ``severity``（warning/error）与 ``suggest_report``
    （引擎预判是否值得上报）字段，供 warning 级错误走独立指引、agent 决策是否
    打点 lesson（§5.1 信号 A）。

    返回:
        {"error": {"code": "E003", "message": "...", "details": [...],
                   "severity": "error", "suggest_report": false}}
    """
    code_name = error.code.name
    return {
        "error": {
            "code": code_name,
            "message": error.message,
            "details": error.details,
            "severity": "warning" if is_warning_code(code_name) else "error",
            "suggest_report": is_suggest_report_code(code_name),
        }
    }
