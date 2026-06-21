已完成“R1 并发重构 - 定点审核”，并生成报告：  
[报告_R1并发重构审核_Codex.md](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/报告_R1并发重构审核_Codex.md)

## 🔴 严重问题
1. `merge_threshold=0` 时会触发未定义变量崩溃：在 `max_segments` 分支日志里引用了未定义的 `merged_chain`。位置：[stage.py:316](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/pipeline/respond/stage.py:316)。  
建议改为使用进入分支前保存的 `original_len`，或先初始化 `merged_chain`。

2. Gemini KVCache 模式错误恢复可能死循环：缓存模式下遇到 `Developer instruction is not enabled` / `Function calling is not enabled`，只清空 `system_instruction/tools`，未清理 `cached_content_name`，会持续重试。位置：[gemini_source.py:674](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:674)、[gemini_source.py:720](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:720)、[gemini_source.py:776](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:776)、[gemini_source.py:798](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:798)。  
建议在该类错误下清空 `cached_content_name` 并降级到非缓存请求。

3. KVCache 哈希未包含模型维度，存在跨模型误复用风险。位置：[gemini_source.py:108](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:108)。  
建议将 `model` 纳入哈希键。

4. OpenAI 调试日志可能导致运行时异常：`_tc.function.arguments[:100]` 在 `arguments` 为 `dict/None` 时会 `TypeError`。位置：[openai_source.py:494](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/openai_source.py:494)。  
建议先 `str()` 再截断。

## 🟡 建议改进
1. 异步路径里调用同步 `caches.create`，并发下会阻塞事件循环且可能重复建缓存。位置：[gemini_source.py:128](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:128)。  
2. 全局移除 `TOOL_CALL_PROMPT` 缺少作用域隔离，建议只对 FlashLite 流程生效。位置：[astr_main_agent.py:1399](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/astr_main_agent.py:1399)。

已做最小静态校验：`python -m py_compile`（覆盖本次改动文件）通过。未跑集成测试。