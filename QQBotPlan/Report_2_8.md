# Report_2_8.md — AstrBot 原生机制对 FlashLite 干扰审计

> 审计时间：2026-04-08 | 对应 Plan_2_2.md 问题 13

---

## 问题 1: AstrBot 触发机制是否在影响群聊和私聊？

### 结论：**群聊有多层触发竞争，私聊已被 FlashLite 完全接管**

### 群聊触发链路分析

AstrBot 对群消息的处理存在 **三层并行触发机制**：

| 层级 | 触发源 | 优先级 | 行为 |
|------|--------|--------|------|
| 1 | `heartflow` | p1000（event_message_type） | 用小模型判断是否回复，通过设 `event.is_at_or_wake_command=True` 唤醒主管道 |
| 2 | `group_chat` | 无优先级（event_message_type） | 独立意愿计算 + ResponseEngine 生成回复，**绕过主模型 persona** |
| 3 | AstrBot 原生管道 | 默认 | @机器人 / 唤醒词触发主模型 |

**问题点**：
- `heartflow` 和 `group_chat` 都在监听群消息，两者对"是否回复"有独立判断逻辑，可能出现**双重回复**
- `heartflow` 修改 `event.is_at_or_wake_command` 后，消息会进入 AstrBot 主管道，此时 FlashLite 的 on_llm_request(p9000) 会注入上下文——这条链路是**正常的**
- `group_chat` 通过 `yield event.plain_result()` 直接生成回复，**完全不经过 persona/FlashLite 管道**，输出风格不受 FlashLite 约束控制

### 私聊触发链路分析

FlashLite 的 `_private_trigger` 方法独立处理私聊消息，通过 `event.stop_event()` 机制拦截 AstrBot 原生管道。**私聊不受上述三个插件影响**（它们都只监听 GROUP_MESSAGE）。

---

## 问题 2: AstrBot 身份提示词注入是否和 FlashLite 重复？

### 结论：**不重复，两者是拼接关系，但存在优化空间**

### 注入架构

```
[AstrBot persona] ← persona_mgr.py L423-425，从 DB 加载，约 1100 字
      ↓ 
      拼接
      ↓
[FlashLite inject_parts] ← on_llm_request(p9000)，追加到 req.system_prompt 末尾
```

- AstrBot persona 定义**老板娘的人格、语气、性格**（"你是xx居酒屋的老板娘..."）
- FlashLite Section 0 定义**系统架构认知**（"你运行在 AstrBot + FlashLite 体系中..."）
- FlashLite Section 1 定义**输出风格硬性约束**（"每次最多1-3句话..."）

**两者不重复**，各司其职。AstrBot persona 是"你是谁"，FlashLite 注入的是"你怎么工作"。

### 潜在问题

AstrBot 的默认 persona 有个 fallback 值 `DEFAULT_PERSONALITY = "You are a helpful and friendly assistant."`。如果 persona 配置丢失，会退化为这个默认值。但当前配置正常，不影响。

---

## 问题 3: AstrBot 是否是导致老板娘无法遵守"简短输出"约束的罪魁祸首？

### 结论：**不是 AstrBot 的锅，但有两个推波助澜的因素**

### 核心分析

FlashLite 的 Section 1 明确规定了"每次最多 1-3 句话"，这条约束在 system_prompt 的前部，位置优先级高。**AstrBot 的 persona 注入不包含任何"详细回答"指令**——它只定义人格。

### 真正的干扰源

#### 1. context_enhancer（优先级 p100）—— **轻微干扰**

`context_enhancer` 的 `on_llm_request` 在 FlashLite(p9000) 之前执行，它会：
1. 检查 `_should_enhance_context` → **仅处理 GROUP_MESSAGE**
2. 直接 **替换** `request.prompt`（L836: `request.prompt = context_enhancement`）
3. 在 prompt 中注入 `passive_reply_instruction` 或 `active_speech_instruction`

但它**不修改 system_prompt**，也有 `_context_enhanced` 标志位防止重复处理。
加上 FlashLite 在 p9000 时追加 system_prompt，两者实际上是在不同字段操作：

- context_enhancer → 修改 `request.prompt`（用户消息）
- FlashLite → 追加 `request.system_prompt`（系统指令）

**不直接冲突**，但 context_enhancer 注入的指令模板可能隐含"详细回复"的暗示。

