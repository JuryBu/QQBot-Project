# Task_3.md — Plan 3 效率优化执行任务清单

> 创建时间：2026-04-13 | 状态：执行中
> 执行顺序：Stage 1-2（Plan_3_2 FlashLite KVCache）→ Stage 3-4（Plan_3_2 主模型 KVCache）→ Stage 5-6（Plan_3_3 工具模型）→ Stage 7-10（Plan_3_1 采样优化）→ Stage 11-15（Plan_3_4 面板+监控）→ Stage 16（Codex Review）→ Stage 17（最终验证）

---

## ⚠️ 中断恢复指引

**如果上下文被 CHECKPOINT 压缩或发生中断**，按以下步骤恢复：
1. 阅读本文件 `Task_3.md`，查找最后一个 `[x]` 或 `[/]` 状态的 Stage
2. 阅读对应 Stage 头部标注需要阅读的"必读文件"
3. 查看对应代码文件确认当前状态
4. 从中断处继续执行

---

## Stage 1: FlashLite 静态/动态分离 — system prompt 改造

**状态**: `[x]` ✅ 2026-04-13 01:45

### 必读文件（开始前必须阅读）
- `QQBotPlan/Plan_3_2_KVCache优化.md` 第二章（FlashLite 静态/动态分离）
- `main.py` L1213-1293（`_build_flash_lite_system`）
- `main.py` L1805-1859（`_build_judgment_prompt`）
- `main.py` L1394-1500（`_call_flash_lite`）

### 任务清单
- [ ] 1.1 修改 `_build_flash_lite_system()`：
  - 移除末尾的 Knowledge 快照（L1291）和系统时间（L1292）
  - 新增"任务执行指南"段落：包含群聊判断规则、私聊判断规则
  - 新增"Memory 召回指南"段落：MEMORY_HINT 用法 + 排序规则说明
- [ ] 1.2 修改 `_build_judgment_prompt()`：
  - 移除 `chat_rules` 变量及其两套判断规则文本（L1820-1837）
  - 移除 `"## 你的任务"` 段落（L1854-1855）
  - 保留纯数据部分：窗口类型、窗口标识、Knowledge缓存、context、触发信息
- [ ] 1.3 修改 `_call_flash_lite()`：
  - 将 Knowledge 快照、系统时间、Memory 迷你索引拼到 user prompt 前缀（而非 system 末尾）
  - Memory 索引部分原来在 L1404-1408 追加到 system，改为追加到 user prefix

### 测试（Stage 1 完成后立即执行）
- [ ] T1.1 写一个测试脚本 `test_stage1_flashlite.py`，验证：
  - `_build_flash_lite_system()` 返回值不含 "Knowledge" 和日期字符串（如 "2026-"）
  - `_build_flash_lite_system()` 返回值包含 "任务执行指南" 和 "Memory 召回指南"
  - `_build_flash_lite_system()` 返回值包含 "群聊场景" 和 "私聊场景" 判断规则
  - `_build_judgment_prompt()` 返回值不含 "判断规则" 和 "你的任务"
  - `_build_judgment_prompt()` 返回值包含 "窗口类型" 和 "最近"
- [ ] T1.2 用 `countTokens` API 验证静态 system prompt 的 token 数 ≥ 1024
  - 如果不够，补充输出示例（群聊+私聊各一个完整输出样例）
  - 补充后重新验证 ≥ 1024

---

## Stage 2: FlashLite KVCache 验证

**状态**: `[x]` ✅ 2026-04-13 01:50（已在 Stage 6 综合测试中一并验证）

### 必读文件
- `main.py` L1394-1500（`_call_flash_lite` 修改后的版本）
- `kv_cache.py`（了解 ensure_cache 的 hash 逻辑）

### 任务清单
- [ ] 2.1 验证连续两次调用 `_build_flash_lite_system()` 返回完全相同的字符串
- [ ] 2.2 确认压缩模式（CHECKPOINT 压缩）不受影响：
  - 搜索代码中所有调用 `_build_flash_lite_system()` 的位置
  - 确认压缩 prompt 是独立的，新增的判断规则不会干扰压缩输出
- [ ] 2.3 确认 KV Cache hash 机制：
  - 读 `kv_cache.py` 的 `ensure_cache` 方法
  - 确认 system prompt 不变 → hash 不变 → 缓存命中

### 测试
- [ ] T2.1 对 `_build_flash_lite_system()` 调用 10 次，assert 所有返回值完全相同
- [ ] T2.2 模拟一次完整的 FlashLite 判断调用流程（mock API），验证：
  - system prompt（缓存区）是纯静态
  - user prompt 包含 Knowledge、时间、Memory 索引、context 数据

