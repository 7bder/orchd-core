#!/usr/bin/env python3
"""orchd 安装器：把 orchd-core 源码组装进宿主项目 .orchd/（纯标准库）。

发布模型 v3（task-121-installer）：orchd-core 是源码仓库，宿主项目通过本安装器
把 orchd-core 的引擎与资源"安装"到自身 .orchd/，形成自包含工作空间。

设计约束：
- 纯 Python 标准库（os / re / shutil / subprocess / argparse / json），无任何第三方依赖；
- 资源根 = 本脚本所在目录的父目录（orchd-core 源码根）；
- 安装器自身不依赖 orchd 引擎，也不依赖 .orchd/，可跨平台（Windows/macOS/Linux）运行；
- 首次安装：完整组装 .orchd/（vendored 引擎 + schema/templates/rules/docs + SKILL +
  零根入口 + 打包配置 + shared/proposals/IDEAS.md 工作区骨架 + 宿主根 ROADMAP.md），
  并按布局生成 .orchd/.gitignore 忽略契约（flat / container 等价），清 __pycache__；
  注意 ROADMAP.md 落宿主项目根（唯一源，不在 .orchd/）；
- 已存在时：无 --update/--force → 非零退出并明确提示；--update 就地升级（保留宿主
  shared/、_master.json、IDEAS/ROADMAP、ledger/checkpoint、session 锁）；
  --force 覆盖安装（重建 .orchd/，覆盖全部）；
- --agent：仅输出最终 JSON（installed/mode/host/orchd_dir/next），无交互提示。
- --client-pointers：默认关闭；开启时才写宿主根四个 agent 客户端一行指针文件
  （C-CLIENT-POINTERS@1），关闭时宿主根不新增任何文件。
- --cleanup：安装成功后删除克隆源目录（orchd-core/），实现无痕安装（仅当脚本位于
  克隆根且目录名为 orchd-core 时删除，主项目布局安全跳过）。

用法：
    python release/install.py <host> [--update] [--force] [--agent]
        [--client-pointers] [--cleanup]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 最低支持 Python（与 pyproject requires-python 同步）：安装器与引擎均要求 >= 3.11。
# 安装路径不经由 pip（文档主路径为 `python orchd-core/install.py .`），requires-python
# 不约束此处 ⇒ 入口显式守卫，低于下限时给明确提示而非深层 SyntaxError
# （task-python311-floor-convergence-fix，L3）。
_MIN_PYTHON = (3, 11)


def _require_min_python(version_info: tuple[int, int, int] | None = None) -> None:
    """低于支持下限时向 stderr 输出中文提示并非零退出（安装路径无 pip 红线兜底）。

    为可单测，版本取 ``version_info`` 形参（默认 ``sys.version_info``）；安装器入口
    ``main()`` 不传参即按真实运行时版本守卫。
    """
    if version_info is None:
        version_info = sys.version_info
    if version_info < _MIN_PYTHON:
        sys.stderr.write(
            f"orchd 需要 Python >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}，"
            f"当前为 {version_info[0]}.{version_info[1]}。"
            "请升级 Python 后重试（安装器与引擎均要求 >= 3.11）。\n"
        )
        sys.exit(2)


# 资源根：本脚本所在目录的父目录（orchd-core 源码根）。
# 自适应两种布局：主项目内脚本位于 release/ 子目录（资源根在 parent.parent），
# 发布到 orchd-core 后脚本位于根目录（扁平布局，资源根即 parent）。
_SELF_DIR = Path(__file__).resolve().parent
RESOURCE_ROOT = _SELF_DIR if (_SELF_DIR / "orchd").is_dir() else _SELF_DIR.parent

# 组装进 .orchd/ 的内容清单
# （与 scripts/sync_orchd_core.sh / scripts/verify_release_self_contained.py 对齐）
ENGINE_DIR = RESOURCE_ROOT / "orchd"
RESOURCE_DIRS = [("schema", "schema"), ("templates", "templates"), ("skill/rules", "rules")]
RESOURCE_FILES = [
    ("docs", "decomposition-guide.md"),
    ("docs", "user-manual.md"),
    ("docs", "runtime-spec.md"),
]
# 预生成契约文档快照（task-docsgen-gate）：安装器把六份产物放进
# <host>/.orchd/docs/_generated/，宿主 docs check 只读校验，不依赖运行时生成。
GENERATED_DOC_FILES = (
    "error-codes.md",
    "commands.md",
    "fields.md",
    "events.md",
    "statemachine.md",
    "dependency-graph.md",
)
PACKAGING_FILES = ["pyproject.toml", "MANIFEST.in", "LICENSE", ".gitignore"]
# SKILL / 零根入口归置 .orchd/（task-12-workspace-docs）；兼容 orchd-core skill/ 与旧根布局
SKILL_CANDIDATES = [
    RESOURCE_ROOT / "skill" / "SKILL.md",   # orchd-core 发布版 skill/ 子目录
    RESOURCE_ROOT / ".orchd" / "SKILL.md",  # 主项目布局
    RESOURCE_ROOT / "SKILL.md",             # 旧扁平布局
]
LAUNCHER_CANDIDATES = [RESOURCE_ROOT / ".orchd" / "__main__.py", RESOURCE_ROOT / "__main__.py"]

# 宿主用户数据（--update 时 .orchd/ 内保留，不覆盖）
# 注：ROADMAP.md 已落宿主项目根（不在 .orchd/），故 .orchd/ 用户数据集合不再含它。
_USER_PATHS = {
    "shared", "proposals", "_master.json", "IDEAS.md",
    "IDEAS-archive.md", "_ledger.jsonl", "_checkpoint.json", ".session.lock",
}

# 安装清单（task-installer-shipped-ownership）：记录安装器写入的 shipped 文件
# sha256，供 --update 判断宿主是否改过。位于 .orchd/ 直接条目，被生成的
# .orchd/.gitignore 的 `/*` 覆盖——不入宿主 git。
INSTALL_MANIFEST = ".install-manifest.json"

# shipped 文件所有权（task-installer-shipped-ownership）：
# - engine/rules/templates/SKILL/__main__/schema/docs 由安装器管理，--update 会覆盖；
#   宿主定制放 shared/（不覆盖）。
# - hooks（.githooks/）不随包：安装器只配置 core.hooksPath，不写 hook 内容。

# .orchd/.gitignore 契约（task-installer-layout-placement）：安装器按宿主布局生成。
# 源码根 .gitignore 的规则按「本文件位于 git 根」编写（.orchd/* + 豁免集），被
# PACKAGING_FILES 原样拷入 .orchd/ 后，模式相对 .orchd/ 解析 → `.orchd/*` 变成
# `.orchd/.orchd/*` 全部失效（container 布局下 .orchd/ROADMAP.md 被 roadmap-land
# 误提交入库即此因，IDEAS/ROADMAP/SKILL 的忽略契约整体断裂）。
# 故安装后覆写为**布局无关的相对规则**：忽略 .orchd/ 直接条目（/*），豁免需入库的
# 工作区文档与共享上下文。实测（git 2.x / flat + container 双布局）：ROADMAP 与
# 运行时被忽略，IDEAS / SKILL / shared / rules / _master.json / __main__.py 可入库。
_ORCHD_GITIGNORE = """\
# orchd 安装器生成（task-installer-layout-placement）：模式一律相对 .orchd/ 解析，
# 与宿主布局（flat / container）无关——勿按「位于 git 根」的写法改写。
# 契约：IDEAS / SKILL / shared / _master.json / rules 入库；ROADMAP 与运行时不入库。
/*
!/_master.json
!/shared/
!/IDEAS.md
!/IDEAS-archive.md
!/SKILL.md
!/__main__.py
!/rules/
!/VERSION
"""

# 工作区文档模板（安装即就位，免去宿主人工补建；已存在则不动，保护宿主内容）
_IDEAS_TEMPLATE = """\
# IDEAS

> 灵感池：讨论产生的灵感先由 `orchd idea propose` 记入 study（论证中），
> 人工 `orchd idea confirm` 后才进入摄入队列（pending）。
"""

_ROADMAP_TEMPLATE = """\
# ROADMAP

> 未来版本规划（宿主资产，纳入 git；ROADMAP 唯一源 = 宿主项目根，引擎不读 .orchd/ 内副本）：
> 新版本先 `orchd roadmap-land <版本>` 落地为 IDEAS study 条目（待用户 confirm 升 pending），
> 再走摄入注册为任务。
"""

# 宿主三文件骨架（task-installer-host-docs-skeleton）：CHANGELOG（已完成记录）与
# docs/system-design.md（架构与关键技术）随安装就位；已存在则不动，不做 git add
# （入库决定权在宿主，沿用 ROADMAP 既定口径：仅提示确认）。
_CHANGELOG_TEMPLATE = """\
# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]
"""

_SYSTEM_DESIGN_TEMPLATE = """\
# SystemDesign

> 系统架构与关键技术描述（宿主资产，纳入 git）。

## 架构总览

## 关键技术

## 决策记录
"""

_MODE_LABEL = {"install": "安装", "update": "升级", "force": "覆盖安装"}

# AGENTS.md 入口指针（安装器维护）：供"不扫隐藏目录、无 orchd skill"的
# 新 agent 在宿主根直接发现引擎入口 .orchd/SKILL.md。
# task-v163-host-installer-pointers（T14）：指针由单段散文升级为 7 行**契约卡**，
# 并改为 marker 区段就地升级——旧版单行指针条目不得重复追加，区段外内容零改写。
_AGENTS_START_MARKER = "<!-- orchd:"
_AGENTS_END_MARKER = "<!-- /orchd -->"
_AGENTS_POINTER = "\n".join((
    "<!-- orchd: agent 入口指针（由 orchd 安装器维护；保留该标记以免重复追加） -->",
    "1. 进场先跑 `python .orchd/__main__.py status`，按返回的 `guidance` / "
    "`next_action` 执行，不自己判断阶段。",
    "2. 任务流：`request` → `claim --confirm` → 实现/提交 → `done` → `review`；"
    "不绕过 claim，不手写 git 写操作。",
    "3. 只读 `guidance.read` 点名的规则文件（入口协议 `.orchd/SKILL.md`）；"
    "stdout 是 JSON，stderr 是给人看的。",
    "4. 六条红线：不破坏 git、不越界改文件、不伪造身份/自审、不提交前中断、"
    "不手改运行时状态、不绕过门禁。",
    "5. `E033` → `session start --adopt`；`exit_type` 不是 `exec-command` 时"
    "按提示停/等/报，不猜。",
    "6. 审查须由本会话 spawn 的独立 subagent 执行：`done` 的 `review_handoff` "
    "已含审查会话与 `prompt`，原样转交，缺交接即停报。",
    "7. 身份三条禁止：自铸 session token 自用、手设 `ORCHD_SESSION_ID`、"
    "复用他人 token；**无 subagent 能力即中断**（`retract` 后停止上报）。",
    "<!-- /orchd -->",
)) + "\n"

# 客户端指针契约 C-CLIENT-POINTERS@1（task-v163-host-installer-pointers，T14）。
# 四个常见 agent 客户端入口文件，内容 = 仓库根同名样例的**LF 规范形态**（逐字节）。
# 默认关闭：仅 --client-pointers 开启时落盘；关闭时宿主根不新增任何文件。
# 换行固定为 LF（write_bytes 写契约字节），避免 Windows 文本模式把 `\n` 翻成 CRLF
# 破坏"逐字节一致"；git autocrlf 的工作树 CRLF 形态由 git 决定，安装器不参与。
_CLIENT_POINTER_LINE = "先读 `.orchd/SKILL.md`（协议入口），按其中的 `guidance` 执行。\n"
_CLIENT_POINTER_SPECS: tuple[tuple[str, str], ...] = (
    ("CLAUDE.md", "@AGENTS.md\n\n" + _CLIENT_POINTER_LINE),
    ("GEMINI.md", _CLIENT_POINTER_LINE),
    (".github/copilot-instructions.md", _CLIENT_POINTER_LINE),
    (
        ".cursor/rules/orchd.mdc",
        "---\ndescription: orchd 协议入口（always apply）\nalwaysApply: true\n---\n\n"
        + _CLIENT_POINTER_LINE,
    ),
)


def _enable_utf8_stdio() -> None:
    """Windows 控制台按 GBK 解码 UTF-8 中文会乱码，强制 stdio 重配为 UTF-8。

    task-installer-hygiene（F5）：此前仅重配 stdout，stderr 的中文
    （inventory 提示 / roadmap 处置 / ERROR 行）仍乱码——双流同配
    （参照 orchd/cli/skeleton.py 双流模式）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _shorten_list(items: list[str], limit: int = 10) -> str:
    """长清单截断为 top-N + 省略计数（task-installer-hygiene，F5）。

    参照 orchd/doctor.py [:10] + "..." 模式：人类可读输出只给前 N 个，
    全量数组仍保留在结构化返回里可审计——刷屏与可审计兼得。
    """
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f"...（等共 {len(items)} 个，全量见返回数组）"


def _find_first(candidates: list[Path]) -> Path:
    for p in candidates:
        if p.is_file():
            return p
    raise FileNotFoundError(f"缺失资源: {candidates[0]}")


def _resolve(src_name: str) -> Path:
    """解析资源源路径，兼容主项目(.orchd/rules)、orchd-core(skill/rules)与旧扁平(rules/)三种形态。"""
    p = RESOURCE_ROOT / src_name
    if src_name == "skill/rules":
        if not p.is_dir():
            p = RESOURCE_ROOT / ".orchd" / "rules"
        if not p.is_dir():
            p = RESOURCE_ROOT / "rules"
    return p


def _copy_tree(src: Path, dst: Path) -> None:
    """整目录拷贝（dst 已存在则先清空再拷贝，避免残留陈旧文件）。"""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _clean_pycache(root: Path) -> None:
    for pyc in root.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)


def _git_ls_files(host: Path) -> set[str] | None:
    """宿主 git 跟踪文件集合（相对宿主根的 posix 路径）；非 git 宿主 → None。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(host), "ls-files"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {
        ln.strip().replace("\\", "/")
        for ln in (proc.stdout or "").splitlines() if ln.strip()
    }


def _check_engine_inventory(host: Path, orchd: Path) -> dict:
    """入库完整性断言（task-vendored-engine-integrity）。

    背景（1.4.5 实踩）：安装器 ``.orchd/.gitignore``（``/*`` + 豁免集，不含
    ``orchd/``）使升级新增的引擎文件落盘即被忽略、永不入库；
    ``git worktree add`` 只带已跟踪文件 → 新 worktree 缺文件 → done 稳定 E999。

    判定（布局感知——flat 宿主不跟踪引擎完全合法，不得误伤）：
    - 非 git 宿主 → ``{"checked": False, "reason": "not_a_git_repo"}``；
    - 磁盘 ``.orchd/orchd/**/*.py`` 全被跟踪 → ``{"checked": True, "missing": []}``；
    - 差集非空 + container 布局（worktree 必用 git 检出，缺文件必断）→ 警告留痕
      （``missing`` + ``hint`` + ``warning``），**不阻断**；
    - 差集非空 + flat/未知布局 → 警告留痕（``missing`` + ``hint``），不阻断。

    task-installer-inventory-atomicity（诚实语义路线）：container 差集非空**不再抛错**。
    断言在 ``_assemble_assets`` 之后执行——此刻盘已改且无回滚，抛错只会被 CLI 兜底成
    ``{"installed": false}`` 而与磁盘矛盾（盘已完整升级、响应却报未安装）。改为返回
    结构化 ``missing``/``warning``/``hint``，使 ``--agent`` JSON 与磁盘真实状态一致。
    检出能力不削弱：``missing`` 仍完整列出未入库文件（负控见测试）。
    """
    engine = orchd / "orchd"
    disk = set()
    if engine.is_dir():
        for p in sorted(engine.rglob("*.py")):
            try:
                disk.add(p.relative_to(host).as_posix())
            except ValueError:
                continue
    tracked = _git_ls_files(host)
    if tracked is None:
        return {"checked": False, "reason": "not_a_git_repo"}
    missing = sorted(disk - tracked)
    if not missing:
        return {"checked": True, "missing": []}
    # task-inventory-honesty（F2）：flat/unknown 与 container 用两套文案——
    # flat 宿主不跟踪引擎完全合法（vendored 随安装器更新，不依赖 git 检出），
    # 旧文案复用 container 的“缺文件即 E999 + git add -f”纯属误导，且与
    # .orchd/.gitignore 的 /* 忽略契约自相冲突（-f 即覆盖本仓自立的忽略）。
    layout = "unknown"
    try:
        marker = orchd / ".layout.json"
        if marker.is_file():
            import json as _json

            layout = str(_json.loads(
                marker.read_text(encoding="utf-8")) .get("layout", "unknown"))
    except (OSError, ValueError):
        layout = "unknown"
    if layout == "container":
        hint = (
            "升级新增的引擎文件未入库（.orchd/.gitignore 覆盖了 orchd/）："
            "任务 worktree 是 git 纯净检出，缺文件即 done E999。请入库后提交："
            "git add -f .orchd/orchd/ ':(exclude)*__pycache__*'"
            "（POSIX sh/bash 用单引号；Windows cmd/PowerShell 改用双引号或"
            " --pathspec-from-file=<清单>）"
            "（-f 必需：忽略规则覆盖所致，非宿主失误）；切勿用无排除的整目录 add"
            "——会把 __pycache__/*.pyc 一并入库。未入库：%s"
            % _shorten_list(missing)
        )
        try:
            print(f"orchd ▸ [inventory] 提示：{hint}", file=sys.stderr)
        except OSError:
            pass
        return {"checked": True, "missing": missing,
                "warning": "container_layout_untracked_engine", "hint": hint}
    hint = (
        "flat/未知布局下 %d 个引擎文件在盘不在库，属合法状态，无需处理"
        "（flat 宿主不跟踪 .orchd/orchd/，引擎随安装器更新；切勿强行入库"
        "——那会覆盖本仓 .orchd/.gitignore 的忽略契约）：%s"
        % (len(missing), _shorten_list(missing))
    )
    try:
        print(f"orchd ▸ [inventory] 提示：{hint}", file=sys.stderr)
    except OSError:
        pass
    return {"checked": True, "missing": missing, "warning": "flat_layout_untracked_engine",
            "hint": hint}


def _copy_generated_docs(orchd: Path) -> list[str]:
    """把六份预生成快照放进 ``<host>/.orchd/docs/_generated/``。"""
    src = RESOURCE_ROOT / "docs" / "_generated"
    dst = orchd / "docs" / "_generated"
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in GENERATED_DOC_FILES:
        shutil.copy2(src / name, dst / name)
        copied.append(name)
    return copied


def _assemble_assets(orchd: Path) -> None:
    """组装分发资产（首次安装 / --update / --force 共用）。"""
    (orchd / "docs").mkdir(parents=True, exist_ok=True)

    # vendored 只读引擎
    _copy_tree(ENGINE_DIR, orchd / "orchd")
    # schema / templates 资源
    for src_name, dst_name in RESOURCE_DIRS:
        _copy_tree(_resolve(src_name), orchd / dst_name)
    # docs/ 单文档
    for sub, name in RESOURCE_FILES:
        shutil.copy2(RESOURCE_ROOT / sub / name, orchd / "docs" / name)
    # 契约文档预生成快照（宿主 docs check 只读校验）
    _copy_generated_docs(orchd)
    # 打包配置（.gitignore 为源码根形态，随后由 _write_orchd_gitignore 规范为
    # .orchd/ 相对规则——拷贝保持包一致性，覆写修正布局错位）
    for name in PACKAGING_FILES:
        shutil.copy2(RESOURCE_ROOT / name, orchd / name)
    # SKILL + 零根入口
    shutil.copy2(_find_first(SKILL_CANDIDATES), orchd / "SKILL.md")
    shutil.copy2(_find_first(LAUNCHER_CANDIDATES), orchd / "__main__.py")

    _clean_pycache(orchd)


def _planned_relpaths() -> list[str]:
    """安装器将写入 ``.orchd/`` 的相对路径全集（与 :func:`_assemble_assets` 同源）。

    用于两件事：``--update`` 覆盖前判断哪些 shipped 文件被宿主改过；安装完成后
    写清单记录 sha256。``__pycache__`` 不计入（组装后会被清理）。
    """
    rels: list[str] = []

    def collect(src: Path, prefix: str) -> None:
        if not src.is_dir():
            return
        for path in src.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                rels.append(f"{prefix}/{path.relative_to(src).as_posix()}")

    collect(ENGINE_DIR, "orchd")
    for src_name, dst_name in RESOURCE_DIRS:
        collect(_resolve(src_name), dst_name)
    rels.extend(f"docs/{name}" for _sub, name in RESOURCE_FILES)
    rels.extend(f"docs/_generated/{name}" for name in GENERATED_DOC_FILES)
    rels.extend(PACKAGING_FILES)
    rels.extend(["SKILL.md", "__main__.py", "VERSION"])
    return sorted(set(rels))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest_payload(orchd: Path) -> dict:
    """读上一次安装的清单全文；缺失/损坏/非对象视为空清单。"""
    path = orchd / INSTALL_MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_manifest(orchd: Path) -> dict[str, str]:
    """读上一次安装记录的 ``{相对路径: sha256}``；缺失/损坏视为无基线。"""
    files = _read_manifest_payload(orchd).get("files")
    return files if isinstance(files, dict) else {}


def _read_host_files(orchd: Path) -> dict[str, str]:
    """读清单里 **宿主根** 安装器自有文件的指纹（``host_files``）。

    task-v163-host-installer-pointers（T14）：仅记录安装器写过且内容等于契约的
    客户端指针文件（created/exists/updated），``kept_host_modified`` 不登记——
    否则下一轮会把宿主内容误判成"上轮安装写的"而覆盖。旧清单（无该键）→ ``{}``
    （无基线 = 保守不覆盖），与 ``_read_manifest`` 的旧宿主语义一致。
    """
    host_files = _read_manifest_payload(orchd).get("host_files")
    return host_files if isinstance(host_files, dict) else {}


def _write_manifest(
    orchd: Path, relpaths: list[str], host_files: dict[str, str] | None = None,
) -> None:
    """写安装清单：``files``（.orchd/ 内 shipped）+ ``host_files``（宿主根自有）。

    ``host_files`` 为**本轮结束时的最终指纹**（只含安装器自有文件），供下一轮
    ``--update`` 判定宿主是否改动过客户端指针。
    """
    files = {
        rel: _sha256_file(orchd / rel)
        for rel in relpaths
        if (orchd / rel).is_file()
    }
    payload = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "host_files": dict(sorted((host_files or {}).items())),
    }
    (orchd / INSTALL_MANIFEST).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup_modified_shipped(
    orchd: Path, relpaths: list[str], previous: dict[str, str],
) -> tuple[list[str], str | None]:
    """覆盖前把宿主改过的 shipped 文件备份到 ``.orchd/.doctor-backup/install-<UTC>/``。

    只认清单里登记过、且当前 sha256 与登记值不同的文件；无清单（首次安装或从
    旧版本升级的第一跑）不误判为「宿主改过」，只建立基线。
    """
    modified = [
        rel for rel in relpaths
        if (orchd / rel).is_file()
        and previous.get(rel) is not None
        and _sha256_file(orchd / rel) != previous[rel]
    ]
    if not modified:
        return [], None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = orchd / ".doctor-backup" / f"install-{stamp}"
    for rel in modified:
        target = backup_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(orchd / rel, target)
    return sorted(modified), str(backup_dir)


def _next_step_text(orchd: Path, mode: str) -> str:
    """安装下一步指引（task-intake-hint-phase）：按 ``_master.json`` 是否存在分流，
    与 ``orchd/guide.py::first_time_guide(has_master)`` 同语义——新项目走
    bootstrap → init，老项目（update 后 master 仍在）走 status / validate 正路。

    注：安装器独立运行、无 orchd 依赖，故为语义同源（非代码同源），由
    ``tests/test_intake_hint_phase.py`` 双边断言锁死一致（改一端即红）。
    """
    if (orchd / "_master.json").exists():
        base = ("python .orchd/__main__.py status 查看项目状态（或 validate 校验），"
                "随后 request/claim 领取任务")
    else:
        base = ("python .orchd/__main__.py bootstrap → init 初始化快照后开始使用"
                "（与 guidance first_time 卡片 steps 顺序一致）")
    # task-deps-host-gate：安装器不预置 deps.json，只提示宿主按需生成；
    # deps guard 在 guards.json 中默认 off，显式 warn/hard 才参与 done 门禁。
    # task-guards-init：安装器不预置 guards.json（避免替宿主做安全决策），
    # 只把最小可用配置的生成入口写进 hint。
    return base + (
        "；依赖真源可选：python .orchd/__main__.py deps init --root <宿主包目录>"
        "（安装器不写 deps.json；需要依赖门禁时在 .orchd/shared/guards.json"
        " 显式开启 deps 的 warn/hard，默认 off）"
        "；宿主 guard 可选：python .orchd/__main__.py guards init"
        "（生成 docs/deps/census 最小配置，默认 warn；不生成则全部 off）"
    )


def _install(
    host: Path, update: bool, force: bool, client_pointers: bool = False,
) -> dict:
    """按目标状态执行安装，返回结果字典。

    ``client_pointers`` 默认关闭（``--client-pointers`` 显式开启）：关闭时宿主根
    不新增任何客户端指针文件，也不创建其父目录。
    """
    orchd = host / ".orchd"
    modified: list[str] = []
    backup_dir: str | None = None
    # 上轮客户端指针基线：--force 为显式重建语义，不沿用旧基线（不覆盖宿主改动）。
    previous_host_files = {} if force else _read_host_files(orchd)

    if orchd.exists():
        if not update and not force:
            raise RuntimeError(
                "已安装，可用 --update 升级（保留宿主数据）或 --force 覆盖安装"
            )
        if force:
            # 覆盖安装：重建 .orchd/（覆盖全部，含用户数据；--force 为显式销毁语义）
            shutil.rmtree(orchd)
            _assemble_assets(orchd)
            mode = "force"
        else:
            # 就地升级：先按上次清单备份宿主改过的 shipped 文件，再覆盖分发资产。
            modified, backup_dir = _backup_modified_shipped(
                orchd, _planned_relpaths(), _read_manifest(orchd))
            _assemble_assets(orchd)
            mode = "update"
    else:
        _assemble_assets(orchd)
        mode = "install"

    # 骨架与忽略契约在三种模式下统一补齐（幂等：宿主已有内容不覆盖）
    # 注意：ROADMAP.md 落到宿主项目根（host/），IDEAS.md 仍在 .orchd/
    # roadmap 为 ROADMAP 处置记录（旧布局迁移 / 保留 / 模板）——结构化透出，无静默分支
    skeleton, roadmap = _mk_skeleton(orchd, host)
    gitignore = _write_orchd_gitignore(orchd)
    version = _write_engine_version(orchd)
    # AGENTS.md 契约卡与客户端指针先落盘，清单最后写（host_files 记最终指纹）。
    agents_entry = _ensure_agents_entry(host)
    if client_pointers:
        client_pointer_result = _ensure_client_pointers(host, previous_host_files)
        host_files = _client_pointer_fingerprints(
            host, client_pointer_result["files"])
    else:
        client_pointer_result = {"enabled": False, "files": {}}
        # 关闭不丢基线：下次开启 --client-pointers 仍能判定契约升级/宿主改动。
        host_files = dict(previous_host_files)
    # 清单必须最后写：.gitignore / VERSION 在此前刚被覆写，哈希以最终形态为准。
    _write_manifest(orchd, _planned_relpaths(), host_files)
    hooks_path = _ensure_repo_hooks(host)
    # 入库完整性断言（task-vendored-engine-integrity）：三种模式统一执行——
    # 新装即有差集同样是未来的 worktree 断点，不限于 --update。
    inventory = _check_engine_inventory(host, orchd)

    return {
        "installed": True,
        "mode": mode,
        "host": str(host),
        "orchd_dir": str(orchd),
        "agents_entry": agents_entry,
        "client_pointers": client_pointer_result,
        "hooks_path": hooks_path,
        "skeleton": skeleton,
        "roadmap": roadmap,
        "gitignore": gitignore,
        "version": version,
        "inventory": inventory,
        "modified_shipped_files": modified,
        "backup_dir": backup_dir,
        "next": _next_step_text(orchd, mode),
    }


def _mk_skeleton(orchd: Path, host: Path) -> tuple[dict, dict]:
    """创建工作区骨架（shared/ + proposals/ + 工作区文档模板），幂等不覆盖宿主内容。

    task-roadmap-installer-root-placement：ROADMAP.md 由安装器直建到**宿主项目根**
    （``host/``，ROADMAP 是宿主资产、唯一源，见
    ``orchd/ledger.py::resolve_roadmap_path``——与布局无关，flat 即仓库根、container
    即 ``<容器>/main/``）；IDEAS.md 仍建在 ``.orchd/`` 工作区文档根（引擎判定不变）。
    安装后 ``.orchd/`` 下**不生成** ROADMAP.md。已存在则不动——``--update`` 不覆盖宿主
    已写内容（``--force`` 因重建 ``.orchd/`` 而重新生成 .orchd/ 内模板，宿主根 ROADMAP
    同样受 ``exists`` 守护）。

    task-installer-legacy-roadmap-nonmask（第三轮审查 NEW3-2）：宿主根无文件时**不再
    无条件写空模板**——旧布局 ``.orchd/ROADMAP.md`` 存在时按 :func:`_roadmap_disposition`
    迁移其内容（宿主真实规划不再被空模板静默遮蔽），处置结果结构化返回，无静默分支。

    Returns:
        ``(docs, roadmap)``：``docs`` 形如
        ``{"IDEAS.md": "created"|"exists", "ROADMAP.md": <roadmap 的 status>,
        "CHANGELOG.md": ..., "docs/system-design.md": ...}``；
        ``roadmap`` 为结构化处置记录（``status``/``action``/``path``/``legacy``/``hint``），
        由 :func:`install` 透出为返回值同名字段，供 ``--agent`` 消费。
    """
    (orchd / "shared").mkdir(exist_ok=True)
    (orchd / "proposals").mkdir(exist_ok=True)
    docs = {}
    # IDEAS.md：工作区文档根（.orchd/，引擎判定不变）
    target = orchd / "IDEAS.md"
    if target.exists():
        docs["IDEAS.md"] = "exists"
    else:
        target.write_text(_IDEAS_TEMPLATE, encoding="utf-8")
        docs["IDEAS.md"] = "created"
    # ROADMAP.md：宿主项目根（唯一源，不在 .orchd/）——含旧布局处置决策
    roadmap_status, roadmap_record = _roadmap_disposition(host)
    docs["ROADMAP.md"] = roadmap_status
    # CHANGELOG.md：宿主项目根（已完成内容记录，纳入 git；只建骨架，已存在不动，
    # 不做 git add——入库决定权在宿主，沿用 ROADMAP 既定口径）。
    target = host / "CHANGELOG.md"
    if target.exists():
        docs["CHANGELOG.md"] = "exists"
    else:
        target.write_text(_CHANGELOG_TEMPLATE, encoding="utf-8")
        docs["CHANGELOG.md"] = "created"
    # docs/system-design.md：宿主 docs/ 目录（架构与关键技术描述，纳入 git）。
    docs_dir = host / "docs"
    target = docs_dir / "system-design.md"
    if target.exists():
        docs["docs/system-design.md"] = "exists"
    else:
        docs_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(_SYSTEM_DESIGN_TEMPLATE, encoding="utf-8")
        docs["docs/system-design.md"] = "created"
    return docs, roadmap_record


# 旧布局 ROADMAP 副本相对宿主根的路径（唯一源在宿主根，见 orchd/ledger.py）
_LEGACY_ROADMAP_SUBPATH = (".orchd", "ROADMAP.md")


def _log_roadmap_disposition(record: dict) -> None:
    """结构化 stderr 留痕（异常静默不阻断安装，与引擎侧 ``_log_legacy_roadmap`` 同型）。"""
    try:
        print(
            f"orchd ▸ [roadmap] {json.dumps(record, ensure_ascii=False)}",
            file=sys.stderr,
        )
    except Exception:
        pass


def _roadmap_disposition(host: Path) -> tuple[str, dict]:
    """决定宿主根 ROADMAP.md 的落位动作，并把处置过程结构化（禁静默分支）。

    task-installer-legacy-roadmap-nonmask（NEW3-2）定稿口径：**迁移**，不再写空模板。
    修复前 = 宿主根无文件时无条件写 :data:`_ROADMAP_TEMPLATE`；而旧布局
    ``.orchd/ROADMAP.md`` 可能是宿主真实规划，且 ``resolve_roadmap_path`` 不读旧位置
    （唯一源 = 宿主项目根）→ 真实规划被空模板**静默遮蔽**；叠加引擎侧旧判据
    （``legacy.exists() and not root.exists()``）被根文件存在性抑制，连留痕都会消失。

    分支（全部结构化返回 + 留痕，无静默路径）：
    - 宿主根已有文件 → ``exists``：内容零改动；旧副本仍在则额外标 ``legacy_copy_present``
      并留痕（不静默放过残留副本）；
    - 宿主根无文件 + 有旧副本 → ``migrated_from_legacy``：**字节级原样搬运**（不改写、
      不合并、不做模板化），宿主规划零丢失；
    - 宿主根无文件 + 旧副本不可读 → ``skipped_legacy_present``：**宁可不写也不遮蔽**，
      报错并要求人工确认；
    - 宿主根无文件 + 无旧副本 → ``created``：沿用模板（全新宿主，行为不变）。

    Returns:
        ``(status, record)``：status ∈ {``created``, ``migrated_from_legacy``,
        ``exists``, ``skipped_legacy_present``}。
    """
    target = host / "ROADMAP.md"
    legacy = host.joinpath(*_LEGACY_ROADMAP_SUBPATH)
    has_legacy = legacy.is_file()
    record: dict = {
        "path": str(target),
        "legacy": str(legacy) if has_legacy else None,
    }
    if target.exists():
        record["status"] = "exists"
        record["action"] = "kept_host_content"
        if has_legacy:
            # 不覆盖宿主根（既有语义），但残留旧副本必须可见（AC2：禁静默）
            record["action"] = "legacy_copy_present"
            record["hint"] = (
                "宿主根 ROADMAP.md 为唯一源、未被覆盖；.orchd/ROADMAP.md 为旧布局副本，"
                "确认根文件内容完整后可删除该副本"
            )
            _log_roadmap_disposition(record)
        return "exists", record
    if not has_legacy:
        target.write_text(_ROADMAP_TEMPLATE, encoding="utf-8")
        record["status"] = "created"
        record["action"] = "template_written"
        return "created", record
    try:
        payload = legacy.read_bytes()
    except OSError as exc:
        record["status"] = "skipped_legacy_present"
        record["action"] = "legacy_unreadable"
        record["error"] = str(exc)
        record["hint"] = (
            "旧布局 .orchd/ROADMAP.md 不可读：不写空模板（避免遮蔽宿主规划），"
            "请人工确认其内容后移到宿主项目根"
        )
        _log_roadmap_disposition(record)
        return "skipped_legacy_present", record
    # 字节级搬运：不做模板化改写 / 换行归一 / 内容合并，宿主规划原样成为唯一源
    target.write_bytes(payload)
    record["status"] = "migrated_from_legacy"
    record["action"] = "migrated_to_host_root"
    record["bytes"] = len(payload)
    record["hint"] = (
        "旧布局 ROADMAP 已按字节搬为唯一源（内容零改写）；请确认后 "
        "git add ROADMAP.md 入库（ROADMAP 属宿主资产）"
    )
    _log_roadmap_disposition(record)
    return "migrated_from_legacy", record


def _resolve_engine_version() -> str:
    """解析引擎源码版本（task-installer-hygiene，F1：版本可自证）。

    三级回退（best-effort，永不抛异常）：已安装分发元数据 →
    源码 git tag（``git describe --tags --always`` 于 RESOURCE_ROOT）→
    ``0.0.0.dev0``。调用方落盘为 ``.orchd/VERSION``（安装收据），
    ``--version`` 经该文件回退（见 orchd/cli/parser.py）。
    """
    try:
        import importlib.metadata as _md

        return _md.version("orchd")
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["git", "-C", str(RESOURCE_ROOT), "describe", "--tags", "--always"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        tag = (proc.stdout or "").strip()
        if proc.returncode == 0 and tag:
            return tag
    except Exception:
        pass
    return "0.0.0.dev0"


def _write_engine_version(orchd: Path) -> str:
    """落盘 ``.orchd/VERSION``（幂等覆写：版本收据恒为本次安装源版本）。

    Returns:
        本次解析到的版本字符串（同 ``_resolve_engine_version``）。
    """
    version = _resolve_engine_version()
    try:
        (orchd / "VERSION").write_text(version + "\n", encoding="utf-8")
    except OSError:
        pass
    return version


def _write_orchd_gitignore(orchd: Path) -> str:
    """把 .orchd/.gitignore 规范为**布局无关的相对规则**（幂等）。

    源码根 .gitignore 由 :data:`PACKAGING_FILES` 拷入 .orchd/（包一致性：发行源同步
    与 release 冒烟均按该文件存在校验），但其规则按「位于 git 根」编写，在 .orchd/
    内解析全部失效（错位契约）。此处覆写为相对规则，使同一套契约在 flat 与
    container 两种布局下等价生效——不改写宿主自有的 git 根 .gitignore（零侵入）。

    Returns:
        ``"written"`` 本次写入；``"exists"`` 已为规范形态（幂等不重写）。
    """
    target = orchd / ".gitignore"
    if target.exists():
        try:
            if target.read_text(encoding="utf-8") == _ORCHD_GITIGNORE:
                return "exists"
        except OSError:
            pass
    target.write_text(_ORCHD_GITIGNORE, encoding="utf-8")
    return "written"


def _ensure_agents_entry(host: Path) -> str:
    """确保宿主根 AGENTS.md 含 orchd 入口**契约卡**（marker 区段就地升级）。

    task-v163-host-installer-pointers（T14）：旧版是"整段散文 + 存在即跳过"，
    导致指针升级永远到不了宿主。现按 marker 区段处理，全程**字节级**读写
    （``read_bytes`` / ``write_bytes``，不经文本模式换行翻译），保证区段外内容
    byte-identical。

    Returns:
        - ``created``：无 AGENTS.md，直接写入契约卡；
        - ``appended``：有文件无开始标记，末尾追加一次（保留宿主已有内容）；
        - ``updated``：旧 marker 区段被替换为 7 行契约卡；
        - ``exists``：区段已是当前契约卡，不写文件；
        - ``malformed_marker``：有开始标记但缺结束标记（或次序颠倒）→ **不改写**
          文件，交由人工修复。
    """
    agents = host / "AGENTS.md"
    card = _AGENTS_POINTER.encode("utf-8")
    start_marker = _AGENTS_START_MARKER.encode("utf-8")
    end_marker = _AGENTS_END_MARKER.encode("utf-8")
    if not agents.exists():
        agents.write_bytes(card)
        return "created"
    data = agents.read_bytes()
    start = data.find(start_marker)
    if start < 0:
        # 追加点保持完整行边界：宿主末行不留半截（区段外字节只增加、不改写）。
        if data and not data.endswith(b"\n"):
            separator = b"\n\n"
        elif data.endswith(b"\n\n"):
            separator = b""
        elif data:
            separator = b"\n"
        else:
            separator = b""
        agents.write_bytes(data + separator + card)
        return "appended"
    end = data.find(end_marker, start)
    if end < 0:
        return "malformed_marker"
    section_end = end + len(end_marker)
    # 区段含结束标记所在行的行尾（CRLF/LF）：就地升级不累积空行，也不吞宿主后文。
    if data[section_end:section_end + 2] == b"\r\n":
        section_end += 2
    elif data[section_end:section_end + 1] == b"\n":
        section_end += 1
    # 换行归一后比较内容：宿主文件由 git autocrlf 检出的 CRLF 区段同样视为"已是新版"
    # （不改写文件、不制造混合换行）。
    if data[start:section_end].replace(b"\r\n", b"\n") == card:
        return "exists"
    agents.write_bytes(data[:start] + card + data[section_end:])
    return "updated"


def _ensure_client_pointers(host: Path, previous: dict[str, str]) -> dict:
    """写四个客户端一行指针文件（C-CLIENT-POINTERS@1），默认不调用（见 --client-pointers）。

    所有权判定（``previous`` = 上轮清单 ``host_files``，即安装器自有文件指纹）：

    - 文件不存在 → 写入契约字节，``created``；
    - 内容即契约 → ``exists``（不写文件）；
    - 内容不同 + 上轮指纹与当前一致 → 上轮安装所写、宿主未改（契约升级）→ 重写，
      ``updated``；
    - 内容不同 + 无基线 / 指纹不符 → 宿主自行维护 → **保持原样**，
      ``kept_host_modified``（绝不覆盖宿主内容）。

    父目录按需创建（``.github/`` / ``.cursor/rules/``），仅在真正写文件时创建。
    """
    files: dict[str, str] = {}
    for rel, content in _CLIENT_POINTER_SPECS:
        path = host / rel
        payload = content.encode("utf-8")
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            files[rel] = "created"
            continue
        actual = path.read_bytes()
        if actual == payload:
            files[rel] = "exists"
            continue
        recorded = previous.get(rel)
        if recorded is not None and _sha256_bytes(actual) == recorded:
            path.write_bytes(payload)
            files[rel] = "updated"
        else:
            files[rel] = "kept_host_modified"
    return {"enabled": True, "files": files}


def _client_pointer_fingerprints(host: Path, statuses: dict[str, str]) -> dict[str, str]:
    """本轮可进入 ``host_files`` 基线的指纹：仅安装器自有（内容必等于契约）的文件。

    ``kept_host_modified`` 不登记——登记会把宿主内容变成下一轮的"我方基线"，
    从而在 ``--update`` 时误覆盖宿主改动。
    """
    owned = {"created", "exists", "updated"}
    out: dict[str, str] = {}
    for rel, status in statuses.items():
        if status in owned and (host / rel).is_file():
            out[rel] = _sha256_file(host / rel)
    return out


# ------------------------------------------------------------------
# 仓库自带 hooks 的启用（task-decl-hooks-autoset）
# ------------------------------------------------------------------
# core.hooksPath 是**本地仓库配置**，不随 git clone 传播：新 clone 检出后仓库内
# .githooks/（pre-push 发版同步保护等）不生效，质量门禁形同虚设。安装流程据此
# 自动把它打开。决策表与 orchd/gitops/hook._ensure_hooks_path **同语义**（该函数是
# 引擎侧实现，供 claim 期 hook 安装复用）——安装器为纯标准库且不依赖 orchd 引擎
# （设计约束），故此处独立实现；两处若调整须同步。
_REPO_HOOKS_DIR = ".githooks"


def _git_config_get(host: Path, key: str) -> str | None:
    """读取 host 仓库的 git 配置值（未设置 / git 不可用 → None）。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(host), "config", "--get", key],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _is_absolute_hooks_path(value: str) -> bool:
    """hooksPath 是否为绝对路径（跨平台：POSIX 绝对或 Windows 盘符形态）。"""
    return Path(value).is_absolute() or bool(re.match(r"^[A-Za-z]:[/\\]", value))


