# Report_2_13: T 文件机制体系 5 项审查报告

**审查时间**: 2026-04-10
**审查方式**: 逐条对照源码实际实现，非推测

---

## Q1: 工具模型是否接收 T 文件上下文？是否可配置？

### 源码事实

工具模型通过 `_call_tool_model()` 调用（main.py:1522），其 messages 初始化为：

```python
messages = [{"role": "user", "parts": [{"text": prompt}]}]  # L1530
```

这是一个**完全独立的 mini agent**——它自带 system prompt（`_build_tool_model_system()`），自带工具声明（sandbox 工具），contents 只有调用时传入的 prompt。**不包含任何窗口 T 文件上下文**。

### 结论

- ❌ **当前不支持**将 T 文件上下文传给工具模型
- ❌ **没有可配置参数**控制是否传入
- ✅ 默认行为符合你的要求——工具模型子代理**没有对话上下文**，只执行具体 task

> 如果未来想让工具模型"知道当前对话说了什么"，需要新增一个配置参数（如 `tool_model_inject_context: bool`），并在 `_call_tool_model` 中注入 `build_flashlite_context()` 作为上下文前缀。当前不需要。

---

## Q2: 主模型和 FlashLite 的上下文来源 & CHECKPOINT 窗口隔离

### 源码事实

**主模型上下文来源**（main.py:2686）：
```python
req.contexts = self._t_file_mgr.build_llm_contexts(t_file)
```
→ 来自**当前窗口的 T 文件**。

**FlashLite 判断上下文来源**（main.py:741/888/1031/1966/2027，共 5 处）：
```python
recent_context = self._t_file_mgr.build_flashlite_context(_t_file)
```
→ 来自**当前窗口的 T 文件**，通过 `window_key` 加载。

**CHECKPOINT 触发**（main.py:2668）：
```python
t_file, compress_result = await self._t_file_mgr.compress_if_needed(
    window_key=window_key, t_file=t_file, ...
)
```
→ 按 `window_key` 触发，每个窗口独立。

**窗口隔离机制**：
- `window_key` 格式：`GroupMessage:{group_id}` 或 `FriendMessage:{user_id}`（main.py:2651/2654）
- 每个窗口对应独立的 JSON 文件：`QQ_data/checkpoints/{window_key}.json`（checkpoint.py:265）
- 每个窗口有独立的 `asyncio.Lock`（checkpoint.py:259）
- 新增的 `_compressing` 互斥标记也是按 window_key 记录（checkpoint.py:570）

### 结论

- ✅ 主模型和 FlashLite **都使用当前窗口 T 文件**
- ✅ CHECKPOINT **按窗口独立触发**
- ✅ 窗口 A 的 CHECKPOINT **不可能影响窗口 B**——所有操作（load/append/compress/save）都通过 `window_key` 隔离
- ✅ 同一窗口不会同时触发两次压缩（`_compressing` 互斥标记保护）

---

## Q3: T 文件机制是否影响 FlashLite 之前的 Knowledge 机制和对话管理

### 源码事实

**Knowledge 系统**（knowledge.py）：
- 完全独立的模块，使用自己的 `knowledge.db`  
- 操作自己的卡片/事实系统，有自己的冷热归档（`card_cold_days`）
- 与 T 文件 **零交叉引用**（grep 确认 knowledge.py 中无 `t_file`/`checkpoint` 引用，checkpoint.py 中无 `knowledge` 引用）

**对话管理（AstrBot A 系统）**：
- AstrBot 自己的 messages.db 管理不受影响
- T 文件从 `req.contexts` 增量提取新消息（L2662），这是**只读操作**——不修改 AstrBot 的 messages.db
- `req.contexts` 替换（L2686）只影响**当次请求发给主模型的内容**，不影响 AstrBot 内部存储

**FlashLite 判断逻辑**：
- 之前从 `_get_recent_context()`（messages.db）取上下文
- 现在从 `build_flashlite_context(T文件)` 取上下文
- **更好**：T 文件包含压缩摘要 + 原文，比 messages.db 的有限窗口信息更完整

### 结论

- ✅ Knowledge 机制 **完全不受影响**
- ✅ AstrBot 对话管理（A 系统） **完全不受影响**——T 文件是只读提取，不写回 messages.db
- ✅ FlashLite 判断 **变得更好了**——上下文从 T 文件来，包含历史摘要

---

## Q4: 图中的内存管理设置只作用于 req.contexts / messages.db，不影响 T 文件

