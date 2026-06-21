# Plan 2-CP 缺漏清单（第二部分）：改进建议与优化方案

> 本文档综合了 Codex 双重 Review、主人 Review 反馈以及原始讨论记录中的设计意图，梳理 **优化改进** 项。

---

## 🟡 P1-3：压缩率硬保证 —— 用 max_tokens API 参数替代纯 Prompt 控制

### 问题描述

当前压缩率控制完全依赖 Prompt 中的文字描述（"你的输出必须在 X 到 Y 字之间"），超出区间只打 warning 但仍接受。Plan_2_CP.md 决策 #9 要求"压缩率必须严格保证"。

### 主人提出的方案（✅ 可行且优雅）

**核心思路**：预计算 `maxOutputTokens` 参数，通过 Gemini API 层面硬限制输出上限，同时在 Prompt 中**去掉一切限制性描述**，鼓励"尽可能详细，写的越多越好"。

### ⚠️ 三点关键要求（主人指定）

#### 要求 1：先测试 maxOutputTokens 参数的有效性
在动手修改前，必须先用一个简单的 API 调用验证 Gemini Flash Lite 是否正确遵守 `maxOutputTokens` 参数。如果模型忽略此参数，整个方案不成立。

测试方法：
```python
# 发送一个需要长回复的 prompt，设置很小的 maxOutputTokens
# 观察返回是否被截断
payload = {
    "contents": [{"role": "user", "parts": [{"text": "写一篇500字的文章"}]}],
    "generationConfig": {"maxOutputTokens": 50}
}
# 预期：返回被截断到约50 tokens
```

#### 要求 2：Prompt 去除限制 + maxOutputTokens 加 Δ 余量
- Prompt 中**不应该有任何字数/token 限制描述**，只鼓励写的越多越好
- `maxOutputTokens` 不能直接设为 `length × target_max`，而应该是 `length × target_max + Δ`
- 原因：模型在接近 maxOutputTokens 上限时一般不会写满，会略微保守
- Δ 的估算：约 10-15% 的余量

```python
# 计算公式
raw_max = int(compress_tokens * target_max)
delta = max(50, int(raw_max * 0.15))  # 15% 余量，最少 50 tokens
max_output_tokens = raw_max + delta
```

#### 要求 3：优先使用 Gemini 官方 countTokens API
当前的 `estimate_tokens()` 是本地粗估（约 chars / 1.5），精度不够。

Gemini 提供**免费**的精确 token 计数 REST API：
```
POST https://generativelanguage.googleapis.com/v1beta/models/{model}:countTokens?key={api_key}
```

**两种使用方式**：
1. **预估（压缩前）**：用 `countTokens` 计算 `compress_tokens` → 精确的 maxOutputTokens
2. **后验（压缩后）**：从响应的 `usage_metadata.candidates_token_count` 直接获取实际输出 token 数

建议优先使用方式 2（零额外 API 调用），方式 1 作为辅助。

### 现状分析

当前 `_call_flash_lite()` 中 `maxOutputTokens` 硬编码为 4096 (`main.py:L1455`)：

```python
_gen_config = {
    "temperature": 0.3,
    "maxOutputTokens": 4096,  # ← 硬编码
    ...
}
```

压缩时调用链：`compress_if_needed()` → `flash_lite_caller(prompt)` → `_call_flash_lite(prompt)`

问题：`_call_flash_lite` 不接受 `max_tokens` 参数，无法从 checkpoint.py 传入动态值。

### 修复方案

#### 步骤 1：扩展 _call_flash_lite 签名

```python
async def _call_flash_lite(
    self, prompt: str, max_output_tokens: int = 4096
) -> str:
    ...
    _gen_config = {
        "temperature": 0.3,
        "maxOutputTokens": max_output_tokens,  # ← 动态
        ...
    }
```

#### 步骤 2：compress_if_needed 传入计算好的 max_tokens（含 Δ）

```python
# checkpoint.py compress_if_needed() 中
raw_max = max(100, int(compress_tokens * target_max))
delta = max(50, int(raw_max * 0.15))  # 15% 余量
max_output_tokens = raw_max + delta

compressed_text = await flash_lite_caller(prompt, max_output_tokens=max_output_tokens)
```

#### 步骤 3：更新 Prompt —— 去除一切限制，鼓励详细

```python
# 旧 Prompt（删除）
"- 你的输出必须在 {min_chars} 到 {max_chars} 字之间"
"- 过短（< {min_chars} 字）或过长（> {max_chars} 字）都是失败"

# 新 Prompt
"## 输出要求
- 尽可能详细地保留所有有价值的信息
- 越详细越好，不要省略重要细节
- 系统会自动控制输出长度上限，你无需担心过长"
```

