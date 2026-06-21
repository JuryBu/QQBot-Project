# 审核报告：Plan_2_CP 文档 vs CHECKPOINT 实现对比

**审核时间**: 2026-04-10  
**审核范围**: `QQBotPlan/Plan_2/Plan_2_CP*.md`、`AstrBot/data/plugins/astrbot_plugin_flashlite/checkpoint.py`、`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py`、`AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py`、`BossLady_Console/backend/routers/models.py`、`BossLady_Console/frontend/app.js`、`BossLady_Console/frontend/index.html`  
**整体评价**: T 文件主链路已经接入，但 FlashLite 判断链路、参数命名链路和旧 CHECKPOINT 调用链没有完全收敛，当前属于“核心能力已落地，外围集成明显不一致”的状态。

## 一、Plan_2_CP.md 已确认决策清单 10 条核对

| # | 文档设计 | 代码验证 | 结论 |
|---|---|---|---|
| 1 | 三系统分立：A/B/C 完全独立，C 负责真正上下文。`QQBotPlan/Plan_2/Plan_2_CP.md:44-45` | `checkpoint.py:248-389` 已实现 T 文件系统；`main.py:2668-2712` 已用 T 替换 `req.contexts`。但 FlashLite 判断仍走 `main.py:2274-2338` 的 `messages.db`，未完全转向 C。 | ⚠️ 部分实现 |
| 2 | T1 作为 `{role:"user"}` 注入 contexts 开头，不放 system_prompt。`QQBotPlan/Plan_2/Plan_2_CP.md:45-46` | `checkpoint.py:395-430` 先插入 T1 user 消息，再插入固定 assistant ACK。 | ✅ 已实现 |
| 3 | T 文件持久化到 `QQ_data/checkpoints/`，per-window JSON，重启不丢。`QQBotPlan/Plan_2/Plan_2_CP.md:46-47` | `checkpoint.py:269-339` 采用 per-window 文件路径、自动创建、原子保存。 | ✅ 已实现 |
| 4 | 工具模型默认不带上下文，仅按需传入。`QQBotPlan/Plan_2/Plan_2_CP.md:47-48` | `_call_tool_model()` 在 `main.py:1548-1557` 仅以任务 prompt 初始化消息，不自动拼接 T 上下文。 | ✅ 已实现 |
| 5 | 主模型和 FlashLite 都使用 T。`QQBotPlan/Plan_2/Plan_2_CP.md:48-49` | 主模型已在 `main.py:2709-2712` 使用 T；FlashLite 触发判断仍在 `main.py:738-748`、`903-909`、`1044-1055` 调用 `_get_recent_context()`，而 `_get_recent_context()` 仍读 `messages.db`（`main.py:2274-2338`）。 | ⚠️ 部分实现 |
| 6 | 工具调用全过程记录在 T 中。`QQBotPlan/Plan_2/Plan_2_CP.md:49-50` | `checkpoint.py:345-389`、`395-430` 支持 `tool_calls` / `tool_call_id` 存取；但 `main.py` 没有“回复完成后立即写回 T”的专门钩子，只能依赖下一次 `on_llm_request` 增量同步。 | ⚠️ 部分实现 |
| 7 | 每个窗口独立 T。`QQBotPlan/Plan_2/Plan_2_CP.md:50-51` | `checkpoint.py:263-272` 按 `window_key` 分文件；`checkpoint.py:263-267` 按窗口加锁。 | ✅ 已实现 |
| 8 | 最终发送给 LLM 的是 C，不是 A。`QQBotPlan/Plan_2/Plan_2_CP.md:51-52` | `main.py:2709-2712` 明确执行 `req.contexts = self._t_file_mgr.build_llm_contexts(t_file)`。 | ✅ 已实现 |
| 9 | 压缩率必须保证：Prompt 目标长度 + 后验证 + warning。`QQBotPlan/Plan_2/Plan_2_CP.md:52-53` | `checkpoint.py:141-194` 构建长度约束 Prompt；`checkpoint.py:616-635` 做压缩率验证并记录 warning/info。 | ✅ 已实现 |
| 10 | 与 AstrBot 框架自带压缩平行：FlashLite 先压缩，框架兜底。`QQBotPlan/Plan_2/Plan_2_CP.md:53-54` | 在本次审核范围内，只能确认插件侧已经先于主模型调用执行 T 压缩与替换（`main.py:2684-2712`）；未见插件关闭 AstrBot 兜底压缩，但也未见显式对接说明。 | ⚠️ 部分可证实 |

