"""orchd/guide/digest.py — 无状态渲染与内容摘要域（W5-G1 拆分，纯搬移）。

来源：``orchd/guide.py`` 起点 e3f21d0b 的 L41-43（_ENTRY_CMD）、L73-100（next_action
词表与 → step 映射）、L103-205（step 映射 / amend 命令渲染 / guidance 分级）、
L570-622（必读面内容摘要）。本域全部是无全局状态、无文件系统路由状态的纯函数与
常量（仅 context_digest 只读取字节）。拆分记录见
design/file-split-program-20261015.md §4.6.1。

依赖方向：仅标准库（hashlib / os / pathlib / typing），不导入任何 orchd 内部模块——
聚合入口从本域重导出，故本域不得反向导入 ``orchd.guide``（否则成加载期环）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any


# orchd 入口命令前缀（零根入口 .orchd/__main__.py，容器布局下从项目根运行）。
# 集中为模块级常量：入口变更时只改此处，避免 20+ 处命令/hint 字符串漂移。
_ENTRY_CMD = "python .orchd/__main__.py"


# 旁路导航（next_action）词表 + → guidance step 的**单一映射（单一决策源）**。
# next_action 由 orchd/onboard/request/（entry + routing 两域）、orchd/review/request.py 与
# orchd/onboard/lifecycle/core/finish.py 产出；历史上它与 guidance.step 是两套互不相识的
# 词表（无一致性约束），且各调用点各自裸写字面量。
# task-guidance-completeness-gate 完成**调用点合流**：词表只在 guide 声明，
# 调用点一律引用下列常量，不得再写裸字面量——tests/test_guidance_sync.py 断言
# 「orchd/ 内除本模块外无任何 next_action 字符串字面量」。
NEXT_ACTION_EXIT: str = "exit"                  # request/review 无候选：停止等指令
NEXT_ACTION_WAIT: str = "wait"                  # request 触达 max_active：等待
NEXT_ACTION_REVIEW_FIRST: str = "review_first"  # request 有可领审查：优先领取
NEXT_ACTION_REVIEW_TAKEOVER: str = "review_takeover"  # 僵尸审查认领：可接管
NEXT_ACTION_AWAIT_REVIEW: str = "await_review"  # done 收尾 lessons 待审核
# 本会话已领审查未提交（review_claimed_by == 自己）：先提交手上的结论，不谈领新任务。
# task-guidance-review-priority-fix 补登记：此前 submit_review 只有 step 词表条目而无
# 对应 next_action 取值 → request 在「已持有审查认领」时只能产 review_first，把会话引向
# 他任务的审查（必被 claim 的 E011 review busy 拒绝），手上的认领就此悬空未提交。
NEXT_ACTION_SUBMIT_REVIEW: str = "submit_review"

NEXT_ACTION_TO_STEP: dict[str, str] = {
    NEXT_ACTION_EXIT: "stop_wait",
    NEXT_ACTION_WAIT: "wait_review",
    NEXT_ACTION_REVIEW_FIRST: "claim_review",
    # review_takeover（request 巡检到僵尸审查认领，candidate=None 时抬升）：动作语义
    # 是「接管并重新领取审查」（retract REVIEW_CLAIMED → re-claim），归入 claim_review。
    NEXT_ACTION_REVIEW_TAKEOVER: "claim_review",
    NEXT_ACTION_AWAIT_REVIEW: "lesson_review",
    NEXT_ACTION_SUBMIT_REVIEW: "submit_review",
}


def step_for_next_action(next_action: str | None) -> str:
    """next_action → guidance step（单一映射；未知取值兜底 check_status）。

    未登记取值返回 ``check_status`` 是**降级**语义：映射覆盖率由测试锁定
    （新增 next_action 取值而未登记 → 测试失败），不在运行期静默猜测。
    """
    if not next_action:
        return "check_status"
    return NEXT_ACTION_TO_STEP.get(next_action, "check_status")


def _quote_value(value: str) -> str:
    """含空白的取值渲染为双引号形态，使其在字面量下就是**一个** token。

    占位符（``<cmd --basetemp=...>``）与真实命令（``pytest -q``）都含空白，
    不加引号则 agent 照抄即被 shell/argparse 拆成多段（E024/E022 实测 exit=2）。
    双引号在 bash / PowerShell / cmd 三端皆认；无空白的值逐字符不变（hook E020
    逃生文本的 ``<file>`` 即属此类）。
    """
    return f'"{value}"' if any(ch.isspace() for ch in value) else value


def _as_declaration(value) -> list:
    """files/exempt 入参形态守卫：裸 str 视为**单个路径**，不是字符序列。

    根因防线（E038 实测）：``for f in '<f>'`` 逐字符迭代，渲染出
    ``--files-to-edit < --files-to-edit f --files-to-edit >``——parser 能吞下，
    但得到三个碎片路径。守卫后任何调用点都不可能再犯。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def amend_patch_cmd(task, files=None, exempt=None, verify=None, brief=None,
                    name=None, entry=_ENTRY_CMD):
    """amend 声明域补登命令的单一事实源（task-amend-decl-patch-channel）。

    guide.py 逃生文案、hook.py 逃生 echo、测试断言皆由此派生，禁止手写
    ``amend --task/--files-to-edit/--exempt-files/--verify-command`` 字符串。
    占位符（``<id>``/``{task_id}``/``<cmd>``）按原文透传，调用方负责替换。
    渲染保证**字面量可执行**（pass13 P1-4）：列表入参走形态守卫、含空白的取值
    加双引号，故产出的命令原样复制即可过 argparse/shlex。

    ``brief`` / ``name``（task-amend-text-field-patch-channel）：E038 的
    pending 出口文案经本生成器派生，与声明域旗标同一单源——状态矩阵见
    control.py 的 gating 块（pending 直接改，终态配 --revise-terminal）。
    """
    parts = [entry, "amend", "--task", _quote_value(str(task))]
    for f in _as_declaration(files):
        parts += ["--files-to-edit", _quote_value(str(f))]
    for f in _as_declaration(exempt):
        parts += ["--exempt-files", _quote_value(str(f))]
    if verify is not None:
        parts += ["--verify-command", _quote_value(str(verify))]
    if brief is not None:
        parts += ["--brief", _quote_value(str(brief))]
    if name is not None:
        parts += ["--name", _quote_value(str(name))]
    return " ".join(parts)