#### 2. 主模型自身的 system_prompt 长度（~8000-11000 字）

FlashLite 注入的 Section 7-15（工具集说明、格式规范、Sandbox 说明等）合计约 5000-6000 字。这些详细的工具使用说明可能**稀释**了简短输出约束的权重。

> **根本原因**：Gemini 模型对 system_prompt 中不同段落的遵守权重与位置和长度相关。当工具说明占据大量篇幅时，开头的"1-3 句话"约束的实际执行力会降低。

### 建议

- 在 system_prompt **末尾**再次重申简短输出约束（首尾夹击）
- 考虑将工具详细说明从 system_prompt 移到工具自身的 description 中

---

## 问题 4: 插件需求评估

### 完整插件清单与评估

| 插件名 | Hook 类型 | 功能 | 评估 |
|--------|-----------|------|------|
| **astrbot_plugin_flashlite** | on_llm_request(p9000) + on_astrbot_loaded + 26个 llm_tool | 核心引擎 | ✅ **必须保留** |
| **astrbot_plugin_context_enhancer** | on_llm_request(p100) + on_llm_response(p100) | 群聊上下文增强 | ⚠️ **需要评估** |
| **astrbot_plugin_heartflow** | event_message_type(p1000) | 群聊主动回复决策 | ⚠️ **需要评估** |
| **astrbot_plugin_group_chat** | on_astrbot_loaded + event_message_type | 群聊智能交互 | ⚠️ **需要评估** |
| **astrbot_plugin_knowledge_base** | on_llm_request(无优先级) | 知识库RAG注入 | ⚠️ **可能冲突** |
| **astrbot_plugin_recall_cancel** | on_llm_request(p1) + on_llm_response + on_decorating_result + after_message_sent + on_astrbot_loaded | 撤回取消 | ✅ 独立功能，无干扰 |
| **astrbot_plugin_letai_sendemojis** | on_decorating_result | 表情包发送 | ⚠️ **有时序问题** |
| **astrbot_plugin_persistence** | on_astrbot_loaded | 数据持久化 | ✅ 独立功能，无干扰 |
| **astrbot_plugin_pixiv_search** | 无 Hook | Pixiv 搜索命令 | ✅ 独立功能，无干扰 |
| **astrbot_plugin_setu** | 无 Hook | 色图命令 | ✅ 独立功能，无干扰 |
| **astrbot_plugin_chatsummary** | 无 Hook | 群聊总结 | ✅ 独立功能，无干扰 |

### 干扰分析

#### `context_enhancer` — 中等干扰

- **影响范围**：仅群聊
- **干扰行为**：在 FlashLite 之前修改 `request.prompt`，注入群聊历史和指令模板
- **判断**：FlashLite 自身已有 Section 3（上下文摘要+最近消息原文），context_enhancer 的群聊历史注入**功能重叠**
- **建议**：如果 FlashLite 的上下文足够丰富，可以考虑禁用此插件或限制其作用域

#### `heartflow` — 低干扰

- **影响范围**：仅群聊的"是否主动回复"决策
- **干扰行为**：设置 `is_at_or_wake_command=True` 后进入主管道
- **判断**：FlashLite 自身有更精准的群聊触发判断（中断引擎），heartflow 的小模型判断**可能与 FlashLite 决策冲突**
- **建议**：如果 FlashLite 的群聊触发已足够智能，heartflow 是冗余的

#### `group_chat` — 高干扰

- **影响范围**：仅群聊
- **干扰行为**：完全独立的回复引擎，**绕过 persona 和 FlashLite**
- **判断**：它的回复不受 FlashLite 的输出风格约束，可能产生不一致的老板娘人格
- **建议**：如果不需要它的独立意愿系统，应禁用

#### `knowledge_base` — 低干扰

- **影响范围**：所有 LLM 请求
- **干扰行为**：往 system_prompt 注入 RAG 检索结果
- **判断**：FlashLite 自身有 Knowledge 系统，可能与此重复
- **建议**：检查是否真的配置了知识库，如果没有则不影响

#### `letai_sendemojis` — 有时序问题

- **影响范围**：所有回复
- **干扰行为**：通过 `asyncio.create_task` 延迟发送表情包
- **时序问题**：延迟计算为 `plain_count * per_comp + buffer`，但这只是对本次回复分段数的估算。如果 AstrBot 发送文字比预期更慢（网络延迟等），表情包可能**先于最后一段文字到达**
- **建议**：见问题 5

