"""master 耐久落盘序（_persist_master_durable/_fsync_file_durable），叶域。"""

from __future__ import annotations

import os
from pathlib import Path

from orchd.ledger import _atomic_replace, _fsync_dir


def _persist_master_durable(master_path: Path, payload: str) -> None:
    """``_master.json`` 落盘的硬化序（task-master-write-hardening-and-replace-registry AC1）。

    与账本同档（``orchd/storage/filesystem.py`` / ``orchd/ledger.py`` 既有范式）：
    ``tmp.write_text → fsync(tmp) → _atomic_replace → _fsync_dir(父目录)``。

    为什么原子的 rename 还不够：rename 只保证「要么旧的、要么新的」，不保证新内容已
    落到设备。断电/崩溃窗口里目录项可持久而数据块未刷 ⇒ 670 任务的唯一账本重启后可能
    零长。第二档收益来自 ``_atomic_replace`` 本身：并发只读命令持有 ``_master.json``
    句柄时 ``os.replace`` 抛 WinError 5/32，旧实现无重试，一次争用即以 E999 逃逸，而
    此刻写锁仍在原子区内、提案草稿尚未消费（消费点绑在落盘之后）。

    ``fsync`` 独立成函数是有意的可测点：用例既能在其上断言「刷盘先于 rename」的顺序
    （顺序错则断电窗口照旧），又能在包装里继续调用真实现而不退化成 mock——真刷盘仍发生。

    文本形态（``indent=2`` / ``ensure_ascii=False`` / 无尾换行）由调用方给定，本函数
    不碰格式；改名保留 ``.<pid>.tmp`` 形态（task-sqlite-robustness-batch：与
    ledger 原子写同口径，多进程各写各的 tmp，replace 原子决胜；崩溃残留经
    doctor 运行时谓词在变更检测中忽略，git 侧本就被 ``.orchd/*`` 忽略）。
    """
    tmp = master_path.with_name(f"{master_path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    _fsync_file_durable(tmp)
    _atomic_replace(tmp, master_path)
    _fsync_dir(master_path.parent)


def _fsync_file_durable(path: Path) -> None:
    """把已写完的 tmp 内容刷到设备（``r+b`` 打开只为拿到可 fsync 的写句柄）。"""
    with open(path, "r+b") as fh:
        fh.flush()
        os.fsync(fh.fileno())
