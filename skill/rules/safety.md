# 安全边界（详见 docs/self-hosting-design-merged.md §9）

> TL;DR: ① 改引擎触碰 §9.1 停服边界先确认

> 原 .orchd/SKILL.md「安全边界」，外置自 task-skill-hub-refactor。

- 禁止走自托管的"停服升级"（人工执行）：schema required 字段与既有枚举语义、事件格式与 `_apply_event` 语义、spec.py 校验逻辑主干
- 任何把内容域假设写死进引擎的改动同样归入停服边界
- 高风险但可走管线的改动（状态机分支 / CLI 契约 / 锁协议）：三连自检 + files_to_read 含相关设计文档章节 + 审查者对照本规范逐条核对
