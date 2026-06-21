# 审核报告：Plan 3 第二轮深度复核（修复后）

**审核时间**: 2026-04-13  
**审核范围**:  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json`  
- `AstrBot/data/plugins/astrbot_plugin_flashlite/kv_cache.py`  
- `QQBotPlan/Plan_3/test_stage6_kvcache_all.py`  
- `QQBotPlan/Plan_3/test_stage7_9_sampling.py`  
- `QQBotPlan/Plan_3/test_stage11_cost_tracker.py`  

**整体评价**: 第一轮的核心修复大部分已落地（含主模型记账接入、配置兼容、写盘 debounce、cleanup 接线），但仍有 2 个高风险问题与 4 个中风险问题，当前代码尚不建议按“成本监控完全闭环”标准直接验收。

---

## 一、第一轮问题复核（逐项）

| 首轮问题 | 第二轮状态 | 证据 | 结论 |
|---|---|---|---|
| 1. `window_key` 传递缺失 | ⚠️ 部分修复 | 已在 `route_message` 赋值：`main.py:626,676`；但记账仍依赖全局变量：`main.py:1623,1852,2241,2377,3261` | 从“恒为 unknown”改进为“可写入”，但并发/后台任务下仍可能串窗 |
| 2. 主模型记账缺失 | ✅ 已修复 | `on_llm_response` 钩子：`main.py:3227-3270`；直连路径记账：`main.py:2237-2245`,`2373-2381` | 主模型 usage 覆盖显著完善 |
| 3. `sync_trigger_interval` 兼容旧键 | ✅ 已修复 | `main.py:115-116` | 新旧配置兼容生效 |
| 4. 模型常量统一 | ✅ 已修复 | `TOOL_MODEL_DEFAULT`：`main.py:84`，并统一用于缓存和调用：`main.py:541,1785,2208,2343` | 一致性问题已消除 |
| 5. CostTracker 写入优化（debounce + to_thread） | ✅ 已修复 | `cost_tracker.py:193-214` | 高频写盘压力明显下降 |
| 6. cleanup 接线 | ✅ 已修复 | 启动后 30s 延迟清理：`cost_tracker.py:108-113` | 90 天清理逻辑已接入 |
| 7. 动态采样防御性校验 | ⚠️ 部分修复 | 阈值/间隔已校验：`main.py:152-158`；但 `window_minutes`、`group_overrides.sync_interval` 仍未校验：`main.py:148,731-735` | 仍存在可触发运行时异常的输入面 |
| 8. `_conf_schema.json` 面板覆盖不足 | ❌ 未修复 | schema 仅含少量字段：`_conf_schema.json:1-55`；但代码依赖更多配置：`main.py:130-133,147-150,170,243-247,731-735` | 面板配置与运行参数仍脱节 |
| 9. 主模型旁路（task/checkpoint）未完成动静分离 | ❌ 未修复 | `systemInstruction` 仍内嵌动态时间：`main.py:2190,2313` | 与主链路 KVCache 策略不一致 |

---

## 二、🔴 严重问题（必须修复）

### 问题 1：`window_key` 仍基于全局可变状态，存在并发串窗与错账
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:166,626,676,1623,1852,2241,2377,3261`
- **描述**：
  - 当前通过 `self._current_window_key` 在事件链路间传递窗口标识。
  - 该字段为实例级全局变量，在多消息并发、后台 task 完成回调、以及长工具链调用期间会被后续消息覆盖。
  - 结果是成本记录可能归到错误窗口（尤其 `tool_model` 多轮调用与 `main_model` 响应钩子）。
- **影响**：按窗口统计不可靠，影响“成本最高窗口识别”和采样调优决策。
- **修复建议**：
  - 把 `window_key` 改为显式参数沿调用链传递（`_sync_trigger/_async_trigger/_private_trigger -> _call_* -> CostTracker.record`）。
  - `on_llm_response` 中优先从 `event` 解析窗口标识，不依赖全局字段。
  - 后台 task 路径（`_wake_main_for_task/_checkpoint_review`）直接从 `task_event` 解析窗口。

