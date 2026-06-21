# Plan 3 KVCache + 采样优化 + 成本监控 — Codex Review 任务

## 审核目标

请对以下文件的修改进行全面审核，这些修改实现了 AstrBot FlashLite 插件的三大优化：

1. **KVCache 优化（Stage 1-6）**: 三个模型（FlashLite、主模型、工具模型）的 system prompt 静态/动态分离
2. **采样优化（Stage 7-9）**: 智能动态采样 + 时间兜底参数化 + 每群独立配置
3. **成本监控（Stage 11-13）**: CostTracker 模块 + API 调用成本追踪

## 需要审核的文件

### 核心修改
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py` — 主要修改文件（5700+ 行）
  - `_build_flash_lite_system()` — FlashLite 纯静态 system prompt
  - `_call_flash_lite()` — 动态前缀注入 + 成本记录
  - `inject_flashlite_context()` — 主模型静态/动态分离
  - `_build_tool_model_system()` — 工具模型纯静态 system prompt
  - `_call_tool_model()` — 动态前缀注入 + 成本记录
  - `_calc_dynamic_interval()` — 智能动态采样
  - `_get_effective_interval()` — 群覆盖 > 动态 > 固定优先级链
  - `__init__()` — 新增配置初始化（采样参数、CostTracker 等）

### 新增文件
- `AstrBot/data/plugins/astrbot_plugin_flashlite/cost_tracker.py` — 成本追踪模块
- `AstrBot/data/plugins/astrbot_plugin_flashlite/_conf_schema.json` — 面板配置 schema

## 审核重点

1. **正确性**: 静态/动态分离是否彻底？有没有遗漏的动态内容仍在 system prompt 中？
2. **兼容性**: 修改是否影响了 checkpoint 压缩、定期 Review、私聊路径等其他功能？
3. **性能**: CostTracker 的异步写入是否可能造成问题（如 event loop 阻塞、文件锁竞争）？
4. **边界情况**: 
   - 动态前缀注入失败时的降级处理
   - 空 Knowledge / 空 Memory 时的表现
   - CostTracker 数据目录不存在时的处理
5. **代码质量**: 命名、注释、错误处理是否规范
6. **安全性**: API Key 处理、文件写入路径等

## 输出要求

请将审核报告输出到指定的输出文件路径，格式为 Markdown，包含：
- 发现的问题（按严重程度分类：Critical / Warning / Info）
- 每个问题的具体位置和修复建议
- 总体评价
