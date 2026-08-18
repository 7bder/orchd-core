#!/usr/bin/env python3
"""orchd 安装器：把 orchd-core 源码组装进宿主项目 .orchd/（纯标准库）。

发布模型 v3（task-121-installer）：orchd-core 是源码仓库，宿主项目通过本安装器
把 orchd-core 的引擎与资源"安装"到自身 .orchd/，形成自包含工作空间。

设计约束：
- 纯 Python 标准库（os / shutil / subprocess / argparse / json），无任何第三方依赖；
- 资源根 = 本脚本所在目录的父目录（orchd-core 源码根）；
- 安装器自身不依赖 orchd 引擎，也不依赖 .orchd/，可跨平台（Windows/macOS/Linux）运行；
- 首次安装：完整组装 .orchd/（vendored 引擎 + schema/templates/rules/docs + SKILL +
  零根入口 + 打包配置 + shared/proposals 工作区骨架），清 __pycache__；
- 已存在时：无 --update/--force → 非零退出并明确提示；--update 就地升级（保留宿主
  shared/、_master.json、IDEAS/ROADMAP、ledger/checkpoint、session 锁）；
  --force 覆盖安装（重建 .orchd/，覆盖全部）；
- --agent：仅输出最终 JSON（installed/mode/host/orchd_dir/next），无交互提示。

用法：
    python release/install.py <host> [--update] [--force] [--agent]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# 资源根：本脚本所在目录的父目录（orchd-core 源码根）
RESOURCE_ROOT = Path(__file__).resolve().parent.parent

# 组装进 .orchd/ 的内容清单（与 scripts/sync_orchd_core.sh / scripts/verify_release_self_contained.py 对齐）
ENGINE_DIR = RESOURCE_ROOT / "orchd"
RESOURCE_DIRS = [("schema", "schema"), ("templates", "templates"), (".orchd/rules", "rules")]
RESOURCE_FILES = [("docs", "decomposition-guide.md")]
PACKAGING_FILES = ["pyproject.toml", "MANIFEST.in", "LICENSE", ".gitignore"]
# SKILL / 零根入口归置 .orchd/（task-12-workspace-docs）；兼容旧根布局
SKILL_CANDIDATES = [RESOURCE_ROOT / ".orchd" / "SKILL.md", RESOURCE_ROOT / "SKILL.md"]
LAUNCHER_CANDIDATES = [RESOURCE_ROOT / ".orchd" / "__main__.py", RESOURCE_ROOT / "__main__.py"]

# 宿主用户数据（--update 时保留，不覆盖）
_USER_PATHS = {
    "shared", "proposals", "_master.json", "IDEAS.md", "ROADMAP.md",
    "IDEAS-archive.md", "_ledger.jsonl", "_checkpoint.json", ".session.lock",
}

_MODE_LABEL = {"install": "安装", "update": "升级", "force": "覆盖安装"}

# AGENTS.md 入口指针（安装器维护）：供"不扫隐藏目录、无 orchd skill"的
# 新 agent 在宿主根直接发现引擎入口 .orchd/SKILL.md。
_AGENTS_MARKER = "<!-- orchd: agent 入口指针"
_AGENTS_POINTER = """<!-- orchd: agent 入口指针（由 orchd 安装器维护；如需自定义请保留该标记以免重复追加） -->
# AI agents

本项目使用 [orchd](https://github.com/7bder/orchd-core) 编排 AI agent 任务协作。

- 每个 AI agent 进场请先读 `.orchd/SKILL.md`（协议入口，含纪律红线与 guidance 导航）
- 引擎命令统一用 `python .orchd/__main__.py <子命令>`
- 具体规则按需读 `.orchd/rules/`（索引 `rules/README.md`）
<!-- /orchd -->
"""


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
        _copy_tree(RESOURCE_ROOT / src_name, orchd / dst_name)
    # docs/ 单文档
    for sub, name in RESOURCE_FILES:
        shutil.copy2(RESOURCE_ROOT / sub / name, orchd / "docs" / name)
    # 打包配置
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
            _mk_skeleton(orchd)
            mode = "force"
        else:
            # 就地升级：覆盖分发资产，保留宿主用户数据
            _assemble_assets(orchd)
            mode = "update"
    else:
        _assemble_assets(orchd)
        _mk_skeleton(orchd)
        mode = "install"

    agents_entry = _ensure_agents_entry(host)

    return {
        "installed": True,
        "mode": mode,
        "host": str(host),
        "orchd_dir": str(orchd),
        "agents_entry": agents_entry,
        "next": "python .orchd/__main__.py bootstrap → init 初始化快照后开始使用（与 guidance first_time 卡片 steps 顺序一致）",
    }


def _mk_skeleton(orchd: Path) -> None:
    """创建工作区骨架（shared/ 共享上下文 + proposals/ 提案目录）。"""
    (orchd / "shared").mkdir(exist_ok=True)
    (orchd / "proposals").mkdir(exist_ok=True)


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
    args = parser.parse_args(argv)

    try:
        result = _install(Path(args.host), args.update, args.force)
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
        print(f"    下一步：{result['next']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())