"""amend 补丁字段段（_amend_patch_fields），依赖 declarations+patch_validation。"""

from __future__ import annotations

from pathlib import Path

from orchd.errors import ErrorCode, OrchdError
from .declarations import _decl_withdraw_branch_overlap, _log_decl_withdraw
from .patch_validation import _validate_additional_sources_patch, _validate_verify_timeout_patch


def _amend_patch_fields(*, args, master, store, project_root: Path,
                        revise_terminal: str | None) -> str | None:
    """amend 补丁段：CLI 补丁旗标解析 + 状态矩阵门 + 字段应用（原 L542-822）。

    声明域补登/撤回（并集/集合差）、AC 覆写仅 pending、brief/name 文本旗标（E038 的
    pending 出口）、reviewers/files_to_read 整体替换、verify_command 覆写、追加
    additional_sources 按 spec.validate_source 同口径校验（E025）；全部只改内存 master。
    状态矩阵拒绝码：amend_multi_field / amend_decl_conflict / ac_empty /
    ac_status_invalid / amend_text_empty / text_status_invalid / amend_decl_empty。
    返回 patch_task（落盘段据此判定 --task 路径是否需要写 master）。
    """
    # task-amend-decl-patch-channel：声明域 CLI 补登（--task + --files-to-edit /
    # --exempt-files / --verify-command）。列表类追加为并集语义；verify_command 为覆写。
    # task-decl-withdraw-channel（本任务）：新增撤回语义 --remove-files-to-edit /
    # --remove-exempt-files（集合差）——此前 CLI 表达不了删除，幽灵声明/幽灵豁免
    # 永久常驻（amend 只增不删）。撤回与追加同路径视为语义冲突，fail-fast（E007）。
    patch_task = getattr(args, "task", None)
    patch_files = list(getattr(args, "files_to_edit", None) or [])
    patch_exempt = list(getattr(args, "exempt_files", None) or [])
    patch_remove_files = list(
        getattr(args, "remove_files_to_edit", None) or [])
    patch_remove_exempt = list(
        getattr(args, "remove_exempt_files", None) or [])
    patch_verify = getattr(args, "verify_command", None)
    patch_sources = list(getattr(args, "additional_sources", None) or [])
    # task-verify-timeout-amend-channel：任务级 verify 预算补丁（default None = 不改）。
    patch_timeout = getattr(args, "verify_timeout_seconds", None)
    # task-spec-hygiene-flat-sweep AC3：reviewers / files_to_read 补登（覆写语义；
    # 名单/阅读域是整体替换，None=缺席不改，空列表=清空；两者本就在
    # schema_policy.amendable_fields 内，claimed/终态附加通道可补登）。
    patch_reviewers = getattr(args, "reviewers", None)
    patch_files_to_read = getattr(args, "files_to_read", None)
    # task-amend-ac-pending-channel：acceptance_criteria 覆写补丁（整体替换；
    # 显式 pending 校验见下，与白名单通道正交）。
    patch_ac = getattr(args, "acceptance_criteria", None)
    # task-amend-text-field-patch-channel：brief / name 文本旗标（单值覆写；
    # 状态矩阵见下方 gating 块——pending 直接改，终态经 --revise-terminal）。
    patch_brief = getattr(args, "brief", None)
    patch_name = getattr(args, "name", None)
    if patch_task is not None:
        if not (patch_files or patch_exempt or patch_remove_files
                or patch_remove_exempt or patch_verify is not None
                or patch_sources or patch_timeout is not None
                or patch_reviewers is not None
                or patch_files_to_read is not None
                or patch_ac is not None
                or patch_brief is not None
                or patch_name is not None):
            raise OrchdError(
                ErrorCode.E007,
                "amend --task 需至少携带一个补丁字段",
                [{
                    "reason": "amend_multi_field",
                    "task_id":
                    patch_task,
                    "hint":
                    "补登示例：--files-to-edit <file> / --exempt-files <file> / "
                    "--verify-command \"<cmd>\" / --verify-timeout-seconds <N> / "
                    "--additional-sources <ref> / --reviewers <id...> / "
                    "--files-to-read <path...> / "
                    "--acceptance-criteria \"<ac>\" ... / --brief \"<text>\" / "
                    "--name \"<text>\"；撤回示例："
                    "--remove-files-to-edit <file> / --remove-exempt-files <file>"
                    "（追加为并集、撤回为集合差，只增不删语义已由撤回通道补齐；"
                    "acceptance_criteria 为整体覆写且仅 pending 态放行；"
                    "brief/name 同为 pending 态直接放行，终态配 --revise-terminal）",
                }],
            )
        target = next(
            (t for t in master.tasks if t.get("id") == patch_task),
            None,
        )
        if target is None:
            raise OrchdError(
                ErrorCode.E005,
                f"task '{patch_task}' not found in master",
                [{
                    "task_id": patch_task,
                    "hint": f"任务 {patch_task} 在 _master.json 中不存在，检查 id 拼写或注册"
                }],
            )
        # 同一路径同时追加与撤回 → 语义不明（用户意图二义）→ fail-fast，不猜测
        _clash = sorted(set(patch_files) & set(patch_remove_files))
        _clash_ex = sorted(set(patch_exempt) & set(patch_remove_exempt))
        if _clash or _clash_ex:
            raise OrchdError(
                ErrorCode.E007,
                "amend_decl_conflict: 同一路径不能同时追加与撤回声明",
                [{
                    "reason": "amend_decl_conflict",
                    "task_id": patch_task,
                    "files_to_edit_clash": _clash,
                    "exempt_files_clash": _clash_ex,
                    "hint": "追加与撤回互斥：确认该路径应保留还是移除后，只带其中一个标志重试",
                }],
            )
        # task-amend-ac-pending-channel AC1/AC2：acceptance_criteria 覆写补丁。
        # 语义为整体覆写（与 --reviewers / --files-to-read 一致）；空值拒绝而非
        # 清空（AC 必填下限 1 条，见 rules/intake.md 粒度下限）；仅 pending 态
        # 放行，claimed / done / in_review 一律 E007（hint 沿用
        # split._ac_field_routing_hint，终态仍走 --revise-terminal）。状态判定走
        # 独立旗标 + 显式 pending 校验，与白名单通道正交——不得把
        # acceptance_criteria 写进 x-amendable / amendable_fields()，否则
        # split.amend 的 claimed 白名单判定会连带放行 AC，打穿 E007 矩阵。
        if patch_ac is not None:
            from orchd.spec import _GRANULARITY_MAX_AC
            from orchd.split import _ac_field_routing_hint

            if (not patch_ac or not any(
                    isinstance(a, str) and a.strip() for a in patch_ac)):
                raise OrchdError(
                    ErrorCode.E007,
                    "amend_ac_empty: --acceptance-criteria 至少须给 1 条非空 AC"
                    "（AC 必填下限 1 条，见 rules/intake.md 粒度下限；"
                    "空值拒绝而非清空）",
                    [{
                        "reason": "ac_empty",
                        "task_id":
                        patch_task,
                        "hint":
                        ("acceptance_criteria 为整体覆写：请给出完整的新 AC 列表，"
                         "至少 1 条、不超过 6 条（E029 粒度锚点）；"
                         "确需清空请走用户裁决逃生口，而非空列表覆写"),
                    }],
                )
            _ac_state = store.replay().get(patch_task)
            _ac_status = (_ac_state.status
                          if _ac_state is not None else "pending")
            if _ac_status != "pending":
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: 任务 {patch_task} 当前为 {_ac_status} 态，"
                    "--acceptance-criteria 仅 pending 态放行",
                    [{
                        "reason": "ac_status_invalid",
                        "task_id": patch_task,
                        "status": _ac_status,
                        "hint": _ac_field_routing_hint(
                            _ac_status, {"acceptance_criteria"}),
                    }],
                )
            if len(patch_ac) > _GRANULARITY_MAX_AC:
                raise OrchdError(
                    ErrorCode.E029,
                    f"granularity_overflow: task '{patch_task}' "
                    f"acceptance_criteria 数量 {len(patch_ac)} 超过粒度锚点 "
                    f"{_GRANULARITY_MAX_AC}（E029，拒绝注册）",
                    [{
                        "task_id": patch_task,
                        "count": len(patch_ac),
                        "limit": _GRANULARITY_MAX_AC,
                        "hint": ("AC 超过 6 条说明任务内聚性不足：请先拆分任务，"
                                 "或删减合并至 6 条以内后重试"),
                    }],
                )
            target["acceptance_criteria"] = list(patch_ac)
        # task-amend-text-field-patch-channel：brief / name 文本旗标（E038 的
        # pending 出口）。状态矩阵与 --acceptance-criteria 通道同构（同一
        # _ac_field_routing_hint 生成器，不另写文案）：pending 直接改；
        # claimed/done/in_review E007 并给既有出路；终态按现规则——不带
        # --revise-terminal 拒绝（hint 指向 revise-terminal），带
        # --revise-terminal 且指向本任务时放行（brief/name 本就在
        # _TERMINAL_TEXT_REVISABLE_FIELDS，--reason 必填校验沿用既有通道，
        # AMEND 审计 reason=terminal_spec_revision）。不进
        # schema x-amendable（否则 claimed 白名单连带放行，打穿 E007 矩阵）。
        _text_patches = {
            "brief": patch_brief,
            "name": patch_name,
        }
        _active_text = {k: v for k, v in _text_patches.items() if v is not None}
        if _active_text:
            from orchd.split import _ac_field_routing_hint

            _empty = sorted(k for k, v in _active_text.items()
                            if not str(v).strip())
            if _empty:
                raise OrchdError(
                    ErrorCode.E007,
                    "amend_text_empty: --brief/--name 须为非空文本"
                    "（schema minLength=1；空值拒绝而非清空）",
                    [{
                        "reason": "amend_text_empty",
                        "task_id": patch_task,
                        "fields": _empty,
                        "hint": "文本字段不接受空串/纯空白；确需清空属删除语义，"
                                "不在本通道（无用户裁决不删必填字段）",
                    }],
                )
            _txt_state = store.replay().get(patch_task)
            _txt_status = (_txt_state.status
                           if _txt_state is not None else "pending")
            if _txt_status != "pending" and revise_terminal != patch_task:
                raise OrchdError(
                    ErrorCode.E007,
                    f"invalid_state: 任务 {patch_task} 当前为 {_txt_status} 态，"
                    "--brief/--name 仅 pending 态直接放行（终态须配 "
                    "--revise-terminal + --reason 走纯文本修订）",
                    [{
                        "reason": "text_status_invalid",
                        "task_id": patch_task,
                        "status": _txt_status,
                        "hint": _ac_field_routing_hint(_txt_status,
                                                       set(_active_text)),
                    }],
                )
            target.update(_active_text)
        withdrawn: list[dict[str, str]] = []
        not_present: list[dict[str, str]] = []
        cur_files = set(target.get("files_to_edit", []))
        cur_exempt = set(target.get("exempt_files", []) or [])
        next_files = sorted((cur_files | set(patch_files)) -
                            set(patch_remove_files))
        next_exempt = sorted((cur_exempt | set(patch_exempt)) -
                             set(patch_remove_exempt))
        # schema 要求 files_to_edit 非空：撤回唯一声明会让任务无声明域 → fail-fast
        #（不落盘、不留半成品；整任务作废应走 force-status/retract 而非清空声明）
        if (patch_files or patch_remove_files) and not next_files:
            raise OrchdError(
                ErrorCode.E007,
                "amend_decl_empty: files_to_edit 撤回后为空（schema 要求非空）",
                [{
                    "reason": "amend_decl_empty",
                    "task_id":
                    patch_task,
                    "removed":
                    sorted(set(patch_remove_files)),
                    "declared_before":
                    sorted(cur_files),
                    "hint":
                    "每个任务至少须保留一个 files_to_edit 声明；若整任务作废，"
                    "请用 force-status / retract 处置，而非清空声明域",
                }],
            )
        if patch_files or patch_remove_files:
            not_present += [{
                "field": "files_to_edit",
                "path": p
            } for p in sorted(set(patch_remove_files) - cur_files)]
            withdrawn += [{
                "field": "files_to_edit",
                "path": p
            } for p in sorted(set(patch_remove_files) & cur_files)]
            target["files_to_edit"] = next_files
        if patch_exempt or patch_remove_exempt:
            not_present += [{
                "field": "exempt_files",
                "path": p
            } for p in sorted(set(patch_remove_exempt) - cur_exempt)]
            withdrawn += [{
                "field": "exempt_files",
                "path": p
            } for p in sorted(set(patch_remove_exempt) & cur_exempt)]
            target["exempt_files"] = next_exempt
        # task-amend-additional-sources-field：additional_sources 补登（并集追加、
        # 只增不删，与 files_to_edit 补登一致）。写入前按 spec.validate_source
        # 同口径校验补丁引用（_validate_additional_sources_patch），非法引用
        # E025 报错且不写入。
        if patch_sources:
            _validate_additional_sources_patch(
                patch_task, patch_sources, project_root)
            cur_sources = set(target.get("additional_sources", []) or [])
            target["additional_sources"] = sorted(
                cur_sources | set(patch_sources))
        if patch_verify is not None:
            target["verify_command"] = patch_verify
        if patch_reviewers is not None:
            # AC3：reviewers 整体替换（空列表即清空；schema 允空，身份模型已不依赖名单）。
            target["reviewers"] = list(patch_reviewers)
        if patch_files_to_read is not None:
            # AC3：files_to_read 整体替换；CLI 路径形态按 priority=reference 登记
            # （must_read 需走 --register 提案注册，避免 CLI 随手抬高阅读强度）。
            target["files_to_read"] = [
                {"path": p, "priority": "reference"} for p in patch_files_to_read
            ]
        if patch_timeout is not None:
            # task-verify-timeout-amend-channel：任务级 verify 预算通道（默认 120s 不变）。
            # 与 --verify-command 同属 patch 语义（可同一次调用组合生效）；claimed /
            # done / in_review 状态均可 patch——`schema_policy.amendable_fields`
            # 为单一事实源，该字段已在白名单内（前置校验在写盘之前完成）。
            target["verify_timeout_seconds"] = _validate_verify_timeout_patch(
                patch_task, patch_timeout)
        if withdrawn or not_present:
            # 撤回留痕（stderr，不受 ORCHD_QUIET 抑制）：撤回若影响仍在分支 diff 中
            # 的路径，会让 review 期 E010 声明完整性反向告警 → 显式提示而非静默
            overlap = _decl_withdraw_branch_overlap(
                project_root, patch_task, [w["path"] for w in withdrawn])
            _log_decl_withdraw(patch_task, withdrawn, not_present, overlap)
        # task-amend-patch-write-after-validate：此处仅改内存 master，不落盘。
        # 后续 dry-run 预计算与 amend() 均以内存对象为准（无任何回读文件），落盘
        # 推迟到 amend() 成功之后——失败路径（E007/E025/E028）不留脏 master，
        # 杜绝"失败 amend 污染声明域并致后续 amend 死锁"。

    return patch_task