def _ensure_repo_hooks(host: Path) -> dict:
    """确保宿主仓库自带的 ``.githooks/`` 被启用（幂等；不覆盖用户自定义 hooksPath）。

    写入值必须是**绝对路径**（与 ``orchd/gitops/hook._ensure_hooks_path`` 同语义）：
    git 把相对 ``core.hooksPath`` 按**当前工作树顶层**解析，``git worktree add`` 出的
    链接工作树里没有 ``.githooks/`` 时 git 静默跳过 hook（fail-open），仓库级
    ``pre-push`` 与 claim 期 L3 强制层在任务工作树内双双失效。实测（git 2.55）：
    相对值 → 主工作树执行 / 链接工作树不执行；绝对值 → 两者都执行。

    决策表（与 ``orchd/gitops/hook._ensure_hooks_path`` 同语义，永不抛异常）：

    - 非 git 仓库 → ``reason="not_a_git_repo"``，不动；
    - 仓库内无 ``.githooks/`` → ``reason="hooks_dir_missing"``，不动；
    - ``core.hooksPath`` 未设置 / 空 → 设为**绝对**目标目录，``reason="set"``；
    - 已指向同一目录且为绝对值 → 幂等不写，``reason="already_set"``；
    - 已指向同一目录但为相对值 → 升级为绝对值，``reason="upgraded_to_absolute"``；
    - 已为绝对值但目录不存在、末级名就是 ``.githooks``（仓库被移动/改名）→
      改写为当前绝对目标，``reason="reinstalled_stale"``；
    - 指向其他路径（用户显式自定义，如 ``.husky``）→ **不改写**，
      ``reason="custom_hooks_path"`` 并附 ``hint`` 提示手动改法。
    """
    try:
        if not (host / ".git").exists():
            return {"configured": False, "reason": "not_a_git_repo"}
        target = host / _REPO_HOOKS_DIR
        if not target.is_dir():
            return {"configured": False, "reason": "hooks_dir_missing",
                    "hooks_dir": _REPO_HOOKS_DIR}
        current = _git_config_get(host, "core.hooksPath")
        absolute_target = target.resolve().as_posix()

        def _write(value: str) -> tuple[bool, str]:
            proc = subprocess.run(
                ["git", "-C", str(host), "config", "core.hooksPath", value],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            return proc.returncode == 0, (proc.stderr or "").strip()

        if current is None:
            ok, err = _write(absolute_target)
            if not ok:
                return {"configured": False, "reason": "config_failed", "error": err}
            return {"configured": True, "reason": "set", "hooks_path": absolute_target}
        resolved = (Path(current) if _is_absolute_hooks_path(current)
                    else host / current)
        try:
            same = resolved.resolve() == target.resolve()
        except OSError:
            same = False
        if same:
            if _is_absolute_hooks_path(current):
                return {"configured": False, "reason": "already_set",
                        "hooks_path": current}
            # 相对等价值：目录对、可达性错 → 只改形态不改指向
            ok, err = _write(absolute_target)
            if not ok:
                return {"configured": False, "reason": "config_failed", "error": err}
            return {"configured": True, "reason": "upgraded_to_absolute",
                    "hooks_path": absolute_target, "previous": current}
        if (_is_absolute_hooks_path(current)
                and not Path(current).is_dir()
                and Path(current).name == _REPO_HOOKS_DIR):
            ok, err = _write(absolute_target)
            if not ok:
                return {"configured": False, "reason": "config_failed", "error": err}
            return {"configured": True, "reason": "reinstalled_stale",
                    "hooks_path": absolute_target, "previous": current}
        return {
            "configured": False,
            "reason": "custom_hooks_path",
            "hooks_path": current,
            "expected": absolute_target,
            "hint": (
                f"core.hooksPath 已自定义为 {current}（非仓库自带 {_REPO_HOOKS_DIR}）："
                f"保持不动、未改写；如需改为仓库自带 hooks，执行 "
                f"git config core.hooksPath {absolute_target}"
            ),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"configured": False, "reason": "error", "error": str(exc)}


def _force_rmtree(path: Path) -> None:
    """跨平台删除目录；Windows 上先清只读属性（git 对象文件只读会阻止 rmtree）。"""
    if sys.platform == "win32":
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files + dirs:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWRITE)
                except OSError:
                    pass
    shutil.rmtree(path)


