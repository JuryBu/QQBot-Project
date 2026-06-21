**🔴 严重问题**
1. `group_overrides` 前后端契约断裂，BossLady“群聊独立配置”保存实际无效。  
位置：[models.py:183](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:183), [models.py:157](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:157), [models.py:239](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:239), [app.js:2050](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js:2050)。  
结论：前端发送了 `group_overrides`，后端未声明也未写入，UI 成功提示是“假成功”。

2. `enabled` 开关未接入触发链路，无法按群关闭 FlashLite。  
位置：[main.py:656](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:656), [main.py:766](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:766)。  
结论：仅读取 `sync_interval`，不读取 `enabled`，配置 `enabled=false` 仍会继续触发。

3. `media_summary` 存在未初始化变量 `_wk` 的可触发运行时错误。  
位置：[main.py:4795](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4795), [main.py:4810](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4810), [main.py:4820](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4820), [main.py:4835](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4835)。  
结论：`_wk` 只在小内容分支赋值，中/大内容分支直接使用，会报 `UnboundLocalError`。

**🟡 建议改进**
1. `sync_time_interval` 未在 BossLady 面板/API 暴露，Plan_3 参数化不完整。  
位置：[models.py:183](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:183), [index.html:471](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/index.html:471), [main.py:142](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:142)。

2. `sync_trigger_interval`（新键）与 `sync_interval`（旧键）双轨并存，存在“显示值不等于生效值”风险。  
位置：[main.py:116](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:116), [_conf_schema.json:2](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:2), [models.py:159](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:159)。

3. `_conf_schema` 里 `dynamic_sampling/group_overrides` 是字符串，但引擎未做 JSON 反序列化。  
位置：[_conf_schema.json:30](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:30), [main.py:277](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:277), [main.py:147](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:147), [main.py:766](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:766)。

4. 成本路由缺少 `POST /pricing`，不满足 Plan_3_4 的“面板可更新定价”要求。  
位置：[cost.py:214](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/cost.py:214)。

**🟢 微调**
1. 成本面板仍是“降级版”：4 卡片 + 折线/环图，未达到计划中的 6 指标 + 折线/饼/柱/面积。  
位置：[index.html:392](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/index.html:392), [app.js:1794](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js:1794), [app.js:1851](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js:1851)。

2. `review_interval_hours` 的“0=关闭”语义前后端不一致。  
位置：[_conf_schema.json:70](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:70), [models.py:237](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:237)。

已生成完整报告：  
[报告_Plan3对照审核_3of6_Codex.md](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/CodexReview/报告_Plan3对照审核_3of6_Codex.md)

测试补充：`test_stage6_kvcache_all.py / test_stage7_9_sampling.py / test_stage11_cost_tracker.py` 在 `PYTHONIOENCODING=utf-8` 下通过；默认 GBK 控制台会因 `✅` 输出触发编码错误。