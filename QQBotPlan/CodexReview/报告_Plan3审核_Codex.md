# 审核报告：Plan 3 KVCache / 采样优化 / 成本监控

**审核时间**: 2026-04-13  
**审核范围**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`、`AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`、`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json`  
**整体评价**: 三模型主链路的动静分离思路基本落地，但成本监控链路目前没有闭环，且采样配置存在兼容性与面板可控性缺口，离“可稳定上线并可观测”还有明显距离。

## Critical

### 1. `CostTracker` 的窗口维度统计实际上始终失效
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1607-1614`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1836-1843`
- **问题**: 两处记账都使用 `getattr(self, '_current_window_key', 'unknown')` 作为 `window_key`，但在 `main.py` 中没有任何地方对 `self._current_window_key` 赋值。实际效果是所有成本记录都会落到 `unknown`，`CostTracker.get_by_window()` 的核心能力直接失真。
- **影响**:
  - “按窗口统计成本”功能不可用。
  - 无法判断哪个群/私聊窗口消耗最高，成本优化目标丢失。
  - 后续做分群采样调优时没有可用观测数据。
- **修复建议**:
  - 不要依赖隐式实例字段，改为把 `window_key` 作为显式参数贯穿调用链。
  - 例如将 `_call_flash_lite(prompt)` 改为 `_call_flash_lite(prompt, window_key)`，`_call_tool_model(...)` 同理。
  - 在 `_sync_trigger()` / `_async_trigger()` / `_private_trigger()` / task 入口处拿到窗口标识后直接传入。

### 2. 成本统计没有覆盖主模型调用，报表必然长期低估
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1607-1614`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1836-1843`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2212-2219`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2333-2340`
- **问题**:
  - 当前只有 FlashLite 和工具模型在记账。
  - `call_type="main_model"` 在代码中根本没有落点。
  - 两条直接调用 Gemini 的主模型路径 `_wake_main_for_task()`、`_checkpoint_review()` 读取响应后也没有提取 `usageMetadata` 并记账。
  - 常规主模型 provider 链路只做了 `inject_flashlite_context()`，没有任何成本追踪钩子。
- **影响**:
  - Stage 11-13 所宣称的“API 调用成本追踪”并未完整实现。
  - 报表会系统性低估总成本，尤其在主模型占大头时误差会非常明显。
  - 由于缺少 `main_model` 分类，按模型 / 按调用类型分析都会失真。
- **修复建议**:
  - 为主模型调用统一增加 usage 采集与 `call_type="main_model"` 记账。
  - 对直连 Gemini 的 `_wake_main_for_task()`、`_checkpoint_review()` 至少补充 `usageMetadata` 解析和 `CostTracker.record()`。
  - 如果常规主模型走 AstrBot provider，需要在 provider response hook 或 source 层补一层通用记账，避免漏记。

## Warning

### 3. 采样配置键名已发生不兼容变更，现有部署升级后会静默失效
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:114`, `AstrBot/data/plugins/astrbot_plugin_flashlite/config.json:8`
- **问题**: 新代码读取的是 `sync_trigger_interval`，但默认配置文件仍然保留旧键 `sync_interval`。这会导致旧配置值完全不生效，并悄悄回落到默认值 `5`。
- **影响**:
  - 已上线实例升级后，管理员自定义的采样频率会被忽略。
  - 行为变化没有任何告警，排查成本高。
- **修复建议**:
  - 做兼容读取：优先 `sync_trigger_interval`，其次回退 `sync_interval`。
  - 启动时如果命中了旧键，打印一次迁移警告。
  - 同步更新默认配置文件与文档，避免新旧键并存太久。

### 4. `_conf_schema.json` 没有暴露本次新增的关键参数，面板可控性不达标
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json:1-55`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:142-148`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:718-722`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:232-237`
- **问题**:
  - schema 只暴露了 `sync_trigger_interval`、`sync_time_interval`、`sync_time_min_msgs`、`sampling_mode` 等少量字段。
  - 代码实际还依赖 `dynamic_sampling.thresholds/intervals/window_minutes`、`group_overrides`、`cost_tracker`、`tool_model`、`review_interval_hours` 等参数。
  - 任务文档明确要求“时间兜底参数化 + 每群独立配置 + 成本监控”，但这些能力当前无法通过面板完整配置。
- **影响**:
  - 设计能力和运维入口脱节。
  - 调优要靠手改 JSON，容易出错，也不符合“面板可控性”目标。
- **修复建议**:
  - 在 schema 中补齐 `dynamic_sampling`、`group_overrides`、`cost_tracker`、`tool_model`、`review_interval_hours` 等字段。
  - 对嵌套配置给出示例和约束说明，至少校验数组长度、类型和值范围。

### 5. 工具模型显式 KV Cache 的默认模型与实际调用模型不一致
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:528-530`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1772`
- **问题**:
  - `on_loaded()` 中创建工具模型缓存时，默认模型是 `self._tool_model or FLASH_LITE_MODEL`。
  - `_call_tool_model()` 实际调用时，默认模型却是 `self._tool_model or "gemini-3-flash-preview"`。
  - 当 `tool_model.model` 未配置时，缓存会按 `gemini-3.1-flash-lite-preview` 创建，请求却发给 `gemini-3-flash-preview`。
- **影响**:
  - 显式缓存可能直接失效，甚至触发 Gemini 侧的模型不匹配错误。
  - 老配置或缺省配置下，工具模型链路行为不稳定。