---

## 问题 5: 表情包功能是否应该内化？

### 结论：**建议内化，理由充分**

### 当前问题

1. **时序不可控**：`letai_sendemojis` 用 `asyncio.sleep` 估算延迟，但无法感知 AstrBot 实际的消息发送完成事件
2. **情感匹配粗糙**：基于关键词的情感分析（670行硬编码关键词映射），准确度远低于主模型自身的理解
3. **表情包来源不稳定**：依赖 GitHub raw 链接下载 ChineseBQB，网络可靠性存疑
4. **概率触发不可预测**：30% 基础概率 + 各种动态调节因子，行为不确定
5. **不感知 FlashLite 语境**：它不知道当前消息是群聊还是私聊、是简短回复还是文件输出

### 内化方案

如果内化表情包功能到 FlashLite：

1. **时序可控**：FlashLite 在 `_handle_llm_result` 中处理分段发送，可以**确保表情包在所有文字段之后发送**
2. **语义精准**：主模型自身判断是否应该发表情包，比关键词匹配精准得多
3. **上下文感知**：知道当前是群聊/私聊、是否为文件输出模式，避免不合时宜的表情包
4. **格式可控**：可以通过 prompt 让主模型在回复中标记 `[emoji:类别]`，FlashLite 解析后匹配发送

### 禁用建议

如果内化：
- 禁用 `letai_sendemojis` 插件
- 将其表情包数据源（ChineseBQB JSON）迁移到 FlashLite 的 data 目录
- 在 FlashLite 的输出解析中增加表情包标记处理

---

## 总结：建议操作优先级

| 优先级 | 操作 | 原因 |
|--------|------|------|
| **P0** | 在 system_prompt 末尾重申简短约束 | 立即可做，效果显著 |
| **P1** | 禁用 `group_chat` 插件 | 它绕过 persona/FlashLite，产生不一致输出 |
| **P1** | 评估是否禁用 `heartflow` | FlashLite 中断引擎已有群聊触发判断 |
| **P2** | 评估是否禁用 `context_enhancer` | 与 FlashLite Section 3 功能重叠 |
| **P2** | 内化表情包功能 + 禁用 `letai_sendemojis` | 解决时序问题，提升质量 |
| **P3** | 检查 `knowledge_base` 配置 | 确认是否有实际知识库在用 |

---

## 深度追查：模型为什么不遵守"简短输出"约束？

> 本节基于对 `astr_main_agent.py` 的完整 system_prompt 注入链路逆向分析

### 1. 完整 system_prompt 注入链路（时序精确）

```
build_main_agent() (astr_main_agent.py L1091)
│
├── 1. _decorate_llm_request (L1356)
│   ├── 1a. _ensure_persona_and_skills (L730)
│   │   └── req.system_prompt += "\n# Persona Instructions\n\n{persona_prompt}\n"  [L346]
│   │   └── req.func_tool = persona_toolset  (注入所有 @llm_tool)
│   ├── 1b. _process_quote_message (L744)
│   └── 1c. _append_system_reminders (L756) → extra_user_content_parts（非 system_prompt）
│
├── 2. _apply_kb (L1358) → 知识库注入（如配置）
│
├── 3. TOOL_CALL_PROMPT 追加 (L1399-1405)
│   └── req.system_prompt += "\nWhen using tools: ...briefly explain..."
│
├── 4. LLM_SAFETY_MODE 追加 (L1367-1368) ← 如果启用
│   └── req.system_prompt += LLM_SAFETY_MODE_SYSTEM_PROMPT
│
└── 返回 req
    │
    ▼
call_event_hook(OnLLMRequestEvent, req) (internal.py L229)
│
├── context_enhancer (p100) → request.prompt = context_enhancement [L836]
│   └── 包含指令："直接回复该用户" / "自然地切入对话"
│
├── knowledge_base (无显式优先级) → enhance_request_with_kb 注入 RAG
│
└── FlashLite (p9000) → req.system_prompt += inject_parts [L2378]
    ├── Section 0: 体系认知
    ├── Section 1: 输出风格硬性约束 ← "每次最多1-3句话"
    ├── Section 2-6: 条件注入（Knowledge/Memory/卡片等）
    └── Section 7-16: 工具说明/格式规范/Sandbox 等 ← ~5000字
```