## 二、Plan_2_CP_T_file.md 核对

### 1. T 文件 JSON 结构

| 文档设计 | 代码验证 | 结论 |
|---|---|---|
| 路径命名 `{window_type}_{window_id}.json`。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:13-16` | `_file_path()` 在 `checkpoint.py:269-272` 用 `window_key.replace(':', '_')` 生成 `GroupMessage_xxx.json` / `FriendMessage_xxx.json`。 | ✅ 已实现 |
| 顶层包含 `version/window_key/window_type/window_id/T1/messages/metadata`。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:19-99` | `_create_empty_t_file()` 在 `checkpoint.py:105-134` 与文档主结构一致。 | ✅ 已实现 |
| `messages` 支持 `role/content/timestamp/meta/tool_calls/tool_call_id`。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:44-90` | `append_messages()` 在 `checkpoint.py:359-383` 会按需保留 `content`、`tool_calls`、`tool_call_id`、`timestamp`、`meta`。 | ✅ 已实现 |
| 构建 LLM contexts 时，T1 需变成 `user + assistant ACK` 两条消息。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:117-147` | `build_llm_contexts()` 在 `checkpoint.py:402-430` 与文档一致。 | ✅ 已实现 |

### 2. 生命周期与读写时机

| 文档设计 | 代码验证 | 结论 |
|---|---|---|
| 首次遇到窗口时创建空 T 文件。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:151-162` | `load()` 在 `checkpoint.py:285-289` 不存在即创建。 | ✅ 已实现 |
| 每次 `on_llm_request` 从 `req.contexts` 增量提取新消息，追加后保存。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:164-176` | `main.py:2684-2690` 会读取 T、调用 `_extract_new_messages()`、再 `append_messages()`。 | ✅ 已实现 |
| 增量检测要结合长度和内容对比，并跳过 T1 已压缩消息。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:172-176` | `_extract_new_messages()` 在 `main.py:3015-3032` 只按 `compressed_count + existing_count` 做切片，没有内容级校验。 | ⚠️ 部分实现 |
| 超阈值时压缩前 N%，更新 T1 与 messages。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:177-185` | `compress_if_needed()` 在 `checkpoint.py:535-715` 已实现完整流程。 | ✅ 已实现 |
| LLM 回复后回写 T 文件。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:186-191` | 代码没有独立回复钩子；当前实现依赖下一次 `on_llm_request` 重新读取 A 系统上下文并增量补入。`main.py:2686-2690` 可间接覆盖上一轮 assistant/tool 消息，但不是“回复后立即回写”。 | ⚠️ 部分实现 |
| 每次写入都要原子保存。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:192-196` | `save()` 在 `checkpoint.py:317-335` 先写临时文件，再重命名。 | ✅ 已实现 |
| 文件损坏回退空 T。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:215-218` | `load()` 在 `checkpoint.py:303-307` 解析失败时重建空 T。 | ✅ 已实现 |
| 同窗口使用 `asyncio.Lock` 保证互斥。`QQBotPlan/Plan_2/Plan_2_CP_T_file.md:220-222` | `checkpoint.py:257-267` 定义 per-window 锁；写路径在 `append_messages()` / `compress_if_needed()` 使用。 | ✅ 已实现 |

## 三、Plan_2_CP_compression.md 参数表与压缩策略核对

### 1. 参数名与默认值

| 文档设计 | 代码验证 | 结论 |
|---|---|---|
| 参数名应为 `checkpoint_token_limit`。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:19-26` | `main.py:160`、`2697` 读取的是 `checkpoint_token_limit`；但 `config.json:9`、`models.py:160/178/199-200`、`app.js:1363/1376`、`index.html:427` 使用的都是 `checkpoint_limit`。主链路与面板链路命名不一致。 | ❌ 未实现 |
| 默认值应为 `50000 / 10 / 0.7 / 300 / 0.20 / 0.40`。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:19-26` | 实际配置文件是 `10000 / 15 / 0.6 / 300 / 0.2 / 0.4`（`config.json:9,21-25`）；前端 HTML 初始值仍写 `50000 / 10 / 0.7 / 300 / 0.20 / 0.40`（`index.html:427-454`），加载后又会被接口值覆盖（`app.js:1363-1368`）。 | ⚠️ 文档默认值已过期 |

### 2. 三重守卫与压缩逻辑

| 文档设计 | 代码验证 | 结论 |
|---|---|---|
| 三重守卫：token 超限、消息数足够、冷却期已过。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:5-15` | `checkpoint.py:541-567` 完整实现三重守卫。 | ✅ 已实现 |
| 压缩前 `compress_front_ratio` 比例，同时至少保留 `keep_recent`。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:28-43` | `checkpoint.py:576-581` 与文档一致。 | ✅ 已实现 |
| Prompt 使用明确字数/token 目标。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:64-113` | `build_compress_prompt()` 在 `checkpoint.py:141-194` 一致实现。 | ✅ 已实现 |
| 压缩后做 ratio 校验并记录 warning/info。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:115-170` | `checkpoint.py:616-635` 一致实现。 | ✅ 已实现 |
| 压缩后更新 T1、messages、metadata。`QQBotPlan/Plan_2/Plan_2_CP_compression.md:172-194` | `checkpoint.py:653-689` 一致实现。 | ✅ 已实现 |

## 四、Plan_2_CP_integration.md 修改清单核对

| 章节 | 文档设计 | 代码验证 | 结论 |
|---|---|---|---|
| 1 | `checkpoint.py` 重写，新增 `TFileManager`，旧逻辑退场。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:16-83` | `checkpoint.py:248-715` 已新增 `TFileManager`；`CheckpointManager` 仅保留兼容层（`checkpoint.py:765-834`）。但 `main.py:867-882`、`1170-1185` 仍在调用一个已经不存在的 `check_and_compress()`。 | ⚠️ 部分实现 |
| 2 | `on_llm_request` 中读取 T、追加新消息、压缩、替换 `req.contexts`。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:87-159` | `main.py:2668-2712` 与文档基本一致。 | ✅ 已实现 |
| 3 | 同步触发删除旧 `check_and_compress`；`_build_judgment_prompt` 改为从 T 取上下文。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:163-190` | 未完成。`main.py:867-882` 与 `1170-1185` 仍调用旧接口；`main.py:738-748`、`903-909`、`1044-1055` 仍使用 `_get_recent_context()`；而 `_get_recent_context()` 继续读 `messages.db`（`main.py:2274-2338`）。 | ❌ 未实现 |
| 4 | “LLM 回复后回写 T 文件”在延迟持久化逻辑中补写。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:193-214` | 延迟持久化逻辑仍只写 `messages.db`（`main.py:2546-2597`、`2344-2398`），没有在该处调用 `append_messages()` 写 T。功能只能通过下一轮 `on_llm_request` 间接补齐。 | ⚠️ 部分实现 |
| 5 | Knowledge 逻辑不变，但 FlashLite 上下文来源改为 T。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:217-235` | Knowledge 更新逻辑还在（`main.py:759-769`、`1066-1074`），但其输入上下文仍来自 `_get_recent_context()` 的 `messages.db`。 | ⚠️ 部分实现 |
| 6 | `agent.py` 删除 `_get_checkpoint_summary`，`build_contents` 暂保留不用。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:239-252` | `build_contents()` 确实未被任何地方调用（仅定义于 `agent.py:98`）；但 `_get_checkpoint_summary()` 没删除，而是保留废弃桩返回 `None`（`agent.py:207-213`）。 | ⚠️ 部分实现 |
| 7 | 面板新增压缩参数，`main.py` 初始化 `TFileManager`。`QQBotPlan/Plan_2/Plan_2_CP_integration.md:255-274` | `main.py:158-164` 已初始化 `TFileManager`；前后端也已新增 6 个字段（`models.py:157-165,175-210`，`index.html:422-460`，`app.js:1354-1383`）。但首个参数名称仍是 `checkpoint_limit`，与主链路读取名不一致。 | ⚠️ 部分实现 |

## 五、指定问题回答

### 1. “LLM 回复后回写 T 文件”是否已实现？

**结论：⚠️ 间接实现，未按集成文档指定方式实现。**

- 文档要求是在延迟持久化逻辑里顺手调用 `append_messages()` 写入 T。见 `QQBotPlan/Plan_2/Plan_2_CP_integration.md:193-214`。
- 实际代码在 `main.py:2546-2597` 只调用 `_persist_bot_reply()`，而 `_persist_bot_reply()` 只写 `messages.db`（`main.py:2344-2398`）。
- 但 `main.py:2686-2690` 会在下一次 `on_llm_request` 从 A 系统的 `req.contexts` 增量提取消息再写入 T，因此上一轮 assistant/tool 消息大概率会在“下一轮请求开始时”补录到 T。
- 所以功能效果不是完全没有，但不是“回复完成即回写”，也没有保证与工具调用链严格同步。

### 2. `_build_judgment_prompt` 是否已改为从 T 文件读取？

**结论：❌ 没有。**

- `_build_judgment_prompt()` 本身只是消费传入的 `context` 参数，定义位于 `main.py:1820-1874`。
- 传给它的 `context` 仍来自 `_get_recent_context()`，调用点在 `main.py:738-748`、`903-909`、`1048-1055`。
- `_get_recent_context()` 明确从 `QQ_data/messages.db` 的 `qq_messages` 表读取数据，见 `main.py:2274-2338`。
- 文档要求的 `self._t_file_mgr.build_flashlite_context(t_file, max_tokens=8000)` 路径并未落地。

## 六、反向检查：代码里存在但文档未完整覆盖的 CHECKPOINT 相关逻辑

| 代码逻辑 | 位置 | 文档覆盖情况 |
|---|---|---|
| 旧同步/私聊触发链路仍调用 `self._checkpoint_mgr.check_and_compress(...)`，但 `CheckpointManager` 已无此方法。 | `main.py:867-882`、`1170-1185`；对照 `checkpoint.py:765-834` 与全局搜索 `def check_and_compress` 无结果 | 文档只写“应删除旧调用”，没有说明当前仓库仍残留失效调用。属于代码残留。 |
| `on_llm_request` 仍额外注入 `flashlite_context_summary` / recent messages / Knowledge / 用户卡片。 | `main.py:2599-2666` | Plan_2_CP 文档主讲 T 文件替换，没有完整描述这条并行注入链。 |
| 延迟持久化会把 bot 回复和工具结果摘要写回 `messages.db`，供 `_get_recent_context()` 使用。 | `main.py:2546-2597`、`2344-2398` | Plan_2_CP 文档未把这条“B 系统补写链路”纳入 CHECKPOINT 重构描述。 |
| 面板和配置接口公开的参数名是 `checkpoint_limit`，不是文档承诺的 `checkpoint_token_limit`。 | `models.py:160,178,199-200`、`app.js:1363,1376`、`index.html:427` | 文档没有说明这个别名，也没有解释与主链路读取名不同。 |
| `agent.py` 仍保留 `_get_checkpoint_summary()` 废弃桩和未使用的 `build_contents()`。 | `agent.py:98-137`、`207-213` | 文档提到未来可删，但没有同步说明当前实现仍保留桩方法。 |

## 七、🔴 严重问题（必须修复）

### 问题 1：CHECKPOINT 参数命名链路断裂，面板修改不会影响主压缩逻辑
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:160,2697`，`AstrBot/data/plugins/astrbot_plugin_flashlite/config.json:9`，`BossLady_Console/backend/routers/models.py:160,178,199-200`，`BossLady_Console/frontend/app.js:1363,1376`
- **描述**：主链路读取 `checkpoint_token_limit`，但配置文件、后端接口、前端面板全部读写 `checkpoint_limit`。结果是主压缩逻辑实际吃不到面板保存值，只会回退到 50000 默认值。
- **修复建议**：统一全链路字段名。最稳妥做法是主链路改为优先读 `checkpoint_limit`，并兼容旧名：

