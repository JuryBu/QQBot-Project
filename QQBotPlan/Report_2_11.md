# Report 2-11: CHECKPOINT 压缩机制深度分析

## 情景定义（来自用户描述）

> - **A**：内存中该窗口的实际存储原始对话上下文记录（AstrBot 框架自有管理）
> - **T**：当前准备发送给后端 LLM 的请求体内容
> - **T = T1 + T2 + T3**
>   - T1 = 上次 CHECKPOINT 压缩输出内容
>   - T2 = 上次 CHECKPOINT 时未被压缩的原文
>   - T3 = 新产生的原文
>
> 当 T 超过面板设置阈值时触发 CHECKPOINT。

---

## 问题 1: 目前 CHECKPOINT 的机制是什么？

### 实际代码流程（`checkpoint.py` 全文 356 行）

#### 触发时机
- 在 `main.py` 中有 **两处调用点**：
  - L866-881：群消息同步触发（每次 FlashLite 处理群消息后）
  - L1169-1184：私聊消息同步触发（每次处理私聊消息后）
- 即 **每条消息进入 FlashLite 处理后都会检查一次** CHECKPOINT

#### 触发判断（`check_and_compress` L94-197）

```python
# 1. 从 messages.db 读取该窗口【全部】未撤回消息
cursor = await db.execute(
    """SELECT ... FROM qq_messages
       WHERE window_type = ? AND window_id = ? AND is_recalled = 0
       ORDER BY created_at ASC""",
    (window_type, window_id),
)
messages = [dict(row) for row in await cursor.fetchall()]

# 2. 估算全部消息的 token 总数
total_tokens = sum(self.estimate_message_tokens(m) for m in messages)

# 3. 和 token_limit（面板设置 50000）比较
if total_tokens < self.token_limit:
    return None  # 不需要压缩
```

> **⚠️ 关键问题**：这里读取的是 `qq_messages` 表中的 **全部未撤回消息**，不是 AstrBot 内存中实际发送给 LLM 的上下文。这是两套完全独立的系统。

#### 压缩过程

```python
# 4. 分割：保留最近 keep_recent（默认10条）不压缩
to_compress = messages[:-self.keep_recent]   # 前面的消息
to_keep = messages[-self.keep_recent:]       # 最近10条保留

# 5. 构建压缩 prompt 发送给 Flash Lite
prompt = self._build_compress_prompt(to_compress, compress_tokens)
compressed_text = await flash_lite_caller(prompt) 

# 6. 计算压缩率 = 压缩后 token / 被压缩的原文 token
compression_ratio = compressed_tokens / max(compress_tokens, 1)

# 7. 写入 checkpoint_history 表
await self._save_checkpoint(...)
```

> **⚠️ 致命问题**：压缩完成后 **不删除、不标记、不修改** `qq_messages` 表中的原始消息。下次处理消息时，`check_and_compress` 再次读取全部消息，token 仍然超限 → **再次触发压缩** → 无限循环！

这就是截图中 **30次压缩 / 12分钟内连续触发** 的根因。

---

## 问题 2: 目前拼装上下文的机制是什么？

### 两套独立的上下文系统

当前存在 **两套完全独立的对话上下文管理**，它们互不影响：

#### 系统 A：AstrBot 框架的内存对话历史（`req.contexts`）
- AstrBot 核心维护的 OpenAI 格式 contexts 列表
- 存储位置：内存（`ProviderRequest.contexts: list[dict]`）
- 由 AstrBot 的 `Conversation` 对象管理（框架级别的对话持久化/DB）
- FlashLite **不参与也不能修改** 这个列表
- 这是实际发送给 LLM API 的对话历史

#### 系统 B：FlashLite 的 `messages.db`（QQ 消息持久化）
- FlashLite 自己记录的 QQ 消息流水
- 存储位置：`QQ_data/messages.db` 的 `qq_messages` 表
- CHECKPOINT 的 token 计算和压缩都基于这个表
- 压缩结果存在 `checkpoint_history` 表

### 实际拼装过程（`on_llm_request` 钩子 L2489-2700）