### 2. 根因分析：不止是"稀释"

**根因 A：context_enhancer 的隐性对抗指令**

context_enhancer 在 p100 执行时，将 `request.prompt` **整体替换**为包含群聊历史 + 指令模板的内容：

```python
# 被动场景（L283）
passive_reply_instruction = '现在，群成员 {sender_name} 正在对你说话，
TA说："{original_prompt}"
你需要根据以上聊天记录和你的角色设定，直接回复该用户。'

# 主动场景（L284）
active_speech_instruction = '以上是最近的聊天记录。
你决定主动参与讨论，并想就以下内容发表你的看法：
你需要根据以上聊天记录和你的角色设定，自然地切入对话。'
```

**"直接回复该用户"和"自然地切入对话"** 这些指令隐含了"给出完整的、自然的回答"的暗示。Gemini 模型在解读 `request.prompt` 中的指令时，会与 `system_prompt` 中 FlashLite 的"1-3句话"约束产生**权重竞争**。

> 这不是简单的稀释——这是两套指令系统在**同一请求**中对输出风格发出**相反方向**的指导。

**根因 B：AstrBot 框架的 TOOL_CALL_PROMPT**

```python
TOOL_CALL_PROMPT = (
    "When using tools: "
    "never return an empty response; "
    "briefly explain the purpose before calling a tool; "
    "after execution, briefly summarize the result for the user; "
    "keep the conversation style consistent."
)
```

**"briefly summarize the result"** 和 **"keep the conversation style consistent"** — 这两条指令虽然是关于工具调用的，但模型可能泛化理解为"每次回复都应该包含解释和总结"，与简短约束冲突。

**根因 C：LLM_SAFETY_MODE_SYSTEM_PROMPT 的隐性影响**

```python
"Try to promote healthy, constructive, and positive content that benefits the user's well-being when appropriate."
"Still follow role-playing or style instructions(if exist) unless they conflict with these rules."
```

"promote constructive content" 鼓励模型生成**更有建设性/更完整**的回复。虽然声明了"follow style instructions"，但当简短约束与"constructive content"冲突时，模型可能倾向后者。

**根因 D：system_prompt 结构的位置效应**

最终 system_prompt 结构：
```
[persona ~1100字]     ← 角色描述，可能包含较长对话示例
[TOOL_CALL_PROMPT]    ← "briefly summarize" "keep style consistent"
[SAFETY_MODE_PROMPT]  ← "promote constructive content"
[Section 0-1]         ← "1-3句话" 约束在这里！
[Section 2-6]         ← 条件注入 ~1000-3000字
[Section 7-16]        ← 工具系统规范 ~5000字
```

简短约束在中间偏前位置，但被后续 **5000+ 字的工具详细说明**所淹没。Gemini 模型对 system_prompt 不同位置的遵守权重呈 **U 型曲线**（首尾强，中间弱），简短约束恰好落在权重最低的中段。

### 3. 模型对标点约束"视若无睹"的特殊原因

模型不遵守"禁止 。 ！ ；"的原因更加明确：

1. **persona 中的对话示例** — 如果 persona 的 `begin_dialogs_processed` 中包含了使用 。！的对话示例，模型会从示例中学习风格，覆盖约束
2. **TOOL_CALL_PROMPT 和 SAFETY_MODE_PROMPT 全部用英文** — 模型内部的语言模态切换导致中文标点规则的执行优先级下降
3. **context_enhancer 注入的群聊历史** — 群聊原文中其他用户大量使用 。！标点，模型在生成回复时会"入乡随俗"，模仿对话上下文的标点风格

### 4. 建议的修复方案等级

| 级别 | 方案 | 预计效果 |
|------|------|----------|
| **Lv.1** | 首尾夹击：在 system_prompt 末尾再次重申简短约束 | 中等 |
| **Lv.2** | 禁用 context_enhancer，消除隐性对抗指令 | 高 |
| **Lv.3** | JSON 格式化输出 | 可探索 |
| **Lv.4** | response_schema + maxOutputTokens 硬限制 | 高但有副作用 |