```python
token_limit = self._cfg("checkpoint_limit", self._cfg("checkpoint_token_limit", 50000))
```

### 问题 2：同步/私聊触发仍调用已不存在的旧 `check_and_compress()`
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:867-882`、`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:1170-1185`
- **描述**：`CheckpointManager` 在 `checkpoint.py:765-834` 中已经只剩兼容层，没有 `check_and_compress()`；这两处调用会在运行时抛异常，然后被 `except` 吞掉，造成“表面可运行，实际每次都走错误分支”。
- **修复建议**：删除这两段旧调用，避免误导；如果确实要保留主动压缩，应改用 `TFileManager.compress_if_needed()`，并与 T 上下文链路统一。

### 问题 3：FlashLite 判断链路没有切换到 T 文件，核心设计承诺未兑现
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:738-748`、`903-909`、`1044-1055`、`2274-2338`
- **描述**：文档要求 FlashLite 用 T 进行触发判断和 Knowledge 更新，但实际仍从 `messages.db` 读取 recent_context。这样会丢失 T1 压缩历史，也让“三系统分立”的核心收益没有落到 FlashLite。
- **修复建议**：在同步/异步/私聊三条判断入口统一改成：

```python
t_file = await self._t_file_mgr.load(window_key)
recent_context = self._t_file_mgr.build_flashlite_context(t_file, max_tokens=8000)
```