```
主模型请求体 T 的实际构成：

┌─────────────────────────────────────────────┐
│              req.system_prompt               │
│  (AstrBot 框架注入的 persona + FlashLite     │
│   on_llm_request 追加的 inject_parts)        │
│                                              │
│  inject_parts 包含：                         │
│  ├── 系统架构认知                             │
│  ├── 输出风格约束                             │
│  ├── Knowledge 缓存                          │
│  ├── 用户卡片                                │
│  ├── CHECKPOINT 摘要（从 checkpoint_history   │  ← 系统 B 的产物
│  │   表读最新一条）                           │
│  ├── 工具集说明                               │
│  └── 回复格式要求                             │
├─────────────────────────────────────────────┤
│              req.contexts                    │  ← 系统 A 的产物
│  (AstrBot 框架管理的对话历史)                  │
│  [user: msg1, assistant: reply1, ...]        │
│  FlashLite 不参与此部分的生成和管理             │
├─────────────────────────────────────────────┤
│     req.prompt + req.image_urls              │
│  (当前消息)                                   │
└─────────────────────────────────────────────┘
```

---

## 问题 3: 工具模型、主模型、FlashLite 模型收到的上下文是什么？

### FlashLite（Flash 模型 / 工具模型）
- **调用方式**：`main.py` 中 `_call_flash_lite(prompt)` 直接调 Gemini API
- **收到的上下文**：仅当次构建的 prompt 字符串（无历史对话）
- **用途**：Knowledge 更新、CHECKPOINT 压缩、触发判断

### 主模型（老板娘 Gemini）
- **调用方式**：AstrBot 框架通过 `ProviderRequest` 调用
- **收到的上下文**：
  ```
  system_prompt = AstrBot persona + FlashLite inject_parts
  contexts     = AstrBot 框架管理的对话历史（req.contexts）
  prompt       = 当前用户消息
  ```
- FlashLite 的 CHECKPOINT 摘要被注入到 `system_prompt` 中（作为文本块追加），**不是** contexts 的一部分

### CHECKPOINT 压缩时的 Flash Lite
- **调用方式**：`check_and_compress` → `flash_lite_caller(prompt)`
- **收到的上下文**：仅包含从 `qq_messages` 表读取的原始 QQ 消息格式化文本
- **不包含**：AstrBot 的对话历史（req.contexts）

---

## 问题 4: 如何保证压缩率在设置范围内？

### 当前实现：**不保证**

```python
# _build_compress_prompt 中只是在 prompt 文字里「告诉」Flash Lite 压缩率目标
# 但没有任何验证或重试机制

prompt = f"""...
- 目标压缩率: {self.target_compression_min*100:.0f}%-{self.target_compression_max*100:.0f}%
  （原文约 {total_tokens} tokens）
..."""

# 压缩后只是被动计算压缩率，然后原样保存
compressed_tokens = self.estimate_tokens(compressed_text)
compression_ratio = compressed_tokens / max(compress_tokens, 1)
# 即使 ratio 远低于 target_compression_min，也直接保存
await self._save_checkpoint(...)
```

面板日志显示 **压缩率 0%-1%**（远低于设置的 20%-40%），说明：
1. Flash Lite 生成的摘要过于简略（~130-386 tokens，但原文可能是数万 tokens）
2. 没有验证压缩率是否在目标范围内
3. 没有重试机制让模型重新生成更详细的摘要

---

## 核心症结总结

用用户的 T1/T2/T3 模型来说明：

| 理想设计 | 当前实现 | 差异 |
|---|---|---|
| T 超过阈值时触发 | `qq_messages` 全表超阈值时触发 | ❌ 压缩的是 B 系统的 DB 消息，不是 A 系统的 req.contexts |
| 压缩 T 的前 N% 为 T' | 压缩全部旧消息（保留最后 10 条） | ❌ 没有"前 N%"参数，且对象不同 |
| T'\_o 替换 T' 部分 | 压缩结果只存 checkpoint\_history 表 | ❌ 不影响 req.contexts，下次仍读全量 |
| 压缩后短时间内不再触发 | 下次消息来仍读全量→仍超限→再触发 | ❌ 每条消息都触发（截图中每分钟数次） |
| 压缩率在 0.2-0.4 范围内 | 只在 prompt 文字中要求，无验证 | ❌ 实际 0%-1%，远低于目标 |
| 不影响内存中的原始记录 A | 确实不影响（但也不影响 T） | ⚠️ 虽然不影响 A 对了，但也不影响 T 就错了 |

### 根本问题

**CHECKPOINT 机制操作的对象（`qq_messages` 表）和实际需要压缩的对象（`req.contexts` 对话历史）是两套完全独立的系统。** 

FlashLite 的 CHECKPOINT 只在旁路（系统 B）自嗨——从 DB 读消息、压缩、存回 DB、注入 system_prompt——但对 AstrBot 框架管理的真正对话上下文（`req.contexts`）完全没有影响。

