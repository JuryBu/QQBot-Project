# Report_2_1: 代码链路审计报告（6 问）

> 审计时间: 2026-04-06 23:35
> 审计范围: `main.py` (3733行) + `knowledge.py` (588行)
> 方法: grep 全文搜索 + 代码段逐行阅读

---

## 1. KVCache 目前使用情况

**结论: ❌ 代码中不存在任何 KVCache 实现**

使用 `kv_cache / KVCache / ensure_cache / cached_content / context_caching` 全文搜索，**结果为 0 匹配**。

当前上下文管理机制是:
- **CHECKPOINT 压缩**: `_agent_builder._get_checkpoint_summary(window_key)` 从数据库读取历史压缩摘要（L1763）
- **Knowledge 缓存**: `self._knowledge.get_formatted()` 格式化每个窗口的 summary+mood+active_users 快照（L1681）
- **FlashLite 消息上下文**: `_get_recent_context(group_id)` 从 sqlite 数据库读取最近 N 条消息（L1338）

这些都是**应用层面的缓存**，不是 Gemini API 的 Context Caching（KVCache/cachedContent）。

**影响**: 每次主模型请求都是全量发送 system prompt + inject_parts，无法利用 Gemini 的 Context Caching 来节省 token。如果后续要优化、需要在 Gemini REST API 调用前使用 `cachedContent` API 创建缓存，但这需要改 AstrBot 框架层面的调用逻辑，目前插件层面做不到。

---

## 2. wait/grep 工具定义、用法、哪个模型能用

**结论: ❌ 不存在 wait 和 grep 工具**

### 2.1 搜索结果
- `Sandbox/base_tools/` 目录下**没有** `wait.tool.json` 和 `grep.tool.json`
- `main.py` 中**没有** `def tool_wait` 和 `def tool_grep` 方法定义
- 完整的 `base_tools/` 目录文件列表：
  ```
  browser_agent.tool.json    generate_image.tool.json   knowledge_update.tool.json
  media_summary.tool.json    memory_query.tool.json     memory_read.tool.json
  memory_update.tool.json    memory_write.tool.json     modify_file.tool.json
  QQ_data_original.tool.json run_custom_tool.tool.json  sandbox_exec.tool.json
  save_data.tool.json        search.tool.json           system_report.tool.json
  task_set.tool.json         upload_data.tool.json      view_file.tool.json
  web_fetch.tool.json
  ```

### 2.2 如果需要类似功能
- **wait（等待）**: 目前没有让模型主动等待/轮询的工具。Task 系统的 `wake_condition=notify_main` 是最接近的替代——任务完成后自动唤醒主模型
- **grep（文件内搜索）**: `search.tool.json` 的 `scope=files` 模式可以搜索 Sandbox 文件内容，功能上等同于 grep

### 2.3 哪个模型能用这些工具
- **主模型**: 通过 AstrBot 框架的 function calling 使用 `base_tools/*.tool.json` 定义的所有工具
- **工具模型**: 通过 `agent_xxx` 前缀调用，但只有在 `main.py` 中有对应 `tool_xxx` 方法的工具才会被动态加载（L1004-1033 排除了 `task_set` 和 `knowledge_update`）

---

## 3. 模型现在要如何操作草稿

**结论: ✅ 工具模型有 agent_draft；主模型通过子代理间接使用 drafts**

### 3.1 工具模型（子代理）的 drafts 操作

**`agent_draft` 工具定义** (L986-1000 硬编码 functionDeclarations):
```python
{
    "name": "agent_draft",
    "description": "读写你的专属草稿纸。写入: 传 filename 和 content; 读取: 只传 filename",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "filename": {"type": "STRING", "description": "草稿文件名"},
            "content": {"type": "STRING", "description": "草稿内容(不传=读取)"}
        },
        "required": ["filename"]
    }
}
```

**执行逻辑** (`_execute_agent_tool` L1016+):
```python
elif tool_name == "agent_draft":
    draft_path = f"workspace/drafts/{args.get('filename', 'draft.md')}"
    if "content" in args:
        await self._sandbox.modify_file(draft_path, args["content"], mode="write")
        return f"草稿已保存: {draft_path}"
    else:
        return await self._sandbox.view_file(draft_path)
```

### 3.2 主模型的 drafts 操作

主模型**没有直接的 agent_draft 工具**。它操作草稿的方式：
- 直接: `modify_file(path="workspace/drafts/xxx.md", content="...")` — 虽然可行但提示词中没有特别指引
- 间接: 创建 Task 让工具模型使用 agent_draft

### 3.3 提示词中的说明
- **工具模型 prompt** (L848-851): 明确说明了 drafts 用法 + 建议
- **主模型 prompt** (L1886-1888): Section 8 Sandbox 中提到草稿纸机制，但描述较泛
- **问题**: 主模型提示词中没有具体说"你可以直接 modify_file 来写草稿"，也没有 agent_draft，所以主模型很可能不知道自己有这个能力

---

## 4. 分轮续发机制目前是如何操作的

**结论: ❌ 当前不存在分轮续发机制**

### 4.1 搜索结果
使用 `分轮 / 续发 / 分段发 / 分多条 / multi_reply / split_reply / continue_reply` 全文搜索，**结果为 0 匹配**。