#### 步骤 4（可选优化）：从响应中提取精确 token 数

```python
# _call_flash_lite 返回时同时提取 usage_metadata
result = response_data.get("candidates", [{}])[0]
usage = response_data.get("usageMetadata", {})
actual_output_tokens = usage.get("candidatesTokenCount", 0)
# 用 actual_output_tokens 替代 estimate_tokens() 做后验证
```

### 效果对比

| 维度 | 当前方案 | 改进后 |
|---|---|---|
| 上限保证 | Prompt 文字要求（soft） | API maxOutputTokens + Δ（hard） |
| 下限保证 | Prompt 文字要求（soft） | Prompt 鼓励详细（soft，模型自然逼近上限） |
| 超出处理 | 只打 warning | 物理不可能超出上限 |
| 压缩质量 | 可能过短过长 | 趋向最大可用空间 → 信息保留最大化 |
| Token 精度 | 本地估算 (~chars/1.5) | Gemini 官方精确值 |

---

## 🟢 P2-1：增量消息提取健壮化

### 问题描述

`_extract_new_messages()` (`main.py:L3015-L3032`) 假设 `req.contexts` 长度只增不减。如果 AstrBot 自行 `truncate_turns`，`len(contexts) < processed_count` 导致此后所有新消息不会被检测到。

### 影响评估

在我们的三系统分立架构下，实际影响有限：
- AstrBot 的截断不影响 T 文件本身
- 但 T 文件可能漏记一些消息
- 在极端场景下（AstrBot 大幅截断历史）可能导致漂移

### 修复方案（渐进式）

#### 方案 A（最小改动）：降级处理
```python
def _extract_new_messages(self, contexts, t_file):
    existing_count = len(t_file.get("messages", []))
    compressed_count = t_file["T1"].get("original_msg_count", 0)
    processed_count = compressed_count + existing_count

    if not contexts:
        return []

    if len(contexts) > processed_count:
        return contexts[processed_count:]
    
    # 新增：截断降级 —— 对比最后几条消息内容
    if len(contexts) < processed_count and len(contexts) > 0:
        logger.warning(
            f"[T-FILE] AstrBot 可能截断了历史: "
            f"contexts={len(contexts)} < processed={processed_count}"
        )
        # 尝试从尾部寻找新消息（基于内容比对）
        t_msgs = t_file.get("messages", [])
        if t_msgs:
            last_t_content = t_msgs[-1].get("content", "")
            for i in range(len(contexts) - 1, -1, -1):
                if contexts[i].get("content") == last_t_content:
                    return contexts[i + 1:] if i + 1 < len(contexts) else []
        # 无法对齐，返回空（安全降级）
        return []
    
    return []
```

#### 方案 B（更健壮）：消息指纹
为 T 文件中的消息记录 `(role, content_hash, timestamp)` 三元组指纹，增量时基于指纹匹配而非纯计数。

工程量较大，作为后续优化。

---

## 🟢 P2-2：后端参数校验完善

### 问题描述

`models.py` 中 CHECKPOINT 参数校验不完整：
- `checkpoint_limit` 没有下界（可以设为 0 或负数）
- `checkpoint_target_min` 和 `checkpoint_target_max` 只做各自范围限制，没有校验 `min ≤ max`

### 修复方案

```python
# models.py POST 处理中增加
if hasattr(req, 'checkpoint_limit') and req.checkpoint_limit is not None:
    req.checkpoint_limit = max(1000, min(req.checkpoint_limit, 500000))

# target_min ≤ target_max 校验
if (req.checkpoint_target_min is not None and 
    req.checkpoint_target_max is not None and
    req.checkpoint_target_min > req.checkpoint_target_max):
    req.checkpoint_target_min, req.checkpoint_target_max = (
        req.checkpoint_target_max, req.checkpoint_target_min
    )
```

---

## 🟢 P2-3：并发安全增强

### 问题描述

`compress_if_needed()` 中，load → extract → append → compress → save 多步操作不在一个完整的锁区间内。`append_messages()` 有锁，`save()` 也在锁内，但中间的 compress 计算和 Flash Lite 调用在锁外。

### 影响

同一窗口并发请求时（罕见场景），可能出现：
- 一个请求的压缩结果覆盖另一个请求刚 append 的新消息
- 两次 compress 同时进行，双方覆盖对方

### 修复方案

把整个 `on_llm_request` 的 T 文件操作包在窗口级锁中：