---

## 理想设计 vs 需要补全的能力

### 缺失参数
- `compress_ratio`：T 中前面多少比例送入压缩（面板中没有此参数）

### 需要修改的核心逻辑
1. **CHECKPOINT 的数据源应该是 `req.contexts`**（系统 A），不是 `qq_messages`（系统 B）
2. 压缩后需要 **修改 `req.contexts`**：用 T'\_o 替换被压缩的部分
3. 需要 **防止反复触发**：压缩后更新内存状态标记
4. 需要 **验证压缩率**：压缩后检查是否在目标范围内，不达标则调整 prompt 重试
5. 整个过程应该在 `on_llm_request` 阶段操作 `req.contexts`，而不是在消息处理后操作 DB

---
---

# 第二部分：重构方案设计

## 0. 新增发现：AstrBot 框架已有的上下文管理

在 `astr_main_agent.py` L1407-1421 发现 AstrBot 框架已有：
```python
agent_runner.reset(
    ...
    llm_compress_instruction=...,     # 框架级别的 LLM 压缩指令
    llm_compress_keep_recent=...,     # 保留最近 N 条
    llm_compress_provider=...,        # 压缩用的 Provider
    truncate_turns=...,               # 截断轮次
    enforce_max_turns=...,            # 强制最大轮次
)
```

这意味着 AstrBot 自己也有上下文管理，但在 `build_main_agent` 中（在 FlashLite 的 `on_llm_request` hook 之后）。我们的 CHECKPOINT 可以和它并存——CHECKPOINT 是更智能的压缩（保留摘要），框架的是兜底截断。

另外确认：**每次请求 AstrBot 都从 `conversation.history`（JSON 字符串）完全重建 `req.contexts`**：
```python
# L1116
req.contexts = json.loads(req.conversation.history)
# L1337  
req.contexts = json.loads(conversation.history)
```

这意味着我们在 `on_llm_request` 中修改 `req.contexts` **只影响本次请求**，下次请求会重新从 DB 加载完整历史。这正是理想设计中"不影响 A"的天然保证。

---

## 1. 核心设计

### 1.1 Per-window 状态

```python
@dataclass
class CheckpointState:
    T1: str = ""                    # 累积压缩摘要
    T1_tokens: int = 0              # T1 的估算 token 数
    compressed_contexts_count: int = 0  # req.contexts 中从头起已被压缩进 T1 的消息数
    last_compress_time: float = 0   # 上次压缩时间戳（防频繁触发）
```

存储位置：`self._checkpoint_states: Dict[window_key, CheckpointState]`（内存中）

### 1.2 on_llm_request 中的执行流程

```
每次 on_llm_request(event, req):

  ① 获取 window_key（从 event 中提取）
  ② 获取 state = _checkpoint_states.get(window_key) 或创建新的
  
  ③ 跳过已压缩消息：
     N = len(req.contexts)
     skip = min(state.compressed_contexts_count, N)
     # 安全检查：如果 N < skip（AstrBot 框架自己截断了旧消息），
     # 说明部分消息既不在 T1 中也不在 contexts 中，
     # 这种情况下 T1 仍然涵盖它们，不用特殊处理
     
     fresh_contexts = req.contexts[skip:]
     
  ④ 构建当前请求的完整上下文 candidate：
     if state.T1:
         T1_msg = {"role": "user", "content": f"[对话历史压缩摘要]\n{state.T1}"}
         T1_ack = {"role": "assistant", "content": "好的，我已了解之前的对话历史。"}
         candidate = [T1_msg, T1_ack] + fresh_contexts
     else:
         candidate = fresh_contexts
  
  ⑤ 估算 total_tokens：
     system_tokens = estimate_tokens(req.system_prompt)
     context_tokens = sum(estimate_msg_tokens(m) for m in candidate)
     total = system_tokens + context_tokens
     
  ⑥ 判断是否触发压缩：
     需要同时满足：
     - total > checkpoint_token_limit（面板设置，默认 50000）
     - len(candidate) > keep_recent（至少有足够消息可压缩）
     - time.now() - state.last_compress_time > cooldown_seconds（冷却期）
     
  ⑦ 如果触发压缩：
     a. 计算压缩范围：
        compress_count = max(1, floor(len(candidate) * compress_front_ratio))
        # 确保至少保留 keep_recent 条消息不压缩
        compress_count = min(compress_count, len(candidate) - keep_recent)
        
     b. 分割：
        to_compress = candidate[:compress_count]  # 包含 T1（如果存在则自然在里面）
        to_keep = candidate[compress_count:]
        
     c. 构建压缩 prompt，调用 Flash Lite
     d. 验证压缩率（见 1.4）
     
     e. 更新状态：
        state.T1 = compressed_output
        state.T1_tokens = estimate_tokens(compressed_output)
        state.compressed_contexts_count = N - len([仅 fresh 中未被压缩的部分])
        state.last_compress_time = time.now()
        
     f. 构建新 candidate：
        new_T1_msg = {"role": "user", "content": f"[对话历史压缩摘要]\n{state.T1}"}
        new_T1_ack = {"role": "assistant", "content": "好的，我已了解之前的对话历史。"}
        req.contexts = [new_T1_msg, new_T1_ack] + to_keep
     
  ⑧ 如果不触发：
     req.contexts = candidate  # T1 + fresh 消息，直接作为请求上下文
  
  ⑨ 保存到 checkpoint_history 表（供面板查看统计）
```

