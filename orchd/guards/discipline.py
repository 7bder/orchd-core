"""宿主测试纪律 guard：把 check_test_dedup 的三条判据暴露为只读评估器。

判据（与 ``scripts/check_test_dedup.py`` 共用同一实现，脚本只是 CLI 外壳）：

- **共享定义重复**：``conftest.py`` 顶层 ``def`` 名称（fixture/helper）不得在其它
  测试文件中再次定义——复制式共享 helper 会让夹具漂移、失效点难以定位。
- **规则 A（git init 夹具）**：function/class 级 ``@pytest.fixture`` 内不得出现
  ``git init`` 子进程；仓库初始化必须收敛到 conftest 的 session 级模板，用例用
  ``copytree`` 取独立副本。
- **规则 B（重脚本）**：``sync_orchd_core.sh`` 等重脚本只允许在 session/module 级
  夹具内执行；出现在方法体或 function 级夹具即违规。

判定要求 ``tests_dir/conftest.py`` 存在（宿主仓库的「测试基座」）。缺少 conftest
说明该仓库没有采用 orchd 的测试纪律约定，评估器返回空集（not_applicable 语义），
不误报；配置非法时由 done 分派层留痕降级。

命中项以 ``GuardFinding`` 逐条返回，``context`` 带文件相对路径与行号，便于宿主
在 warn/hard 两档下生成可操作报告。本模块只读：不写盘、不起子进程，纯文本扫描。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from orchd.guards import GuardEvalContext, GuardFinding, load_guards_config

__all__ = (
    "DEFAULT_TESTS_DIR",
    "DEFAULT_CONFTEST_NAME",
    "DisciplinePolicy",
    "DisciplineGuardConfigError",
    "collect_conftest_names",
    "scan_discipline",
    "scan_tests_dir",
    "parse_policy",
    "evaluate",
    "register",
)

DEFAULT_TESTS_DIR = "tests"
DEFAULT_CONFTEST_NAME = "conftest.py"
KNOWN_OPTIONS = frozenset({"tests_dir", "conftest"})

_DEF_PATTERN = re.compile(r"^(\s*)def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_FIXTURE_PATTERN = re.compile(r"^\s*@pytest\.fixture\b(.*)$")
_SCOPE_PATTERN = re.compile(
    r"""scope\s*=\s*['"](session|module|class|function)['"]"""
)
_GIT_INIT_PATTERN = re.compile(r"""['"]git['"]\s*,\s*['"]init['"]""")
_HEAVY_SCRIPT_PATTERN = re.compile(r"sync_orchd_core\.sh")
_EXEC_VERB_PATTERN = re.compile(
    r"subprocess|Popen|os\.system|check_output|\.run\s*\("
)
_FALLBACK_SHARED = frozenset({"make_task", "store", "orchd_dir"})
_SELF_TEST_FILES = frozenset(
    {"test_test_discipline.py", "test_host_guard_discipline.py"}
)
_IGNORE_MARKER = "check_test_dedup: ignore"
_SESSION_SCOPES = frozenset({"session", "module"})


class DisciplineGuardConfigError(ValueError):
    """discipline guard 配置非法（未知键/坏路径）。"""


@dataclass(frozen=True)
class DisciplinePolicy:
    """宿主可配置的测试纪律扫描面。"""

    tests_dir: str = DEFAULT_TESTS_DIR
    conftest: str | None = None

    def conftest_rel(self) -> str:
        """conftest 相对项目根的路径（缺省 tests_dir/conftest.py）。"""
        if self.conftest is not None:
            return self.conftest
        return f"{self.tests_dir}/{DEFAULT_CONFTEST_NAME}"