def _cleanup_source() -> str:
    """--cleanup：安装成功后删除克隆源目录（orchd-core/），实现无痕安装。

    安全保护：仅当脚本位于克隆根（扁平布局，_SELF_DIR == RESOURCE_ROOT）且
    目录名为 orchd-core 时删除；主项目布局（release/install.py）或目录名
    不符时安全跳过，防止误删主项目源码。
    """
    if _SELF_DIR != RESOURCE_ROOT or RESOURCE_ROOT.name != "orchd-core":
        return "skipped"
    try:
        _force_rmtree(RESOURCE_ROOT)
        return "removed"
    except OSError:
        return "failed"


def main(argv: list[str] | None = None) -> int:
    _require_min_python()
    _enable_utf8_stdio()

    parser = argparse.ArgumentParser(
        prog="release/install.py",
        description="把 orchd-core 源码安装进宿主项目 .orchd/（纯标准库，v3 发布模型）",
    )
    parser.add_argument("host", help="宿主项目目录（安装目标）")
    parser.add_argument("--update", action="store_true",
                        help="已存在时就地升级（保留宿主 shared/、_master.json、台账与运行时文件）")
    parser.add_argument("--force", action="store_true",
                        help="已存在时覆盖安装（重建 .orchd/，覆盖全部）")
    parser.add_argument("--agent", action="store_true",
                        help="非交互：仅输出最终 JSON（installed/mode/host/orchd_dir/"
                             "agents_entry/client_pointers/next）")
    parser.add_argument("--client-pointers", action="store_true",
                        help="写宿主根四个 agent 客户端指针文件（C-CLIENT-POINTERS@1；"
                             "默认关闭，关闭时宿主根不新增任何文件）")
    parser.add_argument("--cleanup", action="store_true",
                        help="安装成功后删除克隆源目录（orchd-core/），实现无痕安装")
    args = parser.parse_args(argv)

    try:
        result = _install(
            Path(args.host), args.update, args.force, args.client_pointers)
        if args.cleanup:
            result["cleanup"] = _cleanup_source()
    except Exception as exc:  # noqa: BLE001 - 统一收敛为错误输出
        if args.agent:
            print(json.dumps(
                {"installed": False, "error": {"message": str(exc)}},
                ensure_ascii=False, indent=2,
            ))
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.agent:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        label = _MODE_LABEL[result["mode"]]
        print(f"[OK] orchd 已{label}到 {result['orchd_dir']}")
        hooks = result.get("hooks_path") or {}
        if hooks.get("reason") == "set":
            print(f"    已启用仓库自带 hooks：core.hooksPath={hooks.get('hooks_path')}")
        elif hooks.get("reason") == "custom_hooks_path":
            print(f"    提示：{hooks.get('hint')}")
        if result.get("agents_entry") == "malformed_marker":
            print("    提示：AGENTS.md 的 orchd 标记区段不完整（缺 <!-- /orchd -->），"
                  "未改写文件；请人工修复后重跑")
        pointers = result.get("client_pointers") or {}
        if pointers.get("enabled"):
            kept = [rel for rel, status in pointers.get("files", {}).items()
                    if status == "kept_host_modified"]
            if kept:
                print(f"    提示：客户端指针保持宿主原样（未覆盖）：{_shorten_list(kept)}")
        print(f"    下一步：{result['next']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())