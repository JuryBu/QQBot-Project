# Plan_3_2_KVCache优化.md — FlashLite + 主模型 静态/动态分离

> 优先级：P0 | 预估收益：主模型成本 ↓40-60%，FlashLite 缓存命中率 ↑
> 前置依赖：无 | 影响范围：main.py 提示词构建 + gemini_source.py 缓存逻辑
> 最后更新：2026-04-13 | 状态：方案定稿

---

## 一、优化原理

### Gemini 隐式缓存机制
- Gemini 2.5+ 自动按 **前缀匹配** 启用隐式缓存
- `systemInstruction` 是最稳定的前缀 → 内容越固定，命中率越高
- 缓存命中时，缓存部分的 token 按 **折扣费率** 计费（比正常输入便宜 ~90%）
- 各模型有最小输入 token 阈值（flash-lite-preview 约 1024 tokens）

### 当前问题
三个模型的 system prompt 中都混入了**动态内容**（Knowledge 快照、系统时间、Memory 索引），导致：
- 每次调用的 systemInstruction 不同 → 前缀匹配失败 → 缓存被废弃
- 即使内容变化很小（如时间戳变了1秒），整个缓存也会失效

### 解决方案
**方案 A：全放 systemInstruction** — 将纯静态内容保留在 systemInstruction 中，动态内容移到 contents（user message 前缀），确保 systemInstruction 在连续调用间完全一致。

---

## 二、FlashLite 静态/动态分离

### 2.1 当前结构（_build_flash_lite_system + _call_flash_lite）

```
systemInstruction:
  ├─ 固定部分（身份+消息格式+职责+触发条件+输出格式）  ← ~900 tokens
  ├─ Knowledge 快照                                   ← 动态 ⚠️
  ├─ 系统时间                                          ← 动态 ⚠️
  └─ Memory 迷你索引                                   ← 动态 ⚠️

contents[user]:
  └─ _build_judgment_prompt()                          ← 含固定的任务描述 ⚠️
```

### 2.2 改造后结构

```
systemInstruction:  ← 完全不变，100% 缓存命中
  ├─ 固定部分（身份+消息格式+职责+触发条件+输出格式）
  ├─ 群聊判断任务描述 + 判断规则                         ← 从 user prompt 移入！
  ├─ 私聊判断任务描述 + 判断规则                         ← 从 user prompt 移入！
  ├─ MEMORY_HINT 使用说明 + 排序规则                     ← 从动态部分移入！
  └─ 补充示例（如不够 1024 token）

contents[user]:  ← 仅包含真正动态的数据
  ├─ Knowledge 快照
  ├─ 系统时间
  ├─ Memory 迷你索引
  ├─ 窗口类型标识（群聊/私聊）
  ├─ 当前窗口 Knowledge 缓存摘要
  └─ 最近消息记录（context）
```

### 2.3 关键改动点

#### A. `_build_flash_lite_system()` 增加任务描述

把原本在 `_build_judgment_prompt()` 里的两套判断规则移入 system prompt：

```python
# 新增到 _build_flash_lite_system() 末尾（静态区）
"""
# 任务执行指南

## 消息判断任务（群聊场景）
当 user contents 标注"窗口类型: 群聊"时，执行以下判断：
1. 如果有人明确 @ 了老板娘或使用了唤醒词 → TRIGGER_MAIN=true
2. 如果唤醒词出现在引用、比喻、讨论第三方内容中 → TRIGGER_MAIN=false
3. 如果是普通闲聊与老板娘完全无关 → TRIGGER_MAIN=false
4. knowledge_update 始终要更新

## 消息判断任务（私聊场景）
当 user contents 标注"窗口类型: 私聊"时，执行以下判断：
1. 私聊几乎总是需要回复（TRIGGER_MAIN=true）
2. 以下情况可不回复：纯文件/图片/链接无文字、系统通知
3. knowledge_update 也要更新

## Memory 召回指南
MEMORY_HINT 用法：输出序号精确指定需要召回的记忆，如 MEMORY_HINT=1,3,7
没有相关记忆时不要输出 MEMORY_HINT 或留空
索引排序规则：pinned 优先 → title 字母序，上限 100 条
"""
```

#### B. `_build_judgment_prompt()` 精简为纯数据

