"""orchd/spec/quality_terms.py — 模糊词/术语语料与 verify 路径判定域（W4-S3 拆分，纯搬移）。

来源：``orchd/spec/__init__.py``（起点 204aaa5a）的 L69-372，_CROSS_PLATFORM_BASETEMP
常量块（L107-111）随唯一消费者迁至 ``verify_cmd.py``
拆分记录见 design/file-split-program-20261015.md §4.5.3。

依赖方向：quality_terms.py → errors.py / spec.structure（仅 Master 类型标注）。
"""

import re
from pathlib import Path

from orchd.spec.structure import Master


# ─────────────────────────────────────────────────────────────────────────────
# 质量校验（弱 LLM 兜底）
# ─────────────────────────────────────────────────────────────────────────────

# 模糊词白名单：这些词虽然看似模糊，但在特定上下文中是可验证的
_VAGUE_WORDS_WHITELIST = {
    "自检通过",
    "测试通过",
    "验证通过",
    "正常输出",
    "正常运行",
    "无报错",
    "无异常",
    "无错误",
    "符合预期",
    "满足要求",
    # task-e023-vague-whitelist：可验证连词（精确子串，不可能误伤裸"正常"）
    "正常仓库",
    "健康仓库",
    "正常执行",
    "正常报",
    "main 正常",
}

# 模糊词检测列表
_VAGUE_WORDS = [
    "应该能",
    "合理地",
    "适当",
    "正常",
    "充分",
    "足够",
    "良好的",
    "优雅的",
    "健壮的",
    "高效的",
]


# 文档后缀白名单（与 onboard.py _DOC_SINGLE_STAGE_SUFFIXES 对齐）：
# files_to_edit 全部命中这些后缀 → 视为文档/基础设施类任务（R5 维持 warning）。
# 未声明 files_to_edit 或含任意代码文件 → 视为代码类（R5 缺 verify_command 升级阻断）。
_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt")

# R4 粒度锚点（与 SKILL.md 任务拆解粒度启发式一致）
_GRANULARITY_MAX_FILES = 5
_GRANULARITY_MAX_HOURS = 8
_GRANULARITY_MAX_AC = 6


# AC 结构术语形态（task-ac-term-resolvability，并入 E023 家族——见 AC 说明）：
# 新码 E040 的替代方案。并入理由：① E023 本就是"AC 不可判定"类 warning，
# 未定义的结构术语是其子种；② E023 逐条 AC 只报一次，并入即天然去重（AC3）；
# ③ warning 级 + 终态豁免现成，存量零变红（AC4）；④ 免动 errors.py 计数契约
# （test_errors.py 断言码数，域外文件）。
# 形态 = 数字 + 环/链/阶/步法（如 六环 / 论证链 / 三阶 / 四步法）。后随 段/级/梯/
# 节/条 时为普通词（阶段/阶级/阶梯/环节/链条），不命中——宁可漏判不可误报。
_STRUCTURAL_TERM_RE = re.compile(
    r"[一二三四五六七八九十百千万\d]+(?:环|链|阶|步法)(?![段级梯节条])"
)

# 结构术语 allowlist（AC2）：命中即视为已定义，不告警。默认空——数字形态术语多为
# 方法论专名（如 四步法），未定义就该告警；确需放行的通用词由用户在此登记。
_STRUCTURAL_TERM_ALLOWLIST: frozenset[str] = frozenset()

# 术语定义语料上限（best-effort 防护）：单文件 512KB、总量 8MB、最多 3000 文件。
# 超限静默截断（已收集部分照常用，不抛异常、不阻断校验）。
_TERM_CORPUS_MAX_FILE_BYTES = 512 * 1024
_TERM_CORPUS_MAX_TOTAL_BYTES = 8 * 1024 * 1024
_TERM_CORPUS_MAX_FILES = 3000
# 语料 walk 跳过的重型目录（与定义无关的生成物/依赖）。
_TERM_CORPUS_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    "target", "build", "dist", ".idea", ".vscode",
})


