# Plan 2-CP 架构设计：三系统分立

## 系统关系图

```
┌──────────────────────────────────────────────────────────┐
│                     QQ 消息进入                           │
│                         │                                │
│                    ┌────▼────┐                            │
│                    │ FlashLite │                           │
│                    │ 中断引擎   │ ← 读取 T 文件作为上下文    │
│                    └────┬────┘                            │
│            ┌────────────┼────────────┐                    │
│            │            │            │                    │
│    ┌───────▼───┐  ┌─────▼─────┐  ┌──▼──────────┐        │
│    │ Knowledge  │  │  触发判断  │  │ messages.db  │       │
│    │  更新      │  │ 唤醒主模型? │  │  写入新消息   │       │
│    └───────────┘  └─────┬─────┘  └─────────────┘        │
│                         │                                │
│                   (如果需要触发)                           │
│                         │                                │
│              ┌──────────▼──────────┐                     │
│              │    on_llm_request    │                     │
│              │                     │                     │
│              │  1. 从 T 文件读取 T   │                    │
│              │  2. 追加新消息到 T     │                    │
│              │  3. 检查是否需要压缩   │                    │
│              │  4. 如需要则压缩前 N%  │                    │
│              │  5. req.contexts = T  │  ← 替换 AstrBot   │
│              └──────────┬──────────┘    的 contexts       │
│                         │                                │
│              ┌──────────▼──────────┐                     │
│              │    主模型 LLM 调用    │                     │
│              │  (使用 T 作为上下文)   │                     │
│              └──────────┬──────────┘                     │
│                         │                                │
│              ┌──────────▼──────────┐                     │
│              │  LLM 回复后回写 T     │                    │
│              │  (assistant 消息 +    │                    │
│              │   tool_call 过程)     │                    │
│              └─────────────────────┘                     │
└──────────────────────────────────────────────────────────┘
```

## 系统 A：AstrBot 的 req.contexts

### 当前行为（不变）
- AstrBot 每次请求从 `conversation.history`（JSON in DB）加载全部对话历史到 `req.contexts`
- 调用链：`build_main_agent()` → 加载 conversation → `req.contexts = json.loads(conversation.history)`
- LLM 回复后，AstrBot 将新消息追加到 `conversation.history`

### 重构后的角色
- **完整的对话备份**（系统的"录像带"）
- 我们 **不修改 A 的数据**
- 但在 `on_llm_request` 钩子中 **替换 `req.contexts`** 为我们的 T
- AstrBot 的 `agent_runner.reset()` 中的 `truncate_turns` / `enforce_max_turns` 作为最终兜底

### 关键代码位置
- `astr_main_agent.py` L1116: `req.contexts = json.loads(req.conversation.history)`
- `astr_main_agent.py` L1337: `req.contexts = json.loads(conversation.history)`
- `astr_main_agent.py` L1407-1421: `agent_runner.reset(truncate_turns=..., enforce_max_turns=...)`

## 系统 B：FlashLite 的 messages.db

### 当前行为（不变）
- 记录所有 QQ 消息到 `qq_messages` 表
- 提供给 FlashLite 构建 `recent_context`（触发判断用的最近 N 条消息）
- CHECKPOINT 旧逻辑的 token 计算来源（即将废弃）

### 重构后的角色
- 继续作为 QQ 消息流水记录
- **不再用于 CHECKPOINT token 估算和压缩**
- FlashLite 触发判断的上下文来源将迁移到从 T 文件读取（质量更高）
- `checkpoint_history` 表继续保留，供面板查看压缩统计

## 系统 C：Per-window T 文件（新增）

### 设计目标
- 每个对话窗口（群/私聊）独立维护一份「智能压缩上下文」
- T = T1（压缩历史摘要）+ T2（未被压缩的旧消息原文）+ T3（新增消息原文）
- 所有发送给 LLM 的上下文都从 T 构建

### 存储位置
```
QQ_data/
├── messages.db          # 系统 B（不动）
├── checkpoints/
│   ├── GroupMessage_<GROUP_B>.json    # 群聊 T 文件
│   ├── GroupMessage_<GROUP_A>.json
│   ├── FriendMessage_1234567.json    # 私聊 T 文件
│   └── ...
```

### 详细格式
→ 见 [Plan_2_CP_T_file.md](Plan_2_CP_T_file.md)

## 三个模型角色的上下文来源

### FlashLite（Flash 模型 / 中断引擎）
- **输入**：从 T 文件读取完整 T 内容 + 当前新消息
- **用途**：触发判断（是否唤醒主模型）+ Knowledge 更新 + Memory 召回 + 用户画像
- **升级点**：原来从 `messages.db` 读最近 N 条 → 改为从 T 文件读取，获得包含压缩历史的更完整上下文
- **⚠️ 影响**：Knowledge 系统的维护质量会提升（因为 FlashLite 现在有完整的压缩历史上下文）

### 主模型（老板娘）
- **输入**：T 文件内容通过 `req.contexts` 替换注入
- **用途**：实际和用户聊天
- **升级点**：原来收到完整的 AstrBot contexts（可能很长且无压缩）→ 改为收到智能压缩后的 T

### 工具模型（子代理）
- **输入**：默认只收到任务描述（不带上下文）
- **可选**：主模型调用时可通过参数传入 T 的摘要/片段
- **不变**：工具模型的调用方式不受影响

### 工具调用的记录

当主模型发起工具调用时，整个过程需要记录在 T 文件中：
```
T 中的工具调用记录格式：

{"role": "assistant", "content": "...", "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "...结果..."}
{"role": "assistant", "content": "最终回复"}
```

这些中间过程是 Knowledge 系统和后续对话理解的关键上下文，不能丢弃。