**关于 JSON 格式化输出**：Gemini API 支持 `response_mime_type: "application/json"` + `response_schema` 来强制 JSON 输出。可以定义 schema 为 `{"reply_segments": ["句子1", "句子2"], "emoji": "可选表情关键词"}`，这样模型**必须**分成短句输出。但这会影响工具调用（function calling 和 JSON 输出模式可能冲突），需要验证兼容性。

---

## 扩展: 表情包内化方案详细设计

### 表情包资源现状

表情包目录: `C:\Users\<user>\Desktop\AstrBotLauncher-0.1.5.6\表情包\`

共 38 个文件，命名规则为**情绪关键词组合**：

| 分类 | 示例文件名 | 触发关键词 |
|------|-----------|-----------|
| 通用语气词 | `啊 嗯 哦 呢 噫.jpg` | 啊/嗯/哦/呢/噫/你/我 |
| 正向情绪 | `开心 高兴 好奇.jpg` | 开心/高兴/喜欢/爱 |
| 负向情绪 | `伤心 难过.jpg` | 伤心/难过/无语 |
| 惊讶系 | `震惊 惊吓 害怕.jpg` | 震惊/惊讶/不会吧 |
| 困惑系 | `困惑 疑惑 ？.jpg` | 困惑/为什么/？ |
| 其它 | `吃瓜 看戏 观察.gif` | 吃瓜/观察/担心 |

通用语气词类（含 啊/嗯/哦/呢/噫）≈15个，是"什么时候都能发"的万能表情包。

### 内化方案

将 `letai_sendemojis` 的表情包匹配逻辑整合到 FlashLite 的 `_handle_llm_result` 中：

1. FlashLite 分段发送文字消息后，解析最终回复文本
2. 基于语气词/情绪词匹配本地表情包文件
3. 在**所有文字段发送完毕后** `await asyncio.sleep(0.5)` 再发送表情包
4. 保留概率控制（但由 FlashLite 自行管理，不依赖外部插件）

---

## 扩展: 唤醒词配置建议

### 当前机制

AstrBot 的唤醒词机制位于 `pipeline/waking_check/stage.py`：

```python
# L102-118
wake_prefixes = self.ctx.astrbot_config["wake_prefix"]  # 配置列表
for wake_prefix in wake_prefixes:
    if event.message_str.startswith(wake_prefix):
        event.is_at_or_wake_command = True
        event.message_str = event.message_str[len(wake_prefix):].strip()
```

如果 `wake_prefix` 配置了"老板娘"，则消息以"老板娘"开头就会触发 `is_at_or_wake_command = True`，进入 LLM 管道。

### 建议

- 移除所有自定义唤醒词（保留 `["/""]` 即可，用于命令触发）
- FlashLite 的中断引擎已经有更精准的触发判断逻辑，不需要关键词硬触发
- 保留 @机器人 触发（这是 QQ 原生机制，不走唤醒词逻辑）

---

## 扩展: 插件保留最终决策

### 结论：除 FlashLite 外，其它插件均可禁用

| 插件 | 决策 | 理由 |
|------|------|------|
| **FlashLite** | ✅ 保留 | 核心引擎 |
| **recall_cancel** | ✅ 保留 | 撤回取消独立功能，无干扰 |
| **persistence** | ✅ 保留 | 数据持久化基础设施 |
| **context_enhancer** | ❌ 禁用 | 与 FlashLite 功能重叠 + "直接回复该用户"隐性对抗简短约束 |
| **heartflow** | ❌ 禁用 | FlashLite 中断引擎已覆盖群聊触发判断 |
| **group_chat** | ❌ 禁用 | 绕过 persona/FlashLite 直接生成回复 |
| **letai_sendemojis** | ❌ 禁用（内化） | 表情包功能内化到 FlashLite |
| **knowledge_base** | ⚠️ 评估 | 检查是否配置了实际知识库，没有则禁用 |
| **pixiv_search** | ✅ 保留 | 命令型独立功能 |
| **setu** | ✅ 保留 | 命令型独立功能 |
| **chatsummary** | ✅ 保留 | 命令型独立功能 |

### 禁用 context_enhancer 后的预期效果

1. `request.prompt` 不再被替换，主模型直接收到用户原始消息
2. 消除"直接回复该用户"/"自然地切入对话"的隐性指令
3. FlashLite 的 Section 3（上下文摘要+最近消息原文）已提供足够的群聊上下文
4. **简短约束的实际执行力预期显著提升**
