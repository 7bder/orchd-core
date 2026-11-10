"""gitops hook 域：运行时可用性探测与安装侧 fail-closed 守卫（T18 从 install 纯搬移）。

域划分：本域收敛「hook 能否被执行」与「探测失败时如何拒绝/留痕」的全部判据——
运行时探测结果的结构化构造（:func:`_runtime_unavailable_result`）、写盘前双检
（:func:`_write_runtime_gate`）、写盘前生成物完整性自检
（:func:`_validate_hook_content`）、stderr 留痕（:func:`_log_hook_skip`）、
owner-aware 宿主 hook 探测（:func:`_is_orchd_hook`）与安装期解释器/源码根烘焙
（:func:`_baked_hook_endpoints`）。

搬移纪律（task-v163-pkg-registry-convergence）：函数体与常量**逐字未改**，仅改变
落点；``install`` 域保留同名导入以维持既有导入路径与补丁面（``hook.install.<名>``、
聚合入口重导出）可解析。写盘守卫 ``_precommit_write_guard`` 仍留在 install 域——
它装饰 ``_write_precommit_hook``，装饰器返回的包装函数其 ``__globals__`` 必须指向
install（``tests/test_pkg_contract.py::test_install_holds_its_own_bindings`` 判据）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


from .content import HOOK_RUNTIME_UNAVAILABLE, probe_hook_runtime
from .paths import (
    _HOOK_FILENAME,
    _acquire_hook_lock,
    clear_hook_runtime_degraded,
)
from .templates import _HOOK_MARKER, _HOOK_MARKER_SCAN_LINES, _HOOK_SHEBANG


ORCHD_ALLOW_HOOK_DEGRADED_ENV = "ORCHD_ALLOW_HOOK_DEGRADED"


def _runtime_unavailable_result(
    probe: dict[str, Any], *, path: Path | None = None,
) -> dict[str, Any]:
    """构造 hook 运行时不可用的结构化结果（安装侧 fail-closed）。"""
    result: dict[str, Any] = {
        "installed": False,
        "reason": HOOK_RUNTIME_UNAVAILABLE,
        "shell": probe.get("shell"),
        "error": probe.get("error", ""),
        "hint": (
            "hook 依赖 sh 执行（sh -c 'exit 0' 与 sh -n 双检）：请安装 sh / Git Bash "
            "并确保其在 PATH，或设置 ORCHD_HOOK_SHELL 指向可用的 sh；"
            "未安装 hook 前不会写入任何 hook 文件（fail-closed）"
        ),
    }
    if path is not None:
        result["path"] = str(path)
    return result


def _write_runtime_gate(
    hook_content: str, hook_path: Path,
) -> dict[str, Any] | None:
    """写盘前的运行时双检；不可用返回结构化结果，通过返回 None。"""
    probe = probe_hook_runtime(hook_content)
    if probe.get("ok"):
        return None
    result = _runtime_unavailable_result(probe, path=hook_path)
    _log_hook_skip("install_rejected_runtime_unavailable", result)
    return result


def _is_orchd_hook(hook_path: Path) -> bool:
    """目标 pre-commit 是否由本引擎生成（首部含 orchd 归属标记，owner-aware）。

    只扫首部 ``_HOOK_MARKER_SCAN_LINES`` 行；读取失败（权限 / 二进制 / 编码）
    一律返回 False——保守方向是「不动宿主文件」，宁可跳过安装也不覆盖未知文件。

    Args:
        hook_path: pre-commit 文件路径。

    Returns:
        True = 本引擎生成物（可安全覆盖 / 删除）。
    """
    try:
        text = hook_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    head = "\n".join(text.splitlines()[:_HOOK_MARKER_SCAN_LINES])
    return _HOOK_MARKER in head


def _validate_hook_content(content: str) -> dict[str, Any]:
    """生成物完整性自检：写盘前拒绝 / 写盘后回读共用的**唯一形态判据**。

    事故背景（task-hook-staged-nul-rootfix）：f-string 内 ``tr '\\0' '\\n'`` 未转义
    → 生成物被写入**真实** NUL / 换行字符；而写入路径只强制 LF（``newline="\\n"``）、
    无内容自检，缺陷只能靠测试侧 ``bash -n`` 事后暴露。本函数把「生成物必须满足的
    形态约束」收敛为写盘前的单一判据：不通过即拒绝写盘，**不落半截生成物、不覆盖
    已有 hook**（宿主自有 hook 的完整性优先）。

    检查项（任一不满足即判失败）：

    - **NUL 控制字符**（``\\x00``：f-string 转义事故的典型产物）；
    - **CR**（``\\r``：hook 须纯 LF，CR 会污染 here-doc 终止符与路径比较）；
    - **shebang 首行**（缺失 → git 无法执行该 hook，等于拦截能力静默失效）；
    - **归属标记在首部**（owner-aware 判据，缺失会让引擎无法识别自己的生成物，
      卸载时不敢删、安装时被误判为宿主 hook）。

    Args:
        content: 待写入（或已写入后回读）的 hook 脚本文本。

    Returns:
        ``{"ok": True}`` 通过；``{"ok": False, "reason": <str>, "error": <str>}``
        拒绝，``reason`` 取值：``empty_content`` / ``nul_byte`` / ``cr_present`` /
        ``missing_shebang`` / ``missing_marker``。
    """
    if not content:
        return {"ok": False, "reason": "empty_content",
                "error": "生成物为空：拒绝写入 pre-commit hook"}
    if "\x00" in content:
        return {"ok": False, "reason": "nul_byte",
                "error": "生成物含 NUL 控制字符（典型为 f-string 内 \\0 被真实转义）"}
    if "\r" in content:
        return {"ok": False, "reason": "cr_present",
                "error": "生成物含 CR（hook 须纯 LF，CR 会污染 here-doc 终止符与路径比较）"}
    lines = content.splitlines()
    if not lines[0].startswith("#!"):
        return {"ok": False, "reason": "missing_shebang",
                "error": f"生成物首行缺失 shebang（须为 {_HOOK_SHEBANG}）"}
    if _HOOK_MARKER not in "\n".join(lines[:_HOOK_MARKER_SCAN_LINES]):
        return {
            "ok": False,
            "reason": "missing_marker",
            "error": (
                f"生成物首部 {_HOOK_MARKER_SCAN_LINES} 行内缺失归属标记"
                f"（{_HOOK_MARKER}）"
            ),
        }
    return {"ok": True}


def _log_hook_skip(action: str, payload: dict[str, Any]) -> None:
    """把 hook 跳过 / 拒绝动作写 stderr 留痕（``orchd ▸ [hook]`` 前缀，best-effort）。

    与 ``worktree._log_recycle`` 同型：结构化 JSON 单行、任何异常静默不阻断主流程。
    差异：**不受 ORCHD_QUIET 抑制**——回收留痕是常规噪声，而「因宿主 hook 存在而
    跳过安装」是必须可追溯的安全决策（R2-3 的静默覆盖正是漏掉了这层留痕）；唯一
    调用方 ``onboard/claim.py::claim`` 目前丢弃 hook_install 返回值，故在此落一条
    stderr 审计行，避免跳过被静默吞掉。
    """
    # 跨 mypy 版本免疫：getattr 取回 Any，无需 type-ignore（旧 ignore 码
    # [attr-defined] 在新版报 union-attr，pass8 F3）；缺失/异常仍静默跳过。
    _reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        record = {"action": action, **payload}
        print(f"orchd ▸ [hook] {json.dumps(record, ensure_ascii=False)}", file=sys.stderr)
    except Exception:
        pass


def _baked_hook_endpoints(project_root: Path) -> tuple[str | None, str | None]:
    """安装期固化 hook 的解释器与引擎源码根（task-ref-tx-hook-cost）。

    运行期钩子每次 checkout 都重走「解释器探测 + 嵌套 ``git rev-parse``」，
    在 MSYS 祖先链下被放大 35–90 倍。安装期 ``project_root`` 已知，一次性
    解析并烘焙进脚本，运行期零探测直达执行；固化路径失效时回退既有探针。

    Returns:
        ``(python_exe, src_dir)``；任一不可用即 ``(None, None)``（= 不烘焙，
        生成物与旧版逐字一致）。
    """
    try:
        py = sys.executable
        if not py:
            return None, None
        from orchd.gitops._run import _run_git

        top = _run_git(project_root, ["rev-parse", "--show-toplevel"])
        if top.returncode != 0 or not (top.stdout or "").strip():
            return None, None
        root = Path((top.stdout or "").strip())
        module_rel = "orchd/gitops/ref_tx_hook.py"
        if (root / ".orchd" / module_rel).is_file():
            return py, str(root / ".orchd")
        if (root / module_rel).is_file():
            return py, str(root)
    except Exception:
        pass
    return None, None