def _build_term_corpus(project_root: Path) -> str:
    """构建术语定义语料（task-ac-term-resolvability）：``.orchd/`` 文本 + 全库 ``*.md``。

    被校验工件自身的 ``*_master.json`` 排除在外——否则待检 AC 文本自命中，
    定义检查恒通过（真空）。定义应落在文档/规则/术语表（IDEAS / shared /
    conventions / docs），不在任务定义里自证。
    """
    parts: list[str] = []
    total = 0
    files = 0

    def _take(path: Path) -> None:
        nonlocal total, files
        if files >= _TERM_CORPUS_MAX_FILES or total >= _TERM_CORPUS_MAX_TOTAL_BYTES:
            return
        try:
            if path.stat().st_size > _TERM_CORPUS_MAX_FILE_BYTES:
                return
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return
        files += 1
        total += len(text)
        parts.append(text)

    try:
        root = Path(project_root)
        orchd_dir = root / ".orchd"
        if orchd_dir.is_dir():
            for path in sorted(orchd_dir.rglob("*")):
                if not path.is_file() or path.name.endswith("_master.json"):
                    continue
                if _TERM_CORPUS_SKIP_DIRS & set(path.parts):
                    continue
                _take(path)
        for path in sorted(root.rglob("*.md")):
            if not path.is_file():
                continue
            if _TERM_CORPUS_SKIP_DIRS & set(path.parts):
                continue
            _take(path)
    except (OSError, RuntimeError):
        pass
    return "\n".join(parts)


def _is_doc_task(t: dict) -> bool:
    """判定任务是否为文档/基础设施类（files_to_edit 全部为文档后缀）。

    与 onboard._is_doc_single_stage 不同的是：此处不校验 blocked 约定文件集合，
    仅按 files_to_edit 后缀白名单判定——空 files_to_edit 视为非文档（代码类），
    避免漏校验。
    """
    files_edit = [
        f for f in (t.get("files_to_edit") or []) if isinstance(f, str)
    ]
    if not files_edit:
        return False
    return all(f.lower().endswith(_DOC_SUFFIXES) for f in files_edit)


def is_code_task(t: dict) -> bool:
    """判定任务是否为代码类（非文档/基础设施类）。

    R5（task-constraint-quality-checks）：代码类任务缺 verify_command 在注册点阻断，
    文档/基础设施类维持 warning。供 split.py amend 阻断判据复用，避免后缀白名单漂移。
    """
    return not _is_doc_task(t)


def _tests_root_from_master(master: Master) -> Path | None:
    """从 _master.json 所在位置推导 tests/ 目录，不存在返回 None（E026 存在性兜底）。"""
    tests = _project_root_from_master(master) / "tests"
    return tests if tests.is_dir() else None


