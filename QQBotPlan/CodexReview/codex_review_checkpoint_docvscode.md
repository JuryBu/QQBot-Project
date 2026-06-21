# CHECKPOINT 重构 Review 任务（Codex B - 文档 vs 代码对比）

## 审核目标
逐条核对设计文档（Plan_2_CP 系列）与实际代码实现的匹配程度，找出文档承诺但未实现的功能、代码实现但文档未覆盖的逻辑。

## 审核方法

### 步骤 1：提取文档中的所有设计决策
从以下文档提取所有明确的设计承诺：
- `QQBotPlan/Plan_2_CP.md` — 已确认决策清单（10 条）
- `QQBotPlan/Plan_2_CP_architecture.md` — 三系统分立架构
- `QQBotPlan/Plan_2_CP_T_file.md` — T 文件格式规范
- `QQBotPlan/Plan_2_CP_compression.md` — 压缩触发参数表
- `QQBotPlan/Plan_2_CP_integration.md` — 代码修改清单

### 步骤 2：逐条验证代码实现
对每个决策/规范，在以下代码中验证是否已实现：
- `AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
- `AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py`
- `BossLady_Console/backend/routers/models.py`
- `BossLady_Console/frontend/app.js`
- `BossLady_Console/frontend/index.html`

### 步骤 3：反向检查
在代码中找到所有 CHECKPOINT 相关逻辑，确认每个逻辑在文档中都有对应描述。

## 重点检查项

1. Plan_2_CP.md 已确认决策清单 10 条 → 逐条验证代码
2. Plan_2_CP_T_file.md 中的 T 文件 JSON 格式 → 对比 checkpoint.py 中 `_create_empty_t_file` 和实际生成的 JSON
3. Plan_2_CP_compression.md 中的参数表（6 个参数）→ 对比 models.py / app.js / index.html / main.py 中实际使用的参数名和默认值
4. Plan_2_CP_integration.md 中的修改清单（7 节）→ 对比实际修改是否完整
5. Plan_2_CP_integration.md 第 4 节"LLM 回复后回写 T 文件" → 这个功能是否已实现？
6. Plan_2_CP_integration.md 第 3 节"FlashLite 上下文来源修改" → 当前 `_build_judgment_prompt` 是否已改为从 T 文件读取？

## 输出要求
请将对比报告写入指定的 outputFile，格式为 Markdown：
- 每条文档设计：一行文档描述 → 一行代码验证结果（✅ 已实现 / ⚠️ 部分实现 / ❌ 未实现）
- 反向检查单独成节
- 最后给出优先级排序的改进建议