- **修复建议**:
  - 抽一个统一的 `_get_tool_model_name()`，缓存创建和实际调用都走同一个结果。
  - 如果默认就是 `gemini-3-flash-preview`，初始化时也必须一致。

### 6. `CostTracker` 采用“每次记录都 `create_task + 全量重写 JSON`”的方式，容易在高频调用下拖慢事件循环
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:179-183`, `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:185-195`
- **问题**:
  - `record()` 每次调用都会 `asyncio.create_task(self._flush())`。
  - `_flush()` 虽然有锁，但内部使用同步 `open/json.dump`，并且每次都是重写当天全部记录。
  - FlashLite 是高频路径，群消息密集时会快速堆积大量 flush 任务，形成重复 I/O。
- **影响**:
  - 事件循环会被同步磁盘写阻塞。
  - 高峰期会产生很多无意义的重复全量写盘。
  - 进程退出时后台 flush 任务未跑完，最后几条记录可能丢失。
- **修复建议**:
  - 改成单消费者写队列，或 debounce 定时批量刷盘。
  - 文件写入放到 `asyncio.to_thread()` / `aiofiles`，避免阻塞主 loop。
  - 关闭插件时显式 `await flush()`，确保尾部数据落盘。

### 7. 成本日志清理逻辑只定义未接线，90 天保留策略不会生效
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py:398-413`
- **问题**: `cleanup_old()` 已实现，但在 `main.py` 没有任何调度或启动调用。当前只会不断累积 `.json` 日志文件。
- **影响**:
  - 长期运行后磁盘占用只增不减。
  - 与模块注释里“保留 90 天历史”的承诺不一致。
- **修复建议**:
  - 在 `on_loaded()` 或每日首次写入时触发一次清理。
  - 最好做成低频后台任务，例如每天一次。

### 8. 动态采样配置没有防御性校验，错误配置会直接引发运行时异常或异常行为
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:145-148`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:702-713`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:721-722`
- **问题**:
  - `dynamic_sampling.thresholds` / `intervals` / `group_overrides.sync_interval` 都直接信任配置。
  - `intervals` 若为空，`intervals[min(...)]` 会触发 `IndexError`。
  - 阈值无序、间隔为 `0`/负数/字符串时，也没有兜底修正。
- **影响**:
  - 面板或手工配置一旦写错，会把同步触发逻辑直接打坏。
  - 问题表现为“不触发 / 频繁触发 / 运行时报错”，不易定位。
- **修复建议**:
  - 初始化时做一次配置归一化。
  - 保证 `thresholds` 升序、`intervals` 长度至少 1 且全为正整数。
  - 对 `group_overrides.sync_interval` 同样做类型和值校验。

### 9. 两条主模型直连辅助路径绕过了当前的动静分离策略
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2171-2181`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2280-2297`
- **问题**:
  - `_wake_main_for_task()` 和 `_checkpoint_review()` 都把“当前时间”等动态信息直接写进 `systemInstruction`。
  - 这两条路径也没有复用 `inject_flashlite_context()` 的静态/动态拆分逻辑。
- **影响**:
  - Task 唤醒 / Checkpoint 审阅场景无法享受前缀稳定带来的缓存收益。
  - 实现层面形成两套主模型调用范式，后续维护和排障成本变高。
- **修复建议**:
  - 抽一个主模型直连请求构造器，统一处理静态 system prompt 和动态 user 前缀。
  - 即使这些路径调用频率较低，也建议保持架构一致性。

## Info

### 10. 三模型主链路的动静分离方向基本正确，核心动态内容已从主 prompt 里剥离
- **位置**: `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1297-1389`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1495-1588`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1664-1808`, `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2617-3150`
- **观察**:
  - FlashLite 已把 Knowledge / 时间 / Memory 索引转移到 user 前缀。
  - 工具模型已把 Knowledge / 时间从静态 system 区抽离。
  - 主模型 `inject_flashlite_context()` 也明确区分了 `inject_parts` 与 `dynamic_parts`。
- **结论**: 从“核心请求主链路”的角度看，KVCache 静态/动态分离的大方向是成立的，问题主要集中在旁路调用、观测补齐和配置治理上。

## 总体结论

这批修改里，**KVCache 动静分离主线基本做对了**，但**CostTracker 目前还不能称为可用的成本监控系统**。最严重的两个问题是：

1. `window_key` 没有真正传进去，导致按窗口统计全废。  
2. 主模型调用大量漏记，导致总成本长期失真。

如果按上线优先级排序，建议先修下面四件事：

1. 修正 `window_key` 传递链，确保所有记录不再落到 `unknown`。  
2. 把主模型所有调用路径统一接入 `usageMetadata -> CostTracker.record()`。  
3. 兼容旧配置键 `sync_interval`，并补齐 `_conf_schema.json`。  
4. 把 `CostTracker` 改成批量/单写者刷盘，避免高频同步 I/O 压垮事件循环。

## 验证说明

- 已完成静态代码审阅与调用链核对。
- 已执行语法编译检查：
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`
  - `python -m py_compile AstrBot/data/plugins/astrbot_plugin_flashlite/kv_cache.py`
- 本次未执行运行时集成测试，因此对 Gemini API 实际返回结构、AstrBot provider 层 usage 透传情况仍保留少量实现层假设。