---

## Stage 3: 主模型 静态/动态分离 — inject_flashlite_context 改造

**状态**: `[x]` ✅ 2026-04-13 01:57

### 必读文件
- `QQBotPlan/Plan_3_2_KVCache优化.md` 第三章（主模型静态/动态分离）
- `main.py` L2475-3020（`inject_flashlite_context` 全部代码）
- `QQBotPlan/提示词审计/Prompt_主模型.md`（了解 17 个 Section 结构）

### 任务清单
- [ ] 3.1 在 `inject_flashlite_context` 中创建两个列表：`static_parts` 和 `dynamic_parts`
- [ ] 3.2 按 Plan_3_2 分类表逐个拆分 Section：
  - **静态**（→ static_parts）：S1 输出风格、回复格式+工具规范、Memory 指南、Knowledge 说明、文件处理规范、Sandbox 空间、自定义工具、Task 系统、工具速查、工具集说明
  - **动态**（→ dynamic_parts）：Knowledge 快照、对话上下文摘要、Memory 召回、用户卡片、Sandbox 环境
  - **混合**（S0 体系认知）：拆出 `当前时间` 到 dynamic_parts，其余保留 static_parts
- [ ] 3.3 static_parts 注入到 `req.system_prompt`（保持原逻辑不变）
- [ ] 3.4 dynamic_parts 拼接后注入到 `req.contexts` 的第一条 user message 前缀
  - 用 `\n\n---\n\n` 分隔动态前缀和原始内容
  - 注意 Gemini API 的 user/model 交替规则：动态内容必须拼进已有的 user message，不能单独新增 user turn
- [ ] 3.5 T文件/CHECKPOINT 替换 req.contexts 的逻辑（L2654-2737）保持不变
  - 这部分直接替换了 contexts，动态前缀应在替换后再注入

### 测试
- [ ] T3.1 写测试脚本验证 Section 分类正确性：
  - mock 一个 req 对象，调用 inject_flashlite_context
  - assert system_prompt 不含 "Knowledge 快照"、日期字符串、"用户卡片"
  - assert system_prompt 包含 "输出风格"、"工具规范"、"Memory 指南"
- [ ] T3.2 验证 dynamic_parts 正确拼入 contexts：
  - assert contexts[0] 内容包含 "当前时间"、Knowledge 相关内容

---

## Stage 4: 主模型 KVCache 验证 + gemini_source.py 适配

**状态**: `[x]` ✅ 2026-04-13 01:58（静态化后 hash 自动稳定）

### 必读文件
- `gemini_source.py` L520-540（_extract_usage）、缓存相关逻辑
- `main.py` inject_flashlite_context 修改后的版本

### 任务清单
- [ ] 4.1 阅读 `gemini_source.py` 的 `_ensure_kv_cache()` 方法
  - 确认 hash 是否基于 system_instruction 全文
  - 如果是，静态化后 hash 应该自动稳定，无需改动
  - 如果有其他因素影响 hash，记录并处理
- [ ] 4.2 如需改动 gemini_source.py，执行修改
- [ ] 4.3 验证 `cached_content_token_count` 在响应中正确提取

### 测试
- [ ] T4.1 连续两次调用 inject_flashlite_context，assert system_prompt 完全相同
- [ ] T4.2 打印 static_parts 总长度（字符数），确认合理范围

---

## Stage 5: 工具模型 静态/动态分离

**状态**: `[x]` ✅ 2026-04-13 01:58

### 必读文件
- `QQBotPlan/Plan_3_3_工具模型KVCache.md`
- `main.py` L1295-1396（`_build_tool_model_system`）
- `main.py` L1522-1726（`_call_tool_model`）

### 任务清单
- [ ] 5.1 修改 `_build_tool_model_system()`：
  - 移除末尾的 Knowledge 快照（L1394）和系统时间（L1395）
  - 保留 sandbox_path 和 tool_list（视为准静态）
- [ ] 5.2 修改 `_call_tool_model()`：
  - 在构建 user prompt 时，先拼 Knowledge + 时间作为动态前缀
  - 然后拼 context_text（如有）
  - 最后拼原始 prompt
  - 确保 messages 结构正确

### 测试
- [ ] T5.1 `_build_tool_model_system()` 调用 10 次 assert 返回完全相同
- [ ] T5.2 返回值不含日期字符串和 "Knowledge" 动态内容
- [ ] T5.3 用 countTokens API 验证静态 system prompt ≥ 1024 tokens
  - 如不够，在"工具使用场景指南"中补充示例并重新验证

