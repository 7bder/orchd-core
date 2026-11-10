from __future__ import annotations

import re

def _collapse(s: str) -> str:
    """折叠连续斜杠为单个（用于 Windows 转义路径与声明路径的归一化对齐）。"""
    return re.sub(r"/{2,}", "/", s)

def classify_dry_run_failure(
    verify_cmd: str,
    exit_code: int,
    stderr: str,
    stdout: str = "",
    to_be_created: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
) -> str:
    """dry-run 失败分类（L3：注册通道校验，2026-08-08）。

    区分六类 dry-run 失败：
    - ``assertion_mismatch``：引用现有文件但断言失败 / 收集期错误 → 阻断；
    - ``usage_error``（task-feedback-verify-win-shell）：pytest rc=4 纯参数 /
      插件错误（unrecognized arguments、缺 xdist 等）→ 阻断，专属 hint；
    - ``invalid_shell``（同卡）：bash syntax error / unexpected token（常见于
      PowerShell 吃引号导致命令截断，rc=2）→ 阻断，专属 hint；
    - ``internal_error``（task-feedback-dryrun-cmd-internal-error）：pytest
      INTERNALERROR（cmd 通道截断 --basetemp 等，常为 rc=3）→ 阻断；真实引擎/
      参数错误不得伪装成「预期失败」；
    - ``expected_pending``：依赖实现产物、预期失败（测试文件尚未创建等）→ 仅提示；
    - ``no_tests_collected``：exit 5 / "no tests ran"，路径笔误可见化。

    启发式判定（顺序即优先级；task-debug-dryrun-classify-order 重排）：
    1. 缺失路径/模块信号优先——缺失路径 ∈ to_be_created → expected_pending，
       否则 assertion_mismatch；
    2. shell 语法错误信号 → invalid_shell；
    3. pytest INTERNALERROR 信号 → internal_error（rc=3 截断/引擎崩溃，阻断）；
    4. exit_code == 4（纯 usage error，无缺失引用信号）→ usage_error；
    5. exit_code == 5 或输出含 "no tests ran" → no_tests_collected；
    6. 收集 / 装配期错误 → assertion_mismatch；
    7. 其余（测试运行但断言失败、实现未完成）→ expected_pending。

    为何 1 必须排在 3 之前（本卡现场实测）：pytest 在 **exit 4（usage error）** 时
    同样会往 stdout 打 ``no tests ran in 0.00s``；旧顺序先判 3，于是「verify 引用本任务
    待创建文件」这一**预期失败**被误分类成 ``no_tests_collected``，与响应里同时展示的
    ``exit_code: 4`` 自相矛盾，直接把 agent 引向「改 verify_command 定义」的错误动作。

    Args:
        verify_cmd: verify_command 定义。
        exit_code: dry-run 子进程退出码。
        stderr: 子进程 stderr。
        stdout: 子进程 stdout（可空）。
        to_be_created: 本任务 files_to_edit 声明（即将创建的路径集合，可空；
            为空时保持存量行为，向后兼容）。

    Returns:
        ``assertion_mismatch`` / ``usage_error`` / ``invalid_shell`` /
        ``internal_error`` / ``no_tests_collected`` / ``expected_pending``。
    """
    stderr_l = (stderr or "").lower()
    hay = stderr_l + " " + (stdout or "").lower()

    # 缺失路径/模块信号（pytest 收集错误）。pytest 引用不存在测试文件时真实
    # 输出为 "ERROR: file or directory not found"（且 exit_code==4），"file not
    # found" 子串不匹配它，须单独列出（task-e028-dryrun-exit4-priority）。
    _MISSING_REF_SIGNALS = (
        "filenotfounderror", "nosuchfile", "no such file",
        "file not found", "file or directory not found",
        "can't open file", "modulenotfounderror", "cannot import",
    )

    def _missing_ref() -> bool:
        return any(k in stderr_l for k in _MISSING_REF_SIGNALS)

    def _hit_to_be_created() -> bool:
        """缺失路径 ∈ to_be_created（本任务即将创建的 files_to_edit）判定。

        与旧缺失信号分支共用同一套归一化对齐：小写、反斜杠→斜杠、折叠连续
        分隔符 + 模块名变体（tests/test_x.py ↔ tests.test_x）。
        """
        if not to_be_created:
            return False
        declared: set[str] = {
            _collapse(re.sub(r"/{2,}", "/", str(p).lower().replace("\\", "/")))
            for p in to_be_created
        }
        variants = set(declared)
        for p in list(declared):
            if p.endswith(".py"):
                variants.add(p[:-3].replace("/", "."))
            elif "." in p and "/" not in p:
                variants.add(p.replace(".", "/") + ".py")
        hay_norm = _collapse(hay.replace("\\", "/"))
        return any(v in hay_norm for v in variants)

    # 1) 缺失引用优先：exit 4（usage error）也会在 stdout 打 "no tests ran"，
    #    先判 no_tests_collected 会把「引用本任务待创建文件」误报为路径笔误。
    if _missing_ref():
        if _hit_to_be_created():
            return "expected_pending"
        return "assertion_mismatch"

    # 2) shell 语法错误（task-feedback-verify-win-shell）：PowerShell 吃引号后
    #    verify_command 被截断，bash 常以 rc=2 报 syntax error / unexpected token。
    #    这不是「实现未完成」，必须单列 invalid_shell，不能落 expected_pending。
    _shell_syntax = any(sig in hay for sig in (
        "syntax error near unexpected token",
        "syntax error: unexpected",
        "syntax of the command is incorrect",
        "unexpected end of file",
        "unexpected eof",
    ))
    if _shell_syntax and (
        "bash:" in hay or "sh:" in hay
        or "syntax of the command is incorrect" in hay
        or exit_code == 2
    ):
        return "invalid_shell"

    # 3) pytest INTERNALERROR（task-feedback-dryrun-cmd-internal-error）：cmd 通道截断
    #    --basetemp 等会得到 rc=3 + "INTERNALERROR>"（可能伴随 Traceback 而非缺失路径
    #    信号）。这是引擎/参数错误，不是「实现未完成」，必须阻断；旧实现落到
    #    expected_pending 会把真实故障静默放过。只认强信号，避免把任意 rc=3 的
    #    业务脚本误判为 pytest 内部错误。
    if "internalerror" in hay or "internal error" in hay:
        return "internal_error"

    # 4) pytest 纯 usage error（rc=4，无缺失引用信号）：unrecognized arguments /
    #    缺插件 / 参数笔误。旧实现归 assertion_mismatch（“引用现有文件但断言失败”）
    #    会误导排查方向；单列 usage_error 并阻断，与真实断言失败区分。
    if exit_code == 4:
        return "usage_error"

    # 3) task-pass9-verify-path-guard（pass9 实测）：exit 5 = pytest "no tests
    #    ran"——addopts 强制 xdist（-n auto）下，pytest/ruff 参数含不存在路径时
    #    usage error 被整吞（-n0 对照 exit 4 可见），合法测试静默不跑。给专项
    #    分类（不阻断，与 expected_pending 同级），把「路径笔误」从
    #    expected_pending 的兜底误分类里拆出来单独可见。
    if exit_code == 5 or "no tests ran" in hay:
        return "no_tests_collected"

    # pytest 收集阶段错误（ERROR at setup/collection，指向现有测试文件）
    if "error" in stderr_l and ("collect" in stderr_l or "setup" in stderr_l):
        return "assertion_mismatch"

    # 其余失败（测试断言失败、实现未完成等）→ 预期失败，不阻断
    return "expected_pending"
