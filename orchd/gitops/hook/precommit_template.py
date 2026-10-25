"""gitops hook 域：pre-commit / reference-transaction hook 的路径、模板与安装。

域：precommit_template（W6-C8 从 orchd/gitops/hook.py 纯搬移，函数体零改动）。
"""

from __future__ import annotations




# F3（task-split2-w3-3-hook-install-template）：pre-commit hook 模板外置——
# 原 hook_install 体内的 369 行 f-string 按行区间原样切出，仅把 17 个插值点
# 换成 .format 字段（字面量 {{ / }} 在 f-string 与 format 模板中同义，逐字未改）。
# 刻意**留在本文件内**（不拆独立数据文件）：tests/test_guide.py 通道 D 以
# hook.py 文本的 echo/print 行做 E017/E020 可达性扫描——外置成 .sh 数据文件会
# 让该通道静默失明，且引入包数据/安装面改动（越出 §2.1 纯搬移口径）。
_PRE_COMMIT_HOOK_TEMPLATE = """#!/bin/sh
{HOOK_MARKER} (auto-generated, do not edit)
# D2（debug-pack D）：GBK 控制台中文输出编码补全（python 段已 reconfigure(utf-8)，
# shell 段此前未覆盖）——best-effort，有 chcp.com 则置 65001，无则静默。
if command -v chcp.com >/dev/null 2>&1; then chcp.com 65001 >/dev/null 2>&1 || true; fi
# 解释器与 staged 精确消费（task-hook-staged-nul-rootfix）：含换行的文件名需要
#   NUL 分隔 + `read -d ''` 才能不被拆——`read -d` 是 **bash 内建**，POSIX sh
#   （dash / ash）不支持。此处**保留 `#!/bin/sh` + 运行时分流**，而非把 shebang
#   改成 `#!/usr/bin/env bash`：
#     · 无 bash 的宿主（如 alpine 容器）下，改 shebang 会让 hook 无法执行 →
#       git 直接拒绝该次提交（把整个提交链路拦死，比降级更糟）；
#     · Windows 上 `env bash` 还会受 PATH 中其它 bash 发行版（WSL 等）遮蔽，
#       解析到非 msys bash 时路径语义（/c/ vs /mnt/c/）不兼容。
#   bash → NUL 精确消费（本任务根治目标）；非 bash → 退回按行消费并**留痕**
#   （仅保留「含换行文件名会被拆」这一已知限制，不静默、也不整体放行）。

# 运行时动态识别当前任务：当前分支 task/<id> → canonical 主工作树 .orchd/_master.json
#   动态解析该任务 files_to_edit ∪ exempt_files（不再依赖生成时快照）；
#   无 task/<id> 分支时回退绑定任务与静态列表（既有 best-effort 语义 + 留痕）。
# Bound task: {TASK_ID}
# Bound allowed files:
{FILES_LIST}

# 账本路径（task-hook-ledger-container-fix）：安装期按布局解析共享账本根并烘绝对路径
# （container → <容器>/.orchd-runtime/_ledger.jsonl；flat → <repo>/.orchd/_ledger.jsonl），
# 运行时对 ORCHD_HOME / 相对默认值兜底。旧实现硬编码相对 ".orchd/_ledger.jsonl"，
# container 任务 worktree 下账本副本不存在 → 恒落「无 ledger」，使 E020 越界拦截与
# R1-b 审查冻结整体静默 fail-open（报告 P1-2/D-1）。
LEDGER={LEDGER_Q}
if [ ! -f "$LEDGER" ]; then
    if [ -n "${{{ORCHD_HOME_ENV}:-}}" ] && [ -f "$ORCHD_HOME/_ledger.jsonl" ]; then
        LEDGER="$ORCHD_HOME/_ledger.jsonl"
    elif [ -f ".orchd/_ledger.jsonl" ]; then
        LEDGER=".orchd/_ledger.jsonl"
    fi
fi

# 0) 当前分支 → 任务 id（动态识别；无匹配分支回退绑定任务并留痕）
# BOUND_TASK：安装期绑定任务（静态列表的归属）。3a 快路径仅在
# “分支任务 == 绑定任务”时生效——不一致时静态列表与动态真值无关，
# 预检命中会误放行（fail-open，2026-09-18 全量回归实证）。
# 反解与 orchd/line.py::parse_task_branch 同源：id 字符集不含斜杠，
# 故末段恒为完整 id（shell 长前缀删除形态，删至最后一个斜杠）；单线 task/<id>
# 下与旧切片同值。
BOUND_TASK={BOUND_TASK}
BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
case "$BRANCH" in
    task/*)
        TASK_ID="${{BRANCH##*/}}"
        SCOPE_MODE=dynamic
        ;;
    "")
        TASK_ID={BOUND_TASK}
        SCOPE_MODE=bound
        echo "[orchd E020] 非 git 环境 / 无法解析当前分支 → 回退绑定任务 {TASK_ID}（best-effort）" >&2
        ;;
    *)
        TASK_ID={BOUND_TASK}
        SCOPE_MODE=bound
        echo "[orchd E020] 分支 '$BRANCH' 非 task/<id> 或 task/<line>/<id> → 回退绑定任务 {TASK_ID}（best-effort）" >&2
        ;;
esac

# 1) 任务未活跃 → 放行：读 ledger 判该任务是否处于活跃状态
#    （最近事件为 CLAIMED / REVIEW_CLAIMED，且无后续 DONE / RETRACT /
#      REVIEW_SUBMITTED）——in_review 阶段任务同样活跃（审查中，实现者
#      仍可能补提交，需拦截越界）
if [ -f "$LEDGER" ]; then
    LAST_TASK=$(grep -F "\\"task_id\\":\\"$TASK_ID\\"" "$LEDGER" 2>/dev/null | grep -E '"type":"(CLAIMED|REVIEW_CLAIMED|DONE|RETRACT|REVIEW_SUBMITTED)"' | tail -1)
    case "$LAST_TASK" in
        *CLAIMED*|*REVIEW_CLAIMED*)
            # 任务活跃，继续校验
            ;;
        *)
            # 任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行
            exit 0
            ;;
    esac
else
    # 无 jsonl 文件（sqlite 部署 / 异常环境）：python 回退查 Store
    # （task-hook-sqlite-python-fallback）。file 部署走上面的 grep 快路径，
    # 此处只在 jsonl 缺失时执行；判定语义（活跃→继续 / 非活跃→放行）与 grep
    # 路径逐字一致。解释器探针与第 4 步动态解析同模式；查询失败（无解释器 /
    # 引擎不可导入 / Store 异常）→ 沿旧语义保守按活跃处理并留痕。
    _LS_OK=no
    _PY_TASK=EMPTY
    _PY_REVIEW=EMPTY
    ORCHD_PY_LS=""
    for _CAND in python python3 py; do
        if command -v "$_CAND" >/dev/null 2>&1; then
            ORCHD_PY_LS="$_CAND"
            break
        fi
    done
    if [ -z "$ORCHD_PY_LS" ]; then
        echo "[orchd E020] ledger 未找到（$LEDGER）且无可用 python → 保守按任务活跃处理（继续范围校验）" >&2
    else
        _LS_RAW=$("$ORCHD_PY_LS" - "$TASK_ID" {ENGINE_Q} <<'ORCHD_HOOK_LEDGER'
{PY_LEDGER_STATE}ORCHD_HOOK_LEDGER
)
        if [ $? -ne 0 ]; then
            echo "[orchd E020] ledger-state 回退查询失败 → 保守按任务活跃处理（继续范围校验）" >&2
            echo "[orchd E020] 若因引擎目录搬移/改名导致安装期烘入路径失效：请重建 hook" >&2
            echo "[orchd E020]   修复：python .orchd/__main__.py doctor --fix" >&2
            echo "[orchd E020]   逃生（须自行留痕可审计）：git commit --no-verify；绕过后在任务 notes 记录理由" >&2
        else
            _PY_TASK=$(printf '%s\\n' "$_LS_RAW" | sed -n '1p' | tr -d '\\r')
            _PY_REVIEW=$(printf '%s\\n' "$_LS_RAW" | sed -n '2p' | tr -d '\\r')
            if [ -z "$_PY_TASK" ]; then _PY_TASK=EMPTY; fi
            if [ -z "$_PY_REVIEW" ]; then _PY_REVIEW=EMPTY; fi
            # NO_LEDGER（账本无载体：无 jsonl/db/归档）≠ EMPTY（载体在但任务无事件）：
            # 前者沿旧语义保守按活跃处理（fail-closed），后者按非活跃放行。
            if [ "$_PY_TASK" = "NO_LEDGER" ] || [ "$_PY_REVIEW" = "NO_LEDGER" ]; then
                echo "[orchd E020] ledger 未找到（$LEDGER）且账本无载体 → 保守按任务活跃处理（继续范围校验）" >&2
            else
                _LS_OK=yes
            fi
        fi
    fi
    if [ "$_LS_OK" = "yes" ]; then
        case "$_PY_TASK" in
            *CLAIMED*|*REVIEW_CLAIMED*)
                # 任务活跃，继续校验（与 grep 路径同语义）
                ;;
            *)
                # 任务未活跃（无 CLAIMED/REVIEW_CLAIMED，或已 DONE/RETRACT/REVIEW_SUBMITTED）→ 放行
                exit 0
                ;;
        esac
    fi
fi

# 2) R1-b 审查期实现者冻结：任务分支上最后 review 事件 REVIEW_CLAIMED
#    （审查进行中）→ 拒绝提交，保护审查基线（任何分支，保留既有契约）。
#    debug-pack B2：**引擎内部提交身份豁免**——staged 全为引擎固定资产
#    （_master.json / IDEAS.md / ROADMAP.md / _full_regression.json）时视为引擎
#    自身 amend/intake/full-regression 提交，不施加冻结；人为提交含任何非固定
#    资产文件仍冻结（负控），越界另由第 4 步范围校验拦截。
_ORCHD_ENGINE_ONLY=yes
_ORCHD_E_STAGED=$(git diff --cached --name-only --no-renames 2>/dev/null)
if [ -z "$_ORCHD_E_STAGED" ]; then
    _ORCHD_ENGINE_ONLY=no
else
    for _ORCHD_E_FILE in $_ORCHD_E_STAGED; do
        case "$_ORCHD_E_FILE" in
            .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md|.orchd/_full_regression.json) ;;
            *) _ORCHD_ENGINE_ONLY=no ;;
        esac
    done
fi
if [ "$_ORCHD_ENGINE_ONLY" != "yes" ]; then
    # 2) R1-b 审查期实现者冻结：任务分支上最后 review 事件是 REVIEW_CLAIMED
    #    （审查进行中，无后续 REVIEW_SUBMITTED / RETRACT）→ 拒绝提交，保护审查基线。
    if [ -f "$LEDGER" ]; then
        LAST_REVIEW=$(grep -F "\\"task_id\\":\\"$TASK_ID\\"" "$LEDGER" 2>/dev/null | grep -E '"type":"(REVIEW_CLAIMED|REVIEW_SUBMITTED|RETRACT)"' | tail -1)
        case "$LAST_REVIEW" in
            *REVIEW_CLAIMED*)
                echo "orchd E017: review in progress, commit blocked on $BRANCH"
                echo "任务正在审查中（REVIEW_CLAIMED）。请等待 reviewer 提交结论，或先执行 retract 撤回审查再提交。"
                echo "To bypass: git commit --no-verify"
                exit 1
                ;;
        esac
    else
        # 无 jsonl 且 python 回退成功时，用回退查到的审查态做同等冻结判定
        # （task-hook-sqlite-python-fallback）；回退不可用时沿旧语义跳过
        # （步骤 1 已保守按活跃处理并留痕）。
        if [ "$_LS_OK" = "yes" ]; then
            case "$_PY_REVIEW" in
                *REVIEW_CLAIMED*)
                    echo "orchd E017: review in progress, commit blocked on $BRANCH"
                    echo "任务正在审查中（REVIEW_CLAIMED）。请等待 reviewer 提交结论，或先执行 retract 撤回审查再提交。"
                    echo "To bypass: git commit --no-verify"
                    exit 1
                    ;;
            esac
        fi
    fi
fi

# 3) 获取 staged 文件列表（相对路径）
# task-decl-hook-delete-parity：与 done 侧 (_guard_out_of_scope → _git_diff_names)
# 对齐 diff 口径——去掉 --diff-filter=ACM，使删除(D)/重命名/复制等改动状态的全部
# staged 路径都进校验集。此前仅 ACM 会导致「删除范围外文件」在提交层零拦截，
# 只能到 done 期才被 E010 拦下（红线 #3 提交层结构性缺口）。done 侧
# _git_diff_names 无 --diff-filter（含 D），--name-only 对 R 默认显示目标路径；
# 此处同义（无 --no-renames），两侧对同一 git 改动得到同一路径集合。
# task-rename-declaration-deadlock-fix：两侧同加 --no-renames，重命名展开为
# 删（旧路径）+ 增（新路径)——声明旧路径者走删除态口径（提交删除态后保持声明），
# 新路径按新增判定；折叠口径下旧路径 path_not_found 死锁（done 侧实证）。
# R2-9a：`-z` 不可省略——不加时 core.quotePath（默认 true）把非 ASCII / 含特殊
# 字符路径转义为八进制引号串，再被 for 按空白拆词，合法 in-scope 文件被误拦 E020
# （对照 orchd/gitops/cleanup.py::unmerged_paths 的 -z 写法）。
# task-hook-staged-nul-rootfix：NUL 分隔集合**原样落临时文件**，下方按 NUL 逐项
# 消费——不再 `tr '\\0' '\\n'` 转行（转行后含换行的文件名会被拆成两项，in-scope
# 误拦 E020）。落文件而非管道：管道开子 shell，循环内累积的 OUT_OF_SCOPE 会随之
# 丢弃；重定向在当前 shell 内消费。临时文件用后即删（trap EXIT，含异常路径）。
STAGED_TMP="${{TMPDIR:-/tmp}}/orchd-hook-staged-$$"
ALLOWED_TMP="${{TMPDIR:-/tmp}}/orchd-hook-allowed-$$"
trap 'rm -f "$STAGED_TMP" "$ALLOWED_TMP"' EXIT
trap 'exit 130' INT TERM HUP
if ! git diff --cached --name-only --no-renames -z > "$STAGED_TMP" 2>/dev/null; then
    echo "[orchd E020] staged 列表获取失败（git diff 异常）→ best-effort 放行（未校验范围，禁止静默）" >&2
    exit 0
fi

# 无 staged 文件 → 放行
if [ ! -s "$STAGED_TMP" ]; then
    exit 0
fi

# staged 集合装进位置参数（3a 预检自包含装载：与第 4 步装载逐字同构，
# 独立一份——test_hook_owner_aware._revert_staged_parsing 按锚点删除
# [trap,3b) 区间复原修复前形态，3a 整体落在该区间内、无需额外适配；
# 第 4 步装载保持原位不动，反形态断言零感知）。
# 以 **NUL** 为唯一分隔符——git -z 的输出即 NUL 分隔，只有 NUL 能无歧义表达
# 「含空格 / 含换行 / 非 ASCII」的路径（沿革见第 4 步注释：R2-9a 空白拆词修、
# task-hook-staged-nul-rootfix 换行拆分修）。不用 here-doc + read（文件名即
# 注入面：here-doc 体做参数/命令替换二次展开）；不用管道 + read（子 shell 丢
# 累积量）。非 bash（dash/ash 无 `read -d`）退回按行消费并留痕。
if [ -n "${{BASH_VERSION:-}}" ]; then
    set --
    while IFS= read -r -d '' _ORCHD_ITEM; do
        set -- "$@" "$_ORCHD_ITEM"
    done < "$STAGED_TMP"
else
    echo "[orchd E020] 当前解释器非 bash（不支持 read -d ''）：staged 集合按行消费（含换行的文件名会被拆分，已知限制）" >&2
    _ORCHD_OLD_IFS=$IFS
    IFS='
'
    set -f
    set -- $(tr '\\0' '\\n' < "$STAGED_TMP")
    set +f
    IFS=$_ORCHD_OLD_IFS
fi

# 3a) 静态预检快路径（task-hook-static-precheck）：仅 dynamic 模式**且分支任务
#     == 绑定任务**。staged 全被 bound 静态列表覆盖 → 跳过 python 动态解析
#     （省一次解释器启动），静态列表落盘供第 4 步动态分支消费（判定仍走动态
#     循环——注入面守卫与历史负控制的锚点不可绕过）。
#     任一 staged 未覆盖 → 保持 dynamic 全量解析（声明可能已 amend 补登，
#     动态为准）。分支任务 ≠ 绑定任务时禁用预检：静态列表归属绑定任务，与
#     当前分支的动态真值无关，命中即误放行（fail-open）。
#     固定资产同样先行覆盖（与第 4 步 case 同表），否则每个含固定资产的提交
#     都回退动态解析，快路径永不命中。
#     task-hook-flat-resolver 收界：静态列表是 claim 时快照，动态真源现为 default
#     分支 blob；claimed 任务 amend 只增不删（split.py 约束），故 static ⊆ blob，
#     静态命中 ⟹ blob 亦放行，快路径不与 dynamic 基线分裂（补登文件不在静态列表
#     → _SC_MISS → 回落 dynamic/blob 判定）。
_SC_SKIP_RESOLVER=no
if [ "$SCOPE_MODE" = "dynamic" ] && [ "$TASK_ID" = "$BOUND_TASK" ]; then
    _SC_MISS=""
    for _SC_FILE in "$@"; do
        _SC_IN=no
        case "$_SC_FILE" in
            .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md|.orchd/_full_regression.json)
                _SC_IN=yes
                ;;
        esac
{STATIC_PRECHECK_BODY}
        if [ "$_SC_IN" != "yes" ]; then _SC_MISS=yes; fi
    done
{STATIC_HIT_ACTION}
fi

# 3b) 正常路径：动态解析任务允许列表（仅 task/<id> 分支）；失败不静默放行
ALLOWED=""
if [ "$SCOPE_MODE" = "dynamic" ] && [ "$_SC_SKIP_RESOLVER" != "yes" ]; then
    COMMON=$(git rev-parse --git-common-dir 2>/dev/null)
    # R2-9c：解释器按可用性解析（POSIX 常见 python3，Windows/MSYS 常见 python/py），
    # 不再硬编码 python——旧实现下解释器缺失即整段动态读取失败，只能降级放行。
    ORCHD_PY=""
    for _CAND in python python3 py; do
        if command -v "$_CAND" >/dev/null 2>&1; then
            ORCHD_PY="$_CAND"
            break
        fi
    done
    if [ -z "$ORCHD_PY" ]; then
        echo "[orchd E020] 动态读取任务定义失败（task=$TASK_ID）：未找到可用 python 解释器" >&2
        echo "[orchd E020] （python / python3 / py 均不可用）→ best-effort 放行（未校验范围，禁止静默）" >&2
        exit 0
    fi
    _ALLOWED_RAW=$("$ORCHD_PY" - "$COMMON" "$TASK_ID" {DEFAULT_REF_Q} <<'ORCHD_HOOK_PY'
{PY_RESOLVER}ORCHD_HOOK_PY
)
    if [ $? -ne 0 ]; then
        echo "[orchd E020] 动态读取任务定义失败（task=$TASK_ID）→ best-effort 放行（未校验范围，禁止静默）" >&2
        exit 0
    fi
    # CRLF 兜底（Windows）：resolver 已按 LF 输出，但若宿主解释器不支持
    # reconfigure(newline) 仍有 CR 残留——命令替换只吞行尾 CRLF、行内 CR 会保留，
    # 会让带尾 CR 的允许路径与 staged 路径永不相等（in-scope 提交被误拦 E020）。
    # 此处统一剔除 CR；退出码判定仍基于 resolver 本身（不改写失败语义）。
    ALLOWED=$(printf '%s\\n' "$_ALLOWED_RAW" | tr -d '\\r')
    # 允许列表落临时文件（**不展开**形态）：此前用未加引号的 here-doc（`done <<EOF`
    # + `$ALLOWED`）消费——该形态**实测不会**对展开结果二次求值（POSIX：here-doc 体只
    # 做一趟参数 / 命令替换，终止符在展开前匹配，bash 5.3 与 dash 双解释器已验证），
    # 但把「不做二次展开」押在「展开趟数」这一实现细节上很脆弱：一旦改成 eval 或未加
    # 引号的词拆分，文件名立刻变成注入面。改为 printf 写临时文件 + 重定向消费：内容
    # 按行原样抵达比较点，与 staged 侧同一形态（不开子 shell，OUT_OF_SCOPE 累积不丢）。
    if ! printf '%s\\n' "$ALLOWED" > "$ALLOWED_TMP" 2>/dev/null; then
        echo "[orchd E020] 允许列表落盘失败（task=$TASK_ID）→ best-effort 放行（未校验范围，禁止静默）" >&2
        exit 0
    fi
fi

# 4) 校验每个 staged 文件：固定资产豁免 或 任务范围（动态 / 回退静态）。
#    "$@" 由 STAGED_TMP 装载（NUL 精确消费：R2-9a 空白拆词修、
#    task-hook-staged-nul-rootfix 换行拆分修；不用 here-doc + read——文件名含
#    $() 会被执行（文件名即注入面）；不用管道 + read——子 shell 丢累积量。
#    非 bash（dash/ash 无 `read -d`）退回按行消费并留痕）。
if [ -n "${{BASH_VERSION:-}}" ]; then
    set --
    while IFS= read -r -d '' _ORCHD_ITEM; do
        set -- "$@" "$_ORCHD_ITEM"
    done < "$STAGED_TMP"
else
    echo "[orchd E020] 当前解释器非 bash（不支持 read -d ''）：staged 集合按行消费（含换行的文件名会被拆分，已知限制）" >&2
    _ORCHD_OLD_IFS=$IFS
    IFS='
'
    set -f
    set -- $(tr '\\0' '\\n' < "$STAGED_TMP")
    set +f
    IFS=$_ORCHD_OLD_IFS
fi
OUT_OF_SCOPE=""
for FILE in "$@"; do
    IN_SCOPE=no
    # 固定资产豁免（引擎自动提交路径，不在任务 files_to_edit 内）：
    # .orchd/_master.json、IDEAS.md（根布局）与 .orchd/IDEAS.md（发布态自包含
    # .orchd 布局）、ROADMAP.md（宿主项目根，唯一源）、.orchd/_full_regression.json
    # （full-regression 的发版回归证据，同属引擎自动提交）——
    # amend / intake / full-regression 在 main 分支提交它们，若不豁免会被本 hook
    # 拦截（引擎自动提交零改动）。
    # 注：.orchd/ROADMAP.md 已非合法形态（ROADMAP 唯一源为宿主根），不列入豁免。
    case "$FILE" in
        .orchd/_master.json|IDEAS.md|.orchd/IDEAS.md|ROADMAP.md|.orchd/_full_regression.json)
            IN_SCOPE=yes
            ;;
    esac
    if [ "$IN_SCOPE" != "yes" ]; then
        if [ "$SCOPE_MODE" = "dynamic" ]; then
            # 目录式声明感知：逐行判定，精确相等或目录前缀匹配
            # here-doc 的 EOF 必须在行首（无缩进），否则 shell 不识别
            while IFS= read -r AP; do
                if [ -n "$AP" ]; then
                    if [ "$FILE" = "$AP" ]; then IN_SCOPE=yes; break; fi
                    # R2-9b：仅目录式声明（以 / 结尾）做前缀匹配，文件声明只认
                    # 精确相等——与静态路径 case 模板、pool._is_path_covered 同边界，
                    # 堵住「声明 README.md 却放行 README.md.evil」的裸前缀误放行。
                    case "$AP" in
                        */)
                            case "$FILE" in "$AP"*) IN_SCOPE=yes; break ;; esac
                            ;;
                    esac
                fi
            done < "$ALLOWED_TMP"
        else
{FILES_CHECK}
{EXEMPTS_CHECK}
        fi
    fi
    if [ "$IN_SCOPE" != "yes" ]; then
        OUT_OF_SCOPE="$OUT_OF_SCOPE$FILE "
    fi
done

# 5) 有越界文件 → 拒绝提交
if [ -n "$OUT_OF_SCOPE" ]; then
    echo "orchd E020: out-of-scope commit blocked (task $TASK_ID active, mode=$SCOPE_MODE)"
    echo "Out-of-scope files:"
    for F in $OUT_OF_SCOPE; do
        echo "  - $F"
    done
    echo ""
    echo "Allowed files for this task:"
    if [ "$SCOPE_MODE" = "dynamic" ]; then
        printf '%s\\n' "$ALLOWED" | while IFS= read -r AF; do
            if [ -n "$AF" ]; then echo "  - $AF"; fi
        done
    else
{STATIC_ALLOWED_ECHO}
{STATIC_EXEMPT_HEADER}{STATIC_EXEMPT_ECHO}
    fi
    echo ""
    echo ""
{E020_ESCAPE_BLOCK}
    echo "To bypass: git commit --no-verify"
    exit 1
fi

exit 0
"""
