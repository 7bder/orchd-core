"""gitops hook 域：pre-commit / reference-transaction hook 的路径、模板与安装。

域：templates（W6-C8 从 orchd/gitops/hook.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations


# hook 归属标记（owner-aware 判定）：生成物首部注释行。宿主自有（husky / lint-staged
# / 手写）的 pre-commit 不含该标记，引擎据此只覆盖 / 删除自己生成的 hook，
# 绝不改写宿主文件（R2-3）。
_HOOK_MARKER = "# orchd L3 pre-commit hook"

# 生成物 shebang（自检项之一）：hook 由 git 交给 shell 解释器直接执行，首行缺失
# shebang 会让 git 无法运行该 hook（事故形态：生成模板首行被误改 / 截断）。
_HOOK_SHEBANG = "#!/bin/sh"

# 归属标记只扫首部 N 行（生成物标记在第 2 行）：避免把正文里偶然提到标记串的
# 宿主 hook 误判为本引擎生成物。
_HOOK_MARKER_SCAN_LINES = 5

# 专属标记：归属判定仍复用 _HOOK_MARKER（owner-aware 语义与 pre-commit 一致），本标记
# 供回读自检与人工辨识「这是强制层而非提交层」。
_REF_TX_HOOK_MARKER = "# orchd ref-transaction hook (red line #1/#2 enforcement)"



# 运行时动态解析任务定义的内嵌 python（task-precommit-hook-multitask）。
# 由 hook 以 heredoc 方式调用：argv = [git_common_dir, task_id]，stdout 输出该任务
# 的 files_to_edit ∪ exempt_files（每行一个）；失败写 stderr 并返回非零。
# 用 % 格式化（而非 f-string），避免插入 hook 模板 f-string 时花括号被二次解析。
_HOOK_PY_RESOLVER = """\
import json
import os
import subprocess
import sys

# 强制 UTF-8 输出：hook 其余部分（MSYS sh echo）产出 UTF-8，若此处按 Windows
# locale(gbk) 写中文诊断会造成混合编码，git/调用方按 UTF-8 解码时失败。
# newline="\\n"（CRLF 回归）：Windows 文本模式会把 print 的 \\n 翻成 \\r\\n，shell
# 侧 while read 得到的路径带尾 \\r，与 staged 路径精确/前缀判定永不命中——只有
# 最后一行因命令替换吞掉尾部 CRLF 而幸免，表现为「允许列表里明明有却判越界」。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", newline="\\n")
    except Exception:
        pass

_common = (sys.argv[1] or ".git").replace("\\\\", "/")
_task = sys.argv[2]
_ref = sys.argv[3] if len(sys.argv) > 3 else ""
if _common == ".git":
    _root = "."
elif _common.endswith("/.git"):
    _root = _common[:-5] or "/"
else:
    _root = os.path.dirname(_common) or "."
_master = os.path.join(_root, ".orchd", "_master.json")


def _load_tasks(_text):
    _parsed = json.loads(_text)
    _tbl = _parsed.get("tasks") if isinstance(_parsed, dict) else None
    if not isinstance(_tbl, list):
        raise ValueError("tasks 表缺失")
    return _tbl


# task-hook-flat-resolver：权威来源 = default 分支 blob（git show <ref>:.orchd/_master.json）。
# flat 任务分支的物理 .orchd/_master.json 是 claim 时快照，main 上 amend 补登后陈旧，
# 读它会把已补登的合法文件误判越界（E020）。与命令侧
# orchd/worktree/layout.py::resolve_declaration_source 同源：blob 不可读 → 回退本地
# 检出 + degraded 留痕（不静默，也不整体放行）。
_tasks = None
if _ref:
    try:
        _proc = subprocess.run(
            ["git", "-C", _root, "show", _ref + ":.orchd/_master.json"],
            capture_output=True, encoding="utf-8", errors="replace",
        )
        if _proc.returncode == 0 and (_proc.stdout or "").strip():
            _tasks = _load_tasks(_proc.stdout)
        else:
            print(
                "[orchd E020] 动态解析降级：git show %s:.orchd/_master.json 不可读"
                "（%s）→ 回退本地检出" % (_ref, (_proc.stderr or "").strip()[:200]),
                file=sys.stderr,
            )
    except Exception as _exc:
        print(
            "[orchd E020] 动态解析降级：git show 异常（%s）→ 回退本地检出" % _exc,
            file=sys.stderr,
        )
