# Plan 3 全面 Review 任务（第二轮）

## 背景
本次 Review 是在第一轮 Codex Review 后，针对修复后代码的二次全面审核。
第一轮发现了 9 个问题（2 Critical + 7 Warning），已全部修复。

## 审核范围

### 核心文件
1. **`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`** — 5870+ 行主逻辑
2. **`AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py`** — 成本追踪模块
3. **`AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json`** — 配置面板 Schema
4. **`AstrBot/data/plugins/astrbot_plugin_flashlite/kv_cache.py`** — KVCache 管理器

### 参考文档（请先阅读）
- `QQBotPlan/Plan_3_1_KVCache优化.md` — KVCache 优化设计文档
- `QQBotPlan/Plan_3_2_采样优化.md` — 采样优化设计文档
- `QQBotPlan/Plan_3_3_CostTracker成本监控.md` — 成本监控设计文档
- `QQBotPlan/Plan_3_4_面板与成本监控.md` — 面板配置设计文档
- `QQBotPlan/Task_3.md` — 任务清单含全部 Stage 描述
- `QQBotPlan/报告_Plan3审核_Codex.md` — 第一轮 Codex Review 报告

### 测试文件
- `QQBotPlan/test_stage6_kvcache_all.py`
- `QQBotPlan/test_stage7_9_sampling.py`
- `QQBotPlan/test_stage11_cost_tracker.py`

## 第一轮修复内容（需确认已正确修复）
1. **window_key 传递** — route_message 中设置 self._current_window_key
2. **主模型记账** — 新增 on_llm_response 钩子 + _wake_main_for_task / _checkpoint_review 直连记账
3. **配置兼容** — sync_trigger_interval 优先，回退 sync_interval
4. **模型常量统一** — TOOL_MODEL_DEFAULT 常量统一
5. **CostTracker 写入优化** — debounce 5s + asyncio.to_thread
6. **cleanup 接线** — 启动时 30s 延迟清理
7. **动态采样防御性校验** — 阈值升序、间隔正整数、长度匹配

## 第二轮审核重点
1. **修复完整性**: 第一轮 9 个问题是否都已正确修复？有无遗漏？
2. **新引入的问题**: 修复过程中有没有引入新的 Bug？
3. **主模型记账覆盖度**: on_llm_response 钩子是否能可靠捕获所有主模型调用的 usage？
4. **CostTracker debounce 可靠性**: debounce 机制在进程异常退出时有无数据丢失风险？
5. **_conf_schema.json 完整性**: 面板配置是否覆盖了所有新增参数？
6. **整体架构一致性**: 三模型 KVCache 分离、采样逻辑、成本记账是否形成一致的闭环

## 输出要求
将审核报告输出到指定的输出文件路径。报告格式为 Markdown，包含：
- 第一轮修复确认（逐项）
- 新发现的问题（按严重程度 Critical/Warning/Info 分类）
- 具体位置和修复建议
- 总体评价
