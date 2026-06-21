# 审核报告：Plan_3 系列 vs 实际实现对照（Review 3/6）

**审核时间**: 2026-04-13 12:01:43
**审核范围**:
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`
- `BossLady_Console/backend/routers/models.py`
- `BossLady_Console/backend/routers/cost.py`
- `BossLady_Console/frontend/app.js`
- `BossLady_Console/frontend/index.html`
**整体评价**: Plan_3 核心改造（KVCache 静态/动态分离、主模型记账、自动刷新）已落地，但仍存在 3 个可直接影响功能正确性的偏差与若干配置契约缺口。

## 🔴 严重问题（必须修复）

### 问题 1：群聊独立配置 `group_overrides` 前后端契约断裂，UI 保存实际无效
- **位置**：
  - `BossLady_Console/backend/routers/models.py:183-199`（请求模型无 `group_overrides` 字段）
  - `BossLady_Console/backend/routers/models.py:157-177`（GET 返回无 `group_overrides`）
  - `BossLady_Console/backend/routers/models.py:239-251`（POST 未处理 `group_overrides`）
  - `BossLady_Console/frontend/app.js:2050`（前端 POST `group_overrides`）
- **描述**：前端已发送并读取 `group_overrides`，但后端路由完全未声明/落盘该字段，导致“看似保存成功、实际不生效”。
- **修复建议**：
  - 在 `UpdateFlashLiteRequest` 增加 `group_overrides: Optional[Dict[str, Any]]`。
  - `GET /models/flashlite` 返回 `group_overrides`。
  - `POST /models/flashlite` 对 `group_overrides` 做类型校验后写入 config。

### 问题 2：`enabled` 开关未接入触发链路，无法按群关闭 FlashLite
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:656-705`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:766-773`
- **描述**：`group_overrides` 仅读取 `sync_interval`，未读取 `enabled`；即使某群设置 `enabled=false`，消息仍会进入同步/异步触发流程。
- **修复建议**：
  - 增加 `_is_group_enabled(group_id)` 判断，群消息入口最前面短路返回。
  - `group_overrides[group_id].enabled=false` 时跳过计数、同步触发、异步触发。

### 问题 3：`media_summary` 中 `window_key` 局部变量在分支下未初始化即使用
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4795`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4810-4813`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4820-4823`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:4835-4838`
- **描述**：`_wk` 仅在小内容分支定义，中/大内容分支直接引用，会触发 `UnboundLocalError`。
- **修复建议**：在分支判断前统一初始化：
  - `_wk = self._extract_window_key(event)` 放到 `if content_len <= 2000 ...` 之前。

## 🟡 建议改进

### 问题 4：`sync_time_interval` 未在 BossLady 面板/API 暴露，Plan_3 参数化不完整
- **位置**：
  - `BossLady_Console/backend/routers/models.py:183-199`（请求模型无 `sync_time_interval`）
  - `BossLady_Console/frontend/index.html:471-487`（仅有固定间隔、最少消息数，无时间兜底秒数输入）
- **描述**：主逻辑已支持 `sync_time_interval`（`main.py:142,695`），但控制台无法配置。
- **修复建议**：在前后端同增 `sync_time_interval` 字段并落盘。

### 问题 5：`sync_trigger_interval`（新键）与 `sync_interval`（旧键）双轨未统一，存在“显示值 ≠ 生效值”风险
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:116`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:2`
  - `BossLady_Console/backend/routers/models.py:159,209-210`
- **描述**：引擎优先读 `sync_trigger_interval`，控制台读写 `sync_interval`。当配置文件同时存在两键时，控制台修改可能不生效。
- **修复建议**：统一迁移到 `sync_trigger_interval`（读旧写新，必要时启动时自动迁移一次）。

### 问题 6：`_conf_schema.json` 将复杂字段定义为字符串，但引擎未做 JSON 反序列化
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:30-40`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:277-282`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:147,766`
- **描述**：`dynamic_sampling/group_overrides` 在 schema 中是字符串；`main.py` 仅接受 dict，若来源是字符串则回退默认，用户配置被静默忽略。
- **修复建议**：在初始化时对这两项做 `json.loads`（失败再回退默认并打 warning）。

### 问题 7：成本路由缺少 `POST /pricing`，无法满足 Plan_3_4 的定价在线更新
- **位置**：`BossLady_Console/backend/routers/cost.py:75-215`
- **描述**：当前只有 `GET /pricing`，无更新入口。
- **修复建议**：补 `POST /pricing`（含字段校验、持久化位置、并发写保护）。

## 🟢 微调建议

### 问题 8：成本面板指标与图表形态仍为“降级版”
- **位置**：
  - `BossLady_Console/frontend/index.html:392-413`（概览仅 4 卡）
  - `BossLady_Console/frontend/app.js:1794-1849`（折线）
  - `BossLady_Console/frontend/app.js:1851-1894`（环图）
- **描述**：与 Plan_3_4 原始描述（6 指标 + 折线/饼/柱/面积）仍有差距。
- **修复建议**：补“采样效率”等指标，并增加柱状/面积图或在文档中明确降级设计。

### 问题 9：`review_interval_hours` 的“0=关闭”语义前后端不一致
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:70-72`
  - `BossLady_Console/backend/routers/models.py:237`
- **描述**：schema 允许 0 关闭；控制台 POST 强制最小 1，无法关闭。
- **修复建议**：统一规则（推荐：允许 0 并在主逻辑显式判定关闭）。

## ✅ 做得好的地方
- `FlashLite/工具模型` 的 system 与动态前缀拆分已落地，KVCache 方向正确（`main.py:1350-1688`, `1709-1862`）。
- 主模型记账已改为从 `event` 提取窗口标识，不再依赖读取全局变量（`main.py:3268-3312`）。
- 终止流程已包含 `CostTracker.shutdown()`，停机刷盘补齐（`main.py:5919-5931`）。
- 成本页 30s 自动刷新与维度切换已实现（`app.js:1766-1776`, `1896-1917`）。
