"""gitops hook 域：pre-commit / reference-transaction hook 的路径、模板与安装。

域：install（W6-C8 从 orchd/gitops/hook.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from orchd.gitops._run import _run_git
from orchd.gitops.cleanup import _safe_delete

from .content import (
    _baked_hook_endpoints,
    _line_ref_tx_env,
    _precommit_hook_content,
    _precommit_hook_params,
    ref_tx_hook_content,
)
from .paths import (
    _HOOK_FILENAME,
    _ensure_hooks_path,
    _get_hook_path,
    _get_hooks_dir,
    _get_ref_tx_hook_path,
)
from .templates import (
    _HOOK_MARKER,
    _HOOK_MARKER_SCAN_LINES,
    _HOOK_SHEBANG,
    _REF_TX_HOOK_MARKER,
)



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



def _write_hook_file(path: Path, content: str) -> None:
    """落盘 hook 脚本（统一 LF + 可执行位）。

    独立成函数的目的：① 写入动作是「自检通过后」的唯一出口，便于审计；
    ② 给落盘层留可替换的接缝（测试可据此模拟截断 / 污染写入，验证回读校验）。
    """
    from orchd.ledger import _write_text_atomic

    _write_text_atomic(path, content)
    path.chmod(0o755)



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



def _install_ref_tx_hook(project_root: Path) -> dict[str, Any]:
    """安装强制层 hook（best-effort；owner-aware：宿主自有同名 hook 不覆盖）。

    与 pre-commit 共用同一 hooks 目录 / hooksPath 解析与形态自检，但不共用文件：
    pre-commit 管「提交范围（E020）」，reference-transaction 管「引用事务（红线 #1/#2）」。
    """
    path = _get_ref_tx_hook_path(project_root)
    if path.exists() and not _is_orchd_hook(path):
        result = {
            "installed": False, "reason": "foreign_hook_present", "path": str(path),
            "hint": (
                f"{path} 已存在且不含 orchd 标记头——判定为宿主自有 hook，未覆盖；"
                "如需红线强制层生效请手动在该 hook 中调用 orchd 校验"
            ),
        }
        _log_hook_skip("install_ref_tx_skipped_foreign_hook", result)
        return result
    line_prefix, line_trunk = _line_ref_tx_env(project_root)
    content = ref_tx_hook_content(
        *_baked_hook_endpoints(project_root),
        line_task_prefix=line_prefix,
        line_trunk=line_trunk,
    )
    check = _validate_hook_content(content)
    if not check["ok"]:
        result = {"installed": False, "reason": "invalid_generated_hook",
                  "path": str(path), "error": check.get("error", "生成物自检未通过")}
        _log_hook_skip("install_ref_tx_rejected_invalid_content", result)
        return result
    try:
        _write_hook_file(path, content)
    except (OSError, IOError) as exc:
        return {"installed": False, "reason": "io_error", "path": str(path),
                "error": str(exc)}
    try:
        written = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"installed": False, "reason": "io_error", "path": str(path),
                "error": str(exc)}
    if _REF_TX_HOOK_MARKER not in written or not _validate_hook_content(written)["ok"]:
        return {"installed": False, "reason": "invalid_written_hook", "path": str(path)}
    installed_result: dict[str, Any] = {"installed": True, "path": str(path)}
    # 烘焙可观测（task-ref-tx-hook-cost）：写盘生成物是否含固化快道。
    installed_result["baked"] = "ORCHD_BAKED_PY=" in written
    return installed_result



def _uninstall_ref_tx_hook(project_root: Path) -> dict[str, Any]:
    """删除强制层 hook（best-effort；owner-aware：只删本引擎生成物）。"""
    path = _get_ref_tx_hook_path(project_root)
    if not path.exists():
        return {"uninstalled": True, "reason": "not_exists", "path": str(path)}
    if not _is_orchd_hook(path):
        return {"uninstalled": False, "reason": "foreign_hook_present", "path": str(path),
                "hint": "不含 orchd 标记头，判定为宿主自有 hook，未删除"}
    try:
        _safe_delete(path, project_root)
        return {"uninstalled": True, "reason": "removed", "path": str(path)}
    except (OSError, IOError) as exc:
        return {"uninstalled": False, "reason": "io_error", "path": str(path),
                "error": str(exc)}



def _write_precommit_hook(
    hooks_dir: Path,
    hooks_path: dict[str, Any],
    hook_content: str,
    project_root: Path,
) -> dict[str, Any]:
    """落盘 pre-commit hook 并安装强制层（原 hook_install 体 L1300-1363，10 条语句）。

    owner-aware 宿主 hook 探测 → 生成物自检 → 原子写盘 → 回读校验 → 强制层安装；
    任一校验失败即结构化返回、不落半截文件（门禁语义与既有测试逐条不变）。
    """
    # 写入 hook 文件（newline="\n"：Windows 文本模式会把脚本写成 CRLF，虽多数
    # MSYS sh 容忍，但 here-doc 终止符 / 静态列表行同样被 CR 污染，属隐患——统一 LF，
    # 与 git 官方对 hook 脚本的可移植性建议一致）。
    hook_path = hooks_dir / _HOOK_FILENAME
    # R2-3 owner-aware：目标 pre-commit 已存在且非本引擎生成（无 orchd 标记头，
    # 典型为宿主自有的 husky / lint-staged hook，或 core.hooksPath 指向用户目录）
    # → 不覆盖、不删除，返回结构化告警；宿主 hook 的完整性与执行权归宿主。
    if hook_path.exists() and not _is_orchd_hook(hook_path):
        result = {
            "installed": False,
            "reason": "foreign_hook_present",
            "path": str(hook_path),
            "hooks_path": hooks_path,
            "hint": (
                f"{hook_path} 已存在且不含 orchd 标记头"
                f"（{_HOOK_MARKER}）——判定为宿主自有 hook，未覆盖；"
                "如需 orchd 越界拦截与之共存，请手动在该 hook 中调用 orchd 校验，"
                f"或先移除/改名该文件再执行 orchd claim"
            ),
        }
        _log_hook_skip("install_skipped_foreign_hook", result)
        return result
    # 生成物完整性自检（task-hook-content-guard）：写盘前统一校验 NUL / CR /
    # shebang / 归属标记，不通过即**拒绝写入**——不落半截生成物、不覆盖已有 hook。
    # 背景：此前写入路径只强制 LF（newline="\n"），缺陷生成物（如 f-string 内
    # `tr '\0' '\n'` 未转义产出的真实控制字符）会一路写到磁盘，只能靠测试侧
    # `bash -n` 事后暴露；宿主 hook 归属判定先于此检查（宿主文件完整性优先）。
    check = _validate_hook_content(hook_content)
    if not check["ok"]:
        result = {
            "installed": False,
            "reason": "invalid_generated_hook",
            "path": str(hook_path),
            "error": check.get("error", "生成物自检未通过"),
            "check": check,
        }
        _log_hook_skip("install_rejected_invalid_content", result)
        return result
    try:
        _write_hook_file(hook_path, hook_content)
    except (OSError, IOError) as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}
    # 写入后内容回读：此前落盘层只有「只读首部归属标记」的 _is_orchd_hook，无内容级
    # 回读——编码 / 换行转换类事故在落盘层即可被拦住，不必等到测试侧语法门禁。
    # read_bytes + decode（不走 read_text 的通用换行翻译）才能如实反映 CR 残留。
    try:
        written = hook_path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"installed": False, "reason": "io_error", "error": str(exc)}
    written_check = _validate_hook_content(written)
    if not written_check["ok"]:
        result = {
            "installed": False,
            "reason": "invalid_written_hook",
            "path": str(hook_path),
            "error": written_check.get("error", "落盘内容与生成物不一致"),
            "check": written_check,
        }
        _log_hook_skip("install_rejected_written_content", result)
        return result
    # 提交层写盘成功后才安装强制层（拒绝路径不落盘任何文件）
    ref_tx = _install_ref_tx_hook(project_root)
    return {"installed": True, "path": str(hook_path), "hooks_path": hooks_path,
            "ref_tx_hook": ref_tx}



def hook_install(
    project_root: Path,
    task_id: str,
    files_to_edit: list[str],
    exempt_files: list[str] | None = None,
) -> dict[str, Any]:
    """安装 pre-commit hook（任务活跃期越界提交拦截，运行时动态识别当前任务）。

    task-precommit-hook-multitask：多任务并行时，``core.hooksPath`` 指向全仓共享目录
    （如 ``main/.githooks``），只有最后安装的那一份生效；旧实现的允许列表是**生成时
    静态烘死**的单任务快照，导致正在实现中的任务越界提交不被拦。本实现改为：

    - **正常路径（当前分支为 ``task/<id>``）**：hook 运行时由分支名解析任务 id，再从
      canonical 主工作树的 ``.orchd/_master.json`` 动态读取该任务的
      ``files_to_edit ∪ exempt_files``（**不再依赖生成时快照**）；
    - **无匹配分支**（非 ``task/<id>``，含非 git / detached HEAD）：回退到本 hook 安装
      时绑定的任务与静态列表（**既有 best-effort 语义，零回归**），并输出 stderr 留痕；
    - **动态读取失败**（master 缺失 / JSON 解析失败 / python 不可用）：输出 stderr 诊断
      并放行（**禁止静默放行**），不阻断非归属提交。

    保留既有三步语义不变：
    - ① 任务未活跃（该 task_id 最近事件为 CLAIMED / REVIEW_CLAIMED，且无后续
      DONE / RETRACT / REVIEW_SUBMITTED）→ 放行（exit 0）；
    - ② R1-b 审查期实现者冻结：任务分支上 REVIEW_CLAIMED 且无后续结论 → 拒绝（E017）；
    - ③ 越界文件 → 拒绝（E020）并输出逃生指引。
    固定资产豁免（``.orchd/_master.json`` / ``IDEAS.md`` / ``.orchd/IDEAS.md`` /
    ``ROADMAP.md`` / ``.orchd/_full_regression.json``）语义不变；``--no-verify`` 可绕过。
    注：``.orchd/ROADMAP.md`` 已非合法形态（ROADMAP 唯一源为宿主项目根，见
    ``orchd/ledger.py::resolve_roadmap_path``），故从豁免清单移除，避免把残留副本当
    合法固定资产放行。

    安装前先调用 :func:`_ensure_hooks_path` 确保仓库自带 ``.githooks/`` 已启用
    （``core.hooksPath`` 是本地配置，不随 clone 传播）：仓库内存在 ``.githooks/``
    且配置未设置时，pre-commit 会落到该目录而非回退 ``.git/hooks``；既有 hooksPath
    解析语义（相对 / 绝对 / 缺省回退）不变。

    owner-aware（task-hook-owner-aware-install）：``core.hooksPath`` 可能指向宿主
    自定义目录（``.husky`` / ``lint-staged`` 等）——此时 ``hooks_dir/pre-commit``
    是宿主自有文件。写入前用 :func:`_is_orchd_hook` 探测：非本引擎生成物一律跳过
    （不写不删）并返回结构化告警 + stderr 留痕，避免 claim 静默覆盖宿主 hook；
    本引擎生成物（含标记头）照旧覆盖安装（重装幂等语义不变）。

    Args:
        project_root: git 仓库根目录。
        task_id: 安装（绑定）时的任务 ID（无 task/<id> 分支时作为回退）。
        files_to_edit: 绑定任务允许修改的文件列表（回退用；正常路径走动态解析）。
        exempt_files: 绑定任务豁免文件列表（回退用）。

    Returns:
        结构化结果，永不抛异常：
            - ``{"installed": True, "path": <str>, "hooks_path": <dict>}`` hook
              安装成功（``hooks_path`` 为 :func:`_ensure_hooks_path` 的处置结果，
              供审计：set / already_set / custom_hooks_path / hooks_dir_missing …）。
            - ``{"installed": False, "reason": "not_a_git_repo"}`` 非 git 仓库。
            - ``{"installed": False, "reason": "io_error", "error": <str>}``
              写入失败（best-effort 降级）。
            - ``{"installed": False, "reason": "unsafe_task_id", "error": <str>}``
              task_id 含 shell 元字符（注入面），拒绝写入 hook。
            - ``{"installed": False, "reason": "foreign_hook_present", "path": <str>,
              "hooks_path": <dict>, "hint": <str>}`` 目标 pre-commit 已存在且不含
              orchd 标记头（宿主自有 hook）→ 不覆盖不删除，附原因与目标路径。
            - ``{"installed": False, "reason": "invalid_generated_hook", "path": <str>,
              "error": <str>, "check": <dict>}`` 生成物自检未通过（含 NUL / CR /
              缺 shebang / 缺归属标记）→ **拒绝写入**：不落半截文件、不动已有 hook。
            - ``{"installed": False, "reason": "invalid_written_hook", "path": <str>,
              "error": <str>, "check": <dict>}`` 落盘后回读内容与生成物不一致
              （写入层污染 / 截断）→ 结构化报错（判据与写盘前自检同源）。
    """
    # P1-5 安全加固：task_id 会被裸插值进 shell hook（grep/echo），须严格白名单，
    # 否则单引号/`$(...)`/换行可逃逸出 shell 引号 → 任意命令注入。
    if not task_id or any(not (c.isalnum() or c in "-_") for c in task_id):
        return {
            "installed": False,
            "reason": "unsafe_task_id",
            "error": "task_id 含非 [A-Za-z0-9_-] 字符，拒绝写入 pre-commit hook（防 shell 注入）",
        }

    # 安装期确保仓库自带 hooks 目录已启用（core.hooksPath 不随 clone 传播）：
    # 先于 hooks_dir 解析执行，使本次安装即落到正确目录；best-effort，不影响主流程。
    hooks_path = _ensure_hooks_path(project_root)

    hooks_dir = _get_hooks_dir(project_root)
    if not (project_root / ".git").exists():
        return {"installed": False, "reason": "not_a_git_repo"}

    # 强制层（task-ref-tx-hook）：与 pre-commit 同时安装——pre-commit 管「提交范围
    # （E020）」，reference-transaction 管「引用事务（红线 #1/#2 强制拦截）」。两者
    # 独立文件、独立 owner-aware 判定。安装时机放在 pre-commit 校验/写盘**之后**：
    # 提交层生成物不合格时整轮安装即失败，此时不应额外落盘强制层文件（保持
    # 「拒绝即不写任何文件」的既有语义，见 test_hook_content_guard）。

    exempt = exempt_files or []

    # 模板渲染与落盘自检拆为两个模块级段函数（F1/F2 同族形态）：
    # 烘焙块 → 模板渲染 → 落盘/自检/强制层；调用顺序与起点逐条一致。
    hook_content = _precommit_hook_content(
        **_precommit_hook_params(project_root, task_id, files_to_edit, exempt))
    return _write_precommit_hook(hooks_dir, hooks_path, hook_content, project_root)




def hook_uninstall(project_root: Path) -> dict[str, Any]:
    """删除 pre-commit hook（best-effort，owner-aware：只删本引擎生成物）。

    Args:
        project_root: git 仓库根目录。

    Returns:
        结构化结果，永不抛异常：
            - ``{"uninstalled": True, "reason": "removed"}`` hook 已删除。
            - ``{"uninstalled": True, "reason": "not_exists"}`` hook 本就不存在（幂等）。
            - ``{"uninstalled": False, "reason": "foreign_hook_present", "path": <str>,
              "hint": <str>}`` 目标 pre-commit 不含 orchd 标记头（宿主自有 hook）
              → **不删除**，附不删原因（保护宿主文件）。
            - ``{"uninstalled": False, "reason": "io_error", "error": <str>}``
              删除失败（best-effort 降级）。
    """
    # 强制层与提交层同生命周期卸载（各自 owner-aware，互不波及宿主文件）
    ref_tx = _uninstall_ref_tx_hook(project_root)
    hook_path = _get_hook_path(project_root)
    if not hook_path.exists():
        return {"uninstalled": True, "reason": "not_exists", "ref_tx_hook": ref_tx}
    # R2-3 owner-aware：只删除本引擎生成物（含 orchd 标记头）。宿主自有 hook
    # （husky / lint-staged / 手写）一律不动，返回不删原因。
    if not _is_orchd_hook(hook_path):
        result = {
            "uninstalled": False,
            "reason": "foreign_hook_present",
            "path": str(hook_path),
            "hint": (
                f"{hook_path} 不含 orchd 标记头（{_HOOK_MARKER}）——判定为宿主自有 "
                "hook，未删除（保护宿主文件）"
            ),
        }
        return result
    try:
        _safe_delete(hook_path, project_root)
        return {"uninstalled": True, "reason": "removed", "ref_tx_hook": ref_tx}
    except (OSError, IOError) as exc:
        return {"uninstalled": False, "reason": "io_error", "error": str(exc),
                "ref_tx_hook": ref_tx}
