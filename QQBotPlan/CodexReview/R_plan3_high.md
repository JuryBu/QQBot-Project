**🔴 严重问题**

1. `media_summary` 的 `_wk` 作用域错误会导致中/大型摘要直接异常。  
位置：[main.py:4795](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4795) [main.py:4812](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4812) [main.py:4838](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4838)  
影响：`content_len > 2000` 分支可能触发 `UnboundLocalError`，返回“摘要错误”。  
建议：将 `_wk = self._extract_window_key(event)` 提前到分支外统一定义。

2. BossLady 的 `group_overrides` 前后端未打通，UI 保存是“假成功”。  
位置：[app.js:1952](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js:1952) [app.js:2050](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js:2050) [models.py:160](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:160) [models.py:183](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:183)  
影响：前端可编辑群覆盖，但后端不返回/不接收该字段，配置不会落盘。  
建议：后端 `GET/POST /models/flashlite` 增加 `group_overrides` 字段并校验结构。

3. CHECKPOINT 压缩调用未传 `window_key`，窗口成本归因会失真。  
位置：[checkpoint.py:669](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py:669) [main.py:1545](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1545)  
影响：该路径成本可能被记到 `unknown` 窗口，破坏“按窗口统计”。  
建议：在压缩回调中显式传当前 `window_key`（闭包或扩展签名）。

**🟡 建议改进**

1. `group_overrides.enabled` 在运行时未生效。  
位置：[main.py:766](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:766)  
建议：群聊入口增加 `enabled=false` 的短路逻辑。

2. `sync_time_interval` 未进入 BossLady 配置链路。  
位置：[index.html:480](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/index.html:480) [models.py:183](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:183)  
建议：前后端同时补齐该字段。

3. `review_interval_hours` 的“0=关闭”语义不一致。  
位置：[models.py:237](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:237)  
建议：统一语义并在主逻辑中显式处理关闭态。

已写入本轮报告：  
[QQBotPlan/CodexReview/codex_review_round4_high.md](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/CodexReview/codex_review_round4_high.md)

1. 如果你要，我可以直接按这 3 个严重问题提交修复补丁。  
2. 也可以先只修复 `group_overrides` 全链路（最影响 Plan_3 对照结论）。