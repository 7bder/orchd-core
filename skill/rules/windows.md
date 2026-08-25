# Windows 环境准备（自托管仓库）与管道编码

> 原 .orchd/SKILL.md「Windows 环境准备（自托管仓库）」，外置自 task-skill-hub-refactor。

- orchd 可执行文件装在用户级 Scripts 目录（如 `%APPDATA%\Python\Python312\Scripts\orchd.exe`），不在 bash 默认 PATH；bash 中先执行 `export PATH="$PATH:$HOME/AppData/Roaming/Python/Python312/Scripts"` 再调用 orchd。**零根入口 `python .orchd/__main__.py` 不依赖 PATH，Windows 同样可直接使用**
- `verify_command` 内嵌 `orchd` 时同受 PATH 影响：未导出 PATH 会 E014（"不是内部或外部命令"）；跑 done/自检前先导出 PATH
- 中文 Windows 上 git/子进程输出编码：引擎已统一 UTF-8 解码（gitops.py / onboard.py）；**管道消费 orchd JSON 须按 UTF-8 解码**——`python .orchd/__main__.py pool --all | python -c "json.load(sys.stdin)"` 在中文 Windows 会 JSONDecodeError/乱码（消费方 stdin 按 GBK 解码 UTF-8 字节流），规避：设 `PYTHONIOENCODING=utf-8` 或 `python -X utf8`、或显式 `encoding='utf-8'` 解码（与 task-encoding-hardening 的引擎读子进程输出解码不同向——此为 CLI 写 stdout 给管道消费方的编码契约，详见 README「Windows 管道编码」）