def _normalize_rel_dir(text: object, field: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise DisciplineGuardConfigError(f"{field} 必须是非空字符串")
    raw = text.strip().replace("\\", "/")
    if raw.startswith("/") or ":" in raw:
        raise DisciplineGuardConfigError(f"{field} 必须是项目内相对路径：{text!r}")
    rel = raw.strip("/")
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        raise DisciplineGuardConfigError(f"{field} 不能包含空段/./..：{text!r}")
    if any(part in ("", ".", "..") for part in parts):
        raise DisciplineGuardConfigError(f"{field} 不能包含空段/./..：{text!r}")
    return rel


def parse_policy(options: object) -> DisciplinePolicy:
    """把 guards.json 的 discipline options 解析为 :class:`DisciplinePolicy`。"""
    if options is None:
        return DisciplinePolicy()
    if not isinstance(options, Mapping):
        raise DisciplineGuardConfigError("discipline 选项必须是对象")
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise DisciplineGuardConfigError(
            f"discipline 存在未知配置键：{', '.join(unknown)}"
        )
    tests_dir = _normalize_rel_dir(
        options.get("tests_dir", DEFAULT_TESTS_DIR), "tests_dir"
    )
    conftest = options.get("conftest")
    if conftest is not None:
        conftest = _normalize_rel_dir(conftest, "conftest")
    return DisciplinePolicy(tests_dir=tests_dir, conftest=conftest)


@dataclass(frozen=True)
class _FixtureBlock:
    """一个 @pytest.fixture 装饰的函数块（行号 0-based，end 排他）。"""

    decorator_line: int
    def_line: int
    end_line: int
    scope: str
    ignored: bool


def collect_conftest_names(conftest: Path) -> frozenset[str]:
    """从 conftest.py 提取顶层共享定义名（缩进的类方法/闭包不参与）。"""
    names: set[str] = set()
    for line in conftest.read_text(encoding="utf-8").splitlines():
        if line[:1] in (" ", "\t"):
            continue
        match = _DEF_PATTERN.match(line)
        if match:
            names.add(match.group(2))
    return frozenset(names) or _FALLBACK_SHARED


def _parse_fixture_blocks(lines: list[str]) -> list[_FixtureBlock]:
    """解析文件中的 @pytest.fixture 块、scope 与行范围。"""
    blocks: list[_FixtureBlock] = []
    n = len(lines)
    i = 0
    while i < n:
        dec = _FIXTURE_PATTERN.match(lines[i])
        if not dec:
            i += 1
            continue
        decorator_line = i
        scope_match = _SCOPE_PATTERN.search(dec.group(1))
        scope = scope_match.group(1) if scope_match else "function"
        ignored = _IGNORE_MARKER in lines[i]

        j = i + 1
        def_line = -1
        def_indent = ""
        while j < n and j <= i + 6:
            match = _DEF_PATTERN.match(lines[j])
            if match:
                def_line = j
                def_indent = match.group(1)
                if _IGNORE_MARKER in lines[j]:
                    ignored = True
                break
            if lines[j].strip() and not lines[j].lstrip().startswith("@"):
                break
            j += 1
        if def_line < 0:
            i += 1
            continue

        base_indent = len(def_indent)
        k = def_line + 1
        while k < n:
            stripped = lines[k].strip()
            if stripped:
                cur_indent = len(lines[k]) - len(lines[k].lstrip())
                if cur_indent <= base_indent:
                    break
            k += 1
        blocks.append(_FixtureBlock(decorator_line, def_line, k, scope, ignored))
        i = k
    return blocks


def _scan_file(lines: list[str], shared: frozenset[str], rel: Path) -> list[str]:
    """单个测试文件的违规报告行（空 = 通过）。"""
    violations: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        match = _DEF_PATTERN.match(line)
        if match and match.group(2) in shared and _IGNORE_MARKER not in line:
            violations.append(
                f"{rel}:{line_no}: 重复定义共享 helper/fixture "
                f"'{match.group(2)}'（已收敛于 conftest，请从 conftest 复用）"
            )

    blocks = _parse_fixture_blocks(lines)
    for blk in blocks:
        if blk.ignored or blk.scope in _SESSION_SCOPES:
            continue
        for offset, line in enumerate(
            lines[blk.def_line + 1:blk.end_line], start=blk.def_line + 2
        ):
            if _GIT_INIT_PATTERN.search(line):
                violations.append(
                    f"{rel}:{offset}: function 级 fixture 内出现 git init（规则A）——"
                    f"git 仓库初始化须收敛到 conftest 的 session 级模板，用例以 "
                    f"copytree 获取独立副本；确属独立语义请加 "
                    f"`# check_test_dedup: ignore <理由>`"
                )
                break

    heavy_blocks = [blk for blk in blocks if blk.scope in _SESSION_SCOPES]
    for line_no0, line in enumerate(lines):
        if not _HEAVY_SCRIPT_PATTERN.search(line):
            continue
        if not _EXEC_VERB_PATTERN.search(line):
            continue
        stripped = line.lstrip()
        if stripped.startswith("#") or _IGNORE_MARKER in line:
            continue
        inside_heavy = any(
            blk.decorator_line <= line_no0 < blk.end_line for blk in heavy_blocks
        )
        if not inside_heavy:
            violations.append(
                f"{rel}:{line_no0 + 1}: sync_orchd_core.sh 出现在非 session/module "
                f"级作用域（规则B）——重脚本须在 session/module 级夹具只执行一次，"
                f"用例以 copytree 模板断言；确需逐例执行请加 "
                f"`# check_test_dedup: ignore <理由>`"
            )
    return violations


def scan_tests_dir(tests_dir: Path, conftest: Path | None = None) -> list[str]:
    """扫描 tests_dir（conftest 缺省为 tests_dir/conftest.py），返回违规行。

    缺 conftest 时返回空列表——「非 pytest 仓/未采用纪律基座」由调用方决定
    是 not_applicable（guard）还是退出码 1（CLI 脚本的红线语义）。
    """
    if not tests_dir.is_dir():
        return []
    conftest = conftest or (tests_dir / DEFAULT_CONFTEST_NAME)
    if not conftest.is_file():
        return []

    shared = collect_conftest_names(conftest)
    violations: list[str] = []
    for py in sorted(tests_dir.glob("*.py")):
        if py.name == conftest.name or py.name in _SELF_TEST_FILES:
            continue
        lines = py.read_text(encoding="utf-8").splitlines()
        violations.extend(_scan_file(lines, shared, py.relative_to(tests_dir.parent)))
    return violations


def scan_discipline(project_root: Path, policy: DisciplinePolicy) -> list[str]:
    """按策略扫描项目内测试纪律；缺 conftest 视为 not_applicable（空集）。"""
    return scan_tests_dir(
        project_root / policy.tests_dir, project_root / policy.conftest_rel()
    )


def _configured_policy(project_root: Path) -> DisciplinePolicy:
    config = load_guards_config(project_root)
    entry = next(
        (entry for entry in config.entries if entry.name == "discipline"), None
    )
    return parse_policy(entry.options if entry is not None else None)


def evaluate(context: GuardEvalContext) -> tuple[GuardFinding, ...]:
    """按宿主配置扫描测试纪律；无 conftest 视为 not_applicable（空集）。"""
    if context.project_root is None:
        return ()
    try:
        policy = _configured_policy(context.project_root)
    except DisciplineGuardConfigError as exc:
        raise RuntimeError(f"discipline guard 配置非法：{exc}") from exc
    violations = scan_discipline(context.project_root, policy)
    return tuple(
        GuardFinding(
            guard="discipline",
            message=violation,
            context={
                "tests_dir": policy.tests_dir,
                "conftest": policy.conftest_rel(),
            },
        )
        for violation in violations
    )


def register() -> None:
    """把本评估器登记到 guards 注册表（幂等）。"""
    from orchd.guards import register_guard_evaluator

    register_guard_evaluator("discipline", evaluate)


register()
