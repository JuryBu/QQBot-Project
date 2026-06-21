已完成 `QQBotPlan` 整体体系 `xhigh` 审核。结论：`4` 个🔴严重问题、`2` 个🟡建议问题、`1` 个🟢微调建议。  
完整报告已保存到：[报告_QQBotPlan整体体系审核_Codex.md](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/CodexReview/报告_QQBotPlan整体体系审核_Codex.md)

1. 🔴 控制台无鉴权，`/system/password` 仅写 hash 不校验，所有高危接口可直接调用。  
位置：[main.py:42](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/main.py:42)、[system.py:358](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routes/system.py:358)、[models.py:71](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:71)、[bot.py:145](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/bot.py:145)。  
建议：加统一鉴权中间件，所有 `/api/*` 强制校验会话/token。

2. 🔴 Sandbox 边界失效：`sandbox_exec` 可回退系统解释器执行任意代码，`web_fetch(file://)` 可读本地任意文件。  
位置：[Plan_1_sandbox.md:60](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1/Plan_1_sandbox.md:60)、[sandbox.py:673](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:673)、[sandbox.py:756](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/sandbox.py:756)、[web_engine.py:268](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/web_engine.py:268)。  
建议：强制仅 Sandbox 内路径、禁绝系统解释器回退、加进程级隔离。

3. 🔴 Memory 工作区隔离被绕过，存在跨窗口读写/召回。  
位置：[Plan_1_memory.md:36](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/Plan_1/Plan_1_memory.md:36)、[main.py:920](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:920)、[main.py:3627](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3627)、[memory.py:672](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:672)、[memory.py:756](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/memory.py:756)。  
建议：工具层强制注入当前 workspace，存储层无 workspace 直接拒绝。

4. 🔴 表情包接口路径未做边界校验，Windows 下可路径穿越导致越权读写删。  
位置：[models.py:616](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:616)、[models.py:636](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:636)、[models.py:651](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:651)、[models.py:673](/c:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py:673)。  
建议：`resolve + relative_to` 硬校验，文件名白名单，上传文件名服务端重写。  

本轮为静态代码审计，未执行攻击性联机验证。