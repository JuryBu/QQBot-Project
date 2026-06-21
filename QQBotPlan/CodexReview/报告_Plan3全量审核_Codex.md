# 审核报告：AstrBot FlashLite Plan_3 系列全量代码改动（Codex）

**审核时间**: 2026-04-13  
**审核范围**:
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json`
- `BossLady_Console/backend/routers/models.py`
- `BossLady_Console/backend/routers/cost.py`
- `BossLady_Console/frontend/app.js`
- `QQBotPlan/Plan_3/test_stage6_kvcache_all.py`
- `QQBotPlan/Plan_3/test_stage7_9_sampling.py`
- `QQBotPlan/Plan_3/test_stage11_cost_tracker.py`

**整体评价**: Plan_3 主线功能（KVCache 分离、动态采样、成本记录）基本可用，但仍有并发归因与停机刷盘两类高风险缺口，且控制台配置链路与插件运行参数仍存在明显脱节。

## 🔴 严重问题（必须修复）

### 问题 1：成本按窗口归因仍依赖全局可变状态，存在并发串窗/错账
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:171`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:631`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:681`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1633`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1862`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2251`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2387`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3271`
- **描述**：`window_key` 通过 `self._current_window_key` 全局字段传递。消息并发、Task 回调、on_llm_response 异步返回时，这个字段会被后续消息覆盖，导致成本记录归到错误窗口。
- **修复建议**：将 `window_key` 改为显式参数贯穿 `_sync_trigger/_async_trigger/_private_trigger -> _call_* -> CostTracker.record`；`on_llm_response` 优先从 `event` 解出窗口标识，不再读全局字段。

### 问题 2：CostTracker 已有 `shutdown()`，但插件终止流程未调用，存在停机前数据丢失窗口
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:220`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:5880`
- **描述**：`record()` 使用 5 秒 debounce 刷盘，但 `terminate()` 仅关闭 session/web 引擎，未执行 `await self._cost_tracker.shutdown()`。插件热重载/进程不退出场景下，尾部记录可能丢失。
- **修复建议**：在 `terminate()` 中补 `await self._cost_tracker.shutdown()`；若存在异常关闭路径，增加 finally 保护。

## 🟡 建议改进

### 问题 3：`_conf_schema.json` 未覆盖 Plan_3 实际运行关键参数
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:1`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:130`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:147`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:175`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:248`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:736`
- **描述**：schema 只暴露少量字段，未覆盖 `dynamic_sampling` 明细、`group_overrides`、`cost_tracker`、`tool_model`、`review_interval_hours` 等。
- **修复建议**：补齐 schema 字段，并在 schema 或加载阶段做类型/范围校验，避免手改 config 触发运行时偏差。

### 问题 4：BossLady 控制台默认值与插件运行默认值不一致，存在“面板显示与真实行为不一致”
- **位置**：
  - `BossLady_Console/backend/routers/models.py:170`
  - `BossLady_Console/backend/routers/models.py:171`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:143`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:144`
- **描述**：控制台默认 `sampling_mode="fixed"`、`sync_time_min_msgs=2`，插件默认 `dynamic`、`3`。用户通过面板读取/保存后可能无意改写策略。
- **修复建议**：统一默认值来源（建议以插件主逻辑为准），并在 GET 返回中显式透传实际生效值。

### 问题 5：BossLady `/models/flashlite` 仍以 `sync_interval` 为主，未完整覆盖 Plan_3 参数
- **位置**：
  - `BossLady_Console/backend/routers/models.py:159`
  - `BossLady_Console/backend/routers/models.py:209`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:116`
- **描述**：后端未暴露/保存 `sync_trigger_interval` 与 `sync_time_interval`，只能间接依赖兼容键，配置语义不完整。
- **修复建议**：新增 `sync_trigger_interval`、`sync_time_interval` 的读写字段，并保留旧键迁移逻辑（读旧写新）。

### 问题 6：成本“定价表接口”与核心计算定价不一致，可能误导运维判断
- **位置**：
  - `BossLady_Console/backend/routers/cost.py:23`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:43`
- **描述**：同一模型 `gemini-2.5-flash-preview-04-17` 的 output 单价在两个模块中不一致（`0.60` vs `3.50`）。虽然统计接口主要使用已落盘 `cost_usd`，但 `/pricing` 返回会误导。
- **修复建议**：统一定价源（复用 `cost_tracker.py` 的 PRICING 或抽共享配置模块）。

### 问题 7：Plan_3 测试脚本可移植性差，默认 Windows GBK 环境下会假失败
- **位置**：
  - `QQBotPlan/Plan_3/test_stage6_kvcache_all.py:4`
  - `QQBotPlan/Plan_3/test_stage7_9_sampling.py:4`
  - `QQBotPlan/Plan_3/test_stage11_cost_tracker.py:10`
  - `QQBotPlan/Plan_3/test_stage6_kvcache_all.py:35`
  - `QQBotPlan/Plan_3/test_stage7_9_sampling.py:23`
  - `QQBotPlan/Plan_3/test_stage11_cost_tracker.py:39`
- **描述**：测试脚本写死本机绝对路径，且输出 `✅` 在默认 GBK 控制台会抛 `UnicodeEncodeError`。
- **修复建议**：改为相对路径（基于 `__file__` 推导）；打印改 ASCII 或在脚本入口统一设置 UTF-8 输出。

## 🟢 微调建议

### 建议 1：私聊窗口命名存在双标准，统计与检索有噪声
- **位置**：
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:681`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1993`
  - `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2852`
- **描述**：同一私聊窗口在不同链路使用 `PrivateMessage:*` 与 `FriendMessage:*`。
- **修复建议**：统一命名规范（建议全链路 `FriendMessage:*` 或提供标准化转换函数）。

## ✅ 已验证项

- 语法检查通过：
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/kv_cache.py`
  - `python -m py_compile BossLady_Console/backend/routers/models.py`
  - `python -m py_compile BossLady_Console/backend/routers/cost.py`
- Plan_3 测试脚本在 `PYTHONIOENCODING=utf-8` 下可通过：
  - `python QQBotPlan/Plan_3/test_stage6_kvcache_all.py`
  - `python QQBotPlan/Plan_3/test_stage7_9_sampling.py`
  - `python QQBotPlan/Plan_3/test_stage11_cost_tracker.py`

## 结论
优先修复顺序建议：
1. 先消除 `window_key` 全局共享导致的并发错账（严重影响成本面板可信度）。
2. 接入 `terminate()->CostTracker.shutdown()`，封住停机丢数风险。
3. 打通 schema/控制台/插件参数的一致性（尤其 `sync_trigger_interval`、`sync_time_interval`、`group_overrides`）。
4. 统一定价表来源与私聊窗口命名，降低运维误判与统计噪声。
