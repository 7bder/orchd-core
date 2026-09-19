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
- --cleanup：安装成功后删除克隆源目录（orchd-core/），实现无痕安装（仅当脚本位于
  克隆根且目录名为 orchd-core 时删除，主项目布局安全跳过）。

用法：
    python release/install.py <host> [--update] [--force] [--agent] [--cleanup]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# 资源根：本脚本所在目录的父目录（orchd-core 源码根）。
# 自适应两种布局：主项目内脚本位于 release/ 子目录（资源根在 parent.parent），
# 发布到 orchd-core 后脚本位于根目录（扁平布局，资源根即 parent）。
_SELF_DIR = Path(__file__).resolve().parent
RESOURCE_ROOT = _SELF_DIR if (_SELF_DIR / "orchd").is_dir() else _SELF_DIR.parent

# 组装进 .orchd/ 的内容清单
# （与 scripts/sync_orchd_core.sh / scripts/verify_release_self_contained.py 对齐）
ENGINE_DIR = RESOURCE_ROOT / "orchd"
RESOURCE_DIRS = [("schema", "schema"), ("templates", "templates"), ("skill/rules", "rules")]
RESOURCE_FILES = [("docs", "decomposition-guide.md")]
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
> 新版本先 `orchd roadmap-land` 落地为 IDEAS pending 条目，再走摄入注册为任务。
"""

_MODE_LABEL = {"install": "安装", "update": "升级", "force": "覆盖安装"}

# AGENTS.md 入口指针（安装器维护）：供"不扫隐藏目录、无 orchd skill"的
# 新 agent 在宿主根直接发现引擎入口 .orchd/SKILL.md。
_AGENTS_MARKER = "<!-- orchd: agent 入口指针"
_AGENTS_POINTER = (
    """<!-- orchd: agent 入口指针（由 orchd 安装器维护；如需自定义请保留该标记以免重复追加） -->
# AI agents