### 1.3 RNN 式遗忘效应

当压缩范围包含 T1（即 `candidate[0]` 是 T1_msg）时：
- 旧的压缩摘要 T1 被和新消息一起再次送入 Flash Lite 压缩
- Flash Lite 会将"摘要 + 新消息"合并为新摘要
- 旧信息的密度自然递减 = 遗忘效应

### 1.4 压缩率保证

```python
# 1. Prompt 中明确目标 token 数而非百分比
target_tokens = int(compress_tokens * (target_min + target_max) / 2)
target_chars = int(target_tokens * 1.5)  # 中文约 1.5 字/token

prompt += f"""
请输出约 {target_chars} 个中文字的摘要（约 {target_tokens} tokens）。
不要过于简略，也不要逐字复制，保持信息密度在 {target_min*100:.0f}%-{target_max*100:.0f}% 之间。
"""

# 2. 后验证（不重试，但记录 warning）
actual_ratio = compressed_tokens / original_tokens
if actual_ratio < target_min:
    logger.warning(f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 低于目标 {target_min:.1%}，摘要可能过于简略")
elif actual_ratio > target_max:
    logger.warning(f"[CHECKPOINT] 压缩率 {actual_ratio:.1%} 高于目标 {target_max:.1%}，摘要可能保留过多细节")
```

---

## 2. 面板参数变更

### 新增参数

| 参数名 | 默认值 | 说明 |
|---|---|---|
| `checkpoint_compress_front_ratio` | `0.7` | 触发时压缩 candidate 的前多少比例（0.7 = 压缩 70%，保留 30% 原文） |
| `checkpoint_cooldown_seconds` | `300` | 两次压缩之间的最小间隔（秒） |

### 保留参数

| 参数名 | 当前值 | 说明 |
|---|---|---|
| `checkpoint_token_limit` | `50000` | 总 token 超过此阈值触发压缩 |
| `checkpoint_keep_recent` | `10` | 至少保留最近 N 条消息不压缩 |
| `target_compression_min` | `0.2` | 压缩率下限 |
| `target_compression_max` | `0.4` | 压缩率上限 |

---

## 3. 需要移除/修改的旧逻辑

### 3.1 移除：消息处理后的 CHECKPOINT 调用

- `main.py` L866-881（群消息处理后调用 `check_and_compress`） → **删除**
- `main.py` L1169-1184（私聊消息处理后调用）→ **删除**

### 3.2 移除：system_prompt 中的 CHECKPOINT 摘要注入

- `main.py` L2667-2687（`on_llm_request` 中注入 CHECKPOINT 到 system_prompt）→ **删除**
- 改为在 ⑦⑧ 步中直接修改 `req.contexts`

### 3.3 重写：checkpoint.py

- `check_and_compress()` → 删除（不再从 messages.db 读取）
- `build_context_for_main_model()` → 删除
- 保留：`estimate_tokens()`, `estimate_message_tokens()`, `_save_checkpoint()`, `get_stats()`
- 新增：`compress_contexts()` — 接收 `List[Dict]` 格式的 contexts 而非 messages.db 消息

### 3.4 保留：checkpoint_history 表

继续使用，供面板查看压缩历史统计。

---

## 4. ⚠️ 需要讨论的问题

### 问题 1：compressed_contexts_count 的准确性

每次请求，AstrBot 从 `conversation.history` JSON 加载全部历史到 `req.contexts`。我们用 `compressed_contexts_count` 记录"前 N 条已被压缩进 T1"。