### 源码事实

**"消息持久化策略"**（models.py:235-280）：
- 配置字段：`hot_days`、`cold_days`、`archive_days`、`enable_auto_cleanup`
- 存储在 `cmd_config.json` 的 `storage_policy` 中
- **main.py 中未引用**这些字段（grep 确认 `storage_policy`/`hot_days`/`cold_days`/`auto_cleanup` 在 main.py 中 0 结果）
- 当前是**纯面板配置项**，后端有读写逻辑但实际执行清理的代码**尚未在 main.py 中实装**

**"图片缓存管理"**（面板截图中）：
- 管理 `QQ_data/images/` 目录
- 与 T 文件 **完全无关**

**AstrBot 框架自身的上下文管理**：
- AstrBot 对 messages.db 有自己的 token 管理
- 但 T 文件独立存储在 `QQ_data/checkpoints/`，AstrBot 框架**不知道 T 文件的存在**，不会操作它

**T 文件膨胀分析**：
- CHECKPOINT 压缩保证 T 文件不会无限膨胀
- 每次压缩：前 70% 消息 → 摘要，保留后 30% 原文 + 中间到达消息
- 压缩不断递归式稀释旧内容，符合遗忘规律
- **不需要额外的清理机制**

### 结论

- ✅ 面板中的所有内存管理设置（消息持久化、图片缓存、KV Cache）**都不操作 T 文件**
- ✅ T 文件因 CHECKPOINT 压缩存在，**不会膨胀**，不需要额外清理
- ✅ 互不影响

---

## Q5: T 文件中消息类型完整性与嵌入顺序

### 源码事实

**消息来源**：`_extract_new_messages`（main.py:3018-3035）按计数从 `req.contexts` 增量提取。

`req.contexts` 中包含的消息类型（由 AstrBot 框架按时序排列）：
1. `role: "user"` — 用户消息
2. `role: "assistant"` — 模型回复（可能含 `tool_calls`）
3. `role: "assistant" + tool_calls` — 模型发出的工具调用请求
4. `role: "tool" + tool_call_id` — 工具返回结果

**append_messages 的保存能力**（checkpoint.py:341-385）：

| 字段 | 处理 | 代码行 |
|------|------|--------|
| `role` | ✅ 保存（user/assistant/tool） | L358 |
| `content` | ✅ 保存（str 或 None） | L362-363 |
| `tool_calls` | ✅ 保存 | L366-367 |
| `tool_call_id` | ✅ 保存 | L370-371 |
| `timestamp` | ✅ 保存 | L374 |
| `meta` | ✅ 保存（含 sender_name/sender_qq） | L377-378 |

**build_llm_contexts 的输出能力**（checkpoint.py:391-426）：

| 字段 | 输出 | 代码行 |
|------|------|--------|
| `role` | ✅ 输出 | L413 |
| `content` | ✅ 输出（None 时不设） | L415-416 |
| `tool_calls` | ✅ 输出 | L418-419 |
| `tool_call_id` | ✅ 输出 | L421-422 |

**顺序保证**：
- `_extract_new_messages` 使用 `contexts[processed_count:]` 切片，**保持 req.contexts 中的原始时序顺序**
- `append_messages` 按 `for msg in new_messages` 顺序 `append` 到 `t_file["messages"]` 末尾，**不重排**
- `build_llm_contexts` 按 `for msg in t_file["messages"]` 顺序输出，**保持存储顺序**

### 一个完整的工具调用序列在 T 文件中的样子

```json
[
  {"role": "user", "content": "帮我查一下天气", "timestamp": "..."},
  {"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "web_search", "arguments": "..."}}], "timestamp": "..."},
  {"role": "tool", "tool_call_id": "call_xxx", "content": "北京今天晴 25°C", "timestamp": "..."},
  {"role": "assistant", "content": "北京今天天气晴朗，25°C~", "timestamp": "..."},
  {"role": "user", "content": "谢谢", "timestamp": "..."}
]
```

### 结论

- ✅ 用户消息（user）—— **正确记载**
- ✅ 模型回复（assistant）—— **正确记载**（含纯文本和带 tool_calls 的）
- ✅ 工具调用（assistant + tool_calls）—— **正确记载**
- ✅ 工具反馈（tool + tool_call_id）—— **正确记载**
- ✅ 嵌入顺序 —— **由 req.contexts 原始时序保证，append 不重排，build 不重排**
