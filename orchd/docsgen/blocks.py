"""生成块协议（task-docsgen-gate）。

契约文档的生成区段用一对标记包裹::

    <!-- BEGIN GENERATED: <id> -->
    ...
    <!-- END GENERATED: <id> -->

本模块只做**纯文本**定位与替换：不读盘、不写盘、不起子进程。调用方（``orchd docs``
与 done 门禁）负责读产物、生成内容与写回。

协议要点：

* 单个块幂等替换：同一 ``id`` 的标记只允许出现一次，重复/未闭合一律 ``BlockError``；
* 生成区外内容逐字节不动（只替换 begin 标记末尾到 end 标记开头的区间）；
* 缺失标记时可显式 ``upsert``（追加新块），与既有 ``docs write`` 创建行为一致。
"""

from __future__ import annotations

import re

from orchd.docs_manifest import BLOCK_BEGIN, BLOCK_END

__all__ = (
    "BlockError",
    "extract_block",
    "has_block",
    "replace_block",
    "upsert_block",
    "validate_blocks",
)

_BEGIN_RE = re.compile(r"<!--\s*BEGIN GENERATED:\s*([^\s>]+)\s*-->")
_END_RE = re.compile(r"<!--\s*END GENERATED:\s*([^\s>]+)\s*-->")


class BlockError(ValueError):
    """生成块标记非法（``reason`` 可机读：duplicate_begin / duplicate_end / unclosed /
    missing / end_before_begin）。"""

    def __init__(self, artifact_id: str, reason: str) -> None:
        self.artifact_id = artifact_id
        self.reason = reason
        super().__init__(f"生成块标记非法（{artifact_id}）：{reason}")


def _locate(text: str, artifact_id: str) -> tuple[int, int, int, int] | None:
    """定位单块：返回 ``(begin_start, body_start, body_end, end_end)``。

    缺失 BEGIN 返回 ``None``（调用方决定按缺失还是按错误处理）；重复 BEGIN/END、
    BEGIN 后无 END、END 早于 BEGIN 一律抛 :class:`BlockError`。
    """
    begin = BLOCK_BEGIN.format(id=artifact_id)
    end = BLOCK_END.format(id=artifact_id)
    starts = [match.start() for match in re.finditer(re.escape(begin), text)]
    if not starts:
        return None
    if len(starts) > 1:
        raise BlockError(artifact_id, "duplicate_begin")
    begin_start = starts[0]
    body_start = begin_start + len(begin)
    body_end = text.find(end, body_start)
    if body_end < 0:
        raise BlockError(artifact_id, "unclosed")
    end_end = body_end + len(end)
    if text.find(end, end_end) >= 0:
        raise BlockError(artifact_id, "duplicate_end")
    if text.find(BLOCK_END.format(id=artifact_id), 0, begin_start) >= 0:
        raise BlockError(artifact_id, "end_before_begin")
    return begin_start, body_start, body_end, end_end


def has_block(text: str, artifact_id: str) -> bool:
    """产物是否含该 id 的合法生成块（非法标记同样抛 ``BlockError``）。"""
    return _locate(text, artifact_id) is not None


def extract_block(text: str, artifact_id: str) -> str | None:
    """取生成块内容（不含标记行；缺失 BEGIN 返回 ``None``）。"""
    located = _locate(text, artifact_id)
    if located is None:
        return None
    _, body_start, body_end, _ = located
    return text[body_start:body_end]


def replace_block(text: str, artifact_id: str, body: str) -> str:
    """幂等替换单个生成块（标记与区外内容逐字节不动）。"""
    located = _locate(text, artifact_id)
    if located is None:
        raise BlockError(artifact_id, "missing")
    _, body_start, body_end, _ = located
    return text[:body_start] + body + text[body_end:]


def upsert_block(text: str, artifact_id: str, body: str) -> str:
    """存在则替换，缺失则在文末追加（保持生成区外内容不变）。"""
    if _locate(text, artifact_id) is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        if _BEGIN_RE.search(text) or _END_RE.search(text):
            # 文本里已有其它生成块：新块前补空行，保持可读性（不影响字节契约以外的语义）。
            separator = "\n" if not text.endswith("\n\n") else ""
        return (
            text
            + separator
            + BLOCK_BEGIN.format(id=artifact_id)
            + body
            + BLOCK_END.format(id=artifact_id)
        )
    return replace_block(text, artifact_id, body)


def validate_blocks(text: str) -> list[str]:
    """全量扫描所有生成块，返回结构化问题列表（空 = 合法）。

    除重复/未闭合/次序错误外，还检测**跨 id 嵌套**：任一 ``BEGIN`` 出现在
    另一个尚未闭合的 ``BEGIN`` 之内，即记 ``nested_begin``（生成块不可嵌套）。
    """
    problems: list[str] = []
    begins: dict[str, int] = {}
    ends: dict[str, int] = {}
    begin_at: dict[str, int] = {}
    end_at: dict[str, int] = {}
    for match in _BEGIN_RE.finditer(text):
        artifact_id = match.group(1)
        begins[artifact_id] = begins.get(artifact_id, 0) + 1
        begin_at.setdefault(artifact_id, match.start())
    for match in _END_RE.finditer(text):
        artifact_id = match.group(1)
        ends[artifact_id] = ends.get(artifact_id, 0) + 1
        end_at.setdefault(artifact_id, match.start())
    for artifact_id in sorted(set(begins) | set(ends)):
        if begins.get(artifact_id, 0) > 1:
            problems.append(f"{artifact_id}: duplicate_begin")
        if ends.get(artifact_id, 0) > 1:
            problems.append(f"{artifact_id}: duplicate_end")
        if begins.get(artifact_id, 0) < 1:
            problems.append(f"{artifact_id}: missing_begin")
        if ends.get(artifact_id, 0) < 1:
            problems.append(f"{artifact_id}: unclosed")
        if (
            artifact_id in begin_at
            and artifact_id in end_at
            and end_at[artifact_id] < begin_at[artifact_id]
        ):
            problems.append(f"{artifact_id}: end_before_begin")
    for artifact_id in _nested_begin_ids(text):
        problems.append(f"{artifact_id}: nested_begin")
    return problems


def _nested_begin_ids(text: str) -> list[str]:
    """按出现顺序扫描标记，返回处于其它未闭合生成块内的 id（去重、排序）。"""
    events: list[tuple[int, str, str]] = []
    for match in _BEGIN_RE.finditer(text):
        events.append((match.start(), "begin", match.group(1)))
    for match in _END_RE.finditer(text):
        events.append((match.start(), "end", match.group(1)))
    stack: list[str] = []
    nested: set[str] = set()
    for _position, kind, artifact_id in sorted(events, key=lambda item: item[0]):
        if kind == "begin":
            if stack:
                nested.add(artifact_id)
            stack.append(artifact_id)
            continue
        if stack and stack[-1] == artifact_id:
            stack.pop()
        elif artifact_id in stack:
            # 交叉闭合（END 关闭非栈顶块）同样视为嵌套/错配。
            nested.add(artifact_id)
            stack.remove(artifact_id)
        elif stack:
            nested.add(artifact_id)
    return sorted(nested)