**风险**：如果 AstrBot 在某些情况下（如达到 `enforce_max_turns`）截断了旧消息，`len(req.contexts)` 可能小于 `compressed_contexts_count`。

**处理方案**：`skip = min(state.compressed_contexts_count, len(req.contexts))`，如果 `skip < compressed_contexts_count`，说明 AstrBot 已经帮我们删了一些消息，T1 仍然包含那些消息的摘要，不受影响。

**主人觉得这个处理合理吗？**

### 问题 2：T1 放在 contexts 还是 system_prompt？

当前方案是放在 contexts 中作为 user/assistant 消息对。

- **优点**：符合 T = T1+T2+T3 的模型，LLM 会把它当作对话历史理解
- **缺点**：占用 contexts 位置（2 条消息），且每次请求都需要注入

**备选**：放在 system_prompt 中（当前做法），但这样就不符合"T 的一部分"的理想模型。

**主人倾向哪种？**

### 问题 3：重启后 T1 丢失

`_checkpoint_states` 存在内存中，AstrBot 重启后丢失。

- **方案 A**：接受丢失——重启后等同于新对话，从零开始积累
- **方案 B**：每次压缩时将 T1 和 compressed_contexts_count 存到 checkpoint_history 表，重启时恢复

**方案 A 更简单，方案 B 更完善。主人选哪个？**

### 问题 4：与 AstrBot 框架自带压缩的关系

AstrBot 的 `agent_runner.reset()` 有 `llm_compress_*` 和 `truncate_turns`/`enforce_max_turns` 参数。

我们的 CHECKPOINT 和它的关系：
- **FlashLite CHECKPOINT**：智能压缩，在 `on_llm_request` 阶段修改 `req.contexts`（先执行）
- **AstrBot 框架压缩**：在 `agent_runner.reset()` 中执行（后执行），作为兜底

两者并存应该没问题——FlashLite 先压缩到合理大小，框架如果还觉得太大会再截断。

**但需要确认：AstrBot 目前的 `truncate_turns` / `enforce_max_turns` 设置值是多少？如果设置很小（比如 20 轮），那么框架会在 FlashLite 之后再截断，我们的压缩可能白做了。**

### 问题 5：Flash Lite 调用方式

当前 `flash_lite_caller` 是 `_call_flash_lite(prompt)`，直接调 Gemini Flash API。

但在 `on_llm_request` 钩子中，我们没有 `_call_flash_lite` 的直接引用（它是 main.py 的方法），而 `on_llm_request` 是通过 event hook 调用的。

**需要确认：`on_llm_request` 中能否访问 `self._call_flash_lite`？**（应该可以，因为 hook 方法是 FlashLite 实例的方法）

---

## 5. Task 分解

```markdown
## CHECKPOINT 重构 Task

### Stage 1: 基础设施
- [ ] 定义 CheckpointState dataclass
- [ ] 在 FlashLite.__init__ 中初始化 _checkpoint_states 字典
- [ ] 面板新增 compress_front_ratio 和 cooldown_seconds 参数
- [ ] BossLady Console 前后端同步新参数

### Stage 2: 核心逻辑重写
- [ ] 重写 checkpoint.py：
  - [ ] 保留 estimate_tokens / estimate_message_tokens / _save_checkpoint / get_stats
  - [ ] 新增 compress_contexts(candidate, compress_front_ratio, keep_recent, caller) → (new_T1, to_keep)
  - [ ] 删除 check_and_compress / build_context_for_main_model
- [ ] 修改 on_llm_request 钩子：
  - [ ] 删除旧的 system_prompt CHECKPOINT 注入（L2667-2687）
  - [ ] 新增 req.contexts 拦截和压缩逻辑（步骤①-⑨）
- [ ] 删除消息处理后的旧调用点：
  - [ ] main.py L866-881（群消息）
  - [ ] main.py L1169-1184（私聊）

### Stage 3: 压缩质量优化
- [ ] 重写 _build_compress_prompt：明确目标 token 数和字数
- [ ] 添加压缩率后验证 + warning 日志
- [ ] 调整 prompt 模板确保摘要信息密度

### Stage 4: 验证
- [ ] 启动 AstrBot，触发群聊对话
- [ ] 检查日志：确认压缩率在 20-40% 目标范围
- [ ] 检查日志：确认不再频繁触发（冷却期生效）
- [ ] 检查面板：CHECKPOINT 历史显示合理的压缩率和 token 数
- [ ] 确认主模型回复质量不受影响
```