## 八、🟡 建议改进

### 建议 1：把“LLM 回复后回写 T 文件”从间接同步改为显式回写
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:2546-2597`
- **描述**：当前只能等下一轮 `on_llm_request` 再把上一轮 assistant/tool 消息补入 T，时序不稳定。
- **修复建议**：在延迟持久化逻辑或专门的回复后钩子中直接调用 `append_messages()`；至少把最近 assistant + tool 消息链按原样写入 T。

### 建议 2：增量检测从“纯长度切片”升级到“长度 + 内容校验”
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/main.py:3015-3032`
- **描述**：当前假设 A 系统历史与 T 文件严格同序，遇到截断、插入、去重差异时容易漂移。
- **修复建议**：至少比对尾部若干条消息的 `role/content/tool_call_id`，发现不一致时做重扫或回退。

### 建议 3：文档与默认值需要同步
- **位置**：`QQBotPlan/Plan_2/Plan_2_CP_compression.md:19-26`，`AstrBot/data/plugins/astrbot_plugin_flashlite/config.json:9,21-25`
- **描述**：文档默认值仍写 50000/10/0.7，但仓库实际配置是 10000/15/0.6。
- **修复建议**：要么更新文档默认值，要么回调配置，避免面板、配置和设计说明三份数字同时存在。

