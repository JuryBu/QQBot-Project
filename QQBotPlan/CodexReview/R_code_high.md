## 🔴 严重问题（必须修复）
1. `merge_threshold=0` 时会触发运行时异常，分段发送链路中断。  
位置：[stage.py:316](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/pipeline/respond/stage.py:316)（相关逻辑在 285-316）  
问题：`merged_chain` 只在 `self.merge_threshold > 0` 分支内定义，但后面的日志无条件使用 `len(merged_chain)`。当配置允许的 `merge_threshold=0` 且触发 `max_segments` 分支时会 `UnboundLocalError`。  
建议：进入分支前保存 `original_len = len(result.chain)`，日志改用 `original_len`，避免引用条件变量。

2. Gemini KVCache 创建在 async 路径内使用同步调用，会阻塞事件循环。  
位置：[gemini_source.py:127](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:127)、[gemini_source.py:143](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:143)、调用点 [gemini_source.py:667](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:667)  
问题：`_ensure_kv_cache()` 是 async，但内部直接 `sync_client.caches.create(...)`，高并发下会卡住主循环。  
建议：改为 `await asyncio.to_thread(...)` 或改用 SDK 异步缓存接口。

## 🟡 建议改进
1. KVCache 复用哈希未包含模型维度。  
位置：[gemini_source.py:108](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:108)、[gemini_source.py:113](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:113)  
风险：切换模型但 system/tool 名不变时可能复用错误缓存。建议把 `model` 纳入 hash key。

2. 全局移除 `TOOL_CALL_PROMPT`，影响范围超出 FlashLite。  
位置：[astr_main_agent.py:1399](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/astr_main_agent.py:1399)  
风险：`build_main_agent` 是全局入口，非 FlashLite 场景也失去工具调用引导。建议改为按 provider/plugin 条件移除。

3. 多处 `info` 级调试日志会输出工具参数、URL、文件路径，存在隐私与日志噪音风险。  
位置：[openai_source.py:432](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/openai_source.py:432)、[openai_source.py:481](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/openai_source.py:481)、[astr_main_agent.py:1148](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/astr_main_agent.py:1148)  
建议：降级到 `debug` 并对参数/URL 做截断脱敏。

4. 前向消息解析阶段同步展开转发内容，热路径延迟风险较高。  
位置：[aiocqhttp_platform_adapter.py:523](c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:523)  
建议：按配置开关控制，或改为惰性展开/后台摘要。

## 说明
- 本次基于当前未提交 diff 做静态审查。  
- 已做语法校验：`py_compile` 通过；未执行端到端运行测试。