```python
# main.py on_llm_request 中
async with self._t_file_mgr._get_lock(window_key):
    t_file = await self._t_file_mgr.load(window_key)
    new_msgs = self._extract_new_messages(req.contexts, t_file)
    if new_msgs:
        t_file = await self._t_file_mgr.append_messages(window_key, new_msgs)
    t_file, result = await self._t_file_mgr.compress_if_needed(...)
    req.contexts = self._t_file_mgr.build_llm_contexts(t_file)
```

> ⚠️ 注意：compress 过程中调用 Flash Lite 是异步 I/O，在锁内持续时间较长（数秒），但由于是 per-window 锁，只阻塞同窗口的请求，不影响其他窗口。实际场景中同窗口并发请求极其罕见。

---

## 🟢 P2-4：系统认知提示词更新

### 问题描述

`main.py:L2526-L2532` 的系统认知文本仍描述旧架构（"实际对话历史是 AstrBot 维护的直接交互记录"），但实际输入已经是 T 文件替换后的上下文。

### 修复方案

更新该段说明，明确"实际聊天上下文来自 FlashLite 的 T 文件智能压缩系统"。

---

## 🟢 P2-5：agent.py 废弃桩清理

### 问题描述

`agent.py` 中仍保留多处废弃痕迹：
- 顶部注释描述旧架构（"CHECKPOINT压缩摘要 + 最近~10条消息"）
- 构造函数持有 `checkpoint_mgr` 和 `_checkpoint`
- `build_contents()` 保留占位
- `_get_checkpoint_summary()` 废弃桩返回 None

### 修复方案

1. 更新顶部注释到现状
2. 删除对 `checkpoint_mgr` 的持有（若无外部依赖）
3. 在 `_get_checkpoint_summary()` 上标注 `@deprecated`
4. `build_contents()` 如无调用直接删除

---

## 🟢 P2-6：Plan 文档默认值同步

### 问题描述

- `Plan_2_CP_compression.md:L19-L26` 参数默认值仍是 50000/10/0.7
- 实际 config.json 是 10000/15/0.6（主人已在面板修改）
- 前端 HTML 的 `value` 属性仍写 50000/10/0.7（启动时被 API 返回值覆盖，无实际影响）

### 修复方案

- 更新文档默认值为当前实际值
- 或者在文档中明确标注"默认值以面板实际配置为准"

---

## 🟢 P2-7：压缩分割点语义完整性

### 来源

主人在 Review 中指出的优化点：
> "你可以比 T 中前面百分之多少 T' 被送入压缩这部分额外多选一两条保证分割处语义完整性"

### 当前实现

`compress_count` 直接按比例切割，不考虑消息边界的语义完整性。

### 改进方案

在计算 `compress_count` 后，向后扫描 1-2 条消息，如果下一条是 assistant 回复（即当前 compress_count 正好把 user-assistant 对切开），则多包含 1 条 assistant 以保证对话对完整：

```python
compress_count = max(1, int(len(candidate) * compress_front_ratio))
compress_count = min(compress_count, len(candidate) - keep_recent)

# 语义完整性：确保不切开 user-assistant 对话对
if compress_count < len(candidate):
    next_msg = candidate[compress_count]
    if next_msg.get("role") == "assistant":
        # 上一条是 user，一问一答不应该被切开
        prev_msg = candidate[compress_count - 1] if compress_count > 0 else None
        if prev_msg and prev_msg.get("role") == "user":
            compress_count += 1  # 多包含这条 assistant
```

---

## 总览：修复优先级排序

| 优先级 | 编号 | 问题 | 估计工作量 |
|---|---|---|---|
| 🔴 P0 | P0-1 | 参数命名断裂 | 5 分钟 |
| 🔴 P0 | P0-2 | 旧 check_and_compress 调用 | 5 分钟 |
| 🔴 P0 | P0-3 | 压缩边界 Bug | 15 分钟 |
| 🔴 P1 | P1-1 | FlashLite 上下文**必须**切 T 文件 | 20 分钟 |
| 🟡 P1 | P1-2 | 回复后回写 T 文件 | 15 分钟 |
| 🟡 P1 | P1-3 | max_tokens 硬保证压缩率（含 Δ + countTokens） | 30 分钟 |
| 🟢 P2 | P2-1 | 增量提取健壮化 | 15 分钟 |
| 🟢 P2 | P2-2 | 后端参数校验 | 5 分钟 |
| 🟢 P2 | P2-3 | 并发安全增强 | 10 分钟 |
| 🟢 P2 | P2-4 | 系统认知提示词 | 5 分钟 |
| 🟢 P2 | P2-5 | agent.py 清理 | 10 分钟 |
| 🟢 P2 | P2-6 | 文档默认值同步 | 5 分钟 |
| 🟢 P2 | P2-7 | 压缩分割语义完整性 | 10 分钟 |

**预估总工作量**：约 2-2.5 小时（全部修复）