### 4.2 当前的"多段"策略
主模型提示词 Section 5 (L1780) 中写道:
```
"每次回复不超过 3 个自然段，复杂内容分多轮说"
```
但这只是一句提示词描述，**没有对应的代码实现**。如果主模型一次回复很长，AstrBot 框架会直接发送整段文字到 QQ，不会自动拆分。

### 4.3 实际行为
- 主模型回复 → AstrBot 框架 → 直接发送整条消息
- 如果回复太长，QQ 协议层（NapCat/go-cqhttp）可能会截断或报错
- 没有任何代码做"超过 N 字就拆成多条消息"的逻辑

### 4.4 如果需要实现
需要在 AstrBot 的消息发送链路（`aiocqhttp_platform_adapter.py`）中加入消息拆分逻辑，或者在 `on_llm_request` 的回复后处理中拆分。但这需要改框架层面的代码而不仅是插件层面。

---

## 5. 目前 Memory 机制情况

**结论: ✅ Memory 系统完整，跨 3 个模型协作**

### 5.1 架构
```
FlashLite → Memory 迷你索引(序号) → MEMORY_HINT → 精确召回 → set_extra("memory_recall") → 主模型注入
主模型 → memory_write/search(scope=memory) → 直接读写
工具模型 → agent_memory_write/agent_memory_query → 通过通用路由调用
```

### 5.2 FlashLite 侧（被动召回）
1. `_build_memory_mini_index()` (L726-763): 从 MemoryStore 获取所有条目，构建编号索引
2. FlashLite 输出 `MEMORY_HINT=1,3,7` 即可精确指定召回哪些条目
3. 解析后通过 `_memory.read_entry(entry_id)` 获取完整内容
4. 结果通过 `event.set_extra("memory_recall", hint_text)` 传递给主模型

### 5.3 主模型侧（主动读写）
- **读取**: `search(scope='memory', query='xxx')` — 搜索记忆
- **写入**: `memory_write(content='...', tags=['标签'], ...)` — 写入记忆
- 注入点: `on_llm_request` L1702-1709 取出 memory_recall 注入

### 5.4 工具模型侧
- 通过 `agent_memory_write / agent_memory_query` 调用（通用路由到 `tool_memory_write / tool_memory_query`）
- 和主模型共享同一个 MemoryStore 实例

### 5.5 存储格式
`memory.py` MemoryStore 基于 JSON 文件持久化:
```json
{
  "entries": [
    {"id": "...", "title": "...", "content": "...", "tags": [...], "pinned": false, "created_at": "..."}
  ]
}
```

---

## 6. 上下文和 Knowledge 中是否包含用户QQ号和昵称

**结论: ⚠️ 部分包含，有遗漏**

### 6.1 消息上下文（`_get_recent_context`）— ✅ 包含 QQ号+昵称
数据库查询 (L1367-1382):
```sql
SELECT sender_name, content_text, created_at, sender_id
FROM message_log
WHERE window_id = ? AND window_type = 'group' AND sender_id != 'bot'
```
格式化 (L1409-1415):
```python
if sender_id == "bot":
    lines.append(f"[{time_str}] 老板娘 [BOT]: {text}")
elif sender_id:
    lines.append(f"[{time_str}] {name}({sender_id}): {text}")  # sender_id 即 QQ号
```
✅ 用户消息: `[23:28] 张三(12345678): 你好` — 包含昵称+QQ号
✅ Bot 消息: `[23:28] 老板娘 [BOT]: 你好呀` — 标记身份

### 6.2 Knowledge 快照 — ⚠️ active_users 仅含昵称
`knowledge.py` 更新代码 (L121):
```python
"active_users": active_users or existing.get("active_users", [])
```
输出格式 (L161):
```python
users = ", ".join(info.get("active_users", [])[:5])  # 仅昵称
```
⚠️ **问题**: active_users 列表只存昵称（`["张三", "李四"]`），**不含 QQ号**。
这意味着 Knowledge 快照中，主模型看到的是 `活跃: 张三, 李四`，无法确认是哪个 QQ 号的用户。

### 6.3 用户卡片 — ✅ 包含 QQ号+昵称
`knowledge.py` 卡片格式 (L458):
```python
header = f"### {nick} (QQ:{qq_id})"
```
✅ 卡片头部明确显示 `张三 (QQ:12345678)`

### 6.4 FlashLite 消息输入 — ✅ 包含 QQ号+昵称
FlashLite 接收的上下文就是 `_get_recent_context` 的输出，格式含 `昵称(QQ号)`

### 6.5 CONTEXT_SUMMARY — 取决于 FlashLite 输出
提示词要求 FlashLite 输出 `CONTEXT_SUMMARY=<包含关键发言者(QQ号)+核心内容>`，但这依赖 FlashLite 的实际表现。提示词中明确要求了 QQ号。

### 6.6 综合结论
| 数据源 | 昵称 | QQ号 | Bot标记 |
|--------|------|------|---------|
| 消息上下文 | ✅ | ✅ | ✅ [BOT] |
| Knowledge active_users | ✅ | ❌ | N/A |
| 用户卡片 | ✅ | ✅ | N/A |
| Memory 记忆 | 视内容 | 视内容 | N/A |
| CONTEXT_SUMMARY | 视FlashLite | 视FlashLite | N/A |

**待修复**: Knowledge 的 `active_users` 应改为 `昵称(QQ号)` 格式，确保全链路 QQ号 可追溯。
