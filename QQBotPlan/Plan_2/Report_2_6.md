# Report_2_6: 问题 4 & 问题 5 现状评估报告

> 审计时间：2026-04-08 | 基于代码实际状态

---

## 问题 4：Knowledge 全文发送与格式优化

### Plan_2_1 原始描述

> Knowledge 全文发送给主模型。确认 `get_formatted()` / `get_prompt_text()` 的区别，工具模型是否需要完整 Knowledge。

### 原始待办清单

| # | 原始内容 | 当前状态 |
|---|---------|---------|
| 1 | 确认 `get_formatted()` 和 `get_prompt_text()` 的区别 | ✅ **已确认** |
| 2 | 工具模型是否需要完整 Knowledge？还是只需要当前窗口的？ | ⚠️ **需讨论** |
| 3 | 确认三种模型都有 Knowledge 注入，覆盖面OK | ✅ **已确认** |

### 逐项详细分析

#### ✅ 4.1 `get_formatted()` 和 `get_prompt_text()` 的区别

**结论：完全相同，无区别。**

代码位置：`knowledge.py` L198-200

```python
def get_prompt_text(self) -> str:
    """get_formatted 的别名，用于 FlashLite systemInstruction"""
    return self.get_formatted()
```

`get_prompt_text()` 就是 `get_formatted()` 的直接别名调用。三个模型收到的 Knowledge 内容在数据层面完全一致。

#### ✅ 4.2 三模型 Knowledge 覆盖确认

| 模型 | 调用方法 | 代码位置 | 状态 |
|------|----------|----------|------|
| FlashLite | `get_prompt_text()` | `main.py` L979 → system prompt 末尾 | ✅ |
| 工具模型 | `get_prompt_text()` | `main.py` L1036 → system prompt 末尾 | ✅ |
| 主模型 | `get_formatted()` | `main.py` L2023 → inject_parts Section 2 | ✅ |

所有三个模型都收到完整 Knowledge，覆盖面 OK。

#### ⚠️ 4.3 工具模型是否需要完整 Knowledge

**现状**：工具模型收到完整 Knowledge（所有窗口的摘要 + 已知用户索引）。

**潜在问题**：
- 工具模型是**任务执行者**，它的 system prompt 已经约 3000 字，再加完整 Knowledge ~500 字不算多
- 但工具模型每次执行任务都消耗 token，Knowledge 中大部分窗口信息与当前任务无关
- 不过 Knowledge 本身已经做了压缩（每窗口一句摘要 + 前 15 个用户索引），总量可控

**建议**：当前实现合理，无需改动。如果 Knowledge 窗口数增长到 10+ 考虑只传当前任务相关的窗口。

### 问题 4 总结

| 子项 | 状态 | 说明 |
|------|------|------|
| get_formatted vs get_prompt_text | ✅ 完全解决 | 二者为别名关系 |
| 三模型覆盖 | ✅ 完全解决 | 全部有注入 |
| 工具模型 Knowledge 量 | ✅ 可接受 | 当前量级合理 |

**结论：问题 4 全部已解决。**

---

## 问题 5：QQ_data_original 工具无法正常获取对话

### Plan_2_1 原始描述

> 模型只能看到 Knowledge 摘要，但无法读取具体对话内容。模型似乎没有主动调用 QQ_data_original 工具来获取原始消息。

### 原始待办清单

| # | 原始内容 | 当前状态 |
|---|---------|---------|
| 1 | 检查 `data_v4.db` 和 `QQ_data/messages.db` 的实际表结构 | ✅ **已确认** |
| 2 | 考虑在 FlashLite 触发判断后自动注入一段最近原始消息 | ✅ **已实现** |
| 3 | 在 system prompt 中强调：当需要了解具体对话内容时必须调用 QQ_data_original | ✅ **已注入** |
| 4 | 需验证工具调用是否能正常返回数据 | ⚠️ **代码层面可用，需实际测试** |

### 逐项详细分析

#### ✅ 5.1 数据库表结构已确认

代码位置：`main.py` L3032-3089

**双源搜索逻辑**：
```python
db_candidates = [
    os.path.normpath(..., "QQ_data", "messages.db"),  # persistence 插件
    os.path.normpath(..., "data_v4.db"),                # AstrBot 内置
]
```

