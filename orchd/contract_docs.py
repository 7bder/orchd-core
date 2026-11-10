"""审查契约文档判据（task-contract-docs-host-enforce）。

出处：`task-review-subagent-contract-strict` 建立的守卫原本住在
``tests/test_review_contract_strict.py``——``tests/`` 不随安装进宿主，于是宿主既没有
自动校验，规则文件里又指向不存在的测试路径（悬空引用）。本模块把**词表 / 表面清单 /
扫描函数**下沉为引擎侧单一真源：

- 引擎仓（自托管）：``tests/test_review_contract_strict.py`` 与本模块配合，扫描 10 个
  契约面并校验「三条禁止 + 中断语义 + 首句来源」在场；
- 宿主仓：``orchd doctor`` 的 ``contract_docs`` 检查复用同一判据，扫描安装器发货的
  ``.orchd/SKILL.md`` / ``.orchd/rules/*.md`` / ``.orchd/templates/*.md`` 与宿主
  ``AGENTS.md``；宿主不要求协议在场标记（那几面不在宿主发货范围），只做旁路词零命中。

降级面判据（task-review-hardblock-e6-protocol-docs，2026-11-02）：自审口径已收口为
「恒阻断（claim + submit 双侧）+ 审查会话 lineage 硬门」，config/任务级开关已从引擎
删除。契约面若仍把降级档（可配置开关 / 提示档 / 自审通知 / 允许自审 / 显式退出）写成
现行能力，即为失实陈述 ⇒ :data:`DOWNGRADE_PATTERNS` 零命中。

判据口径（与 rules/review.md「旁路写法约束」一致）：**零语义否定豁免**——任何旁路
命中即判红；契约面需要引用时用占位名「会话切换旁路」/「身份切换旁路」。词表是开放集：
未覆盖写法会假绿，发现即补 pattern + 负控。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from orchd.repo_role import is_engine_repo

# Tier A：协议层四处（注册 AC2 点名的四处）——三条禁止 + 中断语义 + 首句来源声明全须在场
ENGINE_CORE_SURFACES: tuple[str, ...] = (
    ".orchd/rules/review.md",
    ".orchd/SKILL.md",
    ".orchd/rules/session.md",
    ".orchd/shared/conventions.md",
)
# Tier B：宿主每 session 必读的入口、审查者逐字执行的角色模板、人看的手册
ENGINE_HOST_SURFACES: tuple[str, ...] = (
    "AGENTS.md",
    "release/AGENTS.md",
    "docs/user-manual.md",
    "templates/reviewer.md",
    "templates/spec-reviewer.md",
    "templates/code-reviewer.md",
)
ENGINE_SURFACES: tuple[str, ...] = ENGINE_CORE_SURFACES + ENGINE_HOST_SURFACES

# 宿主仓实际可见的契约面：安装器发货的 SKILL/rules/templates + 宿主根 AGENTS.md。
HOST_FIXED_SURFACES: tuple[str, ...] = (".orchd/SKILL.md", "AGENTS.md")
HOST_GLOB_DIRS: tuple[str, ...] = (".orchd/rules", ".orchd/templates")

# 在场判据：容忍 markdown 强调与反引号 / 大小写 / 下划线写法的差异。
# 容忍偏宽的方向只可能把「旁路」误判成「已禁止」（漏报），该方向可接受。
PRESENCE_MARKERS: dict[str, re.Pattern[str]] = {
    "prohibit_self_minted_token": re.compile(r"自铸\s*`{0,2}session[\s_]*token", re.I),
    "prohibit_handset_session_env": re.compile(
        r"手设\s*(?:（[^）]{0,20}）|\([^)]{0,20}\))?\s*`{0,2}ORCHD_SESSION_ID"
    ),
    "prohibit_reused_token": re.compile(
        r"复用他人\s*`{0,2}(?:session[\s_]*token|token)", re.I
    ),
    "halt_without_subagent": re.compile(r"无\s*subagent\s*能力即"),
}
SOURCE_MARKER: re.Pattern[str] = re.compile(r"首句写明\s*`{0,2}subagent\s*来源")

# 缺席判据：换会话旁路的词汇形态。**字符间允许空白**——逐行判会让折行写法逃逸。
BYPASS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"换\s*[一]?\s*[个]?\s*(?:新|独立|另)?\s*会\s*话"),
    re.compile(r"换\s*[一]?\s*[个]?\s*对\s*话"),
    re.compile(r"另\s*[开起]\s*[一]?\s*[个]?\s*(?:平级)?\s*会\s*话"),
    re.compile(r"[起换用由]\s*[一]?\s*[个]?\s*(?:独立|新)\s*(?:会\s*话|对\s*话)"),
    re.compile(r"唯\s*一\s*出\s*路"),
    # 同义改写：不走「换会话」字面，但走的是同一条旁路
    re.compile(r"取\s*个\s*号"),
    re.compile(r"换\s*[一]?\s*[个份]\s*身\s*份"),
    re.compile(r"另\s*找\s*[一]?\s*份?\s*身\s*份"),
    re.compile(r"(?:再|另)\s*mint\s*[一]?\s*个?\s*(?:新\s*)?token", re.I),
    re.compile(r"换\s*[一]?\s*个\s*号"),
    re.compile(r"fresh\s*chat", re.I),
    re.compile(r"开\s*[一]?\s*个?\s*(?:新\s*)?小\s*号"),
    re.compile(r"(?:另|再)\s*取\s*[一]?\s*个?\s*(?:新\s*)?身\s*份"),
    re.compile(r"换\s*新\s*身\s*份"),
    re.compile(r"(?:新|另)\s*开\s*[一]?\s*个?\s*对\s*话"),
)


# 降级面词表（task-review-hardblock-e6-protocol-docs）：自审降级档已删除，任何把
# 「可配置开关 / 提示档 / 自审通知 / 允许自审 / 显式 opt-out」写成现行能力的措辞都判红。
# 与 BYPASS_PATTERNS 分开：后者防「冒充独立审查的旁路」，本表防「把已删除的降级档写成现行」。
DOWNGRADE_PATTERNS: dict[str, re.Pattern[str]] = {
    "enforce_switch": re.compile(r"enforce_self_review_\s*block", re.I),
    "self_review_notice": re.compile(r"self_review_\s*notice", re.I),
    "notice_tier": re.compile(r"提示\s*档"),
    "three_disclosures": re.compile(r"三\s*项\s*披\s*露"),
    "allow_self_review": re.compile(r"允许\s*自\s*审"),
    "explicit_opt_out": re.compile(r"显式\s*opt[\s-]*out", re.I),
}


def _line_of(text: str, index: int) -> int:
    """匹配偏移 → 1 基行号（整文本扫描后仍要能定位到行）。"""
    return text.count("\n", 0, index) + 1


def contract_violations(text: str) -> list[str]:
    """旁路命中（零豁免）：整文本扫描，折行由字符间 ``\\s*`` 覆盖。"""
    problems: list[str] = []
    for pat in BYPASS_PATTERNS:
        for hit in pat.finditer(text):
            problems.append(f"bypass@L{_line_of(text, hit.start())}:{hit.group(0)}")
    return problems


def downgrade_violations(text: str) -> list[str]:
    """降级面命中（零豁免）：把已删除的自审降级档写成现行能力。"""
    problems: list[str] = []
    for name, pat in DOWNGRADE_PATTERNS.items():
        for hit in pat.finditer(text):
            problems.append(f"downgrade@L{_line_of(text, hit.start())}:{name}:{hit.group(0)}")
    return problems


def contract_presence_missing(text: str) -> list[str]:
    """协议面在场标记缺失（引擎仓专用；宿主不要求这几面在场）。"""
    return [f"missing:{name}" for name, pat in PRESENCE_MARKERS.items() if not pat.search(text)]


def contract_problems(text: str, *, with_presence: bool = False) -> list[str]:
    """统一判据入口：降级面 + 旁路命中必查；引擎仓额外要求在场标记。"""
    problems = downgrade_violations(text) + contract_violations(text)
    if with_presence:
        problems = contract_presence_missing(text) + problems
    return problems


def resolve_surfaces(project_root: Path) -> tuple[str, ...]:
    """按仓角色返回应扫描的契约面（仓库相对 POSIX 路径，仅返回真实存在的文件）。"""
    root = Path(project_root)
    if is_engine_repo(root):
        return tuple(rel for rel in ENGINE_SURFACES if (root / rel).is_file())
    surfaces: list[str] = [rel for rel in HOST_FIXED_SURFACES if (root / rel).is_file()]
    for rel_dir in HOST_GLOB_DIRS:
        directory = root / rel_dir
        if not directory.is_dir():
            continue
        surfaces.extend(
            sorted(p.relative_to(root).as_posix() for p in directory.glob("*.md"))
        )
    return tuple(surfaces)


def scan_project(project_root: Path) -> list[dict[str, Any]]:
    """扫描当前仓的契约面，返回违规清单（空 = 通过）。

    Each item: ``{"path": <repo-relative>, "problems": [...]}``。
    """
    root = Path(project_root)
    engine = is_engine_repo(root)
    findings: list[dict[str, Any]] = []
    for rel in resolve_surfaces(root):
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        problems = contract_problems(text, with_presence=engine)
        if problems:
            findings.append({"path": rel, "problems": problems})
    return findings