# ------------------------------------------------------------------
# 声明口径一致性校验（task-intake-decl-consistency-gate，2026-09-17）
# ------------------------------------------------------------------
# why：撰稿人手写的 brief / verify_command 与机器声明、仓库实际三者之间此前无一致性
# 校验，漂移只能等执行期由 agent 撞上——实测三例：verify_command 引用
# tests/test_precommit.py 与 tests/test_intake.py（两者均不存在，只能 amend 现场修正）；
# files_to_edit 声明当时不存在的 tests/test_session.py（被迫新建文件以满足 E010）。
#
# 错误码归属（task-decl-consistency-error-code 已落地专用码）：
# 校验① → E037（verify_reference_drift，阻断级，终态豁免）
# 校验② → E038（brief_decl_count_mismatch，warning 级，终态豁免）
# E027 回归原语义（verify_command 不安全/不兼容段），E029 回归原语义（粒度越界/建议拆分）。
# 历史背景：task-intake-decl-consistency-gate 交付时因声明边界复用 E027/E029，
# 导致 guidance 文案串味（「建议拆分」用于数量口径不一致）、按码统计无法区分
# 「命令不安全」与「声明口径漂移」。本任务拆出专用码并同步 errors.py / guide.py /
# TERMINAL_EXEMPT_QUALITY_CODES，存量 387 条任务零新增误报（14 例历史命中均为终态豁免）。
_DECL_COUNT_RE = re.compile(r"files_to_edit\s*[）)」\"']?\s*控\s*(\d+)")
# verify_command 中「仓库内相对路径」识别后缀白名单：只认带明确文件后缀的 token，
# 宁可漏判不可误判（目录目标如 `ruff check orchd/`、选项 `-q` 均不参与判定）。
_VERIFY_PATH_SUFFIXES = (
    ".py",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".txt",
    ".sh",
    ".ps1",
)
# 含以下字符的 token 不做路径解析：shell 元字符（$ ` | > < ( ) & ; * ? { } [ ]）、
# 反斜杠（Windows 路径形态）与冒号（env 展开 / 盘符）。
_VERIFY_PATH_SKIP_CHARS = frozenset("$`|><()&;*?{}[]\\:")


def _project_root_from_master(master: Master) -> Path:
    """从 _master.json 位置推导项目根（声明路径的存在性基准）。

    .orchd/ 布局（``<root>/.orchd/_master.json``）→ 项目根为 source_path.parent.parent；
    根布局（``<root>/_master.json``）→ 项目根为 source_path.parent。
    """
    src = master.source_path
    return src.parent.parent if src.parent.name == ".orchd" else src.parent


def _verify_command_path_tokens(verify_cmd: str) -> list[str]:
    """抽取 verify_command 中「按仓库根解析」的路径 token（保守判定）。

    跳过：以 ``-`` 开头（选项）、以 ``/`` 开头（绝对路径如 /dev/null）、无已知文件
    后缀（如 ruff 的目标目录 ``orchd/``）、含 shell 元字符 / 反斜杠 / 冒号的 token。
    """
    out: list[str] = []
    for raw in verify_cmd.split():
        tok = raw.strip("\"'")
        if not tok or tok.startswith("-") or tok.startswith("/"):
            continue
        if not tok.endswith(_VERIFY_PATH_SUFFIXES):
            continue
        if any(ch in _VERIFY_PATH_SKIP_CHARS for ch in tok):
            continue
        out.append(tok)
    return out