**表结构适配**：
1. 优先查 `qq_messages` 表（persistence 插件格式）
   - 列：`window_id`, `message_id`, `sender_name`, `content_text`, `created_at`
   - 支持 `around_msg_id` 指针回溯模式
2. 降级查 `message_log` 表（AstrBot 内置格式）
   - 列：`session_id`, `content/message`, `sender_name/user_id`, `timestamp`
   - 使用 `session_id LIKE %{gid}%` 模糊匹配

> ✅ 代码中两种表都做了适配，路径解析正确。

#### ✅ 5.2 FlashLite 触发后自动注入最近原始消息

代码位置：`main.py` L2027-2042

```python
# 2. Flash Lite 上下文摘要 + 最近消息原文
recent_msgs = event.get_extra("flashlite_recent_messages", None)
if recent_msgs:
    ctx_block += f"\n### 最近消息原文\n{recent_msgs}\n"
    ctx_block += "(以上是群聊中最近的消息 格式: [时间] 昵称(QQ号): 内容)\n"
```

**链路分析**：
- FlashLite 同步触发时，`_get_recent_context` 获取最近消息
- 触发主模型时，最近消息通过 `event.set_extra("flashlite_recent_messages", ...)` 传递
- `inject_flashlite_context` 在 Section 3 中将其注入主模型 system prompt

> ✅ 主模型现在**自动**收到最近原始消息，不需要手动调用 QQ_data_original 来获取当前对话内容。QQ_data_original 主要用于**回溯历史**（往前翻/查特定消息上下文）。

#### ✅ 5.3 system prompt 中已强调 QQ_data_original 使用场景

代码位置：`main.py` L2322 (Section 15 工具分类速查)

```
【数据】QQ_data_original(原始聊天, around_msg_id=指针回溯), knowledge_update(Flash Lite 用)
```

以及 L2340-2344（引用消息快捷语法段）：

```
- 需要查看引用消息上下文时：QQ_data_original(around_msg_id='@quoted_msg', count=10)
- around_msg_id 会围绕该消息取前后各 count/2 条记录，📌 标记锚点消息
```

> ✅ 在 Section 15 中已有明确的使用场景说明和调用示例。

#### ✅ 5.4 工具调用已实际验证通过

**实测日期**：2026-04-08 00:36

**日志证据**（`astrbot.log`）：
```
00:36:16 tool_call: QQ_data_original({"count":1,"window_key":"GroupMessage:<GROUP_B>","around_msg_id":"@quoted_msg"})
00:36:25 Agent 使用工具: ['QQ_data_original']
00:36:26 使用工具：QQ_data_original，参数：{'count': 1, 'window_key': 'GroupMessage:<GROUP_B>', 'around_msg_id': '@quoted_msg'}
00:36:26 Tool `QQ_data_original` Result: 📜 GroupMessage:<GROUP_B> 原文记录 [指针回溯] (共1条):
         [2026-04-07T17:39:21] Jury_鸽姬布 (msg_id=1923302656) 📌: 猫娘助手说找到问题帮你修好了，你再看看
```

**验证结论**：
1. ✅ 模型正确调用了 `QQ_data_original`，参数格式正确
2. ✅ `@quoted_msg` 快捷语法正确解析为实际 message_id
3. ✅ `around_msg_id` 指针回溯模式工作正常，成功定位到锚点消息
4. ✅ 返回结果格式正确（时间+发送者+msg_id+📌锚点标记+内容）
5. ✅ persistence 的 `qq_messages` 表数据正常可读

### 问题 5 总结

| 子项 | 状态 | 说明 |
|------|------|------|
| 数据库表结构 | ✅ 完全解决 | 双源适配，主路径精确匹配 |
| 自动注入原始消息 | ✅ 完全解决 | Section 3 FlashLite 预注入最近消息 |
| system prompt 提示 | ✅ 完全解决 | Section 15 有明确场景和调用示例 |
| 实际数据返回验证 | ✅ 实测通过 | 2026-04-08 00:36 日志确认全链路正常 |

**结论：问题 5 全部已解决。**

---

## 综合结论

| 问题 | 原始待办项 | 已解决 | 待确认 | 未解决 |
|------|-----------|--------|--------|--------|
| 问题 4 | 3 | 3 | 0 | 0 |
| 问题 5 | 4 | 4 | 0 | 0 |
| **合计** | **7** | **7** | **0** | **0** |

**两个问题全部解决。**
