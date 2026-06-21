# Plan 2-CP T 文件规范：格式、生命周期与读写

## T 文件路径规范

```
QQ_data/checkpoints/
├── GroupMessage_<GROUP_B>.json     # 群聊窗口
├── GroupMessage_<GROUP_A>.json
├── FriendMessage_1234567890.json   # 私聊窗口
└── _meta.json                     # 全局元数据（可选）
```

**命名规则**：`{window_type}_{window_id}.json`
- window_type: `GroupMessage` / `FriendMessage`
- window_id: 群号 / QQ号

## T 文件 JSON 格式

```json
{
  "version": 1,
  "window_key": "GroupMessage:<GROUP_B>",
  "window_type": "group",
  "window_id": "<GROUP_B>",

  "T1": {
    "compressed_summary": "历史对话摘要内容...",
    "token_count": 3500,
    "compression_ratio": 0.28,
    "original_msg_count": 45,
    "compression_count": 3,
    "last_compress_time": "2026-04-09T19:00:00",
    "compress_history": [
      {
        "time": "2026-04-09T18:00:00",
        "before_tokens": 12000,
        "after_tokens": 3200,
        "ratio": 0.27,
        "msgs_compressed": 15
      }
    ]
  },

  "messages": [
    {
      "role": "user",
      "content": "[张三] 今天天气真好",
      "timestamp": "2026-04-09T18:30:00",
      "meta": {
        "sender_qq": "1234567890",
        "sender_name": "张三",
        "has_image": false
      }
    },
    {
      "role": "assistant",
      "content": "是呀 天气确实不错呢",
      "timestamp": "2026-04-09T18:30:05",
      "meta": {
        "is_bot": true
      }
    },
    {
      "role": "assistant",
      "content": null,
      "tool_calls": [
        {
          "id": "call_001",
          "function": {
            "name": "web_search",
            "arguments": "{\"query\": \"今天南京天气\"}"
          }
        }
      ],
      "timestamp": "2026-04-09T18:31:00",
      "meta": {"is_bot": true}
    },
    {
      "role": "tool",
      "tool_call_id": "call_001",
      "content": "南京今日晴 25°C...",
      "timestamp": "2026-04-09T18:31:02"
    },
    {
      "role": "assistant",
      "content": "刚帮你查了 今天南京25度呢 很适合出门",
      "timestamp": "2026-04-09T18:31:05",
      "meta": {"is_bot": true}
    }
  ],

  "metadata": {
    "created_at": "2026-04-09T15:00:00",
    "updated_at": "2026-04-09T19:30:00",
    "total_messages_ever": 120,
    "total_compressions": 3,
    "avg_compression_ratio": 0.28
  }
}
```

## T 文件的语义结构

### T 的组成公式

```
T = T1 + T2 + T3

T1 = T 文件中的 "T1.compressed_summary" → 压缩历史摘要
T2 = T 文件中 "messages" 里较早的部分 → 上次压缩时保留的原文
T3 = T 文件中 "messages" 里最新追加的部分 → 新消息
```

> T2 和 T3 在文件中不做物理区分，都在 `messages` 数组中。
> 区分仅在压缩流程中有意义：当触发压缩时，`messages` 的前 N% 被压缩进 T1，剩余成为新的 T2。

### 构建发送给 LLM 的 contexts

```python
def build_llm_contexts(t_file: dict) -> list[dict]:
    """从 T 文件构建 OpenAI 格式 contexts"""
    contexts = []

    # 1. T1：压缩历史摘要
    if t_file["T1"]["compressed_summary"]:
        contexts.append({
            "role": "user",
            "content": f"[对话历史压缩摘要]\n{t_file['T1']['compressed_summary']}"
        })
        contexts.append({
            "role": "assistant",
            "content": "好的，我已了解之前的对话历史。"
        })

    # 2. T2 + T3：原文消息
    for msg in t_file["messages"]:
        ctx_msg = {"role": msg["role"]}
        if msg.get("content") is not None:
            ctx_msg["content"] = msg["content"]
        if msg.get("tool_calls"):
            ctx_msg["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id"):
            ctx_msg["tool_call_id"] = msg["tool_call_id"]
        contexts.append(ctx_msg)

    return contexts
```

## T 文件的生命周期

### 1. 创建

首次遇到某窗口时创建空 T 文件：
```json
{
  "version": 1,
  "window_key": "GroupMessage:<GROUP_B>",
  "T1": {"compressed_summary": "", "token_count": 0, ...},
  "messages": [],
  "metadata": {"created_at": "...", ...}
}
```

### 2. 追加新消息

每次 `on_llm_request` 触发时：
1. 读取 T 文件
2. 从 `req.contexts` 中提取 **尚未记录** 的新消息
3. 追加到 T 文件的 `messages` 数组
4. 保存 T 文件

**增量检测方式**：
- 用 `messages` 数组的长度和内容与 `req.contexts` 对比
- `req.contexts` 中多出的部分就是新消息
- 需要跳过 T1 已压缩的消息（用已压缩数量追踪）

### 3. 压缩

当 T 的总 token 超过阈值时触发压缩：
1. 计算 `candidate = T1_msg + messages` 的总 token
2. 取前 `compress_front_ratio` 比例的消息压缩
3. 调用 Flash Lite 生成新 T1
4. 更新 T 文件：新 T1 + 剩余 messages
5. 保存

### 4. LLM 回复后回写

主模型回复后，将 assistant 回复（含 tool_call 过程）追加到 T 文件：
- 这部分在 `on_llm_request` 的下一次调用时需要处理
- 或通过 AstrBot 的回复钩子（`on_decorating_result` 等）拦截

### 5. 持久化保证

- 每次写操作使用 **先写临时文件再原子重命名** 模式
- 防止写入中断导致文件损坏
- `gzip` 或 JSON 格式均可（根据性能需求选择）

## 容量评估

### 单个 T 文件大小估算

| 组件 | 典型大小 |
|---|---|
| T1 压缩摘要 | 500-2000 字 ≈ 1-4 KB |
| messages（50条） | 每条约 100-300 字 ≈ 25-75 KB |
| 元数据 | < 1 KB |
| **总计** | **30-80 KB / 文件** |

### 磁盘占用
- 假设 20 个活跃窗口 × 80 KB = 1.6 MB
- 可忽略不计

## 安全性考虑

### 文件损坏恢复
- 如果 T 文件读取失败（JSON 损坏），回退到空 T（从零开始积累）
- 记录 error 日志
- 原始消息在 messages.db 和 AstrBot conversation.history 中都有备份

### 并发访问
- 同一窗口的 T 文件在同一时刻只有一个 `on_llm_request` 在操作
- 使用 per-window 的 asyncio.Lock 保证互斥