### 问题 2：采样配置校验仍不完整，错误配置可直接触发运行时异常
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:148,704,731-735`
- **描述**：
  - `dynamic_sampling.window_minutes` 未做类型/范围校验，若配置成字符串会在 `now - window` 处触发 `TypeError`。
  - `group_overrides[gid].sync_interval` 直接返回，若是字符串/0/负数，会在 `count >= interval` 处出错或异常行为。
- **影响**：消息路由主链路可被错误配置打断。
- **修复建议**：
  - 初始化时统一归一化：`window_minutes = max(1, int(...))`；异常回退默认值。
  - `group_overrides.sync_interval` 仅接受正整数，否则忽略并回退动态/全局配置。

---

## 三、🟡 建议改进

### 问题 3：`_conf_schema.json` 仍未覆盖关键新增参数
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:1-55`；对照 `main.py:130-133,147-150,170,243-247,731-735`
- **描述**：面板仅暴露了 `sync_trigger_interval/sync_time_interval/sync_time_min_msgs/sampling_mode` 等少量字段，缺少 `dynamic_sampling`、`group_overrides`、`cost_tracker`、`tool_model`、`review_interval_hours` 等。
- **影响**：运维无法在面板完成关键调优，设计目标与实际入口不一致。
- **修复建议**：补齐 schema 并增加基础校验说明（类型、范围、示例）。

### 问题 4：`get_by_window().main_calls` 对新 call_type 统计不完整
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2240,2376,3265`；`AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:359-361`
- **描述**：
  - 直连路径写入 `main_model_task_wake`、`main_model_checkpoint`。
  - 聚合时 `main_calls` 仅统计 `main_model`，导致按窗口主模型调用次数被低估。
- **修复建议**：将 `main_calls` 统计改为 `k.startswith("main_model")` 或维护 call_type 映射表。

### 问题 5：debounce 写盘仍缺少停机前最终 flush
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:190-201`，`main.py` 仅见 `on_loaded`（`main.py:522`）无对应卸载刷盘逻辑
- **描述**：5 秒 debounce 减压有效，但进程异常退出/重启前最后一批记录仍可能丢失。
- **修复建议**：在插件卸载/进程退出钩子中显式 `await _flush()`（并取消 pending handle）。

### 问题 6：主模型旁路调用仍未遵循动静分离策略
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2190,2313`
- **描述**：`_wake_main_for_task` 与 `_checkpoint_review` 仍把动态时间写在 `systemInstruction`，与主链路注入策略不一致。
- **修复建议**：复用主模型请求构造器，把动态信息移动到 user 前缀。

---

## 四、🟢 微调建议

### 建议 1：统一私聊窗口命名风格
- **位置**：`main.py:676`（`PrivateMessage:`） vs 其他链路使用 `FriendMessage:`（如 `main.py:1983,2117,2842`）
- **建议**：统一为单一命名，减少跨模块统计/检索歧义。

---

## 五、✅ 做得好的地方

- 三模型主链路静态/动态分离已经成型（FlashLite/工具模型/主模型请求注入路径）。
- 主模型记账链路已补齐：provider 钩子 + 直连路径。
- `sync_trigger_interval` 兼容旧配置键处理正确。
- `CostTracker` 的写盘方式从“每次全量同步写”升级为“debounce + to_thread”，方向正确。
- 启动延迟 cleanup 已接线，历史文件不会无限增长。

---

## 六、测试与验证记录

- 已执行并通过（设置 `PYTHONIOENCODING=utf-8` 后）：
  - `python QQBotPlan/Plan_3/test_stage6_kvcache_all.py`
  - `python QQBotPlan/Plan_3/test_stage7_9_sampling.py`
  - `python QQBotPlan/Plan_3/test_stage11_cost_tracker.py`
- 语法检查通过：
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/main.py AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py AstrBot/data/plugins/astrbot_plugin_flashlite/kv_cache.py`

> 注：现有测试以静态字符串断言为主，对并发串窗、错误配置容错、停机 flush 等运行时问题覆盖不足。
