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

    区分两类 dry-run 失败：
    - ``assertion_mismatch``：断言应匹配现有文件而失败（如 pytest 收集到
      现有测试文件但断言失败、exit 4 语法错误、引用不存在文件）→ 阻断注册
      （E028，verify_command 定义可能有误）。
    - ``expected_pending``：依赖实现产物、预期失败（如测试文件尚未由实现者
      创建、断言引用的实现文件不存在）→ 仅提示不阻断。
    - ``no_tests_collected``（task-pass9-verify-path-guard）：exit 5 / 输出含
      "no tests ran"——addopts 强制 xdist 下 verify_command 参数含不存在路径
      时 usage error 被整吞，合法测试静默不跑。不阻断，但单独可见以提示
      路径笔误核对。

    启发式判定（简单、可测）：
    - exit_code == 4（pytest usage error，cmd 语法错误）→ assertion_mismatch
    - stderr 含 "ERROR"/"error:" 指向现有文件（tests/ 下的收集错误）→ assertion_mismatch
    - stderr 含 "file not found"/"No such file"/"ModuleNotFoundError"
      （引用不存在文件/模块）→ 缺失路径 ∈ to_be_created（本任务即将创建的
      files_to_edit）→ expected_pending；否则 → assertion_mismatch
    - 其余（如测试运行但断言失败、实现未完成）→ expected_pending

    Args:
        verify_cmd: verify_command 定义。
        exit_code: dry-run 子进程退出码。
        stderr: 子进程 stderr。
        stdout: 子进程 stdout（可空）。
        to_be_created: 本任务 files_to_edit 声明（即将创建的路径集合，可空；
            为空时保持存量行为，向后兼容）。

    Returns:
        "assertion_mismatch" 或 "expected_pending"。
    """
    stderr_l = (stderr or "").lower()

    # task-pass9-verify-path-guard（pass9 实测）：exit 5 = pytest "no tests
    # ran"——addopts 强制 xdist（-n auto）下，pytest/ruff 参数含不存在路径时
    # usage error 被整吞（-n0 对照 exit 4 可见），合法测试静默不跑。给专项
    # 分类（不阻断，与 expected_pending 同级），把「路径笔误」从
    # expected_pending 的兜底误分类里拆出来单独可见。
    if exit_code == 5 or "no tests ran" in (
            stderr_l + " " + (stdout or "").lower()):
        return "no_tests_collected"

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
        hay = _collapse(
            (stderr_l + " " + (stdout or "").lower()).replace("\\", "/")
        )
        return any(v in hay for v in variants)

    # pytest usage error（cmd 语法错误）——先做 to_be_created 豁免：
    # pytest 引用『本任务将创建的测试文件』时恒返回 exit 4（ERROR: file or
    # directory not found: tests/test_x.py），属预期失败（expected_pending）
    # 而非断言不匹配；纯语法错误或缺失路径不属待创建时仍阻断（assertion_mismatch）。
    if exit_code == 4:
        if _missing_ref() and _hit_to_be_created():
            return "expected_pending"
        return "assertion_mismatch"

    # 引用不存在文件/模块（收集错误）——缺失路径若属于本任务即将创建的
    # files_to_edit，则为 expected_pending（新增测试文件 + verify 引用它），
    # 否则维持 assertion_mismatch（定义引用了本不该缺失的路径）
    if _missing_ref():
        if _hit_to_be_created():
            return "expected_pending"
        return "assertion_mismatch"

    # pytest 收集阶段错误（ERROR at setup/collection，指向现有测试文件）
    if "error" in stderr_l and ("collect" in stderr_l or "setup" in stderr_l):
        return "assertion_mismatch"

    # 其余失败（测试断言失败、实现未完成等）→ 预期失败，不阻断
    return "expected_pending"
