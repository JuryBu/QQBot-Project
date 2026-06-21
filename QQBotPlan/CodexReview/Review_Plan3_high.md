**审查结论（先给问题）**

1. **严重：成本基线在 Plan 与实现间不一致，预算目标不可信。**  
证据：[`Plan_3_4_面板与成本监控.md:65`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:65)、[`Plan_3_2_KVCache优化.md:256`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:256)、[`cost_tracker.py:49`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:49)。  
影响：Plan 里的降本目标（¥15-20/月）会偏差，后续验收口径会打架。

2. **严重：Plan_3_4 记账模型缺少“缓存存储费事件”，总成本会系统性低估。**  
证据：[`Plan_3_4_面板与成本监控.md:63`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:63)、[`Plan_3_4_面板与成本监控.md:77`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:77)。  
影响：只记 `usageMetadata` 调用账，不记 cache create/rebuild 的 storage 账，模型成本对比会失真。

3. **严重：群级 `enabled=false` 的“完全关闭”语义在 Plan 中未闭环到所有触发路径。**  
证据：[`Plan_3_4_面板与成本监控.md:184`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:184)、[`Plan_3_1_FlashLite采样优化.md:15`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_1_FlashLite采样优化.md:15)。  
影响：如果只在“间隔计算”层处理禁用，时间兜底/@即时触发仍可能漏拦，行为与面板承诺不一致。

4. **高：Plan_3_2 提出的“hash 仅基于静态部分”有错误复用缓存风险。**  
证据：[`Plan_3_2_KVCache优化.md:241`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:241)、[`gemini_source.py:97`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/astrbot/core/provider/sources/gemini_source.py:97)。  
影响：忽略 tools 变化会导致缓存与当前工具声明不一致，可能触发 function-calling 异常或行为漂移。

5. **高：动态前缀注入方案有边界条件漏项。**  
证据：[`Plan_3_2_KVCache优化.md:207`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_2_KVCache优化.md:207)。  
影响：示例仅处理“首条消息是 user”，未定义非 user/空 contexts 的兜底策略，可能丢动态上下文。

6. **中：验收标准自相矛盾（是否依赖 GCP）。**  
证据：[`Plan_3.md:92`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3.md:92)、[`Plan_3.md:107`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3.md:107)、[`Plan_3_4_面板与成本监控.md:25`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:25)、[`Plan_3_4_面板与成本监控.md:220`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_4_面板与成本监控.md:220)。  
影响：团队会不知道“以本地 usageMetadata 为准”还是“以 GCP 为准”，验收不可执行。

7. **中：配置键迁移策略未在 Plan 固化，存在“显示值≠生效值”风险。**  
证据：[`Plan_3_1_FlashLite采样优化.md:37`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3_1_FlashLite采样优化.md:37)、[`_conf_schema.json:2`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:2)、[`models.py:159`](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:159)。  
影响：多端同时读写 `sync_interval/sync_trigger_interval` 时，用户看到和实际运行值可能不一致。

**待确认问题**
1. 成本“真值源”最终选哪个：官方最新定价表快照，还是代码内置定价表？  
2. `enabled=false` 是否要阻断该群全部入口（计数触发/时间兜底/@即时/后台唤醒）？  
3. KVCache hash 是否明确包含 `system_instruction + tools签名 + model`（建议写死在 Plan）？

**补充**
- 本轮是文档与架构一致性深审，未改代码文件。  
- 如果你要，我可以下一步直接给出一版“可执行的 Plan_3 修订稿（逐段替换）”，把以上 7 个风险一次性收口，主人。