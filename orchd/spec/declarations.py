"""orchd/spec/declarations.py — 连带文件自动登记域（W4-S1 拆分，纯搬移）。

来源：``orchd/spec.py`` 起点 84e1a085 的 L43-51（域头注释）+ L54-197（5 个函数），
拆分记录见 design/file-split-program-20261015.md §4.5.1。E026 推导（engine → 对应
测试）与 done 越界分诊（``onboard/lifecycle/guards.py``）共用本域函数为**单一来源**，
禁止在别处另写一份推导（双写漂移）。

依赖方向：declarations.py → 仅标准库（不导入 orchd 内部模块）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


# ------------------------------------------------------------------
# 连带文件自动登记（task-decl-concession-autoregister）：白名单单一来源
# ------------------------------------------------------------------
# E026 推导（engine → 对应测试）与 done 越界分诊（guards.py）共用本函数——
# 禁止在 guards.py 另写一份推导，防止双写漂移。白名单必须窄且单一来源
# （红线 #3「禁改范围外文件」护栏不下降）：仅两类自动登记——
#   ① 同名测试：tests/test_<stem>.py（对应 files_to_edit 中 orchd/<stem>.py）
#   ② docs/*.md（文档类连带）
# 其余越界文件一律高风险类，仍 E010 拒绝、需显式确认。


def derive_related_test_files(
        fe: str,
        tests_root: str | Path | None = None,
) -> list[str]:
    """从引擎源码文件推导**全部**同 stem 测试候选（task-e026-related-test-candidates）。

    ``derive_related_test_file``（单数）的复数底座：候选顺序与单数一致
    （嵌套路径先 ``test_<一级目录>_<stem>.py`` 后 ``test_<stem>.py``）。
    传入 ``tests_root`` 时只保留实际存在的文件（E026 对不存在文件不预警）；
    未传时返回全部字符串候选（done 越界分诊的字符串比较口径）。
    非 orchd/ 源码返回空列表。
    """
    if not (fe.startswith("orchd/") and fe.endswith(".py")):
        return []
    rel = fe[len("orchd/"):-3]  # 如 "errors" / "cli/commands/session"
    parts = rel.split("/")
    stem = parts[-1]
    if len(parts) == 1:
        rel_candidates = [f"test_{stem}.py"]
    else:
        rel_candidates = [
            f"test_{parts[0]}_{stem}.py",
            f"test_{stem}.py",
        ]
    if tests_root is None:
        return [f"tests/{c}" for c in rel_candidates]
    root = Path(tests_root)
    return [f"tests/{c}" for c in rel_candidates if (root / c).is_file()]


def derive_related_test_file(fe: str,
                             tests_root: str | Path | None = None
                             ) -> str | None:
    """从引擎源码文件推导对应测试文件（单一来源，E026 与 done 分诊共用）。

    复数底座 :func:`derive_related_test_files` 的首个候选（顺序与历史一致，
    行为零回归）：``orchd/<stem>.py`` → ``tests/test_<stem>.py``；嵌套路径
    先 ``tests/test_<一级目录>_<stem>.py`` 后 ``tests/test_<stem>.py``。
    非 orchd/ 源码或（传 tests_root 时）无实际存在候选返回 ``None``；
    未传 tests_root 时保持纯字符串推导（guards.py 的 done 越界分诊以字符串
    比较复用本函数）。E026 预警与 done 越界分诊**必须**经复数底座取候选，
    禁止各自实现推导（双写漂移检测见 tests）。
    """
    candidates = derive_related_test_files(fe, tests_root)
    return candidates[0] if candidates else None


def is_concession_file(file: str, files_to_edit: list[str]) -> bool:
    """done 越界分诊白名单判定（单一来源，task-decl-concession-autoregister）。

    返回 ``True`` = 连带类（同 stem 测试任一候选 / docs/*.md）→ 引擎自动登记、done 不阻断；
    ``False`` = 高风险类 → 仍 E010 拒绝并需显式确认。白名单判定规则：
      - 同 stem 测试：``file`` 落在 ``derive_related_test_files(fe)`` 任一候选中
        （经复数底座，与 E026 同一推导）；
      - 文档：``docs/*.md``。
    引擎核心 ``orchd/`` 既有文件、约定文件（``.orchd/SKILL.md`` /
    ``.orchd/shared/conventions.md``）、``.orchd/_master.json``、他人声明或
    在途文件一律不在此列（高风险类）。
    """
    if file.startswith("docs/") and file.endswith(".md"):
        return True
    for fe in files_to_edit:
        # task-e026-related-test-candidates：经复数底座判定（单一来源）——
        # 同 stem 任一候选命中即连带（如 control.py 域的 test_control.py），
        # 不再只认首候选（旧口径漏 test_control.py 致 done E010 误拦实证）。
        if file in derive_related_test_files(fe):
            return True
    return False


def is_path_covered(declared: str, target: str) -> bool:
    """目录式声明覆盖判定（task-decl-dir-notation-guard AC2/AC5）。

    目录式声明（``orchd/cli/``）覆盖其下所有文件（``orchd/cli/foo.py``），
    但**不误覆盖同前缀兄弟目录**（``orchd/cli/`` 不覆盖 ``orchd/cli_extra.py``）。

    判定逻辑：尾斜杠归一后，精确相等直接命中；目录式声明要求 target 以
    ``declared + "/"`` 为前缀（归一后的 declared 不含尾斜杠，加 ``/`` 确保
    目录边界，避免 ``orchd/cli`` 前缀误命中 ``orchd/cli_extra.py``）。
    非目录式声明仅精确相等命中。

    Args:
        declared: 声明路径（如 ``orchd/cli/`` / ``orchd/spec.py``）。
        target: 待判定的目标文件路径（如 ``orchd/cli/foo.py``）。

    Returns:
        True 表示 target 被 declared 覆盖。
    """
    if not isinstance(declared, str) or not isinstance(target, str):
        return False
    d = declared.rstrip("/")
    t = target.rstrip("/")
    if d == t:
        return True
    # 目录式声明：declared 原本以 / 结尾，或归一后 target 以 declared/ 开头
    if declared.endswith("/") and t.startswith(d + "/"):
        return True
    return False


def detect_dir_or_glob_declarations(
        task: dict[str, Any]) -> list[dict[str, str]]:
    """检出任务声明中的目录式/通配符路径（task-decl-dir-notation-guard AC1/AC3）。

    扫描 ``files_to_edit`` 与 ``exempt_files``，返回命中清单。判定：
    - **目录式**：路径以 ``/`` 结尾，或对应路径在项目中实际为目录（``Path.is_dir()``）；
    - **通配符**：路径含 ``*`` 或 ``?``。

    与 :func:`validate_quality` 同源不双写——本函数是唯一检出原语，amend 注册
    门禁与消费点前缀匹配均须复用，禁止各自实现判定。

    Args:
        task: 任务定义 dict。

    Returns:
        ``[{"field", "path", "kind"}]``，kind 为 ``"directory"`` 或 ``"glob"``；
        无命中返回空列表。
    """
    hits: list[dict[str, str]] = []
    for decl_field in ("files_to_edit", "exempt_files"):
        for fp in task.get(decl_field, []) or []:
            if not isinstance(fp, str):
                continue
            if "*" in fp or "?" in fp:
                hits.append({"field": decl_field, "path": fp, "kind": "glob"})
                continue
            if fp.endswith("/"):
                hits.append({
                    "field": decl_field,
                    "path": fp,
                    "kind": "directory"
                })
                continue
            # 实际为目录（如 orchd/cli 无尾斜杠但对应目录存在）
            try:
                if Path(fp).is_dir():
                    hits.append({
                        "field": decl_field,
                        "path": fp,
                        "kind": "directory"
                    })
            except OSError:
                pass
    return hits