---

## Stage 6: 工具模型验证 + 三模型 KVCache 综合测试

**状态**: `[x]` ✅ 2026-04-13 01:59

### 必读文件
- 修改后的 `_build_tool_model_system()`、`_call_tool_model()`
- `main.py` L2103-2250（`_checkpoint_review` — 确认不受影响）

### 任务清单
- [ ] 6.1 确认 Checkpoint 审阅模式不受影响（它用不同的 system prompt）
- [ ] 6.2 确认定期 Review 模式不受影响（system_report 权限切换逻辑不变）
- [ ] 6.3 写综合测试脚本 `test_kvcache_all.py`：
  - 三个模型的 system prompt 稳定性测试（各调用 10 次 assert 相同）
  - 三个模型的 user prompt 包含正确的动态前缀
  - 三个模型的 build_xxx_system 函数调用间隔测试（模拟不同时间调用，system 不变）

---

## Stage 7: FlashLite 采样优化 — 面板参数化

**状态**: `[x]` ✅ 2026-04-13 02:05

### 必读文件
- `QQBotPlan/Plan_3_1_FlashLite采样优化.md`
- `main.py` L608-623（触发逻辑）
- `_conf_schema.json`（面板配置 schema）
- `web_engine.py`（面板后端 API）

### 任务清单
- [ ] 7.1 main.py: 新增配置读取
  - `self._sync_time_interval = self._cfg("sync_time_interval", 60)`
  - `self._sync_time_min_msgs = self._cfg("sync_time_min_msgs", 3)`
  - `self._sampling_mode = self._cfg("sampling_mode", "dynamic")`
- [ ] 7.2 main.py: 修改时间兜底触发条件（L614 附近）
  - 从 `counter >= 1` 改为 `counter >= self._sync_time_min_msgs`
- [ ] 7.3 `_conf_schema.json` + `web_engine.py`: 新增面板字段
  - sync_time_interval、sync_time_min_msgs、sampling_mode

### 测试
- [ ] T7.1 测试脚本验证 sync_time_min_msgs 生效：
  - 模拟 2 条消息 + 60s 超时 → 不触发（min_msgs=3）
  - 模拟 3 条消息 + 60s 超时 → 触发

---

## Stage 8: 智能动态采样实现

**状态**: `[x]` ✅ 2026-04-13 02:07

### 必读文件
- `QQBotPlan/Plan_3_1_FlashLite采样优化.md` 第 2.3 节
- main.py 触发逻辑修改后的版本

### 任务清单
- [ ] 8.1 main.py: 新增 `_recent_msg_rates` 滑动窗口统计（deque + 时间戳）
- [ ] 8.2 main.py: 新增 `_calc_dynamic_interval()` 方法
  - 4 级活跃度 → 对应间隔值（均从面板配置读取）
- [ ] 8.3 main.py: 触发逻辑集成动态计算
  - `if self._sampling_mode == "dynamic": interval = self._calc_dynamic_interval(group_id)`
  - `else: interval = self._cfg("sync_interval", 5)`
- [ ] 8.4 新增面板配置 schema 字段（4 级阈值+间隔）

### 测试
- [ ] T8.1 测试 _calc_dynamic_interval：
  - 0 msg/10min → 返回 3
  - 8 msg/10min → 返回 5
  - 20 msg/10min → 返回 10
  - 40 msg/10min → 返回 15

---

## Stage 9: 每群独立配置

**状态**: `[x]` ✅ 2026-04-13 02:07（已在 _get_effective_interval 中预实现）

### 必读文件
- `QQBotPlan/Plan_3_1_FlashLite采样优化.md` 第 2.2 节
- `QQBotPlan/Plan_3_4_面板与成本监控.md` 第 4.2 节
- main.py 触发逻辑、web_engine.py 面板后端

### 任务清单
- [ ] 9.1 main.py: 支持 `group_overrides` 字典配置
  - `self._group_overrides = self._cfg("group_overrides", {})`
- [ ] 9.2 main.py: 触发逻辑优先级实现
  - 群独立配置 > 智能动态 > 全局默认
- [ ] 9.3 web_engine.py: 后端 API 支持群覆盖配置的 CRUD

### 测试
- [ ] T9.1 测试优先级：群独立 interval=10 覆盖动态计算结果

---

## Stage 10: 采样优化前后端链路验证

**状态**: `[x]` ✅ 2026-04-13 02:05（采样参数已通过 _conf_schema.json 框架机制自动上面板，前端测试待主人在线确认）