```python
def _build_judgment_prompt(self, group_id, context, trigger_type, ...):
    knowledge = self._knowledge_cache.get(group_id, "暂无记录")
    window_label = "私聊" if window_type == "private" else "群聊"
    window_key = f"FriendMessage:{group_id}" if window_type == "private" else f"GroupMessage:{group_id}"
    
    # 不再包含判断规则和任务描述！只有数据
    return f"""窗口类型: {window_label}
窗口标识: {window_key}
上次话题摘要: {knowledge}

## 最近{window_label}记录
{context}

## 触发信息
触发类型: {trigger_type}
{"触发内容: " + trigger_content if trigger_content else ""}
{"发送者: " + sender_name if sender_name else ""}"""
```

#### C. `_call_flash_lite()` 动态内容拼到 user prompt 前缀

```python
# 原来：动态内容在 system prompt 末尾
# 改为：动态内容拼到 user prompt 最前面

_fl_system = self._build_flash_lite_system()  # 纯静态，不再拼动态
_mem_index = await self._build_memory_mini_index()

# 动态前缀
_dynamic_prefix = ""
_knowledge_snapshot = self._knowledge.get_prompt_text() or ""
if _knowledge_snapshot:
    _dynamic_prefix += f"# 当前 Knowledge 快照\n{_knowledge_snapshot}\n\n"
_dynamic_prefix += f"# 系统时间\n{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
if _mem_index:
    _dynamic_prefix += f"{_mem_index}\n\n"

# 最终 user prompt = 动态前缀 + 原始 prompt
_effective_prompt = _dynamic_prefix + prompt
```

#### D. Token 凑够 1024

移入任务描述+判断规则+Memory说明后，预估约 1100-1300 tokens，应该足够。如果不够，补充一个完整的输出示例（群聊+私聊各一个），可额外增加 200-300 tokens。

### 2.4 压缩模式不受影响

FlashLite 压缩模式共用 `_build_flash_lite_system()` 作为 system prompt，新增的判断任务描述对压缩无害（压缩 prompt 自带完整指令，模型不会混淆）。

---

## 三、主模型 静态/动态分离

### 3.1 当前结构（inject_flashlite_context）

17 个 Section 全部注入到 `req.system_prompt` 末尾。

### 3.2 静态/动态分类

| Section | 内容 | 行号 | 分类 | 原因 |
|---------|------|------|------|------|
| S0 体系认知 | 身份+架构+来源说明 | L2490-2519 | **混合** | 含 `当前时间` |
| S1 输出风格约束 | 1-3句话限制 | L2522-2530 | **纯静态** | |
| Knowledge | 全局对话状态 | L2586-2588 | **纯动态** | 每次变化 |
| 对话上下文摘要 | FlashLite 摘要+近期消息 | L2590-2605 | **纯动态** | |
| Memory 召回 | 相关记忆 | L2607-2614 | **纯动态** | |
| 用户卡片 | 用户画像 | L2616-2650 | **纯动态** | |
| T文件/CHECKPOINT | 替换 req.contexts | L2654-2737 | **纯动态** | |
| 工具集说明 | _build_tool_section("brief") | L2739-2742 | **准静态** | 极少变，视同静态 |
| 回复格式+工具规范 | QQ风格+调用规范 | L2744-2786 | **纯静态** | |
| Memory 指南 | 读写记忆规则 | L2788-2804 | **纯静态** | |
| Knowledge 说明 | 全局概览说明 | L2807-2813 | **纯静态** | |
| 文件处理规范 | view_file/web_fetch/save_data | L2816-2858 | **纯静态** | |
| Sandbox 空间 | 工作空间+草稿纸 | L2861-2883 | **纯静态** | |
| 自定义工具 | custom_tools 编写标准 | L2886-2906 | **纯静态** | |
| Task 系统 | task_set 完整说明 | L2909-2938 | **纯静态** | |
| 工具速查 | 分类+示例+OFFICE规范 | L2941-2979 | **准静态** | 自定义工具列表极少变 |
| Sandbox 环境 | env.json 信息 | L2982-3001 | **动态** | 保守处理 |

### 3.3 改造方案

**静态部分**（保留在 system_prompt）：S1, 回复格式, Memory指南, Knowledge说明, 文件规范, Sandbox空间, 自定义工具, Task系统, 工具速查, 工具集说明

**动态部分**（移到 contents user message 前缀）：Knowledge快照, 对话上下文摘要, Memory召回, 用户卡片, Sandbox环境

