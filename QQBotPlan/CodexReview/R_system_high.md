# 审核结果（high）

## 🔴 严重问题
1. `group_overrides` 配置链路断裂，前端“保存”实际不生效（且静默失败）。
- 证据：
[app.js#L2045](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js#L2045) 提交 `group_overrides`；  
[models.py#L183](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py#L183) 请求模型未定义该字段；  
[models.py#L243](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py#L243) 更新逻辑未处理该字段；  
[models.py#L157](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py#L157) 读取接口也未返回该字段。
- 影响：群独立配置不可持久化，刷新后丢失，用户误判“已保存”。

2. `review_interval_hours` 语义冲突，`0=关闭`未兑现，存在高频 Review 任务风险。
- 证据：
[_conf_schema.json#L57](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json#L57) 写明 0 可关闭；  
[models.py#L237](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/backend/routers/models.py#L237) 强制最小 1；  
[main.py#L791](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py#L791) 未对 `<=0` 做关闭保护。
- 影响：配置语义与行为不一致；手工写 0 时可能每次同步都触发 Review。

## 🟡 建议改进
1. 群配置 `enabled` 开关未被引擎消费。
- 证据：
[app.js#L2037](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js#L2037) 前端可切换；  
[main.py#L763](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py#L763) 只读取 `sync_interval`。
- 影响：UI 显示可禁用，实际无效。

2. 前端存在注入风险：`gid` 直接拼进内联 `onclick`。
- 证据：
[app.js#L2011](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js#L2011)、[app.js#L2021](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/BossLady_Console/frontend/app.js#L2021)。
- 影响：可构造恶意群号破坏事件属性（建议改 `addEventListener` + 数字白名单）。

## 🟢 微调
1. 配置类型不一致：schema 将 `dynamic_sampling/group_overrides` 定义为字符串，但引擎按对象读取。
- 证据：
[_conf_schema.json#L26](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json#L26)、[main.py#L147](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py#L147)、[main.py#L766](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/AstrBot/data/plugins/astrbot_plugin_flashlite/main.py#L766)。

已生成完整报告：  
[报告_QQBotPlan整体体系审核_Codex.md](C:/Users/<user>/Desktop/AstrBotLauncher-0.1.5.6/QQBotPlan/CodexReview/报告_QQBotPlan整体体系审核_Codex.md)

说明：本轮为静态审查，未执行集成运行测试。