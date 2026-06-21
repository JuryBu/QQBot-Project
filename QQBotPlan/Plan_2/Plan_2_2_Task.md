# Plan_2_2 问题 9：Prompt 系统重构 — Task 清单

> 源文件：`Plan_2_2.md` 问题 9（9-A/9-B/9-C）
> 讨论记录：`QQBotPlan/Prompt注入讨论记录.md`
> 核心代码：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`
> 审计参考：`QQBotPlan/提示词审计/Prompt_主模型.md` / `Prompt_FlashLite.md` / `Prompt_工具模型.md`

---

## Stage 1: 主模型提示词基础修复（9-A 体系/时间/上下文层）

> ⚠️ 执行原则：开始前对照 `Plan_2_2_Task.md` 和 `Plan_2_2_ImplPlan.md`；提示词宁滥毋缺但不过度；每步完成后自检测试；以挑剔视角 Review。

- [x] 1.1 编写体系认知 Section——新增 Section 0（最高优先级）
  - 告知模型：你在 AstrBot + FlashLite 中断引擎体系下运行
  - 告知模型：你的文字输出 → AstrBot → QQ 消息窗口
  - 告知模型：FlashLite 帮你筛选消息 + 工具模型帮你执行后台任务
  - 告知模型：function_call 由框架自动执行，文本回复自动发送到 QQ
- [x] 1.2 注入系统时间（含在 1.1 Section 0 中）到主模型 inject_parts
  - 在 `on_llm_request` 的 inject_parts 开头加入 `datetime.now()` 格式时间
- [x] 1.3 增强上下文注入——CONTEXT_SUMMARY+最近N条消息原文注入完成——FlashLite 摘要扩展
  - Section 2 的 `context_summary` 从一句话摘要 → 包含最近 N 条关键消息原文
  - 注入回复消息的原文和附件信息（解决"看不到回复"问题）
- [x] 1.4 统一 OFFICE 处理规范
  - 删除 Section 9 中的 `view_file 提取文本(pdfplumber)` 指引
  - 保留 Section 13 的 `web_fetch` 方案作为唯一规范
  - 合并到一处，消除矛盾
- [x] 1.5 丰富 Section 14 Sandbox 环境信息
  - 补充：内存限制、执行超时限制、磁盘空间、已安装核心 pip 包
- [x] 1.6 自检+测试 Stage 1（语法校验通过）

---

## Stage 2: FlashLite 消息上下文与身份修复（9-B 身份/格式层）

> ⚠️ 执行原则：开始前对照 `Plan_2_2_Task.md` 和 `Plan_2_2_ImplPlan.md`；提示词宁滥毋缺但不过度；每步完成后自检测试；以挑剔视角 Review。

- [x] 2.1 修改 `_get_recent_context`（L1220-1222）消息拼接格式
  - 从 `[时间] 名称: 内容` → `[时间] 昵称(QQ号): 内容`
  - Bot 消息标记为 `[时间] 老板娘 [BOT]: 内容`
- [x] 2.2 更新 FlashLite 提示词前置说明
  - 在 `_build_flash_lite_system` 开头明确说明：`[BOT]` 标记的消息是 Bot 自己发的
  - 说明 QQ 号是跨窗口唯一标识
- [x] 2.3 统一 FlashLite 两套 prompt 输出格式（新增 CONTEXT_SUMMARY 字段）
  - `_build_flash_lite_system`（标记行格式）和 `_build_judgment_prompt`（JSON 格式）
  - 统一为一种输出格式，消除模型困惑
- [x] 2.4 收紧触发规则措辞（5触发+4不触发具体规则）
  - 减少主观判断空间，补充具体的触发/不触发示例
- [x] 2.5 自检+测试 Stage 2

---

## Stage 3: Memory 迷你索引注入（9-B Memory 层 — 思路 C）

> ⚠️ 执行原则：开始前对照 `Plan_2_2_Task.md` 和 `Plan_2_2_ImplPlan.md`；提示词宁滥毋缺但不过度；每步完成后自检测试；以挑剔视角 Review。

- [x] 3.1 实现 Memory 迷你索引构建函数
  - 查询所有 Memory 条目 → 取 `序号 + title + category + pinned + tags`
  - 格式：`[1] "标题" [pinned] #分类 #标签`
  - 控制总长度：超过阈值时只取 pinned + 最近更新的
- [x] 3.2 注入迷你索引到 FlashLite 提示词
  - 在 `_build_flash_lite_system` 末尾追加 `## Memory 索引` 段
  - 更新 FlashLite 指令：`MEMORY_HINT` 改为输出序号（精确指定）
- [x] 3.3 修改 Memory 召回代码（群聊+私聊两处）
  - 解析 `MEMORY_HINT=1,3` → 按序号精确读取完整 content
  - 替换当前的模糊 `memory.query(keyword)` 方式
- [x] 3.4 边界处理（空/过多/无效序号）
  - Memory 为空时：不注入索引段
  - Memory 过多时（>100条）：只注入 pinned + 最近 50 条
  - 序号超范围时：忽略无效序号
- [x] 3.5 自检+测试 Stage 3（语法通过）

---

## Stage 4: 工具模型架构升级（9-C 工具共享/Task 管理）