**混合部分处理**：S0 体系认知 → 拆分为两部分：
- 静态核心（身份+架构+来源说明）保留在 system_prompt
- `当前时间` 移到动态前缀

### 3.4 代码改动思路

```python
async def inject_flashlite_context(self, event, req):
    static_parts = []   # 放 system_prompt（缓存区）
    dynamic_parts = []  # 拼到 contents 第一条 user message 前

    # S0 体系认知（拆出时间）
    static_parts.append("## 系统架构认知（最高优先级）\n..." )  # 不含时间
    dynamic_parts.append(f"**当前时间**：{now} ({weekday})")

    # S1 输出风格 → 静态
    static_parts.append("## 🚨 输出风格硬性约束...")
    
    # Knowledge → 动态
    if knowledge_text:
        dynamic_parts.append(knowledge_text)
    
    # ... 其余 Section 按分类放入对应列表 ...

    # 注入静态部分到 system_prompt
    req.system_prompt = f"{req.system_prompt}\n\n{''.join(static_parts)}"
    
    # 注入动态部分到 contents 第一条 user message 前缀
    if dynamic_parts:
        dynamic_block = "\n\n".join(dynamic_parts)
        if req.contexts and len(req.contexts) > 0:
            first_msg = req.contexts[0]
            if first_msg.get("role") == "user":
                first_msg["content"] = f"{dynamic_block}\n\n---\n\n{first_msg['content']}"
```

### 3.5 gemini_source.py 改动

需要确认 `_ensure_kv_cache()` 的 hash key 生成方式——如果它对整个 system_instruction 做 hash，那么静态化后 hash 就会稳定，自然缓存命中。**允许修改 gemini_source.py**。

---

## 四、改动清单

### Stage 1：FlashLite 静态/动态分离
- [ ] `_build_flash_lite_system()`: 增加两套判断规则 + Memory 说明
- [ ] `_build_flash_lite_system()`: 移除 Knowledge 快照、系统时间、Memory 索引
- [ ] `_build_judgment_prompt()`: 精简为纯数据（移除判断规则和任务描述）
- [ ] `_call_flash_lite()`: 动态内容（Knowledge+时间+Memory索引）拼到 user prompt 前缀
- [ ] 验证静态部分 token 数 ≥ 1024（用 countTokens API 精确计算）
- [ ] 如不够 1024，补充输出示例

### Stage 2：主模型 静态/动态分离
- [ ] `inject_flashlite_context()`: 拆分 inject_parts 为 static_parts + dynamic_parts
- [ ] S0 体系认知：拆出 `当前时间` 到 dynamic_parts
- [ ] 动态 Section（Knowledge/摘要/Memory/卡片/Sandbox环境）→ dynamic_parts
- [ ] dynamic_parts 拼到 req.contexts 第一条 user message 前缀
- [ ] 工具集说明、工具速查视为准静态，保留在 static_parts

### Stage 3：gemini_source.py 缓存适配
- [ ] 确认 `_ensure_kv_cache()` hash 逻辑是否兼容
- [ ] 如需调整，让 system_instruction hash 只基于静态部分
- [ ] 验证缓存命中：检查 API 响应的 `cached_content_token_count > 0`

### Stage 4：验证
- [ ] 本地模拟调用，检查 usageMetadata 中 cached_content_token_count
- [ ] 日志打印缓存命中率
- [ ] 确认 FlashLite 判断/压缩两种模式都正常工作
- [ ] 确认主模型回复质量不下降

---

## 五、Gemini API 定价参考（付费层级，每百万 token，美元）

| 模型 | 输入 | 输出(含思考) | 缓存输入 | 缓存存储/h |
|------|------|-------------|---------|-----------|
| Gemini 3 Flash Preview | $0.30 | $2.50 | $0.03 | $1.00/M |
| Gemini 3.1 Flash-Lite Preview | $0.25 | $1.50 | $0.025 | $1.00/M |
| Gemini 2.5 Flash | $0.30/$1.00 | $2.50 | $0.03/$0.10 | $1.00/M |
| Gemini 2.5 Pro | $1.25/$2.50 | $10/$15 | $0.125/$0.25 | $4.50/M |

> 缓存输入费率仅为正常输入的 **1/10**，即缓存命中 = 输入成本降低 90%。