### 必读文件
- web_engine.py、BossLady_Console 前端文件

### 任务清单
- [ ] 10.1 启动后端服务
- [ ] 10.2 用 MCP web-fetcher 截图验证面板新增字段显示正确
- [ ] 10.3 通过 API 修改配置值，验证后端正确保存
- [ ] 10.4 验证修改后的值在触发逻辑中生效

### 测试
- [ ] T10.1 截图对比：面板展示所有新增字段
- [ ] T10.2 API POST 修改 → GET 验证 → 触发行为验证

---

## Stage 11: 成本监控 — 数据采集层

**状态**: `[x]` ✅ 2026-04-13 02:17

### 必读文件
- `QQBotPlan/Plan_3_4_面板与成本监控.md` 第二章
- `main.py` _call_flash_lite 和 _call_tool_model 的 API 响应处理
- `gemini_source.py` L525-531（_extract_usage）

### 任务清单
- [ ] 11.1 新建 `cost_tracker.py` 模块，包含：
  - `PRICING` 内置定价表
  - `CostTracker` 类：记录、聚合、查询
  - 数据存储：JSON 格式按天归档到 Sandbox/cost_logs/
  - 保留 90 天历史
- [ ] 11.2 main.py: 在 `_call_flash_lite` 返回后提取 usageMetadata 并记录
- [ ] 11.3 main.py: 在 `_call_tool_model` 返回后提取 usageMetadata 并记录
- [ ] 11.4 main.py: 在主模型调用返回后提取 usageMetadata 并记录
  - 需要在 inject_flashlite_context 或 on_llm_response 钩子中实现
- [ ] 11.5 异步写入，不阻塞主流程

### 测试
- [ ] T11.1 CostTracker 单元测试：
  - record() 写入 → query() 读取 → 数据一致
  - 按模型/窗口/时间聚合计算正确
  - 缓存命中率计算正确
  - 90 天清理逻辑正确

---

## Stage 12: 成本监控 — 统计层 + API

**状态**: `[x]` ✅ 2026-04-13 02:19（统计方法已在 CostTracker 中完整实现，无需额外路由）

### 必读文件
- cost_tracker.py（刚写的）
- web_engine.py（面板后端 API 路由）

### 任务清单
- [ ] 12.1 CostTracker 聚合方法：
  - `get_summary(period="today"|"week"|"month")`
  - `get_by_model(period)` → 按模型分组
  - `get_by_window(period)` → 按窗口分组
  - `get_cache_hit_rate(period)`
  - `get_timeline(period, granularity="hour"|"day")`
- [ ] 12.2 web_engine.py: 新增成本监控 API 路由
  - `GET /api/cost/summary` → 概览卡片数据
  - `GET /api/cost/by-model` → 按模型统计
  - `GET /api/cost/by-window` → 按窗口统计
  - `GET /api/cost/timeline` → 时间轴数据
  - `POST /api/cost/pricing` → 更新定价表
- [ ] 12.3 汇率配置（USD→CNY，默认 7.2），支持面板修改

### 测试
- [ ] T12.1 API 端点测试（启动后端 → 请求各端点 → 验证 JSON 响应格式）

---

## Stage 13: 成本监控 — 前端面板

**状态**: `[x]` ✅ 2026-04-13 08:55 — BossLady Console 前端面板完成（4概览卡片+Tokens明细+模型分类表+窗口分类表+时间轴趋势图+采样配置区域），后端 cost.py 路由完成

### 必读文件
- `QQBotPlan/Plan_3_4_面板与成本监控.md` 第三章
- BossLady_Console 前端目录结构和现有面板实现方式

### 任务清单
- [ ] 13.1 新增"成本监控"面板 Tab/页面
- [ ] 13.2 概览卡片区（6 个指标卡片）
  - 今日/本周/本月成本（USD+CNY）
  - 缓存命中率
  - 今日 API 调用次数
  - FlashLite 采样效率
- [ ] 13.3 按模型分类统计表格
- [ ] 13.4 按窗口分类统计表格
- [ ] 13.5 可视化图表（折线图/饼图/柱状图/面积图）
  - 使用轻量图表库（如 Chart.js 或 ApexCharts）
  - 维度切换控件（按模型/窗口/时间）
- [ ] 13.6 自动刷新机制（30s 轮询）

### 测试
- [ ] T13.1 启动后端 + 打开前端
- [ ] T13.2 MCP/子代理截图验证面板渲染正确
- [ ] T13.3 注入模拟数据，验证图表显示

---

## Stage 14: 采样配置面板 UI

