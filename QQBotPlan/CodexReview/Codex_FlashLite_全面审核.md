# Codex Review 任务：FlashLite 体系全面审核

## 审核目标
对老板娘 FlashLite 体系进行全面审核，涵盖架构完整性、代码质量、提示词一致性、性能与成本优化。

## 审核范围

### 核心代码
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py` — FlashLite 引擎主文件（~5500行）
  - FlashLite 判断系统（_build_flash_lite_system, _call_flash_lite, _build_judgment_prompt）
  - 工具模型子代理系统（_build_tool_model_system, _call_tool_model, tool agent 循环）
  - 主模型上下文注入（inject_flashlite_context, 17个 Section 动态注入）
  - 工具声明（task_set, browser_agent, search, web_fetch 等）
  - KV Cache 机制（ensure_cache, 隐式/显式缓存）
  - T 文件集成（上下文替换、assistant 补录）
  - 表情包/图像生成/媒体摘要等附加功能

- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py` — CHECKPOINT 压缩系统

### 提示词审计文档（必须核对代码与文档一致性）
- `QQBotPlan/提示词审计/00_总览.md` — 架构总览
- `QQBotPlan/提示词审计/Prompt_FlashLite_判断.md` — FlashLite 判断模式提示词
- `QQBotPlan/提示词审计/Prompt_FlashLite_压缩.md` — FlashLite 压缩模式提示词
- `QQBotPlan/提示词审计/Prompt_主模型.md` — 主模型（老板娘）提示词
- `QQBotPlan/提示词审计/Prompt_工具模型.md` — 工具模型子代理提示词

### 设计文档（辅助参考）
- `QQBotPlan/Plan_2.md` — Plan 2 总纲
- `QQBotPlan/Plan_2_2.md` — 提示词系统重构

## 审核要求

### 1. 架构完整性
- 三个模型（FlashLite、主模型、工具模型）的调用链路是否清晰无交叉
- 每个模型收到的 system prompt / user prompt 与审计文档是否完全一致
- inject_context 参数链路（task_set/browser_agent → _call_tool_model）是否正确传递

### 2. 代码质量
- 异常处理是否充分（特别是 API 调用、文件 I/O、JSON 解析）
- 资源泄漏风险（aiohttp session、文件句柄、asyncio Task）
- 无用代码、重复逻辑、过长函数的识别
- 日志级别是否合理（debug/info/warning/error）

### 3. 提示词一致性
- `_build_flash_lite_system()` 的输出与 Prompt_FlashLite_判断.md 文档描述是否一致
- `_build_tool_model_system()` 的输出与 Prompt_工具模型.md 文档描述是否一致
- `inject_flashlite_context()` 的各 Section 与 Prompt_主模型.md 文档描述是否一致
- 双模式输出格式（判断/压缩）是否正确合并，无冲突

### 4. 性能与成本
- KV Cache 命中率优化：system prompt 前缀稳定性评估
- FlashLite 调用频率与 token 消耗估算
- 工具模型 API Key 池轮转与冷却机制
- T 文件 I/O 频率与锁竞争

### 5. 安全性
- API Key 存储与传递安全
- Sandbox 代码执行的隔离性
- Prompt 注入防护（用户消息中可能包含恶意指令）

## 输出格式

请输出结构化审核报告，按以下格式：
- 每个审核维度的评估结果（优秀/良好/需改进/有风险）
- 发现的问题按严重程度分级（Critical/High/Medium/Low）
- 提示词一致性的逐项核对结果
- 性能优化建议（有数据支撑更好）
- 每个问题提供文件名、行号、问题描述、建议修复方案