if _tasks is None:
    try:
        with open(_master, encoding="utf-8") as _fh:
            _tasks = _load_tasks(_fh.read())
    except Exception as _exc:
        print("[orchd E020] 动态解析失败：无法读取 %s：%s" % (_master, _exc), file=sys.stderr)
        sys.exit(2)
_match = [t for t in _tasks if t.get("id") == _task]
if not _match:
    print("[orchd E020] 动态解析失败：%s 中无任务 %s" % (_master, _task), file=sys.stderr)
    sys.exit(3)
for _p in list(_match[0].get("files_to_edit") or []) + list(_match[0].get("exempt_files") or []):
    if _p:
        print(_p)
"""



# 无 jsonl 时查任务态/审查态的内嵌 python（task-hook-sqlite-python-fallback）。
# 由 hook 以 heredoc 方式调用：argv = [task_id, engine_dir]，stdout 输出两行
# （任务态 / 审查态，取值为事件类型、EMPTY 或 NO_LEDGER）；失败写 stderr 并
# 返回非零，shell 侧落回既有保守分支。file 部署走 grep 快路径，不经过此处。
# 三态语义（task-hook-no-ledger-triage，根因：EMPTY 与"无账本"不可混同——旧
# 实现无载体即保守拦截，回退若一律报 EMPTY 会把该保证冲成放行）：
# - 事件类型：载体中有该任务的最新相关事件；
# - EMPTY：载体存在但该任务无相关事件（非活跃 → 放行，与 grep 无命中同语义）；
# - NO_LEDGER：账本根无任何载体（jsonl / sqlite 库 / 归档均缺失；查询前判定，
#   不构造 Store、不建库）→ shell 保守按活跃处理（与旧无-ledger 分支同语义）。
# 约束（与 _HOOK_PY_RESOLVER 同源）：经 hook 模板 f-string 插入，故体内不得含
# 花括号（用 % 格式化）；TASK_ID 只经 argv 传递，不做模板插值（注入面）。
# 引擎导入不依赖 hook cwd（含 orchd 包与否）：先把安装期烘入的 engine_dir 插到
# sys.path 首位再 import（目录搬移/缺失则导入失败 → 非零退出 → 保守分支）。
_HOOK_PY_LEDGER_STATE = """\
import sys
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", newline="\\n")
    except Exception:
        pass
_baked = sys.argv[2] if len(sys.argv) > 2 else ""
if _baked and _baked not in sys.path:
    sys.path.insert(0, _baked)
try:
    from orchd.ledger import Store, resolve_store_dir
    from orchd.storage import ARCHIVE_FILENAME
    from pathlib import Path
except Exception as _exc:
    print("[orchd E020] ledger-state 查询不可用（引擎导入失败）：%s" % _exc, file=sys.stderr)
    sys.exit(4)
_task = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    _sdir = resolve_store_dir(Path(".orchd"))
    _has = False
    for _name in ("_ledger.jsonl", "_ledger.sqlite3", ARCHIVE_FILENAME):
        if (_sdir / _name).is_file():
            _has = True
            break
    if not _has:
        print("NO_LEDGER")
        print("NO_LEDGER")
        sys.exit(0)
    _store = Store(Path(".orchd"))
    _events = _store.backend.read_events()
except SystemExit:
    raise
except Exception as _exc:
    print("[orchd E020] ledger-state 查询失败：%s" % _exc, file=sys.stderr)
    sys.exit(5)
_task_types = ("CLAIMED", "REVIEW_CLAIMED", "DONE", "RETRACT", "REVIEW_SUBMITTED")
_review_types = ("REVIEW_CLAIMED", "REVIEW_SUBMITTED", "RETRACT")
_last_task = "EMPTY"
_last_review = "EMPTY"
for _ev in _events:
    if not isinstance(_ev, dict):
        continue
    if _ev.get("task_id") != _task:
        continue
    _t = _ev.get("type")
    if _t in _task_types:
        _last_task = _t
    if _t in _review_types:
        _last_review = _t
print(_last_task)
print(_last_review)
"""
