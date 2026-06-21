## 🔴 严重问题（必须修复）

1. `merge_threshold=0` 时会触发未定义变量崩溃  
位置：[stage.py:316](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/pipeline/respond/stage.py:316)  
问题：`max_segments` 分支日志使用 `merged_chain`，但当 `merge_threshold <= 0` 时该变量未定义，触发 `UnboundLocalError`。  
建议：进入硬限分段前记录 `original_len = len(result.chain)`，日志改用 `original_len`。

2. Gemini KVCache 错误恢复可能死循环  
位置：[gemini_source.py:674](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:674), [gemini_source.py:720](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:720), [gemini_source.py:776](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:776), [gemini_source.py:798](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:798)  
问题：缓存模式报 `Developer instruction is not enabled` / `Function calling is not enabled` 后，仅清 `system_instruction/tools`，未清 `cached_content_name`，`while True` 会重复走缓存路径。  
建议：错误分支同时 `cached_content_name = None`，必要时禁用本次请求缓存。

3. KVCache 哈希缺少模型维度，存在跨模型误复用  
位置：[gemini_source.py:108](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:108)  
问题：hash 仅含 `system_instruction + tool_names`，模型切换时可能复用到错误缓存。  
建议：将 `model`（建议再加工具签名）纳入 hash。

4. OpenAI 调试日志可导致运行时异常  
位置：[openai_source.py:494](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/openai_source.py:494)  
问题：`_tc.function.arguments[:100]` 假设是字符串；若为 `dict/None` 会 `TypeError`，中断请求流程。  
建议：先 `str(_tc.function.arguments)` 再截断。

## 🟡 建议改进

1. 全局移除 `TOOL_CALL_PROMPT` 风险范围过大  
位置：[astr_main_agent.py:1399](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/astr_main_agent.py:1399)  
建议：改为按插件/会话开关控制，不要在 core 全局无条件移除。

2. KVCache 创建使用同步客户端，异步路径可能阻塞  
位置：[gemini_source.py:128](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:128)  
建议：改异步调用或 `asyncio.to_thread`，并加按 hash 的并发锁。

## 说明

- 本轮审查范围：`AstrBot` 子仓库当前 8 个已修改文件（R1-R6 对应改动）。  
- 已做静态校验：`py_compile` 通过。未执行集成测试/真实 Provider 联调。  
- 完整报告已保存：[报告_R1-R6代码修改审核_xhigh_Codex.md](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/CodexReview/报告_R1-R6代码修改审核_xhigh_Codex.md)。