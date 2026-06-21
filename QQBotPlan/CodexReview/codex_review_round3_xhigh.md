已完成 Plan_3 全量审查（含 FlashLite 插件主代码 + Plan_3 对应控制台链路），结论是：`2` 个🔴严重问题、`5` 个🟡建议问题、`1` 个🟢微调问题。

# 审核发现（按严重度）
1. 🔴 成本归因仍依赖全局 `_current_window_key`，并发下会串窗错账。  
位置：[main.py:171](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:171)、[main.py:1633](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1633)、[main.py:3271](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3271)。  
建议：改为显式参数传递 `window_key`，不要在记账路径读取全局可变字段。

2. 🔴 `CostTracker.shutdown()` 已实现但插件终止流程未调用，存在停机前丢数窗口。  
位置：[cost_tracker.py:220](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:220)、[main.py:5880](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5880)。  
建议：在 `terminate()` 中补 `await self._cost_tracker.shutdown()`。

3. 🟡 `_conf_schema.json` 未覆盖 Plan_3 实际关键参数，面板可控性不足。  
位置：[_conf_schema.json:1](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:1)、[main.py:248](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:248)、[main.py:736](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:736)。  
建议：补齐 `dynamic_sampling/group_overrides/cost_tracker/tool_model/review_interval_hours` 等字段。

4. 🟡 BossLady 控制台默认值与插件默认值不一致，存在“显示值与真实行为不一致”。  
位置：[models.py:170](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:170)、[main.py:144](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:144)、[models.py:171](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:171)、[main.py:143](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:143)。  
建议：统一默认值来源（建议以插件运行逻辑为准）。

5. 🟡 控制台仍以 `sync_interval` 为核心字段，未完整覆盖 `sync_trigger_interval/sync_time_interval`。  
位置：[models.py:159](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:159)、[models.py:209](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:209)、[main.py:116](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:116)。  
建议：读旧写新，逐步迁移到新键。

6. 🟡 成本定价表在控制台与核心模块不一致，`/pricing` 会误导。  
位置：[cost.py:23](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/cost.py:23)、[cost_tracker.py:43](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:43)。  
建议：统一定价源，避免双份常量漂移。

7. 🟡 Plan_3 测试脚本可移植性差：硬编码绝对路径 + GBK 控制台会因 `✅` 报错。  
位置：[test_stage6_kvcache_all.py:4](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3/test_stage6_kvcache_all.py:4)、[test_stage6_kvcache_all.py:35](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3/test_stage6_kvcache_all.py:35)、[test_stage11_cost_tracker.py:10](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3/test_stage11_cost_tracker.py:10)、[test_stage11_cost_tracker.py:39](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_3/test_stage11_cost_tracker.py:39)。  
建议：改相对路径，输出改 ASCII 或统一 UTF-8。

8. 🟢 私聊窗口命名双标准（`PrivateMessage`/`FriendMessage`）会制造统计噪声。  
位置：[main.py:681](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:681)、[main.py:1993](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1993)。  
建议：统一窗口命名规范。

# 验证记录
- 语法检查通过：`main.py/cost_tracker.py/kv_cache.py/models.py/cost.py`。
- Plan_3 测试脚本在 `PYTHONIOENCODING=utf-8` 下通过；默认 GBK 下会因 emoji 打印报错（已复现）。

完整报告已保存到：[报告_Plan3全量审核_Codex.md](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/CodexReview/报告_Plan3全量审核_Codex.md)。