### 建议 4：清理 `agent.py` 的废弃桩和过期注释
- **位置**：`AstrBot/data/plugins/astrbot_plugin_flashlite/agent.py:1-9`、`207-213`
- **描述**：文件头注释仍描述“CHECKPOINT 压缩摘要 + 最近~10条消息”，与 T 文件接管后的现状不完全一致； `_get_checkpoint_summary()` 也还在保留。
- **修复建议**：如果没有外部依赖，直接删除桩方法并更新顶部说明；否则至少在注释中明确“仅兼容保留，不参与主链路”。

## 九、🟢 微调建议

### 建议 1：面板文案中补充“主链路当前实际读取的参数名”
- **位置**：`BossLady_Console/frontend/index.html:422-460`
- **描述**：当前 UI 展示的是完整参数集，但没有暴露命名兼容状态，排障时不透明。
- **修复建议**：待字段统一后，在后端接口返回值中附带规范字段名，前端按规范名渲染。

### 建议 2：补一条集成测试覆盖“面板保存 -> main.py 读取 -> 实际压缩触发”
- **位置**：建议新增到 FlashLite 集成测试
- **描述**：这次最关键的错配是跨文件参数流转断裂，单元测试覆盖不到。
- **修复建议**：构造一次配置保存，再断言 `TFileManager.compress_if_needed()` 收到的 token limit 与面板保存值一致。

## 十、✅ 做得好的地方

- `checkpoint.py` 的 T 文件主实现比较完整，尤其是 `build_llm_contexts()`、`compress_if_needed()`、原子保存和损坏恢复这几块，与设计文档贴合度很高。
- `test_checkpoint_v2.py` 在 UTF-8 控制台环境下已跑通，说明 T 文件的核心单元能力当前是可工作的。
- 面板已经把 6 个压缩参数全部铺出来了，后续只要修正命名链路，运维可观测性会比较好。

## 十一、验证记录

- 已执行：`$env:PYTHONIOENCODING='utf-8'; python AstrBot/data/plugins/astrbot_plugin_flashlite/test_checkpoint_v2.py`
- 结果：**通过**
- 备注：直接用默认 Windows 控制台编码运行会因测试输出 emoji 触发 `UnicodeEncodeError`，需显式设置 UTF-8 输出编码。

## 十二、优先级排序的改进建议

1. 先统一 `checkpoint_limit` / `checkpoint_token_limit` 字段名，否则面板配置对主链路无效。
2. 删除 `main.py` 中两处失效的旧 `check_and_compress()` 调用，避免运行时反复报错。
3. 把 FlashLite 判断上下文正式切到 `TFileManager.build_flashlite_context()`，兑现“三系统分立”的核心承诺。
4. 增加显式的“回复后回写 T 文件”逻辑，尤其保证 assistant/tool/tool_result 链条不再依赖下一轮请求补录。
5. 同步更新 Plan_2_CP 文档默认值与当前实现，清理 `agent.py` 中的废弃说明与兼容桩。