# ------------------------------------------------------------------
# AC → verify 覆盖反向一致性（E040 ac_verify_gap，task-ac-verify-coverage，2026-10-07）
# ------------------------------------------------------------------
# 现象：AC 明写「旧路径残留 0（对全仓库与全部任务声明做 grep）」，而 verify_command
# 只是 trivial 命令——声称与验证手段脱节，351 处旧名照样通过验收。本组常量把该类
# 「可判定声称」的启发式形态集中于此，供 validate_quality 追加 E040 warning。
# 方向：宁可漏判不可误报（中文启发式）；AC 仍人类文本、verify_command 仍机器真源。
#
# 声称形态①「零残留」：残留 0 / 为零 / 清零 / 归零 …
# task-e040-zero-fp-fix：裸 `为零` 会把「行为零变化/因为零…/作为零…」（为+零相邻）
# 误判为「为零」声称——该短语在本仓 AC 中极常见。故对 `为零` 加负向环视排除
# 行为/因为/作为 前缀；合法语境（残留为零 / 数量为零）不受影响。
_AC_ZERO_RESIDUAL_RE = re.compile(
    r"(?:残留|剩下|剩余)\s*[0０]|(?<!行)(?<!因)(?<!作)为零|清零|归零|均已?清除|全部清除"
)
# 声称形态②「全仓范围」：全仓库 / 全库 / 整个仓库（"全部任务"须与检查动词同现，
# 见 _ac_repo_claim，避免「覆盖全部任务字段」这类无关陈述误报）。
_AC_REPO_WIDE_RE = re.compile(r"(?:全仓库|全库|整个仓库|全仓)")
_AC_ALL_TASKS_RE = re.compile(r"(?:全部任务|所有任务|全量任务)")
_AC_CHECK_VERB_RE = re.compile(r"(?:grep|检索|扫描|检查|清除|残留|不复存在|不存在)")
# 人工裁决形态：AC 声明 manual / 需人工 → 跳过（人类文本不要求机器 verify 覆盖）。
_AC_MANUAL_RE = re.compile(r"(?:manual|需人工|人工确认|人工核|人工审查|手动确认)")
# verify 侧：grep 族程序（grep / rg / findstr / ack）。
_VERIFY_GREP_FAMILY_RE = re.compile(
    r"(?:^|[\s|;&(])(?:git\s+)?(?:grep|rg|findstr|ack)(?=$|[\s|;&)])"
)
# verify 侧：repo 级 grep 判据（递归旗标 / git grep / rg 默认递归 / findstr /s /
# 目标为仓库根）。文件级 grep（无递归、指定单文件）不算全仓覆盖。
_VERIFY_GREP_REPO_RE = re.compile(
    r"(?:^|\s)-[A-Za-z]*[rR][A-Za-z]*(?=\s|$)"
    r"|--recursive"
    r"|(?:^|\s)(?:\.|\./)(?=\s|$)"
    r"|(?:^|[\s|;&(])git\s+grep"
    r"|(?:^|[\s|;&(])rg(?=$|[\s|;&)])"
    r"|findstr(?=[^|;&]*\s/[sS])"
)
# verify 侧：run 引用（引用 scripts/、tools/ 下的检查脚本或独立 run 令牌）——
# 视为可能自带检查逻辑，不判缺口（避免对合法的脚本化检查误报）。
_VERIFY_RUN_REF_RE = re.compile(
    r"(?:scripts|tools)[/\\][\w./\\-]+\.(?:py|sh)"
    r"|(?:^|[\s|;&(])run(?=$|[\s|;&)])"
)


def _ac_repo_claim(ac: str) -> tuple[bool, bool]:
    """AC 是否声称「零残留 / 全仓」可判定性质（task-ac-verify-coverage）。

    Returns:
        ``(claimed, repo_wide)``：是否声称；若声称，是否为**全仓范围**（全仓
        声称要求 verify 含 repo 级 grep，文件级 grep 不算覆盖）。含 manual /
        需人工 的 AC 一律不声称（人类裁决，机器 verify 无义务覆盖）。
    """
    if not isinstance(ac, str) or not ac:
        return False, False
    if _AC_MANUAL_RE.search(ac):
        return False, False
    repo_wide = bool(_AC_REPO_WIDE_RE.search(ac)) or bool(
        _AC_ALL_TASKS_RE.search(ac) and _AC_CHECK_VERB_RE.search(ac)
    )
    zero = bool(_AC_ZERO_RESIDUAL_RE.search(ac))
    return (zero or repo_wide), repo_wide


def _verify_has_grep(verify_cmd: str) -> bool:
    """verify_command 是否含 grep 族程序。"""
    return bool(verify_cmd and _VERIFY_GREP_FAMILY_RE.search(verify_cmd))


def _verify_has_repo_grep(verify_cmd: str) -> bool:
    """verify_command 是否为 repo 级 grep（递归 / git grep / rg / findstr /s / 目标为根）。"""
    return bool(
        verify_cmd
        and _VERIFY_GREP_FAMILY_RE.search(verify_cmd)
        and _VERIFY_GREP_REPO_RE.search(verify_cmd)
    )


def _verify_has_run_ref(verify_cmd: str) -> bool:
    """verify_command 是否引用仓库内检查脚本（run 引用）。"""
    return bool(verify_cmd and _VERIFY_RUN_REF_RE.search(verify_cmd))
