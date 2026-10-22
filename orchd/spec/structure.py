"""orchd/spec/structure.py — `_master.json` 的结构校验域（W4-S2 拆分，纯搬移）。

来源：``orchd/spec/__init__.py`` 起点 bd808744 的 L57-289（@dataclass ``ValidationError`` /
``Master`` / ``load_master`` / ``_TASK_PHASE_CONTROL_CHARS`` /
``_check_declared_path_control_chars`` / ``_check_task_phase`` / ``validate_structure``），
拆分记录见 design/file-split-program-20261015.md §4.5.2。

依赖方向：structure.py → errors.py / spec.schema（另在 ``load_master`` 内**函数级惰性导入**
``orchd.registry``：保持调用点解析并避免与聚合入口形成加载期环）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchd.errors import ErrorCode
from orchd.spec.schema import _build_validator, spec_degraded_guard

@dataclass
class ValidationError:
    """单条校验错误。

    Attributes:
        code: 错误码，对应 errors.ErrorCode 中的枚举值（E003/E004/E005/E006）。
        path: 错误定位路径，采用 JSON Path 风格（如 ``$.tasks[0].depends_on[1]``），
              方便前端或日志系统直接定位到 _master.json 中的问题字段。
        message: 人类可读的错误描述。
    """

    code: ErrorCode
    path: str  # JSON Path 风格，如 "$.tasks[0].depends_on"
    message: str


@dataclass
class Master:
    """_master.json 解析后的数据对象。

    封装了从磁盘加载并 JSON 解析后的原始字典（``raw``）以及来源文件路径
    （``source_path``）。各子结构（project / modules / tasks / shared）通过
    ``@property`` 提供延迟访问，避免在不需要时产生额外拷贝。
    """

    raw: dict[str, Any]
    source_path: Path

    @property
    def project(self) -> dict[str, Any]:
        return self.raw.get("project", {})

    @property
    def modules(self) -> list[dict[str, Any]]:
        return self.raw.get("modules", [])

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return self.raw.get("tasks", [])

    @property
    def shared(self) -> dict[str, Any] | None:
        return self.raw.get("shared")

    @property
    def config(self) -> dict[str, Any]:
        """_master.json 顶层 config 段（引擎行为配置，1.1 起支持）。

        当前支持键：
        - ``importance``: derive_importance 阈值覆盖
          （critical/high/normal 三个下界，缺省键回退默认值）。
        """
        return self.raw.get("config", {})


def load_master(path: Path | str) -> Master:
    """加载 _master.json（经注册表抽象层，默认文件后端）。

    保留本入口以兼容既有调用点；实现委托 :func:`orchd.registry.load_registry`，
    切换后端（file / 未来 sqlite）不改调用语义。文件读取与 E001/E002 语义由
    文件后端（``orchd.registry.FileRegistryBackend``）保证。

    Raises:
        OrchdError E001: 文件不存在（由文件后端保证）。
        OrchdError E002: JSON 解析失败（含编码错误，由文件后端保证）。
    """
    from orchd.registry import load_registry

    return load_registry(path)


def _check_declared_path_control_chars(master: Master) -> list[ValidationError]:
    """声明路径控制字符检查（加法式，validate_structure 子步骤）。

    合法路径不可能包含控制字符（L3 pre-commit 钩子按行消费声明列表，
    换行路径可突破生成物注释行注入任意 shell 语句），故归入 E003 结构
    校验（阻断级，fail-closed）。纯 Python 实现，不依赖 jsonschema，
    缺库降级路径下仍执行（见 validate_structure）。
    """
    errors: list[ValidationError] = []
    for i, t in enumerate(master.tasks):
        tid = t.get("id", "")
        for decl_field in ("files_to_edit", "exempt_files"):
            for j, p in enumerate(t.get(decl_field) or []):
                if not isinstance(p, str):
                    continue
                for ch in ("\n", "\r", "\x00"):
                    if ch in p:
                        errors.append(
                            ValidationError(
                                code=ErrorCode.E003,
                                path=f"$.tasks[{i}].{decl_field}[{j}]",
                                message=(
                                    f"task '{tid}': {decl_field}[{j}] contains control "
                                    f"character {ch!r} — declared paths are consumed "
                                    "line-wise by the pre-commit hook "
                                    "(fail-closed, pass9 review F1)"
                                ),
                            ))
    return errors


#: 阶段值不允许出现的控制字符（与声明路径同族：门禁按行消费）。
#: \n/\r 是显式换行；\x00 是 NUL；\v(\x0b)/\f(\x0c) 也是 str.splitlines()
#: 的行边界，会让按行消费的门禁错位——同族风险一并兜底。
_TASK_PHASE_CONTROL_CHARS = ("\n", "\r", "\x00", "\v", "\f")


def _check_task_phase(master: Master) -> list[ValidationError]:
    """任务阶段字段（``tasks[].phase``）值域检查（加法式，接入点见下方）。

    字段非必填；一旦声明则不得为空串、纯空白或含控制字符（换行 / CR / NUL
    会让按行消费的门禁错位，与声明路径控制字符检查同族风险）。非字符串值
    交由 JSON Schema 判类型，本处只拦值域（与声明路径检查同口径）。

    纯 Python 实现、不依赖 jsonschema：schema 层在缺依赖时整体降级跳过
    （``validate_structure`` 的 ImportError 分支），故本函数是阶段值域的
    fail-closed 兜底——降级路径与正常路径**两处均挂载**，任一条路径下非法
    阶段值都会被拦（由 tests/test_spec.py 的降级用例锁定）。

    命名说明：引擎内「阶段」一词已被评审侧语义独占（见 review.py 与
    guide.py 的评审阶段变量），本函数及其局部变量一律以 ``task_`` 前缀与
    之物理隔离，由 tests/contract/test_phase_schema_field.py 用
    ``inspect.getsource`` 断言源码不含评审侧标识符（防后续漂移复用）。
    """
    errors: list[ValidationError] = []
    for i, task in enumerate(master.tasks):
        task_phase = task.get("phase")
        if task_phase is None:
            continue
        if not isinstance(task_phase, str):
            continue
        if not task_phase.strip():
            errors.append(
                ValidationError(
                    code=ErrorCode.E003,
                    path=f"$.tasks[{i}].phase",
                    message=(
                        f"task '{task.get('id', '')}': phase must not be empty or "
                        f"whitespace-only (got {task_phase!r}); omit the field "
                        f"entirely to opt out of phase gating"
                    ),
                ))
            continue
        for ch in _TASK_PHASE_CONTROL_CHARS:
            if ch in task_phase:
                errors.append(
                    ValidationError(
                        code=ErrorCode.E003,
                        path=f"$.tasks[{i}].phase",
                        message=(
                            f"task '{task.get('id', '')}': phase contains control "
                            f"character {ch!r} — phase values are consumed "
                            f"line-wise by gates (fail-closed)"
                        ),
                    ))
                break
    return errors


def validate_structure(
    master: Master,
    degraded_guards: list[dict[str, Any]] | None = None,
) -> list[ValidationError]:
    """基于 JSON Schema（Draft 2020-12）做结构校验（字段类型、必选/可选、枚举值）。

    根据 _master.json 中的 ``schema_version`` 字段决定加载哪个版本的 schema：
    优先查找 ``schema/v{version}/_master.schema.json``，不存在时回退到默认 schema。
    未知版本（当前支持 1..3，与 orchd/migrate.py 双边锁定）直接返回 E003，
    不静默回退（fail-closed，W1-③版本闸门）。
    返回 E003 错误列表；合法时返回空列表。

    加法式检查（task-pass9-hook-path-injection，pass9 评审 F1）：声明路径
    （files_to_edit / exempt_files）控制字符检查——L3 pre-commit 钩子对声明
    列表按行消费（静态列表与动态允许列表同为行式形态），声明含换行 / CR /
    NUL 会让行式消费错位，换行路径可突破生成物注释行注入任意 shell 语句。
    合法路径不可能包含控制字符，故归入 E003 结构校验（阻断级，fail-closed）。

    加法式检查之二（task-phase-schema-field）：任务阶段字段值域（空串 /
    纯空白 / 控制字符）——schema 层在缺依赖时整体降级，故该检查在降级与
    正常**两条路径均挂载**（见函数体内两处调用），保证 fail-closed。

    缺依赖降级（task-spec-lazy-import-degrade）：jsonschema 缺失时跳过
    schema 校验（控制字符检查仍执行），返回其错误列表（通常为空）；
    如传入 ``degraded_guards`` 列表则追加 :func:`spec_degraded_guard`
    条目（既有 degraded_guards 通道，E030，不新造字段）。依赖齐备时
    行为与此前逐字一致（E003 错误码零变化）。
    """
    version = master.raw.get("schema_version", 1)
    if not isinstance(version, int) or version not in (1, 2, 3):
        return [
            ValidationError(
                code=ErrorCode.E003,
                path="$.schema_version",
                message=(
                    f"unsupported schema_version: {version!r} "
                    f"(supported 1..3; "
                    f"run `orchd migrate` or upgrade engine)"
                ),
            )
        ]
    try:
        validator = _build_validator(version)
    except ImportError:
        if degraded_guards is not None:
            degraded_guards.append(spec_degraded_guard())
        return _check_declared_path_control_chars(master) + _check_task_phase(master)
    errors: list[ValidationError] = []
    for err in sorted(validator.iter_errors(master.raw),
                      key=lambda e: list(e.absolute_path)):
        json_path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                                  for p in err.absolute_path)
        # 从 $.tasks[i] 路径解析 task id，增强诊断信息
        tid = None
        for i, p in enumerate(err.absolute_path):
            if isinstance(p, int) and i + 1 < len(err.absolute_path):
                next_p = err.absolute_path[i + 1]
                if isinstance(next_p, str) and p < len(master.tasks):
                    tid = master.tasks[p].get("id")
                    break
        message = f"task '{tid}': {err.message}" if tid else err.message
        errors.append(
            ValidationError(
                code=ErrorCode.E003,
                path=json_path,
                message=message,
            ))
    # 声明路径控制字符检查（加法式，见 docstring）：非字符串交由上方 JSON
    # Schema 判定类型，这里只拦「合法路径不可能出现的控制字符」。
    errors.extend(_check_declared_path_control_chars(master))
    # 任务阶段字段值域检查（加法式，见 docstring）：两路径同挂载。
    errors.extend(_check_task_phase(master))
    return errors