> ⚠️ 执行原则：开始前对照 `Plan_2_2_Task.md` 和 `Plan_2_2_ImplPlan.md`；提示词宁滥毋缺但不过度；每步完成后自检测试；以挑剔视角 Review。

- [x] 4.1 将主模型工具库注入工具模型 functionDeclarations
  - 读取 `Sandbox/base_tools/*.tool.json` → 转换为 Gemini functionDeclaration 格式
  - 排除不适合工具模型的工具（如 QQ 消息发送相关）
  - 添加到 `_call_tool_model` 的 `tool_declarations` 列表
- [x] 4.2 扩展 `_execute_agent_tool` 工具路由
  - 新增路由：sandbox_exec/search/memory_write/memory_query/web_fetch/save_data 等
  - 复用已有的 `tool_xxx` 方法作为执行后端
- [x] 4.3 为 `_execute_agent_tool` 增加超时保护
  - 每个工具调用包装 `asyncio.wait_for(coro, timeout=X)`
  - 读取对应 `.tool.json` 的 `timeout_ms` 字段作为超时参数
  - 超时返回错误信息而非卡死
- [x] 4.4 `max_agent_steps` 改为可配置
  - 默认值从 10 → 30
  - 支持在 `_call_tool_model` 调用时传入 `max_steps` 参数
- [x] 4.5 Task 管理增强（tool_task_set）——name字段+list增强+window_id/created_at
  - 支持自定义 `name` 字段
  - 改进 `action=list` 展示（含 name + 进度 + 状态）
  - 支持多 task 并发管理
- [x] 4.6 自检+测试 Stage 4（语法通过 但 4.5 未完成）

---

## Stage 5: 新工具 + 草稿/指针使用规范（9-A/9-C 工具层+思考层）

> ⚠️ 执行原则：开始前对照 `Plan_2_2_Task.md` 和 `Plan_2_2_ImplPlan.md`；提示词宁滥毋缺但不过度；每步完成后自检测试；以挑剔视角 Review。

- [x] 5.1 新增 `wait` 工具——tool.json + tool_wait 方法已实现（asyncio.sleep 1-300s）
  - 参数：`seconds`（等待秒数，上限 300）
  - 实现：`asyncio.sleep(seconds)` + 返回唤醒确认
  - 注册到主模型和工具模型
- [x] 5.2 新增 `grep` 工具——tool.json + tool_grep 方法已实现（大小写不敏感文件搜索）
  - 参数：`pattern`（搜索模式）、`path`（搜索路径，默认 workspace/）
  - 实现：Sandbox 内文件内容搜索，返回匹配行+文件路径
  - 注册到主模型和工具模型
- [x] 5.3 编写草稿纸使用规范——工具模型+主模型均已有具体操作指引
  - 写入主模型和工具模型的 systemInstruction
  - 含：用途（思考/规划/中间结果）、命名规范、清理规则
- [x] 5.4 编写 workspace/base_tools 使用规范——注入工具模型 prompt
  - `.tool.json` schema 文档
  - 如何创建自定义工具的指南
- [x] 5.5 文档化指针系统——注入主模型文件获取流程第7条
  - 统一格式规范、使用场景说明
- [x] 5.6 补充工具使用场景指南——6种场景注入工具模型 prompt
  - 什么时候用什么工具、工具组合最佳实践
- [x] 5.7 自检+测试 Stage 5（语法通过）

---

## Stage 6: KVCache 激活 + 输出机制优化（9-A 输出/缓存层）

> ⚠️ 执行原则：开始前对照 `Plan_2_2_Task.md` 和 `Plan_2_2_ImplPlan.md`；提示词宁滥毋缺但不过度；每步完成后自检测试；以挑剔视角 Review。

- [x] 6.1 激活 KVCache — FlashLite 模型——已接入 ensure_cache + 优雅降级（token不足时fallback原方案）
  - 在 `_call_flash_lite` 中接入 `_kv_cache.ensure_cache()`
  - 将固定内容（system prompt + Knowledge）作为 cache 内容
  - 动态内容（消息上下文/Memory 索引）保持每次传入
- [x] 6.2 激活 KVCache — 工具模型——独立 _tool_kv_cache + 循环外缓存 system prompt
  - 在 `_call_tool_model` 中接入 `_kv_cache`（或独立 KVCacheManager 实例）
  - system prompt 固定部分缓存
- [x] 6.3 评估主模型 KVCache 可行性——主模型走 AstrBot OpenAI 兼容层无法直接接入 Gemini cachedContent，需框架改造。标记为 Plan_2_3 后续项
  - 主模型走 AstrBot 框架 `gemini_source.py`，需要确认框架是否支持 cachedContent
  - 如不支持：记录为后续框架层改进
- [x] 6.4 分段系统——确认 BossLady Console 分段配置正常工作（短/中/长句分级延迟+MD清洗+最大3段），已更新提示词告知模型分段系统会自动处理输出
  - 方案 A：模型输出 `[CONTINUE]` 标记 → 框架检测后自动续发
  - 方案 B：移除提示词中"分多轮说"，改为"控制长度"
- [x] 6.5 persona 锚定——Section 0 末尾新增身份锚定段
  - 方案：将 Section 1 核心约束合并到 persona prompt 内部
- [x] 6.6 全面回归测试（py_compile 全通过 + 静态检查）
- [x] 6.7 保存记忆到 MCP memory-store
