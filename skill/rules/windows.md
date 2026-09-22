---
# done 路由理由：verify_command 本机执行，Windows 约束直接相关
guide:
  done: 5
---
# Windows 环境准备（自托管仓库）与管道编码

> TL;DR: ① **Windows 下 agent 执行任何 shell 命令必须走 Git Bash（`bash -lc`）或零根入口 `python .orchd/__main__.py`，禁止用 PowerShell / cmd.exe 直接执行协议中的 POSIX 命令** ② 管道消费 orchd JSON 须按 UTF-8 解码 ③ 零根入口不依赖 PATH，优先使用

> 原 .orchd/SKILL.md「Windows 环境准备（自托管仓库）」，外置自 task-skill-hub-refactor。

## Windows shell 命令执行规则（硬性）

orchd 协议文本（`AGENTS.md`、`SKILL.md`、`rules/*.md`、`templates/*.md`）中的命令示例以 POSIX / bash 语法书写（`&&`、`export`、`$VAR`、`${TMPDIR:-/tmp}`、`grep|tail`、`cd /c/...` 等）。这些构式在 Windows 默认 shell（PowerShell 5.1 / cmd.exe）下会语法错误、语义错误或命令缺失。

**硬性规定**：

- **优先零根入口**：所有 orchd 命令统一使用 `python .orchd/__main__.py <子命令>`，不使用短命令 `orchd ...`（零根入口不依赖 PATH，Windows / macOS / Linux 行为一致）。
- **必须走 Git Bash**：协议中涉及 POSIX 工具（`grep`、`tail`、`sed`、`cp`、`test`）或 bash 语法（`export`、`$VAR`、`&&`、heredoc）的命令，在 Windows 下必须用 Git Bash 执行：`bash -lc "<命令>"`，禁止用 PowerShell / cmd.exe 直接执行。
- **引擎已兜底**：`done` 的 `verify_command`、全量回归等由引擎执行的命令，已通过 `orchd/subproc.py` 在 Windows 自动用 Git Bash 兜底，agent 无需手动包装。
- **Git Bash 未安装**：若机器无 Git Bash，优先使用零根入口 + `python -c` 替代 POSIX 工具；无法替代时报告阻塞，不得在 PowerShell / cmd 中强行执行 POSIX 命令。

## PATH 与短命令

- orchd 可执行文件装在用户级 Scripts 目录（如 `%APPDATA%\Python\Python312\Scripts\orchd.exe`），通常不在 PowerShell / cmd 默认 PATH 中。
- **在 Git Bash 中**如需使用短命令，先执行 `export PATH="$PATH:$HOME/AppData/Roaming/Python/Python312/Scripts"` 再调用 `orchd`（此命令仅在 Git Bash 中有效，PowerShell / cmd 中等价写法为 `$env:Path += ";$env:APPDATA\Python\Python312\Scripts"`）。
- **零根入口 `python .orchd/__main__.py` 不依赖 PATH**，Windows / macOS / Linux 均可直接使用，**推荐优先使用零根入口**。
- `verify_command` 内嵌 `orchd` 短命令时同受 PATH 影响：未导出 PATH 会 E014（"不是内部或外部命令"）；引擎执行 verify 时已用 Git Bash 兜底，agent 手工自检前需确保 PATH 或改用零根入口。

## 管道编码（中文 Windows）

- 中文 Windows 上 git/子进程输出编码：引擎已统一 UTF-8 解码（`orchd/gitops_ops.py` 的稳健解码 helper（UTF-8 优先 / GBK 回退 / 有损兜底）+ `orchd/subproc.py`；原扁平模块 `gitops.py` / `onboard.py` 已拆分为 `orchd/gitops/` / `orchd/onboard/` 子模块）。
- **管道消费 orchd JSON 须按 UTF-8 解码**：`python .orchd/__main__.py pool --all | python -c "json.load(sys.stdin)"` 在中文 Windows 会 JSONDecodeError/乱码（消费方 stdin 按 GBK 解码 UTF-8 字节流）。
- 规避方式：设 `PYTHONIOENCODING=utf-8` 或 `python -X utf8`、或显式 `encoding='utf-8'` 解码。
- 此为 CLI 写 stdout 给管道消费方的编码契约，与 task-encoding-hardening 的引擎读子进程输出解码不同向，详见 README「Windows 管道编码」。

## PowerShell 管道传参编码陷阱（取证 / 复核）

PowerShell 5.1 把内容经管道喂给原生进程时，默认以 **UTF-16LE（带 BOM）** 形态写 stdin；按 UTF-8 解析的原生进程会整体误判——实测 `git show <rev>:<path> | python -m ruff check --stdin-filename <path> -` 时 6 个文件全部报 `invalid-syntax`，**与代码本身无关**（纯环境编码陷阱，复查基线时极易误判为"代码有问题"）。

正确做法（任选其一）：

- **推荐**：改用临时文件 + 显式路径复核（`git show <rev>:<path> > "$TMP/x.py"` 后由 Python 读文件），绕开管道形态转换；
- 或先显式设置 `$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`（必要时连同 `[Console]::OutputEncoding`）再管道；
- 或直接用 Git Bash（`bash -lc`）执行同一命令（见上节「Windows shell 命令执行规则」）。

## PowerShell 落盘与分流（JSON 消费）

引擎 stdout 本身是严格可解析 JSON（`orchd/cli/skeleton.py::_output` 已净化不安全字符），解析失败一律先查**调用侧**两处手法，勿报引擎缺陷：

- **禁 `>` 落盘 JSON**：PowerShell 5.1 的 `>` 写 UTF-16LE+BOM，且中文先被控制台按 GBK 解码损坏。统一走字节级捕获器（`scripts/orchd_cap.py`，subprocess 捕获 + 分流落盘，退出码透传）：
  `python scripts/orchd_cap.py --out C:/Temp/s.json --err C:/Temp/s.err -- status`
- **禁 `2>&1` 混流**：stderr 的 `orchd ▸` 引导块是给人看的，混进文件则任何解析必炸。stdout/stderr 必须分流（捕获器默认即分流）。
- **解析**：stdout 按 UTF-8（BOM 容错 `utf-8-sig`）一次 strict `json.loads` 即可；`--text` 类命令本来就不是 JSON，不得按 JSON 解析。
