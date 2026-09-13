#!/usr/bin/env python3
"""orchd 安装器：把 orchd-core 源码组装进宿主项目 .orchd/（纯标准库）。

发布模型 v3（task-121-installer）：orchd-core 是源码仓库，宿主项目通过本安装器
把 orchd-core 的引擎与资源"安装"到自身 .orchd/，形成自包含工作空间。

设计约束：
- 纯 Python 标准库（os / re / shutil / subprocess / argparse / json），无任何第三方依赖；
- 资源根 = 本脚本所在目录的父目录（orchd-core 源码根）；
- 安装器自身不依赖 orchd 引擎，也不依赖 .orchd/，可跨平台（Windows/macOS/Linux）运行；
- 首次安装：完整组装 .orchd/（vendored 引擎 + schema/templates/rules/docs + SKILL +
  零根入口 + 打包配置 + shared/proposals 工作区骨架），清 __pycache__；
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

# 组装进 .orchd/ 的内容清单（与 scripts/sync_orchd_core.sh / scripts/verify_release_self_contained.py 对齐）
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

若本目录尚无 `.orchd/`（新环境或尚未安装），可自行执行安装接入：

```bash
git clone https://github.com/7bder/orchd-core.git && python orchd-core/install.py . --agent --cleanup
```

装好后读 `.orchd/SKILL.md` 进入工作流。
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
    hooks_path = _ensure_repo_hooks(host)

    return {
        "installed": True,
        "mode": mode,
        "host": str(host),
        "orchd_dir": str(orchd),
        "agents_entry": agents_entry,
        "hooks_path": hooks_path,
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