def amend_mainwt_command(task, main_wt, files=None, exempt=None, verify=None,
                         entry=_ENTRY_CMD):
    """带主工作树绝对路径的 amend 补登命令（task-amend-guidance-mainwt）。

    在 amend_patch_cmd 基础上前缀 cd "<main_wt>";，使持任务 agent
    可直接复制执行——任务 worktree 内无 _master.json，amend 必须回主工作树。
    跨平台用分号分隔（PowerShell / bash / cmd 均兼容）；main_wt 含空格时
    双引号包裹。占位符按原文透传，调用方负责替换。
    """
    cmd = amend_patch_cmd(task, files=files, exempt=exempt, verify=verify, entry=entry)
    return f'cd "{main_wt}"; {cmd}'

# ---------------------------------------------------------------------------
# W-1 guidance 分级：T0/T1/T2（task-guide-tiering）
# ---------------------------------------------------------------------------
# 信息瘦身口径（owner 已确认）：单视角 + 5 键 / 红线上限 1 条 / hint 单行。
# 全命令分三级注入，避免"每条命令灌全量、三层重复"的信息超载。
_T0_CMDS = frozenset({"status", "doctor", "help", "version", "session-status",
                      "session-current", "session", "pool", "validate",
                      "context-digest"})
_T1_CMDS = frozenset({"retract", "force-status"})


def guidance_tier(command: str) -> int:
    """命令 → 引导分级（纯函数，W-1）。

    T0  读/健康命令：guidance 置空（仅保留退出态一行）。
    T1  轻写/恢复命令：只给 step + command + hint + 1 条红线 + branch_ctx。
    T2  决策写命令：完整但精简（step/command/hint/read/branch_ctx 5 键）。
    未列出的命令（含子命令首 token，如 "session"）默认 T2。
    """
    if not command:
        return 2
    base = command.strip()
    if base in _T0_CMDS:
        return 0
    if base in _T1_CMDS:
        return 1
    return 2


# 必读面内容哈希（task-context-digest-command）：session-doc-digest 的引擎侧最小闭环。
# 覆盖面单一真源：显式文件清单 + rules/*.md 目录枚举（sorted）。只读输出，
# 无时间戳 / pid / mtime 等易变字段，同一输入字节稳定。
CONTEXT_DIGEST_FILES = (
    ".orchd/SKILL.md",
    "AGENTS.md",
    ".orchd/shared/conventions.md",
    ".orchd/shared/architecture.md",
)


def _digest_one(path: Path, rel: str) -> dict[str, Any]:
    """单个必读面文件的内容哈希记录（best-effort：缺失/异常标 exists=false，不抛异常）。"""
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        return {"path": rel, "sha256": "", "bytes": 0, "exists": False}
    return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "exists": True}


def context_digest(project_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """必读面文件的内容哈希与字节数（纯函数，只读，task-context-digest-command）。

    与 :func:`attach_read_versions` 同层（best-effort 容错风格），但判据是内容
    sha256 而非 mtime/size（checkout/clone 改 mtime 而内容相同、size 相同内容
    不同时 mtime/size 均误判）。

    Returns:
        ``{"files": [...], "digest": <hex>, "root": <str>}``：
        files 每项 ``{"path", "sha256", "bytes", "exists"}``（path 为相对项目根
        的正斜杠路径，按 path 升序；缺失文件 exists=false、sha256 为空串）；
        digest 为已存在文件 ``path:sha256`` 行拼接的 sha256（聚合指纹）；
        root 为解析用项目根（调用方传入原值，跨调用稳定）。
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    entries: list[dict[str, Any]] = [_digest_one(root / rel, rel) for rel in CONTEXT_DIGEST_FILES]
    try:
        rules_dir = root / ".orchd" / "rules"
        entries.extend(
            _digest_one(rules_dir / p.name, ".orchd/rules/" + p.name)
            for p in sorted(rules_dir.glob("*.md"))
            if p.is_file()
        )
    except OSError:
        pass
    entries.sort(key=lambda e: e["path"])
    agg = hashlib.sha256(
        "\n".join(f"{e['path']}:{e['sha256']}" for e in entries if e["exists"]).encode("utf-8")
    ).hexdigest()
    return {"files": entries, "digest": agg, "root": str(root)}