本项目使用 [orchd](https://github.com/7bder/orchd-core) 编排 AI agent 任务协作。

- 每个 AI agent 进场请先读 `.orchd/SKILL.md`（协议入口，含纪律红线与 guidance 导航）
- 引擎命令统一用 `python .orchd/__main__.py <子命令>`
- 具体规则按需读 `.orchd/rules/`（索引 `rules/README.md`）

若本目录尚无 `.orchd/`（新环境或尚未安装），可自行执行安装接入：

```bash
git clone https://github.com/7bder/orchd-core.git"""
    """ && python orchd-core/install.py . --agent --cleanup
```

装好后读 `.orchd/SKILL.md` 进入工作流。
<!-- /orchd -->
"""
)


def _enable_utf8_stdio() -> None:
    """Windows 控制台按 GBK 解码 UTF-8 中文会乱码，强制 stdout 重配为 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


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
    # 打包配置（.gitignore 为源码根形态，随后由 _write_orchd_gitignore 规范为
    # .orchd/ 相对规则——拷贝保持包一致性，覆写修正布局错位）
    for name in PACKAGING_FILES:
        shutil.copy2(RESOURCE_ROOT / name, orchd / name)
    # SKILL + 零根入口
    shutil.copy2(_find_first(SKILL_CANDIDATES), orchd / "SKILL.md")
    shutil.copy2(_find_first(LAUNCHER_CANDIDATES), orchd / "__main__.py")

    _clean_pycache(orchd)


def _install(host: Path, update: bool, force: bool) -> dict:
    """按目标状态执行安装，返回结果字典。"""
    orchd = host / ".orchd"

    if orchd.exists():
        if not update and not force:
            raise RuntimeError(
                "已安装，可用 --update 升级（保留宿主数据）或 --force 覆盖安装"
            )
        if force:
            # 覆盖安装：重建 .orchd/（覆盖全部，含用户数据）
            shutil.rmtree(orchd)
            _assemble_assets(orchd)
            mode = "force"
        else:
            # 就地升级：覆盖分发资产，保留宿主用户数据
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

    agents_entry = _ensure_agents_entry(host)
    hooks_path = _ensure_repo_hooks(host)

    return {
        "installed": True,
        "mode": mode,
        "host": str(host),
        "orchd_dir": str(orchd),
        "agents_entry": agents_entry,
        "hooks_path": hooks_path,
        "skeleton": skeleton,
        "roadmap": roadmap,
        "gitignore": gitignore,
        "next": (
            "python .orchd/__main__.py bootstrap → init 初始化快照后开始使用"
            "（与 guidance first_time 卡片 steps 顺序一致）"
        ),
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
        ``{"IDEAS.md": "created"|"exists", "ROADMAP.md": <roadmap 的 status>}``；
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
    """确保宿主根 AGENTS.md 含 orchd 入口指针（幂等）。

    供"不扫隐藏目录、无 orchd skill"的新 agent 进场后能直接发现引擎入口
    ``.orchd/SKILL.md``。安装/升级/覆盖每次运行都会确保其存在：
    - 无 AGENTS.md → 新建（created）
    - 有但无 orchd 标记 → 末尾追加（appended，保留宿主已有内容）
    - 已有 orchd 标记 → 不动（exists，幂等）
    """
    agents = host / "AGENTS.md"
    if agents.exists():
        text = agents.read_text(encoding="utf-8")
        if _AGENTS_MARKER in text:
            return "exists"
        with agents.open("a", encoding="utf-8") as f:
            f.write("\n\n" + _AGENTS_POINTER)
        return "appended"
    agents.write_text(_AGENTS_POINTER, encoding="utf-8")
    return "created"


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

    决策表（与 ``orchd/gitops/hook._ensure_hooks_path`` 同语义，永不抛异常）：

    - 非 git 仓库 → ``reason="not_a_git_repo"``，不动；
    - 仓库内无 ``.githooks/`` → ``reason="hooks_dir_missing"``，不动；
    - ``core.hooksPath`` 未设置 / 空 → 设为 ``.githooks``（相对仓库根），``reason="set"``；
    - 已指向同一目录（相对或等价绝对路径）→ 幂等不写，``reason="already_set"``；
    - 指向其他路径（用户显式自定义）→ **不改写**，``reason="custom_hooks_path"``
      并附 ``hint`` 提示手动改法。
    """
    try:
        if not (host / ".git").exists():
            return {"configured": False, "reason": "not_a_git_repo"}
        target = host / _REPO_HOOKS_DIR
        if not target.is_dir():
            return {"configured": False, "reason": "hooks_dir_missing",
                    "hooks_dir": _REPO_HOOKS_DIR}
        current = _git_config_get(host, "core.hooksPath")
        if current is None:
            proc = subprocess.run(
                ["git", "-C", str(host), "config", "core.hooksPath", _REPO_HOOKS_DIR],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                return {"configured": False, "reason": "config_failed",
                        "error": (proc.stderr or "").strip()}
            return {"configured": True, "reason": "set", "hooks_path": _REPO_HOOKS_DIR}
        resolved = (Path(current) if _is_absolute_hooks_path(current)
                    else host / current)
        try:
            same = resolved.resolve() == target.resolve()
        except OSError:
            same = False
        if same:
            return {"configured": False, "reason": "already_set", "hooks_path": current}
        return {
            "configured": False,
            "reason": "custom_hooks_path",
            "hooks_path": current,
            "expected": _REPO_HOOKS_DIR,
            "hint": (
                f"core.hooksPath 已自定义为 {current}（非仓库自带 {_REPO_HOOKS_DIR}）："
                f"保持不动、未改写；如需改为仓库自带 hooks，执行 "
                f"git config core.hooksPath {_REPO_HOOKS_DIR}"
            ),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"configured": False, "reason": "error", "error": str(exc)}


def _force_rmtree(path: Path) -> None:
    """跨平台删除目录；Windows 上先清只读属性（git 对象文件只读会阻止 rmtree）。"""
    if os.name == "nt":
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
                        help="非交互：仅输出最终 JSON（installed/mode/host/orchd_dir/next）")
    parser.add_argument("--cleanup", action="store_true",
                        help="安装成功后删除克隆源目录（orchd-core/），实现无痕安装")
    args = parser.parse_args(argv)

    try:
        result = _install(Path(args.host), args.update, args.force)
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
        print(f"    下一步：{result['next']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())