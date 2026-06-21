# FlashLite 压缩模式 — 完整提示词审计

> 审计时间：2026-04-13 | 基于 checkpoint.py (934行) + main.py 最新代码逐行提取
> 模型：gemini-2.5-flash-preview（FlashLite 引擎，复用同一模型）
> 调用入口：`_call_flash_lite(prompt, max_output_tokens=dynamic_max_tokens, window_key=...)`
> 触发条件：`compress_if_needed()` 三重守卫全部通过时自动调用

---

## 一、与判断模式的关键区别

| 维度 | 判断模式 | 压缩模式 |
|------|---------|---------|
| systemInstruction | `_build_flash_lite_system()` | **完全相同**（共用入口） |
| contents[0].user | 动态前缀 + judgment_prompt | 动态前缀 + **compress_prompt** |
| maxOutputTokens | 固定 4096 | **动态计算**：`raw_max + delta` |
| 输出格式 | 标记行（TRIGGER_MAIN=...） | **结构化摘要文本** |

⚠️ systemInstruction 共用导致约 2000+ token 冗余（身份/判断规则对压缩无用），但 KVCache 命中率收益大于冗余成本。

---

## 二、systemInstruction（与判断模式完全相同）

参见 [Prompt_FlashLite_判断.md](./Prompt_FlashLite_判断.md) 第二节。

其中 system prompt 的「模式二」段落明确指示：
> 当你收到对话压缩任务时 不要使用上述标记行格式 而是直接输出结构化摘要文本。
> 压缩任务的详细格式和原则由任务提示本身指定 你只需按其要求输出即可。

---

## 三、contents[0].user 完整结构

### 3.1 动态前缀（与判断模式结构相同）

```
# 当前 Knowledge 快照
【动态：knowledge.get_prompt_text() 输出】

# 系统时间
2026-04-13 13:17:51

## Memory 索引（共 N 条）
【动态：_build_memory_mini_index() 输出】

---

```

### 3.2 压缩体 `build_compress_prompt()`

> 来源：`checkpoint.py:141-188`
> 参数由 `compress_if_needed()` 计算后传入

**完整 prompt 模板（实际拼接结果）：**

```
你是一个对话压缩引擎。将以下对话内容压缩为结构化摘要。

【条件段：当 has_previous_summary=True 时追加】
注意：输入内容开头有一段 [对话历史压缩摘要]，这是之前轮次的压缩结果。
请将其与后续新消息融合为一份统一的新摘要，旧摘要中的信息可以适当精简但不要完全丢弃。

## 输出要求
- 尽可能详细地保留所有有价值的信息
- 越详细越好，不要省略重要细节
- 系统会自动控制输出长度上限，你无需担心过长
- 不要刻意缩减内容，宁可多写也不要遗漏

## 压缩原则
1. 按话题/时间段分块，用简洁的标题标注每个话题段
2. 保留所有参与者名字和 QQ 号
3. 保留关键事实：人名、地名、数字、日期、结论、决定
4. 保留情感倾向和关系动态
5. 用「」包围重要原文引用
6. 去除：重复内容、纯表情、日常闲聊（你好/再见）、无信息量的应答
7. 如涉及图片/文件/工具调用，注明 [图片] [文件] [工具:名称→结果摘要]

## 输出格式
直接输出摘要，不要输出其他说明文字。格式参考：

【话题：xxx（时间段）】
参与者A 和 B 讨论了...关键信息:「原文引用」

## 原始内容（{msg_char_count} 字）
【动态：serialize_messages_for_compress() 输出的待压缩消息文本】
```

---

## 四、待压缩消息的序列化格式

> 来源：`checkpoint.py:191-235 serialize_messages_for_compress()`

每条消息格式：
```
[时间] 发送者: 内容
```

具体规则：
- **assistant** 角色 → `老板娘 [BOT]`
- **user** 角色 → `昵称(QQ号)`（从 msg.meta 提取）
- **tool** 角色 → `[工具结果 tool_call_id]: 内容`
- **tool_calls** → 追加 `[工具调用: 工具名]`
- 时间戳简化：`2026-04-13T12:30:00` → `12:30:00`

**实际示例：**
```
[12:30:00] 柚子(<ADMIN_QQ>): 老板娘帮我搜一下天气
[12:30:05] 老板娘 [BOT]: 好的~ [工具调用: search]
[12:30:10] tool: [工具结果 call_abc123]: 南京今天晴 28°C
[12:30:15] 老板娘 [BOT]: 南京今天晴天 28度呢 适合出门呀
[12:31:00] 小明(987654321): 谢谢老板娘
```

---

## 五、动态 maxOutputTokens 计算

> 来源：`checkpoint.py:658-665`

```python
raw_max = max(100, int(compress_tokens * target_max))   # 原文 token × 目标上限比例
delta = max(50, int(raw_max * 0.15))                    # 15% 余量
dynamic_max_tokens = raw_max + delta                     # 最终值
```

| 参数 | 默认值 | 来源 |
|------|-------|------|
| target_min | 0.20 | 配置 `checkpoint_target_min` |
| target_max | 0.40 | 配置 `checkpoint_target_max` |
| token_limit | 50000 | 配置 `checkpoint_limit` |
| keep_recent | 10 | 配置 `checkpoint_keep_recent` |
| compress_front_ratio | 0.7 | 前 70% 消息压缩，后 30% 保留原文 |
| cooldown_seconds | 300 | 连续压缩冷却期 5 分钟 |

**v2 设计理念**：不在 Prompt 中限制字数/token，由 API `maxOutputTokens` 硬保证上限。
Prompt 鼓励「尽可能详细」使模型自然趋向上限，最大化信息保留。

---

## 六、三重守卫触发条件

> 来源：`checkpoint.py:538-603`

压缩仅在以下三个条件**同时满足**时触发：
1. ① `total_tokens > token_limit`（默认 50000）
2. ② `raw_msg_count > keep_recent`（默认 10 条）
3. ③ 距上次压缩 > `cooldown_seconds`（默认 300 秒）

附加守卫：
- ④ 同一窗口并发互斥（`_compressing` set 检查）

---

## 七、压缩结果去向

1. 压缩文本作为 T1 摘要写入 T 文件：`t_file["T1"]["summary"]`
2. 被压缩的原始消息从 T 文件 messages 中移除
3. 下次 FlashLite 调用时，T1 摘要会作为 `[对话历史压缩摘要]` 出现在 LLM 上下文开头
4. 主模型通过 `build_llm_contexts()` 看到：T1 压缩摘要（user+assistant 两条） + 保留的近期消息