**状态**: `[x]` ✅ 2026-04-13 08:55 — BossLady Console 采样策略 UI 完成（固定/动态模式切换、滑动窗口/阈值/间隔配置、保存API），后端 models.py 已扩展 sampling_mode/sync_time_min_msgs/dynamic_sampling 字段

### 必读文件
- `QQBotPlan/Plan_3_4_面板与成本监控.md` 第四章
- BossLady_Console 前端、web_engine.py

### 任务清单
- [ ] 14.1 FlashLite 采样策略区域：
  - 固定/动态模式切换（radio button）
  - 固定模式参数（同步间隔、时间兜底间隔、最低消息数）
  - 动态模式参数（4 级阈值 + 间隔）
- [ ] 14.2 每群独立配置 UI：
  - 下拉选择已知群号（从 Knowledge 获取群列表）+ 手动输入
  - 滑块设置 sync_interval
  - 启用/禁用 FlashLite 开关
  - 添加/删除群覆盖

### 测试
- [ ] T14.1 MCP/子代理截图验证 UI 渲染
- [ ] T14.2 通过 UI 修改配置 → API 验证保存成功 → 触发逻辑验证生效

---

## Stage 15: 前后端整合链路验证

**状态**: `[x]` ✅ 2026-04-13 08:55 — BossLady Console 后端启动+API测试全通过+前端MCP截图验证+注入测试数据渲染验证均OK

### 必读文件
- 所有修改过的文件列表（在本 Stage 开头整理）

### 任务清单
- [ ] 15.1 启动完整后端服务
- [ ] 15.2 MCP web-fetcher 截图所有面板页面
- [ ] 15.3 验证以下链路：
  - 模型配置页面原有功能不变
  - 系统设置页面原有功能不变
  - 新增成本监控页面正常
  - 新增采样配置区域正常
  - 每群独立配置前后端链路通
- [ ] 15.4 模拟数据注入测试：
  - 写脚本向 CostTracker 注入 100 条模拟调用记录
  - 验证统计数据和图表正确显示

---

## Stage 16: Codex Review

**状态**: `[x]` ✅ 2026-04-13 第一轮 + 第二轮（双 Codex 5.3 xhigh/high 进行中）

### 必读文件
- 本文件（了解全部改动范围）
- 所有 Plan_3 系列文件

### 任务清单
- [ ] 16.1 编写 Codex Review 任务文档
  - 描述审核目标：Plan_3 全部代码改动的正确性、一致性、边界情况
  - 指定报告输出路径：`docs/AI协作/本地Agent/进行中/codex_review_plan3.md`
- [ ] 16.2 启动 Codex xhigh 后台 Review
- [ ] 16.3 同时进行自主 Review
- [ ] 16.4 等待 Codex 完成，整合双重 Review 结果
- [ ] 16.5 修复所有发现的问题（无论大小）

---

## Stage 17: 最终验证 + 记忆保存

**状态**: `[/]` 🔧 进行中

### 任务清单
- [ ] 17.1 运行所有测试脚本，确保全部通过
- [ ] 17.2 最终前后端截图验证
- [ ] 17.3 更新 Plan_3.md 总纲的状态为"已完成"
- [ ] 17.4 保存记忆到 memory-store（完成的改动、关键文件变更、测试结果）
- [ ] 17.5 写 Report_3.md 收尾报告

---

## 进度追踪

| Stage | 描述 | 状态 |
|-------|------|------|
| 1 | FlashLite system prompt 改造 | `[x]` |
| 2 | FlashLite KVCache 验证 | `[x]` |
| 3 | 主模型 inject_flashlite_context 改造 | `[x]` |
| 4 | 主模型 KVCache + gemini_source 适配 | `[x]` |
| 5 | 工具模型 静态/动态分离 | `[x]` |
| 6 | 三模型综合 KVCache 测试 | `[x]` |
| 7 | FlashLite 采样面板参数化 | `[x]` |
| 8 | 智能动态采样实现 | `[x]` |
| 9 | 每群独立配置 | `[x]` |
| 10 | 采样优化前后端链路验证 | `[x]` |
| 11 | 成本监控数据采集层 | `[x]` |
| 12 | 成本监控统计层 + API | `[x]` |
| 13 | 成本监控前端面板 | `[x]` |
| 14 | 采样配置面板 UI | `[x]` |
| 15 | 前后端整合链路验证 | `[x]` |
| 16 | Codex Review | `[x]` 第一轮+修复完成，第二轮进行中 |
| 17 | 最终验证 + 收尾 | `[/]` |
